"""features.py を合成セッションで検証する（定義は docs/decisions/0012）。

実行: python -m unittest discover -s analysis -v
合成セッションは tempfile に作る。data/ と実データは使わない。乱数は seed を固定する。
"""
from __future__ import annotations
import csv, dataclasses, json, tempfile, unittest, wave
from pathlib import Path

import numpy as np

import features

HZ = 16000
CHUNK = 64            # 64 サンプル・4 ms ごと
IMU_PERIOD_MS = 9.48  # 約 105.5 Hz。整数に丸めるので t_ms の差分は 9 と 10 が混ざる
NAMES = features.FEATURE_NAMES
I_AXIS = slice(NAMES.index("acc_axis_x"), NAMES.index("acc_axis_z") + 1)
I_GYRO = [NAMES.index(n) for n in ("gyro_norm_ptp", "gyro_rms_x", "gyro_rms_y", "gyro_rms_z")]
I_CENTROID = NAMES.index("spectral_centroid_hz")
I_ZCR = NAMES.index("zero_crossing_rate")


def sine(freq_hz: float, amp: float = 0.5, phase: float = 0.3):
    return lambda t: amp * np.sin(2 * np.pi * freq_hz * t + phase)


def silence(t):
    return np.zeros_like(t)


def imu_t_ms(t0_ms: int = 0, dur_ms: float = 10000, period_ms: float = IMU_PERIOD_MS) -> np.ndarray:
    k = np.arange(int(np.ceil(dur_ms / period_ms)))
    return t0_ms + np.round(k * period_ms).astype(np.int64)


