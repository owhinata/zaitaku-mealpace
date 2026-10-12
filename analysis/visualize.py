"""咽喉マイクの学習側の交差検証の結果を図にする（Issue #43、docs/decisions/0033）。

描くのは学習側のセッションだけ（split_sessions の train に無い名前、subject が
self でない名前は止まる）。確率は交差検証の検証側の確率（そのセッションを学習に
使っていない fold のモデル）。PNG は docs/log/ の下だけに書く（外のパスは止まる）。
確率・特徴量をファイルに書かない。

評価側の図（#40、docs/decisions/0034）は analysis/final_eval.py からだけ
draw(..., side="eval") で描く（評価側の名前だけを受け付ける。CLI には無い）。
確率は final_eval.py の 1 回だけの採点の確率で、ここでは採点し直さない。
評価側 2 点の比較の図は fig_final。

図の 3 種類:
  1. セッションごと: 窓平均 RMS、窓平均メル（0.25 秒ごと）、マーカー、窓ごとの
     陽性確率、後処理後の陽性、検出／見逃し／誤検出。窓ごとにまとめた値だけで描く。
  2. イベントごと: s の前後 2 秒（4 秒）の 20 ms の包絡とフレームごとのメル。
     検出・見逃し・誤検出から各 MAX_EVENTS_PER_KIND 件（時刻順の先頭から）、
     1 枚に EVENTS_PER_FIG 件まで。
  3. 組の比較: 誤検出/分 × 検出率（学習側 CV の各行）と合格線。

音声の切り出しは 1 枚 10 秒以内（CLAUDE.md、0033 の 6）: 波形と 25 ms 以下の時間
分解能の表示（フレームごとのメル、20 ms の包絡）を数え、1 枚の合計が FINE_LIMIT_S
を超えたら止まる。窓（0.25 秒）ごとにまとめた値は数えない。

使い方（単体。組と後処理を受け、交差検証をやり直して描く）:
  python analysis/visualize.py <root> --eval-min <N> --set <組> --post none|hyst
      --out docs/log/<日付>-43 [--sessions <名前> ...]
"""

from __future__ import annotations
import argparse
from pathlib import Path
from typing import Sequence

import numpy as np

import evaluate, features, features_v2, fixed_eval, split, train_eval

LOG_ROOT = Path(__file__).resolve().parent.parent / "docs" / "log"
FINE_LIMIT_S = 10.0
EVENT_HALF_S = 2.0
MAX_EVENTS_PER_KIND = 4
EVENTS_PER_FIG = 2
DPI = 100
MARKER_LABELS = ("s", "t", "c", "n")
JP_FONTS = ("Noto Sans CJK JP", "IPAexGothic", "IPAGothic", "PlemolJP")
COLORS = {
    "検出": "#1b7837",
    "見逃し": "#d6604d",
    "誤検出": "#7b3294",
    "陽性確率": "#2166ac",
}


def _plt():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    have = {f.name for f in font_manager.fontManager.ttflist}
    for name in JP_FONTS:
        if name in have:
            plt.rcParams["font.family"] = name
            break
    return plt


class FineBudget:
    """1 枚の細かい表示（波形、25 ms 以下の時間分解能）の秒数の合計。"""

    def __init__(self, limit_s: float = FINE_LIMIT_S):
        self.limit_s = limit_s
        self.total_s = 0.0

    def add(self, seconds: float) -> None:
        if self.total_s + seconds > self.limit_s + 1e-9:
            raise ValueError(
                f"1 枚の音声の切り出しが {self.limit_s} 秒を超えます"
                f"（{self.total_s + seconds:.2f} 秒）"
            )
        self.total_s += seconds


# --- 検査 ---


def check_out_dir(out: Path) -> Path:
    """docs/log/ の下のディレクトリだけを受け付ける。"""
    p = Path(out).resolve()
    root = LOG_ROOT.resolve()
    if p == root or root not in p.parents:
        raise SystemExit(
            f"図の出力先は docs/log/ の下のディレクトリだけです: {out}"
        )
    return p


def _split(root: Path, eval_min: int) -> dict:
    parts = split.split_sessions(
        root, eval_min, subject=train_eval.SUBJECT, kind="throat"
    )
    train_eval.check_collection_days(
        parts["train"], parts["eval"], train_eval.THROAT_EVAL_DAYS
    )
    return parts


