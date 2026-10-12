"""咽喉マイク（M3）の評価側を 1 回だけ採点する（Issue #40、docs/decisions/0034）。

使い方: python analysis/final_eval.py <root>

  引数は root だけ。構成・閾値・後処理・種・評価側の日数・--eval-min を選ぶ引数は
  無い。再実行の引数（--reproduce など）も無い。

事前に登録した 2 つの構成（CONFIGS。0034 の FINAL-CONFIG の行と同じ）を、学習側の
全 14 本で学習し直し、評価側（最後の 1 収集日の 7 本。docs/decisions/0032）を
1 回だけ採点する。判定（#41）は構成 J の数字で読む。構成 R は M1 と同じ手法の参照。

手順（前の手順で止まったら、後の手順に進まない）:
  1. 実行前の検査（データを 1 つも開かない）: git（追跡ファイルの変更、
     analysis・tools・docs/decisions・docs/evaluation.md の未追跡ファイル）、
     0034 がコミット済み、0034 の FINAL-* の行とこのファイルの定数が一致、
     印（MARKER）が無い、図の出力先が無い、依存（torch・librosa・matplotlib）。
  2. 分割: split_sessions(kind="throat") の一覧が EXPECTED_TRAIN・EXPECTED_EVAL と
     一致（meta.json とファイルの有無だけを見る）。
  3. 学習（学習側だけ）: 構成 R は train_eval.fit_model、構成 J は compare.cnn_fit。
  4. 印を書く: 固定のパス MARKER を O_EXCL で排他作成し、書き込み・fsync・close
     （親のディレクトリも fsync）の後に次へ進む。失敗したら評価側を開かずに止まる。
     印は、この後の手順で止まっても消さない。
  5. 評価側の採点: 各構成で評価側の各セッションをちょうど 1 回採点し、
     postprocess.hysteresis → evaluate.event_metrics → evaluate.aggregate。
  6. 報告: 標準出力に Markdown。
  7. 図: 手順 5 の確率と mask から docs/log/<実行日>-40/ に PNG を描く。

ファイルに書くのは印と PNG だけ。モデル・特徴量・確率は保存しない。報告に波形、
特徴量の値、個々の窓の確率は出さない。
"""

from __future__ import annotations
import argparse, importlib, os, sys, time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Sequence

import compare, evaluate, features_v2, fixed_eval, postprocess, split
import train_eval
import visualize

# --- 定数（docs/decisions/0034。評価側を開く前に固定。実行時に 0034 と照合） ---
DECISION = fixed_eval.DECISION


@dataclass(frozen=True)
class Config:
    key: str
    set: str
    classifier: str  # "cnn" / "lr"
    seed: int | None  # CNN の乱数の種（LR は None）
    post: str  # "hyst" だけ
    op: tuple  # (enter, exit)
    role: str


CONFIGS = (
    Config(
        key="J",
        set="mfe40",
        classifier="cnn",
        seed=0,
        post="hyst",
        op=(0.95, 0.85),
        role="判定に使う",
    ),
    Config(
        key="R",
        set="a52",
        classifier="lr",
        seed=None,
        post="hyst",
        op=(0.85, 0.65),
        role="M1 と同じ手法の参照",
    ),
)
# 固定の一覧・印・リポジトリ直下は fixed_eval.py（#43 の経路からも読む）
EXPECTED_TRAIN = fixed_eval.EXPECTED_TRAIN
EXPECTED_EVAL = fixed_eval.EXPECTED_EVAL
EVAL_MIN = 7  # FINAL-EVAL の本数。引数では選べない
MARKER = fixed_eval.MARKER  # 固定のパス（実行日によらない）
# 文書・印・図・git のパスはここを基準にする
REPO_ROOT = fixed_eval.REPO_ROOT
# 未追跡のファイルも含めて変更が無いことを確かめる場所
GIT_PATHS = ("analysis", "tools", "docs/decisions", "docs/evaluation.md")
DEPENDENCIES = ("torch", "librosa", "matplotlib")
FIG_SUFFIX = "-40"
M1_REFERENCE = (
    "- 参考: M1 の評価側の数字（基板の IMU＋PDM マイク、docs/decisions/0017）は"
    "検出率 0.783（54/69）、誤検出率 1.99 回/分。センサも装着も手法も違うので、"
    "そのまま比べない。"
)
TIMING_HEADING = "## 時間の実測（再現の条件の対象外）"


