"""咽喉マイクの特徴量の組（Issue #43。式と選び方は docs/decisions/0033）。

比べる組（FEATURE_SETS）:
  base15     0012 の音の 15 次元（MFCC 13 の平均、スペクトル重心、ゼロ交差率）。
             features._audio_features と同じ値（librosa の部品で書き直したもの）。
  a52        MFCC 13 係数（メル 26 本、0〜A_FMAX_HZ）の窓内の平均・最大・標準偏差・
             デルタの絶対値の平均（52 次元）。
  base15_mw  base15 を 0.5・1.0・2.0 秒の窓（中心をそろえる）に当てて並べる（45）。
  a52_mw     a52 を同じ 3 つの窓に当てて並べる（156）。
CNN の入力（mfe40）: 対数メル 40 本（0〜A_FMAX_HZ）の 98 × 40。小さな CNN の
numpy の順伝播（cnn_forward）は features_np.py にあり、ここから同じ名前で出す
（装置と PC で同じコード。docs/decisions/0025）。学習（PyTorch）は compare.py。
mfe40・a52 の式（フレーム、|FFT|²、メル行列、デルタ）も features_np.py の numpy
だけの実装を呼ぶ（Issue #44）。base15 は librosa・scipy のまま（装置で使わない）。

中心はメモリ上の信号から作る関数（window_features・features_one・mfe_one）。
セッションフォルダの読み込みは extract・extract_mfe だけが行い、窓の格子・valid・
記録の範囲・サンプルの時刻は features._throat_windows（extract_throat_session と
同じもの）を使う。戻り値に波形を含めない。特徴量をファイルに書かない。

o start より前の音: 16 kHz の信号はセッション全体を features_np.decimate3
（resample_poly(x, 1, 3) と同じ係数・位置合わせ）で落としてから窓を切る
（features.extract_throat_session と同じ）。保証するのは窓の選び方で、
どの組のどの窓も [lo, hi)（記録の範囲 [t0, t1) に当たるサンプルの番号）の外を
切り出さない。リサンプルのフィルタの裾（数 ms）と最近傍の 1 サンプルの規則による
o start 直前の音の混入は extract_throat_session と同じで、変えない。

依存: numpy、features_np（numpy だけ）、scipy（base15 の DCT だけ）、librosa
（base15 のメル行列と重心の周波数だけ。音声の読み込みには使わない）。
"""

from __future__ import annotations
import os, sys
from pathlib import Path
from typing import Mapping

import numpy as np
import scipy.fft

import features
import features_np

_CACHE_STOP = (
    "{}。librosa のキャッシュは特徴量をディスクや標準出力に出すので、この環境では"
    "実行しません（環境変数を外して実行し直す。docs/decisions/0033）"
)


def check_librosa_cache() -> None:
    """librosa のキャッシュ（joblib）が有効なら止まる。環境変数
    LIBROSA_CACHE_DIR（設定されていれば空でも。librosa は空の値を「保存先あり」と
    扱い、作業フォルダの joblib/ に書く）・LIBROSA_CACHE_VERBOSE（0 より大きい）と、
    import 済みの librosa のキャッシュの設定（保存先・verbose）を見る。環境は
    書き換えない。"""
    if "LIBROSA_CACHE_DIR" in os.environ:
        raise SystemExit(
            _CACHE_STOP.format("LIBROSA_CACHE_DIR が設定されています")
        )
    try:
        verbose = int(os.environ.get("LIBROSA_CACHE_VERBOSE", "0") or 0)
    except ValueError:
        verbose = 1  # 読めない値は有効とみなす
    if verbose > 0:
        raise SystemExit(
            _CACHE_STOP.format("LIBROSA_CACHE_VERBOSE が 0 より大きい値です")
        )
    mod = sys.modules.get("librosa._cache")
    cache = getattr(mod, "cache", None)
    memory = getattr(cache, "memory", None)
    if memory is not None and (
        getattr(memory, "location", None) is not None
        or getattr(memory, "_verbose", 0)
    ):
        raise SystemExit(
            _CACHE_STOP.format("import 済みの librosa のキャッシュが有効です")
        )


