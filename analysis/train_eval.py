"""M1 の分岐点: 手作り特徴量＋ロジスティック回帰で学習し、docs/evaluation.md の定義で採点する。

手順の定義と理由は docs/decisions/0014。実データを見る前に固定し、結果を見てから動かさない。

使い方: python analysis/train_eval.py <root> --eval-min <N> [--final]
  既定      学習側のセッションだけを使い、交差検証の数字と閾値を出す。評価側は採点しない
            （評価側の imu.csv・audio.wav・audio_chunks.csv・events.csv を開かない）。
  --final   上に加えて、評価側を1回採点する。

分割は split.split_sessions、特徴量は features.extract_session、数え方は evaluate.event_metrics / aggregate。
報告は標準出力に Markdown で出す。ファイルは書かない（モデル・特徴量・確率を保存しない）。
波形、特徴量の値、個々の窓の確率は出さない。
"""
from __future__ import annotations
import argparse, subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np
import sklearn
from sklearn.linear_model import LogisticRegression

import evaluate, features, split

# --- 定数（docs/decisions/0014。結果を見てから動かさない） ---
SUBJECT = "self"                  # docs/evaluation.md「M1・M2 の数字は self のみ」
EVAL_DAYS = 2                     # 評価側は最後の2収集日（docs/recording-protocol.md）
LABEL_POS_S = 1.0                 # 窓の中心が [t, t + 1.0] に入る窓が陽性
LABEL_MARGIN_S = 0.5              # 中心が [t − 0.5, t) か (t + 1.0, t + 1.5] の窓は学習に使わない
POSITIVE, NEGATIVE, UNUSED = 1, 0, -1
THRESHOLDS = tuple(round(0.05 * k, 2) for k in range(1, 20))   # 0.05, 0.10, …, 0.95
PASS_DETECTION_RATE = 0.70        # docs/evaluation.md の M1 の合格線
PASS_FP_PER_MIN = 3.0             # 同上。閾値の基準にも使う
LR_C = 1.0                        # ハイパーパラメータは固定で、探索しない
LR_CLASS_WEIGHT = "balanced"
LR_MAX_ITER = 1000
LR_SOLVER = "lbfgs"               # 乱数を使わない


@dataclass(frozen=True, eq=False)
class SessionData:
    name: str
    feats: features.WindowFeatures
    swallows: list                # 嚥下のマーカー（秒）
    t0_s: float                   # 記録の範囲（evaluate.session_span）
    duration_s: float
    labels: np.ndarray            # (n,) 学習のラベル。POSITIVE / NEGATIVE / UNUSED


@dataclass(frozen=True, eq=False)
class Scored:
    data: SessionData
    prob: np.ndarray              # (n,) 窓ごとの確率。報告にもファイルにも出さない


@dataclass(frozen=True, eq=False)
class Model:
    standardizer: features.Standardizer
    clf: LogisticRegression
    n_fit_rows: int               # 分類器の fit に渡した行数（valid かつ陽性か陰性の窓）


@dataclass(frozen=True, eq=False)
class Fold:
    name: str
    val_sessions: tuple
    train_sessions: tuple
    standardizer_sessions: tuple  # その fold の Standardizer.sessions
    n_fit_rows: int


@dataclass(frozen=True, eq=False)
class Result:
    eval_min: int
    final: bool
    train: list                   # セッション名
    eval: list
    folds: list
    cv_table: list                # 閾値の候補ごとの交差検証の数字
    threshold: float
    threshold_rule: str
    cv_reference: dict
    train_windows: tuple          # (無効な窓, 全窓)
    model: Model
    eval_per_session: dict | None     # --final のときだけ
    eval_conds: dict | None
    eval_total: dict | None
    eval_reference: dict | None
    eval_windows: tuple | None


# --- 分割の検査 ---

def _day(name: str) -> str:
    """フォルダ名の先頭 8 桁（収集日）。"""
    return name[:8]


def check_collection_days(train: Sequence[str], eval_: Sequence[str]) -> None:
    """評価側がちょうど 2 収集日、学習側が 1 収集日以上で、学習側のどの日も評価側のどの日より前。外れたら止まる。"""
    train_days = sorted({_day(n) for n in train})
    eval_days = sorted({_day(n) for n in eval_})
    if len(eval_days) != EVAL_DAYS:
        raise SystemExit(f"評価側の収集日が {EVAL_DAYS} 日ではありません（{len(eval_days)} 日: "
                         f"{', '.join(eval_days)}）。--eval-min は最後の2収集日のセッション数にしてください")
    if not train_days:
        raise SystemExit("学習側の収集日がありません（1 日以上が必要）")
    if train_days[-1] >= eval_days[0]:
        raise SystemExit(f"学習側と評価側の収集日が重なっています（学習側の最後 {train_days[-1]}、"
                         f"評価側の最初 {eval_days[0]}）。--eval-min は最後の2収集日のセッション数にしてください")


