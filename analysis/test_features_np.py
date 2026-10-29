"""features_np.py（numpy だけの特徴量・間引き・CNN の順伝播）を合成データで
検証する（Issue #44）。

実行: python -m unittest discover -s analysis -v
合成のセッションは tempfile に作る（test_features.make_throat_session）。data/ は
使わない。言えるのは「合成入力で旧実装（librosa・scipy の式）との差が許容差以内」
まで。M3 の性能の数字は確かめ直さない。
"""

from __future__ import annotations
import ast, sys, tempfile, unittest
from pathlib import Path

import numpy as np

import features, features_np as fnp, features_v2 as v2
import make_features_np_consts as consts
from test_features import make_throat_session

# --- 旧の参照（変更前の HEAD の式を写したもの。librosa・scipy を使う） ---

v2.check_librosa_cache()
import librosa  # noqa: E402
import scipy.fft  # noqa: E402
import scipy.signal  # noqa: E402

REF_SR = 16000
REF_FRAME_LEN = 400
REF_FRAME_HOP = 160
REF_N_FFT = 512
REF_N_MFCC = 13
REF_HAMMING = np.hamming(REF_FRAME_LEN)
REF_FFT_FREQS = librosa.fft_frequencies(sr=REF_SR, n_fft=REF_N_FFT)


def ref_mel(n_mels: int, fmax: float) -> np.ndarray:
    return librosa.filters.mel(
        sr=REF_SR,
        n_fft=REF_N_FFT,
        n_mels=n_mels,
        fmin=0.0,
        fmax=fmax,
        htk=True,
        norm=None,
        dtype=np.float64,
    )


REF_MEL_BASE = ref_mel(26, 8000.0)
REF_MEL_A = ref_mel(26, 4000.0)
REF_MEL_CNN = ref_mel(40, 4000.0)


def ref_power(x):
    fr = librosa.util.frame(
        x, frame_length=REF_FRAME_LEN, hop_length=REF_FRAME_HOP, axis=0
    )
    spec = np.fft.rfft(fr * REF_HAMMING, n=REF_N_FFT, axis=1)
    return (spec.real**2 + spec.imag**2) / REF_N_FFT


def ref_preemph(x):
    return np.append(x[0], x[1:] - 0.97 * x[:-1])


def ref_mfcc(x, mel_fb):
    logmel = np.log(ref_power(ref_preemph(x)) @ mel_fb.T + 1e-10)
    return scipy.fft.dct(logmel, type=2, norm="ortho", axis=1)[:, :REF_N_MFCC]


def ref_delta(C):
    """(フレーム数, 13) → 同じ形のデルタ（librosa、width 9、interp）。"""
    return librosa.feature.delta(
        np.asarray(C, dtype=np.float64).T,
        width=9,
        order=1,
        mode="interp",
        axis=-1,
    ).T


def ref_a_stats(C):
    C = np.asarray(C, dtype=np.float64)
    if len(C) < 9:
        return np.zeros(4 * REF_N_MFCC)
    d = ref_delta(C)
    return np.concatenate(
        [C.mean(axis=0), C.max(axis=0), C.std(axis=0), np.abs(d).mean(axis=0)]
    )


def ref_a52(x):
    x = np.asarray(x, dtype=np.float64)
    if len(x) < REF_FRAME_LEN:
        return np.zeros(4 * REF_N_MFCC)
    return ref_a_stats(ref_mfcc(x, REF_MEL_A))


def ref_base15(x):
    out = np.zeros(REF_N_MFCC + 2)
    x = np.asarray(x, dtype=np.float64)
    if len(x) < REF_FRAME_LEN:
        return out
    out[:REF_N_MFCC] = ref_mfcc(x, REF_MEL_BASE).mean(axis=0)
    p = ref_power(x)
    total = p.sum(axis=1)
    ok = total >= 1e-12
    centroid = np.zeros(len(p))
    centroid[ok] = (p[ok] @ REF_FFT_FREQS) / total[ok]
    out[REF_N_MFCC] = centroid.mean()
    pos = (x - x.mean()) >= 0
    out[REF_N_MFCC + 1] = np.count_nonzero(pos[1:] != pos[:-1]) / (len(x) - 1)
    return out


