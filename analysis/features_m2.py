"""M2 の窓ごとの特徴量（IMU＋音、29 次元）。音の式の定義は docs/decisions/0020（段階 1 の素案）。

docs/decisions/0012 の式（`analysis/features.py`、M1 の定義）のうち、音の 15 次元（MFCC 13・スペクトル重心・
ゼロ交差率）だけを M2 用に置き換えたもの。IMU 14 次元、読み込みと検証、チャンクの時刻の換算、飛び、窓の格子、
`valid` の規則は `features.py` の関数をそのまま使う（二重に定義しない）。`features.py` は変えない。

音の式（0012 との違い）:
- 特徴量の計算に使う音は 8 kHz。16 kHz の隣り合う 2 サンプルの平均 `x8[i] = (x16[2i] + x16[2i+1]) >> 1`
  （int32 で足して算術シフト。装置と同じ整数演算。長さが奇数なら最後の 1 サンプルを捨てる）。
  8 kHz のサンプル j の時刻は 16 kHz のサンプル 2j の時刻。記録形式（audio.wav 16 kHz）は変えない。
- 窓は 8000 サンプル固定（窓の開始時刻に最も近い 8 kHz のサンプルから）。窓の格子は features.py と同じ。
- フレーム 25 ms（200）/ stride 25 ms（200、重なりなし）。1 窓 40 フレーム、1 ホップ（2000 サンプル）10 フレーム。
- プリエンファシス 0.97 はフレームごとに独立（各フレームの pre[0] = x[0]）。
- Hamming（200）→ rfft 256 → |X|² ÷ 256（129 ビン）→ メル 26 本（0〜4000 Hz、HTK 式）→ log(E + 1e-10)
  → DCT-II（ortho）の先頭 13 → 40 フレームの平均。
- スペクトル重心は MFCC と同じ（プリエンファシス後の）パワースペクトルから Σ f·P / Σ P。Σ P < 1e-12 のフレームは 0。
- ゼロ交差率は 8 kHz の 8000 サンプルで、平均を引いた符号（≥ 0 を正）の変化回数 ÷ 7999。

次元数・順序・名前は features.FEATURE_NAMES と同じ。M1 の値と取り違えない目印は FEATURE_SET。
生の波形と IMU の系列は戻り値に含めない。特徴量をファイルに書く機能は置かない。

使い方: python analysis/features_m2.py <セッションフォルダ>
"""
from __future__ import annotations
import argparse, math, sys
from pathlib import Path

import numpy as np

import features
from evaluate import WINDOW_S, HOP_S   # 窓長とホップは二重に定義しない
from features import (WindowFeatures, Standardizer, FEATURE_NAMES, N_FEATURES, N_IMU_FEATURES,   # noqa: F401
                      AUDIO_SCALE, MIN_IMU_ROW_RATIO)

# --- 定数（docs/decisions/0020。段階 1 の素案。人が式を確定してから固定する） ---
FEATURE_SET = "m2-0020"           # meta.json の feature_set と m2_norm.json に書く識別子
AUDIO_HZ_IN = 16000               # 記録形式（docs/data-schema.md）。features._load_audio が検証する
AUDIO_HZ = 8000                   # 特徴量の計算に使う周波数
DECIMATION = AUDIO_HZ_IN // AUDIO_HZ                # 2
WINDOW_SAMPLES = int(round(WINDOW_S * AUDIO_HZ))     # 8000
HOP_SAMPLES = int(round(HOP_S * AUDIO_HZ))           # 2000
FRAME_LEN = 200                   # 25 ms
FRAME_HOP = 200                   # 25 ms（重なりなし）。退避先 f′ は 400
PREEMPHASIS = 0.97
N_FFT = 256
N_MEL = 26
MEL_FMIN_HZ, MEL_FMAX_HZ = 0.0, 4000.0
LOG_FLOOR = 1e-10                 # log(E + 1e-10)
N_MFCC = 13                       # c0 を含む先頭 13 係数
CENTROID_MIN_POWER = 1e-12        # フレームの Σ P がこれ未満ならスペクトル重心は 0
N_AUDIO_FEATURES = N_MFCC + 2     # 15

assert AUDIO_HZ_IN == features.AUDIO_HZ
assert DECIMATION * AUDIO_HZ == AUDIO_HZ_IN
assert HOP_SAMPLES % FRAME_HOP == 0, "1 ホップがフレームの stride の整数倍でないと装置の再利用版と格子が合わない"


# --- 音の表 ---

