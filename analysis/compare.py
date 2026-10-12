"""咽喉マイクの特徴量の組と後処理を、学習側の交差検証だけで比べる（Issue #43）。

式・格子・選び方は docs/decisions/0033（実データの前に固定）。手順は
docs/decisions/0014（ラベル、正規化、ロジスティック回帰、閾値の格子と規則、
止まる条件）と 0032（評価側 = 最後の 1 収集日）のまま。

使い方: python analysis/compare.py <root> --eval-min <N> [--figures <docs/log/...>]

  学習側（収集日 1）のセッションだけを読む。評価側は split_sessions が返す名前
  だけを扱い、throat.wav・throat_chunks.csv・events.csv を開かない。評価側を採点
  する引数（--final に当たるもの）は無い。評価側の数字は #40 で、ここで選んだ組
  について 1 回だけ出す。

組（features_v2.FEATURE_SETS ＋ CNN）× 後処理（なし / ヒステリシス）の 10 行を
表にし、ロジスティック回帰の 8 行だけから 1 行を選ぶ（CNN の 2 行は参考。
0033、D1 = (a)）。報告は標準出力に Markdown。ファイルは書かない（--figures の
PNG を除く）。波形、特徴量の値、個々の窓の確率は出さない。
"""

from __future__ import annotations
import argparse, importlib.metadata, os, platform, sys, time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np

import evaluate, features, features_v2, postprocess, split, train_eval
from train_eval import NEGATIVE, POSITIVE, UNUSED, Fold, Scored, SessionData

# --- 定数（docs/decisions/0033。学習側の数字を見る前に固定） ---
LR_SETS = features_v2.FEATURE_SETS
CNN_SET = features_v2.CNN_SET
# 単純な順（同率のとき前のほう）
SIMPLE_ORDER = (*LR_SETS, CNN_SET)
POSTS = ("none", "hyst")
POST_LABEL = {"none": "なし", "hyst": "ヒステリシス"}
CNN_SEED = 0  # 選ぶのはこの種だけ
CNN_REF_SEEDS = (1, 2)  # 揺れの参考
CNN_LR = 1e-3
CNN_BATCH = 64
CNN_EPOCHS = 20
CNN_THREADS = 4
CNN_SCORE_BATCH = 256  # numpy の順伝播のまとめ（結果は変わらない）
TIMING_SAMPLES = 200  # 時間の実測に使う窓の数（学習側の最初のセッション）


@dataclass(frozen=True, eq=False)
class CnnModel:
    mean: np.ndarray  # (40,) メルの帯域ごと
    std: np.ndarray
    pos_weight: float  # 陰性数 ÷ 陽性数
    params: dict  # numpy の重み（PyTorch の state_dict と同じ名前）
    sessions: tuple  # 正規化と学習に使ったセッション名
    n_fit_rows: int


@dataclass(frozen=True, eq=False)
class Row:
    set: str
    post: str
    dims: int
    classifier: str  # "LR" / "CNN"
    candidate: bool  # #40 の選択の対象（ロジスティック回帰だけ）
    op: tuple  # なし: (閾値,)、ヒステリシス: (enter, exit)
    rule: str
    metrics: dict  # 動作点の sum_metrics
    positive_windows: int
    windows: int
    table: list  # 候補ごとの行
    default_row: dict | None = None  # ヒステリシスの初期値（0.7 / 0.4）

    @property
    def positive_ratio(self):
        return self.positive_windows / self.windows if self.windows else None


@dataclass(eq=False)
class CompareResult:
    eval_min: int
    train: list
    eval: list
    folds: dict  # 組 → [Fold]
    rows: list  # 10 行
    selected: Row
    select_rule: str
    always: dict  # 常時陽性の場合（学習側 CV。全行で同じ）
    ref_rows: dict  # 種 → [Row]（CNN の参考）
    scored: dict  # 組 → [Scored]（図に使う。報告に出さない）
    train_windows: tuple
    timings: dict = field(default_factory=dict)


# --- 読み込み ---