def _now() -> datetime:
    return datetime.now().astimezone()


def config_line(c: Config) -> str:
    seed = "" if c.seed is None else f" seed={c.seed}"
    return (
        f"FINAL-CONFIG {c.key}: set={c.set} classifier={c.classifier}{seed} "
        f"post={c.post} enter={c.op[0]:.2f} exit={c.op[1]:.2f}"
    )


def expected_lines() -> list:
    """0034 にあるべき FINAL-* の行（この順）。"""
    return [
        *(config_line(c) for c in CONFIGS),
        "FINAL-TRAIN: " + " ".join(EXPECTED_TRAIN),
        "FINAL-EVAL: " + " ".join(EXPECTED_EVAL),
    ]


def _desc(c: Config) -> str:
    if c.classifier == "cnn":
        return f"{c.set} CNN 種 {c.seed}"
    return f"{c.set} ロジスティック回帰"


def _op_text(c: Config) -> str:
    return f"ヒステリシス 入り {c.op[0]:.2f}・出 {c.op[1]:.2f}"


def config_heading(c: Config) -> str:
    return f"## 構成 {c.key}（{c.role}）: {_desc(c)}、{_op_text(c)}"


# --- 結果 ---


@dataclass(eq=False)
class ConfigResult:
    config: Config
    model: object  # train_eval.Model / compare.CnnModel
    norm_sessions: tuple  # 正規化と学習に使ったセッション
    n_fit_rows: int
    pos_weight: float | None  # CNN だけ
    scored: list = field(default_factory=list)  # 評価側（図だけに使う）
    masks: list = field(default_factory=list)
    per_session: dict = field(default_factory=dict)
    total: dict | None = None
    reference: dict | None = None
    eval_windows: tuple | None = None


@dataclass(eq=False)
class FinalResult:
    root: Path
    eval_min: int
    train: list
    eval: list
    commit: str
    started: str
    out_dir: Path
    configs: list  # [ConfigResult]（CONFIGS の順）
    timings: dict = field(default_factory=dict)


# --- 手順 1: 実行前の検査（データを 1 つも開かない） ---


def check_git() -> None:
    if train_eval.git_dirty():
        raise SystemExit(
            "追跡しているファイルに未コミットの変更があります（または git が使えません）。"
            "コミットしてから実行してください（docs/decisions/0034）"
        )
    out = train_eval._git("status", "--porcelain", "--", *GIT_PATHS)
    if out is None:
        raise SystemExit("git が使えません（docs/decisions/0034）")
    if out.strip():
        raise SystemExit(
            f"{'・'.join(GIT_PATHS)} に未コミットの変更か未追跡のファイルがあります。"
            "コミットするか消してから実行してください:\n" + out.rstrip()
        )


def check_decision() -> None:
    path = REPO_ROOT / DECISION
    if not path.is_file():
        raise SystemExit(
            f"{DECISION} がありません（評価側の採点の前に書いてコミットする）"
        )
    if train_eval._git("ls-files", "--error-unmatch", "--", DECISION) is None:
        raise SystemExit(
            f"{DECISION} が git に追跡されていません（コミットしてから実行する）"
        )
    got = [
        s.strip()
        for s in path.read_text(encoding="utf-8").splitlines()
        if s.strip().startswith("FINAL-")
    ]
    want = expected_lines()
    if got != want:
        raise SystemExit(
            f"{DECISION} の FINAL-* の行が analysis/final_eval.py の定数と一致しません。\n"
            "0034:\n" + "\n".join(got) + "\nコード:\n" + "\n".join(want)
        )


def check_marker() -> None:
    p = REPO_ROOT / MARKER
    if p.exists() or p.is_symlink():
        raise SystemExit(
            f"印 {MARKER} があります。評価側は採点済み（または採点を始めた実行がある）"
            "ので、採点し直しません（docs/decisions/0034）"
        )