def ref_mfe40(seg):
    if len(seg) < 16000:
        return np.zeros((98, 40), dtype=np.float32)
    x = np.asarray(seg[:16000], dtype=np.float64)
    logmel = np.log(ref_power(ref_preemph(x)) @ REF_MEL_CNN.T + 1e-10)
    return logmel.astype(np.float32)


_REF_SEG = {"base15": ref_base15, "a52": ref_a52}
_REF_DIMS = {"base15": 15, "a52": 52}


def ref_window_features(x, starts, feature_set, lo, hi):
    """旧の features_v2.window_features と features_one（窓の切り方は
    v2.segment_bounds。この Issue で変えない関数）。"""
    base = feature_set.removesuffix("_mw")
    lengths = v2.MW_LENGTHS_S if feature_set.endswith("_mw") else (1.0,)
    X = np.zeros((len(starts), _REF_DIMS[base] * len(lengths)), np.float32)
    for k, i in enumerate(starts):
        parts = []
        for L in lengths:
            a, b = v2.segment_bounds(i, L, lo, hi)
            seg = x[a:b] if b > a else x[0:0]
            if L == 1.0 and len(seg) < 16000:
                parts.append(np.zeros(_REF_DIMS[base]))
            elif L == 1.0:
                parts.append(_REF_SEG[base](seg[:16000]))
            else:
                parts.append(_REF_SEG[base](seg))
        X[k] = np.concatenate(parts)
    return X


def ref_throat_windows(session_dir):
    """旧の features._throat_windows（間引きは scipy の resample_poly）。
    時刻の対応と範囲は tools/throat_check.py・evaluate（この Issue で変えない）。"""
    import evaluate

    tc = features.throat_check_module()
    ses = tc.load_session(session_dir)
    c0, s = tc.sample_time_map(ses)
    t0_s, duration_s = evaluate.throat_span(ses, tc, c0, s)
    L = ses["wav"][:, 0].astype(float)
    x = (scipy.signal.resample_poly(L, 1, 3) / 32768.0).astype(np.float32)
    sample_t = tc.sample_to_time(3 * np.arange(len(x) + 1), c0, s, 48000) / 1e3
    span = duration_s
    nw = int(np.floor((span - 1.0) / 0.25 + 1e-9)) + 1 if span >= 1.0 else 0
    t_start = t0_s + 0.25 * np.arange(nw)
    start_idx = np.zeros(nw, dtype=np.int64)
    valid = np.ones(nw, dtype=bool)
    for k, st in enumerate(t_start):
        i = int(np.searchsorted(sample_t[:-1], st, side="left"))
        if i > 0 and (i == len(x) or st - sample_t[i - 1] <= sample_t[i] - st):
            i -= 1
        start_idx[k] = i
        if len(x[i : i + 16000]) < 16000:
            valid[k] = False
    return x, sample_t, t_start, start_idx, valid, t0_s, t0_s + duration_s


def burst_signal(t):
    """2 つの正弦波と、0.6 秒ごとに入る短い高い音（決定的な数式）。"""
    env = (np.mod(t, 0.6) < 0.15).astype(float)
    return (
        0.2 * np.sin(2 * np.pi * 440.0 * t)
        + 0.05 * np.sin(2 * np.pi * 2500.0 * t + 0.3)
        + 0.3 * env * np.sin(2 * np.pi * 1200.0 * t)
    )


