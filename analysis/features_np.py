"""咽喉マイクの間引き・特徴量・CNN の順伝播を numpy だけで計算する（Issue #44）。

装置（UNO Q の Linux 側）と PC で同じコードを使う（docs/decisions/0025・0035）。
依存は numpy と標準ライブラリだけ（UNO Q には librosa・scipy が無い）。

間引き（48 kHz → 16 kHz）:
  decimate3  セッション全体。scipy.signal.resample_poly(x, 1, 3) と同じ係数
             （61 タップ、firwin(61, 1/3, window=("kaiser", 5.0)) と同じ式）・
             同じ位置合わせ・長さ ceil(n / 3)・端は 0 埋め。
  Decimate3  ブロックごとの間引き。直前の 60 サンプルと位相を持つ。出力を
             つなぐと decimate3 の出力の先頭と一致する（遅れは入力 30 サンプル）。
特徴量（1 窓 = 16000 サンプル、16 kHz）:
  mfe40  対数メル 40 本（0〜4000 Hz）の (98, 40) float32（features_v2.mfe_one）。
  mfe_and_feat  mfe40 と、FEAT の 82 次元（窓ごとの要約。#47、0037 の 4）。
  a52    MFCC 13（メル 26 本、0〜4000 Hz）の平均・最大・標準偏差・デルタの絶対値の
         平均（52 次元。features_v2.a52_segment）。
メル行列は PC で librosa.filters.mel から作った .npy（make_features_np_consts.py）
を読む。cnn_forward は小さな CNN の numpy の順伝播（学習は compare.py）。
畳み込みは im2col ＋ 行列の積（docs/decisions/0040 決定 1）。

波形を返すのは間引きの関数だけ。特徴量と順伝播の関数は波形を戻り値に含めない。
どの関数もファイルを書かず、標準出力に出さない。
"""

from __future__ import annotations
from pathlib import Path
from typing import Mapping

import numpy as np

# --- 定数（features・features_v2 の同名の定数と同じ値。テストで確かめる） ---
SR = 16000
IN_HZ = 48000
DECIMATE = IN_HZ // SR  # 3
DECIMATE_TAPS = 61  # resample_poly の half_len 10 × 3 の両側 + 1
DECIMATE_BETA = 5.0  # Kaiser 窓（resample_poly の既定）
FRAME_LEN = 400  # 25 ms
FRAME_HOP = 160  # 10 ms
PREEMPHASIS = 0.97
N_FFT = 512
LOG_FLOOR = 1e-10
N_MFCC = 13
N_MEL_A = 26  # a52 のメルの本数
N_MEL_CNN = 40
A_FMAX_HZ = 4000.0
DELTA_WIDTH = 9  # librosa.feature.delta の width（mode="interp"）と同じ
WINDOW_SAMPLES = 16000  # 1 窓 1.0 秒
N_FRAMES = 1 + (WINDOW_SAMPLES - FRAME_LEN) // FRAME_HOP  # 98
# (入力チャンネル, 出力チャンネル)。3×3、pad 1。1・2 層目の後に 2×2 の最大値
CNN_CONV = ((1, 8), (8, 16), (16, 32))

_HERE = Path(__file__).parent
MEL40_PATH = _HERE / "mel40_htk_0_4000.npy"
MEL26_PATH = _HERE / "mel26_htk_0_4000.npy"
REF_PATH = _HERE / "features_np_ref.npz"


# --- 間引き ---


def decimate3_taps() -> np.ndarray:
    """61 タップの低域通過（遮断 1/3、Kaiser β 5.0、和で正規化）。"""
    m = np.arange(DECIMATE_TAPS) - (DECIMATE_TAPS - 1) / 2
    h = (
        np.sinc(m / DECIMATE)
        / DECIMATE
        * np.kaiser(DECIMATE_TAPS, DECIMATE_BETA)
    )
    return h / h.sum()


TAPS = decimate3_taps()
_TAPS_REV = TAPS[::-1].copy()
_HALF = (DECIMATE_TAPS - 1) // 2  # 30


def _fir_down(xp: np.ndarray, k: int) -> np.ndarray:
    """xp[3j : 3j + 61] と逆順のタップの積和を j = 0…k−1 で（多相の形）。"""
    y = np.zeros(k)
    stop = DECIMATE * k
    for i in range(DECIMATE_TAPS):
        y += _TAPS_REV[i] * xp[i : i + stop : DECIMATE]
    return y