def _extract_fn(feature_set: str):
    if feature_set == CNN_SET:

        def ex(p):
            t, M, valid = features_v2.extract_mfe(p)
            return features.WindowFeatures(
                session=Path(p).name, t_start_s=t, X=M, valid=valid
            )

        return ex
    return lambda p: features_v2.extract(p, feature_set)


def load_train(root: Path, names: Sequence[str], feature_set: str) -> list:
    ex = _extract_fn(feature_set)
    return [
        train_eval.load_session(root / n, ex, evaluate.throat_session_span)
        for n in names
    ]


# --- CNN（学習は PyTorch、採点は numpy の順伝播） ---


def _make_net(torch):
    nn = torch.nn

    class Net(nn.Module):
        def __init__(self):
            super().__init__()
            (a, b), (c, d), (e, f) = features_v2.CNN_CONV
            self.conv1 = nn.Conv2d(a, b, 3, padding=1)
            self.conv2 = nn.Conv2d(c, d, 3, padding=1)
            self.conv3 = nn.Conv2d(e, f, 3, padding=1)
            self.fc = nn.Linear(f, 1)

        def forward(self, x):  # x (B, 98, 40) → ロジット (B,)
            x = x[:, None]
            x = nn.functional.max_pool2d(torch.relu(self.conv1(x)), 2)
            x = nn.functional.max_pool2d(torch.relu(self.conv2(x)), 2)
            x = torch.relu(self.conv3(x)).mean(dim=(2, 3))
            return self.fc(x)[:, 0]

    return Net()


def _torch_setup(seed: int):
    import torch

    torch.manual_seed(seed)
    torch.use_deterministic_algorithms(True)
    torch.set_num_threads(CNN_THREADS)
    return torch


def cnn_fit(
    data: Sequence[SessionData], where: str, seed: int = CNN_SEED
) -> CnnModel:
    """data のセッションだけで、メルの帯域ごとの正規化・陽性の重み・重みを作る。
    正規化は valid な窓の全部、学習は valid かつ陽性か陰性の窓だけ。"""
    names = ", ".join(d.name for d in data)
    used = [d for d in data if d.feats.valid.any()]
    if not used:
        raise SystemExit(
            f"{where}: 有効な窓が1つもありません。学習に使うセッション: {names}"
        )
    allv = np.concatenate([d.feats.X[d.feats.valid] for d in used]).astype(
        np.float64
    )
    mean = allv.mean(axis=(0, 1))
    std = allv.std(axis=(0, 1))
    std[std == 0] = 1.0
    use = [d.feats.valid & (d.labels != UNUSED) for d in data]
    X = np.concatenate([d.feats.X[u] for d, u in zip(data, use)])
    y = np.concatenate([d.labels[u] for d, u in zip(data, use)])
    n_pos, n_neg = int((y == POSITIVE).sum()), int((y == NEGATIVE).sum())
    if n_pos == 0 or n_neg == 0:
        raise SystemExit(
            f"{where}: 学習に使う窓に陽性と陰性の両方がそろっていません（陽性 {n_pos}、陰性 {n_neg}）。"
            f"学習に使うセッション: {names}"
        )
    pos_weight = n_neg / n_pos
    torch = _torch_setup(seed)
    net = _make_net(torch)
    Xn = torch.from_numpy(((X - mean) / std).astype(np.float32))
    yt = torch.from_numpy((y == POSITIVE).astype(np.float32))
    loss_fn = torch.nn.BCEWithLogitsLoss(
        pos_weight=torch.tensor(pos_weight, dtype=torch.float32)
    )
    opt = torch.optim.Adam(net.parameters(), lr=CNN_LR)
    g = torch.Generator().manual_seed(seed)
    net.train()
    for _ in range(CNN_EPOCHS):
        perm = torch.randperm(len(y), generator=g)
        for k in range(0, len(y), CNN_BATCH):
            idx = perm[k : k + CNN_BATCH]
            opt.zero_grad()
            loss = loss_fn(net(Xn[idx]), yt[idx])
            loss.backward()
            opt.step()
    params = {
        k: v.detach().cpu().numpy().copy() for k, v in net.state_dict().items()
    }
    return CnnModel(
        mean=mean,
        std=std,
        pos_weight=pos_weight,
        params=params,
        sessions=tuple(d.name for d in used),
        n_fit_rows=len(y),
    )


