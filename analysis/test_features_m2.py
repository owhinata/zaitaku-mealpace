"""features_m2.py を合成セッションで検証する（定義は docs/decisions/0020）。

実行: python -m unittest discover -s analysis -v
合成セッションは test_features.make_session で tempfile に作る。data/ と実データは使わない。
M1 の features.py と同じ合成セッションで両方を呼び、窓の格子・IMU 14 次元・valid が一致することを固定する。
"""
from __future__ import annotations
import contextlib, io, sys, unittest
from pathlib import Path

import numpy as np

import features
import features_m2 as m2
from test_features import (TmpCase, make_session, imu_t_ms, sine, silence, on_axis, intersects,
                           read_chunks, write_chunks, HZ)

NAMES = features.FEATURE_NAMES
I_IMU = slice(0, features.N_IMU_FEATURES)          # 0〜13
I_AUDIO = slice(features.N_IMU_FEATURES, 29)       # 14〜28
I_MFCC = slice(NAMES.index("mfcc_0"), NAMES.index("mfcc_12") + 1)
I_CENTROID = NAMES.index("spectral_centroid_hz")
I_ZCR = NAMES.index("zero_crossing_rate")


class ConstantsTest(unittest.TestCase):
    def test_feature_set_and_names(self):
        self.assertEqual(m2.FEATURE_SET, "m2-0020")
        self.assertIs(m2.FEATURE_NAMES, features.FEATURE_NAMES)
        self.assertEqual(m2.FEATURE_NAMES, features.FEATURE_NAMES)
        self.assertEqual(m2.N_FEATURES, 29)
        self.assertEqual(len(m2.FEATURE_NAMES), 29)

    def test_d_prime_constants(self):
        # docs/decisions/0020 の表。8 kHz、窓 8000、フレーム 200 / stride 200、n_fft 256、メル 0〜4000 Hz
        self.assertEqual((m2.AUDIO_HZ_IN, m2.AUDIO_HZ, m2.DECIMATION), (16000, 8000, 2))
        self.assertEqual((m2.WINDOW_SAMPLES, m2.HOP_SAMPLES), (8000, 2000))
        self.assertEqual((m2.FRAME_LEN, m2.FRAME_HOP, m2.N_FFT), (200, 200, 256))
        self.assertEqual((m2.N_MEL, m2.N_MFCC, m2.PREEMPHASIS), (26, 13, 0.97))
        self.assertEqual((m2.MEL_FMIN_HZ, m2.MEL_FMAX_HZ), (0.0, 4000.0))
        self.assertEqual(m2.HOP_SAMPLES % m2.FRAME_HOP, 0)   # 1 ホップがフレームの整数倍（10 フレーム）
        self.assertEqual(m2.HOP_SAMPLES // m2.FRAME_HOP, 10)

    def test_frame_grid(self):
        idx = m2._frame_index()
        self.assertEqual(idx.shape, (40, 200))                 # 1 窓 40 フレーム
        np.testing.assert_array_equal(idx[:, 0], 200 * np.arange(40))
        self.assertEqual(idx[-1, -1], 7999)                    # 重なりなしで窓をちょうど使い切る
        self.assertEqual(len(m2._FFT_FREQS), 129)
        self.assertAlmostEqual(float(m2._FFT_FREQS[1]), 31.25)
        self.assertEqual(m2._MEL_FB.shape, (26, 129))
        self.assertEqual(m2._DCT.shape, (13, 26))


class DecimateTest(unittest.TestCase):
    def test_exact_integer_average_with_negatives(self):
        x = np.array([0, 1, -1, 0, -3, 0, 5, 6, -32768, 32767, 32767, 32767, -32768, -32768, 7, -8],
                     dtype=np.int16)
        got = m2._decimate(x)
        self.assertEqual(got.dtype, np.int16)
        # Python の >> は算術シフト（負は −∞ 方向に丸める）。装置の int32 の (a + b) >> 1 と同じ
        expected = [(int(x[2 * i]) + int(x[2 * i + 1])) >> 1 for i in range(len(x) // 2)]
        self.assertEqual(got.tolist(), expected)
        self.assertEqual(got.tolist()[:4], [0, -1, -2, 5])       # (0+1)>>1 = 0、(-1+0)>>1 = -1、(-3+0)>>1 = -2
        self.assertEqual(got.tolist()[4:7], [-1, 32767, -32768])  # int16 の端で溢れない

    def test_odd_length_drops_the_last_sample(self):
        x = np.arange(-5, 6, dtype=np.int16)   # 11 サンプル
        got = m2._decimate(x)
        self.assertEqual(len(got), 5)
        self.assertEqual(got.tolist(), [(-5 + -4) >> 1, (-3 + -2) >> 1, (-1 + 0) >> 1, (1 + 2) >> 1, (3 + 4) >> 1])
        self.assertEqual(len(m2._decimate(np.zeros(0, dtype=np.int16))), 0)
        self.assertEqual(len(m2._decimate(np.zeros(1, dtype=np.int16))), 0)

    def test_rejects_non_int16(self):
        with self.assertRaises(ValueError):
            m2._decimate(np.zeros(4, dtype=np.float32))

    def test_to_int16_round_trip(self):
        x = np.array([-32768, -1, 0, 1, 32767], dtype=np.int16)
        f = x.astype(np.float32) / features.AUDIO_SCALE
        np.testing.assert_array_equal(m2._to_int16(f), x)
        with self.assertRaises(ValueError):
            m2._to_int16(np.array([0.5 / 32768.0], dtype=np.float32))   # int16 ÷ 32768 の形ではない


class AudioFormulaTest(unittest.TestCase):
    """窓 1 つ分の波形に対する音の 15 次元（_audio_features）。"""

    @staticmethod
    def window(fn, n16: int = 16000) -> np.ndarray:
        t = np.arange(n16) / HZ
        pcm = np.round(fn(t) * 32767).astype(np.int16)
        return m2._decimate(pcm).astype(np.float32) / features.AUDIO_SCALE

    def test_sine_1khz(self):
        a = m2._audio_features(self.window(sine(1000)))
        self.assertEqual(a.shape, (15,))
        self.assertTrue(np.isfinite(a).all())
        self.assertAlmostEqual(float(a[13]), 1000.0, delta=m2._FFT_FREQS[1] / 2)   # 重心はビン 31.25 Hz の中
        self.assertAlmostEqual(float(a[14]), 0.25, delta=0.001)                     # 8 kHz で 1 周期 8 サンプル

    def test_silence(self):
        a = m2._audio_features(np.zeros(8000, dtype=np.float32))
        # log(1e-10) が 26 本すべてに入るので、c0 = log(1e-10) × 26 × sqrt(1/26) = log(1e-10) × sqrt(26)、c1〜c12 は 0
        self.assertAlmostEqual(float(a[0]), np.log(m2.LOG_FLOOR) * np.sqrt(m2.N_MEL), places=6)
        np.testing.assert_allclose(a[1:13], 0.0, atol=1e-9)
        self.assertEqual(float(a[13]), 0.0)
        self.assertEqual(float(a[14]), 0.0)

    def test_short_window_is_all_zero(self):
        a = m2._audio_features(self.window(sine(1000))[:7999])
        self.assertTrue((a == 0).all())
        self.assertFalse((m2._audio_features(self.window(sine(1000))) == 0).all())

    def test_forty_frames_and_per_frame_preemphasis(self):
        x = self.window(sine(1000)).astype(np.float64)
        p = m2._power_frames(x)
        self.assertEqual(p.shape, (40, 129))
        # フレームごとのプリエンファシス（各フレームの pre[0] = x[0]）と、窓全体に掛けてから切る 0012 の方式は、
        # 2 番目以降のフレームの先頭 1 サンプルだけ違う
        frames = x[m2._frame_index()]
        per_frame = np.empty_like(frames)
        per_frame[:, 0] = frames[:, 0]
        per_frame[:, 1:] = frames[:, 1:] - m2.PREEMPHASIS * frames[:, :-1]
        whole = np.append(x[0], x[1:] - m2.PREEMPHASIS * x[:-1])[m2._frame_index()]
        np.testing.assert_array_equal(per_frame[0], whole[0])
        np.testing.assert_array_equal(per_frame[:, 1:], whole[:, 1:])
        self.assertTrue((per_frame[1:, 0] != whole[1:, 0]).all())
        # 実装が「フレームごと」を使っている（窓全体の方式ではパワーが変わる）
        p_whole = np.fft.rfft(whole * m2._HAMMING, n=m2.N_FFT, axis=1)
        p_whole = (p_whole.real ** 2 + p_whole.imag ** 2) / m2.N_FFT
        spec = np.fft.rfft(per_frame * m2._HAMMING, n=m2.N_FFT, axis=1)
        np.testing.assert_allclose(p, (spec.real ** 2 + spec.imag ** 2) / m2.N_FFT, rtol=1e-12)
        self.assertFalse(np.allclose(p[1:], p_whole[1:]))

    def test_centroid_from_the_same_power_spectrum(self):
        x = self.window(sine(1000)).astype(np.float64)
        p = m2._power_frames(x)
        expected = float(((p @ m2._FFT_FREQS) / p.sum(axis=1)).mean())
        self.assertAlmostEqual(float(m2._audio_features(x)[13]), expected, places=9)


class SessionTest(TmpCase):
    """features.extract_session と同じ合成セッションで両方を呼ぶ。"""

    def both(self, d: Path):
        return features.extract_session(d), m2.extract_session(d)

    def assert_same_grid_imu_valid(self, a, b):
        self.assertEqual(a.session, b.session)
        np.testing.assert_array_equal(a.t_start_s, b.t_start_s)
        self.assertEqual(b.X.shape, (len(a.t_start_s), 29))
        self.assertEqual(b.X.dtype, np.float32)
        np.testing.assert_array_equal(a.X[:, I_IMU], b.X[:, I_IMU])
        np.testing.assert_array_equal(a.valid, b.valid)
        self.assertTrue(np.isfinite(b.X).all())

    def test_grid_and_imu_match_features(self):
        d = make_session(self.root, acc=on_axis(1, sine(5, 0.1)), gyro=lambda t: np.stack(
            [2 * np.sin(2 * np.pi * 3 * t), 1.5 * np.sin(2 * np.pi * 4 * t + 1), np.sin(2 * np.pi * 2 * t + 2)], axis=1),
            audio=sine(1000))
        a, b = self.both(d)
        self.assertEqual(b.X.shape, (37, 29))
        self.assert_same_grid_imu_valid(a, b)
        self.assertTrue(b.valid.all())
        self.assertTrue((b.X[:, I_IMU] != 0).any())

    def test_nonzero_start_and_later_stream(self):
        for name, imu0, audio0 in (("a", 123456, 123456), ("b", 1000, 1500), ("c", 1800, 1500)):
            a, b = self.both(make_session(self.root, name, imu_t0_ms=imu0, audio_t0_ms=audio0, audio=sine(1000)))
            self.assert_same_grid_imu_valid(a, b)
            self.assertAlmostEqual(b.t_start_s[0], max(imu0, audio0) / 1000.0, places=9)

    def test_sine_1khz_session(self):
        a, b = self.both(make_session(self.root, audio=sine(1000)))
        self.assert_same_grid_imu_valid(a, b)
        np.testing.assert_allclose(b.X[:, I_CENTROID], 1000.0, atol=m2._FFT_FREQS[1] / 2)
        np.testing.assert_allclose(b.X[:, I_ZCR], 0.25, atol=0.001)
        # M1 の値とは違う（16 kHz の ZCR は 0.125）。取り違えの目印は FEATURE_SET
        np.testing.assert_allclose(a.X[:, I_ZCR], 0.125, atol=0.001)

    def test_silence_session(self):
        a, b = self.both(make_session(self.root, audio=silence))
        self.assert_same_grid_imu_valid(a, b)
        np.testing.assert_allclose(b.X[:, NAMES.index("mfcc_0")], np.log(m2.LOG_FLOOR) * np.sqrt(m2.N_MEL), rtol=1e-6)
        np.testing.assert_allclose(b.X[:, NAMES.index("mfcc_1"):NAMES.index("mfcc_12") + 1], 0.0, atol=1e-5)
        self.assertTrue((b.X[:, I_CENTROID] == 0).all())
        self.assertTrue((b.X[:, I_ZCR] == 0).all())

    def test_decimated_samples_keep_the_16khz_time(self):
        # 500 Hz → 3000 Hz の切り替えを 5.0 秒に置き、IMU を 1.0 秒から始めて窓のグリッドを IMU で決める（features.py の f10 と同じ）。
        # 8 kHz のサンプル j の時刻が 16 kHz のサンプル 2j の時刻なら、窓 [4, 5) は 500 Hz だけ、[5, 6) は 3000 Hz だけになる
        def audio(t):
            return np.where(t < 5.0, sine(500)(t), sine(3000)(t))
        a, b = self.both(make_session(self.root, imu_t0_ms=1000, audio=audio))
        self.assert_same_grid_imu_valid(a, b)
        self.assertEqual(b.X.shape, (33, 29))
        c = {float(s): float(v) for s, v in zip(np.round(b.t_start_s, 9), b.X[:, I_CENTROID])}
        self.assertAlmostEqual(c[2.0], 500.0, delta=m2._FFT_FREQS[1] / 2)
        self.assertAlmostEqual(c[6.0], 3000.0, delta=m2._FFT_FREQS[1] / 2)
        self.assertAlmostEqual(c[4.0], c[2.0], delta=0.01)
        self.assertAlmostEqual(c[5.0], c[6.0], delta=0.01)
        self.assertTrue(c[2.0] + 100 < c[4.5] < c[6.0] - 100, c[4.5])

    def test_imu_rows_dropped_same_valid(self):
        t = imu_t_ms()
        drop = np.zeros(len(t), dtype=bool)
        drop[400:430] = True
        d = make_session(self.root, acc=on_axis(1, sine(5, 0.1)), audio=sine(1000), drop_imu=lambda t_ms: drop)
        a, b = self.both(d)
        self.assert_same_grid_imu_valid(a, b)
        expected_bad = intersects(b.t_start_s, t[399] / 1000.0, t[430] / 1000.0)
        self.assertEqual(int(expected_bad.sum()), 5)
        np.testing.assert_array_equal(b.valid, ~expected_bad)

    def test_imu_3_seconds_dropped_same_valid(self):
        d = make_session(self.root, audio=sine(1000), drop_imu=lambda t_ms: (t_ms >= 4000) & (t_ms < 7000))
        a, b = self.both(d)
        self.assert_same_grid_imu_valid(a, b)
        k = int(np.argmin(np.abs(b.t_start_s - 5.0)))
        self.assertFalse(b.valid[k])
        self.assertTrue((b.X[k, I_IMU] == 0).all())
        self.assertFalse((b.X[k, I_AUDIO] == 0).all())   # 音声は揃っているので音の 15 次元は計算されている

    def test_audio_chunks_dropped_same_valid(self):
        def audio(t):
            return np.where(t < 6.0, sine(500)(t), sine(3000)(t))
        d = make_session(self.root, audio=audio, drop_chunks=range(1000, 1025))
        a, b = self.both(d)
        self.assert_same_grid_imu_valid(a, b)
        expected_bad = intersects(b.t_start_s, 4.000, 4.100)
        np.testing.assert_array_equal(b.t_start_s[expected_bad], [3.25, 3.5, 3.75, 4.0])
        np.testing.assert_array_equal(b.valid, ~expected_bad)
        c = b.X[:, I_CENTROID]
        for s, hz in ((2.0, 500.0), (4.25, 500.0), (5.0, 500.0), (6.0, 3000.0), (9.0, 3000.0)):
            k = int(np.argmin(np.abs(b.t_start_s - s)))
            self.assertAlmostEqual(float(c[k]), hz, delta=m2._FFT_FREQS[1] / 2, msg=f"窓の開始 {s}")

    def test_chunk_jitter_same_valid(self):
        for name, ms in (("a", 1), ("b", 2)):
            d = make_session(self.root, name, audio=sine(1000))
            rows = read_chunks(d)
            rows[1000][0] += ms
            write_chunks(d, rows)
            a, b = self.both(d)
            self.assert_same_grid_imu_valid(a, b)
        self.assertFalse(b.valid.all())   # 6 ms の差分は飛び（features.py と同じ扱い）

    def test_short_audio_windows_have_zero_audio_features(self):
        # 8.9〜9.5 秒のチャンクを抜く（150 チャンク = 9600 サンプル = 0.6 秒）。飛びの直前と飛びの中に開始がある窓は、
        # 8 kHz のサンプルが WAV の終端までで 8000 に満たないので、音の 15 次元が 0 で valid = False
        d = make_session(self.root, audio=sine(1000), drop_chunks=range(2225, 2375))
        a, b = self.both(d)
        self.assert_same_grid_imu_valid(a, b)
        short16 = (a.X[:, I_AUDIO] == 0).all(axis=1)
        short8 = (b.X[:, I_AUDIO] == 0).all(axis=1)
        self.assertGreater(int(short8.sum()), 0)
        self.assertLess(int(short8.sum()), len(short8))
        np.testing.assert_array_equal(short8, short16)           # 16000 に満たない窓と 8000 に満たない窓は同じ
        self.assertFalse(b.valid[short8].any())
        self.assertTrue(np.isfinite(b.X).all())
        # 直接: 8 kHz の窓の音声が 7999 サンプル → 全部 0
        self.assertTrue((m2._audio_features(np.zeros(7999, dtype=np.float32)) == 0).all())

    def test_short_last_chunk_and_odd_wav_length(self):
        # 最終チャンクが 32 サンプル短い（features.py と同じ扱い）。WAV の長さが奇数（間引きで最後の 1 サンプルを捨てる）
        for name, trim in (("even", 32), ("odd", 33)):
            a, b = self.both(make_session(self.root, name, imu_dur_ms=10500, wav_trim=trim, audio=sine(1000)))
            self.assert_same_grid_imu_valid(a, b)
            self.assertEqual(len(b.t_start_s), 36)
            self.assertTrue(b.valid.all())

    def test_validation_is_shared_with_features(self):
        for kw in ({"wav_hz": 8000}, {"meta": {"audio_hz": 8000}}, {"channels": 2}):
            d = make_session(self.root, "x" + "".join(kw), imu_dur_ms=3000, n_samples=48000, **kw)
            with self.assertRaises(ValueError):
                m2.extract_session(d)

    def test_no_waveform_length_arrays(self):
        b = m2.extract_session(make_session(self.root, audio=sine(1000)))
        n = len(b.t_start_s)
        self.assertEqual(b.t_start_s.shape, (n,))
        self.assertEqual(b.X.shape, (n, 29))
        self.assertEqual(b.valid.shape, (n,))
        self.assertIsInstance(b, features.WindowFeatures)


class StandardizerTest(TmpCase):
    def test_fit_transform_on_m2_features(self):
        rng = np.random.default_rng(1)
        noise = rng.standard_normal(160000)
        def audio(t):
            return 0.2 * noise[:len(t)] * (0.6 + 0.4 * np.sin(2 * np.pi * 0.3 * t))
        def acc(t):
            return on_axis(1, lambda u: 0.05 * np.sin(2 * np.pi * (2 + 0.5 * u) * u))(t)
        train = m2.extract_session(make_session(self.root, "20260920-100000_self_water", acc=acc, audio=audio))
        s = features.Standardizer.fit([train])
        self.assertEqual(s.sessions, ("20260920-100000_self_water",))
        self.assertEqual((s.mean.shape, s.std.shape), ((29,), (29,)))
        z = s.transform(train.X)
        self.assertEqual((z.shape, z.dtype), ((len(train.t_start_s), 29), np.float32))
        self.assertTrue(np.isfinite(z).all())
        np.testing.assert_allclose(z.astype(np.float64).mean(axis=0), 0.0, atol=1e-3)
        self.assertTrue((s.std >= 1e-12).all())
        with self.assertRaises(ValueError):
            s.transform(np.zeros((3, 28)))


class MainTest(TmpCase):
    def test_main_prints_no_feature_values(self):
        d = make_session(self.root, audio=sine(1000), acc=on_axis(1, sine(5, 0.1)))
        f = m2.extract_session(d)
        out = io.StringIO()
        argv = sys.argv
        sys.argv = ["features_m2.py", str(d)]
        try:
            with contextlib.redirect_stdout(out):
                m2.main()
        finally:
            sys.argv = argv
        n = len(f.t_start_s)
        self.assertEqual(out.getvalue().splitlines(), [
            f"セッション: {f.session}",
            "特徴量の式: m2-0020（音 8000 Hz、フレーム 200 / 200）",
            f"窓の数: {n}（窓 1.0 秒 / ホップ 0.25 秒）",
            "次元: 29",
            f"valid: {n} / {n}",
            f"t_start_s: {f.t_start_s[0]:.3f} 〜 {f.t_start_s[-1]:.3f}",
        ])
        # 特徴量の値（重心 ≈ 1000、ZCR ≈ 0.25、加速度の RMS）は表示に出ない
        text = out.getvalue()
        for v in (f.X[0, I_CENTROID], f.X[0, I_ZCR], f.X[0, NAMES.index("acc_rms_y")]):
            self.assertNotIn(f"{float(v):.3f}", text)
            self.assertNotIn(f"{float(v):.6g}", text)


if __name__ == "__main__":
    unittest.main()
