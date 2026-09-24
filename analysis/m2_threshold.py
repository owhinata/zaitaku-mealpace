"""検証側（収集日4）を書き出したライブラリで採点し、閾値の表を出して凍結する（Issue #22、docs/decisions/0019）。

使い方:
  python analysis/m2_threshold.py data/raw            # 表と参考値を出す（ファイルは書かない）
  python analysis/m2_threshold.py data/raw --freeze   # 加えて firmware/detector/m2_threshold.h を書く（未コミットの変更があれば止まる）

- 対象は `split.split_sessions(data/raw, 7, "self")` の `eval`（収集日4 の 7 本）だけ。収集日1〜3 は `meta.json` 以外を開かない。
- 確率は PC 上の実行ファイル（`m2_scorer.Scorer`。書き出した C++ ライブラリ ＋ `m2_norm.h`）から。特徴量は `features_m2.extract_session`
  （正規化前）。正規化は実行ファイルの中で行う。`valid = False` の窓は陽性にしない（docs/decisions/0014「無効な窓の効果」と同じ扱い）。
- 閾値の候補は `train_eval.THRESHOLDS`（0.05 刻み）。規則は誤検出率が 1.0 回/分以下の候補のうち検出率が最大（同率なら高いほう）、
  無ければ誤検出率が最小（同率なら高いほう）（0014 の形を M2 の合格線に当てたもの）。候補と規則は引数で変えられない。
- 数字はイベント単位（`evaluate.event_metrics` → 合算）。**装置上の推論の前の参考値で、M2 の合格線の数字は #27 で出す。**
- `--freeze` は `train_eval.git_dirty()` なら止まる（凍結した数字はコミットから再現できる状態でだけ出す）。ヘッダが既にあって内容が違えば止まる。
- 報告は標準出力に Markdown。波形・特徴量の値・個々の窓の確率は出さない。
"""
from __future__ import annotations
import argparse, sys
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

import ei_upload, evaluate, evaluate_detector, features_m2, m2_norm_header, m2_scorer, train_eval

THRESHOLDS = train_eval.THRESHOLDS                          # 0.05, 0.10, …, 0.95（決定 D1）
FP_PER_MIN_LIMIT = evaluate_detector.PASS_FP_PER_MIN        # 1.0（docs/evaluation.md の M2 の合格線。閾値の規則にも使う）
PASS_DETECTION_RATE = evaluate_detector.PASS_DETECTION_RATE  # 0.85（参考の比較にだけ使う）
COUGH_LINK_S = evaluate_detector.COUGH_LINK_S
CLASS_ORDER = ("swallow", "cough", "other")                 # 窓単位の混同行列の並び
NORM_PATH = ei_upload.NORM_PATH
NORM_HEADER_PATH = m2_norm_header.HEADER_PATH
THRESHOLD_HEADER_PATH = m2_norm_header.REPO / "firmware" / "detector" / "m2_threshold.h"
GUARD = "M2_THRESHOLD_H"
f32 = evaluate_detector.f32
LABEL3_NAME = {ei_upload.SWALLOW: "swallow", ei_upload.COUGH: "cough", ei_upload.OTHER: "other"}


# --- 閾値の規則 ---

def pick_threshold(table: Sequence[dict], max_fp_per_min: float) -> tuple[float, str]:
    """誤検出率が max_fp_per_min 以下の候補のうち検出率が最大（同率なら高いほう）。無ければ誤検出率が最小（同率なら高いほう）。

    max_fp_per_min = 3.0 のとき train_eval.pick_threshold と同じ結果になる（テストで固定）。
    """
    ok = [r for r in table if r["false_positives_per_min"] <= max_fp_per_min]
    if ok:
        best = max(ok, key=lambda r: (r["detection_rate"], r["threshold"]))
        return best["threshold"], f"誤検出率が {max_fp_per_min} 回/分以下の候補のうち、検出率が最大（同率なら高いほう）"
    best = min(table, key=lambda r: (r["false_positives_per_min"], -r["threshold"]))
    return best["threshold"], (f"誤検出率が {max_fp_per_min} 回/分以下の候補が無いので、"
                               "誤検出率が最小（同率なら高いほう）")


