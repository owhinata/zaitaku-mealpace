#!/usr/bin/env python3
"""装置のセッション（fw = unoq-detector）の採否の材料を出す（Issue #47、plan #47 の
9 節。docs/decisions/0037 の 5 の捨てる条件（装置）1〜9）。

使い方: .venv/bin/python tools/unoq_session_check.py data/raw/<セッション>

読むもの: detect.csv と feat.csv の最初の 2 列（t_ms・window_t_ms）だけ（行の列の
数は数えるが、3 列目以降を数として読まない・出さない）、feat.csv のヘッダ（次元の
数）、detect_rx.csv、events.csv、meta.json。
出すもの: 1〜9 の「当たる / 当たらない」と数、ほかに窓の数・記録の長さ・止まり方・
時間・時計の診断値。採否は人が決める（自動では決めない）。
出さないもの: positive・prob・led・FEAT の値と、それらの集計。

条件 4（受信の遅れ）と時計のずれの許す幅は、評価用 meal の前に人が 0037 に追記
する規則を目安として当てる。追記の前（RX_DELAY_RULE・CLOCK_RULE が None）は値を
出すだけ。ファイルを書かない。
"""

from __future__ import annotations
import argparse, csv, json, statistics, sys
from pathlib import Path

import check_session
import record

MAX_GAP_MS = 1000  # evaluate.MAX_GAP_MS（docs/decisions/0021）
WINDOW_MS = 1000
LOSS_LIMIT = 0.01  # 条件 1
END_S_MS = 1500  # 条件 6
# 人が 0037 に追記する規則（評価用 meal の前）。決まるまで None（値を出すだけ）
RX_DELAY_RULE = None  # 例: ("delay_upper", "max", 1000.0)
CLOCK_RULE = None  # 例: {"anchor_spread_ms": 200.0, ...}
CONV = "conv"
WRONG_KEY = "wrong key"