class OldPipelineTest(unittest.TestCase):
    """経路全体の旧実装との照合（plan #44 の CONCERN 1）。合成の咽喉マイクの
    セッションを旧の参照経路と今の経路の両方に通して比べる。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def sessions(self):
        return [
            make_throat_session(
                self.root,
                name="20261024-100000_self_water",
                seconds=12.0,
                noise_lsb=30.0,
            ),
            make_throat_session(
                self.root,
                name="20261024-110000_self_water",
                seconds=12.0,
                noise_lsb=20.0,
                signal=burst_signal,
                seed=3,
            ),
        ]

    def test_session_path_matches_old_reference(self):
        rel = dict(rtol=1e-5, atol=0.0)
        for info in self.sessions():
            d = info["dir"]
            with self.subTest(d.name):
                ref = ref_throat_windows(d)
                new = features._throat_windows(d)
                self.assertEqual(new[0].dtype, np.float32)
                self.assertEqual(len(new[0]), len(ref[0]))
                np.testing.assert_allclose(new[0], ref[0], rtol=0, atol=1e-6)
                for k in (1, 2, 3, 4, 5, 6):
                    np.testing.assert_array_equal(new[k], ref[k])
                x, sample_t, t_start, start_idx, valid, _, t1 = ref
                self.assertGreater(len(t_start), 10)
                lo, hi = v2._session_bounds(x, sample_t, start_idx, t1)
                t, M, va = v2.extract_mfe(d)
                np.testing.assert_array_equal(t, t_start)
                np.testing.assert_array_equal(va, valid)
                M_ref = np.zeros_like(M)
                for k, i in enumerate(start_idx):
                    a, b = v2.segment_bounds(i, 1.0, lo, hi)
                    M_ref[k] = ref_mfe40(x[a:b])
                np.testing.assert_allclose(M, M_ref, rtol=0, atol=1e-5)
                for fs in ("a52", "base15", "a52_mw", "base15_mw"):
                    f = v2.extract(d, fs)
                    np.testing.assert_array_equal(f.t_start_s, t_start)
                    np.testing.assert_array_equal(f.valid, valid)
                    X_ref = ref_window_features(x, start_idx, fs, lo, hi)
                    np.testing.assert_allclose(f.X, X_ref, **rel, err_msg=fs)


class ConstantsTest(unittest.TestCase):
    def test_same_values_as_features_and_features_v2(self):
        pairs = {
            "SR": (fnp.SR, features.AUDIO_HZ),
            "IN_HZ": (fnp.IN_HZ, features.THROAT_HZ),
            "DECIMATE": (fnp.DECIMATE, features.THROAT_DECIMATE),
            "FRAME_LEN": (fnp.FRAME_LEN, v2.FRAME_LEN),
            "FRAME_HOP": (fnp.FRAME_HOP, v2.FRAME_HOP),
            "PREEMPHASIS": (fnp.PREEMPHASIS, v2.PREEMPHASIS),
            "N_FFT": (fnp.N_FFT, v2.N_FFT),
            "LOG_FLOOR": (fnp.LOG_FLOOR, v2.LOG_FLOOR),
            "N_MFCC": (fnp.N_MFCC, v2.N_MFCC),
            "N_MEL_A": (fnp.N_MEL_A, v2.N_MEL),
            "N_MEL_CNN": (fnp.N_MEL_CNN, v2.N_MEL_CNN),
            "A_FMAX_HZ": (fnp.A_FMAX_HZ, v2.A_FMAX_HZ),
            "DELTA_WIDTH": (fnp.DELTA_WIDTH, v2.DELTA_WIDTH),
            "WINDOW_SAMPLES": (fnp.WINDOW_SAMPLES, v2.WINDOW_SAMPLES),
            "CNN_CONV": (fnp.CNN_CONV, v2.CNN_CONV),
        }
        for name, (a, b) in pairs.items():
            with self.subTest(name):
                self.assertEqual(a, b)
        self.assertEqual(fnp.N_FRAMES, 98)
        # 順伝播は移した先と同じもの（test_throat_compare の PyTorch との照合が
        # そのまま features_np.cnn_forward を確かめる）
        self.assertIs(v2.cnn_forward, fnp.cnn_forward)


class MelTest(unittest.TestCase):
    def test_npy_matches_librosa_exactly(self):
        for M, n in ((fnp.MEL40, 40), (fnp.MEL26, 26)):
            with self.subTest(n=n):
                self.assertEqual(M.dtype, np.float64)
                self.assertEqual(M.shape, (n, 257))
                np.testing.assert_array_equal(M, ref_mel(n, 4000.0))
        self.assertIs(v2._MEL_CNN, fnp.MEL40)
        self.assertIs(v2._MEL_A, fnp.MEL26)


class DecimateTest(unittest.TestCase):
    def test_taps_match_firwin(self):
        ref = scipy.signal.firwin(61, 1 / 3, window=("kaiser", 5.0))
        self.assertEqual(fnp.TAPS.shape, (61,))
        np.testing.assert_allclose(fnp.TAPS, ref, rtol=0, atol=1e-15)
        np.testing.assert_allclose(fnp.decimate3_taps(), ref, atol=1e-15)

    def test_decimate3_matches_resample_poly(self):
        rng = np.random.default_rng(4)
        for n in (48000, 48001, 48002, 100, 60, 2, 1):
            with self.subTest(n=n):
                x = np.round(rng.normal(0.0, 3000.0, n))
                ref = scipy.signal.resample_poly(x, 1, 3)
                got = fnp.decimate3(x)
                self.assertEqual(len(got), len(ref))
                self.assertEqual(len(got), -(-n // 3))
                np.testing.assert_allclose(got, ref, rtol=0, atol=1e-8)
        self.assertEqual(len(fnp.decimate3(np.zeros(0))), 0)

    def test_block_output_matches_decimate3_prefix(self):
        rng = np.random.default_rng(5)
        x = np.round(rng.normal(0.0, 3000.0, 60000))
        full = fnp.decimate3(x)
        plans = {
            "1": [1] * 3000,
            "7": [7] * 3000,
            "4800": [4800] * 12,
            "12000": [12000] * 5,
            "ばらばら": list(rng.integers(1, 9000, 30)),
        }
        for name, sizes in plans.items():
            with self.subTest(name):
                d = fnp.Decimate3()
                outs, pos = [], 0
                for s in sizes:
                    outs.append(d.process(x[pos : pos + s]))
                    pos = min(pos + s, len(x))
                y = np.concatenate(outs)
                # 出力 j には入力 3j + 30 までが要る（遅れ 30 サンプル）
                self.assertEqual(len(y), max(0, (pos - 31) // 3 + 1))
                np.testing.assert_allclose(y, full[: len(y)], atol=1e-9)


SIGNALS = {
    "noise": lambda t, r: r.normal(0.0, 0.1, len(t)),
    "sine": lambda t, r: 0.4 * np.sin(2 * np.pi * 900.0 * t + 0.2),
    "silence": lambda t, r: np.zeros(len(t)),
    "mix": lambda t, r: (
        0.2 * np.sin(2 * np.pi * 300.0 * t) + r.normal(0.0, 0.02, len(t))
    ),
}


class FeatureTest(unittest.TestCase):
    def segs(self, n=16000):
        rng = np.random.default_rng(6)
        t = np.arange(n) / 16000
        return {k: f(t, rng) for k, f in SIGNALS.items()}

    def test_mfe40_matches_librosa_version(self):
        for name, x in self.segs().items():
            with self.subTest(name):
                got = fnp.mfe40(x)
                self.assertEqual(got.dtype, np.float32)
                self.assertEqual(got.shape, (98, 40))
                np.testing.assert_allclose(got, ref_mfe40(x), atol=1e-5)
                np.testing.assert_array_equal(v2.mfe_one(x), got)
        short = fnp.mfe40(np.ones(15999))
        np.testing.assert_array_equal(short, np.zeros((98, 40), np.float32))

    def test_a52_matches_librosa_version(self):
        for n in (16000, 8000, 32000):
            for name, x in self.segs(n).items():
                with self.subTest(name=name, n=n):
                    got = fnp.a52(x)
                    np.testing.assert_allclose(
                        got, ref_a52(x), rtol=0, atol=1e-9
                    )
                    np.testing.assert_array_equal(v2.a52_segment(x), got)
        np.testing.assert_array_equal(fnp.a52(np.ones(399)), np.zeros(52))
        # 400 サンプル = 1 フレーム < DELTA_WIDTH
        np.testing.assert_array_equal(fnp.a52(np.ones(400)), np.zeros(52))

    def test_delta_matches_librosa(self):
        rng = np.random.default_rng(7)
        for n in (9, 10, 98):
            with self.subTest(n=n):
                C = rng.normal(0.0, 3.0, (n, 13))
                np.testing.assert_allclose(
                    fnp.delta(C), ref_delta(C), rtol=0, atol=1e-12
                )
        with self.assertRaises(ValueError):
            fnp.delta(np.zeros((8, 13)))

    def test_logmel_frames(self):
        x = self.segs(5000)["mix"]
        got = v2.logmel_frames(x)
        ref = np.log(ref_power(ref_preemph(x)) @ REF_MEL_CNN.T + 1e-10)
        np.testing.assert_allclose(got, ref, rtol=0, atol=1e-9)


class RefFileTest(unittest.TestCase):
    def test_ref_is_up_to_date(self):
        mels, ref = consts.build()
        self.assertEqual(consts.differences(mels, ref), [])
        with np.load(fnp.REF_PATH, allow_pickle=False) as z:
            self.assertEqual(sorted(z.files), sorted(consts.REF_KEYS))
            self.assertEqual(z["mfe40"].shape, (98, 40))
            self.assertEqual(z["a52"].shape, (52,))
            self.assertEqual(z["cnn"].shape, (1,))
            # numpy 版が参照と許容差の中（装置の bench_infer.py と同じ確かめ）
            x = fnp.reference_signal()
            np.testing.assert_allclose(fnp.mfe40(x), z["mfe40"], atol=1e-5)
            np.testing.assert_allclose(fnp.a52(x), z["a52"], atol=1e-9)
            np.testing.assert_allclose(fnp.TAPS, z["taps"], atol=1e-15)
            self.assertEqual(
                sorted(fnp.MEL40_PATH.parent.glob("*.npy")),
                sorted(consts.MEL_SPECS),
            )


# --- im2col の置き換え（docs/decisions/0040 決定 1、plan #47 の 3 節） ---


def old_conv3x3(x, W, b):
    """置き換える前の features_np._conv3x3（einsum 版）をそのまま写したもの。"""
    B, Ci, H, Wd = x.shape
    xp = np.pad(x, ((0, 0), (0, 0), (1, 1), (1, 1)))
    cols = np.lib.stride_tricks.sliding_window_view(xp, (3, 3), axis=(2, 3))
    # cols (B, Ci, H, W, 3, 3)
    out = np.einsum("bchwij,ocij->bohw", cols, W, optimize=True)
    return out + b[None, :, None, None]


def old_cnn_forward(params, M):
    """置き換える前の cnn_forward（畳み込みだけが old_conv3x3）。"""
    x = np.asarray(M, dtype=np.float32)[:, None, :, :]
    for k in range(3):
        x = old_conv3x3(
            x, params[f"conv{k + 1}.weight"], params[f"conv{k + 1}.bias"]
        )
        x = np.maximum(x, 0.0)
        if k < 2:
            x = fnp._maxpool2(x)
    g = x.mean(axis=(2, 3))
    z = g @ params["fc.weight"].T[:, 0] + params["fc.bias"][0]
    return 1.0 / (1.0 + np.exp(-z.astype(np.float64)))


IM2COL_TOL = 1e-6  # 0040 決定 1
M4_MODEL_PATH = Path(__file__).resolve().parent / "models" / "m4-0038.npz"


def synthetic_mfe(n: int, seed: int) -> np.ndarray:
    """正弦波・雑音から作った n 窓の mfe40 (n, 98, 40)。"""
    rng = np.random.default_rng(seed)
    t = np.arange(16000) / 16000
    out = []
    for i in range(n):
        x = (
            (0.05 + 0.3 * rng.random())
            * np.sin(2 * np.pi * (150 + 90 * i) * t + rng.random())
            + rng.normal(0.0, 10 ** rng.uniform(-4, -1), len(t))
            + 0.1 * (i % 3 == 0) * rng.normal(0.0, 0.2, len(t))
        )
        out.append(fnp.mfe40(x))
    return np.stack(out)


class Im2colTest(unittest.TestCase):
    def test_conv_matches_old_einsum(self):
        rng = np.random.default_rng(0)
        for B, Ci, Co, H, W in ((1, 1, 8, 98, 40), (3, 8, 16, 49, 20)):
            x = rng.normal(0, 1, (B, Ci, H, W)).astype(np.float32)
            Wt = rng.normal(0, 0.1, (Co, Ci, 3, 3)).astype(np.float32)
            b = rng.normal(0, 0.1, Co).astype(np.float32)
            got = fnp._conv3x3(x, Wt, b)
            self.assertEqual(got.shape, (B, Co, H, W))
            np.testing.assert_allclose(
                got, old_conv3x3(x, Wt, b), rtol=0, atol=1e-5
            )
        self.assertIs(v2._conv3x3, fnp._conv3x3)

    def test_random_weights_new_vs_old(self):
        worst = 0.0
        for s in range(5):
            params = fnp.random_cnn(np.random.default_rng(s))
            for B in (1, 7, 256):
                with self.subTest(seed=s, B=B):
                    M = (
                        np.random.default_rng(100 + s)
                        .normal(0, 1, (B, 98, 40))
                        .astype(np.float32)
                    )
                    new = fnp.cnn_forward(params, M)
                    old = old_cnn_forward(params, M)
                    self.assertEqual(new.shape, (B,))
                    d = float(np.abs(new - old).max())
                    worst = max(worst, d)
                    self.assertLessEqual(d, IM2COL_TOL)
        self.assertLessEqual(worst, IM2COL_TOL)

    def test_m4_model_new_vs_old(self):
        import m4_infer

        model = m4_infer.load_model(M4_MODEL_PATH)
        a = model["arrays"]
        X = synthetic_mfe(24, 3)
        self.assertEqual(model["cfg"]["seeds"], (0, 1, 2))
        for s in model["cfg"]["seeds"]:
            with self.subTest(seed=s):
                params = {k: a[f"s{s}/{k}"] for k in m4_infer.CNN_SHAPES}
                Z = ((X - a[f"s{s}/mean"]) / a[f"s{s}/std"]).astype(np.float32)
                d = np.abs(
                    fnp.cnn_forward(params, Z) - old_cnn_forward(params, Z)
                ).max()
                self.assertLessEqual(float(d), IM2COL_TOL)

    def test_reference_npz_within_existing_tolerance(self):
        params, M = fnp.reference_cnn()
        with np.load(fnp.REF_PATH, allow_pickle=False) as z:
            ref = z["cnn"]
        np.testing.assert_allclose(
            fnp.cnn_forward(params, M), ref, rtol=0, atol=1e-5
        )
        np.testing.assert_allclose(
            old_cnn_forward(params, M), ref, rtol=0, atol=1e-5
        )


# --- FEAT の 82 次元（plan #47 の 5 節） ---


class MfeAndFeatTest(unittest.TestCase):
    def test_mfe_is_bit_identical_to_mfe40(self):
        for name, x in FeatureTest().segs().items():
            with self.subTest(name):
                M, f = fnp.mfe_and_feat(x)
                np.testing.assert_array_equal(M, fnp.mfe40(x))
                self.assertEqual(M.dtype, np.float32)
                self.assertEqual(f.shape, (82,))
                self.assertEqual(f.dtype, np.float64)
        M, f = fnp.mfe_and_feat(np.ones(15999))
        np.testing.assert_array_equal(M, np.zeros((98, 40), np.float32))
        np.testing.assert_array_equal(f, np.zeros(82))

    def test_names(self):
        n = fnp.FEAT82_NAMES
        self.assertEqual(len(n), 82)
        self.assertEqual(len(set(n)), 82)
        self.assertEqual(n[0], "mel_mean_00")
        self.assertEqual(n[39], "mel_mean_39")
        self.assertEqual(n[40], "mel_std_00")
        self.assertEqual(n[79], "mel_std_39")
        self.assertEqual(n[80:], ("log_energy_mean", "log_energy_max"))
        self.assertEqual(fnp.FEAT82_SET, "m4-0037-feat82")

    def test_formula_by_hand(self):
        """手で計算した値と比べる。L の平均・標準偏差（÷ n）は mfe40 の
        float32 の値から、対数エネルギーは |FFT|² ÷ 512 のフレームの和から。"""
        rng = np.random.default_rng(11)
        x = 0.2 * np.sin(2 * np.pi * 500 * np.arange(16000) / 16000)
        x += rng.normal(0, 0.01, 16000)
        _, f = fnp.mfe_and_feat(x)
        L = fnp.mfe40(x).astype(np.float64)
        n = L.shape[0]
        mean = L.sum(axis=0) / n
        std = np.sqrt(((L - mean) ** 2).sum(axis=0) / n)
        np.testing.assert_allclose(f[:40], mean, rtol=0, atol=1e-12)
        np.testing.assert_allclose(f[40:80], std, rtol=0, atol=1e-12)
        # フレームごとのエネルギーを素朴に（preemph → Hamming → |rfft|² / 512）
        y = np.append(x[0], x[1:] - 0.97 * x[:-1])
        e = []
        for k in range(98):
            fr = y[160 * k : 160 * k + 400] * np.hamming(400)
            s = np.fft.rfft(fr, n=512)
            e.append(np.log((np.abs(s) ** 2 / 512).sum() + 1e-10))
        self.assertAlmostEqual(f[80], float(np.mean(e)), places=10)
        self.assertAlmostEqual(f[81], float(np.max(e)), places=10)
        # 無音: L = log(1e-10)（float32）、std 0、エネルギー log(1e-10)
        _, z = fnp.mfe_and_feat(np.zeros(16000))
        np.testing.assert_allclose(
            z[:40], float(np.float32(np.log(1e-10))), rtol=0, atol=1e-6
        )
        np.testing.assert_array_equal(z[40:80], np.zeros(40))
        self.assertAlmostEqual(z[80], np.log(1e-10))
        self.assertAlmostEqual(z[81], np.log(1e-10))


WRITE_ATTRS = {
    "save",
    "savez",
    "savez_compressed",
    "savetxt",
    "tofile",
    "write_text",
    "write_bytes",
}


class StaticTest(unittest.TestCase):
    def test_features_np_imports_numpy_and_stdlib_only_and_writes_nothing(self):
        path = Path(fnp.__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
            elif isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name):
                    self.assertNotIn(f.id, ("print", "open"), f.id)
                if isinstance(f, ast.Attribute):
                    self.assertNotIn(f.attr, WRITE_ATTRS, f.attr)
        others = names - {"numpy", "__future__"}
        self.assertTrue(others <= set(sys.stdlib_module_names), others)
        self.assertIn("numpy", names)


if __name__ == "__main__":
    unittest.main()