# librosa の import はここだけ。キャッシュの関数の包みは import の時点で決まる
check_librosa_cache()
import librosa  # noqa: E402

check_librosa_cache()

# --- 定数（docs/decisions/0033。学習側の数字を見る前に固定） ---
SR = features.AUDIO_HZ  # 16000
FRAME_LEN = features.FRAME_LEN  # 400
FRAME_HOP = features.FRAME_HOP  # 160
PREEMPHASIS = features.PREEMPHASIS  # 0.97
N_FFT = features.N_FFT  # 512
LOG_FLOOR = features.LOG_FLOOR  # 1e-10
N_MFCC = features.N_MFCC  # 13
N_MEL = features.N_MEL  # 26
CENTROID_MIN_POWER = features.CENTROID_MIN_POWER
A_FMAX_HZ = 4000.0  # 案A と CNN のメルの上端（D3）
DELTA_WIDTH = 9  # librosa.feature.delta の width（mode="interp"）
N_MEL_CNN = 40
WINDOW_SAMPLES = features.WINDOW_SAMPLES  # 16000（判定の窓 1.0 秒）
# 複数窓の長さ（秒）。中心を判定の窓の中心にそろえる
MW_LENGTHS_S = (0.5, 1.0, 2.0)
FEATURE_SETS = ("base15", "a52", "base15_mw", "a52_mw")
CNN_SET = "mfe40"
# 判定の窓の終端より後に要る音（秒）。装置で結果が出るまでの追加の待ち時間
EXTRA_LATENCY_S = {
    "base15": 0.0,
    "a52": 0.0,
    "base15_mw": 0.5,
    "a52_mw": 0.5,
    CNN_SET: 0.0,
}

_FFT_FREQS = librosa.fft_frequencies(sr=SR, n_fft=N_FFT)
_MEL_BASE = librosa.filters.mel(
    sr=SR,
    n_fft=N_FFT,
    n_mels=N_MEL,
    fmin=0.0,
    fmax=features.MEL_FMAX_HZ,
    htk=True,
    norm=None,
    dtype=np.float64,
)
# 案A と CNN のメル行列は features_np が読む .npy（librosa.filters.mel の値。
# make_features_np_consts.py で作り、テストで一致を確かめる）
_MEL_A = features_np.MEL26
_MEL_CNN = features_np.MEL40

A_STATS = ("mean", "max", "std", "dabs")
BASE15_NAMES: tuple[str, ...] = features.AUDIO_FEATURE_NAMES
A52_NAMES: tuple[str, ...] = tuple(
    f"a_{st}_{i}" for st in A_STATS for i in range(N_MFCC)
)


def _mw_names(names) -> tuple[str, ...]:
    return tuple(
        f"w{int(round(1000 * L)):04d}ms_{n}"
        for L in MW_LENGTHS_S
        for n in names
    )


FEATURE_NAMES: dict[str, tuple[str, ...]] = {
    "base15": BASE15_NAMES,
    "a52": A52_NAMES,
    "base15_mw": _mw_names(BASE15_NAMES),
    "a52_mw": _mw_names(A52_NAMES),
}
N_DIMS = {k: len(v) for k, v in FEATURE_NAMES.items()}


def _check_set(feature_set: str) -> None:
    if feature_set not in FEATURE_SETS:
        raise ValueError(
            f"特徴量の組は {' / '.join(FEATURE_SETS)} のどれかです: {feature_set!r}"
        )


# --- 1 つの区間の式 ---


def _power(x: np.ndarray) -> np.ndarray:
    """区間の波形 → フレームごとの |FFT|² ÷ N_FFT。(フレーム数, 257)。"""
    return features_np.power(x)


def _preemph(x: np.ndarray) -> np.ndarray:
    """区間全体に掛けてから切る（0012 のまま）。"""
    return features_np.preemph(x)


def _mfcc(x: np.ndarray, mel_fb: np.ndarray) -> np.ndarray:
    """区間の波形（float64）→ フレームごとの MFCC 13。(フレーム数, 13)。
    base15 だけが使う（DCT は scipy）。"""
    logmel = np.log(_power(_preemph(x)) @ mel_fb.T + LOG_FLOOR)
    return scipy.fft.dct(logmel, type=2, norm="ortho", axis=1)[:, :N_MFCC]


