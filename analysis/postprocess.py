"""窓ごとの陽性確率から陽性の窓の列を作る後処理（Issue #43、docs/decisions/0033）。

threshold   (prob >= thr) & valid（train_eval.positive_windows と同じ規則）。
hysteresis  入り enter・出 exit の状態機械。窓を時刻順に見る。初期は陰性。
            陰性のとき valid かつ prob >= enter で陽性に入る。陽性のとき
            prob < exit または valid = False で陰性に出る。出力はその窓の状態。
            exit == enter のとき threshold と一致する。

出力の mask で t_start_s[mask] を evaluate.event_metrics の陽性窓に渡す。数え方・
窓・非嚥下区間は変えない。多数決は入れない（人の決定 10/12）。

注記: exit < enter のヒステリシスは陽性の連続を延ばすだけなので、陽性窓の割合は
増える。誤検出の回数（連続の塊の数）は、途切れた陽性がつながれば減ることがあり、
嚥下の区間の中だけだった塊が非嚥下区間まで延びれば増えることもある。
"""

from __future__ import annotations

import numpy as np

from evaluate import HOP_S

# 格子（D9）: enter ∈ {0.05, …, 0.95}、exit = enter − d（d ∈ {0.1, 0.2, 0.3}、
# exit ≥ 0.05）。45 点
HYST_ENTERS = tuple(round(0.05 * k, 2) for k in range(1, 20))
HYST_GAPS = (0.1, 0.2, 0.3)
HYST_GRID = tuple(
    (e, round(e - d, 2))
    for e in HYST_ENTERS
    for d in HYST_GAPS
    if round(e - d, 2) >= 0.05
)
HYST_DEFAULT = (0.7, 0.4)  # 参考として常に並べる行
GRID_TOL_S = 1e-6  # 窓の開始時刻の間隔と HOP_S の差の許容


def threshold(prob, valid, thr: float) -> np.ndarray:
    return (np.asarray(prob) >= thr) & np.asarray(valid, dtype=bool)


def _check_grid(t_start_s) -> None:
    t = np.asarray(t_start_s, dtype=np.float64)
    if len(t) > 1 and np.any(np.abs(np.diff(t) - HOP_S) > GRID_TOL_S):
        raise ValueError(
            f"窓の開始時刻が {HOP_S} 秒の等間隔ではありません（格子が崩れた入力）"
        )


def hysteresis(prob, valid, enter: float, exit: float, t_start_s) -> np.ndarray:
    """入り・出の状態機械。0 < exit <= enter <= 1 でなければ ValueError。
    t_start_s が HOP_S の等間隔でなければ ValueError。"""
    if not 0.0 < exit <= enter <= 1.0:
        raise ValueError(
            f"0 < exit <= enter <= 1 ではありません（enter {enter}, exit {exit}）"
        )
    prob = np.asarray(prob, dtype=np.float64)
    valid = np.asarray(valid, dtype=bool)
    if len(prob) != len(valid) or len(prob) != len(t_start_s):
        raise ValueError("prob・valid・t_start_s の長さがそろっていません")
    _check_grid(t_start_s)
    out = np.zeros(len(prob), dtype=bool)
    on = False
    for k in range(len(prob)):
        if on:
            on = bool(valid[k]) and prob[k] >= exit
        else:
            on = bool(valid[k]) and prob[k] >= enter
        out[k] = on
    return out
