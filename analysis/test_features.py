"""features.py を合成セッションで検証する（定義は docs/decisions/0012）。

実行: python -m unittest discover -s analysis -v
合成セッションは tempfile に作る。data/ と実データは使わない。乱数は seed
を固定する。
"""

from __future__ import annotations
import csv, dataclasses, json, tempfile, unittest, wave
from pathlib import Path

import numpy as np

import features

HZ = 16000
CHUNK = 64  # 64 サンプル・4 ms ごと
# 約 105.5 Hz。整数に丸めるので t_ms の差分は 9 と 10 が混ざる
IMU_PERIOD_MS = 9.48
NAMES = features.FEATURE_NAMES
I_AXIS = slice(NAMES.index("acc_axis_x"), NAMES.index("acc_axis_z") + 1)
I_GYRO = [
    NAMES.index(n)
    for n in ("gyro_norm_ptp", "gyro_rms_x", "gyro_rms_y", "gyro_rms_z")
]
I_CENTROID = NAMES.index("spectral_centroid_hz")
I_ZCR = NAMES.index("zero_crossing_rate")


def sine(freq_hz: float, amp: float = 0.5, phase: float = 0.3):
    return lambda t: amp * np.sin(2 * np.pi * freq_hz * t + phase)


def silence(t):
    return np.zeros_like(t)


def imu_t_ms(
    t0_ms: int = 0, dur_ms: float = 10000, period_ms: float = IMU_PERIOD_MS
) -> np.ndarray:
    k = np.arange(int(np.ceil(dur_ms / period_ms)))
    return t0_ms + np.round(k * period_ms).astype(np.int64)