def base15_segment(x: np.ndarray) -> np.ndarray:
    """区間の波形 → 15 次元（0012 の式）。FRAME_LEN に満たなければ全部 0。"""
    out = np.zeros(N_MFCC + 2)
    x = np.asarray(x, dtype=np.float64)
    if len(x) < FRAME_LEN:
        return out
    out[:N_MFCC] = _mfcc(x, _MEL_BASE).mean(axis=0)
    p = _power(x)  # スペクトル重心はプリエンファシス前
    total = p.sum(axis=1)
    ok = total >= CENTROID_MIN_POWER
    centroid = np.zeros(len(p))
    centroid[ok] = (p[ok] @ _FFT_FREQS) / total[ok]
    out[N_MFCC] = centroid.mean()
    pos = (x - x.mean()) >= 0  # 窓内の平均を引いた波形の符号
    out[N_MFCC + 1] = np.count_nonzero(pos[1:] != pos[:-1]) / (len(x) - 1)
    return out


def a_stats(C: np.ndarray) -> np.ndarray:
    """係数行列 (フレーム数, 13) → 52 次元（平均、最大、標準偏差（÷ n）、
    デルタの絶対値の平均）。フレームが DELTA_WIDTH に満たなければ全部 0。"""
    # デルタは features_np の numpy の式（librosa.feature.delta の width 9・
    # interp と同じ値）。キャッシュの確かめは残す（0033）
    check_librosa_cache()  # import の後にキャッシュを有効にされた場合
    return features_np.a_stats(C)


def a52_segment(x: np.ndarray) -> np.ndarray:
    """区間の波形 → 52 次元（案A）。フレームが足りなければ全部 0。"""
    x = np.asarray(x, dtype=np.float64)
    if len(x) < FRAME_LEN:
        return np.zeros(4 * N_MFCC)
    return a_stats(features_np.mfcc_a(x))


def mfe_one(seg: np.ndarray) -> np.ndarray:
    """判定の窓の波形（16000 サンプル）→ 対数メル 40 本 (98, 40) float32。
    16000 に満たなければ全部 0（features_np.mfe40）。"""
    return features_np.mfe40(seg)


def logmel_frames(x: np.ndarray) -> np.ndarray:
    """任意の長さの波形 → フレームごとの対数メル 40 本（図に使う）。"""
    x = np.asarray(x, dtype=np.float64)
    if len(x) < FRAME_LEN:
        return np.zeros((0, N_MEL_CNN))
    return features_np.logmel(x, _MEL_CNN)


_SEGMENT_FN = {"base15": base15_segment, "a52": a52_segment}


def features_one(
    seg_by_length: Mapping[float, np.ndarray], feature_set: str
) -> np.ndarray:
    """1 窓の特徴量。seg_by_length は窓の長さ（秒）→ その窓の波形（16 kHz）。
    単一窓の組は {1.0: 16000 サンプル}、_mw の組は MW_LENGTHS_S の 3 つ
    （記録の範囲の端では切り詰めた長さでよい）。判定の窓（1.0 秒）が 16000
    サンプルに満たなければ、その部分は全部 0（0012 の規則）。"""
    _check_set(feature_set)
    base = feature_set.removesuffix("_mw")
    fn = _SEGMENT_FN[base]
    lengths = MW_LENGTHS_S if feature_set.endswith("_mw") else (1.0,)
    parts = []
    for L in lengths:
        seg = seg_by_length[L]
        if L == 1.0 and len(seg) < WINDOW_SAMPLES:
            parts.append(np.zeros(N_DIMS[base]))
        elif L == 1.0:
            parts.append(fn(seg[:WINDOW_SAMPLES]))
        else:
            parts.append(fn(seg))
    return np.concatenate(parts)