def cnn_score(model: CnnModel, d: SessionData) -> Scored:
    M = d.feats.X
    prob = np.zeros(len(M))
    for k in range(0, len(M), CNN_SCORE_BATCH):
        Z = ((M[k : k + CNN_SCORE_BATCH] - model.mean) / model.std).astype(
            np.float32
        )
        prob[k : k + CNN_SCORE_BATCH] = features_v2.cnn_forward(model.params, Z)
    return Scored(data=d, prob=prob)


def cnn_cross_validate(data: Sequence[SessionData], seed: int = CNN_SEED):
    """train_eval.cross_validate と同じ fold の分け方（cv_groups）。fold の学習側
    だけで正規化・陽性の重み・学習。戻り値 (folds, scored, models)。"""
    groups = train_eval.cv_groups(data)
    if len(groups) < 2:
        raise SystemExit(
            "交差検証のグループが 2 つ未満です（学習側: "
            f"{', '.join(d.name for d in data)}）。評価側のセッションでは補いません"
        )
    folds, scored, models = [], [], []
    for name, val in groups:
        val_names = {d.name for d in val}
        rest = [d for d in data if d.name not in val_names]
        where = f"CNN の交差検証の fold（検証側 = {name}: {', '.join(sorted(val_names))}）"
        model = cnn_fit(rest, where, seed)
        models.append(model)
        folds.append(
            Fold(
                name=name,
                val_sessions=tuple(d.name for d in val),
                train_sessions=tuple(d.name for d in rest),
                standardizer_sessions=model.sessions,
                n_fit_rows=model.n_fit_rows,
            )
        )
        scored.extend(cnn_score(model, d) for d in val)
    return folds, scored, models


# --- 後処理と動作点 ---


def masks(scored: Sequence[Scored], post: str, op: tuple) -> list:
    """セッションごとの陽性窓の mask。"""
    if post == "none":
        return [
            postprocess.threshold(s.prob, s.data.feats.valid, op[0])
            for s in scored
        ]
    return [
        postprocess.hysteresis(
            s.prob, s.data.feats.valid, op[0], op[1], s.data.feats.t_start_s
        )
        for s in scored
    ]


def metrics_at(scored: Sequence[Scored], post: str, op: tuple) -> dict:
    per = {}
    for s, m in zip(scored, masks(scored, post, op)):
        d = s.data
        per[d.name] = evaluate.event_metrics(
            d.swallows, d.feats.t_start_s[m].tolist(), d.duration_s, d.t0_s
        )
    return train_eval.sum_metrics(per)


def hysteresis_table(scored: Sequence[Scored]) -> list:
    """格子（D9）の各点で採点して合算する。検出率か誤検出率が出ないなら止まる。"""
    names = ", ".join(s.data.name for s in scored)
    rows = []
    for enter, exit_ in postprocess.HYST_GRID:
        m = metrics_at(scored, "hyst", (enter, exit_))
        if m["swallows"] == 0:
            raise SystemExit(
                f"交差検証の合算で嚥下が 0 件です（検出率が出ません）。検証したセッション: {names}"
            )
        if m["false_positives_per_min"] is None:
            raise SystemExit(
                f"交差検証の合算で非嚥下の時間が 0 です（誤検出率が出ません）。検証したセッション: {names}"
            )
        rows.append({"enter": enter, "exit": exit_, **m})
    return rows


def pick_hysteresis(table: Sequence[dict]) -> tuple:
    """0014 の規則: 誤検出率が 3.0 回/分以下のうち検出率が最大（同率なら enter
    が高いほう、さらに exit が高いほう）。無ければ誤検出率が最小（同率なら
    enter・exit が高いほう）。"""
    lim = train_eval.PASS_FP_PER_MIN
    ok = [r for r in table if r["false_positives_per_min"] <= lim]
    if ok:
        b = max(ok, key=lambda r: (r["detection_rate"], r["enter"], r["exit"]))
        return (b["enter"], b["exit"]), (
            f"誤検出率が {lim} 回/分以下の点のうち、検出率が最大（同率なら enter、さらに exit が高いほう）"
        )
    b = min(
        table,
        key=lambda r: (r["false_positives_per_min"], -r["enter"], -r["exit"]),
    )
    return (b["enter"], b["exit"]), (
        f"誤検出率が {lim} 回/分以下の点が無いので、誤検出率が最小（同率なら enter・exit が高いほう）"
    )