def _mel_filterbank() -> np.ndarray:
    """三角フィルタ N_MEL 本（HTK 式のメル尺度、FFT のビンの周波数で評価、面積の正規化なし）。(N_MEL, N_FFT/2+1)。"""
    def to_mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)
    mel = np.linspace(to_mel(MEL_FMIN_HZ), to_mel(MEL_FMAX_HZ), N_MEL + 2)
    hz = 700.0 * (10.0 ** (mel / 2595.0) - 1.0)
    fb = np.zeros((N_MEL, len(_FFT_FREQS)))
    for m in range(N_MEL):
        lo, c, hi = hz[m:m + 3]
        fb[m] = np.clip(np.minimum((_FFT_FREQS - lo) / (c - lo), (hi - _FFT_FREQS) / (hi - c)), 0.0, None)
    return fb


def _dct_matrix() -> np.ndarray:
    """DCT-II（ortho）の先頭 N_MFCC 行。(N_MFCC, N_MEL)。"""
    k = np.arange(N_MFCC)[:, None]
    m = np.arange(N_MEL)[None, :]
    d = np.cos(np.pi * k * (2 * m + 1) / (2 * N_MEL)) * math.sqrt(2.0 / N_MEL)
    d[0] *= math.sqrt(0.5)
    return d


_FFT_FREQS = np.arange(N_FFT // 2 + 1) * AUDIO_HZ / N_FFT   # 31.25 Hz 刻み
_MEL_FB = _mel_filterbank()
_DCT = _dct_matrix()
_HAMMING = np.hamming(FRAME_LEN)


def _frame_index() -> np.ndarray:
    """フレームの添字 (n_frames, FRAME_LEN)。FRAME_HOP を差し替えたときも呼ぶたびに作り直す。"""
    starts = np.arange(0, WINDOW_SAMPLES - FRAME_LEN + 1, FRAME_HOP)
    return starts[:, None] + np.arange(FRAME_LEN)[None, :]


# --- 音の処理 ---

def _decimate(x16: np.ndarray) -> np.ndarray:
    """16 kHz の int16 → 8 kHz の int16。隣り合う 2 サンプルの平均 (a + b) >> 1（int32、算術シフト）。奇数長は最後を捨てる。"""
    x = np.asarray(x16)
    if x.dtype != np.int16:
        raise ValueError(f"int16 ではありません: {x.dtype}")
    n = (len(x) // DECIMATION) * DECIMATION
    pairs = x[:n].astype(np.int32).reshape(-1, DECIMATION)
    return (pairs.sum(axis=1) >> 1).astype(np.int16)


def _to_int16(x: np.ndarray) -> np.ndarray:
    """features._load_audio の float32（int16 ÷ 32768）を int16 に戻す。÷ 32768 は 2 の冪なので float32 で厳密。"""
    x = np.asarray(x, dtype=np.float32) * np.float32(AUDIO_SCALE)
    if not np.array_equal(x, np.round(x)) or x.min() < -32768 or x.max() > 32767:
        raise ValueError("音声の値が int16 ÷ 32768 の形ではありません")
    return x.astype(np.int16)


def _power_frames(x: np.ndarray) -> np.ndarray:
    """8000 サンプル → フレームごとの独立なプリエンファシス → Hamming → rfft → |X|² ÷ N_FFT。(n_frames, N_FFT/2+1)。"""
    frames = x[_frame_index()]
    pre = np.empty_like(frames)
    pre[:, 0] = frames[:, 0]
    pre[:, 1:] = frames[:, 1:] - PREEMPHASIS * frames[:, :-1]
    spec = np.fft.rfft(pre * _HAMMING, n=N_FFT, axis=1)
    return (spec.real ** 2 + spec.imag ** 2) / N_FFT


def _audio_features(x: np.ndarray) -> np.ndarray:
    """8 kHz の窓の波形（float32、int16 ÷ 32768）→ 15 次元。WINDOW_SAMPLES に満たなければ全部 0。"""
    out = np.zeros(N_AUDIO_FEATURES)
    if len(x) < WINDOW_SAMPLES:
        return out
    x = x[:WINDOW_SAMPLES].astype(np.float64)
    p = _power_frames(x)
    logmel = np.log(p @ _MEL_FB.T + LOG_FLOOR)
    out[:N_MFCC] = (logmel @ _DCT.T).mean(axis=0)
    total = p.sum(axis=1)                       # スペクトル重心は MFCC と同じパワースペクトルから
    ok = total >= CENTROID_MIN_POWER
    centroid = np.zeros(len(p))
    centroid[ok] = (p[ok] @ _FFT_FREQS) / total[ok]
    out[N_MFCC] = centroid.mean()
    pos = (x - x.mean()) >= 0
    out[N_MFCC + 1] = np.count_nonzero(pos[1:] != pos[:-1]) / (len(x) - 1)
    return out


def extract_session(session_dir: Path) -> WindowFeatures:
    """セッション1つの窓ごとの特徴量（正規化前）。窓の格子と valid の規則は features.extract_session と同じ。"""
    session_dir = Path(session_dir)
    imu_t_ms, acc, gyro = features._load_imu(session_dir)
    audio16, chunk_si, chunk_t_ms, chunk_len = features._load_audio(session_dir)

    imu_t = imu_t_ms / 1000.0
    imu_period_ms = features._imu_period_ms(imu_t_ms)
    chunk_lengths = features._chunk_lengths(len(audio16), chunk_si)
    anchor_ms = features._chunk_anchors_ms(chunk_t_ms, chunk_lengths)
    sample_t16 = features._sample_times(len(audio16), chunk_si, anchor_ms / 1000.0)
    gaps = features._gaps(imu_t_ms, imu_period_ms) + features._audio_gaps(anchor_ms, chunk_lengths, chunk_len)

    # セッション全体で 1 回間引く。8 kHz のサンプル j の時刻は 16 kHz のサンプル 2j の時刻
    audio8 = _decimate(_to_int16(audio16)).astype(np.float32) / AUDIO_SCALE
    sample_t8 = sample_t16[0:DECIMATION * len(audio8) + 1:DECIMATION]   # 最後は最終サンプルの直後
    del audio16

    # 窓の格子は features.py と同じ（各ストリームの範囲は半開区間。音声の終端は 16 kHz の最終サンプルの直後）
    t_lo = max(imu_t[0], sample_t16[0])
    t_hi = min(imu_t[-1] + imu_period_ms / 1000.0, sample_t16[-1])
    span = t_hi - t_lo
    n = int(math.floor((span - WINDOW_S) / HOP_S + 1e-9)) + 1 if span >= WINDOW_S else 0
    t_start = t_lo + HOP_S * np.arange(n)

    X = np.zeros((n, N_FEATURES), dtype=np.float32)
    valid = np.ones(n, dtype=bool)
    min_rows = MIN_IMU_ROW_RATIO * WINDOW_S / (imu_period_ms / 1000.0)
    for k, s in enumerate(t_start):
        a, b = np.searchsorted(imu_t, [s, s + WINDOW_S], side="left")
        # 音声は窓の開始時刻に最も近い 8 kHz のサンプルから WINDOW_SAMPLES 固定
        i = int(np.searchsorted(sample_t8[:-1], s, side="left"))
        if i > 0 and (i == len(audio8) or s - sample_t8[i - 1] <= sample_t8[i] - s):
            i -= 1
        seg = audio8[i:i + WINDOW_SAMPLES]
        X[k, :N_IMU_FEATURES] = features._imu_features(imu_t[a:b], acc[a:b], gyro[a:b])
        X[k, N_IMU_FEATURES:] = _audio_features(seg)
        if b - a < min_rows or len(seg) < WINDOW_SAMPLES:
            valid[k] = False
        if any(g0 < s + WINDOW_S and g1 > s for g0, g1 in gaps):
            valid[k] = False
    return WindowFeatures(session=session_dir.name, t_start_s=t_start, X=X, valid=valid)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session_dir", type=Path, help="セッションフォルダ")
    a = ap.parse_args()
    if not a.session_dir.is_dir():
        sys.exit(f"セッションフォルダが見つかりません: {a.session_dir}")
    f = extract_session(a.session_dir)
    n = len(f.t_start_s)
    # 特徴量の値は表示しない
    print(f"セッション: {f.session}")
    print(f"特徴量の式: {FEATURE_SET}（音 {AUDIO_HZ} Hz、フレーム {FRAME_LEN} / {FRAME_HOP}）")
    print(f"窓の数: {n}（窓 {WINDOW_S} 秒 / ホップ {HOP_S} 秒）")
    print(f"次元: {f.X.shape[1]}")
    print(f"valid: {int(f.valid.sum())} / {n}")
    if n:
        print(f"t_start_s: {f.t_start_s[0]:.3f} 〜 {f.t_start_s[-1]:.3f}")


if __name__ == "__main__":
    main()
