"""M4（検出器・咽喉マイク版）の誤検出を減らす候補を、学習側の交差検証だけで
比べ、構成を決める材料を出す（Issue #45、plan #45。決める構成は
docs/decisions/0038）。

使い方:
  python analysis/m4_compare.py data/raw --smoke
      予行（ステップ 1）。M4_EXPECTED_TRAIN が None の間だけ走る。学習側の一覧・
      fold・窓の数・学習の回数と、3 回の学習の時間・見積もり・メモリだけを出す。
      性能の数字は出さず、図も描かない（plan #45 の 8.2・11 節）。
  python analysis/m4_compare.py data/raw [--figures docs/log/<日付>-45]
      本番（ステップ 2）。M4_EXPECTED_TRAIN が入っていて 0037 の M4- の行と一致し、
      追跡しているファイルに未コミットの変更が無いときだけ走る（データを読む前に
      止まる）。分類器 5 × 後処理 5 = 25 行と参考の行の表、選んだ行、誤検出の中身。
  python analysis/m4_compare.py data/raw --final-model analysis/models/m4-0038.npz
      最終モデル（ステップ 3）。fixed_eval.M4_CONFIG が入っていて 0038 の
      M4-CONFIG の行と一致し、未コミットの変更が無いときだけ作る。

学習側の一覧は list_train（plan #45 の 3 節）。名前の日付が M4_TRAIN_CUTOFF より
後のフォルダは meta.json を含めて何も開かない（M4_EVAL_FROM 以降の評価側を含む）。
交差検証は train_eval.cv_groups（0014 の 4）。正規化・陽性の重み・hard negative は
fold の学習側だけで作る。hard negative は入れ子（外側の fold の学習側の中の交差
検証）で決める（5.3）。数え方は evaluate.event_metrics（0011）のまま。

報告は標準出力に Markdown。ファイルは書かない（--figures の PNG と --final-model の
重みのファイルを除く）。波形、特徴量の値、個々の窓の確率は出さない。

load_model・model_prob は numpy と features_np だけで計算する（#47 が同じ関数を
使う。ただしこのモジュールの import は scikit-learn・librosa・torch の経路を含む）。
"""

from __future__ import annotations
import argparse, csv, hashlib, os, resource, sys, time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np

import evaluate, features_np, fixed_eval, postprocess, split, train_eval
from train_eval import NEGATIVE, POSITIVE, UNUSED, Fold, Scored, SessionData

# --- 定数（plan #45 の 5・6 節。ステップ 1 で固定し、数字を見てから変えない） ---
M4_SELECT_FP_PER_MIN = 1.0  # 選び方の誤検出率の上限（0014 の 3.0 の読み替え）
M4_PASS_DETECTION_RATE = 0.85  # M4 の合格線（報告で満たすかを並べるだけ）
M4_PASS_FP_PER_MIN = 1.0
HN_WEIGHT = 3.0  # hard negative の窓の損失の重み（探索しない）
HN_OP = (0.95, 0.85)  # hard negative を決める固定の動作点（構成 J の値）
R0_OP = (0.95, 0.85)  # 参考の行 R0「構成 J のまま」
SEEDS = (0, 1, 2)
CNN_SET = "mfe40"
LR_SET = "a52"
MEAL = "meal"
CLS_ORDER = ("C1", "C2", "C3", "C4", "C5")  # 「単純」の順
POST_ORDER = ("P0", "P1", "P2", "P3", "P4")
CLS_LABEL = {
    "C1": "LR a52",
    "C2": "CNN 種 0",
    "C3": "CNN 種 0 ＋ hard negative",
    "C4": "CNN 種 0・1・2 の平均",
    "C5": "CNN 種 0・1・2 の平均（各種 hard negative）",
}
POST_LABEL = {
    "P0": "なし",
    "P1": "ヒステリシス",
    "P2": "ヒステリシス＋不応期 0.5 秒",
    "P3": "ヒステリシス＋塊の上限",
    "P4": "ヒステリシス＋塊の上限＋不応期 0.5 秒",
}
# 予行の見積もり（11 節）: 残りの学習用 meal 1 本の長さ（推測）と本数
SMOKE_MEAL_S = 25 * 60
MEAL_TARGET = 3
MEAL_DAYS = ("20261025", "20261026", "20261027", "20261028")
BUDGET_S = 3 * 3600  # 本番 1 回の予算（17 節の Q11）
# 誤検出の中身の主分類（7.1。この順に最初に当たった 1 つ）
FP_CATS = ("after", "before", "motion", "meal_conv", "meal_quiet", "other")
FP_CAT_LABEL = {
    "after": "嚥下の直後の延び",
    "before": "嚥下の直前",
    "motion": "動作に付くもの（t・c・n）",
    "meal_conv": "meal の会話あり",
    "meal_quiet": "meal の会話なし",
    "other": "その他",
}
MOTION_MARKERS = ("t", "c", "n")
MOTION_PAD_S = 1.0  # 塊の前後に見る長さ（窓の終わりの 1.0 秒後まで）
# 予行の報告に出さない語（plan #45 の 8.2。テストで報告の文字列を確かめる）
SMOKE_FORBIDDEN = ("検出", "陽性", "確率", "hard negative", "選んだ")


# --- 学習側の一覧（3 節） ---


@dataclass(frozen=True)
class Listing:
    train: list  # 採る名前（self、pc-throat、締めまで）
    after_cutoff: list  # 締めより後・評価側の窓より前（名前だけ）
    eval_window: list  # M4_EVAL_FROM 以降（名前だけ。何も開かない）
    excluded: list  # 検査を当てて採らない（fw 無し・detector・p1）
    skipped: list  # trial-throat


def list_train(root: Path) -> Listing:
    """data/raw の直下を名前順に見て、M4 の学習側の名前を返す（データは読まない）。

    締め（M4_TRAIN_CUTOFF）より後の名前は meta.json を含めて開かない。残りには
    split.split_sessions と同じ検査を同じ順で当て、外れたら止まる。最後に
    fixed_eval.assert_m4_train を当てる。M3 の assert_fixed_eval_not_in は
    呼ばない（0037 の 2）。"""
    root = Path(root)
    train, late, ev, excluded, skipped = [], [], [], [], []
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue
        if p.is_symlink():
            raise SystemExit(
                f"セッションのフォルダがシンボリックリンクです: {p.name}"
            )
        if p.name in split.SKIP_DIRS:
            skipped.append(p.name)
            continue
        m = split.NAME_RE.fullmatch(p.name)
        if m is None:
            raise SystemExit(
                f"フォルダ名が形式に合いません（消すか直す）: {p.name}"
            )
        day = p.name[:8]
        if day > fixed_eval.M4_TRAIN_CUTOFF:
            # 名前だけ。meta.json も開かない（0037 の 2・3）
            (ev if day >= fixed_eval.M4_EVAL_FROM else late).append(p.name)
            continue
        meta = split._meta(p)
        if meta["subject"] != m.group(1):
            raise SystemExit(
                f"フォルダ名と meta.json の subject が違います: {p.name}"
            )
        fw = meta.get("fw")
        if fw != split.THROAT_FW and (p / "throat.wav").exists():
            raise SystemExit(
                f"throat.wav があるのに meta.json の fw が {split.THROAT_FW} ではありません: {p.name}"
            )
        if fw not in (split.THROAT_FW, split.DETECTOR_FW) and any(
            (p / n).is_file() for n in split.DETECTOR_FILES
        ):
            raise SystemExit(
                f"検出器のファイル（detect.csv / feat.csv）があるのに meta.json の fw が {split.DETECTOR_FW} では"
                f"ありません（META が届かなかったセッションは消す）: {p.name}"
            )
        if m.group(1) != train_eval.SUBJECT:
            excluded.append(p.name)  # p1 は M4 に入れない（0037）
            continue
        if fw == split.THROAT_FW:
            split._check_throat(p, meta)
            train.append(p.name)
        else:
            excluded.append(p.name)
    fixed_eval.assert_m4_train(train)
    return Listing(train, late, ev, excluded, skipped)


def cond_of(name: str) -> str:
    return name.rsplit("_", 1)[-1]


def is_meal(name: str) -> bool:
    return cond_of(name) == MEAL


# --- 読み込み ---


@dataclass(frozen=True)
class Info:
    cond: str
    markers: dict  # t・c・n → 秒の並び
    conv: bool  # o conv がある（meal の会話あり）


