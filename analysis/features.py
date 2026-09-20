"""窓ごとの特徴量（IMU＋音、29 次元）。式の定義は docs/decisions/0012。

入力: docs/data-schema.md のセッションフォルダ（imu.csv、audio.wav、audio_chunks.csv、meta.json）。
出力: extract_session が窓（1.0 秒 / 0.25 秒）ごとの特徴量 X と、取りこぼしに掛かる窓の目印 valid。
時間軸は装置の t_ms ÷ 1000（秒）。evaluate.load_events と同じ軸で、0 への付け替えはしない。

正規化は Standardizer で、呼び出し側が渡したセッション（学習側）だけから統計量を作る。
生の波形と IMU の系列は戻り値に含めない。特徴量をファイルに書く機能は置かない。
窓へのラベル付けと学習は #13。

使い方: python analysis/features.py <セッションフォルダ>
"""
from __future__ import annotations
import argparse, csv, json, math, sys, wave
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

from evaluate import WINDOW_S, HOP_S   # 窓長とホップは二重に定義しない

# --- 定数（docs/decisions/0012。#13 の結果を見てから動かさない） ---
AUDIO_HZ = 16000                  # docs/data-schema.md。これ以外の WAV は受け付けない
AUDIO_SCALE = 32768.0             # int16 → float32
WINDOW_SAMPLES = int(round(WINDOW_S * AUDIO_HZ))   # 16000
AUDIO_CHUNK_T_OFFSET_MS = 0.0     # 先頭サンプルの時刻への換算（t_ms − チャンクの長さぶん）のあとに足す補正。
                                  # 系統的なずれが分かったとき用（docs/decisions/0013）
GAP_RATIO = 1.5                   # 1周期のこの倍以上の差分を飛びとする（tools/check_session.py と同じ）。
                                  # 音声は、チャンクの間の空白が周期の GAP_RATIO − 1 倍以上
MIN_IMU_ROW_RATIO = 0.9           # 窓の IMU 行数が期待のこの割合未満なら valid = False

PEAK_HEIGHT_RMS_RATIO = 1.0       # ピークの高さの下限 = 窓内の RMS × この値
PEAK_MIN_DISTANCE_S = 0.050       # ピークの最小間隔
AXIS_SIGN_EPS = 1e-6              # 主軸の符号をそろえるとき、成分を 0 とみなす境
AXIS_MIN_EIGENVALUE = 1e-8        # g²。第1固有値がこれ未満なら静止とみなす
AXIS_MIN_EIGEN_GAP_RATIO = 0.01   # λ1 − λ2 < この値 × λ1 なら主軸は一意に決まらない
AXIS_DEFAULT = (0.0, 1.0, 0.0)    # 主軸の既定値（Y 軸 = 首の上下方向）

FRAME_LEN = 400                   # 25 ms
FRAME_HOP = 160                   # 10 ms
PREEMPHASIS = 0.97
N_FFT = 512
N_MEL = 26
MEL_FMIN_HZ, MEL_FMAX_HZ = 0.0, 8000.0
LOG_FLOOR = 1e-10                 # log(E + 1e-10)
N_MFCC = 13                       # c0 を含む先頭 13 係数
CENTROID_MIN_POWER = 1e-12        # フレームの Σ P がこれ未満ならスペクトル重心は 0

FEATURE_NAMES: tuple[str, ...] = (
    "acc_ptp_x", "acc_ptp_y", "acc_ptp_z", "gyro_norm_ptp",
    "acc_rms_x", "acc_rms_y", "acc_rms_z", "gyro_rms_x", "gyro_rms_y", "gyro_rms_z",
    "acc_peak_count",
    "acc_axis_x", "acc_axis_y", "acc_axis_z",
    *(f"mfcc_{i}" for i in range(N_MFCC)),
    "spectral_centroid_hz", "zero_crossing_rate",
)
N_FEATURES = len(FEATURE_NAMES)   # 29
N_IMU_FEATURES = 14


