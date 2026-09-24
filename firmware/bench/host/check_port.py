"""移植の答え合わせ（PC 側、#17）。check_port の出力を analysis/features.py の値と比べる。

同じ合成信号を numpy で作り（synth.cpp と同じ xorshift32 と式）、_audio_features と _imu_features を呼ぶ。
(a) 全窓版の 15 + 14 次元、(b) 再利用版の連続 8 ホップ（最初の窓を満たした後の 8 窓）を Python の全窓の値と比べる。
許容差（plan #17）: MFCC・重心・RMS・peak-to-peak は相対 1e-3、ゼロ交差率とピーク数と主軸の符号は一致。
これは解析でも評価でもなく、移植の確認。analysis/ は変えない。

使い方: .venv/bin/python firmware/bench/host/check_port.py build-host/check_port
"""
from __future__ import annotations
import math, subprocess, sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "analysis"))
import features as F   # noqa: E402  analysis/features.py（読むだけ）

SEED_AUDIO = 0x12345678
SEED_IMU = 0x9ABCDEF1
AUDIO_SECONDS = 3
SINE16 = np.array([0, 3061, 5657, 7391, 8000, 7391, 5657, 3061,
                   0, -3061, -5657, -7391, -8000, -7391, -5657, -3061], dtype=np.int64)
REL_TOL = 1e-3


def xorshift32(seed: int, n: int) -> np.ndarray:
    s = seed & 0xFFFFFFFF or 1
    out = np.empty(n, dtype=np.uint64)
    for i in range(n):
        s ^= (s << 13) & 0xFFFFFFFF
        s ^= s >> 17
        s ^= (s << 5) & 0xFFFFFFFF
        out[i] = s
    return out


def synth_audio(n: int) -> np.ndarray:
    r = xorshift32(SEED_AUDIO, n)
    noise = (r % 2001).astype(np.int64) - 1000
    x = SINE16[np.arange(n) & 15] + noise
    return x.astype(np.int16)


def synth_imu(n_rows: int):
    r = xorshift32(SEED_IMU, 6 * n_rows).reshape(n_rows, 6)
    u = ((r % 20001).astype(np.int64) - 10000) / 10000.0
    rows = np.arange(n_rows, dtype=np.int64)
    t_ms = (rows * 948 + 50) // 100
    ts = t_ms / 1000.0
    acc = np.empty((n_rows, 3))
    gyro = np.empty((n_rows, 3))
    acc[:, 0] = 0.01 * u[:, 0]
    acc[:, 1] = 1.0 + 0.1 * np.sin(2.0 * np.pi * 5.0 * ts) + 0.01 * u[:, 1]
    acc[:, 2] = 0.01 * u[:, 2]
    gyro[:, 0] = 1.5 + 2.0 * u[:, 3]
    gyro[:, 1] = -0.7 + 2.0 * u[:, 4]
    gyro[:, 2] = 0.3 + 2.0 * u[:, 5]
    # 装置と同じく float32 に落とした値を入力にする
    return t_ms, acc.astype(np.float32).astype(np.float64), gyro.astype(np.float32).astype(np.float64)


def run_binary(path: str) -> dict:
    text = subprocess.run([path], check=True, capture_output=True, text=True).stdout
    out = {"reuse": {}}
    for line in text.splitlines():
        parts = line.split()
        if parts[0] == "reuse":
            out["reuse"][int(parts[1])] = np.array([float(v) for v in parts[2:]])
        else:
            out[parts[0]] = np.array([float(v) for v in parts[1:]])
    return out