# --- ラベルと読み込み ---

def window_labels(t_start_s: np.ndarray, swallow_t: Sequence[float]) -> np.ndarray:
    """学習のラベル。陽性は「使わない」より優先する。評価の数え方には使わない。"""
    c = np.asarray(t_start_s, dtype=np.float64) + evaluate.WINDOW_S / 2
    labels = np.full(len(c), NEGATIVE, dtype=np.int8)
    for t in swallow_t:
        labels[(c >= t - LABEL_MARGIN_S) & (c <= t + LABEL_POS_S + LABEL_MARGIN_S)] = UNUSED
    for t in swallow_t:
        labels[(c >= t) & (c <= t + LABEL_POS_S)] = POSITIVE
    return labels


def load_session(session_dir: Path) -> SessionData:
    f = features.extract_session(session_dir)
    swallows = evaluate.load_events(session_dir)
    t0_s, duration_s = evaluate.session_span(session_dir)
    return SessionData(name=session_dir.name, feats=f, swallows=swallows, t0_s=t0_s, duration_s=duration_s,
                       labels=window_labels(f.t_start_s, swallows))


# --- モデル ---

def fit_model(data: Sequence[SessionData], where: str) -> Model:
    """data のセッションだけで Standardizer とモデルを作る。

    正規化の統計量は valid な窓の全部（「使わない」窓も含む）。分類器の fit は valid かつ陽性か陰性の窓だけ。
    """
    names = ", ".join(d.name for d in data)
    try:
        standardizer = features.Standardizer.fit([d.feats for d in data])
    except ValueError as e:
        raise SystemExit(f"{where}: {e}。学習に使うセッション: {names}")
    use = [d.feats.valid & (d.labels != UNUSED) for d in data]
    X = np.concatenate([d.feats.X[u] for d, u in zip(data, use)])
    y = np.concatenate([d.labels[u] for d, u in zip(data, use)])
    n_pos, n_neg = int((y == POSITIVE).sum()), int((y == NEGATIVE).sum())
    if n_pos == 0 or n_neg == 0:
        raise SystemExit(f"{where}: 学習に使う窓に陽性と陰性の両方がそろっていません（陽性 {n_pos}、陰性 {n_neg}）。"
                         f"学習に使うセッション: {names}")
    clf = LogisticRegression(C=LR_C, class_weight=LR_CLASS_WEIGHT, max_iter=LR_MAX_ITER, solver=LR_SOLVER)
    clf.fit(standardizer.transform(X), y)
    return Model(standardizer=standardizer, clf=clf, n_fit_rows=len(y))


def score(model: Model, d: SessionData) -> Scored:
    """セッションの全窓に確率を付ける（無効な窓にも付くが、採点では陽性にしない）。"""
    if len(d.feats.X) == 0:
        return Scored(data=d, prob=np.zeros(0))
    col = list(model.clf.classes_).index(POSITIVE)
    return Scored(data=d, prob=model.clf.predict_proba(model.standardizer.transform(d.feats.X))[:, col])


# --- 採点 ---

def positive_windows(s: Scored, threshold: float) -> list:
    """確率が閾値以上で valid な窓の開始時刻。無効な窓は陰性として扱う。"""
    return s.data.feats.t_start_s[(s.prob >= threshold) & s.data.feats.valid].tolist()


def session_metrics(s: Scored, threshold: float) -> dict:
    d = s.data
    return evaluate.event_metrics(d.swallows, positive_windows(s, threshold), d.duration_s, d.t0_s)


def sum_metrics(per_session: dict) -> dict:
    """回数の合算。式は evaluate.aggregate と同じで、3 セッション未満でも止めない（交差検証と参考値に使う）。"""
    swallows = sum(m["swallows"] for m in per_session.values())
    detected = sum(m["detected"] for m in per_session.values())
    fp_runs = sum(m["false_positive_runs"] for m in per_session.values())
    non_swallow_min = sum(m["non_swallow_min"] for m in per_session.values())
    return {
        "swallows": swallows,
        "detected": detected,
        "detection_rate": detected / swallows if swallows else None,
        "false_positive_runs": fp_runs,
        "non_swallow_min": non_swallow_min,
        "false_positives_per_min": fp_runs / non_swallow_min if non_swallow_min > 0 else None,
    }