# --- 採点 ---

@dataclass(frozen=True, eq=False)
class Scored:
    name: str
    cond: str
    t_start_s: np.ndarray        # (n,)
    valid: np.ndarray            # (n,) bool
    prob: np.ndarray             # (n,) swallow の確率
    argmax: np.ndarray           # (n,) 実行ファイルの labels の添字
    labels3: np.ndarray          # (n,) ei_upload.window_labels3（投入した窓の集合に使う）
    swallows: list               # s（秒）
    coughs: list                 # c（秒）
    t0_s: float
    duration_s: float

    @property
    def upload_mask(self) -> np.ndarray:
        return self.valid & (self.labels3 != ei_upload.UNUSED)

    @property
    def day(self) -> str:
        return self.name[:8]


def positive_windows(s: Scored, threshold: float) -> list:
    """valid かつ f32(p) >= f32(thr) の窓の開始時刻。無効な窓は陽性にしない。比較は float32（docs/decisions/0021）。"""
    thr = f32(threshold)
    p32 = np.asarray(s.prob, dtype=np.float64).astype(np.float32).astype(np.float64)
    return s.t_start_s[(p32 >= thr) & s.valid].tolist()


def _cond(name: str) -> str:
    return name.rsplit("_", 1)[-1]


def load_and_score(session_dir: Path, scorer: m2_scorer.Scorer) -> Scored:
    """セッション 1 本を読み、全窓（valid を問わず）を実行ファイルに流す。収集日4 の imu.csv / audio.wav / audio_chunks.csv / events.csv を開く。"""
    session_dir = Path(session_dir)
    f = features_m2.extract_session(session_dir)
    swallows = evaluate.load_events(session_dir, "s")
    coughs = evaluate.load_events(session_dir, "c")
    t0_s, duration_s = evaluate.session_span(session_dir)
    probs = scorer.score_rows(f.X.tolist())
    labels3 = ei_upload.window_labels3(f.t_start_s, swallows, coughs)
    return Scored(name=session_dir.name, cond=_cond(session_dir.name), t_start_s=np.asarray(f.t_start_s, dtype=np.float64),
                  valid=np.asarray(f.valid, dtype=bool), prob=probs[:, scorer.swallow_index],
                  argmax=np.argmax(probs, axis=1) if len(probs) else np.zeros(0, dtype=int),
                  labels3=labels3, swallows=swallows, coughs=coughs, t0_s=t0_s, duration_s=duration_s)


def session_metrics(s: Scored, threshold: float) -> dict:
    return evaluate.event_metrics(s.swallows, positive_windows(s, threshold), s.duration_s, s.t0_s)


def threshold_table(scored: Sequence[Scored]) -> list[dict]:
    names = ", ".join(s.name for s in scored)
    rows = []
    for thr in THRESHOLDS:
        m = train_eval.sum_metrics({s.name: session_metrics(s, thr) for s in scored})
        if m["swallows"] == 0:
            raise SystemExit(f"合算で嚥下が 0 件です（検出率が出ません）。セッション: {names}")
        if m["false_positives_per_min"] is None:
            raise SystemExit(f"合算で非嚥下の時間が 0 です（誤検出率が出ません）。セッション: {names}")
        rows.append({"threshold": thr, **m})
    return rows


