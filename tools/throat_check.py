#!/usr/bin/env python3
"""咽喉マイクだけのセッション（meta.json の fw が pc-throat）を記録の直後に確かめる
（Issue #36、docs/decisions/0029）。check_session.py の咽喉マイク版。

使い方: python tools/throat_check.py <セッションフォルダ> [--tap-mode enter|finger]

出すもの（集計値だけ。波形・サンプルの値は出さない）:
  - フレーム数と秒数、meta.json の overrun の行・推定差（診断値）・止まり方、到着の行数
  - 受信の時刻による時刻の対応（下の sample_time_map）: 10 秒ごとの区間の最小の
    アンカーの段差（50 ms 超）、直線の c0 とクロックの差（ppm）、残差
  - L と R の相関と RMS の比
  - 叩き（events.csv の o で note が tap のもの）の見つかった数と Δ
  - 使わない目安に当たるか

時刻の対応（受信の時刻。主）:
  throat_chunks.csv の読み取り k の最後のフレーム e_k について、
  アンカー a_k = t_ms_k − 1000 × (e_k + 1) ÷ fs（ms）。a_k は取り込みの時刻 +
  遅れ（≥ 0）なので、下側の包絡を取る。記録を 10 秒ごとの区間（サンプルの公称の
  時刻 1000 × (e_k + 1) ÷ fs で分ける。端の 10 秒未満は直前の区間に入れる）に分け、
  区間ごとの a_k の最小値を区間の中央の時刻に対して直線で当てはめる（最小二乗）。
  サンプル i の時刻 τ(i) = 1000 × i ÷ fs + c0 + s × (1000 × i ÷ fs)。
  区間が 2 つ未満（20 秒未満の記録）は全体の最小値を c0、s = 0。
"""

from __future__ import annotations
import argparse, csv, json, sys, wave
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

BIN_MS = 10_000  # 区間の長さ
STEP_MS = 50.0  # 隣り合う区間の最小の差がこれを超えたら段差
ENV_MS = 20  # 包絡の窓
FS_ANALYSIS = 16_000  # 解析の基準（docs/data-schema.md）
PERIOD_MS = 125.0  # arecord の 1 ピリオド。推定差の目安
# 叩き（plan #36 第 4.3 節。記録を見る前に固定）
TAP_SEARCH_ENTER_MS = (-300.0, 300.0)
TAP_SEARCH_FINGER_MS = (-5000.0, 0.0)
TAP_BASE_MS = (-1300.0, -300.0)
TAP_MAD_K = 8.0
TAP_MIN_LSB = 5.0
TAP_MIN_FOUND = 3
TAP_MAX_ABS_MEDIAN_MS = 50.0