def _first_two(path: Path) -> tuple[list[str], list[tuple[int, int]], set]:
    """(ヘッダ, (t_ms, window_t_ms) の並び, 行の列の数の集合)。3 列目以降は数と
    して読まない。"""
    with path.open(newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        header = next(r, [])
        rows, ncols = [], set()
        for i, row in enumerate(r, start=2):
            if not row:
                continue
            ncols.add(len(row))
            try:
                rows.append((int(row[0]), int(row[1])))
            except (ValueError, IndexError):
                # 値を例外の文に出さない（列がずれると prob などが先頭に来る）
                raise SystemExit(
                    f"{path.name} の {i} 行目の t_ms・window_t_ms が整数として読めません"
                    "（このセッションは使わない）"
                ) from None
    return header, rows, ncols


def _time_violations(rows) -> int:
    """evaluate_detector._check_times と同じ規則に当たる行の数（window_t_ms は
    狭義の単調増加、t_ms は単調非減少、どちらも隣との差が 1000 ms 以下）。"""
    bad = 0
    for (t0, w0), (t1, w1) in zip(rows, rows[1:]):
        if w1 <= w0 or w1 - w0 > MAX_GAP_MS:
            bad += 1
        if t1 < t0 or t1 - t0 > MAX_GAP_MS:
            bad += 1
    return bad


def _med_max(v):
    v = list(v)
    if not v:
        return None
    return round(statistics.median(v), 3), round(max(v), 3)


def _hit(flag: bool) -> str:
    return "当たる" if flag else "当たらない"


def analyze(d: Path) -> list[str]:
    fixed_eval, _, features_np = record.analysis_modules()
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    out = [f"セッション: {d.name}"]
    _, det, det_cols = _first_two(d / "detect.csv")
    has_feat = (d / "feat.csv").is_file()
    if has_feat:
        f_head, feat, feat_cols = _first_two(d / "feat.csv")
        f_dims = sum(1 for h in f_head[2:] if h.startswith("f"))
    else:
        feat, feat_cols, f_dims = [], set(), 0
    with (d / "detect_rx.csv").open(newline="", encoding="utf-8") as f:
        rx = [{k: int(v) for k, v in row.items()} for row in csv.DictReader(f)]
    with (d / "events.csv").open(newline="", encoding="utf-8") as f:
        ev = [
            (int(r["t_ms"]), r["label"], r.get("note") or "")
            for r in csv.DictReader(f)
        ]
    w = [x for _, x in det]
    t = [x for x, _ in det]
    if not det:
        return out + ["detect.csv に窓の行がありません（使わない）"]

    # 1. 窓の取りこぼし
    r1 = (
        check_session.analyze(w, 250.0)
        if len(w) > 1
        else {"lost": 0, "loss_rate": 0.0}
    )
    out.append(
        f"1. 窓の取りこぼし（window_t_ms の飛び）: {r1['lost']} 窓、"
        f"{100 * r1['loss_rate']:.2f}% → {_hit(r1['loss_rate'] >= LOSS_LIMIT)}"
    )
    # 2. 読めない行
    bad = meta.get("bad_lines")
    out.append(
        f"2. 読めない・形の違う行（meta.json の bad_lines）: {bad}"
        f" → {_hit(not isinstance(bad, int) or bad >= 1)}"
    )
    # 3. 逆行・飛び
    v3 = _time_violations(det) + _time_violations(feat)
    out.append(
        f"3. window_t_ms・t_ms の逆行・隣との差 {MAX_GAP_MS} ms 超: {v3} 箇所"
        f"（detect.csv・feat.csv）→ {_hit(v3 > 0)}"
    )
    # 4. 受信の遅れ
    off = meta.get("clock_offset_ms")
    rtt = (meta.get("ssh_rtt_ms") or {}).get("max_ms")
    if isinstance(off, (int, float)) and rx:
        meas, upper = record.rx_delays(
            [(r["window_t_ms"], r["rx_ms"]) for r in rx], off, rtt
        )
        mm, mu = _med_max(meas), _med_max(upper)
        txt = (
            f"delay_meas 中央値 {mm[0]}・最大 {mm[1]} ms / delay_upper "
            + (
                f"中央値 {mu[0]}・最大 {mu[1]} ms"
                if mu
                else "なし（往復時間が無い）"
            )
            + f"（ssh の往復時間の最大 {rtt} ms）"
        )
        if RX_DELAY_RULE is None:
            verdict = "目安の規則はまだ無い（0037 に人が追記する）。値だけ"
        else:
            which, stat, lim = RX_DELAY_RULE
            v = mm if which == "delay_meas" else mu
            v = None if v is None else v[0 if stat == "median" else 1]
            verdict = _hit(v is None or v > lim)
        out.append(f"4. 受信の遅れ: {txt} → {verdict}")
    else:
        out.append(
            "4. 受信の遅れ: 計算できない（detect_rx.csv か clock_offset_ms が無い）→ 当たる"
        )
    # 5. o start
    starts = [e for e in ev if e[1] == "o" and e[2].strip() == "start"]
    first_w = w[0]
    before = []
    if len(starts) == 1:
        before = [
            e for e in ev if e[1] in ("s", "t", "c") and e[0] < starts[0][0]
        ]
    early = len(starts) == 1 and starts[0][0] < first_w
    out.append(
        f"5. o start: {len(starts)} 個"
        + (
            f"、最初の窓より前 {'はい' if early else 'いいえ'}"
            if len(starts) == 1
            else ""
        )
        + f"、o start より前の s・t・c {len(before)} 個"
        f" → {_hit(len(starts) != 1 or early or bool(before))}"
    )
    # 6. 終わりの 1.5 秒以内の s
    t1 = max(max(t), max(w) + WINDOW_MS, max((x for x, _ in feat), default=0))
    late = [e for e in ev if e[1] == "s" and e[0] >= t1 - END_S_MS]
    out.append(
        f"6. 記録の範囲の終わり（{t1} ms）の {END_S_MS / 1000:g} 秒以内の s: "
        f"{len(late)} 個 → {_hit(bool(late))}"
    )
    # 7. 装置・構成・FEAT
    model = meta.get("model") or {}
    want_sha = record.m4_model_fields(fixed_eval.M4_MODEL)["sha256"]
    checks = {
        "fw = unoq-detector": meta.get("fw") == record.UNOQ_FW,
        "m4_config = 0038": meta.get("m4_config") == fixed_eval.M4_CONFIG,
        "model.sha256 = 0038": model.get("sha256") == want_sha,
        "FEAT あり": has_feat,
        "FEAT の次元 82": f_dims == 82 and feat_cols <= {84},
        "feature_names = 82 の並び": meta.get("feature_names")
        == list(features_np.FEAT82_NAMES),
        "detect.csv と feat.csv の窓の集合が同じ": set(w)
        == {x for _, x in feat},
        "rpc = false": meta.get("rpc") is False,
        "detect.csv の列の数 5": det_cols <= {5},
    }
    ng = [k for k, ok in checks.items() if not ok]
    out.append(
        "7. 装置・構成・FEAT: "
        + ("すべて合う" if not ng else "合わない: " + "、".join(ng))
        + f" → {_hit(bool(ng))}"
    )
    # 8. conv の綴り
    notes = [e[2] for e in ev if e[1] == "o"]
    conv = any(n == CONV for n in notes)
    odd = [n for n in notes if n != CONV and n.strip().lower().startswith(CONV)]
    out.append(
        f"8. conv の綴り違い: {len(odd)} 個（{', '.join(repr(n) for n in odd) or 'なし'}）、"
        f"会話 {'あり' if conv else 'なし'} → {_hit(bool(odd))}"
    )
    # 9. wrong key
    wk = []
    for i, e in enumerate(ev):
        if e[1] == "o" and WRONG_KEY in e[2].strip().lower():
            prev = next((p for p in reversed(ev[:i]) if p[1] != "o"), None)
            wk.append(
                f"{e[0]} ms（直前のマーカー "
                + (f"{prev[1]} {prev[0]} ms" if prev else "なし")
                + "）"
            )
    out.append(
        f"9. wrong key: {len(wk)} 個"
        + (f"（{'; '.join(wk)}）" if wk else "")
        + " → 人が s に関わるかを決める"
    )

    # ほか
    end = (meta.get("unoq_detector") or {}).get("end") or {}
    proc = meta.get("proc_ms") or {}
    gaps = meta.get("line_gaps") or {}
    e2e = _med_max(x - (y + WINDOW_MS) for x, y in det)
    counts = {lb: sum(1 for e in ev if e[1] == lb) for lb in ("s", "c", "t")}
    out += [
        f"窓の数 {len(w)}、記録の長さ {(t1 - first_w) / 1000:.1f} 秒、止まり方 {meta.get('stop')}"
        f"（検出器 {end.get('stop')}）",
        f"overrun の行 {end.get('overrun_lines')}、RPC の失敗 {end.get('rpc_errors')}",
        "proc_ms: "
        + (
            f"中央値 {proc.get('median')}・95 パーセンタイル {proc.get('p95')}・最大 {proc.get('max')}"
            if proc
            else "なし"
        ),
        f"e2e_ms（t_ms − (window_t_ms + 1000)）: 中央値 {e2e[0]}・最大 {e2e[1]} ms",
        f"時計: anchor_spread_ms {end.get('anchor_spread_ms')}、clock_drift_min_ms "
        f"{end.get('clock_drift_min_ms')}、clock_drift_max_ms {end.get('clock_drift_max_ms')}"
        + ("（許す幅の目安はまだ無い。値だけ）" if CLOCK_RULE is None else ""),
        f"marker_offset_shift_max_ms {meta.get('marker_offset_shift_max_ms')}、"
        f"ssh の往復時間の最大 {rtt} ms",
        f"行の途切れ {gaps.get('n')} 回（最長 {gaps.get('max_s')} 秒）",
        f"マーカー: s {counts['s']}、c {counts['c']}、t {counts['t']}",
    ]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("session", type=Path)
    a = ap.parse_args(argv)
    for ln in analyze(a.session):
        print(ln)
    return 0


if __name__ == "__main__":
    sys.exit(main())