@dataclass(frozen=True, eq=False)
class WindowFeatures:
    session: str             # フォルダ名
    t_start_s: np.ndarray    # (n,) 窓の開始時刻。evaluate.event_metrics の positive_windows と同じ軸
    X: np.ndarray            # (n, 29) float32。正規化前
    valid: np.ndarray        # (n,) bool。取りこぼしに掛かる窓は False


@dataclass(frozen=True, eq=False)
class Standardizer:
    """平均 0・標準偏差 1 への変換。統計量は fit に渡されたセッションの valid な窓だけから作る。"""
    mean: np.ndarray
    std: np.ndarray
    sessions: tuple[str, ...]   # 統計量に使ったセッション名（評価側が混ざっていないことの確認用）

    @classmethod
    def fit(cls, feats: Sequence[WindowFeatures]) -> "Standardizer":
        used = [f for f in feats if f.valid.any()]
        if not used:
            raise ValueError("有効な窓が1つもありません")
        X = np.concatenate([f.X[f.valid] for f in used]).astype(np.float64)
        std = X.std(axis=0)
        std[std == 0] = 1.0   # 分散が 0 の次元は割らない（変換後は 0）
        return cls(mean=X.mean(axis=0), std=std, sessions=tuple(f.session for f in used))

    def transform(self, X: np.ndarray) -> np.ndarray:
        X = np.asarray(X)
        if X.ndim == 0 or X.shape[-1] != N_FEATURES:
            raise ValueError(f"末尾の次元が {N_FEATURES} ではありません: {X.shape}")
        return ((X.astype(np.float64) - self.mean) / self.std).astype(np.float32)


# --- 読み込みと検証 ---