def decimate3(x: np.ndarray) -> np.ndarray:
    """セッション全体を 16 kHz に落とす。出力 j は入力 3j を中心にした 61 タップの
    積和（範囲の外は 0）。長さ ceil(n / 3)。"""
    x = np.asarray(x, dtype=np.float64)
    n = len(x)
    k = -(-n // DECIMATE)
    if k == 0:
        return np.zeros(0)
    tail = DECIMATE * k + DECIMATE_TAPS - 1 - _HALF - n
    xp = np.concatenate((np.zeros(_HALF), x, np.zeros(tail)))
    return _fir_down(xp, k)


class Decimate3:
    """ブロックごとの間引き。process(block) は出せるだけの 16 kHz のサンプルを
    返す（出力 j には入力 3j + 30 までが要る）。状態は直前の入力と位相だけ。"""

    def __init__(self):
        self._buf = np.zeros(_HALF)  # 入力 −30 以降（先頭の 0 埋め）
        self._buf_start = -_HALF  # _buf[0] の入力の番号
        self._n_in = 0
        self._k = 0  # 次に出す出力の番号

    def process(self, block: np.ndarray) -> np.ndarray:
        block = np.asarray(block, dtype=np.float64)
        self._buf = np.concatenate((self._buf, block))
        self._n_in += len(block)
        # 出せる出力: 3k + 30 ≤ n_in − 1
        k_end = (self._n_in - 1 - _HALF) // DECIMATE + 1
        count = max(0, k_end - self._k)
        if count == 0:
            return np.zeros(0)
        off = DECIMATE * self._k - _HALF - self._buf_start
        y = _fir_down(self._buf[off:], count)
        self._k += count
        drop = DECIMATE * self._k - _HALF - self._buf_start
        # 要る末尾だけを別の配列にする（スライスのままだと連結した入力の全体を
        # 参照し続ける。#47 の adversarial-review の指摘）
        self._buf = self._buf[drop:].copy()
        self._buf_start += drop
        return y


# --- フレームと |FFT|² ---

_HAMMING = np.hamming(FRAME_LEN)  # 対称形（0012 と同じ）


def preemph(x: np.ndarray) -> np.ndarray:
    """区間全体に掛けてから切る（0012 のまま）。"""
    return np.append(x[0], x[1:] - PREEMPHASIS * x[:-1])


def power(x: np.ndarray) -> np.ndarray:
    """区間の波形 → フレームごとの |FFT|² ÷ N_FFT。(フレーム数, 257)。"""
    fr = np.lib.stride_tricks.sliding_window_view(x, FRAME_LEN)[::FRAME_HOP]
    spec = np.fft.rfft(fr * _HAMMING, n=N_FFT, axis=1)
    return (spec.real**2 + spec.imag**2) / N_FFT


# --- メル行列と DCT ---

MEL40 = np.load(MEL40_PATH, allow_pickle=False)  # (40, 257)
MEL26 = np.load(MEL26_PATH, allow_pickle=False)  # (26, 257)


def _dct_matrix(n_out: int, n_in: int) -> np.ndarray:
    """DCT-II（ortho）の先頭 n_out 行。(n_out, n_in)。"""
    k = np.arange(n_out)[:, None]
    m = np.arange(n_in)[None, :]
    d = np.cos(np.pi * k * (2 * m + 1) / (2 * n_in)) * np.sqrt(2.0 / n_in)
    d[0] *= np.sqrt(0.5)
    return d


_DCT_A = _dct_matrix(N_MFCC, N_MEL_A)


def logmel(x: np.ndarray, mel_fb: np.ndarray) -> np.ndarray:
    """区間の波形（float64）→ フレームごとの対数メル。(フレーム数, メルの本数)。"""
    return np.log(power(preemph(x)) @ mel_fb.T + LOG_FLOOR)


def mfcc_a(x: np.ndarray) -> np.ndarray:
    """区間の波形（float64）→ フレームごとの MFCC 13（メル 26 本、0〜4000 Hz、
    DCT-II の ortho）。(フレーム数, 13)。"""
    return logmel(x, MEL26) @ _DCT_A.T


# --- 特徴量 ---


def mfe40(seg: np.ndarray) -> np.ndarray:
    """1 窓の波形（16000 サンプル）→ 対数メル 40 本 (98, 40) float32。
    16000 に満たなければ全部 0。"""
    if len(seg) < WINDOW_SAMPLES:
        return np.zeros((N_FRAMES, N_MEL_CNN), dtype=np.float32)
    x = np.asarray(seg[:WINDOW_SAMPLES], dtype=np.float64)
    return logmel(x, MEL40).astype(np.float32)


# FEAT の 82 次元（docs/decisions/0037 の 4、plan #47 の 5 節。feature_set
# m4-0037-feat82）。窓の中の 98 フレームを時間方向に畳んだ要約だけ
FEAT82_SET = "m4-0037-feat82"
FEAT82_NAMES = (
    tuple(f"mel_mean_{i:02d}" for i in range(N_MEL_CNN))
    + tuple(f"mel_std_{i:02d}" for i in range(N_MEL_CNN))
    + ("log_energy_mean", "log_energy_max")
)


def mfe_and_feat(seg: np.ndarray) -> tuple:
    """1 窓の波形（16000 サンプル）→ (mfe40 (98, 40) float32, FEAT (82,)
    float64)。mfe は mfe40(seg) とビット一致する。FEAT は正規化の前の値で、
    L = mfe（float32）の時間方向の平均 40・標準偏差（÷ n）40 と、フレーム
    ごとの log(|FFT|² ÷ 512 の和 + 1e-10) の平均・最大（plan #47 の 5 節）。
    フレームごとの値は返さない。16000 に満たなければ両方とも全部 0。"""
    if len(seg) < WINDOW_SAMPLES:
        return (
            np.zeros((N_FRAMES, N_MEL_CNN), dtype=np.float32),
            np.zeros(len(FEAT82_NAMES)),
        )
    x = np.asarray(seg[:WINDOW_SAMPLES], dtype=np.float64)
    P = power(preemph(x))
    M = np.log(P @ MEL40.T + LOG_FLOOR).astype(np.float32)
    L = M.astype(np.float64)
    E = np.log(P.sum(axis=1) + LOG_FLOOR)
    feat = np.concatenate((L.mean(axis=0), L.std(axis=0), [E.mean(), E.max()]))
    return M, feat


def delta(C: np.ndarray) -> np.ndarray:
    """(フレーム数, 係数) → 同じ形のデルタ。中心の係数 k / 60（k = −4…4）、
    端は 4 番目・n − 5 番目の値の複製（librosa.feature.delta の width 9、
    order 1、mode "interp" と同じ値）。フレームが DELTA_WIDTH 以上のこと。"""
    C = np.asarray(C, dtype=np.float64)
    n = len(C)
    h = DELTA_WIDTH // 2  # 4
    if n < DELTA_WIDTH:
        raise ValueError(f"フレームが {DELTA_WIDTH} に満たない（{n}）")
    denom = float(sum(k * k for k in range(-h, h + 1)))  # 60
    mid = np.zeros((n - 2 * h, C.shape[1]))
    for k in range(-h, h + 1):
        if k:
            mid += k * C[h + k : n - h + k]
    mid /= denom
    return np.concatenate(
        (np.repeat(mid[:1], h, axis=0), mid, np.repeat(mid[-1:], h, axis=0))
    )


def a_stats(C: np.ndarray) -> np.ndarray:
    """係数行列 (フレーム数, 13) → 52 次元（平均、最大、標準偏差（÷ n）、
    デルタの絶対値の平均）。フレームが DELTA_WIDTH に満たなければ全部 0。"""
    C = np.asarray(C, dtype=np.float64)
    if len(C) < DELTA_WIDTH:
        return np.zeros(4 * N_MFCC)
    d = delta(C)
    return np.concatenate(
        [C.mean(axis=0), C.max(axis=0), C.std(axis=0), np.abs(d).mean(axis=0)]
    )


def a52(seg: np.ndarray) -> np.ndarray:
    """区間の波形 → 52 次元（案A）。フレームが足りなければ全部 0。"""
    x = np.asarray(seg, dtype=np.float64)
    if len(x) < FRAME_LEN:
        return np.zeros(4 * N_MFCC)
    return a_stats(mfcc_a(x))


# --- 小さな CNN の numpy の順伝播（docs/decisions/0033。学習は compare.py） ---


def _conv3x3(x: np.ndarray, W: np.ndarray, b: np.ndarray) -> np.ndarray:
    """x (B, Ci, H, W)、W (Co, Ci, 3, 3) → (B, Co, H, W)。pad 1、相互相関。

    im2col ＋ 行列の積（docs/decisions/0040 決定 1。bench_runtimes._conv_im2col
    と同じ式）: パディング → sliding_window_view → (B, Ci × 9, H × W) に並べ
    直す → W.reshape(Co, −1) @ cols。前の einsum の版との差はテスト
    （test_features_np の TestIm2col）で 1e-6 以下を確かめる。"""
    B, Ci, H, Wd = x.shape
    Co = W.shape[0]
    xp = np.pad(x, ((0, 0), (0, 0), (1, 1), (1, 1)))
    cols = np.lib.stride_tricks.sliding_window_view(xp, (3, 3), axis=(2, 3))
    # (B, Ci, H, W, 3, 3) → (B, Ci × 9, H × W)。W の (Ci, 3, 3) と同じ並び
    cols = cols.transpose(0, 1, 4, 5, 2, 3).reshape(B, Ci * 9, H * Wd)
    out = W.reshape(Co, -1) @ cols
    return out.reshape(B, Co, H, Wd) + b[None, :, None, None]


def _maxpool2(x: np.ndarray) -> np.ndarray:
    B, C, H, W = x.shape
    h, w = H // 2, W // 2
    x = x[:, :, : 2 * h, : 2 * w].reshape(B, C, h, 2, w, 2)
    return x.max(axis=(3, 5))


def cnn_forward(params: Mapping[str, np.ndarray], M: np.ndarray) -> np.ndarray:
    """params: conv{1,2,3}.{weight,bias}、fc.{weight,bias}（PyTorch の
    state_dict と同じ名前）。M: 正規化済みの (B, 98, 40)。陽性確率 (B,)。"""
    x = np.asarray(M, dtype=np.float32)[:, None, :, :]
    for k in range(3):
        x = _conv3x3(
            x, params[f"conv{k + 1}.weight"], params[f"conv{k + 1}.bias"]
        )
        x = np.maximum(x, 0.0)
        if k < 2:
            x = _maxpool2(x)
    g = x.mean(axis=(2, 3))  # (B, 32)
    z = g @ params["fc.weight"].T[:, 0] + params["fc.bias"][0]
    return 1.0 / (1.0 + np.exp(-z.astype(np.float64)))


# --- 照合用の決定的な合成入力（乱数を使わない数式。装置と PC で作り直す） ---


def reference_signal() -> np.ndarray:
    """照合用の 16 kHz・16000 サンプルの合成信号（2 つの正弦波、振幅の変わる
    正弦波、チャープ）。音声ではない。"""
    t = np.arange(WINDOW_SAMPLES) / SR
    return (
        0.3 * np.sin(2 * np.pi * 440.0 * t)
        + 0.1 * np.sin(2 * np.pi * 1250.0 * t + 0.7)
        + 0.2
        * (0.5 + 0.5 * np.sin(2 * np.pi * 3.0 * t))
        * np.sin(2 * np.pi * 2300.0 * t)
        + 0.05 * np.sin(2 * np.pi * (100.0 * t + 1800.0 * t * t))
    )


def reference_input48k(n: int) -> np.ndarray:
    """照合用の 48 kHz の合成信号（int16 の目盛り）。音声ではない。"""
    t = np.arange(n) / IN_HZ
    return 9000.0 * np.sin(2 * np.pi * 700.0 * t) + 3000.0 * np.sin(
        2 * np.pi * 9000.0 * t + 0.2
    )


def _cnn_shapes() -> dict:
    shapes = {}
    for k, (ci, co) in enumerate(CNN_CONV):
        shapes[f"conv{k + 1}.weight"] = (co, ci, 3, 3)
        shapes[f"conv{k + 1}.bias"] = (co,)
    shapes["fc.weight"] = (1, CNN_CONV[-1][1])
    shapes["fc.bias"] = (1,)
    return shapes


def random_cnn(rng) -> dict:
    """時間を測るための乱数の重み（同じ形。学習済みではない）。
    compare.measure_timings と同じ作り方（rng は np.random.default_rng など）。
    装置の計測（bench_infer・bench_runtimes）と PC の変換・テストで同じ重みを
    作るために、ここに 1 つだけ置く（Issue #51）。"""
    params = {}
    for k, (ci, co) in enumerate(CNN_CONV):
        params[f"conv{k + 1}.weight"] = rng.normal(
            0, 0.1, (co, ci, 3, 3)
        ).astype(np.float32)
        params[f"conv{k + 1}.bias"] = np.zeros(co, np.float32)
    params["fc.weight"] = rng.normal(0, 0.1, (1, CNN_CONV[-1][1])).astype(
        np.float32
    )
    params["fc.bias"] = np.zeros(1, np.float32)
    return params


def reference_cnn() -> tuple:
    """照合用の決定的な CNN の重みと入力 (params, M (1, 98, 40) float32)。
    学習済みの重みではない（数式で作る）。"""
    params = {}
    for j, (name, shape) in enumerate(_cnn_shapes().items()):
        size = int(np.prod(shape))
        v = np.sin(0.7 * np.arange(size) + 0.9 * j)
        scale = 0.3 if name.endswith("weight") else 0.05
        params[name] = (scale * v).reshape(shape).astype(np.float32)
    i = np.arange(N_FRAMES)[:, None]
    m = np.arange(N_MEL_CNN)[None, :]
    M = np.sin(0.21 * i + 0.13 * m) + 0.5 * np.cos(0.017 * i * m)
    return params, M[None].astype(np.float32)