def make_rows(feature_set: str, scored: Sequence[Scored]) -> list:
    """組 1 つの「なし」と「ヒステリシス」の 2 行。"""
    cnn = feature_set == CNN_SET
    dims = (
        98 * features_v2.N_MEL_CNN if cnn else features_v2.N_DIMS[feature_set]
    )
    n_all = sum(len(s.prob) for s in scored)
    out = []
    t_none = train_eval.threshold_table(scored)
    thr, rule = train_eval.pick_threshold(t_none)
    t_hyst = hysteresis_table(scored)
    op_h, rule_h = pick_hysteresis(t_hyst)
    default = next(
        r for r in t_hyst if (r["enter"], r["exit"]) == postprocess.HYST_DEFAULT
    )
    for post, op, rl, table, dflt in (
        ("none", (thr,), rule, t_none, None),
        ("hyst", op_h, rule_h, t_hyst, default),
    ):
        n_pos = int(sum(m.sum() for m in masks(scored, post, op)))
        out.append(
            Row(
                set=feature_set,
                post=post,
                dims=dims,
                classifier="CNN" if cnn else "LR",
                candidate=not cnn,
                op=op,
                rule=rl,
                metrics=metrics_at(scored, post, op),
                positive_windows=n_pos,
                windows=n_all,
                table=table,
                default_row=dflt,
            )
        )
    return out


def _rank(r: Row) -> tuple:
    return SIMPLE_ORDER.index(r.set), POSTS.index(r.post)


def pick_row(rows: Sequence[Row]) -> tuple:
    """#40 の選択（D2）。対象はロジスティック回帰の行（candidate）だけ。誤検出率が
    3.0 回/分以下のうち検出率が最大（同率なら単純なほう）。無ければ誤検出率が最小
    （同率なら単純なほう）。"""
    lim = train_eval.PASS_FP_PER_MIN
    cand = [r for r in rows if r.candidate]
    if not cand:
        raise SystemExit("選択の対象の行（ロジスティック回帰）がありません")
    ok = [r for r in cand if r.metrics["false_positives_per_min"] <= lim]
    if ok:
        best = min(ok, key=lambda r: (-r.metrics["detection_rate"], *_rank(r)))
        return best, (
            f"ロジスティック回帰の {len(cand)} 行のうち、動作点の誤検出率が {lim} 回/分以下の行で検出率が最大"
            "（同率なら単純なほう: base15 < a52 < base15_mw < a52_mw、同じ組ならなし < ヒステリシス）"
        )
    best = min(
        cand, key=lambda r: (r.metrics["false_positives_per_min"], *_rank(r))
    )
    return best, (
        f"誤検出率が {lim} 回/分以下の行が無いので、ロジスティック回帰の {len(cand)} 行のうち誤検出率が最小"
        "（同率なら単純なほう）"
    )


# --- 実行 ---


def split_train(root: Path, eval_min: int) -> dict:
    parts = split.split_sessions(
        root, eval_min, subject=train_eval.SUBJECT, kind="throat"
    )
    train_eval.check_collection_days(
        parts["train"], parts["eval"], train_eval.THROAT_EVAL_DAYS
    )
    return parts


def cv_scored(
    root: Path, names: Sequence[str], feature_set: str, seed=CNN_SEED
):
    """組 1 つの交差検証。(folds, scored)。"""
    data = load_train(root, names, feature_set)
    if feature_set == CNN_SET:
        folds, scored, _ = cnn_cross_validate(data, seed)
    else:
        folds, scored = train_eval.cross_validate(data)
    return data, folds, scored