def rel_err(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.abs(a - b) / np.abs(b)


def check_audio(label: str, got: np.ndarray, ref: np.ndarray) -> tuple[bool, float]:
    ok = True
    e = rel_err(got[:14], ref[:14])          # MFCC 13 と重心: 相対
    worst = float(e.max())
    if worst > REL_TOL:
        ok = False
    # ゼロ交差率: 変化回数（÷ (N − 1) の前の整数）が一致。C++ は float で返すので回数に戻して比べる
    got_count = int(round(got[14] * (F.WINDOW_SAMPLES - 1)))
    ref_count = int(round(ref[14] * (F.WINDOW_SAMPLES - 1)))
    zcr_match = got_count == ref_count
    if not zcr_match:
        ok = False
    print(f"  {label}: max_rel_err(mfcc,centroid)={worst:.3e} (at index {int(e.argmax())}) "
          f"zcr count {'match' if zcr_match else 'MISMATCH'} ({got_count} vs {ref_count})")
    return ok, worst


def check_imu(got: np.ndarray, ref: np.ndarray) -> tuple[bool, float]:
    ok = True
    e = rel_err(got[:10], ref[:10])          # ptp 4 と RMS 6: 相対
    worst = float(e.max())
    if worst > REL_TOL:
        ok = False
    peaks_match = got[10] == ref[10]
    if not peaks_match:
        ok = False
    signs_match = True
    for k in range(3):
        if abs(ref[11 + k]) >= F.AXIS_SIGN_EPS and np.sign(got[11 + k]) != np.sign(ref[11 + k]):
            signs_match = False
    if not signs_match:
        ok = False
    axis_abs = float(np.abs(got[11:14] - ref[11:14]).max())
    print(f"  imu: max_rel_err(ptp,rms)={worst:.3e} (at index {int(e.argmax())}) "
          f"peaks {'match' if peaks_match else 'MISMATCH'} ({got[10]:.0f} vs {ref[10]:.0f}) "
          f"axis signs {'match' if signs_match else 'MISMATCH'} max_abs_diff={axis_abs:.3e}")
    return ok, worst


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    got = run_binary(sys.argv[1])
    n = AUDIO_SECONDS * F.AUDIO_HZ
    x = synth_audio(n).astype(np.float32) / F.AUDIO_SCALE
    all_ok = True

    print("(a) 全窓版")
    ref_audio = F._audio_features(x[:F.WINDOW_SAMPLES])
    ok, _ = check_audio("audio", got["full_audio"], ref_audio)
    all_ok &= ok
    t_ms, acc, gyro = synth_imu(128)
    m = t_ms < 1000
    ref_imu = F._imu_features(t_ms[m] / 1000.0, acc[m], gyro[m])
    ok, _ = check_imu(got["full_imu"], ref_imu)
    all_ok &= ok
    print(f"  C++ full_audio: {np.array2string(got['full_audio'], precision=6, max_line_width=200)}")
    print(f"  Py  full_audio: {np.array2string(ref_audio, precision=6, max_line_width=200)}")
    print(f"  C++ full_imu:   {np.array2string(got['full_imu'], precision=6, max_line_width=200)}")
    print(f"  Py  full_imu:   {np.array2string(ref_imu, precision=6, max_line_width=200)}")

    print("(b) 再利用版（最初の窓を満たした後の 8 ホップ。k = 0 は最初の窓、参考）")
    worst_hops = 0.0
    hop_count = 0
    for k in sorted(got["reuse"]):
        start = k * F.WINDOW_SAMPLES // 4
        ref = F._audio_features(x[start:start + F.WINDOW_SAMPLES])
        ok, worst = check_audio(f"hop {k}", got["reuse"][k], ref)
        if k >= 1:
            all_ok &= ok
            worst_hops = max(worst_hops, worst)
            hop_count += 1
    print(f"  8 ホップの最大相対誤差: {worst_hops:.3e}（ホップ数 {hop_count}）")
    if hop_count != 8:
        print("  ホップ数が 8 ではありません")
        all_ok = False

    # 参考: 先頭フレームの既知の差（pre[0] に前のサンプルを使う）だけを Python で再現したときの、全窓版との差
    diffs = []
    for k in range(1, 9):
        start = k * F.WINDOW_SAMPLES // 4
        seg = x[start:start + F.WINDOW_SAMPLES].astype(np.float64)
        pre = np.append(seg[0] - F.PREEMPHASIS * float(x[start - 1]), seg[1:] - F.PREEMPHASIS * seg[:-1])
        logmel = np.log(F._power_frames(pre) @ F._MEL_FB.T + F.LOG_FLOOR)
        mfcc_prev = (logmel @ F._DCT.T).mean(axis=0)
        ref = F._audio_features(x[start:start + F.WINDOW_SAMPLES])
        diffs.append(rel_err(mfcc_prev, ref[:F.N_MFCC]).max())
    print(f"  参考: 先頭フレームの pre[0] の差だけを Python で再現したときの MFCC の最大相対差: {max(diffs):.3e}")

    print("PASS" if all_ok else "FAIL")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