# --- 読み込み ---
def load_session(path) -> dict:
    """セッションのフォルダを読む。wav は int16 の (N, ch)。"""
    d = Path(path)
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    with wave.open(str(d / "throat.wav"), "rb") as w:
        fs = w.getframerate()
        ch = w.getnchannels()
        if w.getsampwidth() != 2:
            raise SystemExit("throat.wav が 16 bit ではありません")
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    x = x.reshape(-1, ch)
    with open(d / "throat_chunks.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))[1:]
    t_ms = np.array([float(r[0]) for r in rows])
    idx = np.array([int(r[1]) for r in rows], dtype=np.int64)
    with open(d / "events.csv", newline="", encoding="utf-8") as f:
        events = [
            (float(r[0]), r[1], r[2] if len(r) > 2 else "")
            for r in list(csv.reader(f))[1:]
        ]
    return {
        "dir": d,
        "meta": meta,
        "fs": fs,
        "wav": x,
        "chunk_t_ms": t_ms,
        "chunk_idx": idx,
        "events": events,
    }


def _as_session(session) -> dict:
    return session if isinstance(session, dict) else load_session(session)


# --- 時刻の対応 ---
def anchors(session) -> tuple[np.ndarray, np.ndarray]:
    """(公称の時刻 1000 × (e_k + 1) ÷ fs, アンカー a_k)。どちらも ms。"""
    s = _as_session(session)
    idx, t = s["chunk_idx"], s["chunk_t_ms"]
    n = len(s["wav"])
    ends = np.append(idx[1:], n) - 1  # e_k
    nominal = 1000.0 * (ends + 1) / s["fs"]
    return nominal, t - nominal


def bin_minima(session) -> tuple[np.ndarray, np.ndarray]:
    """区間ごとの (中央の公称の時刻, a_k の最小値)。端の 10 秒未満は直前の区間に
    入れる。"""
    nominal, a = anchors(session)
    if len(a) == 0:
        return np.array([]), np.array([])
    nbins = max(1, int(nominal[-1] // BIN_MS))
    b = np.minimum((nominal // BIN_MS).astype(int), nbins - 1)
    centers, mins = [], []
    for k in range(nbins):
        m = b == k
        if not m.any():
            continue
        start = k * BIN_MS
        end = (k + 1) * BIN_MS if k < nbins - 1 else nominal[-1]
        centers.append((start + end) / 2)
        mins.append(a[m].min())
    return np.array(centers), np.array(mins)


def sample_time_map(session) -> tuple[float, float]:
    """(c0 ms, s)。サンプル i の時刻 τ(i) = 1000 i/fs × (1 + s) + c0。"""
    centers, mins = bin_minima(session)
    if len(mins) == 0:
        raise ValueError("throat_chunks.csv に行がありません")
    if len(mins) < 2:
        return float(anchors(session)[1].min()), 0.0
    s, c0 = np.polyfit(centers, mins, 1)
    return float(c0), float(s)


def sample_to_time(i, c0: float, s: float, fs: int):
    """サンプル番号（fs の）→ PC の時計の ms。"""
    return 1000.0 * np.asarray(i, dtype=float) / fs * (1.0 + s) + c0


def marker_to_sample(t_ms, c0: float, s: float, fs: int):
    """マーカーの t_ms → サンプル番号（fs の。float）。"""
    return (np.asarray(t_ms, dtype=float) - c0) / (1.0 + s) * fs / 1000.0


def steps(session) -> list[tuple[float, float]]:
    """隣り合う区間の最小の差が STEP_MS を超えた所（中央の時刻 ms, 差 ms）。"""
    centers, mins = bin_minima(session)
    d = np.diff(mins)
    return [
        (float(centers[k + 1]), float(d[k]))
        for k in range(len(d))
        if abs(d[k]) > STEP_MS
    ]


def est_diff_frames(session) -> int:
    """推定差（診断値）= (T × fs + 最初の読み取りのフレーム数) − N（record.py と同じ）。"""
    s = _as_session(session)
    t, idx = s["chunk_t_ms"], s["chunk_idx"]
    if len(t) == 0:
        return 0
    first = (idx[1] if len(idx) > 1 else len(s["wav"])) - idx[0]
    T = (t[-1] - t[0]) / 1000.0
    return int(round(T * s["fs"] + first)) - len(s["wav"])


# --- 包絡と叩き ---
def to_analysis(session) -> np.ndarray:
    """L（ch0）を float にし、16 kHz に落とす（resample_poly(x, 1, 3)）。"""
    s = _as_session(session)
    if s["fs"] != 48000:
        raise SystemExit(f"audio_hz が 48000 ではありません: {s['fs']}")
    return resample_poly(s["wav"][:, 0].astype(float), 1, 3)


def moving_rms(x: np.ndarray, n: int) -> np.ndarray:
    """n サンプルの中心の移動 RMS（端は窓の中にある分で平均）。"""
    c = np.concatenate(([0.0], np.cumsum(x * x)))
    i = np.arange(len(x))
    lo = np.clip(i - n // 2, 0, len(x))
    hi = np.clip(i - n // 2 + n, 0, len(x))
    return np.sqrt((c[hi] - c[lo]) / np.maximum(hi - lo, 1))


def envelope(x16: np.ndarray, center: float) -> np.ndarray:
    return moving_rms(x16 - center, FS_ANALYSIS * ENV_MS // 1000)


def find_taps(session, mode: str = "enter", c0=None, s=None) -> list[dict]:
    """events.csv の o（note が tap）ごとに叩きを探す。各要素は
    {t_ms, found, delta_ms}（delta_ms は enter のときだけ）。"""
    ses = _as_session(session)
    if c0 is None:
        c0, s = sample_time_map(ses)
    taps = [e for e in ses["events"] if e[1] == "o" and e[2].strip() == "tap"]
    if not taps:
        return []
    x16 = to_analysis(ses)
    env = envelope(x16, float(np.median(x16)))
    fs = ses["fs"]
    ratio = fs // FS_ANALYSIS
    win = TAP_SEARCH_ENTER_MS if mode == "enter" else TAP_SEARCH_FINGER_MS

    def j_of(t):  # PC の時計の ms → 16 kHz のサンプル番号
        return int(round(float(marker_to_sample(t, c0, s, fs)) / ratio))

    out = []
    for t, _, _ in taps:
        r = {"t_ms": t, "found": False, "delta_ms": None}
        a, b = j_of(t + win[0]), j_of(t + win[1])
        ba, bb = j_of(t + TAP_BASE_MS[0]), j_of(t + TAP_BASE_MS[1])
        a, b = max(a, 0), min(b, len(env))
        ba, bb = max(ba, 0), min(bb, len(env))
        if b <= a or bb <= ba:
            out.append(r)
            continue
        base = env[ba:bb]
        med = float(np.median(base))
        mad = float(np.median(np.abs(base - med)))
        thr = med + max(TAP_MAD_K * mad, TAP_MIN_LSB)
        k = a + int(np.argmax(env[a:b]))
        if env[k] > thr:
            r["found"] = True
            if mode == "enter":
                j = k
                while j > 0 and env[j - 1] > thr:
                    j -= 1
                onset = float(sample_to_time(j * ratio, c0, s, fs))
                r["delta_ms"] = onset - t
        out.append(r)
    return out


# --- まとめ ---
def check(session, tap_mode: str = "enter") -> dict:
    ses = _as_session(session)
    fs = ses["fs"]
    meta = ses["meta"]
    th = meta.get("throat", {}) or {}
    n = len(ses["wav"])
    c0, s = sample_time_map(ses)
    nominal, a = anchors(ses)
    fit = c0 + s * nominal
    resid = a - fit
    st = steps(ses)
    L = ses["wav"][:, 0].astype(float)
    R = ses["wav"][:, 1].astype(float) if ses["wav"].shape[1] > 1 else None
    corr = rms_ratio = None
    if R is not None:
        sl, sr = L.std(), R.std()
        corr = float(np.corrcoef(L, R)[0, 1]) if sl > 0 and sr > 0 else None
        rl = float(np.sqrt(np.mean((L - L.mean()) ** 2)))
        rr = float(np.sqrt(np.mean((R - R.mean()) ** 2)))
        rms_ratio = rr / rl if rl > 0 else None
    taps = find_taps(ses, tap_mode, c0, s)
    found = [t for t in taps if t["found"]]
    deltas = np.array(
        [t["delta_ms"] for t in found if t["delta_ms"] is not None]
    )
    est = est_diff_frames(ses)
    est_ms = 1000.0 * est / fs
    flags = []
    if (th.get("overrun_lines") or 0) > 0:
        flags.append(f"overrun の行 {th.get('overrun_lines')} > 0")
    if abs(est_ms) > PERIOD_MS:
        flags.append(f"|推定差| {abs(est_ms):.0f} ms > {PERIOD_MS:.0f} ms")
    if st:
        flags.append(f"段差 {len(st)} 箇所")
    tap_flags = []
    if tap_mode == "enter":
        if len(found) < TAP_MIN_FOUND:
            tap_flags.append(f"叩き {len(found)} 回 < {TAP_MIN_FOUND}")
        elif abs(float(np.median(deltas))) > TAP_MAX_ABS_MEDIAN_MS:
            tap_flags.append(
                f"|Δ の中央値| {abs(float(np.median(deltas))):.1f} ms > {TAP_MAX_ABS_MEDIAN_MS:.0f} ms"
            )
    return {
        "frames": n,
        "seconds": n / fs,
        "fs": fs,
        "channels": int(ses["wav"].shape[1]),
        "meta_overrun_lines": th.get("overrun_lines"),
        "meta_est_diff_frames": th.get("est_diff_frames"),
        "meta_stop": th.get("stop"),
        "reads": int(len(a)),
        "est_diff_frames": est,
        "est_diff_ms": est_ms,
        "c0_ms": c0,
        "s": s,
        "ppm": s * 1e6,
        "resid_median_ms": float(np.median(resid)) if len(resid) else None,
        "resid_p95_ms": float(np.percentile(resid, 95)) if len(resid) else None,
        "resid_max_ms": float(resid.max()) if len(resid) else None,
        "steps": st,
        "lr_corr": corr,
        "lr_rms_ratio": rms_ratio,
        "tap_mode": tap_mode,
        "taps_total": len(taps),
        "taps_found": len(found),
        "tap_deltas_ms": [float(d) for d in deltas],
        "flags": flags,
        "tap_flags": tap_flags,
    }


def _fmt(v, f="{:.1f}"):
    return "なし" if v is None else f.format(v)


def report(r: dict) -> str:
    lines = [
        f"フレーム {r['frames']:,}（{r['seconds']:.3f} 秒、{r['fs']} Hz、{r['channels']} ch）",
        f"meta.json: overrun の行 {r['meta_overrun_lines']}、推定差 {r['meta_est_diff_frames']} フレーム、止まり方 {r['meta_stop']}",
        f"到着の行 {r['reads']}、推定差（診断値。到着から再計算）{r['est_diff_frames']:+,} フレーム（{r['est_diff_ms']:+.0f} ms）",
        f"時刻の対応: c0 {r['c0_ms']:.1f} ms、クロックの差 {r['ppm']:+.1f} ppm",
        f"  残差 中央値 {_fmt(r['resid_median_ms'])} ms、95 パーセンタイル {_fmt(r['resid_p95_ms'])} ms、最大 {_fmt(r['resid_max_ms'])} ms",
        f"  段差（{STEP_MS:.0f} ms 超）{len(r['steps'])} 箇所"
        + "".join(
            f"、{c / 1000:.0f} 秒付近 {d:+.0f} ms" for c, d in r["steps"]
        ),
        f"L と R: 相関 {_fmt(r['lr_corr'], '{:.3f}')}、RMS の比（R / L）{_fmt(r['lr_rms_ratio'], '{:.3f}')}",
    ]
    if r["tap_mode"] == "enter":
        d = np.array(r["tap_deltas_ms"])
        if len(d):
            q1, q3 = np.percentile(d, [25, 75])
            lines.append(
                f"叩き（enter）: {r['taps_found']} / {r['taps_total']} 回見つかった。"
                f"Δ 中央値 {np.median(d):+.1f} ms、四分位範囲 {q1:+.1f}〜{q3:+.1f} ms、"
                f"最小 {d.min():+.1f} ms、最大 {d.max():+.1f} ms"
            )
        else:
            lines.append(
                f"叩き（enter）: {r['taps_found']} / {r['taps_total']} 回見つかった"
            )
    else:
        lines.append(
            f"叩き（finger）: 参考の件数 {r['taps_found']} / {r['taps_total']}。"
            "マーカーと叩きの 1 対 1 の対応は確かめていない（Δ の判断は使わない）"
        )
    if r["flags"]:
        lines.append(
            "使わない目安に当たる（録り直す）: " + "、".join(r["flags"])
        )
    else:
        lines.append("使わない目安: 当たらない")
    if r["tap_flags"]:
        lines.append(
            "時刻の対応が確かめられない（集計の表に印、使うかは人）: "
            + "、".join(r["tap_flags"])
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("session")
    ap.add_argument("--tap-mode", choices=("enter", "finger"), default="enter")
    a = ap.parse_args(argv)
    ses = load_session(a.session)
    if ses["meta"].get("fw") != "pc-throat":
        print(f"fw が pc-throat ではありません: {ses['meta'].get('fw')}")
        return 1
    print(report(check(ses, a.tap_mode)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