def run(root: Path, eval_min: int) -> CompareResult:
    t_all = time.perf_counter()
    root = Path(root)
    parts = split_train(root, eval_min)
    names = parts["train"]
    timings: dict = {"extract_s": {}, "cv_s": {}}
    all_folds, rows, scored_by_set, ref_rows = {}, [], {}, {}
    data_cnn = None
    train_windows = None
    for fs in SIMPLE_ORDER:
        t = time.perf_counter()
        data = load_train(root, names, fs)
        timings["extract_s"][fs] = time.perf_counter() - t
        t = time.perf_counter()
        if fs == CNN_SET:
            folds, scored, _ = cnn_cross_validate(data, CNN_SEED)
            data_cnn = data
        else:
            folds, scored = train_eval.cross_validate(data)
        timings["cv_s"][fs] = time.perf_counter() - t
        if train_windows is None:
            train_windows = train_eval.count_windows(data)
        all_folds[fs] = folds
        scored_by_set[fs] = scored
        rows.extend(make_rows(fs, scored))
    for seed in CNN_REF_SEEDS:
        t = time.perf_counter()
        _, sc, _ = cnn_cross_validate(data_cnn, seed)
        timings["cv_s"][f"{CNN_SET} 種 {seed}"] = time.perf_counter() - t
        ref_rows[seed] = make_rows(CNN_SET, sc)
    selected, rule = pick_row(rows)
    first = scored_by_set[LR_SETS[0]]
    always = train_eval.reference(first, 0.5)["always_positive"]
    timings.update(measure_timings(root, names[0]))
    timings["total_s"] = time.perf_counter() - t_all
    return CompareResult(
        eval_min=eval_min,
        train=list(names),
        eval=list(parts["eval"]),
        folds=all_folds,
        rows=rows,
        selected=selected,
        select_rule=rule,
        always=always,
        ref_rows=ref_rows,
        scored=scored_by_set,
        train_windows=train_windows,
        timings=timings,
    )


def _pct(v: list) -> tuple:
    a = np.asarray(v) * 1000.0
    return float(np.median(a)), float(np.percentile(a, 95))


def measure_timings(root: Path, name: str) -> dict:
    """1 窓あたりの特徴量の計算時間と CNN の numpy の推論時間（1 スレッド）。
    学習側の最初のセッションの先頭 TIMING_SAMPLES 窓。報告の末尾にだけ出す。"""
    from threadpoolctl import threadpool_limits

    x, sample_t, _, start_idx, _, _, t1 = features._throat_windows(root / name)
    lo, hi = features_v2._session_bounds(x, sample_t, start_idx, t1)
    starts = start_idx[:TIMING_SAMPLES]
    out: dict = {"feature_ms": {}, "n": len(starts)}
    rng = np.random.default_rng(0)
    params = {
        "conv1.weight": rng.normal(0, 0.1, (8, 1, 3, 3)).astype(np.float32),
        "conv1.bias": np.zeros(8, np.float32),
        "conv2.weight": rng.normal(0, 0.1, (16, 8, 3, 3)).astype(np.float32),
        "conv2.bias": np.zeros(16, np.float32),
        "conv3.weight": rng.normal(0, 0.1, (32, 16, 3, 3)).astype(np.float32),
        "conv3.bias": np.zeros(32, np.float32),
        "fc.weight": rng.normal(0, 0.1, (1, 32)).astype(np.float32),
        "fc.bias": np.zeros(1, np.float32),
    }
    with threadpool_limits(1):
        for fs in LR_SETS:
            ts = []
            for i in starts:
                t = time.perf_counter()
                features_v2.window_features(x, [i], fs, lo, hi)
                ts.append(time.perf_counter() - t)
            out["feature_ms"][fs] = _pct(ts)
        ts, ti = [], []
        for i in starts:
            t = time.perf_counter()
            M = features_v2.mfe_one(x[i : i + features_v2.WINDOW_SAMPLES])
            t2 = time.perf_counter()
            features_v2.cnn_forward(params, M[None])
            ts.append(t2 - t)
            ti.append(time.perf_counter() - t2)
        out["feature_ms"][CNN_SET] = _pct(ts)
        out["cnn_infer_ms"] = _pct(ti)
    return out