def check_eval_sessions(
    root: Path, eval_min: int, names: Sequence[str]
) -> dict:
    """名前がすべて評価側（subject self）にあり、学習側の名前が無いこと
    （#40 の評価側の図。final_eval.py からだけ使う）。"""
    parts = _split(root, eval_min)
    for n in names:
        if f"_{train_eval.SUBJECT}_" not in n:
            raise SystemExit(
                f"subject が {train_eval.SUBJECT} でないセッションは描きません: {n}"
            )
        if n in parts["train"]:
            raise SystemExit(f"学習側のセッションは評価側の図に描きません: {n}")
        if n not in parts["eval"]:
            raise SystemExit(f"評価側のセッションではありません: {n}")
    return parts


def check_sessions(root: Path, eval_min: int, names: Sequence[str]) -> dict:
    """名前がすべて学習側（subject self）にあること。0034 の評価側の 7 本が
    学習側に入る分割では止まる。"""
    parts = _split(root, eval_min)
    fixed_eval.assert_fixed_eval_not_in(parts["train"])
    for n in names:
        if f"_{train_eval.SUBJECT}_" not in n:
            raise SystemExit(
                f"subject が {train_eval.SUBJECT} でないセッションは描きません: {n}"
            )
        if n in parts["eval"]:
            raise SystemExit(
                f"評価側のセッションは描きません（#40 で作る）: {n}"
            )
        if n not in parts["train"]:
            raise SystemExit(f"学習側のセッションではありません: {n}")
    return parts


# --- 集計 ---


def classify(swallows: Sequence[float], pos_t: Sequence[float]) -> dict:
    """evaluate.event_metrics と同じ数え方で、検出・見逃しの s と誤検出の塊。"""
    pos = sorted(set(pos_t))
    det = [t for t in swallows if any(evaluate._hits(w, t) for w in pos)]
    miss = [t for t in swallows if t not in det]
    runs: list[list[float]] = []
    for w in pos:
        if runs and w - runs[-1][-1] <= evaluate.HOP_S * 1.5:
            runs[-1].append(w)
        else:
            runs.append([w])
    fp = [
        (run[0], run[-1] + evaluate.WINDOW_S)
        for run in runs
        if any(not any(evaluate._hits(w, t) for t in swallows) for w in run)
    ]
    return {"検出": det, "見逃し": miss, "誤検出": fp}


def cell_summary(x: np.ndarray, sample_t: np.ndarray, t0: float, t1: float):
    """0.25 秒ごとの窓平均 RMS と窓平均メル（40 本）。(セルの開始時刻, rms, mel)。"""
    hop = evaluate.HOP_S
    n = int(np.floor((t1 - t0) / hop))
    edges = np.searchsorted(sample_t[:-1], t0 + hop * np.arange(n + 1))
    rms = np.zeros(n)
    mel = np.zeros((n, features_v2.N_MEL_CNN))
    for k in range(n):
        seg = x[edges[k] : edges[k + 1]].astype(np.float64)
        if len(seg):
            rms[k] = np.sqrt(np.mean((seg - seg.mean()) ** 2))
        lm = features_v2.logmel_frames(seg)
        if len(lm):
            mel[k] = lm.mean(axis=0)
    return t0 + hop * np.arange(n), rms, mel


def _markers(session: Path) -> dict:
    return {lb: evaluate.load_events(session, lb) for lb in MARKER_LABELS}


# --- 図 ---