def count_windows(data: Sequence[SessionData]) -> tuple:
    """(無効な窓の数, 全窓の数)。"""
    return (sum(int((~d.feats.valid).sum()) for d in data), sum(len(d.feats.valid) for d in data))


def reference(scored: Sequence[Scored], threshold: float) -> dict:
    """参考値: 全窓を陽性にした場合の数字と、モデルの陽性窓の割合（陽性窓 ÷ 全窓）。docs/decisions/0011「既知の弱点」。"""
    always = sum_metrics({s.data.name: evaluate.event_metrics(
        s.data.swallows, s.data.feats.t_start_s.tolist(), s.data.duration_s, s.data.t0_s) for s in scored})
    n_pos = sum(len(positive_windows(s, threshold)) for s in scored)
    n_all = sum(len(s.prob) for s in scored)
    return {"always_positive": always, "positive_windows": n_pos, "windows": n_all,
            "positive_ratio": n_pos / n_all if n_all else None}


def threshold_table(scored: Sequence[Scored]) -> list:
    """閾値の候補ごとに、評価と同じ数え方で採点して合算する。検出率か誤検出率が出ないなら止まる。"""
    names = ", ".join(s.data.name for s in scored)
    rows = []
    for thr in THRESHOLDS:
        m = sum_metrics({s.data.name: session_metrics(s, thr) for s in scored})
        if m["swallows"] == 0:
            raise SystemExit(f"交差検証の合算で嚥下が 0 件です（検出率が出ません）。検証したセッション: {names}")
        if m["false_positives_per_min"] is None:
            raise SystemExit(f"交差検証の合算で非嚥下の時間が 0 です（誤検出率が出ません）。検証したセッション: {names}")
        rows.append({"threshold": thr, **m})
    return rows


def pick_threshold(table: Sequence[dict]) -> tuple:
    """誤検出率が 3.0 回/分以下の候補のうち検出率が最大（同率なら高いほう）。無ければ誤検出率が最小（同率なら高いほう）。"""
    ok = [r for r in table if r["false_positives_per_min"] <= PASS_FP_PER_MIN]
    if ok:
        best = max(ok, key=lambda r: (r["detection_rate"], r["threshold"]))
        return best["threshold"], f"誤検出率が {PASS_FP_PER_MIN} 回/分以下の候補のうち、検出率が最大（同率なら高いほう）"
    best = min(table, key=lambda r: (r["false_positives_per_min"], -r["threshold"]))
    return best["threshold"], (f"誤検出率が {PASS_FP_PER_MIN} 回/分以下の候補が無いので、"
                               "誤検出率が最小（同率なら高いほう）")


# --- 交差検証（学習側だけ） ---

def cv_groups(data: Sequence[SessionData]) -> list:
    """学習側の収集日が 2 日以上なら収集日ごと、1 日なら1セッションごと。"""
    days = sorted({_day(d.name) for d in data})
    if len(days) >= 2:
        return [(f"収集日 {day}", [d for d in data if _day(d.name) == day]) for day in days]
    return [(f"セッション {d.name}", [d]) for d in data]


def cross_validate(data: Sequence[SessionData]) -> tuple:
    """fold ごとに、fold の学習側だけで Standardizer とモデルを作り、検証側の全窓に確率を付ける。"""
    groups = cv_groups(data)
    if len(groups) < 2:
        raise SystemExit("交差検証のグループが 2 つ未満です（学習側: "
                         f"{', '.join(d.name for d in data)}）。評価側のセッションでは補いません")
    folds, scored = [], []
    for name, val in groups:
        val_names = {d.name for d in val}
        rest = [d for d in data if d.name not in val_names]
        where = f"交差検証の fold（検証側 = {name}: {', '.join(sorted(val_names))}）"
        model = fit_model(rest, where)
        folds.append(Fold(name=name, val_sessions=tuple(d.name for d in val),
                          train_sessions=tuple(d.name for d in rest),
                          standardizer_sessions=model.standardizer.sessions, n_fit_rows=model.n_fit_rows))
        scored.extend(score(model, d) for d in val)
    return folds, scored


# --- 実行 ---

def _cond(name: str) -> str:
    return name.rsplit("_", 1)[-1]   # フォルダ名の cond（split.NAME_RE で形式は確認済み）