def make_session(
    root: Path,
    name: str = "20260920-120000_self_quiet",
    *,
    imu_t0_ms: int = 0,
    imu_dur_ms: float = 10000,
    imu_period_ms: float = IMU_PERIOD_MS,
    acc=None,
    gyro=None,
    drop_imu=None,
    audio_t0_ms: int = 0,
    n_samples: int = 160000,
    audio=silence,
    drop_chunks=None,
    chunk_lens: dict | None = None,
    wav_trim: int = 0,
    wav_extra: int = 0,
    wav_hz: int = HZ,
    channels: int = 1,
    meta: dict | None = None,
) -> Path:
    """合成セッションを作る。acc / gyro は秒 → (n,3)、audio は秒 → [-1, 1]。

    drop_imu: 抜く行の bool を返す関数（引数は t_ms）。drop_chunks:
    抜くチャンク番号の range
    （tools/record.py と同じく、CSV の行と WAV のサンプルの両方を抜き、以後の
    sample_index を詰める）。
    chunk_lens: {チャンク番号: サンプル数}。指定の無いチャンクは CHUNK（64）。
    チャンクの t_ms は、装置と同じく「先頭サンプルの時刻 + チャンクの長さぶん」
    （64 サンプルなら + 4 ms。
    docs/decisions/0013）。wav_trim で最終チャンクが短くなるときは、
    その長さぶん。
    """
    d = root / name
    d.mkdir()
    t_ms = imu_t_ms(imu_t0_ms, imu_dur_ms, imu_period_ms)
    t = t_ms / 1000.0
    a = acc(t) if acc else np.zeros((len(t), 3))
    g = gyro(t) if gyro else np.zeros((len(t), 3))
    a = a + np.array([0.0, 0.0, 1.0])  # 重力。窓内の平均で消える
    keep = ~drop_imu(t_ms) if drop_imu else np.ones(len(t), dtype=bool)
    with (d / "imu.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["t_ms", "ax", "ay", "az", "gx", "gy", "gz"])
        for i in np.nonzero(keep)[0]:
            w.writerow([t_ms[i]] + [f"{v:.4f}" for v in (*a[i], *g[i])])

    chunk_ids = np.arange(n_samples // CHUNK)
    lens = np.full(len(chunk_ids), CHUNK, dtype=np.int64)
    for c, n in (chunk_lens or {}).items():
        lens[c] = n
    # 取りこぼしが無いときの先頭サンプルの番号
    starts = np.concatenate([[0], np.cumsum(lens)[:-1]])
    x = audio(audio_t0_ms / 1000.0 + np.arange(int(lens.sum())) / HZ)
    pcm = np.round(x * 32767).astype("<i2")
    if drop_chunks is not None:
        chunk_ids = np.array([c for c in chunk_ids if c not in drop_chunks])
        pcm = np.concatenate(
            [pcm[starts[c] : starts[c] + lens[c]] for c in chunk_ids]
        )
    sent = lens[chunk_ids].copy()  # 装置が送った長さ。t_ms の進みはこの長さぶん
    sent[-1] = max(sent[-1] - wav_trim, 0)
    chunk_t_ms = [
        audio_t0_ms + int(round((starts[c] + n) * 1000 / HZ))
        for c, n in zip(chunk_ids, sent)
    ]
    chunk_si = np.concatenate([[0], np.cumsum(lens[chunk_ids])[:-1]])
    if wav_trim:
        pcm = pcm[:-wav_trim]
    if wav_extra:
        pcm = np.concatenate([pcm, np.zeros(wav_extra, dtype="<i2")])
    if channels == 2:
        pcm = np.repeat(pcm, 2)
    with wave.open(str(d / "audio.wav"), "wb") as wv:
        wv.setnchannels(channels)
        wv.setsampwidth(2)
        wv.setframerate(wav_hz)
        wv.writeframes(pcm.tobytes())
    write_chunks(d, [(int(t), int(i)) for t, i in zip(chunk_t_ms, chunk_si)])

    (d / "events.csv").write_text("t_ms,label,note\n", encoding="utf-8")
    m = {
        "subject": "self",
        "cond": "quiet",
        "sample_rates": {"imu_hz": 104, "audio_hz": HZ, "analog_hz": 0},
        "fw": "logger",
        "imu_hz": 104,
        "audio_hz": HZ,
    }
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
        return [
            [int(r["t_ms"]), int(r["sample_index"])] for r in csv.DictReader(f)
        ]


def chunk_timing(d: Path):
    """features.py の換算の途中の値: (チャンクごとの長さ,
    先頭サンプルの時刻のアンカー ms, 音声の飛びの区間)。"""
    x, si, t_ms, chunk_len = features._load_audio(d)
    lengths = features._chunk_lengths(len(x), si)
    anchor_ms = features._chunk_anchors_ms(t_ms, lengths)
    return (
        lengths,
        anchor_ms,
        features._audio_gaps(anchor_ms, lengths, chunk_len),
    )


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
        f = features.extract_session(
            make_session(self.root, imu_t0_ms=123456, audio_t0_ms=123456)
        )
        self.assertEqual(f.X.shape, (37, 29))
        self.assertAlmostEqual(f.t_start_s[0], 123.456, places=9)
        self.assertTrue(f.valid.all())

    def test_f2_grid_on_t_ms_axis(self):
        f = features.extract_session(
            make_session(self.root, imu_t0_ms=5000, audio_t0_ms=5000)
        )
        np.testing.assert_allclose(
            f.t_start_s, 5.0 + 0.25 * np.arange(37), atol=1e-9
        )

    def test_f2_starts_from_the_later_stream(self):
        a = features.extract_session(
            make_session(self.root, "a", imu_t0_ms=1000, audio_t0_ms=1500)
        )
        self.assertAlmostEqual(a.t_start_s[0], 1.5, places=9)
        np.testing.assert_allclose(np.diff(a.t_start_s), 0.25, atol=1e-9)
        b = features.extract_session(
            make_session(self.root, "b", imu_t0_ms=1800, audio_t0_ms=1500)
        )
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
        f = features.extract_session(
            make_session(self.root, acc=on_axis(1, sine(5, self.A)))
        )
        np.testing.assert_allclose(
            f.X[:, I_AXIS], np.tile([0, 1, 0], (37, 1)), atol=0.02
        )
        self.assertTrue((f.X[:, NAMES.index("acc_axis_y")] > 0).all())
        np.testing.assert_allclose(
            f.X[:, NAMES.index("acc_rms_y")], self.A / np.sqrt(2), rtol=0.03
        )
        np.testing.assert_allclose(
            f.X[:, NAMES.index("acc_ptp_y")], 2 * self.A, rtol=0.03
        )
        peaks = f.X[:, NAMES.index("acc_peak_count")]
        self.assertTrue(((peaks >= 9) & (peaks <= 11)).all(), peaks)

    def test_f4_sine_on_x(self):
        f = features.extract_session(
            make_session(self.root, acc=on_axis(0, sine(5, self.A)))
        )
        np.testing.assert_allclose(
            f.X[:, I_AXIS], np.tile([1, 0, 0], (37, 1)), atol=0.02
        )

    @staticmethod
    def xy(ax: float, ay: float):
        def f(t):
            out = np.zeros((len(t), 3))
            out[:, 0] = ax * np.sin(2 * np.pi * 5 * t)
            out[:, 1] = ay * np.cos(2 * np.pi * 5 * t)
            return out

        return f

    def eigen_gap_ratios(self, acc_fn, period_ms: float) -> np.ndarray:
        """テストの入力そのものの (λ1 − λ2) / λ1 を窓ごとに出す（入力の前提の確
        認用）。"""
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
        # 同じ分散で無相関。IMU を 10 ms ちょうどの周期にして、どの窓にも 5 Hz
        # の整数周期（100 行）が入るようにする。
        # 9.48 ms の周期だと窓の行数が 105 / 106 で端数が残り、比が閾値 0.01
        # の境目に来る
        fn = self.xy(self.A, self.A)
        ratios = self.eigen_gap_ratios(fn, 10.0)
        self.assertLess(ratios.max(), 0.003)
        f = features.extract_session(
            make_session(self.root, acc=fn, imu_period_ms=10.0)
        )
        self.assertEqual(f.X.shape, (37, 29))
        self.assertTrue(
            (f.X[:, I_AXIS] == np.array([0, 1, 0], dtype=np.float32)).all()
        )

    def test_f4_clear_gap_gives_eigenvector(self):
        # 境目の反対側: X の分散が Y よりはっきり大きい（比 0.36）→
        # 既定値ではなく固有ベクトル（X 軸）
        fn = self.xy(self.A, 0.8 * self.A)
        ratios = self.eigen_gap_ratios(fn, 10.0)
        self.assertGreater(ratios.min(), 0.3)
        f = features.extract_session(
            make_session(self.root, acc=fn, imu_period_ms=10.0)
        )
        np.testing.assert_allclose(
            f.X[:, I_AXIS], np.tile([1, 0, 0], (37, 1)), atol=0.02
        )

    def test_f4_still(self):
        f = features.extract_session(make_session(self.root))
        self.assertTrue(np.isfinite(f.X).all())
        self.assertTrue(
            (f.X[:, I_AXIS] == np.array([0, 1, 0], dtype=np.float32)).all()
        )
        self.assertTrue((f.X[:, : NAMES.index("acc_axis_x")] == 0).all())

    def test_f5_gyro_offset_is_removed(self):
        def gyro(offset):
            def f(t):
                return (
                    np.stack(
                        [
                            2 * np.sin(2 * np.pi * 3 * t),
                            1.5 * np.sin(2 * np.pi * 4 * t + 1),
                            np.sin(2 * np.pi * 2 * t + 2),
                        ],
                        axis=1,
                    )
                    + offset
                )

            return f

        a = features.extract_session(
            make_session(self.root, "a", gyro=gyro(0.0))
        )
        b = features.extract_session(
            make_session(self.root, "b", gyro=gyro(1.0))
        )
        self.assertTrue((a.X[:, I_GYRO] > 0.5).all())
        np.testing.assert_allclose(a.X[:, I_GYRO], b.X[:, I_GYRO], atol=1e-3)


class GapTest(TmpCase):
    def test_f6_imu_30_rows_dropped(self):
        t = imu_t_ms()
        drop = np.zeros(len(t), dtype=bool)
        drop[400:430] = True
        d = make_session(
            self.root,
            acc=on_axis(1, sine(5, 0.1)),
            audio=sine(1000),
            drop_imu=lambda t_ms: drop,
        )
        f = features.extract_session(d)
        self.assertEqual(f.X.shape, (37, 29))
        np.testing.assert_allclose(np.diff(f.t_start_s), 0.25, atol=1e-9)
        expected_bad = intersects(f.t_start_s, t[399] / 1000.0, t[430] / 1000.0)
        self.assertEqual(int(expected_bad.sum()), 5)
        np.testing.assert_array_equal(f.valid, ~expected_bad)
        self.assertTrue(np.isfinite(f.X).all())

    def test_f6_imu_3_seconds_dropped(self):
        d = make_session(
            self.root,
            audio=sine(1000),
            drop_imu=lambda t_ms: (t_ms >= 4000) & (t_ms < 7000),
        )
        f = features.extract_session(d)
        self.assertEqual(f.X.shape, (37, 29))
        self.assertTrue(np.isfinite(f.X).all())
        # 窓 [5, 6) には IMU が 0 行
        k = int(np.argmin(np.abs(f.t_start_s - 5.0)))
        self.assertFalse(f.valid[k])
        self.assertTrue((f.X[k, :14] == 0).all())
        self.assertTrue(f.valid[0] and f.valid[-1])

    def test_f6_audio_25_chunks_dropped(self):
        # 周波数の切り替えは飛び（4.0〜4.1 秒）ではなく 6.0 秒に置く。時刻を
        # sample_index だけで数えると、
        # 窓 [5, 6) に 0.1 秒ぶんの 3000 Hz が混ざって重心が外れる
        def audio(t):
            return np.where(t < 6.0, sine(500)(t), sine(3000)(t))

        d = make_session(self.root, audio=audio, drop_chunks=range(1000, 1025))
        f = features.extract_session(d)
        self.assertEqual(f.X.shape, (37, 29))
        np.testing.assert_allclose(np.diff(f.t_start_s), 0.25, atol=1e-9)
        # 生の t_ms は 4000（チャンク 999）と 4104（チャンク 1025）。
        # 飛びの区間は、チャンク 999 の推定の終端 4.000 から
        # チャンク 1025 の推定の先頭 4.100 まで。窓 [3, 4) は交わらない
        rows = read_chunks(d)
        self.assertEqual([rows[999][0], rows[1000][0]], [4000, 4104])
        expected_bad = intersects(f.t_start_s, 4.000, 4.100)
        self.assertEqual(int(expected_bad.sum()), 4)  # 開始 3.25〜4.0
        np.testing.assert_array_equal(
            f.t_start_s[expected_bad], [3.25, 3.5, 3.75, 4.0]
        )
        np.testing.assert_array_equal(f.valid, ~expected_bad)
        lengths, anchor_ms, gaps = chunk_timing(d)
        self.assertEqual(gaps, [(4.000, 4.100)])
        # 飛びの後のアンカー = チャンク 1025 の先頭サンプルの時刻
        self.assertEqual(anchor_ms[1000], 4100.0)
        self.assertTrue(np.isfinite(f.X).all())
        c = f.X[:, I_CENTROID]
        for s, hz in (
            (2.0, 500.0),
            (4.25, 500.0),
            (5.0, 500.0),
            (6.0, 3000.0),
            (9.0, 3000.0),
        ):
            k = int(np.argmin(np.abs(f.t_start_s - s)))
            self.assertAlmostEqual(f.t_start_s[k], s, places=9)
            self.assertAlmostEqual(
                float(c[k]), hz, delta=20.0, msg=f"窓の開始 {s}"
            )


class ChunkTimestampTest(TmpCase):
    """チャンクの t_ms（読み出した直後の時刻）から先頭サンプルの時刻への換算（
    docs/decisions/0013）。"""

    def test_f10_frequency_switch_appears_at_the_right_time(self):
        # IMU を 1.0 秒から始めて、窓のグリッドを音声の先頭ではなく IMU
        # で決める（音声の時刻が 4 ms ずれると、
        # 同じ開始時刻の窓に入るサンプルが 64 個ずれる）。500 Hz → 3000 Hz
        # の切り替えは 5.0 秒
        def audio(t):
            return np.where(t < 5.0, sine(500)(t), sine(3000)(t))

        f = features.extract_session(
            make_session(self.root, imu_t0_ms=1000, audio=audio)
        )
        self.assertEqual(f.X.shape, (33, 29))
        np.testing.assert_allclose(
            f.t_start_s, 1.0 + 0.25 * np.arange(33), atol=1e-9
        )
        self.assertTrue(f.valid.all())
        c = {
            float(s): float(v)
            for s, v in zip(np.round(f.t_start_s, 9), f.X[:, I_CENTROID])
        }
        self.assertAlmostEqual(c[2.0], 500.0, delta=20.0)
        self.assertAlmostEqual(c[6.0], 3000.0, delta=20.0)
        # 窓 [4, 5) は 500 Hz だけ、窓 [5, 6) は 3000 Hz だけ。
        # 切り替えの反対側のサンプルが混ざらない
        self.assertAlmostEqual(c[4.0], c[2.0], delta=0.01)
        self.assertAlmostEqual(c[5.0], c[6.0], delta=0.01)
        # 窓 [4.5, 5.5) は半分ずつ
        self.assertTrue(c[2.0] + 100 < c[4.5] < c[6.0] - 100, c[4.5])

    def test_f10_chunks_of_32_and_96_samples(self):
        d = make_session(
            self.root, audio=sine(1000), chunk_lens={500: 32, 1500: 96}
        )
        rows = read_chunks(d)
        # 32 サンプルぶん = 2 ms
        self.assertEqual([r[0] for r in rows[499:502]], [2000, 2002, 2006])
        # 96 サンプルぶん = 6 ms
        self.assertEqual([r[0] for r in rows[1499:1502]], [5998, 6004, 6008])
        lengths, anchor_ms, gaps = chunk_timing(d)
        self.assertEqual(
            [
                int(lengths[500]),
                int(lengths[1500]),
                int(lengths[0]),
                int(lengths[-1]),
            ],
            [32, 96, 64, 64],
        )
        si = np.array([r[1] for r in rows])
        # 各アンカー = 先頭サンプルの時刻
        np.testing.assert_allclose(anchor_ms, si * 1000.0 / HZ, atol=1e-9)
        t = features._sample_times(160000, si, anchor_ms / 1000.0)
        np.testing.assert_allclose(t, np.arange(160001) / HZ, atol=1e-9)
        np.testing.assert_allclose(np.diff(t), 1.0 / HZ, atol=1e-12)
        # 96 サンプルのチャンクの t_ms の差分 6 ms は飛びではない
        self.assertEqual(gaps, [])
        f = features.extract_session(d)
        self.assertEqual(f.X.shape, (37, 29))
        self.assertTrue(f.valid.all())

    def test_f10_short_last_chunk_anchor(self):
        d = make_session(self.root, wav_trim=32)
        rows = read_chunks(d)
        self.assertEqual([rows[-2][0], rows[-1][0]], [9996, 9998])
        lengths, anchor_ms, gaps = chunk_timing(d)
        self.assertEqual(int(lengths[-1]), 32)
        self.assertEqual(anchor_ms[-1], rows[-1][0] - 2.0)  # t_ms − 2 ms = 9996
        self.assertEqual(gaps, [])

    def jitter(self, name: str, ms: int) -> Path:
        """チャンク 1000 の t_ms だけを ms 遅らせる（前との差分 4 + ms、
        前のチャンクとの空白 ms）。"""
        d = make_session(self.root, name)
        rows = read_chunks(d)
        rows[1000][0] += ms
        write_chunks(d, rows)
        return d

    def test_f10_diff_5ms_is_not_a_gap(self):
        d = self.jitter("a", 1)
        rows = read_chunks(d)
        self.assertEqual(rows[1000][0] - rows[999][0], 5)
        self.assertEqual(chunk_timing(d)[2], [])
        self.assertTrue(features.extract_session(d).valid.all())

    def test_f10_diff_6ms_is_a_gap(self):
        d = self.jitter("b", 2)
        rows = read_chunks(d)
        self.assertEqual(rows[1000][0] - rows[999][0], 6)
        self.assertEqual(chunk_timing(d)[2], [(4.000, 4.002)])
        f = features.extract_session(d)
        expected_bad = intersects(f.t_start_s, 4.000, 4.002)
        np.testing.assert_array_equal(
            f.t_start_s[expected_bad], [3.25, 3.5, 3.75, 4.0]
        )
        np.testing.assert_array_equal(f.valid, ~expected_bad)


class StandardizerTest(TmpCase):
    def session(self, name: str, seed: int, scale: float):
        rng = np.random.default_rng(seed)
        noise = rng.standard_normal(160000)

        def audio(t):
            return (
                scale
                * 0.2
                * noise[: len(t)]
                * (0.6 + 0.4 * np.sin(2 * np.pi * 0.3 * t))
            )

        def acc(t):
            return on_axis(
                1,
                lambda u: scale * 0.05 * np.sin(2 * np.pi * (2 + 0.5 * u) * u),
            )(t)

        return features.extract_session(
            make_session(self.root, name, acc=acc, audio=audio)
        )

    def test_f7_fit_uses_only_given_sessions(self):
        train = self.session("20260920-100000_self_water", 1, 1.0)
        eval_a = self.session("20260921-100000_self_water", 2, 1.0)
        eval_b = self.session("20260922-100000_self_water", 3, 3.0)
        self.assertFalse(np.allclose(eval_a.X, eval_b.X))
        s = features.Standardizer.fit([train])
        self.assertEqual(s.sessions, ("20260920-100000_self_water",))
        self.assertEqual(s.mean.shape, (29,))
        # 評価側の値が何であっても、学習側だけの統計量は同じ。
        # 評価側を渡せば変わる
        raw_mean, raw_std = (
            train.X.astype(np.float64).mean(axis=0),
            train.X.astype(np.float64).std(axis=0),
        )
        np.testing.assert_allclose(s.mean, raw_mean)
        self.assertFalse(
            np.allclose(features.Standardizer.fit([train, eval_b]).mean, s.mean)
        )
        self.assertEqual(
            features.Standardizer.fit([train, eval_b]).sessions,
            (train.session, eval_b.session),
        )

        z = s.transform(train.X).astype(np.float64)
        self.assertTrue(np.isfinite(z).all())
        np.testing.assert_allclose(z.mean(axis=0), 0.0, atol=1e-3)
        flat = raw_std == 0
        self.assertTrue(flat.any() and (~flat).any())  # acc_rms_x などは分散 0
        np.testing.assert_allclose(z.std(axis=0)[~flat], 1.0, atol=1e-3)
        self.assertTrue((z[:, flat] == 0).all())
        self.assertTrue((s.std[flat] == 1).all())
        self.assertTrue(np.isfinite(s.transform(eval_a.X)).all())

    def test_f7_fit_skips_invalid_windows(self):
        train = self.session("20260920-100000_self_water", 1, 1.0)
        valid = train.valid.copy()
        valid[:10] = False
        part = dataclasses.replace(train, valid=valid)
        np.testing.assert_allclose(
            features.Standardizer.fit([part]).mean,
            train.X[10:].astype(np.float64).mean(axis=0),
        )

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
    N = 48000  # 3 秒

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
        (d / "meta.json").write_text(
            json.dumps({"sample_rates": {"audio_hz": 16000}}), encoding="utf-8"
        )
        self.assertEqual(features.extract_session(d).X.shape, (9, 29))
        (d / "meta.json").write_text(
            json.dumps({"sample_rates": {"audio_hz": 8000}}), encoding="utf-8"
        )
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
        self.assert_rejected(
            make_session(
                self.root, "b", imu_dur_ms=3000, n_samples=self.N, wav_trim=100
            )
        )

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
        (d / "imu.csv").write_text(
            "\n".join(lines[:2]) + "\n", encoding="utf-8"
        )
        self.assert_rejected(d)

    def test_f8_short_last_chunk_is_accepted(self):
        # IMU を長くして t_hi を音声の終端にする。32 サンプル（2 ms）
        # 短いと終端が 9.998 秒になり、窓が1つ減る
        full = features.extract_session(
            make_session(self.root, "full", imu_dur_ms=10500)
        )
        short = features.extract_session(
            make_session(self.root, "short", imu_dur_ms=10500, wav_trim=32)
        )
        self.assertEqual(len(full.t_start_s), 37)
        self.assertEqual(len(short.t_start_s), 36)
        self.assertTrue(short.valid.all())


# --- 咽喉マイク（docs/decisions/0031。plan #39 ステップ B のテスト 7〜11） ---

THROAT_FS = 48000
THROAT_PERIOD = 6000  # arecord の 1 ピリオド（125 ms）
THROAT_IFACE = "sh12jk-wired-unoq-usbaudio"


def make_throat_session(
    root: Path,
    name: str = "20261011-100000_self_water",
    *,
    seconds: float = 40.0,
    signal=None,
    noise_lsb: float = 0.0,
    s_true: float = -20e-6,
    ostart_s: float | None = 3.0,
    events=(),
    taps=(),
    tap_delay_ms: float = 3.0,
    delay_jump=None,
    wav_hz: int = THROAT_FS,
    channels: int = 2,
    meta: dict | None = None,
    throat_meta: dict | None = None,
    seed: int = 0,
) -> dict:
    """48 kHz・2 ch の咽喉マイクの合成のセッション（tools/test_throat_check.py と
    同じ作り方）。時刻は「本当の秒」（サンプル i の本当の時刻 i ÷ 48000）で指定する。

    signal: 本当の秒 → [-1, 1]（L と R に同じ値）。noise_lsb: 白色雑音の RMS。
    s_true: クロックの差（取り込みの PC の時刻 = c + 1000 i/fs × (1 + s)）。
    到着 = 各ピリオドの終端の取り込みの時刻 + 遅れ（2 ms + 指数分布 平均 3 ms）。
    delay_jump: (本当の秒, ms)。その時刻より後の到着をすべて ms だけ遅らせる（段差）。
    events: (本当の秒, label, note)。ostart_s が None でなければ o start を足す。
    taps: 叩き（2 ms の減衰パルス）の本当の秒。o tap はパルスの tap_delay_ms 後。
    戻り値: {"to_ms": 本当の秒 → events.csv の t_ms（float）, "n": フレーム数,
    "dir": フォルダ}。
    """
    rng = np.random.default_rng(seed)
    d = root / name
    d.mkdir()
    n = int(round(seconds * THROAT_FS))
    t = np.arange(n) / THROAT_FS
    x = np.zeros(n) if signal is None else 32767.0 * signal(t)
    if noise_lsb:
        x = x + rng.normal(0.0, noise_lsb, n)
    tap_len = int(0.002 * THROAT_FS)
    pulse = 4000.0 * np.exp(-np.arange(tap_len) / (0.0005 * THROAT_FS))
    for ts in taps:
        i = int(ts * THROAT_FS)
        x[i : i + tap_len] += pulse
    x = np.clip(np.round(x), -32768, 32767).astype("<i2")
    c_true = 1234.5

    def cap(i):
        return c_true + 1000.0 * np.asarray(i, float) / THROAT_FS * (
            1.0 + s_true
        )

    frames = np.stack([x] * channels, axis=1)
    with wave.open(str(d / "throat.wav"), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(wav_hz)
        w.writeframes(frames.tobytes())
    starts = np.arange(0, n, THROAT_PERIOD)
    ends = np.minimum(starts + THROAT_PERIOD, n) - 1
    delay = 2.0 + rng.exponential(3.0, len(starts))
    if delay_jump is not None:
        delay[ends / THROAT_FS > delay_jump[0]] += delay_jump[1]
    arr = np.maximum.accumulate(cap(ends) + 1000.0 / THROAT_FS + delay)
    t0 = arr[0]
    with open(d / "throat_chunks.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["t_ms", "sample_index"])
        for ta, s0 in zip(arr, starts):
            w.writerow([int(ta - t0), int(s0)])

    def to_ms(ts):
        return float(cap(ts * THROAT_FS) - t0)

    ev = [(to_ms(ts) + tap_delay_ms, "o", "tap") for ts in taps]
    if ostart_s is not None:
        ev.append((to_ms(ostart_s), "o", "start"))
    ev += [(to_ms(ts), label, note) for ts, label, note in events]
    ev.sort()
    with open(d / "events.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["t_ms", "label", "note"])
        w.writerows([int(round(tm)), label, note] for tm, label, note in ev)
    m = {
        "subject": name.split("_")[1],
        "cond": name.rsplit("_", 1)[-1],
        "fw": "pc-throat",
        "sample_rates": {"imu_hz": 0, "audio_hz": THROAT_FS, "analog_hz": 0},
        "sensors": [{"id": "throat", "part": "SH-12JK", "iface": THROAT_IFACE}],
        "throat": {
            "overrun_lines": 0,
            "est_diff_frames": 0,
            "stop": "duration",
        },
    }
    m.update(meta or {})
    m["throat"].update(throat_meta or {})
    (d / "meta.json").write_text(json.dumps(m), encoding="utf-8")
    return {"to_ms": to_ms, "n": n, "dir": d}


def rewrite_throat_chunks(d: Path, fn) -> None:
    """throat_chunks.csv の行 [[t_ms, sample_index], ...] を fn で書き換える。"""
    with open(d / "throat_chunks.csv", newline="", encoding="utf-8") as f:
        rows = [[int(v) for v in r] for r in list(csv.reader(f))[1:]]
    with open(d / "throat_chunks.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["t_ms", "sample_index"])
        w.writerows(fn(rows))


class ThroatFeatureTest(TmpCase):
    def tc(self):
        return features.throat_check_module()

    def test_t7_dims_and_window_grid(self):
        info = make_throat_session(self.root, noise_lsb=30.0)
        f = features.extract_throat_session(info["dir"])
        self.assertEqual(features.N_AUDIO_FEATURES, 15)
        self.assertEqual(features.AUDIO_FEATURE_NAMES, NAMES[14:])
        self.assertEqual(f.X.shape[1], 15)
        tc = self.tc()
        ses = tc.load_session(info["dir"])
        c0, s = tc.sample_time_map(ses)
        t_end = float(tc.sample_to_time(info["n"], c0, s, THROAT_FS)) / 1000.0
        t0 = round(info["to_ms"](3.0)) / 1000.0
        n_expected = int(np.floor((t_end - t0 - 1.0) / 0.25 + 1e-9)) + 1
        self.assertEqual(len(f.t_start_s), n_expected)
        np.testing.assert_allclose(
            f.t_start_s, t0 + 0.25 * np.arange(n_expected)
        )
        self.assertLessEqual(f.t_start_s[-1] + 1.0, t_end)
        self.assertTrue(f.valid.all())
        self.assertEqual(f.session, info["dir"].name)

    def test_t8_centroid_matches_16k_path(self):
        sig = sine(1000.0, amp=0.3)
        info = make_throat_session(self.root, signal=sig)
        f = features.extract_throat_session(info["dir"])
        t16 = np.arange(HZ) / HZ
        ref = features._audio_features(sig(t16).astype(np.float32))
        k = len(f.X) // 2
        i = features.N_MFCC  # 15 次元の中のスペクトル重心
        self.assertAlmostEqual(
            float(f.X[k, i]) / ref[i], 1.0, delta=0.01, msg=(f.X[k, i], ref[i])
        )
        self.assertAlmostEqual(float(f.X[k, i]), 1000.0, delta=30.0)

    def test_t9_burst_lands_in_the_mapped_windows(self):
        # o start の後 k 番目の窓の開始から 20 ms 後に 5 ms の塊。対応が ±15 ms
        # 程度の内側なら、塊が入る窓はちょうど k−3〜k の 4 つ
        ostart_s, k = 3.0, 40
        probe = make_throat_session(
            self.root, name="20261011-090000_self_water"
        )
        o_ms = round(probe["to_ms"](ostart_s))
        burst_ms = o_ms + 1000.0 * (0.25 * k + 0.020)
        # to_ms の逆: 本当の秒
        burst_s = (burst_ms - probe["to_ms"](0.0)) / 1000.0 / (1.0 - 20e-6)

        def sig(t):
            return np.where(
                (t >= burst_s) & (t < burst_s + 0.005),
                0.3 * np.sin(2 * np.pi * 2000 * t),
                0.0,
            )

        info = make_throat_session(self.root, signal=sig, ostart_s=ostart_s)
        self.assertEqual(round(info["to_ms"](ostart_s)), o_ms)
        f = features.extract_throat_session(info["dir"])
        e = f.X[:, 0]  # 対数エネルギー（mfcc_0）
        hit = np.nonzero(e > e.min() + 0.5)[0].tolist()
        self.assertEqual(hit, [k - 3, k - 2, k - 1, k])

    def assert_stops(self, info_or_dir, text=None):
        d = info_or_dir["dir"] if isinstance(info_or_dir, dict) else info_or_dir
        with self.assertRaises(ValueError) as cm:
            features.extract_throat_session(d)
        if text:
            self.assertIn(text, str(cm.exception))

    def test_t10_input_checks(self):
        cases = {
            "meta_hz": (
                dict(
                    meta={
                        "sample_rates": {
                            "imu_hz": 0,
                            "audio_hz": 44100,
                            "analog_hz": 0,
                        }
                    }
                ),
                "meta.json 44100",
            ),
            "wav_hz": (dict(wav_hz=44100), "WAV 44100"),
            "mono": (dict(channels=1), "1 ch"),
            "fw": (dict(meta={"fw": "logger"}), "fw"),
            "overrun": (dict(throat_meta={"overrun_lines": 1}), "overrun"),
            "step": (dict(delay_jump=(20.0, 100.0)), "段差"),
        }
        for i, (label, (kw, word)) in enumerate(cases.items()):
            with self.subTest(label):
                info = make_throat_session(
                    self.root, name=f"20261011-10{i:02d}00_self_water", **kw
                )
                self.assert_stops(info, word)

    def test_t10b_sample_index_checks(self):
        cases = {
            "first_not_0": lambda r: [[t, i + 1] for t, i in r],
            "repeat": lambda r: r[:5] + [[r[5][0], r[4][1]]] + r[6:],
            "decrease": lambda r: r[:5] + [[r[5][0], r[3][1]]] + r[6:],
            "beyond_n": lambda r: r[:-1] + [[r[-1][0], 10**9]],
        }
        for i, (label, fn) in enumerate(cases.items()):
            with self.subTest(label):
                info = make_throat_session(
                    self.root, name=f"20261011-11{i:02d}00_self_water"
                )
                rewrite_throat_chunks(info["dir"], fn)
                self.assert_stops(info, "sample_index")

    def test_t10d_each_flag_alone_stops(self):
        tc = self.tc()
        cases = {
            "overrun": (dict(throat_meta={"overrun_lines": 2}), "overrun"),
            "est_diff": (dict(s_true=4000e-6), "推定差"),
            "step": (dict(delay_jump=(20.0, 100.0)), "段差"),
        }
        for i, (label, (kw, word)) in enumerate(cases.items()):
            with self.subTest(label):
                info = make_throat_session(
                    self.root,
                    name=f"20261011-12{i:02d}00_self_water",
                    noise_lsb=30.0,
                    **kw,
                )
                flags = tc.check(tc.load_session(info["dir"]))["flags"]
                self.assertEqual(len(flags), 1, flags)
                self.assertIn(word, flags[0])
                self.assert_stops(info, word)

    def test_t10c_tap_flags_do_not_stop(self):
        # o tap がパルスの 150 ms 後（|Δ の中央値| ≈ 150 ms > 125 ms）
        info = make_throat_session(
            self.root,
            noise_lsb=6.0,
            taps=(4.0, 7.0, 10.0, 13.0, 16.0),
            tap_delay_ms=150.0,
            ostart_s=20.0,
        )
        tc = self.tc()
        r = tc.check(tc.load_session(info["dir"]))
        self.assertEqual(r["flags"], [])
        self.assertEqual(r["taps_found"], 5)
        self.assertTrue(r["tap_flags"], r)
        self.assertGreater(abs(float(np.median(r["tap_deltas_ms"]))), 125.0)
        f = features.extract_throat_session(info["dir"])
        self.assertGreater(len(f.t_start_s), 0)

    def test_t11_standardizer_with_15_dims(self):
        info = make_throat_session(self.root, noise_lsb=30.0)
        f = features.extract_throat_session(info["dir"])
        s = features.Standardizer.fit([f])
        self.assertEqual(s.mean.shape, (15,))
        z = s.transform(f.X)
        self.assertEqual(z.shape, f.X.shape)
        with self.assertRaises(ValueError):
            s.transform(np.zeros((3, 29)))

    def test_t11b_scipy_is_not_imported_at_module_top(self):
        # 咽喉マイクの経路の読み込みは関数の中だけ（features の先頭で読まない）
        src = Path(features.__file__).read_text(encoding="utf-8")
        head = src.split("\ndef ", 1)[0]
        self.assertNotIn("import scipy", head)
        self.assertNotIn("from scipy", head)


if __name__ == "__main__":
    unittest.main()