def fig_session(path: Path, title: str, ses: dict) -> None:
    """図 1。ses: t0, cells_t, rms, mel, markers, t_start, prob, mask, cls。"""
    plt = _plt()
    t0 = ses["t0"]
    fig, ax = plt.subplots(
        4,
        1,
        figsize=(16, 9),
        sharex=True,
        gridspec_kw={"height_ratios": [1, 2, 1.4, 0.8]},
    )
    ct = ses["cells_t"] - t0
    ax[0].plot(ct, ses["rms"], color="0.3", lw=0.8)
    ax[0].set_ylabel("窓平均 RMS")
    if len(ct):
        ax[1].imshow(
            ses["mel"].T,
            origin="lower",
            aspect="auto",
            extent=(ct[0], ct[-1] + evaluate.HOP_S, 0, ses["mel"].shape[1]),
            cmap="magma",
        )
    ax[1].set_ylabel("窓平均メル（0.25 秒ごと）")
    c = ses["t_start"] + evaluate.WINDOW_S / 2 - t0
    ax[2].plot(
        c, ses["prob"], color=COLORS["陽性確率"], lw=0.8, label="陽性確率"
    )
    ax[2].fill_between(
        c,
        0,
        1,
        where=ses["mask"],
        color=COLORS["陽性確率"],
        alpha=0.15,
        step="mid",
        label="後処理後の陽性",
    )
    ax[2].set_ylim(0, 1)
    ax[2].set_ylabel("陽性確率")
    ax[2].legend(loc="upper right", fontsize=8)
    for k, lb in enumerate(MARKER_LABELS):
        for t in ses["markers"][lb]:
            ax[3].plot([t - t0], [k], "|", color="0.2", ms=10)
    for t in ses["cls"]["検出"]:
        ax[3].plot([t - t0], [0], "o", color=COLORS["検出"], ms=6)
    for t in ses["cls"]["見逃し"]:
        ax[3].plot([t - t0], [0], "x", color=COLORS["見逃し"], ms=8, mew=2)
    for a, b in ses["cls"]["誤検出"]:
        for axx in (ax[2], ax[3]):
            axx.axvspan(a - t0, b - t0, color=COLORS["誤検出"], alpha=0.12)
    ax[3].set_yticks(range(len(MARKER_LABELS)), MARKER_LABELS)
    ax[3].set_ylim(-0.7, len(MARKER_LABELS) - 0.3)
    ax[3].set_ylabel("マーカー")
    ax[3].set_xlabel("o start からの秒")
    handles = [
        plt.Line2D(
            [], [], marker="o", ls="", color=COLORS["検出"], label="検出"
        ),
        plt.Line2D(
            [], [], marker="x", ls="", color=COLORS["見逃し"], label="見逃し"
        ),
        plt.Rectangle(
            (0, 0), 1, 1, color=COLORS["誤検出"], alpha=0.3, label="誤検出"
        ),
    ]
    ax[3].legend(handles=handles, loc="upper right", fontsize=8, ncol=3)
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(path, dpi=DPI)
    plt.close(fig)


def fig_events(path: Path, title: str, items: Sequence[dict]) -> None:
    """図 2。items の各要素: kind, session, center（秒）, x（波形 16 kHz、中心の
    前後）, x_t0（x の先頭の時刻）, t_start, prob, mask。1 枚の細かい表示の合計が
    FINE_LIMIT_S を超えたら止まる（描く前に数える）。"""
    budget = FineBudget()
    for it in items:
        budget.add(len(it["x"]) / features_v2.SR)
    plt = _plt()
    tc = features.throat_check_module()
    fig, axes = plt.subplots(
        3, len(items), figsize=(7 * len(items), 7), squeeze=False
    )
    for j, it in enumerate(items):
        x = np.asarray(it["x"], dtype=np.float64)
        tx = it["x_t0"] + np.arange(len(x)) / features_v2.SR - it["center"]
        env = tc.envelope(x, float(np.mean(x)) if len(x) else 0.0)
        axes[0, j].plot(tx, env, color="0.3", lw=0.8)
        axes[0, j].set_ylabel("20 ms の包絡")
        lm = features_v2.logmel_frames(x)
        if len(lm):
            ft0 = tx[0] if len(tx) else 0.0
            axes[1, j].imshow(
                lm.T,
                origin="lower",
                aspect="auto",
                extent=(
                    ft0,
                    ft0 + len(lm) * features_v2.FRAME_HOP / features_v2.SR,
                    0,
                    lm.shape[1],
                ),
                cmap="magma",
            )
        axes[1, j].set_ylabel("メル（フレームごと）")
        c = it["t_start"] + evaluate.WINDOW_S / 2 - it["center"]
        sel = np.abs(c) <= EVENT_HALF_S
        axes[2, j].plot(
            c[sel], it["prob"][sel], color=COLORS["陽性確率"], label="陽性確率"
        )
        axes[2, j].fill_between(
            c[sel],
            0,
            1,
            where=it["mask"][sel],
            color=COLORS["陽性確率"],
            alpha=0.15,
            step="mid",
        )
        axes[2, j].set_ylim(0, 1)
        axes[2, j].set_ylabel("陽性確率")
        axes[2, j].set_xlabel("中心からの秒")
        for a in axes[:, j]:
            a.axvline(0, color=COLORS[it["kind"]], lw=1)
            a.set_xlim(-EVENT_HALF_S, EVENT_HALF_S)
        axes[0, j].set_title(
            f"{it['kind']}: {it['session']}（o start から {it['center'] - it['t0']:.1f} 秒）",
            fontsize=9,
        )
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(path, dpi=DPI)
    plt.close(fig)