def run(root: Path, eval_min: int, final: bool = False) -> Result:
    root = Path(root)
    parts = split.split_sessions(root, eval_min, subject=SUBJECT)
    check_collection_days(parts["train"], parts["eval"])

    train = [load_session(root / name) for name in parts["train"]]
    folds, cv_scored = cross_validate(train)
    table = threshold_table(cv_scored)
    threshold, rule = pick_threshold(table)
    model = fit_model(train, "最終モデル（学習側の全セッション）")

    per_session = conds = total = eval_reference = eval_windows = None
    if final:
        eval_data = [load_session(root / name) for name in parts["eval"]]
        eval_scored = [score(model, d) for d in eval_data]
        per_session = {s.data.name: session_metrics(s, threshold) for s in eval_scored}
        # aggregate に渡すキーは split_sessions の eval の一覧そのまま（docs/decisions/0011「塞がっていない経路」）
        if list(per_session) != list(parts["eval"]):
            raise AssertionError("aggregate に渡すセッションが split_sessions の eval の一覧と一致しません")
        total = evaluate.aggregate(per_session)
        conds = {name: _cond(name) for name in parts["eval"]}
        eval_reference = reference(eval_scored, threshold)
        eval_windows = count_windows(eval_data)

    return Result(eval_min=eval_min, final=final, train=list(parts["train"]), eval=list(parts["eval"]),
                  folds=folds, cv_table=table, threshold=threshold, threshold_rule=rule,
                  cv_reference=reference(cv_scored, threshold), train_windows=count_windows(train), model=model,
                  eval_per_session=per_session, eval_conds=conds, eval_total=total,
                  eval_reference=eval_reference, eval_windows=eval_windows)


# --- 報告 ---

def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=Path(__file__).resolve().parent,
                             capture_output=True, text=True, check=True)
        return out.stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def git_dirty() -> bool:
    """追跡しているファイルに未コミットの変更があるか。git が使えないときも True（再現の条件を確かめられない）。"""
    out = _git("status", "--porcelain", "--untracked-files=no")
    return out is None or bool(out.strip())


def git_commit() -> str:
    """報告に出すコミット。未コミットの変更があれば `-dirty` を付ける（その数字は HEAD からは再現できない）。"""
    out = _git("rev-parse", "--short", "HEAD")
    sha = (out or "").strip() or "unknown"
    return f"{sha}-dirty" if git_dirty() else sha


def _rate(v) -> str:
    return "—" if v is None else f"{v:.3f}"


def _per_min(v) -> str:
    return "—" if v is None else f"{v:.2f}"


def _reference_lines(ref: dict) -> list:
    a = ref["always_positive"]
    return [f"- 常時陽性の場合（全窓を陽性にした場合）: 検出率 {_rate(a['detection_rate'])}"
            f"（{a['detected']}/{a['swallows']}）、誤検出 {a['false_positive_runs']} 回、"
            f"誤検出率 {_per_min(a['false_positives_per_min'])} 回/分",
            f"- モデルの陽性窓の割合（陽性窓 ÷ 全窓）: {_rate(ref['positive_ratio'])}"
            f"（{ref['positive_windows']}/{ref['windows']}）"]


def _meets(ok: bool | None) -> str:
    return "出ない" if ok is None else ("満たす" if ok else "満たさない")