def segment_bounds(start: int, length_s: float, lo: int, hi: int) -> tuple:
    """判定の窓の開始サンプル start に対して、中心をそろえた長さ length_s の
    窓の [a, b)。[lo, hi) に切り詰める。0.5 秒は start + 4000、2.0 秒は
    start − 8000 から（判定の窓の開始のサンプルからの固定の差）。"""
    n = int(round(length_s * SR))
    a = int(start) + (WINDOW_SAMPLES - n) // 2
    return max(a, lo), min(a + n, hi)


def window_features(
    x16k: np.ndarray, starts, feature_set: str, lo: int, hi: int
) -> np.ndarray:
    """メモリ上の 16 kHz の信号から、判定の窓（開始サンプル starts）ごとの
    特徴量 (n, 次元) float32。どの窓も [lo, hi) の外を切り出さない。"""
    _check_set(feature_set)
    lengths = MW_LENGTHS_S if feature_set.endswith("_mw") else (1.0,)
    X = np.zeros((len(starts), N_DIMS[feature_set]), dtype=np.float32)
    for k, i in enumerate(starts):
        segs = {}
        for L in lengths:
            a, b = segment_bounds(i, L, lo, hi)
            segs[L] = x16k[a:b] if b > a else x16k[0:0]
        X[k] = features_one(segs, feature_set)
    return X


def _session_bounds(x, sample_t, start_idx, t1_s) -> tuple:
    """[lo, hi): lo = 最初の窓の開始サンプル（o start に最も近いサンプル）、
    hi = 時刻が t1 より前のサンプルの数。"""
    lo = int(start_idx[0]) if len(start_idx) else 0
    hi = min(len(x), int(np.searchsorted(sample_t[:-1], t1_s, side="left")))
    return lo, hi


def extract(session_dir: Path, feature_set: str) -> features.WindowFeatures:
    """セッション1つの窓ごとの特徴量（正規化前）。t_start_s・valid は
    features.extract_throat_session と同じ。"""
    _check_set(feature_set)
    session_dir = Path(session_dir)
    x, sample_t, t_start, start_idx, valid, _, t1 = features._throat_windows(
        session_dir
    )
    lo, hi = _session_bounds(x, sample_t, start_idx, t1)
    X = window_features(x, start_idx, feature_set, lo, hi)
    return features.WindowFeatures(
        session=session_dir.name, t_start_s=t_start, X=X, valid=valid
    )


def extract_mfe(session_dir: Path) -> tuple:
    """CNN の入力: (t_start_s, M (n, 98, 40) float32, valid)。"""
    x, sample_t, t_start, start_idx, valid, _, t1 = features._throat_windows(
        Path(session_dir)
    )
    lo, hi = _session_bounds(x, sample_t, start_idx, t1)
    M = np.zeros((len(t_start), 98, N_MEL_CNN), dtype=np.float32)
    for k, i in enumerate(start_idx):
        a, b = segment_bounds(i, 1.0, lo, hi)
        M[k] = mfe_one(x[a:b])
    return t_start, M, valid


# --- 小さな CNN の numpy の順伝播（docs/decisions/0033。学習は compare.py） ---

# 本体（CNN_CONV、_conv3x3、_maxpool2、cnn_forward）は features_np.py（装置と
# PC で同じコード）。compare.py とテストの呼び出しのために同じ名前で出す
from features_np import (  # noqa: E402, F401
    CNN_CONV,
    _conv3x3,
    _maxpool2,
    cnn_forward,
)


def cnn_param_count() -> int:
    return sum(ci * co * 9 + co for ci, co in CNN_CONV) + CNN_CONV[-1][1] + 1


def cnn_macs(h: int = 98, w: int = N_MEL_CNN) -> int:
    """1 窓あたりの積和の数（畳み込みと全結合）。"""
    total = 0
    for k, (ci, co) in enumerate(CNN_CONV):
        total += h * w * co * ci * 9
        if k < 2:
            h, w = h // 2, w // 2
    return total + CNN_CONV[-1][1]


def _main() -> None:  # pragma: no cover
    print(__doc__)
    print({k: N_DIMS[k] for k in FEATURE_SETS})
    print("CNN: パラメータ", cnn_param_count(), "積和", cnn_macs())


if __name__ == "__main__":
    _main()