def fig_compare(path: Path, rows: Sequence, selected=None) -> None:
    """図 3。学習側 CV の各行（評価側の点は描かない）。"""
    plt = _plt()
    fig, ax = plt.subplots(figsize=(9, 6))
    markers = {"none": "o", "hyst": "^"}
    sets = list(dict.fromkeys(r.set for r in rows))
    cmap = plt.get_cmap("tab10")
    for r in rows:
        m = r.metrics
        x, y = m["false_positives_per_min"], m["detection_rate"]
        if x is None or y is None:
            continue
        col = cmap(sets.index(r.set))
        lab = f"{r.set}・{'なし' if r.post == 'none' else 'ヒステリシス'}"
        if r.classifier == "CNN":
            lab += "（CNN、参考）"
        if r is selected:
            lab += "（選択）"
            ax.scatter(
                [x], [y], marker="o", s=300, facecolors="none", edgecolors="k"
            )
        ax.scatter(
            [x],
            [y],
            marker=markers[r.post],
            s=60,
            facecolors="none" if r.classifier == "CNN" else col,
            edgecolors=col,
            label=lab,
        )
    ax.legend(fontsize=8, loc="center left", bbox_to_anchor=(1.01, 0.5))
    ax.axhline(train_eval.PASS_DETECTION_RATE, color="0.5", ls="--", lw=1)
    ax.axvline(train_eval.PASS_FP_PER_MIN, color="0.5", ls="--", lw=1)
    ax.set_xlabel("誤検出（回/分、学習側 CV）")
    ax.set_ylabel("検出率（学習側 CV）")
    ax.set_ylim(0, 1.02)
    ax.set_xlim(left=0)
    ax.set_title(
        "組の比較（学習側の交差検証。○ なし、△ ヒステリシス、白抜きは CNN の参考）",
        fontsize=10,
    )
    fig.tight_layout()
    fig.savefig(path, dpi=DPI)
    plt.close(fig)


def fig_final(path: Path, points: Sequence[dict]) -> None:
    """評価側の比較（#40）。points の各要素: label, fp_per_min,
    detection_rate。評価側の点と合格線の破線だけ（学習側 CV の点は描かない）。"""
    plt = _plt()
    fig, ax = plt.subplots(figsize=(9, 6))
    marks = ("o", "s", "^", "D")
    for k, p in enumerate(points):
        x, y = p["fp_per_min"], p["detection_rate"]
        if x is None or y is None:
            continue
        ax.scatter(
            [x], [y], marker=marks[k % len(marks)], s=80, label=p["label"]
        )
    ax.axhline(train_eval.PASS_DETECTION_RATE, color="0.5", ls="--", lw=1)
    ax.axvline(train_eval.PASS_FP_PER_MIN, color="0.5", ls="--", lw=1)
    ax.set_xlabel("誤検出（回/分、評価側）")
    ax.set_ylabel("検出率（評価側）")
    ax.set_ylim(0, 1.02)
    ax.set_xlim(left=0)
    ax.legend(fontsize=8, loc="lower right")
    ax.set_title(
        "評価側（#40、1 回だけの採点）。破線は合格線（70% / 3 回/分）",
        fontsize=10,
    )
    fig.tight_layout()
    fig.savefig(path, dpi=DPI)
    plt.close(fig)


# --- まとめて描く ---

SIDE_TITLE = {
    "train": "学習側 CV の検証側の確率",
    "eval": "評価側（#40、1 回だけの採点の確率）",
}


