"""イベント単位の評価（docs/evaluation.md の定義）。

入力: セッションフォルダのリストと、窓ごとの陽性/陰性の判定結果。
出力: 検出率、誤検出率（回/分）、混同行列、評価に使ったセッション一覧。

窓単位の分割は実装しない。セッション単位の分割は split.py で行う。
"""
from __future__ import annotations
import csv, json, sys
from pathlib import Path

WINDOW_S = 1.0
HOP_S = 0.25
PRE_S, POST_S = 0.5, 1.5   # マーカー t に対する [t-0.5, t+1.5]


def load_events(session: Path, label: str = "s") -> list[float]:
    """events.csv から label の開始時刻（秒）を返す。"""
    out = []
    with (session / "events.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["label"] == label:
                out.append(int(row["t_ms"]) / 1000.0)
    return out


def event_metrics(swallow_t: list[float], positive_windows: list[float], duration_s: float) -> dict:
    """positive_windows: 陽性と判定された窓の開始時刻（秒）。"""
    positive_windows = sorted(positive_windows)
    detected = 0
    for t in swallow_t:
        lo, hi = t - PRE_S, t + POST_S
        if any(lo <= w < hi or lo <= w + WINDOW_S < hi for w in positive_windows):
            detected += 1
    # 非嚥下区間での陽性: 嚥下区間に重ならない陽性窓を、連続はまとめて 1 回と数える
    def in_swallow(w):
        return any(t - PRE_S <= w < t + POST_S for t in swallow_t)
    fp_runs, prev = 0, None
    for w in positive_windows:
        if in_swallow(w):
            prev = None
            continue
        if prev is None or w - prev > HOP_S * 1.5:
            fp_runs += 1
        prev = w
    swallow_time = len(swallow_t) * (PRE_S + POST_S)
    non_swallow_min = max(duration_s - swallow_time, 1e-9) / 60.0
    return {
        "swallows": len(swallow_t),
        "detected": detected,
        "detection_rate": detected / len(swallow_t) if swallow_t else None,
        "false_positive_runs": fp_runs,
        "false_positives_per_min": fp_runs / non_swallow_min,
    }


if __name__ == "__main__":
    print(__doc__)
    sys.exit(0)