def fig_dir(started: datetime) -> Path:
    return REPO_ROOT / "docs" / "log" / f"{started:%Y-%m-%d}{FIG_SUFFIX}"


def check_fig_dir(out: Path) -> None:
    visualize.check_out_dir(out)
    if out.exists() or out.is_symlink():
        raise SystemExit(f"図の出力先が既にあります（上書きしない）: {out}")


def check_dependencies() -> None:
    for name in DEPENDENCIES:
        try:
            importlib.import_module(name)
        except ImportError as e:
            raise SystemExit(f"{name} を import できません: {e}")
    features_v2.check_librosa_cache()


def preflight(started: datetime) -> tuple:
    """手順 1。(コミット, 図の出力先)。"""
    check_git()
    check_decision()
    check_marker()
    out = fig_dir(started)
    check_fig_dir(out)
    check_dependencies()
    return train_eval.git_commit(), out


# --- 手順 2: 分割 ---


def split_and_check(root: Path) -> dict:
    parts = split.split_sessions(
        root, EVAL_MIN, subject=train_eval.SUBJECT, kind="throat"
    )
    train_eval.check_collection_days(
        parts["train"], parts["eval"], train_eval.THROAT_EVAL_DAYS
    )
    for side, got, want in (
        ("学習側", parts["train"], EXPECTED_TRAIN),
        ("評価側", parts["eval"], EXPECTED_EVAL),
    ):
        if tuple(got) != tuple(want):
            raise SystemExit(
                f"{side}の一覧が {DECISION} と一致しません（セッションの追加・削除・名前の変更）。\n"
                f"分割: {', '.join(got)}\n0034: {', '.join(want)}"
            )
    return parts


# --- 手順 3: 学習（学習側だけ） ---


def _load(root: Path, names: Sequence[str], feature_set: str) -> list:
    ex = compare._extract_fn(feature_set)
    return [
        train_eval.load_session(root / n, ex, evaluate.throat_session_span)
        for n in names
    ]


def fit(root: Path, train: Sequence[str], c: Config) -> ConfigResult:
    data = compare.load_train(root, train, c.set)
    where = f"最終モデル（構成 {c.key}、学習側の全 {len(train)} 本）"
    if c.classifier == "cnn":
        model = compare.cnn_fit(data, where, seed=c.seed)
        norm, pos_weight = model.sessions, model.pos_weight
    else:
        model = train_eval.fit_model(data, where)
        norm, pos_weight = model.standardizer.sessions, None
    if not set(norm) <= set(train):
        raise AssertionError(
            f"構成 {c.key} の正規化に学習側の外のセッションが入っています"
        )
    return ConfigResult(
        config=c,
        model=model,
        norm_sessions=tuple(norm),
        n_fit_rows=model.n_fit_rows,
        pos_weight=pos_weight,
    )


# --- 手順 4: 印 ---


def marker_text(started: str, commit: str, command: str) -> str:
    return (
        "# #40 評価側の採点の印（docs/decisions/0034）。"
        "この印があれば analysis/final_eval.py は止まる\n"
        f"started: {started}\n"
        f"commit: {commit}\n"
        f"decision: {DECISION}\n"
        f"command: {command}\n"
    )