def draw(
    root: Path,
    eval_min: int,
    scored: Sequence,
    mask_list: Sequence,
    out_dir: Path,
    label: str,
    rows: Sequence = (),
    selected=None,
    side: str = "train",
    title_suffix: str | None = None,
) -> list:
    """scored（交差検証の検証側の確率）と mask から 3 種類の図を描く。
    side="eval"（final_eval.py からだけ）は評価側の名前だけを受け付け、
    出力先が既にあれば止まる（上書きしない）。"""
    if side not in SIDE_TITLE:
        raise ValueError(f"side は train / eval のどちらかです: {side!r}")
    out = check_out_dir(out_dir)
    root = Path(root)
    names = [s.data.name for s in scored]
    if side == "eval":
        check_eval_sessions(root, eval_min, names)
    else:
        check_sessions(root, eval_min, names)
    suffix = SIDE_TITLE[side] if title_suffix is None else title_suffix
    out.mkdir(parents=True, exist_ok=side == "train")
    paths = []
    events = {"検出": [], "見逃し": [], "誤検出": []}
    for s, m in zip(scored, mask_list):
        d = s.data
        p = root / d.name
        x, sample_t, _, _, _, t0, t1 = features._throat_windows(p)
        cells_t, rms, mel = cell_summary(x, sample_t, t0, t1)
        cls = classify(d.swallows, d.feats.t_start_s[m].tolist())
        path = out / f"session_{d.name}.png"
        fig_session(
            path,
            f"{d.name}（{label}、{suffix}）",
            dict(
                t0=t0,
                cells_t=cells_t,
                rms=rms,
                mel=mel,
                markers=_markers(p),
                t_start=d.feats.t_start_s,
                prob=s.prob,
                mask=m,
                cls=cls,
            ),
        )
        paths.append(path)
        for kind in events:
            for v in cls[kind]:
                if len(events[kind]) >= MAX_EVENTS_PER_KIND:
                    break
                center = v if kind != "誤検出" else (v[0] + v[1]) / 2
                a = int(
                    np.searchsorted(
                        sample_t[:-1], max(center - EVENT_HALF_S, t0)
                    )
                )
                b = int(
                    np.searchsorted(
                        sample_t[:-1], min(center + EVENT_HALF_S, t1)
                    )
                )
                events[kind].append(
                    dict(
                        kind=kind,
                        session=d.name,
                        center=center,
                        t0=t0,
                        x=x[a:b].copy(),
                        x_t0=float(sample_t[a]),
                        t_start=d.feats.t_start_s,
                        prob=s.prob,
                        mask=m,
                    )
                )
        del x
    items = events["検出"] + events["見逃し"] + events["誤検出"]
    for k in range(0, len(items), EVENTS_PER_FIG):
        path = out / f"events_{k // EVENTS_PER_FIG + 1:02d}.png"
        fig_events(
            path,
            # 学習側の題は #43 のまま
            f"イベントごと（{label}）"
            if side == "train"
            else f"イベントごと（{label}、{suffix}）",
            items[k : k + EVENTS_PER_FIG],
        )
        paths.append(path)
    if rows:
        path = out / "compare.png"
        fig_compare(path, rows, selected)
        paths.append(path)
    return paths


def draw_all(root: Path, eval_min: int, result, out_dir: Path) -> list:
    """compare.run の結果から、選んだ行の図と組の比較の図を描く。"""
    import compare

    r = result.selected
    scored = result.scored[r.set]
    label = f"{r.set}・{compare.POST_LABEL[r.post]}・{compare._op(r)}"
    return draw(
        root,
        eval_min,
        scored,
        compare.masks(scored, r.post, r.op),
        out_dir,
        label,
        result.rows,
        r,
    )


def main(argv: Sequence[str] | None = None) -> None:
    import compare

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("root", type=Path)
    ap.add_argument("--eval-min", type=int, required=True)
    ap.add_argument("--set", required=True, choices=compare.SIMPLE_ORDER)
    ap.add_argument("--post", required=True, choices=compare.POSTS)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument(
        "--sessions", nargs="*", help="学習側のセッション名（省くと全部）"
    )
    a = ap.parse_args(argv)
    check_out_dir(a.out)
    parts = check_sessions(a.root, a.eval_min, a.sessions or [])
    _, _, scored = compare.cv_scored(a.root, parts["train"], a.set)
    rows = compare.make_rows(a.set, scored)
    row = next(r for r in rows if r.post == a.post)
    if a.sessions:
        keep = [k for k, s in enumerate(scored) if s.data.name in a.sessions]
        scored = [scored[k] for k in keep]
    label = f"{a.set}・{compare.POST_LABEL[a.post]}・{compare._op(row)}"
    paths = draw(
        a.root,
        a.eval_min,
        scored,
        compare.masks(scored, a.post, row.op),
        a.out,
        label,
    )
    print(f"図: {len(paths)} 枚（{a.out}）")


if __name__ == "__main__":
    main()