def make_session(root: Path, name: str = "20260920-120000_self_quiet", *,
                 imu_t0_ms: int = 0, imu_dur_ms: float = 10000, imu_period_ms: float = IMU_PERIOD_MS,
                 acc=None, gyro=None, drop_imu=None,
                 audio_t0_ms: int = 0, n_samples: int = 160000, audio=silence, drop_chunks=None,
                 wav_trim: int = 0, wav_extra: int = 0, wav_hz: int = HZ, channels: int = 1,
                 meta: dict | None = None) -> Path:
    """合成セッションを作る。acc / gyro は秒 → (n,3)、audio は秒 → [-1, 1]。

    drop_imu: 抜く行の bool を返す関数（引数は t_ms）。drop_chunks: 抜くチャンク番号の range
    （tools/record.py と同じく、CSV の行と WAV のサンプルの両方を抜き、以後の sample_index を詰める）。
    """
    d = root / name
    d.mkdir()
    t_ms = imu_t_ms(imu_t0_ms, imu_dur_ms, imu_period_ms)
    t = t_ms / 1000.0
    a = acc(t) if acc else np.zeros((len(t), 3))
    g = gyro(t) if gyro else np.zeros((len(t), 3))
    a = a + np.array([0.0, 0.0, 1.0])   # 重力。窓内の平均で消える
    keep = ~drop_imu(t_ms) if drop_imu else np.ones(len(t), dtype=bool)
    with (d / "imu.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["t_ms", "ax", "ay", "az", "gx", "gy", "gz"])
        for i in np.nonzero(keep)[0]:
            w.writerow([t_ms[i]] + [f"{v:.4f}" for v in (*a[i], *g[i])])

    x = audio(audio_t0_ms / 1000.0 + np.arange(n_samples) / HZ)
    pcm = np.round(x * 32767).astype("<i2")
    chunk_ids = np.arange(n_samples // CHUNK)
    if drop_chunks is not None:
        chunk_ids = np.array([c for c in chunk_ids if c not in drop_chunks])
        pcm = np.concatenate([pcm[c * CHUNK:(c + 1) * CHUNK] for c in chunk_ids])
    if wav_trim:
        pcm = pcm[:-wav_trim]
    if wav_extra:
        pcm = np.concatenate([pcm, np.zeros(wav_extra, dtype="<i2")])
    if channels == 2:
        pcm = np.repeat(pcm, 2)
    with wave.open(str(d / "audio.wav"), "wb") as wv:
        wv.setnchannels(channels); wv.setsampwidth(2); wv.setframerate(wav_hz)
        wv.writeframes(pcm.tobytes())
    write_chunks(d, [(audio_t0_ms + int(c) * 4, i * CHUNK) for i, c in enumerate(chunk_ids)])

    (d / "events.csv").write_text("t_ms,label,note\n", encoding="utf-8")
    m = {"subject": "self", "cond": "quiet", "sample_rates": {"imu_hz": 104, "audio_hz": HZ, "analog_hz": 0},
         "fw": "logger", "imu_hz": 104, "audio_hz": HZ}
    m.update(meta or {})
    (d / "meta.json").write_text(json.dumps(m), encoding="utf-8")
    return d


def write_chunks(d: Path, rows) -> None:
    with (d / "audio_chunks.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["t_ms", "sample_index"])
        w.writerows(rows)


def read_chunks(d: Path) -> list[list[int]]:
    with (d / "audio_chunks.csv").open(encoding="utf-8") as f:
        return [[int(r["t_ms"]), int(r["sample_index"])] for r in csv.DictReader(f)]


def on_axis(axis: int, fn):
    """fn の値を1軸だけに入れた (n,3)。"""
    def f(t):
        out = np.zeros((len(t), 3))
        out[:, axis] = fn(t)
        return out
    return f


def intersects(t_start: np.ndarray, g0: float, g1: float) -> np.ndarray:
    """飛びの区間 (g0, g1) と交わる窓 [s, s + 1.0)。"""
    return (g0 < t_start + 1.0) & (g1 > t_start)


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)


class GridTest(TmpCase):
    def test_f1_window_count_and_shape(self):
        f = features.extract_session(make_session(self.root))
        self.assertEqual(len(NAMES), 29)
        self.assertEqual(f.X.shape, (37, 29))
        self.assertEqual(f.X.dtype, np.float32)
        self.assertEqual(f.t_start_s.shape, (37,))
        self.assertTrue(f.valid.all())
        self.assertEqual(f.session, "20260920-120000_self_quiet")

    def test_f1_nonzero_start_does_not_lose_a_window(self):
        f = features.extract_session(make_session(self.root, imu_t0_ms=123456, audio_t0_ms=123456))
        self.assertEqual(f.X.shape, (37, 29))
        self.assertAlmostEqual(f.t_start_s[0], 123.456, places=9)
        self.assertTrue(f.valid.all())

    def test_f2_grid_on_t_ms_axis(self):
        f = features.extract_session(make_session(self.root, imu_t0_ms=5000, audio_t0_ms=5000))
        np.testing.assert_allclose(f.t_start_s, 5.0 + 0.25 * np.arange(37), atol=1e-9)

    def test_f2_starts_from_the_later_stream(self):
        a = features.extract_session(make_session(self.root, "a", imu_t0_ms=1000, audio_t0_ms=1500))
        self.assertAlmostEqual(a.t_start_s[0], 1.5, places=9)
        np.testing.assert_allclose(np.diff(a.t_start_s), 0.25, atol=1e-9)
        b = features.extract_session(make_session(self.root, "b", imu_t0_ms=1800, audio_t0_ms=1500))
        self.assertAlmostEqual(b.t_start_s[0], 1.8, places=9)
        np.testing.assert_allclose(np.diff(b.t_start_s), 0.25, atol=1e-9)

    def test_f9_no_waveform_length_arrays(self):
        f = features.extract_session(make_session(self.root, audio=sine(1000)))
        n = len(f.t_start_s)
        for field in dataclasses.fields(f):
            v = getattr(f, field.name)
            if isinstance(v, np.ndarray):
                self.assertEqual(v.shape[0], n, field.name)
                self.assertIn(v.shape, ((n,), (n, 29)), field.name)
            else:
                self.assertIsInstance(v, str, field.name)


class AudioFeatureTest(TmpCase):
    def test_f3_sine_1khz(self):
        f = features.extract_session(make_session(self.root, audio=sine(1000)))
        np.testing.assert_allclose(f.X[:, I_CENTROID], 1000.0, atol=15.0)
        np.testing.assert_allclose(f.X[:, I_ZCR], 0.125, atol=0.001)
        self.assertTrue(np.isfinite(f.X).all())

    def test_f3_silence_is_finite(self):
        f = features.extract_session(make_session(self.root, audio=silence))
        self.assertTrue(np.isfinite(f.X).all())
        self.assertTrue((f.X[:, I_CENTROID] == 0).all())
        self.assertTrue((f.X[:, I_ZCR] == 0).all())


class ImuFeatureTest(TmpCase):
    A = 0.1

    def test_f4_sine_on_y(self):
        f = features.extract_session(make_session(self.root, acc=on_axis(1, sine(5, self.A))))
        np.testing.assert_allclose(f.X[:, I_AXIS], np.tile([0, 1, 0], (37, 1)), atol=0.02)
        self.assertTrue((f.X[:, NAMES.index("acc_axis_y")] > 0).all())
        np.testing.assert_allclose(f.X[:, NAMES.index("acc_rms_y")], self.A / np.sqrt(2), rtol=0.03)
        np.testing.assert_allclose(f.X[:, NAMES.index("acc_ptp_y")], 2 * self.A, rtol=0.03)
        peaks = f.X[:, NAMES.index("acc_peak_count")]
        self.assertTrue(((peaks >= 9) & (peaks <= 11)).all(), peaks)

    def test_f4_sine_on_x(self):
        f = features.extract_session(make_session(self.root, acc=on_axis(0, sine(5, self.A))))
        np.testing.assert_allclose(f.X[:, I_AXIS], np.tile([1, 0, 0], (37, 1)), atol=0.02)

    @staticmethod
    def xy(ax: float, ay: float):
        def f(t):
            out = np.zeros((len(t), 3))
            out[:, 0] = ax * np.sin(2 * np.pi * 5 * t)
            out[:, 1] = ay * np.cos(2 * np.pi * 5 * t)
            return out
        return f

    def eigen_gap_ratios(self, acc_fn, period_ms: float) -> np.ndarray:
        """テストの入力そのものの (λ1 − λ2) / λ1 を窓ごとに出す（入力の前提の確認用）。"""
        t = imu_t_ms(period_ms=period_ms) / 1000.0
        a = np.round(acc_fn(t), 4)
        out = []
        for s in 0.25 * np.arange(37):
            w = a[(t >= s) & (t < s + 1.0)]
            w = w - w.mean(axis=0)
            lam = np.linalg.eigvalsh(w.T @ w / len(w))
            out.append((lam[2] - lam[1]) / lam[2])
        return np.array(out)

    def test_f4_equal_variance_gives_default_axis(self):
        # 同じ分散で無相関。IMU を 10 ms ちょうどの周期にして、どの窓にも 5 Hz の整数周期（100 行）が入るようにする。
        # 9.48 ms の周期だと窓の行数が 105 / 106 で端数が残り、比が閾値 0.01 の境目に来る
        fn = self.xy(self.A, self.A)
        ratios = self.eigen_gap_ratios(fn, 10.0)
        self.assertLess(ratios.max(), 0.003)
        f = features.extract_session(make_session(self.root, acc=fn, imu_period_ms=10.0))
        self.assertEqual(f.X.shape, (37, 29))
        self.assertTrue((f.X[:, I_AXIS] == np.array([0, 1, 0], dtype=np.float32)).all())

    def test_f4_clear_gap_gives_eigenvector(self):
        # 境目の反対側: X の分散が Y よりはっきり大きい（比 0.36）→ 既定値ではなく固有ベクトル（X 軸）
        fn = self.xy(self.A, 0.8 * self.A)
        ratios = self.eigen_gap_ratios(fn, 10.0)
        self.assertGreater(ratios.min(), 0.3)
        f = features.extract_session(make_session(self.root, acc=fn, imu_period_ms=10.0))
        np.testing.assert_allclose(f.X[:, I_AXIS], np.tile([1, 0, 0], (37, 1)), atol=0.02)

    def test_f4_still(self):
        f = features.extract_session(make_session(self.root))
        self.assertTrue(np.isfinite(f.X).all())
        self.assertTrue((f.X[:, I_AXIS] == np.array([0, 1, 0], dtype=np.float32)).all())
        self.assertTrue((f.X[:, :NAMES.index("acc_axis_x")] == 0).all())

    def test_f5_gyro_offset_is_removed(self):
        def gyro(offset):
            def f(t):
                return np.stack([2 * np.sin(2 * np.pi * 3 * t), 1.5 * np.sin(2 * np.pi * 4 * t + 1),
                                 np.sin(2 * np.pi * 2 * t + 2)], axis=1) + offset
            return f
        a = features.extract_session(make_session(self.root, "a", gyro=gyro(0.0)))
        b = features.extract_session(make_session(self.root, "b", gyro=gyro(1.0)))
        self.assertTrue((a.X[:, I_GYRO] > 0.5).all())
        np.testing.assert_allclose(a.X[:, I_GYRO], b.X[:, I_GYRO], atol=1e-3)


class GapTest(TmpCase):
    def test_f6_imu_30_rows_dropped(self):
        t = imu_t_ms()
        drop = np.zeros(len(t), dtype=bool)
        drop[400:430] = True
        d = make_session(self.root, acc=on_axis(1, sine(5, 0.1)), audio=sine(1000),
                         drop_imu=lambda t_ms: drop)
        f = features.extract_session(d)
        self.assertEqual(f.X.shape, (37, 29))
        np.testing.assert_allclose(np.diff(f.t_start_s), 0.25, atol=1e-9)
        expected_bad = intersects(f.t_start_s, t[399] / 1000.0, t[430] / 1000.0)
        self.assertEqual(int(expected_bad.sum()), 5)
        np.testing.assert_array_equal(f.valid, ~expected_bad)
        self.assertTrue(np.isfinite(f.X).all())

    def test_f6_imu_3_seconds_dropped(self):
        d = make_session(self.root, audio=sine(1000), drop_imu=lambda t_ms: (t_ms >= 4000) & (t_ms < 7000))
        f = features.extract_session(d)
        self.assertEqual(f.X.shape, (37, 29))
        self.assertTrue(np.isfinite(f.X).all())
        k = int(np.argmin(np.abs(f.t_start_s - 5.0)))   # 窓 [5, 6) には IMU が 0 行
        self.assertFalse(f.valid[k])
        self.assertTrue((f.X[k, :14] == 0).all())
        self.assertTrue(f.valid[0] and f.valid[-1])

    def test_f6_audio_25_chunks_dropped(self):
        # 周波数の切り替えは飛び（4.0〜4.1 秒）ではなく 6.0 秒に置く。時刻を sample_index だけで数えると、
        # 窓 [5, 6) に 0.1 秒ぶんの 3000 Hz が混ざって重心が外れる
        def audio(t):
            return np.where(t < 6.0, sine(500)(t), sine(3000)(t))
        d = make_session(self.root, audio=audio, drop_chunks=range(1000, 1025))
        f = features.extract_session(d)
        self.assertEqual(f.X.shape, (37, 29))
        np.testing.assert_allclose(np.diff(f.t_start_s), 0.25, atol=1e-9)
        expected_bad = intersects(f.t_start_s, 3.996, 4.100)
        self.assertEqual(int(expected_bad.sum()), 5)   # 開始 3.0〜4.0
        np.testing.assert_array_equal(f.valid, ~expected_bad)
        self.assertTrue(np.isfinite(f.X).all())
        c = f.X[:, I_CENTROID]
        for s, hz in ((2.0, 500.0), (4.25, 500.0), (5.0, 500.0), (6.0, 3000.0), (9.0, 3000.0)):
            k = int(np.argmin(np.abs(f.t_start_s - s)))
            self.assertAlmostEqual(f.t_start_s[k], s, places=9)
            self.assertAlmostEqual(float(c[k]), hz, delta=20.0, msg=f"窓の開始 {s}")


class StandardizerTest(TmpCase):
    def session(self, name: str, seed: int, scale: float):
        rng = np.random.default_rng(seed)
        noise = rng.standard_normal(160000)
        def audio(t):
            return scale * 0.2 * noise[:len(t)] * (0.6 + 0.4 * np.sin(2 * np.pi * 0.3 * t))
        def acc(t):
            return on_axis(1, lambda u: scale * 0.05 * np.sin(2 * np.pi * (2 + 0.5 * u) * u))(t)
        return features.extract_session(make_session(self.root, name, acc=acc, audio=audio))

    def test_f7_fit_uses_only_given_sessions(self):
        train = self.session("20260920-100000_self_water", 1, 1.0)
        eval_a = self.session("20260921-100000_self_water", 2, 1.0)
        eval_b = self.session("20260922-100000_self_water", 3, 3.0)
        self.assertFalse(np.allclose(eval_a.X, eval_b.X))
        s = features.Standardizer.fit([train])
        self.assertEqual(s.sessions, ("20260920-100000_self_water",))
        self.assertEqual(s.mean.shape, (29,))
        # 評価側の値が何であっても、学習側だけの統計量は同じ。評価側を渡せば変わる
        raw_mean, raw_std = train.X.astype(np.float64).mean(axis=0), train.X.astype(np.float64).std(axis=0)
        np.testing.assert_allclose(s.mean, raw_mean)
        self.assertFalse(np.allclose(features.Standardizer.fit([train, eval_b]).mean, s.mean))
        self.assertEqual(features.Standardizer.fit([train, eval_b]).sessions, (train.session, eval_b.session))

        z = s.transform(train.X).astype(np.float64)
        self.assertTrue(np.isfinite(z).all())
        np.testing.assert_allclose(z.mean(axis=0), 0.0, atol=1e-3)
        flat = raw_std == 0
        self.assertTrue(flat.any() and (~flat).any())   # acc_rms_x などは分散 0
        np.testing.assert_allclose(z.std(axis=0)[~flat], 1.0, atol=1e-3)
        self.assertTrue((z[:, flat] == 0).all())
        self.assertTrue((s.std[flat] == 1).all())
        self.assertTrue(np.isfinite(s.transform(eval_a.X)).all())

    def test_f7_fit_skips_invalid_windows(self):
        train = self.session("20260920-100000_self_water", 1, 1.0)
        valid = train.valid.copy()
        valid[:10] = False
        part = dataclasses.replace(train, valid=valid)
        np.testing.assert_allclose(features.Standardizer.fit([part]).mean,
                                   train.X[10:].astype(np.float64).mean(axis=0))

    def test_f8_fit_without_valid_windows(self):
        f = features.extract_session(make_session(self.root))
        none = dataclasses.replace(f, valid=np.zeros_like(f.valid))
        with self.assertRaises(ValueError):
            features.Standardizer.fit([none])
        with self.assertRaises(ValueError):
            features.Standardizer.fit([])

    def test_f8_transform_wrong_dim(self):
        f = features.extract_session(make_session(self.root))
        s = features.Standardizer.fit([f])
        with self.assertRaises(ValueError):
            s.transform(np.zeros((5, 28)))
        self.assertEqual(s.transform(np.zeros(29)).shape, (29,))


class ValidationTest(TmpCase):
    N = 48000   # 3 秒

    def make(self, **kw) -> Path:
        kw.setdefault("imu_dur_ms", 3000)
        kw.setdefault("n_samples", self.N)
        return make_session(self.root, **kw)

    def assert_rejected(self, d: Path):
        with self.assertRaises(ValueError):
            features.extract_session(d)

    def test_f8_base_session_is_accepted(self):
        self.assertEqual(features.extract_session(self.make()).X.shape, (9, 29))

    def test_f8_wav_8khz(self):
        self.assert_rejected(self.make(wav_hz=8000))

    def test_f8_meta_8khz(self):
        self.assert_rejected(self.make(meta={"audio_hz": 8000}))

    def test_f8_meta_fallback_to_sample_rates(self):
        d = self.make()
        (d / "meta.json").write_text(json.dumps({"sample_rates": {"audio_hz": 16000}}), encoding="utf-8")
        self.assertEqual(features.extract_session(d).X.shape, (9, 29))
        (d / "meta.json").write_text(json.dumps({"sample_rates": {"audio_hz": 8000}}), encoding="utf-8")
        self.assert_rejected(d)

    def test_f8_wav_stereo(self):
        self.assert_rejected(self.make(channels=2))

    def test_f8_sample_index_goes_back(self):
        d = self.make()
        rows = read_chunks(d)
        rows[10][1], rows[11][1] = rows[11][1], rows[10][1]
        write_chunks(d, rows)
        self.assert_rejected(d)

    def test_f8_first_sample_index_not_zero(self):
        d = self.make()
        write_chunks(d, read_chunks(d)[1:])
        self.assert_rejected(d)

    def test_f8_chunk_table_empty_or_single(self):
        d = self.make()
        rows = read_chunks(d)
        write_chunks(d, rows[:1])
        self.assert_rejected(d)
        write_chunks(d, [])
        self.assert_rejected(d)

    def test_f8_wav_longer_than_chunk_table(self):
        self.assert_rejected(self.make(wav_extra=CHUNK))

    def test_f8_wav_shorter_than_last_sample_index(self):
        self.assert_rejected(self.make(wav_trim=CHUNK))
        self.assert_rejected(make_session(self.root, "b", imu_dur_ms=3000, n_samples=self.N, wav_trim=100))

    def test_f8_chunk_t_ms_goes_back(self):
        d = self.make()
        rows = read_chunks(d)
        rows[20][0] -= 50
        write_chunks(d, rows)
        self.assert_rejected(d)

    def test_f8_imu_t_ms_goes_back(self):
        d = self.make()
        lines = (d / "imu.csv").read_text(encoding="utf-8").splitlines()
        lines[50], lines[51] = lines[51], lines[50]
        (d / "imu.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assert_rejected(d)

    def test_f8_imu_too_short(self):
        d = self.make()
        lines = (d / "imu.csv").read_text(encoding="utf-8").splitlines()
        (d / "imu.csv").write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")
        self.assert_rejected(d)

    def test_f8_short_last_chunk_is_accepted(self):
        # IMU を長くして t_hi を音声の終端にする。32 サンプル（2 ms）短いと終端が 9.998 秒になり、窓が1つ減る
        full = features.extract_session(make_session(self.root, "full", imu_dur_ms=10500))
        short = features.extract_session(make_session(self.root, "short", imu_dur_ms=10500, wav_trim=32))
        self.assertEqual(len(full.t_start_s), 37)
        self.assertEqual(len(short.t_start_s), 36)
        self.assertTrue(short.valid.all())


if __name__ == "__main__":
    unittest.main()