def cough_linked(s: Scored, threshold: float) -> dict:
    """c に紐づく誤検出の塊（evaluate_detector.cough_linked と同じ定義。塊は evaluate.event_metrics と同じ間隔）。"""
    fp_runs = [run for run in evaluate_detector._runs(positive_windows(s, threshold))
               if any(not any(evaluate._hits(w, t) for t in s.swallows) for w in run)]
    linked_c: set = set()
    n_linked = 0
    for run in fp_runs:
        center = run[0] + evaluate.WINDOW_S / 2
        near = [(abs(center - tc), i) for i, tc in enumerate(s.coughs) if COUGH_LINK_S[0] <= center - tc <= COUGH_LINK_S[1]]
        if near:
            n_linked += 1
            linked_c.add(min(near)[1])
    return {"coughs": len(s.coughs), "fp_runs": len(fp_runs), "linked_runs": n_linked, "coughs_with_run": len(linked_c)}


def reference(scored: Sequence[Scored], threshold: float) -> dict:
    """常時陽性（全窓を陽性）、陽性窓の割合、c に紐づく誤検出の塊、無効な窓の数。"""
    always = train_eval.sum_metrics({s.name: evaluate.event_metrics(s.swallows, s.t_start_s.tolist(), s.duration_s, s.t0_s)
                                     for s in scored})
    n_pos = sum(len(positive_windows(s, threshold)) for s in scored)
    n_all = sum(len(s.prob) for s in scored)
    n_invalid = sum(int((~s.valid).sum()) for s in scored)
    link = {s.name: cough_linked(s, threshold) for s in scored}
    return {"always_positive": always, "positive_windows": n_pos, "windows": n_all,
            "positive_ratio": n_pos / n_all if n_all else None, "invalid_windows": n_invalid, "cough_linked": link}


def window_confusion(scored: Sequence[Scored], labels: Sequence[str]) -> dict:
    """投入した窓（valid かつ UNUSED でない）の argmax × ラベルの 3 × 3（行 = 正解、列 = argmax。CLASS_ORDER の順）と accuracy。"""
    m = np.zeros((len(CLASS_ORDER), len(CLASS_ORDER)), dtype=int)
    col = {name: CLASS_ORDER.index(name) for name in labels if name in CLASS_ORDER}
    n = 0
    for s in scored:
        for k in np.nonzero(s.upload_mask)[0]:
            true = LABEL3_NAME[int(s.labels3[k])]
            pred = labels[int(s.argmax[k])]
            if pred not in col:
                raise SystemExit(f"実行ファイルの labels に {CLASS_ORDER} 以外があります: {pred!r}")
            m[CLASS_ORDER.index(true), col[pred]] += 1
            n += 1
    return {"windows": n, "matrix": m.tolist(), "accuracy": int(np.trace(m)) / n if n else None,
            "per_class": {c: int(m[i].sum()) for i, c in enumerate(CLASS_ORDER)}}


# --- 凍結 ---

def render_threshold_header(threshold: float, commit: str, deploy_version: int, today: str, rule: str) -> str:
    thr32 = f32(threshold)
    lines = [
        "// firmware/detector/m2_threshold.h — M2 の凍結した閾値。analysis/m2_threshold.py --freeze が生成する。手で編集しない。",
        f"// 決めた日 {today}、コミット {commit}、EI の deploy version {deploy_version}。",
        "// 収集日4（検証側）で決めた。#27 の記録が終わるまで、この閾値もモデルも変えない（Issue #22、docs/decisions/0019）。",
        f"// 決め方: 候補 {THRESHOLDS[0]:.2f}〜{THRESHOLDS[-1]:.2f}（{THRESHOLDS[1] - THRESHOLDS[0]:.2f} 刻み）。{rule}。",
        f"// float32 では {thr32:.9g}（%.9g）。装置は float32 の確率と float32 のこの値を比べる。META の threshold にはこの 9 桁の値を書く（docs/decisions/0021）。",
        f"#ifndef {GUARD}",
        f"#define {GUARD}",
        "",
        f"static const float M2_THRESHOLD = {threshold!r}f;",
        f"#define M2_THRESHOLD_FP_PER_MIN_LIMIT {FP_PER_MIN_LIMIT!r}",
        "",
        f"#endif  // {GUARD}",
        "",
    ]
    return "\n".join(lines)


