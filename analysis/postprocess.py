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

M4 の後処理（Issue #45、plan #45 の 5.4。決める構成は docs/decisions/0038）:
PostStepper  1 窓ずつ進める状態機械（因果的。装置と PC で同じコード）。
             ヒステリシス → 不応期 → 塊の上限 の順に当てる。
post_mask    配列の関数。PostStepper を先頭から回すだけ。cap_s と
             refractory_s が None なら hysteresis とビット一致する。
ensemble_mean  種ごとの確率（シグモイドの出力）の算術平均。
装置に配るので、numpy と evaluate のほかは import しない（device/unoq/DEPLOY_FILES）。
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


# --- M4 の後処理（plan #45 の 5.4。実データの前に固定） ---

CAP_GRID_S = (1.5, 2.0, 2.5)  # 塊の上限の格子（秒。HOP_S の整数倍）
REFRACTORY_S = 0.5  # 不応期（人の決定 10/14。この長さだけ）


def _check_post(enter: float, exit: float, cap_s, refractory_s) -> None:
    if not 0.0 < exit <= enter <= 1.0:
        raise ValueError(
            f"0 < exit <= enter <= 1 ではありません（enter {enter}, exit {exit}）"
        )
    if cap_s is not None:
        n = cap_s / HOP_S
        if not cap_s > 0 or abs(n - round(n)) > 1e-9:
            raise ValueError(
                f"塊の上限 cap_s は None か {HOP_S} 秒の正の整数倍です: {cap_s}"
            )
    if refractory_s is not None and refractory_s != REFRACTORY_S:
        raise ValueError(
            f"不応期 refractory_s は None か {REFRACTORY_S} です: {refractory_s}"
        )


class PostStepper:
    """1 窓ずつ進める後処理（plan #45 の 5.4）。step(p, valid, t) が
    その窓の陽性・陰性を返す。p は確率、t は窓の開始時刻（秒、HOP_S の等間隔）。

    1. ヒステリシス: 状態 h は初期は偽。h が偽なら valid かつ p >= enter で真、
       真なら valid かつ p >= exit のときだけ真のまま（hysteresis と同じ規則）。
    2. h が偽から真になった窓で新しい「h の塊」が始まる。
    3. 不応期: 新しい塊の最初の窓の t と、直前の塊の最後の窓の t の差が
       refractory_s 以下なら、その塊を塊ごと抑える（直前の塊が抑えられた・
       上限で切られた塊でも、その最後の窓から数える）。
    4. 塊の上限: 抑えていない塊の最初の窓から round(cap_s / HOP_S) 窓までを
       陽性にし、それより後は陰性（塊の中で陽性に戻らない）。
    出力 = h が真 かつ 塊が抑えられていない かつ 上限の内。
    """

    def __init__(
        self, enter: float, exit: float, cap_s=None, refractory_s=None
    ):
        _check_post(enter, exit, cap_s, refractory_s)
        self.enter, self.exit = enter, exit
        self.refractory_s = refractory_s
        self.cap_n = None if cap_s is None else int(round(cap_s / HOP_S))
        self.h = False
        self.suppressed = False
        self.count = 0  # 今の h の塊の窓の数
        self.last_h_t = None  # 直前（または今）の h の塊の最後の窓の t
        self.prev_t = None

    def step(self, p, valid, t) -> bool:
        t = float(t)
        if (
            self.prev_t is not None
            and abs(t - self.prev_t - HOP_S) > GRID_TOL_S
        ):
            raise ValueError(
                f"窓の開始時刻が {HOP_S} 秒の等間隔ではありません"
                f"（{self.prev_t} → {t}）"
            )
        self.prev_t = t
        v = bool(valid)
        p = float(p)
        if self.h:
            h = v and p >= self.exit
        else:
            h = v and p >= self.enter
        if h and not self.h:
            self.count = 0
            self.suppressed = (
                self.refractory_s is not None
                and self.last_h_t is not None
                and t - self.last_h_t <= self.refractory_s + GRID_TOL_S
            )
        self.h = h
        if not h:
            return False
        self.last_h_t = t
        self.count += 1
        if self.suppressed:
            return False
        return self.cap_n is None or self.count <= self.cap_n


def post_mask(
    prob,
    valid,
    t_start_s,
    enter: float,
    exit: float,
    cap_s=None,
    refractory_s=None,
) -> np.ndarray:
    """PostStepper を先頭から回した陽性窓の mask。値の検査は PostStepper と
    同じ。t_start_s が HOP_S の等間隔でなければ ValueError。"""
    _check_post(enter, exit, cap_s, refractory_s)
    prob = np.asarray(prob, dtype=np.float64)
    valid = np.asarray(valid, dtype=bool)
    t = np.asarray(t_start_s, dtype=np.float64)
    if len(prob) != len(valid) or len(prob) != len(t):
        raise ValueError("prob・valid・t_start_s の長さがそろっていません")
    _check_grid(t)
    st = PostStepper(enter, exit, cap_s, refractory_s)
    out = np.zeros(len(prob), dtype=bool)
    for k in range(len(prob)):
        out[k] = st.step(prob[k], valid[k], t[k])
    return out


def ensemble_mean(probs) -> np.ndarray:
    """種ごとの確率の算術平均（窓ごと）。空、または長さがそろわなければ
    ValueError。"""
    arrs = [np.asarray(p, dtype=np.float64) for p in probs]
    if not arrs:
        raise ValueError("平均する確率がありません")
    if any(a.ndim != 1 or len(a) != len(arrs[0]) for a in arrs):
        raise ValueError("平均する確率の長さがそろっていません")
    return np.sum(arrs, axis=0) / len(arrs)
