"""イベント単位の評価（docs/evaluation.md の定義）。

入力: セッションごとの嚥下マーカー（events.csv の `s`）、陽性と判定された窓の開始時刻、記録の範囲。
出力: event_metrics がセッションごとの検出率と誤検出率（回/分）、aggregate が複数セッションの合算、
混同行列（イベント単位）、評価に使ったセッション一覧。

定義が決めていない点の解釈は docs/decisions/0011。
窓単位の分割は実装しない。セッション単位の分割は split.py で行う。
"""
from __future__ import annotations
import csv, sys
from pathlib import Path

WINDOW_S = 1.0
HOP_S = 0.25
PRE_S, POST_S = 0.5, 1.5   # マーカー t に対する [t-0.5, t+1.5]
MIN_SESSIONS = 3           # docs/evaluation.md「評価には 3 セッション以上を使う」
MAX_GAP_MS = 1000          # 1ストリームの中で許す t_ms の飛び。IMU は約 9.5 ms、音声チャンクは 4 ms ごと


def load_events(session: Path, label: str = "s") -> list[float]:
    """events.csv から label の開始時刻（秒）を返す。"""
    out = []
    with (session / "events.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["label"] == label:
                out.append(int(row["t_ms"]) / 1000.0)
    return out


def session_span(session: Path) -> tuple[float, float]:
    """記録の範囲 (t0_s, duration_s)。imu.csv と audio_chunks.csv の t_ms を合わせた最小〜最大。

    ストリームの中で t_ms が逆行する、または MAX_GAP_MS を超えて飛ぶセッションは止める。
    外れた t_ms が1行あるだけで記録が長く見え、誤検出率が薄まるのを防ぐ。
    """
    t_ms = []
    for name in ("imu.csv", "audio_chunks.csv"):
        with (session / name).open(encoding="utf-8") as f:
            ts = [int(row["t_ms"]) for row in csv.DictReader(f)]
        for a, b in zip(ts, ts[1:]):
            if b < a or b - a > MAX_GAP_MS:
                raise ValueError(f"{name} の t_ms が逆行したか {MAX_GAP_MS} ms を超えて飛んでいます: "
                                 f"{a} → {b} ({session})")
        t_ms.extend(ts)
    if not t_ms:
        raise ValueError(f"t_ms が1行もありません: {session}")
    return min(t_ms) / 1000.0, (max(t_ms) - min(t_ms)) / 1000.0


def _hits(w: float, t: float) -> bool:
    """陽性窓 [w, w+1.0] の中心が、嚥下 t の閉区間 [t-0.5, t+1.5] に入るか。検出と誤検出で共通。"""
    return t - PRE_S <= w + WINDOW_S / 2 <= t + POST_S


def _swallow_time(swallow_t: list[float], t0_s: float, t1_s: float) -> float:
    """各嚥下の区間を記録の範囲に切り詰めて結合した、和集合の長さ（秒）。"""
    total, end = 0.0, None
    for t in sorted(swallow_t):
        lo, hi = max(t - PRE_S, t0_s), min(t + POST_S, t1_s)
        if end is None or lo > end:
            total += hi - lo
            end = hi
        elif hi > end:
            total += hi - end
            end = hi
    return total


def event_metrics(swallow_t: list[float], positive_windows: list[float], duration_s: float,
                  t0_s: float = 0.0) -> dict:
    """positive_windows: 陽性と判定された窓の開始時刻（秒）。記録の範囲は [t0_s, t0_s + duration_s]。"""
    t1_s = t0_s + duration_s
    for t in swallow_t:
        if not t0_s <= t <= t1_s:
            raise ValueError(f"嚥下マーカーが記録の範囲の外です: {t} (範囲 {t0_s}〜{t1_s})")
    positive_windows = sorted(set(positive_windows))
    detected = sum(1 for t in swallow_t if any(_hits(w, t) for w in positive_windows))
    # 陽性窓を連続の塊にまとめる。嚥下の区間では塊を切らない
    runs: list[list[float]] = []
    for w in positive_windows:
        if runs and w - runs[-1][-1] <= HOP_S * 1.5:
            runs[-1].append(w)
        else:
            runs.append([w])
    # どの嚥下の区間にも中心が入らない窓が1つでもある塊を、誤検出 1 回と数える
    fp_runs = sum(1 for run in runs
                  if any(not any(_hits(w, t) for t in swallow_t) for w in run))
    non_swallow_min = (duration_s - _swallow_time(swallow_t, t0_s, t1_s)) / 60.0
    return {
        "swallows": len(swallow_t),
        "detected": detected,
        "detection_rate": detected / len(swallow_t) if swallow_t else None,
        "false_positive_runs": fp_runs,
        "non_swallow_min": non_swallow_min,
        "false_positives_per_min": fp_runs / non_swallow_min if non_swallow_min > 0 else None,
    }


def aggregate(per_session: dict[str, dict]) -> dict:
    """セッション名 → event_metrics の結果、を回数の合算でまとめる。"""
    if len(per_session) < MIN_SESSIONS:
        raise ValueError(f"評価セッションが足りません: {len(per_session)} ({MIN_SESSIONS} 以上が必要)")
    swallows = sum(m["swallows"] for m in per_session.values())
    detected = sum(m["detected"] for m in per_session.values())
    fp_runs = sum(m["false_positive_runs"] for m in per_session.values())
    non_swallow_min = sum(m["non_swallow_min"] for m in per_session.values())
    return {
        "sessions": list(per_session),
        "swallows": swallows,
        "detected": detected,
        "detection_rate": detected / swallows if swallows else None,
        "false_positive_runs": fp_runs,
        "non_swallow_min": non_swallow_min,
        "false_positives_per_min": fp_runs / non_swallow_min if non_swallow_min > 0 else None,
        # イベント単位。TN は定義できないので出さない
        "confusion": {"tp": detected, "fn": swallows - detected, "fp": fp_runs, "tn": None},
    }


if __name__ == "__main__":
    print(__doc__)
    sys.exit(0)