def format_report(r: Result) -> str:
    """Markdown の報告。波形、特徴量の値、個々の窓の確率は入れない。"""
    out = ["# M1 分岐点: 手作り特徴量＋ロジスティック回帰", ""]
    out += ["## 実行の条件", "",
            f"- 実行: {'--final（評価側を採点した）' if r.final else '既定（学習側だけ。評価側は採点していない）'}",
            f"- `--eval-min`: {r.eval_min}",
            f"- subject: `{SUBJECT}`",
            f"- コミット: {git_commit()}",
            f"- scikit-learn {sklearn.__version__}、numpy {np.__version__}",
            f"- モデル: LogisticRegression(C={LR_C}, class_weight=\"{LR_CLASS_WEIGHT}\", max_iter={LR_MAX_ITER}, "
            f"solver=\"{LR_SOLVER}\")、特徴量 {features.N_FEATURES} 次元、窓 {evaluate.WINDOW_S} 秒 / "
            f"ホップ {evaluate.HOP_S} 秒",
            "- 手順の定義: docs/decisions/0014", ""]
    out += [f"## 学習に使ったセッション（{len(r.train)}）", ""] + [f"- {n}" for n in r.train] + [""]
    if r.final:
        out += [f"## 評価に使ったセッション（{len(r.eval)}）", ""]
    else:
        out += [f"## 評価側のセッション（{len(r.eval)}。評価側は採点していない。セッション名だけ）", ""]
    out += [f"- {n}" for n in r.eval] + [""]

    out += ["## 交差検証（学習側だけ）と閾値", "",
            f"- fold の数: {len(r.folds)}"]
    out += [f"  - 検証側 = {f.name}: {', '.join(f.val_sessions)}" for f in r.folds]
    out += [f"- 閾値: {r.threshold:.2f}",
            f"- 決め方: {r.threshold_rule}。学習側の交差検証だけで決めた",
            f"- 無効な窓（`valid = False`。学習に使わず、採点では陰性として扱う）: "
            f"{r.train_windows[0]} / {r.train_windows[1]}", "",
            "| 閾値 | 検出率 | 検出/嚥下 | 誤検出（回） | 誤検出率（回/分） | 採用 |",
            "|---|---|---|---|---|---|"]
    for row in r.cv_table:
        out.append(f"| {row['threshold']:.2f} | {_rate(row['detection_rate'])} | {row['detected']}/{row['swallows']} "
                   f"| {row['false_positive_runs']} | {_per_min(row['false_positives_per_min'])} "
                   f"| {'○' if row['threshold'] == r.threshold else ''} |")
    out += ["", "参考値（交差検証、採用した閾値）:", ""] + _reference_lines(r.cv_reference) + [""]

    if not r.final:
        out += ["## 評価", "", "評価側は採点していない（`--final` を付けたときだけ採点する）。", ""]
        return "\n".join(out)

    t = r.eval_total
    out += ["## 評価（評価側、セッションごと）", "",
            "| セッション | cond | 嚥下 | 検出 | 誤検出（回） | 非嚥下（分） |",
            "|---|---|---|---|---|---|"]
    for name, m in r.eval_per_session.items():
        out.append(f"| {name} | {r.eval_conds[name]} | {m['swallows']} | {m['detected']} "
                   f"| {m['false_positive_runs']} | {m['non_swallow_min']:.2f} |")
    c = t["confusion"]
    out += ["", "## 評価（合算）", "",
            f"- 検出率: {_rate(t['detection_rate'])}（{t['detected']}/{t['swallows']}）",
            f"- 誤検出率: {_per_min(t['false_positives_per_min'])} 回/分"
            f"（{t['false_positive_runs']} 回 / {t['non_swallow_min']:.2f} 分）",
            f"- 無効な窓（採点では陰性として扱う）: {r.eval_windows[0]} / {r.eval_windows[1]}", "",
            "混同行列（イベント単位。TN は定義できないので出さない）:", "",
            "| | 陽性と出た | 陽性と出なかった |", "|---|---|---|",
            f"| 嚥下 | TP {c['tp']} | FN {c['fn']} |",
            f"| 非嚥下区間 | FP {c['fp']}（回） | — |", "",
            "参考値（評価側、採用した閾値）:", ""] + _reference_lines(r.eval_reference) + [""]
    det, fp = t["detection_rate"], t["false_positives_per_min"]
    out += ["## 合格線との比較（docs/evaluation.md、M1 分岐点）", "",
            f"- 検出率 {_rate(det)}（合格線 {PASS_DETECTION_RATE:.2f} 以上）: "
            f"{_meets(None if det is None else det >= PASS_DETECTION_RATE)}",
            f"- 誤検出率 {_per_min(fp)} 回/分（合格線 {PASS_FP_PER_MIN:.1f} 回/分以下）: "
            f"{_meets(None if fp is None else fp <= PASS_FP_PER_MIN)}", ""]
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", type=Path, help="セッションフォルダの親（data/raw）")
    ap.add_argument("--eval-min", type=int, required=True, help="最後の2収集日のセッション数")
    ap.add_argument("--final", action="store_true", help="評価側を採点する")
    a = ap.parse_args(argv)
    if a.final and git_dirty():
        # 評価側の数字は、コミットから再現できる状態でだけ出す（docs/decisions/0014）
        raise SystemExit("未コミットの変更があります（または git が使えません）。--final は、追跡しているファイルを"
                         "コミットしてから実行してください。既定の実行（学習側だけ）は止めません")
    print(format_report(run(a.root, a.eval_min, a.final)))


if __name__ == "__main__":
    main()