# --- 報告 ---


def _versions() -> list:
    import librosa, scipy, sklearn, torch

    direct = (
        f"- numpy {np.__version__}、scipy {scipy.__version__}、scikit-learn {sklearn.__version__}、"
        f"librosa {librosa.__version__}、torch {torch.__version__}"
    )
    dists = sorted(
        {
            (d.metadata["Name"] or "").lower(): d.version
            for d in importlib.metadata.distributions()
        }.items()
    )
    return [
        direct,
        "- `.venv` の全パッケージ（間接依存を含む）: "
        + "、".join(f"{n}=={v}" for n, v in dists),
        f"- Python {platform.python_version()}",
    ]


def _cpu() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _rate(v) -> str:
    return "—" if v is None else f"{v:.3f}"


def _per_min(v) -> str:
    return "—" if v is None else f"{v:.2f}"


def _op(r: Row) -> str:
    if r.post == "none":
        return f"閾値 {r.op[0]:.2f}"
    return f"入り {r.op[0]:.2f}・出 {r.op[1]:.2f}"


def _row_name(r: Row) -> str:
    return (
        f"{r.set}（{'CNN' if r.classifier == 'CNN' else 'ロジスティック回帰'}）"
    )


def _metric_cells(m: dict) -> str:
    return (
        f"{_rate(m['detection_rate'])}（{m['detected']}/{m['swallows']}） | "
        f"{_per_min(m['false_positives_per_min'])}（{m['false_positive_runs']} 回 / {m['non_swallow_min']:.2f} 分）"
    )