def freeze(text: str, header_path: Path) -> bool:
    """ヘッダを書く。既にあって同じなら書かない（False）。違えば止まる。"""
    header_path = Path(header_path)
    if header_path.is_file():
        if header_path.read_text(encoding="utf-8") != text:
            raise SystemExit(f"{header_path} が既にあり、内容が違います（黙って上書きしない。作り直すときは人が消す）")
        return False
    header_path.parent.mkdir(parents=True, exist_ok=True)
    header_path.write_text(text, encoding="utf-8")
    return True


# --- 実行 ---

@dataclass(frozen=True, eq=False)
class Result:
    argv: list
    root: Path
    freeze: bool
    commit: str
    norm: dict
    scorer_model: tuple
    scorer_labels: tuple
    eval_names: list
    scored: list
    table: list
    threshold: float
    rule: str
    per_session: dict
    total: dict
    reference: dict
    window: dict
    header_text: str | None
    header_written: bool | None
    header_path: Path


def run(root: Path, freeze_: bool = False, *, argv: Sequence[str] = (), scorer_bin: Path | str | None = None,
        norm_path: Path = NORM_PATH, norm_header_path: Path = NORM_HEADER_PATH,
        threshold_header_path: Path = THRESHOLD_HEADER_PATH, git_dirty: Callable[[], bool] = train_eval.git_dirty,
        today: str | None = None) -> Result:
    root = Path(root)
    if not root.is_dir():
        raise SystemExit(f"フォルダが見つかりません: {root}")
    if freeze_ and git_dirty():
        raise SystemExit("追跡しているファイルに未コミットの変更があります。--freeze はコミット済みの状態でだけ実行する（先にコミットする）")

    # 1. 分割（収集日1〜3 は meta.json しか開かない）
    _, eval_names = ei_upload.check_split(root)

    # 2. 正規化の定数と、ヘッダとの一致
    norm = ei_upload.read_norm(Path(norm_path))
    bad = [n for n in norm["stats_sessions"] if ei_upload._day(n) in ei_upload.TEST_DAYS]
    if bad:
        raise SystemExit(f"{norm_path} の stats_sessions に収集日4 のセッションがあります: {bad}")
    norm_header_path = Path(norm_header_path)
    if not norm_header_path.is_file():
        raise SystemExit(f"{norm_header_path} がありません（python analysis/m2_norm_header.py で生成する）")
    diff = m2_norm_header.differences(norm, norm_header_path.read_text(encoding="utf-8"))
    if diff:
        raise SystemExit(f"{norm_header_path} が {norm_path} と一致しません（実行ファイルはヘッダの定数で正規化する）:\n"
                         + "\n".join(f"- {d}" for d in diff))

    # 3. 実行ファイル
    scorer = m2_scorer.Scorer(scorer_bin, norm_path=Path(norm_path))
    if not {"swallow", "cough", "other"} <= set(scorer.labels):
        raise SystemExit(f"実行ファイルの labels に swallow / cough / other がそろっていません: {scorer.labels}")

    # 4. 収集日4 の 7 本を採点
    scored = [load_and_score(root / n, scorer) for n in eval_names]

    # 5〜6. 閾値の表と規則
    table = threshold_table(scored)
    threshold, rule = pick_threshold(table, FP_PER_MIN_LIMIT)

    # 7. 採用した閾値で合算
    per_session = {s.name: session_metrics(s, threshold) for s in scored}
    if list(per_session) != list(eval_names):
        raise AssertionError("aggregate に渡すセッションが eval の一覧と一致しません")
    total = evaluate.aggregate(per_session)
    if total["sessions"] != list(eval_names):
        raise AssertionError("aggregate のセッション一覧が eval の一覧と一致しません")

    # 8〜9. 参考値と窓単位
    ref = reference(scored, threshold)
    window = window_confusion(scored, scorer.labels)

    # 凍結
    commit = train_eval.git_commit()
    header_text = header_written = None
    if freeze_:
        header_text = render_threshold_header(threshold, commit, scorer.deploy_version,
                                              today or date.today().isoformat(), rule)
        header_written = freeze(header_text, Path(threshold_header_path))
    return Result(argv=list(argv), root=root, freeze=freeze_, commit=commit, norm=norm, scorer_model=scorer.model,
                  scorer_labels=scorer.labels, eval_names=list(eval_names), scored=scored, table=table, threshold=threshold,
                  rule=rule, per_session=per_session, total=total, reference=ref, window=window, header_text=header_text,
                  header_written=header_written, header_path=Path(threshold_header_path))