def write_marker(text: str) -> Path:
    """固定のパスに排他作成し、書き込み・fsync・close（親も fsync）の後に戻る。
    失敗したら評価側を開かずに止まる（作成した印は消さない）。"""
    path = REPO_ROOT / MARKER
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise SystemExit(
            f"印 {MARKER} が既にあります（別の実行が作った）。評価側は開いていません"
        )
    except OSError as e:
        raise SystemExit(
            f"印 {MARKER} を作れません（評価側は開いていません）: {e}"
        )
    try:
        try:
            data = text.encode("utf-8")
            while data:
                n = os.write(fd, data)
                data = data[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError as e:
        raise SystemExit(
            f"印 {MARKER} の書き込みに失敗しました（印は消していません。"
            f"評価側は開いていません）: {e}"
        )
    return path


# --- 手順 5: 評価側の採点（各構成で各セッション 1 回だけ） ---


def score_eval(root: Path, eval_names: Sequence[str], r: ConfigResult) -> None:
    c = r.config
    if c.post != "hyst":
        raise AssertionError(f"後処理は hyst だけです: {c.post}")
    enter, exit_ = c.op
    data = _load(root, eval_names, c.set)
    score = compare.cnn_score if c.classifier == "cnn" else train_eval.score
    scored = [score(r.model, d) for d in data]
    masks = [
        postprocess.hysteresis(
            s.prob, s.data.feats.valid, enter, exit_, s.data.feats.t_start_s
        )
        for s in scored
    ]
    per = {}
    for s, m in zip(scored, masks):
        d = s.data
        per[d.name] = evaluate.event_metrics(
            d.swallows, d.feats.t_start_s[m].tolist(), d.duration_s, d.t0_s
        )
    # aggregate に渡すキーは split_sessions の eval の一覧そのまま（0014 手順 6）
    if list(per) != list(eval_names):
        raise AssertionError(
            "aggregate に渡すセッションが split_sessions の eval の一覧と一致しません"
        )
    r.total = evaluate.aggregate(per)
    r.per_session = per
    always = train_eval.sum_metrics(
        {
            s.data.name: evaluate.event_metrics(
                s.data.swallows,
                s.data.feats.t_start_s.tolist(),
                s.data.duration_s,
                s.data.t0_s,
            )
            for s in scored
        }
    )
    n_pos = int(sum(int(m.sum()) for m in masks))
    n_all = sum(len(s.prob) for s in scored)
    r.reference = {
        "always_positive": always,
        "positive_windows": n_pos,
        "windows": n_all,
        "positive_ratio": n_pos / n_all if n_all else None,
    }
    r.eval_windows = train_eval.count_windows(data)
    r.scored, r.masks = scored, masks


# --- 実行（手順 1〜5） ---


def run(root: Path, command: str | None = None) -> FinalResult:
    t_all = time.perf_counter()
    root = Path(root)
    started = _now()
    commit, out = preflight(started)
    parts = split_and_check(root)
    timings: dict = {"fit_s": {}, "eval_s": {}}
    results = []
    for c in CONFIGS:
        t = time.perf_counter()
        results.append(fit(root, parts["train"], c))
        timings["fit_s"][c.key] = time.perf_counter() - t
    head = train_eval._git("rev-parse", "HEAD")
    write_marker(
        marker_text(
            started.isoformat(timespec="seconds"),
            (head or "").strip() or commit,
            command or f"python analysis/final_eval.py {root}",
        )
    )
    for r in results:
        t = time.perf_counter()
        score_eval(root, parts["eval"], r)
        timings["eval_s"][r.config.key] = time.perf_counter() - t
    timings["run_s"] = time.perf_counter() - t_all
    return FinalResult(
        root=root,
        eval_min=EVAL_MIN,
        train=list(parts["train"]),
        eval=list(parts["eval"]),
        commit=commit,
        started=started.isoformat(timespec="seconds"),
        out_dir=out,
        configs=results,
        timings=timings,
    )


# --- 手順 7: 図 ---


def draw_figures(r: FinalResult) -> list:
    """構成 J のセッションごと・イベントごとの図と、評価側 2 点の比較の図。"""
    j = next(x for x in r.configs if x.config.key == "J")
    paths = visualize.draw(
        r.root,
        r.eval_min,
        j.scored,
        j.masks,
        r.out_dir,
        f"構成 J: {_desc(j.config)}・{_op_text(j.config)}",
        side="eval",
    )
    points = [
        dict(
            label=f"構成 {x.config.key}（{x.config.role}）: {_desc(x.config)}",
            fp_per_min=x.total["false_positives_per_min"],
            detection_rate=x.total["detection_rate"],
        )
        for x in r.configs
    ]
    path = r.out_dir / "final.png"
    visualize.fig_final(path, points)
    return [*paths, path]


# --- 報告 ---


def _cond(name: str) -> str:
    return name.rsplit("_", 1)[-1]


def _config_section(x: ConfigResult, n_train: int, train: set) -> list:
    c, t = x.config, x.total
    only_train = set(x.norm_sessions) <= train
    out = [
        config_heading(c),
        "",
        "### 学習（学習側だけ）",
        "",
        f"- 正規化と学習に使ったセッション: {len(x.norm_sessions)} 本"
        f"（学習側 {n_train} 本のうち。学習側だけ: {'はい' if only_train else '要確認'}）",
        f"- fit に使った窓（valid かつ陽性か陰性）: {x.n_fit_rows}",
    ]
    if x.pos_weight is not None:
        out.append(f"- 陽性の重み（陰性数 ÷ 陽性数）: {x.pos_weight:.4f}")
    out += [
        "",
        "### セッションごと",
        "",
        "| セッション | cond | 嚥下 | 検出 | 誤検出（回） | 非嚥下（分） | 陽性窓 / 全窓 |",
        "|---|---|---|---|---|---|---|",
    ]
    for s, m in zip(x.scored, x.masks):
        p = x.per_session[s.data.name]
        out.append(
            f"| {s.data.name} | {_cond(s.data.name)} | {p['swallows']} | {p['detected']} "
            f"| {p['false_positive_runs']} | {p['non_swallow_min']:.2f} | {int(m.sum())} / {len(m)} |"
        )
    cm = t["confusion"]
    det, fp = t["detection_rate"], t["false_positives_per_min"]
    out += [
        "",
        "### 合算",
        "",
        f"- 検出率: {train_eval._rate(det)}（{t['detected']}/{t['swallows']}）",
        f"- 誤検出率: {train_eval._per_min(fp)} 回/分"
        f"（{t['false_positive_runs']} 回 / {t['non_swallow_min']:.2f} 分）",
        f"- 無効な窓（採点では陰性として扱う）: {x.eval_windows[0]} / {x.eval_windows[1]}",
        "",
        "### 混同行列（イベント単位。TN は定義できないので出さない）",
        "",
        "| | 陽性と出た | 陽性と出なかった |",
        "|---|---|---|",
        f"| 嚥下 | TP {cm['tp']} | FN {cm['fn']} |",
        f"| 非嚥下区間 | FP {cm['fp']}（回） | — |",
        "",
        "### 参考値（docs/decisions/0011「既知の弱点」）",
        "",
        *train_eval._reference_lines(x.reference),
        "",
        "### 合格線との比較（docs/evaluation.md、M1 分岐点の合格線を咽喉マイクに当てる。"
        "docs/plan.md の M3 (3)）",
        "",
        f"- 検出率 {train_eval._rate(det)}（合格線 {train_eval.PASS_DETECTION_RATE:.2f} 以上）: "
        f"{train_eval._meets(None if det is None else det >= train_eval.PASS_DETECTION_RATE)}",
        f"- 誤検出率 {train_eval._per_min(fp)} 回/分（合格線 {train_eval.PASS_FP_PER_MIN:.1f} 回/分以下）: "
        f"{train_eval._meets(None if fp is None else fp <= train_eval.PASS_FP_PER_MIN)}",
        "",
    ]
    return out


def format_report(r: FinalResult) -> str:
    """Markdown（時間の実測の節を除く。format_timings を後に続ける）。"""
    out = [
        "# M3 咽喉マイク: 評価側の採点（1 回だけ。#40、docs/decisions/0034）",
        "",
        "## 実行の条件",
        "",
        f"- 実行: 評価側の採点（印 `{MARKER}` を作成してから評価側を開いた）",
        f"- `--eval-min` に当たる定数: {r.eval_min}（引数では選べない）",
        f"- subject: `{train_eval.SUBJECT}`、分割: `kind=throat`",
        f"- 評価側の収集日: {train_eval.THROAT_EVAL_DAYS}（docs/decisions/0032。"
        f"M1 は {train_eval.EVAL_DAYS} 収集日）",
        f"- コミット: {r.commit}",
        f"- 手順と構成の定義: {DECISION}",
        *compare._versions(),
        f"- CPU: {compare._cpu()}、コア数（os.cpu_count）{os.cpu_count()}",
        f"- torch のスレッド数: {compare.CNN_THREADS}",
        *(
            f"- 構成 {c.key} の固定値（0034）: `{config_line(c)}`"
            for c in CONFIGS
        ),
        f'- ロジスティック回帰: LogisticRegression(C={train_eval.LR_C}, class_weight="{train_eval.LR_CLASS_WEIGHT}", '
        f'max_iter={train_eval.LR_MAX_ITER}, solver="{train_eval.LR_SOLVER}")（docs/decisions/0014）',
        f"- CNN: Conv 1→8→16→32（3×3）、パラメータ {features_v2.cnn_param_count()}、Adam 学習率 {compare.CNN_LR}、"
        f"バッチ {compare.CNN_BATCH}、{compare.CNN_EPOCHS} エポック、陽性の重み = 陰性数 ÷ 陽性数、決定的"
        "（docs/decisions/0033 §2）",
        f"- 窓 {evaluate.WINDOW_S} 秒 / ホップ {evaluate.HOP_S} 秒",
        "",
        f"## 学習に使ったセッション（{len(r.train)}）",
        "",
        *[f"- {n}（{_cond(n)}）" for n in r.train],
        "",
        f"## 評価に使ったセッション（{len(r.eval)}）",
        "",
        *[f"- {n}（{_cond(n)}）" for n in r.eval],
        "",
    ]
    for x in r.configs:
        out += _config_section(x, len(r.train), set(r.train))
    out += [
        "## 注記",
        "",
        "- 判定（#41）は構成 J の数字で読む。構成 J は `docs/evaluation.md` 28 行目の手法（手作り特徴量＋ロジスティック回帰）と"
        "8 行目の特徴量の並びからの記録した逸脱（docs/decisions/0034）。構成 R の `a52` は 8 行目の「広げた読み」"
        "（docs/decisions/0033 §5）で、28 行目の手法のまま。",
        f"- 2 つの構成は評価側を開く前に固定した（docs/decisions/0034）。どちらも学習側の全 {len(r.train)} 本で学習し直し、"
        "閾値・後処理・種は選び直していない。",
        f"- 評価側は {train_eval.THROAT_EVAL_DAYS} 収集日（docs/decisions/0032）。"
        f"M1 の評価側は {train_eval.EVAL_DAYS} 収集日。",
        M1_REFERENCE,
        "- 学習側 CV の数字（docs/log/2026-10-12.md「#43 段階 2」）は選択に使った数字で楽観側。評価側の数字とは別に読む。",
        "",
    ]
    return "\n".join(out)


def format_timings(t: dict) -> str:
    out = [TIMING_HEADING, ""]
    if "run_s" in t:
        out.append(f"- 手順 1〜5 の時間: {t['run_s']:.1f} 秒")
    for k, v in t.get("fit_s", {}).items():
        out.append(f"- 学習（構成 {k}、特徴量の計算を含む）: {v:.1f} 秒")
    for k, v in t.get("eval_s", {}).items():
        out.append(f"- 評価側の特徴量と採点（構成 {k}）: {v:.1f} 秒")
    if "fig_s" in t:
        out.append(f"- 図（{t.get('fig_n', 0)} 枚）: {t['fig_s']:.1f} 秒")
    if "total_s" in t:
        out.append(f"- 全体: {t['total_s']:.1f} 秒")
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "root", type=Path, help="セッションフォルダの親（data/raw）"
    )
    a = ap.parse_args(argv)
    t0 = time.perf_counter()
    r = run(a.root, f"python analysis/final_eval.py {a.root}")
    # 数字は図より先に出す（図で止まっても、採点の結果は失わない）
    print(format_report(r), flush=True)
    t = time.perf_counter()
    paths = draw_figures(r)
    r.timings["fig_s"] = time.perf_counter() - t
    r.timings["fig_n"] = len(paths)
    r.timings["total_s"] = time.perf_counter() - t0
    print(format_timings(r.timings))
    print(f"\n図: {len(paths)} 枚（{r.out_dir}）", file=sys.stderr)


if __name__ == "__main__":
    main()