def format_report(r: CompareResult) -> str:
    """Markdown。時間の実測は末尾の節だけ（再現の対象外）。"""
    out = [
        "# M3 咽喉マイク: 特徴量の組と後処理の比較（学習側の交差検証だけ。#43）",
        "",
        "## 実行の条件",
        "",
        "- 実行: 学習側だけ（評価側は採点していない。評価側を採点する引数は無い）",
        f"- `--eval-min`: {r.eval_min}",
        f"- subject: `{train_eval.SUBJECT}`、分割: `kind=throat`",
        f"- 評価側の収集日: {train_eval.THROAT_EVAL_DAYS}（docs/decisions/0032）",
        f"- コミット: {train_eval.git_commit()}",
        *_versions(),
        f"- CPU: {_cpu()}、コア数（os.cpu_count）{os.cpu_count()}",
        f"- torch のスレッド数: {CNN_THREADS}、乱数の種: {CNN_SEED}（参考 {', '.join(map(str, CNN_REF_SEEDS))}）",
        f'- ロジスティック回帰: LogisticRegression(C={train_eval.LR_C}, class_weight="{train_eval.LR_CLASS_WEIGHT}", '
        f'max_iter={train_eval.LR_MAX_ITER}, solver="{train_eval.LR_SOLVER}")（0014）',
        f"- CNN: Conv 1→8→16→32（3×3）、パラメータ {features_v2.cnn_param_count()}、1 窓の積和 "
        f"{features_v2.cnn_macs()}、Adam 学習率 {CNN_LR}、バッチ {CNN_BATCH}、{CNN_EPOCHS} エポック",
        f"- 窓 {evaluate.WINDOW_S} 秒 / ホップ {evaluate.HOP_S} 秒、ヒステリシスの格子 {len(postprocess.HYST_GRID)} 点",
        "- 式・格子・選び方: docs/decisions/0033",
        "",
        f"## 学習側のセッション（{len(r.train)}）",
        "",
        *[f"- {n}" for n in r.train],
        "",
        f"## 評価側のセッション（{len(r.eval)}。評価側は採点していない。セッション名だけ）",
        "",
        *[f"- {n}" for n in r.eval],
        "",
        "## 交差検証の fold と正規化に使ったセッション",
        "",
    ]
    ev = set(r.eval)
    first = r.folds[SIMPLE_ORDER[0]]
    out += [
        f"- fold の数: {len(first)}（{'収集日ごと' if len(first) and first[0].name.startswith('収集日') else '1 セッションごと'}）",
        "",
        "| 検証側 | "
        + " | ".join(f"{fs} の正規化のセッション" for fs in SIMPLE_ORDER)
        + " |",
        "|---|" + "---|" * len(SIMPLE_ORDER),
    ]
    for k, f in enumerate(first):
        cells = []
        for fs in SIMPLE_ORDER:
            g = r.folds[fs][k]
            ss = set(g.standardizer_sessions)
            ok = (
                ss <= set(g.train_sessions)
                and not (ss & ev)
                and not (ss & set(g.val_sessions))
            )
            cells.append(
                f"{len(ss)} 本（{'学習側の fold だけ' if ok else '要確認: 学習側の fold の外を含む'}）"
            )
        out.append(
            f"| {', '.join(f.val_sessions)} | " + " | ".join(cells) + " |"
        )
    out += [
        "",
        f"- 無効な窓（`valid = False`）: {r.train_windows[0]} / {r.train_windows[1]}",
        "",
        "## 比較の表（学習側の交差検証）",
        "",
        "| 組 | 次元 | 後処理 | 動作点 | 学習側 CV 検出率 | 学習側 CV 誤検出率（回/分） | TP / FN / FP | 陽性窓の割合 "
        "| 追加の待ち時間 | #40 の候補 | 評価側（検出率・誤検出率） |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in r.rows:
        m = row.metrics
        mark = " **（選択）**" if row is r.selected else ""
        out.append(
            f"| {_row_name(row)}{mark} | {row.dims} | {POST_LABEL[row.post]} | {_op(row)} | "
            f"{_metric_cells(m)} | {m['detected']} / {m['swallows'] - m['detected']} / {m['false_positive_runs']} | "
            f"{_rate(row.positive_ratio)}（{row.positive_windows}/{row.windows}） | "
            f"{features_v2.EXTRA_LATENCY_S[row.set]:.1f} 秒 | "
            f"{'はい' if row.candidate else 'いいえ（参考）'} | （空欄。#40 で選んだ組について 1 回だけ） |"
        )
    a = r.always
    out += [
        "",
        f"- 常時陽性の場合（全窓を陽性にした場合。学習側 CV、全行で同じ）: 検出率 {_rate(a['detection_rate'])}"
        f"（{a['detected']}/{a['swallows']}）、誤検出 {a['false_positive_runs']} 回、"
        f"誤検出率 {_per_min(a['false_positives_per_min'])} 回/分",
        "- 次元の CNN は入力の大きさ（98 × 40）。",
        "- a52 と base15 の差には、メルの上端（4000 Hz / 8000 Hz）と統計量の両方が入る（0033、D3）。",
        "- 追加の待ち時間: 装置で判定の窓の結果が出るまでに、判定の窓の終端より後に要る音の長さ。"
        "採点（docs/evaluation.md の数え方）はこの遅れを含まない。",
        "- ヒステリシスの陽性窓の割合は、なしより増える（陽性の連続を延ばすだけ）。",
        "",
        "## 選んだ行（#40 の候補の中から）",
        "",
        f"- **{_row_name(r.selected)}、後処理 {POST_LABEL[r.selected.post]}、{_op(r.selected)}**",
        f"- 規則: {r.select_rule}。学習側の交差検証だけで選んだ",
        "- 注記: 10 行と閾値・格子を同じ交差検証の数字で選ぶので、選んだ行の学習側 CV の数字は楽観側に出る"
        "（同じ日の 2 セットが互いの学習側に入る。0032）。選んだ組の性能は #40 の評価側の数字で読む。",
        "",
        "## 案B（CNN）の参考（#40 の候補ではない）",
        "",
        f"- パラメータ {features_v2.cnn_param_count()}、1 窓の積和 {features_v2.cnn_macs()}（推論時間は末尾の時間の実測）",
        "",
        "| 種 | 後処理 | 動作点 | 学習側 CV 検出率 | 学習側 CV 誤検出率（回/分） |",
        "|---|---|---|---|---|",
    ]
    cnn0 = [row for row in r.rows if row.set == CNN_SET]
    for seed, rws in [(CNN_SEED, cnn0), *r.ref_rows.items()]:
        for row in rws:
            out.append(
                f"| {seed}{'（表の行）' if seed == CNN_SEED else '（参考）'} | {POST_LABEL[row.post]} | {_op(row)} | "
                f"{_metric_cells(row.metrics)} |"
            )
    out += ["", "## 付録: 行ごとの候補の表", ""]
    for row in r.rows:
        out += [
            f"### {_row_name(row)}、後処理 {POST_LABEL[row.post]}",
            "",
            f"- 決め方: {row.rule}",
        ]
        if row.default_row is not None:
            d = row.default_row
            out.append(
                f"- 参考（初期値 入り {d['enter']:.2f}・出 {d['exit']:.2f}）: {_metric_cells(d)}"
            )
        out.append("")
        if row.post == "none":
            out += [
                "| 閾値 | 検出率 | 検出/嚥下 | 誤検出（回） | 誤検出率（回/分） | 採用 |",
                "|---|---|---|---|---|---|",
            ]
            for t in row.table:
                out.append(
                    f"| {t['threshold']:.2f} | {_rate(t['detection_rate'])} | {t['detected']}/{t['swallows']} "
                    f"| {t['false_positive_runs']} | {_per_min(t['false_positives_per_min'])} "
                    f"| {'○' if (t['threshold'],) == row.op else ''} |"
                )
        else:
            out += [
                "| 入り | 出 | 検出率 | 検出/嚥下 | 誤検出（回） | 誤検出率（回/分） | 採用 |",
                "|---|---|---|---|---|---|---|",
            ]
            for t in row.table:
                out.append(
                    f"| {t['enter']:.2f} | {t['exit']:.2f} | {_rate(t['detection_rate'])} | {t['detected']}/{t['swallows']} "
                    f"| {t['false_positive_runs']} | {_per_min(t['false_positives_per_min'])} "
                    f"| {'○' if (t['enter'], t['exit']) == row.op else ''} |"
                )
        out.append("")
    out.append(TIMING_HEADING)
    out.append("")
    out += format_timings(r.timings)
    return "\n".join(out)


TIMING_HEADING = "## 時間の実測（再現の条件の対象外）"


def format_timings(t: dict) -> list:
    if not t:
        return ["- （測っていない）"]
    out = [f"- 全体の実行時間: {t.get('total_s', float('nan')):.1f} 秒"]
    for fs, v in t.get("extract_s", {}).items():
        out.append(f"- 特徴量の計算（学習側の全セッション、{fs}）: {v:.1f} 秒")
    for fs, v in t.get("cv_s", {}).items():
        out.append(f"- 交差検証（{fs}）: {v:.1f} 秒")
    n = t.get("n", 0)
    for fs, (med, p95) in t.get("feature_ms", {}).items():
        out.append(
            f"- 1 窓の特徴量の計算（{fs}、1 スレッド、{n} 窓）: 中央値 {med:.2f} ms、95 パーセンタイル {p95:.2f} ms"
        )
    if "cnn_infer_ms" in t:
        med, p95 = t["cnn_infer_ms"]
        out.append(
            f"- CNN の 1 窓の推論（numpy、1 スレッド、{n} 窓）: 中央値 {med:.2f} ms、95 パーセンタイル {p95:.2f} ms"
        )
    return out


def main(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "root", type=Path, help="セッションフォルダの親（data/raw）"
    )
    ap.add_argument(
        "--eval-min",
        type=int,
        required=True,
        help="評価側（最後の 1 収集日）のセッション数",
    )
    ap.add_argument(
        "--figures",
        type=Path,
        help="図の PNG の出力先（docs/log/ の下だけ）。学習側のセッションだけを描く",
    )
    a = ap.parse_args(argv)
    if a.figures is not None:
        import visualize

        visualize.check_out_dir(a.figures)  # 計算の前に止める
    r = run(a.root, a.eval_min)
    print(format_report(r))
    if a.figures is not None:
        import visualize

        paths = visualize.draw_all(a.root, a.eval_min, r, a.figures)
        print(f"\n図: {len(paths)} 枚（{a.figures}）", file=sys.stderr)


if __name__ == "__main__":
    main()