# --- 報告 ---

def _rate(v) -> str:
    return "—" if v is None else f"{100 * v:.1f}%"


def _per_min(v) -> str:
    return "—" if v is None else f"{v:.2f} 回/分"


def _meets(ok: bool | None) -> str:
    return "—" if ok is None else ("満たす" if ok else "満たさない")


def format_report(r: Result) -> str:
    out = ["# M2 検証側（収集日4）の採点と閾値（参考値。M2 の合格線の数字は #27 で出す）", ""]
    out += ["## 実行の条件",
            f"- コマンドライン: `m2_threshold.py {' '.join(r.argv)}`",
            f"- コミット: {r.commit}",
            f"- numpy: {np.__version__}",
            f"- 実行ファイルの model: deploy version {r.scorer_model[1]}（`model_metadata.h` の値のとおり）、labels {' / '.join(r.scorer_labels)}",
            f"- 正規化の定数: `m2_norm.json`（commit {r.norm.get('commit', 'unknown')}、stats_sessions {len(r.norm['stats_sessions'])} 本、"
            f"valid な窓 {int(r.norm['n_windows'])}、収集日4 を含まない）。`m2_norm.h` と一致",
            f"- 閾値の候補: {THRESHOLDS[0]:.2f}〜{THRESHOLDS[-1]:.2f}（{THRESHOLDS[1] - THRESHOLDS[0]:.2f} 刻み）",
            f"- 規則: 誤検出率が {FP_PER_MIN_LIMIT} 回/分以下の候補のうち検出率が最大（同率なら高いほう）。無ければ誤検出率が最小（同率なら高いほう）",
            "- 数え方: `evaluate.event_metrics`（イベント単位）。無効な窓は陽性にしない。確率と閾値の比較は float32",
            ""]
    out += [f"## 評価に使ったセッション（収集日4、{len(r.scored)} 本）",
            "| セッション | cond | 嚥下 | c | 全窓 | valid | 無効 | 投入した窓 |", "|---|---|---|---|---|---|---|---|"]
    for s in r.scored:
        out.append(f"| {s.name} | {s.cond} | {len(s.swallows)} | {len(s.coughs)} | {len(s.prob)} | {int(s.valid.sum())} | "
                   f"{int((~s.valid).sum())} | {int(s.upload_mask.sum())} |")
    out.append("")
    out += ["## 閾値の候補（合算）", "| 閾値 | 嚥下 | 検出 | 検出率 | 誤検出 | 非嚥下（分） | 誤検出率 |", "|---|---|---|---|---|---|---|"]
    for row in r.table:
        mark = " ←" if row["threshold"] == r.threshold else ""
        out.append(f"| {row['threshold']:.2f}{mark} | {row['swallows']} | {row['detected']} | {_rate(row['detection_rate'])} | "
                   f"{row['false_positive_runs']} | {row['non_swallow_min']:.2f} | {_per_min(row['false_positives_per_min'])} |")
    out.append("")
    out += ["## 採用した閾値", f"- {r.threshold:.2f}（float32 では {f32(r.threshold):.9g}。META の threshold にはこの 9 桁の値を書く）",
            f"- 理由: {r.rule}", ""]
    out += ["## 採用した閾値でのセッションごとの数字", "| セッション | cond | 嚥下 | 検出 | 検出率 | 誤検出 | 非嚥下（分） | 誤検出率 |",
            "|---|---|---|---|---|---|---|---|"]
    for s in r.scored:
        m = r.per_session[s.name]
        out.append(f"| {s.name} | {s.cond} | {m['swallows']} | {m['detected']} | {_rate(m['detection_rate'])} | "
                   f"{m['false_positive_runs']} | {m['non_swallow_min']:.2f} | {_per_min(m['false_positives_per_min'])} |")
    out.append("")
    t = r.total
    c = t["confusion"]
    out += ["## 合算（`evaluate.aggregate`、収集日4 の 7 本）",
            f"- 検出率: {t['detected']} / {t['swallows']} = {_rate(t['detection_rate'])}",
            f"- 誤検出率: {t['false_positive_runs']} 回 / {t['non_swallow_min']:.2f} 分 = {_per_min(t['false_positives_per_min'])}",
            f"- 混同行列（イベント単位）: TP {c['tp']}、FN {c['fn']}、FP {c['fp']}（TN は定義しない）",
            f"- 合格線との比較（参考。M2 の合格線の数字は #27）: 検出率 {PASS_DETECTION_RATE:.0%} 以上 "
            f"{_meets(None if t['detection_rate'] is None else t['detection_rate'] >= PASS_DETECTION_RATE)}、"
            f"誤検出 {FP_PER_MIN_LIMIT} 回/分以下 "
            f"{_meets(None if t['false_positives_per_min'] is None else t['false_positives_per_min'] <= FP_PER_MIN_LIMIT)}",
            ""]
    ref = r.reference
    a = ref["always_positive"]
    linked = ref["cough_linked"]
    out += ["## 参考値",
            f"- 常時陽性の場合（全窓を陽性）: 検出率 {_rate(a['detection_rate'])}、誤検出率 {_per_min(a['false_positives_per_min'])}",
            f"- 陽性窓の割合: {ref['positive_windows']} / {ref['windows']} = {_rate(ref['positive_ratio'])}",
            f"- 無効な窓: {ref['invalid_windows']}",
            f"- c に紐づく誤検出の塊（最初の窓の中心 − t_c が [{COUGH_LINK_S[0]:+.1f}, {COUGH_LINK_S[1]:+.1f}] 秒）: "
            f"{sum(v['linked_runs'] for v in linked.values())} / 誤検出の塊 {sum(v['fp_runs'] for v in linked.values())}、"
            f"c {sum(v['coughs'] for v in linked.values())} 回のうち塊が付いたもの {sum(v['coughs_with_run'] for v in linked.values())}",
            ""]
    w = r.window
    out += ["## 窓単位（EI の Model testing との突き合わせ用。投入した窓 = valid かつ境目でない窓）",
            f"- 窓の数: {w['windows']}（" + "、".join(f"{k} {v}" for k, v in w["per_class"].items()) + "）",
            f"- accuracy（argmax）: {_rate(w['accuracy'])}",
            "| 正解 \\ argmax | " + " | ".join(CLASS_ORDER) + " |", "|---|" + "---|" * len(CLASS_ORDER)]
    for i, name in enumerate(CLASS_ORDER):
        out.append(f"| {name} | " + " | ".join(str(v) for v in w["matrix"][i]) + " |")
    out.append("")
    out += ["## 凍結"]
    if not r.freeze:
        out.append(f"- --freeze 無し。{r.header_path.name} は書かない")
    else:
        out.append(f"- {'書いた' if r.header_written else '既にあり同じ内容（書かない）'}: {r.header_path}")
        out.append("```c")
        out.append(r.header_text.rstrip("\n"))
        out.append("```")
    out.append("")
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else list(argv)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", type=Path, help="data/raw")
    ap.add_argument("--freeze", action="store_true", help="firmware/detector/m2_threshold.h を書く（コミット済みの状態でだけ）")
    a = ap.parse_args(argv)
    print(format_report(run(a.root, a.freeze, argv=argv)))


if __name__ == "__main__":
    main()