def _read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _load_imu(session: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """imu.csv → (t_ms, 加速度 (n,3) [g], ジャイロ (n,3) [deg/s])。"""
    rows = _read_csv(session / "imu.csv")
    if len(rows) < 2:
        raise ValueError(f"imu.csv のデータ行が2行未満です: {session}")
    t_ms = np.array([int(r["t_ms"]) for r in rows], dtype=np.int64)
    if np.any(np.diff(t_ms) < 0):
        raise ValueError(f"imu.csv の t_ms が逆行しています: {session}")
    acc = np.array([[float(r[c]) for c in ("ax", "ay", "az")] for r in rows], dtype=np.float64)
    gyro = np.array([[float(r[c]) for c in ("gx", "gy", "gz")] for r in rows], dtype=np.float64)
    return t_ms, acc, gyro


def _declared_audio_hz(session: Path) -> int:
    """装置の申告値（トップレベル audio_hz）を優先し、無ければ sample_rates.audio_hz（docs/decisions/0006）。"""
    meta = json.loads((session / "meta.json").read_text(encoding="utf-8"))
    hz = meta.get("audio_hz")
    if hz is None:
        hz = (meta.get("sample_rates") or {}).get("audio_hz")
    if hz is None:
        raise ValueError(f"meta.json に audio_hz がありません: {session}")
    return int(hz)


def _load_audio(session: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """audio.wav と audio_chunks.csv → (波形 float32, チャンクの sample_index, チャンクの t_ms, チャンク長)。"""
    hz = _declared_audio_hz(session)
    with wave.open(str(session / "audio.wav"), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(f"audio.wav が mono・int16 ではありません: {session}")
        if hz != AUDIO_HZ or w.getframerate() != AUDIO_HZ:
            raise ValueError(f"サンプルレートが {AUDIO_HZ} Hz ではありません "
                             f"(meta.json {hz}, WAV {w.getframerate()}): {session}")
        pcm = w.readframes(w.getnframes())
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / AUDIO_SCALE

    rows = _read_csv(session / "audio_chunks.csv")
    if len(rows) < 2:
        raise ValueError(f"audio_chunks.csv のデータ行が2行未満です: {session}")
    t_ms = np.array([int(r["t_ms"]) for r in rows], dtype=np.int64)
    si = np.array([int(r["sample_index"]) for r in rows], dtype=np.int64)
    if si[0] != 0:
        raise ValueError(f"audio_chunks.csv の最初の sample_index が 0 ではありません: {session}")
    if np.any(np.diff(si) <= 0):
        raise ValueError(f"audio_chunks.csv の sample_index が単調増加ではありません: {session}")
    if np.any(np.diff(t_ms) < 0):
        raise ValueError(f"audio_chunks.csv の t_ms が逆行しています: {session}")
    chunk_len = Counter(np.diff(si).tolist()).most_common(1)[0][0]
    last_len = len(x) - int(si[-1])
    if not 1 <= last_len <= chunk_len:
        raise ValueError(f"audio.wav の長さ ({len(x)}) が audio_chunks.csv と合いません "
                         f"(最後の sample_index {si[-1]}, チャンク長 {chunk_len}): {session}")
    return x, si, t_ms, chunk_len


def _chunk_lengths(n: int, si: np.ndarray) -> np.ndarray:
    """チャンクごとのサンプル数 L[i] = sample_index[i+1] − sample_index[i]。最終行は WAV のサンプル数 n − sample_index[last]。"""
    return np.diff(np.append(si, n))


def _chunk_anchors_ms(t_ms: np.ndarray, lengths: np.ndarray) -> np.ndarray:
    """チャンクの先頭サンプルの時刻の推定値（ms）。

    audio_chunks.csv の t_ms は、装置がチャンクを読み出した直後の millis()。論理上はチャンクの終端側の時刻として扱い、
    チャンクの長さぶん戻す（docs/data-schema.md、docs/decisions/0013）。
    """
    return t_ms - 1000.0 * lengths / AUDIO_HZ + AUDIO_CHUNK_T_OFFSET_MS


def _audio_gaps(anchor_ms: np.ndarray, lengths: np.ndarray, chunk_len: int) -> list[tuple[float, float]]:
    """音声の飛びの区間 (前のチャンクの推定の終端, 次のチャンクの推定の先頭)（秒）。

    空白が、最頻のチャンク長から出す周期の GAP_RATIO − 1 倍以上の箇所を飛びとする。チャンク長が一定なら
    「t_ms の差分が 1.5 周期以上」（tools/check_session.py）と同じ判定。長さの違う正常なチャンクは空白が 0。
    """
    end_ms = anchor_ms + 1000.0 * lengths / AUDIO_HZ
    blank_ms = anchor_ms[1:] - end_ms[:-1]
    i = np.nonzero(blank_ms >= (GAP_RATIO - 1.0) * 1000.0 * chunk_len / AUDIO_HZ)[0]
    return [(end_ms[k] / 1000.0, anchor_ms[k + 1] / 1000.0) for k in i]


def _sample_times(n: int, si: np.ndarray, anchor_s: np.ndarray) -> np.ndarray:
    """サンプル 0〜n（n は最終サンプルの直後）の時刻。チャンクの間は線形補間、最後のアンカー以後は 16 kHz で補外。"""
    idx = np.arange(n + 1, dtype=np.float64)
    t = np.interp(idx, si.astype(np.float64), anchor_s)
    tail = idx > si[-1]
    t[tail] = anchor_s[-1] + (idx[tail] - si[-1]) / AUDIO_HZ
    return t


def _imu_period_ms(t_ms: np.ndarray) -> float:
    """IMU の1周期 = 飛びでない t_ms の差分（中央値の 1.5 倍未満）の平均（tools/check_session.py と同じ）。"""
    d = np.diff(t_ms)
    med = float(np.median(d))
    normal = d[d < GAP_RATIO * med]
    period = float(normal.mean()) if len(normal) else med
    if period <= 0:
        raise ValueError("imu.csv の1周期の推定値が 0 以下です。t_ms の並びを確認してください")
    return period


def _gaps(t_ms: np.ndarray, period_ms: float) -> list[tuple[float, float]]:
    """IMU 用。差分が 1.5 周期以上の箇所を、飛びの区間 (前の時刻, 次の時刻)（秒）で返す。"""
    i = np.nonzero(np.diff(t_ms) >= GAP_RATIO * period_ms)[0]
    return [(t_ms[k] / 1000.0, t_ms[k + 1] / 1000.0) for k in i]


# --- 特徴量 ---

def _count_peaks(x: np.ndarray, t_s: np.ndarray) -> int:
    """局所極大の数。高さが RMS × PEAK_HEIGHT_RMS_RATIO 以上、間隔が PEAK_MIN_DISTANCE_S 未満なら高いほうを残す。"""
    if len(x) < 3:
        return 0
    rms = math.sqrt(float(np.mean(x ** 2)))
    cand = np.nonzero((x[1:-1] > x[:-2]) & (x[1:-1] >= x[2:]))[0] + 1
    cand = cand[x[cand] >= PEAK_HEIGHT_RMS_RATIO * rms]
    kept: list[int] = []
    for i in cand[np.argsort(-x[cand], kind="stable")]:
        if all(abs(t_s[i] - t_s[j]) >= PEAK_MIN_DISTANCE_S for j in kept):
            kept.append(int(i))
    return len(kept)


def _principal_axis(acc: np.ndarray) -> np.ndarray:
    """平均を引いた加速度 (n,3) の共分散（÷ n）の第1固有ベクトル。決まらないときは AXIS_DEFAULT。"""
    w, v = np.linalg.eigh(acc.T @ acc / len(acc))   # 固有値は昇順
    l1, l2 = w[2], w[1]
    if l1 < AXIS_MIN_EIGENVALUE or l1 - l2 < AXIS_MIN_EIGEN_GAP_RATIO * l1:
        return np.array(AXIS_DEFAULT)
    axis = v[:, 2]
    for k in (1, 0, 2):   # Y、X、Z の順で、最初の 0 でない成分を正にそろえる
        if abs(axis[k]) >= AXIS_SIGN_EPS:
            return axis if axis[k] > 0 else -axis
    return np.array(AXIS_DEFAULT)


def _imu_features(t_s: np.ndarray, acc: np.ndarray, gyro: np.ndarray) -> np.ndarray:
    """窓に入った IMU の行 → 14 次元。2 行未満なら全部 0。"""
    out = np.zeros(N_IMU_FEATURES)
    if len(t_s) < 2:
        return out
    acc = acc - acc.mean(axis=0)     # 重力・姿勢を除く
    gyro = gyro - gyro.mean(axis=0)  # ゼロ点のずれを除く
    out[0:3] = np.ptp(acc, axis=0)
    out[3] = np.ptp(np.linalg.norm(gyro, axis=1))
    out[4:7] = np.sqrt(np.mean(acc ** 2, axis=0))
    out[7:10] = np.sqrt(np.mean(gyro ** 2, axis=0))
    out[10] = _count_peaks(np.linalg.norm(acc, axis=1), t_s)
    out[11:14] = _principal_axis(acc)
    return out


def _mel_filterbank() -> np.ndarray:
    """三角フィルタ N_MEL 本（HTK 式のメル尺度、面積の正規化なし）。(N_MEL, N_FFT/2+1)。"""
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


_FFT_FREQS = np.arange(N_FFT // 2 + 1) * AUDIO_HZ / N_FFT
_MEL_FB = _mel_filterbank()
_DCT = _dct_matrix()
_HAMMING = np.hamming(FRAME_LEN)
_FRAME_INDEX = (np.arange(0, WINDOW_SAMPLES - FRAME_LEN + 1, FRAME_HOP)[:, None]
                + np.arange(FRAME_LEN)[None, :])   # (98, 400)


def _power_frames(x: np.ndarray) -> np.ndarray:
    """16000 サンプル → フレームごとのパワースペクトル |FFT|² ÷ N_FFT。(98, 257)。"""
    spec = np.fft.rfft(x[_FRAME_INDEX] * _HAMMING, n=N_FFT, axis=1)
    return (spec.real ** 2 + spec.imag ** 2) / N_FFT


def _audio_features(x: np.ndarray) -> np.ndarray:
    """窓の波形 → 15 次元（MFCC 13、スペクトル重心、ゼロ交差率）。16000 サンプルに満たなければ全部 0。"""
    out = np.zeros(N_MFCC + 2)
    if len(x) < WINDOW_SAMPLES:
        return out
    x = x[:WINDOW_SAMPLES].astype(np.float64)
    pre = np.append(x[0], x[1:] - PREEMPHASIS * x[:-1])
    logmel = np.log(_power_frames(pre) @ _MEL_FB.T + LOG_FLOOR)
    out[:N_MFCC] = (logmel @ _DCT.T).mean(axis=0)
    p = _power_frames(x)   # スペクトル重心はプリエンファシス前
    total = p.sum(axis=1)
    ok = total >= CENTROID_MIN_POWER
    centroid = np.zeros(len(p))
    centroid[ok] = (p[ok] @ _FFT_FREQS) / total[ok]
    out[N_MFCC] = centroid.mean()
    pos = (x - x.mean()) >= 0
    out[N_MFCC + 1] = np.count_nonzero(pos[1:] != pos[:-1]) / (len(x) - 1)
    return out


def extract_session(session_dir: Path) -> WindowFeatures:
    """セッション1つの窓ごとの特徴量（正規化前）。窓は落とさず、グリッドは等間隔のまま返す。"""
    session_dir = Path(session_dir)
    imu_t_ms, acc, gyro = _load_imu(session_dir)
    audio, chunk_si, chunk_t_ms, chunk_len = _load_audio(session_dir)

    imu_t = imu_t_ms / 1000.0
    imu_period_ms = _imu_period_ms(imu_t_ms)
    chunk_lengths = _chunk_lengths(len(audio), chunk_si)
    anchor_ms = _chunk_anchors_ms(chunk_t_ms, chunk_lengths)
    sample_t = _sample_times(len(audio), chunk_si, anchor_ms / 1000.0)
    gaps = _gaps(imu_t_ms, imu_period_ms) + _audio_gaps(anchor_ms, chunk_lengths, chunk_len)

    # 各ストリームの範囲は半開区間。音声は最終サンプルの直後まで、IMU は最後の行 + 1周期まで
    t_lo = max(imu_t[0], sample_t[0])
    t_hi = min(imu_t[-1] + imu_period_ms / 1000.0, sample_t[-1])
    span = t_hi - t_lo
    n = int(math.floor((span - WINDOW_S) / HOP_S + 1e-9)) + 1 if span >= WINDOW_S else 0
    t_start = t_lo + HOP_S * np.arange(n)

    X = np.zeros((n, N_FEATURES), dtype=np.float32)
    valid = np.ones(n, dtype=bool)
    min_rows = MIN_IMU_ROW_RATIO * WINDOW_S / (imu_period_ms / 1000.0)
    for k, s in enumerate(t_start):
        a, b = np.searchsorted(imu_t, [s, s + WINDOW_S], side="left")
        # 音声は窓の開始時刻に最も近いサンプルから 16000 サンプル固定
        i = int(np.searchsorted(sample_t[:-1], s, side="left"))
        if i > 0 and (i == len(audio) or s - sample_t[i - 1] <= sample_t[i] - s):
            i -= 1
        seg = audio[i:i + WINDOW_SAMPLES]
        X[k, :N_IMU_FEATURES] = _imu_features(imu_t[a:b], acc[a:b], gyro[a:b])
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
    print(f"窓の数: {n}（窓 {WINDOW_S} 秒 / ホップ {HOP_S} 秒）")
    print(f"次元: {f.X.shape[1]}")
    print(f"valid: {int(f.valid.sum())} / {n}")
    if n:
        print(f"t_start_s: {f.t_start_s[0]:.3f} 〜 {f.t_start_s[-1]:.3f}")


if __name__ == "__main__":
    main()