def load_info(session: Path) -> Info:
    conv = False
    with (Path(session) / "events.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["label"] == "o" and row["note"].strip() == "conv":
                conv = True
    return Info(
        cond=cond_of(Path(session).name),
        markers={
            lb: evaluate.load_events(Path(session), lb) for lb in MOTION_MARKERS
        },
        conv=conv,
    )


def load_all(root: Path, names: Sequence[str], lr: bool = True):
    """(CNN の data, LR の data | None, Info の辞書)。"""
    import compare

    root = Path(root)
    d_cnn = compare.load_train(root, names, CNN_SET)
    d_lr = compare.load_train(root, names, LR_SET) if lr else None
    info = {n: load_info(root / n) for n in names}
    return d_cnn, d_lr, info


# --- CNN の学習の使い回し（5.3） ---


@dataclass(frozen=True)
class _Name:
    name: str


def _names(data) -> list:
    return [d if isinstance(d, str) else d.name for d in data]


def train_key(seed: int, hn: bool, data) -> tuple:
    """学習の鍵 (種, hard negative の有無, 学習に使うセッションの集合)。"""
    return (seed, hn, frozenset(_names(data)))


def fit_rows(d: SessionData) -> int:
    return int((d.feats.valid & (d.labels != UNUSED)).sum())


class Trainer:
    """CNN の学習を鍵で使い回す。同じ鍵は同じ順・同じ種で学習するので同じ重み
    （5.3）。fit・score は compare.cnn_fit・cnn_score（テストでは偽物）。"""

    def __init__(self, fit=None, score=None):
        if fit is None or score is None:
            import compare

            fit = fit or compare.cnn_fit
            score = score or compare.cnn_score
        self.fit, self.score = fit, score
        self.models: dict = {}
        self.calls: list = []  # 学習した鍵（順に）
        self.fit_sets: list = []  # 各学習に渡したセッション名
        self.times: list = []  # (鍵, 秒, 学習に使った窓の数)
        self.hn_counts: dict = {}  # 鍵 → hard negative の窓の数
        self.score_sets: list = []  # (目的, 採点したセッション名)

    def _fit(self, key, data, where, seed, weights=None):
        t = time.perf_counter()
        if weights is None:
            m = self.fit(list(data), where, seed)
        else:
            m = self.fit(list(data), where, seed, sample_weight=weights)
        self.times.append(
            (key, time.perf_counter() - t, sum(fit_rows(d) for d in data))
        )
        self.calls.append(key)
        self.fit_sets.append(tuple(d.name for d in data))
        self.models[key] = m
        return m

    def plain(self, data, seed: int, where: str):
        key = train_key(seed, False, data)
        if key not in self.models:
            self._fit(key, data, where, seed)
        return self.models[key]

    def hard(self, data, seed: int, where: str):
        key = train_key(seed, True, data)
        if key not in self.models:
            weights, n = hn_weights(self, data, seed, where)
            self.hn_counts[key] = n
            self._fit(key, data, where, seed, weights)
        return self.models[key]

    def scored(self, model, d: SessionData, purpose: str) -> Scored:
        self.score_sets.append((purpose, d.name))
        return self.score(model, d)


def fp_runs(swallow_t: Sequence[float], pos_t: Sequence[float]) -> list:
    """evaluate.event_metrics と同じ塊のうち、誤検出に数える塊（窓の開始時刻の並び）。"""
    pos = sorted(set(pos_t))
    runs: list = []
    for w in pos:
        if runs and w - runs[-1][-1] <= evaluate.HOP_S * 1.5:
            runs[-1].append(w)
        else:
            runs.append([w])
    return [
        run
        for run in runs
        if any(not any(evaluate._hits(w, t) for t in swallow_t) for w in run)
    ]


def hn_windows(prob: np.ndarray, d: SessionData) -> np.ndarray:
    """固定の動作点 HN_OP で誤検出になった塊の、陰性のラベルで valid な窓の番号。"""
    t = d.feats.t_start_s
    m = postprocess.hysteresis(prob, d.feats.valid, *HN_OP, t)
    runs = fp_runs(d.swallows, t[m].tolist())
    if not runs:
        return np.zeros(0, dtype=np.int64)
    in_run = np.isin(t, np.array([w for run in runs for w in run]))
    return np.flatnonzero(in_run & d.feats.valid & (d.labels == NEGATIVE))


def hn_weights(trainer: Trainer, S: Sequence[SessionData], seed: int, where):
    """学習の集合 S の中の交差検証（cv_groups(S)）で、同じ種の hard negative なし
    の学習が出した検証側の確率から hard negative を決める（5.3）。条件別の
    セッションだけ。戻り値 (S と同じ順の窓の重み, hard negative の窓の数)。"""
    inner = train_eval.cv_groups(S)
    if len(inner) < 2:
        raise SystemExit(
            f"{where}: hard negative を決める内側の交差検証のグループが 2 つ未満です"
            f"（{', '.join(d.name for d in S)}）"
        )
    w = {d.name: np.ones(len(d.labels)) for d in S}
    n = 0
    for gname, G in inner:
        gn = {d.name for d in G}
        rest = [d for d in S if d.name not in gn]
        m = trainer.plain(
            rest,
            seed,
            f"{where} の内側（検証側 = {gname}: {', '.join(sorted(gn))}）",
        )
        for d in G:
            if is_meal(d.name):
                continue  # meal には付けない（17 節の Q6）
            idx = hn_windows(trainer.scored(m, d, "内側").prob, d)
            w[d.name][idx] = HN_WEIGHT
            n += len(idx)
    return [w[d.name] for d in S], n


def outer_groups(data) -> list:
    groups = train_eval.cv_groups(data)
    if len(groups) < 2:
        raise SystemExit(
            "交差検証のグループが 2 つ未満です（学習側: "
            f"{', '.join(d.name for d in data)}）。評価側のセッションでは補いません"
        )
    return groups


def cnn_cv(trainer: Trainer, data, seed: int, hn: bool):
    """外側の交差検証。fold の学習側だけで学習し、検証側を採点する。
    戻り値 (folds, scored)。"""
    folds, scored = [], []
    for name, val in outer_groups(data):
        vn = {d.name for d in val}
        rest = [d for d in data if d.name not in vn]
        where = f"CNN の交差検証の fold（検証側 = {name}: {', '.join(sorted(vn))}、種 {seed}{'、hard negative' if hn else ''}）"
        m = (trainer.hard if hn else trainer.plain)(rest, seed, where)
        folds.append(
            Fold(
                name=name,
                val_sessions=tuple(d.name for d in val),
                train_sessions=tuple(d.name for d in rest),
                standardizer_sessions=tuple(m.sessions),
                n_fit_rows=m.n_fit_rows,
            )
        )
        scored.extend(trainer.scored(m, d, "外側") for d in val)
    return folds, scored


def plan_trainings(data) -> dict:
    """本番の CNN の学習の鍵を、実際のグループから列挙する（11 節）。data は
    セッション名か .name を持つもの。戻り値 {"outer": 鍵の集合（hard negative
    なしの外側）, "inner": （hard negative を決める内側）, "hard": （hard negative
    ありの外側）, "all": 重複を除いた全部}。"""
    items = [_Name(n) for n in _names(data)]
    outer, inner, hard = set(), set(), set()
    for _, val in outer_groups(items):
        vn = {d.name for d in val}
        rest = [d for d in items if d.name not in vn]
        for seed in SEEDS:
            outer.add(train_key(seed, False, rest))
            hard.add(train_key(seed, True, rest))
            groups = train_eval.cv_groups(rest)
            for _, G in groups:
                gn = {d.name for d in G}
                inner.add(
                    train_key(
                        seed, False, [d for d in rest if d.name not in gn]
                    )
                )
    return {
        "outer": outer,
        "inner": inner,
        "hard": hard,
        "all": outer | inner | hard,
    }


# --- 後処理の動作点と行（5・6 節） ---


@dataclass(frozen=True)
class Op:
    post: str  # P0〜P4
    threshold: float | None = None
    enter: float | None = None
    exit: float | None = None
    cap_s: float | None = None
    refractory_s: float | None = None

    def tie(self) -> tuple:
        """同率のときに高いほうを採る順（P0 は閾値、ほかは 入り・出・上限）。"""
        if self.post == "P0":
            return (self.threshold,)
        return (self.enter, self.exit, self.cap_s or 0.0)


def post_grid(post: str) -> list:
    R = postprocess.REFRACTORY_S
    H = postprocess.HYST_GRID
    if post == "P0":
        return [Op("P0", threshold=t) for t in train_eval.THRESHOLDS]
    if post == "P1":
        return [Op("P1", enter=e, exit=x) for e, x in H]
    if post == "P2":
        return [Op("P2", enter=e, exit=x, refractory_s=R) for e, x in H]
    caps = postprocess.CAP_GRID_S
    if post == "P3":
        return [Op("P3", enter=e, exit=x, cap_s=c) for e, x in H for c in caps]
    if post == "P4":
        return [
            Op("P4", enter=e, exit=x, cap_s=c, refractory_s=R)
            for e, x in H
            for c in caps
        ]
    raise ValueError(f"後処理は P0〜P4 です: {post!r}")


def op_mask(s: Scored, op: Op) -> np.ndarray:
    f = s.data.feats
    if op.post == "P0":
        return postprocess.threshold(s.prob, f.valid, op.threshold)
    if op.post == "P1":
        # post_mask（上限・不応期なし）とビット一致する（test_postprocess）
        return postprocess.hysteresis(
            s.prob, f.valid, op.enter, op.exit, f.t_start_s
        )
    return postprocess.post_mask(
        s.prob,
        f.valid,
        f.t_start_s,
        op.enter,
        op.exit,
        op.cap_s,
        op.refractory_s,
    )


def per_session(scored: Sequence[Scored], op: Op) -> dict:
    out = {}
    for s in scored:
        d = s.data
        m = op_mask(s, op)
        out[d.name] = evaluate.event_metrics(
            d.swallows, d.feats.t_start_s[m].tolist(), d.duration_s, d.t0_s
        )
    return out


def grid_table(scored: Sequence[Scored], post: str) -> list:
    """格子の各点で採点して合算する。検出率か誤検出率が出ないなら止まる。"""
    names = ", ".join(s.data.name for s in scored)
    rows = []
    for op in post_grid(post):
        m = train_eval.sum_metrics(per_session(scored, op))
        if m["swallows"] == 0:
            raise SystemExit(
                f"交差検証の合算で嚥下が 0 件です（検出率が出ません）。検証したセッション: {names}"
            )
        if m["false_positives_per_min"] is None:
            raise SystemExit(
                f"交差検証の合算で非嚥下の時間が 0 です（誤検出率が出ません）。検証したセッション: {names}"
            )
        rows.append((op, m))
    return rows


def pick_point(table: Sequence[tuple]) -> tuple:
    """6.1: 誤検出率が 1.0 回/分以下の点のうち検出率が最大（同率なら 入り・出・
    上限 が高い・長いほう、P0 は閾値が高いほう）。無ければ誤検出率が最小（同率
    なら同じ順）。"""
    lim = M4_SELECT_FP_PER_MIN
    ok = [(op, m) for op, m in table if m["false_positives_per_min"] <= lim]
    if ok:
        op, _ = max(ok, key=lambda r: (r[1]["detection_rate"], *r[0].tie()))
        return op, (
            f"誤検出率が {lim} 回/分以下の点のうち、検出率が最大（同率なら 入り・出・上限 が高い・長いほう。"
            "P0 は閾値が高いほう）"
        )
    op, _ = min(
        table,
        key=lambda r: (
            r[1]["false_positives_per_min"],
            *(-v for v in r[0].tie()),
        ),
    )
    return op, (
        f"誤検出率が {lim} 回/分以下の点が無いので、誤検出率が最小（同率なら 入り・出・上限 が高い・長いほう）"
    )


def _subset(per: dict, keep) -> dict | None:
    sub = {n: m for n, m in per.items() if keep(n)}
    return train_eval.sum_metrics(sub) if sub else None


@dataclass(eq=False)
class M4Row:
    cls: str  # C1〜C5、参考の行は "R0" など
    post: str  # P0〜P4
    op: Op
    rule: str
    metrics: dict  # 合算
    per_session: dict
    positive_windows: int
    windows: int
    table: list | None  # 格子の各点（固定の動作点の行は None）
    candidate: bool = True
    label: str = ""

    @property
    def set(self) -> str:  # visualize.fig_compare と同じ名前
        return LR_SET if self.cls == "C1" else CNN_SET

    @property
    def classifier(self) -> str:
        return "LR" if self.cls == "C1" else "CNN"

    @property
    def positive_ratio(self):
        return self.positive_windows / self.windows if self.windows else None

    @property
    def meal(self):
        return _subset(self.per_session, is_meal)

    @property
    def conds(self):
        return _subset(self.per_session, lambda n: not is_meal(n))


def make_row(
    cls: str,
    post: str,
    scored: Sequence[Scored],
    candidate: bool = True,
    fixed: Op | None = None,
    label: str = "",
) -> M4Row:
    if fixed is None:
        table = grid_table(scored, post)
        op, rule = pick_point(table)
    else:
        table, op, rule = None, fixed, "動作点を選び直さない（固定）"
    per = per_session(scored, op)
    return M4Row(
        cls=cls,
        post=post,
        op=op,
        rule=rule,
        metrics=train_eval.sum_metrics(per),
        per_session=per,
        positive_windows=int(sum(op_mask(s, op).sum() for s in scored)),
        windows=sum(len(s.prob) for s in scored),
        table=table,
        candidate=candidate,
        label=label or f"{CLS_LABEL.get(cls, cls)} × {POST_LABEL[post]}",
    )


def _rank(r: M4Row) -> tuple:
    return CLS_ORDER.index(r.cls), POST_ORDER.index(r.post)


def pick_row(rows: Sequence[M4Row]) -> tuple:
    """6.2: 候補の行のうち、誤検出率が 1.0 回/分以下で検出率が最大（同率なら単純な
    ほう: 分類器の順、次に後処理の順）。無ければ誤検出率が最小（同率なら単純なほう）。"""
    lim = M4_SELECT_FP_PER_MIN
    cand = [r for r in rows if r.candidate]
    if not cand:
        raise SystemExit("選択の対象の行がありません")
    ok = [r for r in cand if r.metrics["false_positives_per_min"] <= lim]
    if ok:
        best = min(ok, key=lambda r: (-r.metrics["detection_rate"], *_rank(r)))
        return best, (
            f"{len(cand)} 行のうち、動作点の誤検出率が {lim} 回/分以下の行で検出率が最大"
            "（同率なら単純なほう: C1 < C2 < C3 < C4 < C5、同じ分類器なら P0 < P1 < P2 < P3 < P4）"
        )
    best = min(
        cand, key=lambda r: (r.metrics["false_positives_per_min"], *_rank(r))
    )
    return best, (
        f"誤検出率が {lim} 回/分以下の行が無いので、{len(cand)} 行のうち誤検出率が最小（同率なら単純なほう）"
    )


# --- 誤検出の中身（7.1） ---


def classify_run(run, swallows, info: Info) -> tuple:
    """誤検出の塊 1 つの主分類と、範囲にある t・c・n の種類。"""
    centers = [w + evaluate.WINDOW_S / 2 for w in run]
    lo_s, hi_s = evaluate.PRE_S, evaluate.POST_S
    kinds = set()
    lo = run[0] - MOTION_PAD_S
    hi = run[-1] + evaluate.WINDOW_S + MOTION_PAD_S
    for lb in MOTION_MARKERS:
        if any(lo <= t <= hi for t in info.markers.get(lb, ())):
            kinds.add(lb)
    inside = [
        t for t in swallows if any(t - lo_s <= c <= t + hi_s for c in centers)
    ]
    if any(any(c > t + hi_s for c in centers) for t in inside):
        return "after", kinds
    if any(any(c < t - lo_s for c in centers) for t in inside):
        return "before", kinds
    if kinds:
        return "motion", kinds
    if info.cond == MEAL:
        return ("meal_conv" if info.conv else "meal_quiet"), kinds
    return "other", kinds


def fp_breakdown(scored: Sequence[Scored], op: Op, info: dict) -> dict:
    """主分類 × 条件の回数、副表、条件ごとの非嚥下の分数。主分類の合計が
    false_positive_runs と一致しなければ止まる。"""
    main = {c: {} for c in FP_CATS}
    sub = {lb: 0 for lb in MOTION_MARKERS}
    minutes: dict = {}
    total = 0
    for s in scored:
        d = s.data
        inf = info[d.name]
        pos = d.feats.t_start_s[op_mask(s, op)].tolist()
        runs = fp_runs(d.swallows, pos)
        want = evaluate.event_metrics(d.swallows, pos, d.duration_s, d.t0_s)
        if len(runs) != want["false_positive_runs"]:
            raise SystemExit(
                f"誤検出の内訳の合計（{len(runs)}）が誤検出の回数（{want['false_positive_runs']}）と一致しません: {d.name}"
            )
        minutes[inf.cond] = minutes.get(inf.cond, 0.0) + want["non_swallow_min"]
        for run in runs:
            cat, kinds = classify_run(run, d.swallows, inf)
            main[cat][inf.cond] = main[cat].get(inf.cond, 0) + 1
            if cat == "motion":
                for lb in kinds:
                    sub[lb] += 1
            total += 1
    if sum(sum(v.values()) for v in main.values()) != total:
        raise SystemExit("誤検出の主分類の合計が塊の数と一致しません")
    return {"main": main, "sub": sub, "minutes": minutes, "total": total}


# --- 本番の実行（ステップ 2） ---


def check_production() -> None:
    """本番の前提（2 節の順序の約束 3）。データを読む前に呼ぶ。"""
    if fixed_eval.M4_EXPECTED_TRAIN is None:
        raise SystemExit(
            "fixed_eval.M4_EXPECTED_TRAIN がまだ None です。本番は学習側の一覧を 0037 の M4-TRAIN と"
            " fixed_eval.py に固定してから（予行は --smoke）"
        )
    text = (fixed_eval.REPO_ROOT / fixed_eval.M4_DECISION).read_text(
        encoding="utf-8"
    )
    if not fixed_eval.m4_lines_match(text):
        raise SystemExit(
            f"{fixed_eval.M4_DECISION} の M4- の行が fixed_eval.py の定数と一致しません"
        )
    if train_eval.git_dirty():
        raise SystemExit(
            "未コミットの変更があります（または git が使えません）。本番は、追跡しているファイルを"
            "コミットしてから実行してください（17 節の Q13）"
        )


@dataclass(eq=False)
class M4Result:
    listing: Listing
    folds: list  # CNN 種 0 の外側の fold
    lr_folds: list
    hn_counts: dict  # (種, 外側の fold の名前) → hard negative の窓の数
    rows: list  # 25 行
    ref_rows: list  # 参考の行
    selected: M4Row
    select_rule: str
    always: dict
    breakdown: dict  # "R0" / "selected" → fp_breakdown
    scored: dict  # 分類器 → [Scored]（図に使う。報告に出さない）
    info: dict
    train_windows: tuple
    sessions: list  # (名前, cond, s の数, 非嚥下の分数)
    n_trainings: int
    timings: dict = field(default_factory=dict)
    marker_note: str | None = None


def _ensemble(per_seed: Sequence[Sequence[Scored]]) -> list:
    out = []
    for group in zip(*per_seed):
        names = {s.data.name for s in group}
        if len(names) != 1:
            raise SystemExit("種の平均でセッションの並びがそろっていません")
        out.append(
            Scored(
                data=group[0].data,
                prob=postprocess.ensemble_mean([s.prob for s in group]),
            )
        )
    return out


def run(
    root: Path, fit=None, score=None, loader=None, trainer=None
) -> M4Result:
    """本番の中身（check_production は main と run の両方で呼ぶ）。loader は
    (root, names) → (CNN の data, LR の data, Info の辞書)、trainer は Trainer
    （どちらもテストで差し替える）。"""
    check_production()
    t_all = time.perf_counter()
    root = Path(root)
    note = fixed_eval.m4_marker_note()
    listing = list_train(root)
    names = listing.train
    planned = plan_trainings(names)
    print(
        f"本番の CNN の学習: {len(planned['all'])} 回（plan #45 の 11 節の列挙）",
        file=sys.stderr,
    )
    t = time.perf_counter()
    d_cnn, d_lr, info = (loader or load_all)(root, names)
    timings: dict = {"extract_s": time.perf_counter() - t}
    trainer = trainer or Trainer(fit, score)
    t = time.perf_counter()
    plain, hard, folds_by = {}, {}, {}
    for seed in SEEDS:
        folds_by[seed], plain[seed] = cnn_cv(trainer, d_cnn, seed, False)
    for seed in SEEDS:
        _, hard[seed] = cnn_cv(trainer, d_cnn, seed, True)
    timings["cnn_s"] = time.perf_counter() - t
    timings["trainings"] = len(trainer.calls)
    t = time.perf_counter()
    lr_folds, lr_scored = train_eval.cross_validate(d_lr)
    timings["lr_s"] = time.perf_counter() - t
    scored = {
        "C1": lr_scored,
        "C2": plain[0],
        "C3": hard[0],
        "C4": _ensemble([plain[s] for s in SEEDS]),
        "C5": _ensemble([hard[s] for s in SEEDS]),
    }
    t = time.perf_counter()
    rows = [make_row(c, p, scored[c]) for c in CLS_ORDER for p in POST_ORDER]
    selected, rule = pick_row(rows)
    refs = [
        make_row(
            "R0",
            "P1",
            plain[0],
            candidate=False,
            fixed=Op("P1", enter=R0_OP[0], exit=R0_OP[1]),
            label="R0 構成 J のまま（CNN 種 0、入り 0.95・出 0.85）",
        )
    ]
    for seed in SEEDS[1:]:
        refs.append(
            make_row(
                "ref", "P1", plain[seed], False, label=f"CNN 種 {seed}（参考）"
            )
        )
    for seed in SEEDS[1:]:
        refs.append(
            make_row(
                "ref",
                "P1",
                hard[seed],
                False,
                label=f"CNN 種 {seed} ＋ hard negative（参考）",
            )
        )
    timings["grid_s"] = time.perf_counter() - t
    breakdown = {
        "R0": fp_breakdown(plain[0], refs[0].op, info),
        "selected": fp_breakdown(scored[selected.cls], selected.op, info),
    }
    for key, b in (("R0", refs[0]), ("selected", selected)):
        if breakdown[key]["total"] != b.metrics["false_positive_runs"]:
            raise SystemExit(
                f"誤検出の内訳の合計が {b.label} の誤検出の回数と一致しません"
            )
    hn_counts = {}
    for seed in SEEDS:
        for f in folds_by[seed]:
            k = train_key(seed, True, f.train_sessions)
            hn_counts[(seed, f.name)] = trainer.hn_counts.get(k)
    always = train_eval.reference(lr_scored, 0.5)["always_positive"]
    per_r0 = refs[0].per_session
    sessions = [
        (n, cond_of(n), per_r0[n]["swallows"], per_r0[n]["non_swallow_min"])
        for n in names
    ]
    timings["total_s"] = time.perf_counter() - t_all
    timings["max_rss_kib"] = max_rss_kib()
    return M4Result(
        listing=listing,
        folds=folds_by[0],
        lr_folds=lr_folds,
        hn_counts=hn_counts,
        rows=rows,
        ref_rows=refs,
        selected=selected,
        select_rule=rule,
        always=always,
        breakdown=breakdown,
        scored=scored,
        info=info,
        train_windows=train_eval.count_windows(d_cnn),
        sessions=sessions,
        n_trainings=len(planned["all"]),
        timings=timings,
        marker_note=note,
    )


def max_rss_kib() -> int:
    """このプロセスの最大の常駐メモリ（KiB。Linux の ru_maxrss）。"""
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


# --- 予行（ステップ 1。8.2・11 節） ---


@dataclass(eq=False)
class SmokeResult:
    listing: Listing
    outer: list  # (fold の名前, 検証側の名前)
    measured_fold: str
    inner: list  # 測った fold の内側 (名前, 検証側の名前)
    windows: tuple  # (valid = False, 全窓)
    plan_now: dict  # plan_trainings の内訳の数
    scenarios: list  # (名前, 収集日, 内訳の数, 見積もりの秒 | None)
    measured: list  # (何の学習, 秒, 学習に使った窓の数)
    sec_per_1000: float
    extract_s: float
    max_rss_kib: int
    marker_note: str | None = None


def _counts(p: dict) -> dict:
    return {k: len(v) for k, v in p.items()}


def scenario_names(names: Sequence[str]) -> list:
    """本番の見積もりの 3 通り（11 節）: 学習用 meal が 3 本になるまで仮の名前を足し、
    meal の収集日を 1・2・3 日にする。今の meal の日だけで作れない形は None。
    戻り値 [(収集日の数, 足した仮の名前 | None)]。"""
    meals = [n for n in names if is_meal(n)]
    have = sorted({n[:8] for n in meals})
    left = max(0, MEAL_TARGET - len(meals))
    out = []
    for days in (1, 2, 3):
        new_days = [d for d in MEAL_DAYS if d not in have]
        need = days - len(have)
        if need < 0 or need > min(left, len(new_days)):
            out.append((days, None))
            continue
        use = have + new_days[:need]
        if not use:
            out.append((days, None))
            continue
        fake = []
        for i in range(left):
            day = new_days[i] if i < need else use[0]
            fake.append(f"{day}-99{i:04d}_self_{MEAL}")
        if len({n[:8] for n in meals + fake}) != days:
            out.append((days, None))
            continue
        out.append((days, fake))
    return out


def smoke(root: Path, fit=None, score=None, loader=None) -> SmokeResult:
    """予行。3 回の学習（外側の最初の fold・種 0 の hard negative なし、その fold
    の内側の最初の fold の hard negative なし、予行専用の重みでの重み付き）と
    特徴量の計算だけを回す。性能の数字は作るが報告に出さない（8.2）。"""
    if fixed_eval.M4_EXPECTED_TRAIN is not None:
        raise SystemExit(
            "fixed_eval.M4_EXPECTED_TRAIN が入っています。予行（--smoke）は一覧の固定の前だけ"
            "（0037 の追記の「M4 のどの性能の数字も見ていない」を守るため）"
        )
    note = fixed_eval.m4_marker_note()
    root = Path(root)
    listing = list_train(root)
    names = listing.train
    t = time.perf_counter()
    if loader is None:
        d_cnn, _, _ = load_all(root, names, lr=False)
    else:
        d_cnn, _, _ = loader(root, names)
    extract_s = time.perf_counter() - t
    groups = outer_groups(d_cnn)
    plan_now = _counts(plan_trainings(names))
    # 本番の見積もりの窓: 仮の meal 1 本は SMOKE_MEAL_S、学習に使う窓の割合は今の全体
    n_win = sum(len(d.labels) for d in d_cnn)
    frac = sum(fit_rows(d) for d in d_cnn) / n_win if n_win else 0.0
    rows_of = {d.name: fit_rows(d) for d in d_cnn}
    fake_rows = int(round(SMOKE_MEAL_S / evaluate.HOP_S * frac))
    # 3 回の学習
    trainer = Trainer(fit, score)
    gname, val = groups[0]
    vn = {d.name for d in val}
    S = [d for d in d_cnn if d.name not in vn]
    inner = train_eval.cv_groups(S)
    if len(inner) < 2:
        raise SystemExit("測る fold の内側の交差検証のグループが 2 つ未満です")
    iname, G = inner[0]
    gn = {d.name for d in G}
    R = [d for d in S if d.name not in gn]
    measured = []
    trainer.plain(S, 0, f"予行の 1 回目（外側の検証側 = {gname}）")
    measured.append(
        (
            "1 回目: 外側の最初の fold の学習側（重みなし、種 0）",
            *trainer.times[-1][1:],
        )
    )
    m2 = trainer.plain(R, 0, f"予行の 2 回目（内側の検証側 = {iname}）")
    measured.append(
        (
            "2 回目: その fold の内側の最初の fold の学習側（重みなし、種 0）",
            *trainer.times[-1][1:],
        )
    )
    w = {d.name: np.ones(len(d.labels)) for d in S}
    for d in G:
        if is_meal(d.name):
            continue
        idx = hn_windows(trainer.scored(m2, d, "予行の内側").prob, d)
        w[d.name][idx] = HN_WEIGHT
    key = train_key(0, True, S)
    trainer._fit(
        key,
        S,
        f"予行の 3 回目（外側の検証側 = {gname}、予行専用の重み）",
        0,
        [w[d.name] for d in S],
    )
    measured.append(
        (
            "3 回目: 1 回目と同じ学習側（予行専用の重み付き、種 0）",
            *trainer.times[-1][1:],
        )
    )
    secs = sum(m[1] for m in measured)
    rows = sum(m[2] for m in measured)
    rate = secs / rows * 1000.0 if rows else float("nan")
    scenarios = []
    for days, fake in scenario_names(names):
        if fake is None:
            scenarios.append((days, None, None))
            continue
        p = plan_trainings(list(names) + fake)
        r = dict(rows_of, **{n: fake_rows for n in fake})
        est = sum(sum(r[n] for n in k[2]) for k in p["all"]) / 1000.0 * rate
        scenarios.append((days, _counts(p), est))
    return SmokeResult(
        listing=listing,
        outer=[(n, [d.name for d in v]) for n, v in groups],
        measured_fold=gname,
        inner=[(n, [d.name for d in v]) for n, v in inner],
        windows=train_eval.count_windows(d_cnn),
        plan_now=plan_now,
        scenarios=scenarios,
        measured=measured,
        sec_per_1000=rate,
        extract_s=extract_s,
        max_rss_kib=max_rss_kib(),
        marker_note=note,
    )


def _plan_line(c: dict) -> str:
    return (
        f"{c['all']} 回（重みなしの外側 {c['outer']}、内側（重みを決める学習）{c['inner']}、"
        f"重み付きの外側 {c['hard']}。重複を除いた数）"
    )


def format_smoke(r: SmokeResult) -> str:
    """予行の報告（8.2 の項目だけ）。"""
    L = r.listing
    out = ["# M4 #45 予行（ステップ 1。性能の数字は出さない）", ""]
    if r.marker_note:
        out += [r.marker_note, ""]
    out += [
        "## 実行の条件",
        "",
        "- 実行: `--smoke`（`fixed_eval.M4_EXPECTED_TRAIN` が None の間だけ。plan #45 の 8.2・11 節）",
        f"- コミット: {train_eval.git_commit()}",
        f"- CPU: {_cpu()}、コア数（os.cpu_count）{os.cpu_count()}",
        f"- `M4_TRAIN_CUTOFF` {fixed_eval.M4_TRAIN_CUTOFF}、`M4_EVAL_FROM` {fixed_eval.M4_EVAL_FROM}"
        "（それより後の名前は meta.json を含めて開いていない）",
        "",
        f"## 学習側のセッション（{len(L.train)}）",
        "",
        *[f"- {n}" for n in L.train],
        "",
        "## 使わないセッション（名前だけ）",
        "",
        f"- 締めより後・評価側の窓より前: {', '.join(L.after_cutoff) or 'なし'}",
        f"- `M4_EVAL_FROM` 以降: {', '.join(L.eval_window) or 'なし'}",
        f"- 採らないもの（`fw` が `pc-throat` でない・`self` でない）: {len(L.excluded)} 本",
        f"- 飛ばしたもの: {', '.join(L.skipped) or 'なし'}",
        "",
        "## fold の分け方",
        "",
        f"- 外側: {len(r.outer)} fold",
        *[f"  - 検証側 = {n}: {len(v)} 本" for n, v in r.outer],
        f"- 測った fold（外側の名前順の最初）: {r.measured_fold}。その内側: {len(r.inner)} fold",
        *[f"  - 検証側 = {n}: {len(v)} 本" for n, v in r.inner],
        "",
        "## 窓の数",
        "",
        f"- 全体 {r.windows[1]}、`valid = False` {r.windows[0]}",
        "",
        "## 本番の CNN の学習の回数（plan_trainings。鍵 = 種・重みの有無・学習のセッションの集合）",
        "",
        f"- 予行の時点の学習側: {_plan_line(r.plan_now)}",
    ]
    for days, c, est in r.scenarios:
        if c is None:
            out.append(
                f"- 本番の見込み（学習用 meal が {days} 収集日）: 今の meal の日からは作れない形"
            )
        else:
            out.append(
                f"- 本番の見込み（学習用 meal が {days} 収集日）: {_plan_line(c)}"
            )
    out += [
        "",
        "## 時間の実測（予行の 3 回の学習。再現の対象外）",
        "",
        "| 学習 | 秒 | 学習に使った窓 | 窓 1,000 あたりの秒 |",
        "|---|---|---|---|",
    ]
    for what, s, n in r.measured:
        out.append(
            f"| {what} | {s:.1f} | {n} | {s / n * 1000.0 if n else float('nan'):.2f} |"
        )
    out += [
        "",
        f"- 3 回の合計の窓 1,000 あたり: {r.sec_per_1000:.2f} 秒",
        f"- 特徴量の計算（{CNN_SET}、学習側の全セッション）: {r.extract_s:.1f} 秒",
        f"- 最大の常駐メモリ: {r.max_rss_kib / 1024 / 1024:.2f} GiB",
        "",
        "## 本番の所要時間の見積もり（推測）",
        "",
        f"- 前提: 残りの学習用 meal は 1 本 {SMOKE_MEAL_S // 60} 分（推測）。学習に使う窓の割合は予行の時点の全体と同じと置く。"
        "学習の時間は学習に使う窓の数に比例すると置き、3 回の合計の窓 1,000 あたりの秒で数える",
        "- ロジスティック回帰・後処理の格子の採点・特徴量の計算（2 組）は含まない",
    ]
    for days, c, est in r.scenarios:
        if est is None:
            continue
        over = (
            "予算（3 時間）を超える" if est > BUDGET_S else "予算（3 時間）の内"
        )
        out.append(
            f"- 学習用 meal が {days} 収集日: CNN の学習 {c['all']} 回で約 {est / 3600:.2f} 時間（{over}）"
        )
    out += ["", "- 性能の数字は出していない（plan #45 の 8.2）"]
    return "\n".join(out)


# --- 最終モデル（ステップ 3。9.1 節） ---

CONFIG_KEYS = (
    "set",
    "classifier",
    "seeds",
    "hard_negative",
    "post",
    "threshold",
    "enter",
    "exit",
    "cap_s",
    "refractory_s",
)
CNN_SHAPES = {
    "conv1.weight": (8, 1, 3, 3),
    "conv1.bias": (8,),
    "conv2.weight": (16, 8, 3, 3),
    "conv2.bias": (16,),
    "conv3.weight": (32, 16, 3, 3),
    "conv3.bias": (32,),
    "fc.weight": (1, 32),
    "fc.bias": (1,),
}
N_MEL = features_np.N_MEL_CNN
N_LR = 4 * features_np.N_MFCC  # 52
MODEL_FORMAT = 1


def _f2(v) -> str:
    return f"{v:.2f}"


def config_of(row: M4Row) -> str:
    """選んだ行の M4-CONFIG の値の部分（9 節の形）。"""
    if row.cls not in CLS_ORDER:
        raise ValueError(f"候補の行ではありません: {row.cls}")
    op = row.op
    lr = row.cls == "C1"
    return " ".join(
        f"{k}={v}"
        for k, v in zip(
            CONFIG_KEYS,
            (
                LR_SET if lr else CNN_SET,
                "lr" if lr else "cnn",
                "-" if lr else ("0" if row.cls in ("C2", "C3") else "0,1,2"),
                f"w{HN_WEIGHT:.1f}" if row.cls in ("C3", "C5") else "none",
                "none" if op.post == "P0" else "hyst",
                _f2(op.threshold) if op.post == "P0" else "-",
                "-" if op.post == "P0" else _f2(op.enter),
                "-" if op.post == "P0" else _f2(op.exit),
                "none" if op.cap_s is None else f"{op.cap_s:.1f}",
                "none" if op.refractory_s is None else f"{op.refractory_s:.1f}",
            ),
        )
    )


def parse_config(text: str) -> dict:
    """M4-CONFIG の値を読む。形が違えば ValueError。"""
    parts = text.split(" ")
    kv = [p.split("=", 1) for p in parts]
    if [k[0] for k in kv] != list(CONFIG_KEYS) or any(len(k) != 2 for k in kv):
        raise ValueError(f"M4-CONFIG の形が違います: {text!r}")
    c = dict(kv)
    lr = c["classifier"] == "lr"
    ok = (
        (c["set"], c["classifier"]) in ((LR_SET, "lr"), (CNN_SET, "cnn"))
        and c["seeds"] in (("-",) if lr else ("0", "0,1,2"))
        and c["hard_negative"]
        in (("none",) if lr else ("none", f"w{HN_WEIGHT:.1f}"))
        and c["post"] in ("none", "hyst")
        and c["cap_s"]
        in ("none", *(f"{v:.1f}" for v in postprocess.CAP_GRID_S))
        and c["refractory_s"] in ("none", f"{postprocess.REFRACTORY_S:.1f}")
    )
    if not ok:
        raise ValueError(f"M4-CONFIG の値が違います: {text!r}")
    out = {
        "set": c["set"],
        "kind": c["classifier"],
        "seeds": () if lr else tuple(int(s) for s in c["seeds"].split(",")),
        "hard_negative": c["hard_negative"] != "none",
        "post": c["post"],
        "threshold": None,
        "enter": None,
        "exit": None,
        "cap_s": None if c["cap_s"] == "none" else float(c["cap_s"]),
        "refractory_s": None
        if c["refractory_s"] == "none"
        else float(c["refractory_s"]),
    }
    try:
        if c["post"] == "none":
            if (
                c["enter"] != "-"
                or c["exit"] != "-"
                or out["cap_s"]
                or out["refractory_s"]
            ):
                raise ValueError
            out["threshold"] = float(c["threshold"])
        else:
            if c["threshold"] != "-":
                raise ValueError
            out["enter"], out["exit"] = float(c["enter"]), float(c["exit"])
    except ValueError:
        raise ValueError(
            f"M4-CONFIG の後処理の値が違います: {text!r}"
        ) from None
    return out


def config_mask(cfg: dict, prob, valid, t_start_s) -> np.ndarray:
    """0038 の後処理（P0 は threshold、ほかは post_mask）。"""
    if cfg["post"] == "none":
        return postprocess.threshold(prob, valid, cfg["threshold"])
    return postprocess.post_mask(
        prob,
        valid,
        t_start_s,
        cfg["enter"],
        cfg["exit"],
        cfg["cap_s"],
        cfg["refractory_s"],
    )


def _expected_keys(cfg: dict) -> dict:
    """鍵 → (形, 型)。"""
    keys = {"config": ((), "U"), "format": ((), "i")}
    if cfg["kind"] == "lr":
        keys.update(
            {
                "std_mean": ((N_LR,), "f8"),
                "std_std": ((N_LR,), "f8"),
                "coef": ((N_LR,), "f8"),
                "intercept": ((), "f8"),
            }
        )
        return keys
    for s in cfg["seeds"]:
        for k, shp in CNN_SHAPES.items():
            keys[f"s{s}/{k}"] = (shp, "f4")
        keys[f"s{s}/mean"] = ((N_MEL,), "f8")
        keys[f"s{s}/std"] = ((N_MEL,), "f8")
    return keys


def model_arrays(config: str, models) -> dict:
    """保存する配列。models は CNN なら 種 → compare.CnnModel、LR なら
    train_eval.Model。"""
    cfg = parse_config(config)
    a = {"config": np.array(config), "format": np.array(MODEL_FORMAT)}
    if cfg["kind"] == "lr":
        m = models
        col = list(m.clf.classes_).index(POSITIVE)
        sign = 1.0 if col == 1 else -1.0  # 陽性の向きに直す（9.1）
        a["std_mean"] = np.asarray(m.standardizer.mean, np.float64)
        a["std_std"] = np.asarray(m.standardizer.std, np.float64)
        a["coef"] = sign * np.asarray(m.clf.coef_[0], np.float64)
        a["intercept"] = np.array(sign * float(m.clf.intercept_[0]), np.float64)
        return a
    for s in cfg["seeds"]:
        m = models[s]
        for k in CNN_SHAPES:
            a[f"s{s}/{k}"] = np.asarray(m.params[k], np.float32)
        a[f"s{s}/mean"] = np.asarray(m.mean, np.float64)
        a[f"s{s}/std"] = np.asarray(m.std, np.float64)
    return a


def save_model(path: Path, config: str, models) -> None:
    a = model_arrays(config, models)
    _check_arrays(a)
    with open(path, "wb") as f:
        np.savez(f, **a)


def _check_arrays(a: dict) -> dict:
    cfg = parse_config(str(a["config"])) if "config" in a else None
    if cfg is None:
        raise SystemExit("重みのファイルに config がありません")
    want = _expected_keys(cfg)
    if set(a) != set(want):
        raise SystemExit(
            f"重みのファイルの鍵が違います（足りない: {sorted(set(want) - set(a))}、"
            f"余分: {sorted(set(a) - set(want))}）"
        )
    for k, (shp, kind) in want.items():
        v = a[k]
        if v.shape != shp:
            raise SystemExit(
                f"重みのファイルの {k} の形が違います: {v.shape}（{shp}）"
            )
        if kind == "U":
            ok = v.dtype.kind == "U"
        elif kind == "i":
            ok = v.dtype.kind == "i" and int(v) == MODEL_FORMAT
        else:
            ok = v.dtype == np.dtype(kind)
        if not ok:
            raise SystemExit(f"重みのファイルの {k} の型が違います: {v.dtype}")
    return cfg


def load_model(path: Path) -> dict:
    """重みのファイルを読む（allow_pickle を使わない）。鍵の過不足・形・型が違えば
    止まる。戻り値 {"config", "cfg", "arrays"}。numpy だけで読む。"""
    with np.load(path, allow_pickle=False) as z:
        a = {k: z[k] for k in z.files}
    cfg = _check_arrays(a)
    return {"config": str(a["config"]), "cfg": cfg, "arrays": a}


def model_prob(model: dict, X: np.ndarray) -> np.ndarray:
    """窓ごとの確率。CNN は X (n, 98, 40) の対数メル、LR は X (n, 52) の a52。
    種が複数なら確率の算術平均（5.2）。numpy と features_np だけで計算する。"""
    cfg, a = model["cfg"], model["arrays"]
    X = np.asarray(X)
    if cfg["kind"] == "lr":
        # 正規化の後に float32 に丸めるのは features.Standardizer.transform と同じ
        Z = ((X.astype(np.float64) - a["std_mean"]) / a["std_std"]).astype(
            np.float32
        )
        z = Z.astype(np.float64) @ a["coef"] + float(a["intercept"])
        return 1.0 / (1.0 + np.exp(-z))
    probs = []
    for s in cfg["seeds"]:
        params = {k: a[f"s{s}/{k}"] for k in CNN_SHAPES}
        mean, std = a[f"s{s}/mean"], a[f"s{s}/std"]
        p = np.zeros(len(X))
        B = 256  # cnn_score と同じ 256 窓ずつのまとめ（結果は変わらない）
        for k in range(0, len(X), B):
            Z = ((X[k : k + B] - mean) / std).astype(np.float32)
            p[k : k + B] = features_np.cnn_forward(params, Z)
        probs.append(p)
    return probs[0] if len(probs) == 1 else postprocess.ensemble_mean(probs)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_final() -> None:
    """最終モデルの前提（2 節の順序の約束 5）。データを読む前に呼ぶ。"""
    if fixed_eval.M4_CONFIG is None:
        raise SystemExit(
            "fixed_eval.M4_CONFIG がまだ None です（0038 を書いてから）"
        )
    if not fixed_eval.m4_config_file_matches():
        raise SystemExit(
            f"{fixed_eval.M4_CONFIG_DECISION} の M4-CONFIG の行が fixed_eval.M4_CONFIG と一致しません"
            "（またはファイルがありません）"
        )
    check_production()


def final_model(
    root: Path, out: Path, fit=None, score=None, loader=None
) -> list:
    """0038 の構成で学習側の全セッションから最終モデルを作り、保存して読み戻し、
    一致を確かめる。戻り値は報告の行。外れたらファイルを消して止まる。"""
    check_final()
    out = Path(out)
    if out.exists():
        raise SystemExit(f"出力先が既にあります（上書きしない）: {out}")
    config = fixed_eval.M4_CONFIG
    cfg = parse_config(config)
    root = Path(root)
    listing = list_train(root)
    d_cnn, d_lr, _ = (loader or load_all)(root, listing.train)
    trainer = Trainer(fit, score)
    if cfg["kind"] == "lr":
        model = train_eval.fit_model(d_lr, "最終モデル（学習側の全セッション）")
        data = d_lr
        want = [train_eval.score(model, d).prob for d in d_lr]
        models = model
    else:
        data = d_cnn
        models = {}
        for s in cfg["seeds"]:
            where = f"最終モデル（学習側の全セッション、種 {s}）"
            models[s] = (
                trainer.hard if cfg["hard_negative"] else trainer.plain
            )(d_cnn, s, where)
        per_seed = [
            [trainer.score(models[s], d).prob for d in d_cnn]
            for s in cfg["seeds"]
        ]
        want = [
            ps[0] if len(ps) == 1 else postprocess.ensemble_mean(ps)
            for ps in zip(*per_seed)
        ]
    out.parent.mkdir(parents=True, exist_ok=True)
    save_model(out, config, models)
    try:
        loaded = load_model(out)
        if loaded["config"] != config:
            raise SystemExit("読み戻した config が M4_CONFIG と違います")
        diff, same = 0.0, True
        for d, p in zip(data, want):
            q = model_prob(loaded, d.feats.X)
            if len(q):
                diff = max(diff, float(np.max(np.abs(q - p))))
            f = d.feats
            same &= bool(
                np.array_equal(
                    config_mask(cfg, p, f.valid, f.t_start_s),
                    config_mask(cfg, q, f.valid, f.t_start_s),
                )
            )
        if diff > 1e-6 or not same:
            raise SystemExit(
                f"保存前後の確率の差の最大 {diff:.3g}（1e-6 以下が要る）、後処理の一致 {same}"
            )
    except BaseException:
        out.unlink(missing_ok=True)
        raise
    digest = sha256_of(out)
    return [
        "# M4 #45 最終モデル（ステップ 3）",
        "",
        f"- コミット: {train_eval.git_commit()}",
        f"- 構成: `M4-CONFIG: {config}`",
        f"- 学習側のセッション: {len(listing.train)} 本（0037 の M4-TRAIN）",
        *(
            [
                f"- hard negative の窓の数（種ごと）: {', '.join(str(trainer.hn_counts[train_key(s, True, d_cnn)]) for s in cfg['seeds'])}"
            ]
            if cfg["kind"] == "cnn" and cfg["hard_negative"]
            else []
        ),
        f"- ファイル: `{out}`（{out.stat().st_size} バイト）",
        f"- 保存前後の確率の差の最大: {diff:.3g}（1e-6 以下）、後処理の出力: 完全に一致",
        f"- `M4-MODEL: path={out.as_posix()} sha256={digest}`",
    ]


# --- 本番の報告（8.1） ---


def _cpu() -> str:
    import compare

    return compare._cpu()


def _rate(v) -> str:
    return "—" if v is None else f"{v:.3f}"


def _per_min(v) -> str:
    return "—" if v is None else f"{v:.2f}"


def op_text(op: Op) -> str:
    if op.post == "P0":
        return f"閾値 {op.threshold:.2f}"
    s = f"入り {op.enter:.2f}・出 {op.exit:.2f}"
    if op.cap_s is not None:
        s += f"・上限 {op.cap_s:.1f} 秒"
    if op.refractory_s is not None:
        s += f"・不応期 {op.refractory_s:.1f} 秒"
    return s


def _cells(m: dict | None) -> str:
    if m is None:
        return "— | —"
    return (
        f"{_rate(m['detection_rate'])}（{m['detected']}/{m['swallows']}） | "
        f"{_per_min(m['false_positives_per_min'])}（{m['false_positive_runs']} 回 / {m['non_swallow_min']:.2f} 分）"
    )


def _meets(m: dict) -> str:
    det, fp = m["detection_rate"], m["false_positives_per_min"]
    a = det is not None and det >= M4_PASS_DETECTION_RATE
    b = fp is not None and fp <= M4_PASS_FP_PER_MIN
    return (
        f"{'満たす' if a else '満たさない'} / {'満たす' if b else '満たさない'}"
    )


def _row_line(r: M4Row, mark: str = "") -> str:
    m = r.metrics
    return (
        f"| {r.label}{mark} | {op_text(r.op)} | {_cells(m)} | "
        f"{m['detected']} / {m['swallows'] - m['detected']} / {m['false_positive_runs']} | "
        f"{_rate(r.positive_ratio)}（{r.positive_windows}/{r.windows}） | "
        f"{_cells(r.meal)} | {_cells(r.conds)} | {_meets(m)} |"
    )


def format_breakdown(b: dict, title: str) -> list:
    conds = sorted(b["minutes"])
    total_min = sum(b["minutes"].values())
    out = [
        f"### {title}（{b['total']} 回）",
        "",
        "| 主分類 | "
        + " | ".join(conds)
        + " | 計 | 回/分（計 ÷ 非嚥下の分数の計） |",
        "|---|" + "---|" * (len(conds) + 2),
    ]
    for c in FP_CATS:
        cells = [str(b["main"][c].get(k, 0)) for k in conds]
        n = sum(b["main"][c].values())
        out.append(
            f"| {FP_CAT_LABEL[c]} | "
            + " | ".join(cells)
            + f" | {n} | {_per_min(n / total_min if total_min > 0 else None)} |"
        )
    out.append(
        "| 非嚥下（分） | "
        + " | ".join(f"{b['minutes'][k]:.2f}" for k in conds)
        + f" | {total_min:.2f} | |"
    )
    out.append(
        "| 回/分（計） | "
        + " | ".join(
            _per_min(
                sum(b["main"][c].get(k, 0) for c in FP_CATS) / b["minutes"][k]
                if b["minutes"][k] > 0
                else None
            )
            for k in conds
        )
        + " | |"
    )
    out += [
        "",
        "副表（重複あり。「動作に付くもの」の塊で、範囲にあるマーカーの種類ごとの塊の数）: "
        + "、".join(f"`{lb}` {b['sub'][lb]}" for lb in MOTION_MARKERS),
        "",
    ]
    return out


def format_report(r: M4Result) -> str:
    """本番の報告（8.1）。時間の実測は末尾の節だけ（再現の対象外）。"""
    import compare

    L = r.listing
    out = ["# M4 #45 誤検出を減らす候補の比較（学習側の交差検証だけ）", ""]
    if r.marker_note:
        out += [r.marker_note, ""]
    out += [
        "## 実行の条件",
        "",
        "- 実行: 本番（学習側だけ。評価側は開いていない・採点していない）",
        f"- コミット: {train_eval.git_commit()}",
        *compare._versions(),
        f"- CPU: {_cpu()}、コア数（os.cpu_count）{os.cpu_count()}",
        f"- torch のスレッド数: {compare.CNN_THREADS}、種: {', '.join(map(str, SEEDS))}",
        f"- 閾値の格子 {len(train_eval.THRESHOLDS)} 点、ヒステリシスの格子 {len(postprocess.HYST_GRID)} 点、"
        f"塊の上限 {', '.join(f'{c:.1f}' for c in postprocess.CAP_GRID_S)} 秒、不応期 {postprocess.REFRACTORY_S} 秒",
        f"- hard negative: 重み {HN_WEIGHT}、決める動作点 入り {HN_OP[0]}・出 {HN_OP[1]}（固定）、条件別のセッションだけ、入れ子",
        f"- 選び方の誤検出率の上限: {M4_SELECT_FP_PER_MIN} 回/分（`M4_SELECT_FP_PER_MIN`）",
        f"- 0037 の `M4-` の行と `fixed_eval.py` の定数: 一致（{fixed_eval.M4_DECISION}）",
        f"- CNN の学習の回数: {r.n_trainings}（plan_trainings）",
        "",
        f"## 学習側のセッション（{len(L.train)}）",
        "",
        "| セッション | cond | `s` の数 | 非嚥下（分） |",
        "|---|---|---|---|",
        *[f"| {n} | {c} | {k} | {m:.2f} |" for n, c, k, m in r.sessions],
        "",
        "## 使わないセッション（名前だけ）",
        "",
        f"- 締めより後・評価側の窓より前: {', '.join(L.after_cutoff) or 'なし'}",
        f"- `M4_EVAL_FROM` 以降（開いていない）: {', '.join(L.eval_window) or 'なし'}",
        f"- 採らないもの（`fw` が `pc-throat` でない・`self` でない）: {len(L.excluded)} 本",
        "",
        "## 交差検証の fold",
        "",
        "| 検証側 | 正規化のセッション（CNN 種 0） | hard negative の窓（種 0・1・2） |",
        "|---|---|---|",
    ]
    for f in r.folds:
        ss = set(f.standardizer_sessions)
        ok = ss <= set(f.train_sessions) and not (ss & set(f.val_sessions))
        hn = "・".join(str(r.hn_counts.get((s, f.name))) for s in SEEDS)
        out.append(
            f"| {f.name}: {', '.join(f.val_sessions)} | {len(ss)} 本（{'学習側の fold だけ' if ok else '要確認: 学習側の fold の外を含む'}） | {hn} |"
        )
    out += [
        "",
        f"- 無効な窓（`valid = False`）: {r.train_windows[0]} / {r.train_windows[1]}",
        "",
        "## 候補の表（25 行。学習側の交差検証の合算）",
        "",
        "| 行 | 動作点 | 検出率 | 誤検出率（回/分） | TP / FN / FP | 陽性窓の割合 | meal だけ 検出率 | meal だけ 誤検出率 "
        "| 条件別だけ 検出率 | 条件別だけ 誤検出率 | 合格線 85% / 1.0 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in r.rows:
        out.append(_row_line(row, " **（選択）**" if row is r.selected else ""))
    out += [
        "",
        "## 参考の行（候補ではない）",
        "",
        "| 行 | 動作点 | 検出率 | 誤検出率（回/分） | TP / FN / FP | 陽性窓の割合 | meal だけ 検出率 | meal だけ 誤検出率 "
        "| 条件別だけ 検出率 | 条件別だけ 誤検出率 | 合格線 85% / 1.0 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
        *[_row_line(row) for row in r.ref_rows],
        "",
    ]
    a = r.always
    out += [
        f"- 常時陽性の場合（全窓を陽性にした場合。学習側 CV、全行で同じ）: 検出率 {_rate(a['detection_rate'])}"
        f"（{a['detected']}/{a['swallows']}）、誤検出 {a['false_positive_runs']} 回、"
        f"誤検出率 {_per_min(a['false_positives_per_min'])} 回/分",
        "- TP / FN / FP はイベント単位（学習側 CV の合算。TN は定義できない）。評価側の混同行列と評価に使ったセッションの一覧は #49。",
        "",
        "## 選んだ行",
        "",
        f"- **{r.selected.label}、{op_text(r.selected.op)}**",
        f"- 規則: {r.select_rule}。学習側の交差検証の 24 本の合算だけで選んだ（`meal` だけ・条件別だけの数字は選択に使っていない）",
        f"- 0038 の機械が読む行: `M4-CONFIG: {config_of(r.selected)}`",
        f"- 分類器: {'ロジスティック回帰（装置は numpy のまま）' if r.selected.cls == 'C1' else 'CNN'}",
        "- 注記: 25 行と各行の格子を同じ交差検証の数字で選ぶので、選んだ行の学習側 CV の数字は楽観側に出る。選んだ構成の性能は #49 の評価側で読む。",
        "",
        "## 誤検出の中身（R0 と選んだ行。主分類は排他、上から順に最初に当たった 1 つ）",
        "",
        *format_breakdown(r.breakdown["R0"], "R0 構成 J のまま"),
        *format_breakdown(
            r.breakdown["selected"], f"選んだ行: {r.selected.label}"
        ),
        "- `meal` の咀嚼と会話（同席者の声を含む）はマーカーで分けられないので分けない。",
        "- M3 の評価側の内訳（23 回: water の非嚥下区間 9、saliva 5、neck 5、talk 4、cough 0、quiet 0）は評価側、ここは学習側 CV で、そのまま比べない。",
        "",
        "## 付録: 行ごとの格子",
        "",
    ]
    for row in r.rows:
        out += [f"### {row.label}", "", f"- 決め方: {row.rule}", ""]
        out += [
            "| 動作点 | 検出率 | 検出/嚥下 | 誤検出（回） | 誤検出率（回/分） | 採用 |",
            "|---|---|---|---|---|---|",
        ]
        for op, m in row.table:
            out.append(
                f"| {op_text(op)} | {_rate(m['detection_rate'])} | {m['detected']}/{m['swallows']} "
                f"| {m['false_positive_runs']} | {_per_min(m['false_positives_per_min'])} | {'○' if op == row.op else ''} |"
            )
        out.append("")
    t = r.timings
    out += [
        TIMING_HEADING,
        "",
        f"- 全体: {t.get('total_s', float('nan')):.1f} 秒、特徴量の計算: {t.get('extract_s', float('nan')):.1f} 秒",
        f"- CNN の学習と採点: {t.get('cnn_s', float('nan')):.1f} 秒（学習 {t.get('trainings', 0)} 回）、"
        f"ロジスティック回帰: {t.get('lr_s', float('nan')):.1f} 秒、格子の採点: {t.get('grid_s', float('nan')):.1f} 秒",
        f"- 最大の常駐メモリ: {t.get('max_rss_kib', 0) / 1024 / 1024:.2f} GiB",
    ]
    return "\n".join(out)


TIMING_HEADING = "## 時間の実測（再現の条件の対象外）"


def main(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "root", type=Path, help="セッションフォルダの親（data/raw）"
    )
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--smoke", action="store_true", help="予行（ステップ 1）")
    g.add_argument(
        "--final-model", type=Path, help="最終モデルの出力先（ステップ 3）"
    )
    ap.add_argument(
        "--figures",
        type=Path,
        help="図の PNG の出力先（docs/log/ の下だけ。本番だけ）",
    )
    a = ap.parse_args(argv)
    if a.figures is not None and (a.smoke or a.final_model is not None):
        ap.error("--figures は本番だけで使う（予行は図を描かない）")
    if a.smoke:
        print(format_smoke(smoke(a.root)))
        return
    if a.final_model is not None:
        print("\n".join(final_model(a.root, a.final_model)))
        return
    check_production()  # 計算の前に止める
    if a.figures is not None:
        import visualize

        visualize.check_out_dir(a.figures)
    r = run(a.root)
    print(format_report(r))
    if a.figures is not None:
        import visualize

        paths = visualize.draw_m4(a.root, r, a.figures)
        print(f"\n図: {len(paths)} 枚（{a.figures}）", file=sys.stderr)


if __name__ == "__main__":
    main()
