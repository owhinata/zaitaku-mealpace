"""検出器のセッション（detect.csv・events.csv）から、docs/evaluation.md の定義でイベント単位の数字を出す（#23）。

数え方は evaluate.event_metrics / evaluate.aggregate をそのまま使う（docs/decisions/0011）。検出器のセッションには imu.csv と
audio_chunks.csv が無いので、記録の範囲は detect.csv と feat.csv の時刻から取る（docs/decisions/0021。0011 の読み替え）。
positive だけを評価に使う。led と prob は使わない（docs/decisions/0018・0019）。

使い方:
  python analysis/evaluate_detector.py data/raw                        # 既定: self の meal で fw が detector のセッション全部
  python analysis/evaluate_detector.py data/raw --sessions A B C        # 明示したセッションだけ（3 セッション未満なら合算を出さない）
  python analysis/evaluate_detector.py data/raw --scorer path/to/m2_scorer.py   # feat.csv からの再採点と装置の出力の差（#22）

報告は標準出力に Markdown。ファイルは書かない。個々の窓の確率と特徴量の値は出さない。
未コミットの変更があってもコミットに -dirty を付けて止めない（#27 の M2 の数字はコミット済みの状態で走らせる）。
"""
from __future__ import annotations
import argparse, csv, importlib.util, json, struct, sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import evaluate, split
from train_eval import git_commit, sum_metrics

# --- 定数 ---
SUBJECT = "self"                  # docs/evaluation.md「M1・M2 の数字は self のみ」
COND = "meal"                     # docs/recording-protocol.md「M2 の通し記録」
DETECTOR_FW = split.DETECTOR_FW
WINDOW_MS = int(round(evaluate.WINDOW_S * 1000))
MAX_SEND_LAG_MS = evaluate.MAX_GAP_MS   # 送信の遅れ t_ms − (window_t_ms + 1000) の上限（docs/decisions/0021）
PASS_DETECTION_RATE = 0.85        # docs/evaluation.md の M2 の合格線
PASS_FP_PER_MIN = 1.0             # 同上
COUGH_LINK_S = (-1.0, 5.0)        # c に紐づく誤検出の塊: 最初の窓の中心 − t_c の範囲（docs/log/2026-09-24.md「#20」）
CONV_NOTE = "conv"                # 会話ありのセッションの印（o の note）
DETECT_HEADER = ["t_ms", "window_t_ms", "positive", "prob", "led"]


def f32(x: float) -> float:
    """float32 に丸める。装置は閾値を float32 の定数として持ち float32 の確率と比べるので、PC も同じ型で比べる。"""
    return struct.unpack("<f", struct.pack("<f", float(x)))[0]


@dataclass(frozen=True, eq=False)
class DetectRow:
    t_ms: int
    window_t_ms: int
    positive: int
    prob: float


@dataclass(frozen=True, eq=False)
class SessionData:
    name: str
    dir: Path
    cond: str
    meta: dict
    rows: list                    # DetectRow（detect.csv の順）
    has_feat: bool
    feat_missing: int             # DETECT にあって FEAT に無い窓の数（feat.csv が無ければ 0）
    dims: int | None              # feat.csv の f* の列数。無ければ meta の feature_names の長さ、それも無ければ None
    swallows: list                # s（秒）
    coughs: list                  # c（秒）
    conv: bool                    # o の note が conv の行があるか
    t0_s: float
    duration_s: float

    @property
    def day(self) -> str:
        return self.name[:8]

    def positive_windows(self) -> list:
        return [r.window_t_ms / 1000.0 for r in self.rows if r.positive == 1]

    def all_windows(self) -> list:
        return [r.window_t_ms / 1000.0 for r in self.rows]


# --- 読み込みと検査 ---

def _stop(session: str, msg: str):
    raise SystemExit(f"{session}: {msg}")


def _check_times(session: str, fname: str, t: Sequence[int], w: Sequence[int]) -> None:
    """window_t_ms は狭義に単調増加、t_ms は単調非減少、どちらも隣との差が MAX_GAP_MS 以下。送信の遅れは MAX_SEND_LAG_MS 以下。"""
    for i in range(1, len(w)):
        if w[i] <= w[i - 1] or w[i] - w[i - 1] > evaluate.MAX_GAP_MS:
            _stop(session, f"{fname} の window_t_ms が逆行したか {evaluate.MAX_GAP_MS} ms を超えて飛んでいます: "
                           f"{w[i - 1]} → {w[i]}")
        if t[i] < t[i - 1] or t[i] - t[i - 1] > evaluate.MAX_GAP_MS:
            _stop(session, f"{fname} の t_ms が逆行したか {evaluate.MAX_GAP_MS} ms を超えて飛んでいます: {t[i - 1]} → {t[i]}")
    for ti, wi in zip(t, w):
        if ti - (wi + WINDOW_MS) > MAX_SEND_LAG_MS:
            _stop(session, f"{fname} の送信の遅れ t_ms − (window_t_ms + {WINDOW_MS}) が {MAX_SEND_LAG_MS} ms を超えています: "
                           f"t_ms={ti} window_t_ms={wi}")


def _read_csv(path: Path) -> tuple:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        rows = [r for r in reader if r]
    return header, rows


def load_detect(session_dir: Path) -> list:
    name = session_dir.name
    path = session_dir / "detect.csv"
    if not path.is_file():
        _stop(name, "detect.csv がありません")
    header, raw = _read_csv(path)
    if header != DETECT_HEADER:
        _stop(name, f"detect.csv のヘッダが {DETECT_HEADER} ではありません: {header}")
    if not raw:
        _stop(name, "detect.csv に行がありません")
    rows = []
    for r in raw:
        if len(r) != len(DETECT_HEADER):
            _stop(name, f"detect.csv に列数の違う行があります: {r}")
        t_ms, window_t_ms, positive, prob, led = int(r[0]), int(r[1]), int(r[2]), float(r[3]), int(r[4])
        if positive not in (0, 1):
            _stop(name, f"detect.csv の positive が 0 / 1 ではありません: {positive}")
        if led not in (0, 1, 2):
            _stop(name, f"detect.csv の led が 0 / 1 / 2 ではありません: {led}")
        if not 0.0 <= prob <= 1.0:
            _stop(name, f"detect.csv の prob が [0, 1] の外です: {r[3]}")
        rows.append(DetectRow(t_ms=t_ms, window_t_ms=window_t_ms, positive=positive, prob=prob))
    _check_times(name, "detect.csv", [r.t_ms for r in rows], [r.window_t_ms for r in rows])
    return rows


def feat_header_dims(session_dir: Path) -> int | None:
    """feat.csv の f* の列数。feat.csv が無ければ None。ヘッダが t_ms, window_t_ms, f0..f(N−1) でなければ止まる。"""
    path = session_dir / "feat.csv"
    if not path.is_file():
        return None
    header, _ = _read_csv(path)
    n = len(header or []) - 2
    if header is None or n < 0 or header[:2] != ["t_ms", "window_t_ms"] or header[2:] != [f"f{i}" for i in range(n)]:
        _stop(session_dir.name, f"feat.csv のヘッダが t_ms, window_t_ms, f0..f(N−1) ではありません: {header}")
    return n


def load_feat_times(session_dir: Path) -> tuple:
    """feat.csv の (t_ms の列, window_t_ms の列)。特徴量の値は読まない。"""
    _, raw = _read_csv(session_dir / "feat.csv")
    t = [int(r[0]) for r in raw]
    w = [int(r[1]) for r in raw]
    _check_times(session_dir.name, "feat.csv", t, w)
    return t, w


def load_feat_values(session_dir: Path, n: int) -> tuple:
    """feat.csv の (window_t_ms の列, 特徴量の行の列)。--scorer のときだけ呼ぶ。"""
    _, raw = _read_csv(session_dir / "feat.csv")
    w, x = [], []
    for r in raw:
        if len(r) != 2 + n:
            _stop(session_dir.name, f"feat.csv に列数の違う行があります（{2 + n} 列が要る）: {len(r)} 列")
        w.append(int(r[1]))
        x.append([float(v) for v in r[2:]])
    return w, x


def load_conv(session_dir: Path) -> bool:
    with (session_dir / "events.csv").open(encoding="utf-8") as f:
        return any(row["label"] == "o" and row["note"].strip() == CONV_NOTE for row in csv.DictReader(f))


def load_session(session_dir: Path, meta: dict) -> SessionData:
    name = session_dir.name
    rows = load_detect(session_dir)
    detect_w = {r.window_t_ms for r in rows}
    t1_ms = max(max(r.t_ms for r in rows), max(r.window_t_ms for r in rows) + WINDOW_MS)
    dims = feat_header_dims(session_dir)
    has_feat = dims is not None
    feat_missing = 0
    if has_feat:
        feat_t, feat_w = load_feat_times(session_dir)
        extra = sorted(set(feat_w) - detect_w)
        if extra:
            # DETECT より長く続く FEAT で記録の範囲（誤検出率の分母）が伸びる経路を閉じる
            _stop(name, f"feat.csv に detect.csv に無い窓があります（{len(extra)} 窓。最初: window_t_ms={extra[0]}）")
        feat_missing = len(detect_w - set(feat_w))
        if feat_t:
            t1_ms = max(t1_ms, max(feat_t))
    else:
        names = meta.get("feature_names")
        dims = len(names) if isinstance(names, list) else None
    t0_s = rows[0].window_t_ms / 1000.0
    duration_s = t1_ms / 1000.0 - t0_s
    return SessionData(name=name, dir=session_dir, cond=name.rsplit("_", 1)[-1], meta=meta, rows=rows, has_feat=has_feat,
                       feat_missing=feat_missing, dims=dims,
                       swallows=evaluate.load_events(session_dir), coughs=evaluate.load_events(session_dir, "c"),
                       conv=load_conv(session_dir), t0_s=t0_s, duration_s=duration_s)


# --- 対象の選び方 ---

@dataclass(frozen=True, eq=False)
class Candidate:
    name: str
    subject: str
    cond: str
    meta: dict
    used: bool
    reason: str                   # 使わない理由（使うときは空）


def scan_root(root: Path) -> list:
    """root 直下の検出器のセッション（fw が detector）を名前順に返す。形式に合わないフォルダ・meta.json の不備は split.py と同じ理由で止まる。

    self の meal なのに fw が detector でないセッションがあれば止まる（docs/recording-protocol.md「fw が detector でないセッションは使わない」）。
    """
    found = []
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue
        if p.is_symlink():
            raise SystemExit(f"セッションのフォルダがシンボリックリンクです: {p.name}")
        m = split.NAME_RE.fullmatch(p.name)
        if m is None:
            raise SystemExit(f"フォルダ名が形式に合いません（消すか直す）: {p.name}")
        meta = split._meta(p)
        if meta["subject"] != m.group(1):
            raise SystemExit(f"フォルダ名と meta.json の subject が違います: {p.name}")
        subject, cond = m.group(1), m.group(2)
        if meta.get("fw") == DETECTOR_FW:
            found.append((p.name, subject, cond, meta))
        elif subject == SUBJECT and cond == COND:
            raise SystemExit(f"{COND} のセッションなのに meta.json の fw が {DETECTOR_FW} ではありません（fw={meta.get('fw')!r}。"
                             f"検出器で録ったことを示せないので消す）: {p.name}")
    return found


def select_sessions(root: Path, sessions: Sequence[str] | None) -> list:
    found = scan_root(root)
    names = [f[0] for f in found]
    if sessions is not None:
        if len(set(sessions)) != len(sessions):
            raise SystemExit(f"--sessions に同じセッションが 2 回あります: {list(sessions)}")
        for s in sessions:
            if s not in names:
                raise SystemExit(f"--sessions のセッションが root の検出器のセッション（fw が {DETECTOR_FW}）にありません: {s}")
    out = []
    for name, subject, cond, meta in found:
        if subject != SUBJECT:
            used, reason = False, f"subject が {SUBJECT} でない"
        elif sessions is None:
            used, reason = (cond == COND), ("" if cond == COND else f"cond が {COND} でない")
        else:
            used, reason = (name in sessions), ("" if name in sessions else "--sessions に無い")
        out.append(Candidate(name=name, subject=subject, cond=cond, meta=meta, used=used, reason=reason))
    if sessions is not None:
        for s in sessions:
            c = next(c for c in out if c.name == s)
            if not c.used:
                raise SystemExit(f"--sessions のセッションは評価に使えません（{c.reason}）: {s}")
    return out


def check_same_firmware(data: Sequence[SessionData]) -> None:
    """評価に使うセッションの threshold・model・feature_names が全部一致しなければ止まる（同じファームウェアで録る）。"""
    for d in data:
        thr = d.meta.get("threshold")
        if isinstance(thr, bool) or not isinstance(thr, (int, float)):
            _stop(d.name, f"meta.json の threshold が数値ではありません: {thr!r}")
    for key in ("threshold", "model", "feature_names"):
        groups: dict = {}
        for d in data:
            groups.setdefault(json.dumps(d.meta.get(key), sort_keys=True, ensure_ascii=False), []).append(d.name)
        if len(groups) > 1:
            listing = "; ".join(f"{k}: {', '.join(v)}" for k, v in groups.items())
            raise SystemExit(f"評価に使うセッションの meta.json の {key} が一致しません（同じファームウェアで録ったものだけを合算する）。"
                             f"{listing}")


# --- 数え方 ---

def session_metrics(d: SessionData, positive_windows: Sequence[float] | None = None) -> dict:
    windows = d.positive_windows() if positive_windows is None else list(positive_windows)
    try:
        return evaluate.event_metrics(d.swallows, windows, d.duration_s, d.t0_s)
    except ValueError as e:
        _stop(d.name, str(e))


def _runs(positive_windows: Sequence[float]) -> list:
    """evaluate.event_metrics と同じ塊の作り方（間隔 HOP_S × 1.5 以下）。"""
    runs: list = []
    for w in sorted(set(positive_windows)):
        if runs and w - runs[-1][-1] <= evaluate.HOP_S * 1.5:
            runs[-1].append(w)
        else:
            runs.append([w])
    return runs


def cough_linked(d: SessionData) -> dict:
    """誤検出の塊のうち、最初の窓の中心 − t_c が COUGH_LINK_S に入る c がある塊（複数なら最も近い 1 つ）を「c に紐づく」とする。"""
    fp_runs = [run for run in _runs(d.positive_windows())
               if any(not any(evaluate._hits(w, t) for t in d.swallows) for w in run)]
    linked_c: set = set()
    n_linked = 0
    for run in fp_runs:
        center = run[0] + evaluate.WINDOW_S / 2
        near = [(abs(center - tc), i) for i, tc in enumerate(d.coughs) if COUGH_LINK_S[0] <= center - tc <= COUGH_LINK_S[1]]
        if near:
            n_linked += 1
            linked_c.add(min(near)[1])
    return {"coughs": len(d.coughs), "fp_runs": len(fp_runs), "linked_runs": n_linked, "coughs_with_run": len(linked_c)}


def mismatch_count(d: SessionData) -> int:
    """positive != (prob >= threshold) の行数。比較は float32（docs/decisions/0021）。"""
    thr = f32(d.meta["threshold"])
    return sum(1 for r in d.rows if r.positive != int(f32(r.prob) >= thr))


def reference(data: Sequence[SessionData]) -> dict:
    always = sum_metrics({d.name: session_metrics(d, d.all_windows()) for d in data})
    n_pos = sum(len(d.positive_windows()) for d in data)
    n_all = sum(len(d.rows) for d in data)
    cough = {d.name: cough_linked(d) for d in data}
    return {
        "always_positive": always,
        "positive_windows": n_pos, "windows": n_all, "positive_ratio": n_pos / n_all if n_all else None,
        "mismatch": {d.name: mismatch_count(d) for d in data},
        "cough": {
            "coughs": sum(c["coughs"] for c in cough.values()),
            "fp_runs": sum(c["fp_runs"] for c in cough.values()),
            "linked_runs": sum(c["linked_runs"] for c in cough.values()),
            "coughs_with_run": sum(c["coughs_with_run"] for c in cough.values()),
        },
        "by_day": {day: sum_metrics({d.name: session_metrics(d) for d in data if d.day == day})
                   for day in sorted({d.day for d in data})},
        "by_conv": {label: sum_metrics({d.name: session_metrics(d) for d in data if d.conv == flag})
                    for label, flag in (("会話あり", True), ("会話なし", False)) if any(d.conv == flag for d in data)},
    }


# --- 再採点（--scorer） ---

def load_scorer(path: Path):
    spec = importlib.util.spec_from_file_location("m2_scorer", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"scorer を読めません: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "score", None)):
        raise SystemExit(f"scorer に score(features, meta) がありません: {path}")
    return module.score


def rescore(d: SessionData, score) -> dict:
    """feat.csv を scorer で再採点し、装置の positive との差を出す。閾値は meta.json の値（上書きしない）。"""
    if not d.has_feat:
        _stop(d.name, "feat.csv がありません（--scorer には要る）")
    n = d.dims
    names = d.meta.get("feature_names")
    if not isinstance(names, list) or len(names) != n:
        _stop(d.name, f"meta.json の feature_names の長さが feat.csv の次元数 {n} と違います: "
                      f"{len(names) if isinstance(names, list) else names!r}")
    w, x = load_feat_values(d.dir, n)
    detect_w = [r.window_t_ms for r in d.rows]
    if set(w) != set(detect_w):
        _stop(d.name, f"feat.csv と detect.csv の window_t_ms の集合が一致しません（再採点は全窓に付ける）: "
                      f"detect {len(set(detect_w))} 窓、feat {len(set(w))} 窓")
    order = sorted(range(len(w)), key=lambda i: w[i])
    probs = list(score([x[i] for i in order], d.meta))
    if len(probs) != len(order):
        _stop(d.name, f"scorer の戻り値の長さが窓の数と違います: {len(probs)} != {len(order)}")
    thr = f32(d.meta["threshold"])
    by_window = {r.window_t_ms: r for r in d.rows}
    dev1_pc0 = dev0_pc1 = 0
    max_abs = 0.0
    pc_positive = []
    for i, p in zip(order, probs):
        r = by_window[w[i]]
        pc = int(f32(p) >= thr)
        if r.positive == 1 and pc == 0:
            dev1_pc0 += 1
        elif r.positive == 0 and pc == 1:
            dev0_pc1 += 1
        max_abs = max(max_abs, abs(r.prob - float(p)))
        if pc:
            pc_positive.append(r.window_t_ms / 1000.0)
    return {"windows": len(order), "dev1_pc0": dev1_pc0, "dev0_pc1": dev0_pc1, "max_abs_diff": max_abs,
            "metrics": session_metrics(d, pc_positive)}


# --- 実行 ---

@dataclass(frozen=True, eq=False)
class Result:
    root: Path
    sessions_arg: list | None
    scorer: Path | None
    candidates: list
    used: list                    # SessionData（名前順）
    per_session: dict
    total: dict | None            # evaluate.aggregate の結果。3 セッション未満（--sessions）なら None
    reference: dict
    rescored: dict | None


def run(root: Path, sessions: Sequence[str] | None = None, scorer: Path | None = None) -> Result:
    root = Path(root)
    candidates = select_sessions(root, sessions)
    used_names = [c.name for c in candidates if c.used]
    data = [load_session(root / c.name, c.meta) for c in candidates if c.used]
    if not data:
        raise SystemExit(f"評価に使うセッションがありません（root: {root}）")
    check_same_firmware(data)
    per_session = {d.name: session_metrics(d) for d in data}
    if list(per_session) != used_names:
        raise AssertionError("aggregate に渡すセッションが評価に使う一覧と一致しません")
    total = None
    if sessions is None or len(per_session) >= evaluate.MIN_SESSIONS:
        try:
            total = evaluate.aggregate(per_session)
        except ValueError as e:
            raise SystemExit(f"{e}。--sessions で明示したときだけ、3 セッション未満でもセッションごとの数字を出す")
    rescored = None
    if scorer is not None:
        score = load_scorer(scorer)
        rescored = {d.name: rescore(d, score) for d in data}
    return Result(root=root, sessions_arg=list(sessions) if sessions is not None else None, scorer=scorer,
                  candidates=candidates, used=data, per_session=per_session, total=total,
                  reference=reference(data), rescored=rescored)


# --- 報告 ---

def _rate(v) -> str:
    return "—" if v is None else f"{v:.3f}"


def _per_min(v) -> str:
    return "—" if v is None else f"{v:.2f}"


def _meets(ok: bool | None) -> str:
    return "出ない" if ok is None else ("満たす" if ok else "満たさない")


def _sum_line(m: dict) -> str:
    return (f"検出率 {_rate(m['detection_rate'])}（{m['detected']}/{m['swallows']}）、誤検出 {m['false_positive_runs']} 回、"
            f"誤検出率 {_per_min(m['false_positives_per_min'])} 回/分（非嚥下 {m['non_swallow_min']:.2f} 分）")


def format_report(r: Result) -> str:
    """Markdown の報告。個々の窓の確率と特徴量の値は入れない。"""
    d0 = r.used[0]
    n_used = len(r.used)
    out = ["# M2 検出器の評価（detect.csv の positive と events.csv の s）", ""]
    args = [str(r.root)]
    if r.sessions_arg is not None:
        args += ["--sessions", *r.sessions_arg]
    if r.scorer is not None:
        args += ["--scorer", str(r.scorer)]
    out += ["## 実行の条件", "",
            f"- 実行: `python analysis/evaluate_detector.py {' '.join(args)}`",
            f"- 対象: {'--sessions で明示したセッション' if r.sessions_arg is not None else f'既定（subject `{SUBJECT}`、cond `{COND}`、fw `{DETECTOR_FW}` の全部）'}",
            f"- コミット: {git_commit()}",
            f"- 窓 {evaluate.WINDOW_S} 秒 / ホップ {evaluate.HOP_S} 秒。数え方は docs/evaluation.md と docs/decisions/0011、"
            f"記録の範囲（送信の遅れの上限 {MAX_SEND_LAG_MS} ms）は docs/decisions/0021",
            f"- `threshold`: {d0.meta['threshold']!r}（float32 {f32(d0.meta['threshold']):.9g}。装置の申告値）、"
            f"`model`: {json.dumps(d0.meta.get('model'), sort_keys=True, ensure_ascii=False)}、"
            f"`feature_set`: {d0.meta.get('feature_set')!r}", ""]

    out += [f"## root の検出器のセッション（fw が `{DETECTOR_FW}`。{len(r.candidates)}）", "",
            "| セッション | cond | 評価 | 理由 |", "|---|---|---|---|"]
    for c in r.candidates:
        out.append(f"| {c.name} | {c.cond} | {'使う' if c.used else '使わない'} | {c.reason} |")
    out.append("")

    out += [f"## 評価に使ったセッション（{n_used}）", "",
            "| セッション | cond | 収集日 | 会話 | 嚥下 | 検出 | 誤検出（回） | 非嚥下（分） | 次元数 | FEAT に無い窓 | M2 の数字 |",
            "|---|---|---|---|---|---|---|---|---|---|---|"]
    for d in r.used:
        m = r.per_session[d.name]
        out.append(f"| {d.name} | {d.cond} | {d.day} | {'あり' if d.conv else 'なし'} | {m['swallows']} | {m['detected']} "
                   f"| {m['false_positive_runs']} | {m['non_swallow_min']:.2f} | {d.dims if d.dims is not None else '—'} "
                   f"| {d.feat_missing if d.has_feat else '—（feat.csv 無し）'} "
                   f"| {'使う' if d.cond == COND else '使わない（cond が ' + COND + ' でない）'} |")
    out.append("")

    out += ["## 合算", ""]
    if r.total is None:
        out += [f"合算は出さない（{n_used} セッション。{evaluate.MIN_SESSIONS} セッション未満。M2 の数字ではない）。", ""]
    else:
        t = r.total
        c = t["confusion"]
        out += [f"- 検出率: {_rate(t['detection_rate'])}（{t['detected']}/{t['swallows']}）",
                f"- 誤検出率: {_per_min(t['false_positives_per_min'])} 回/分（{t['false_positive_runs']} 回 / {t['non_swallow_min']:.2f} 分）",
                f"- 評価に使ったセッション: {', '.join(t['sessions'])}", "",
                "混同行列（イベント単位。TN は定義できないので出さない）:", "",
                "| | 陽性と出た | 陽性と出なかった |", "|---|---|---|",
                f"| 嚥下 | TP {c['tp']} | FN {c['fn']} |",
                f"| 非嚥下区間 | FP {c['fp']}（回） | — |", ""]
        det, fp = t["detection_rate"], t["false_positives_per_min"]
        out += ["## 合格線との比較（docs/evaluation.md、M2 検出器）", "",
                f"- 検出率 {_rate(det)}（合格線 {PASS_DETECTION_RATE:.2f} 以上）: "
                f"{_meets(None if det is None else det >= PASS_DETECTION_RATE)}",
                f"- 誤検出率 {_per_min(fp)} 回/分（合格線 {PASS_FP_PER_MIN:.1f} 回/分以下）: "
                f"{_meets(None if fp is None else fp <= PASS_FP_PER_MIN)}", ""]
        if any(d.cond != COND for d in r.used):
            out += [f"- 注意: cond が `{COND}` でないセッションを含む。M2 の数字ではない", ""]

    ref = r.reference
    a = ref["always_positive"]
    mism = ref["mismatch"]
    cough = ref["cough"]
    out += ["## 参考値（docs/decisions/0011「既知の弱点」、#27）", "",
            f"- 常時陽性の場合（全窓を陽性にした場合）: {_sum_line(a)}",
            f"- 陽性窓の割合（陽性窓 ÷ 全窓）: {_rate(ref['positive_ratio'])}（{ref['positive_windows']}/{ref['windows']}）",
            f"- `positive != (prob >= threshold)` の行数（float32 で比較）: {sum(mism.values())}"
            + ("" if sum(mism.values()) == 0 else "（装置の内部の整合が取れていない。" + ", ".join(f"{k}: {v}" for k, v in mism.items() if v) + "）"),
            f"- `c` に紐づく誤検出の塊（塊の最初の窓の中心 − t_c が [{COUGH_LINK_S[0]:+.1f}, {COUGH_LINK_S[1]:+.1f}] 秒）: "
            f"`c` {cough['coughs']} 回、紐づく塊 {cough['linked_runs']} / 誤検出の塊 {cough['fp_runs']}、"
            f"紐づく塊が 1 つ以上ある `c` の割合 "
            f"{_rate(cough['coughs_with_run'] / cough['coughs'] if cough['coughs'] else None)}"
            f"（{cough['coughs_with_run']}/{cough['coughs']}）",
            "- 収集日ごと（式は回数の合算。3 セッション未満でも出す）:"]
    out += [f"  - {day}: {_sum_line(m)}" for day, m in ref["by_day"].items()]
    out += ["- 会話の有無ごと（`o` の note が `conv` のセッションが会話あり）:"]
    out += [f"  - {label}: {_sum_line(m)}" for label, m in ref["by_conv"].items()]
    out.append("")

    if r.rescored is not None:
        out += [f"## 再採点（`--scorer {r.scorer}`。feat.csv を PC で採点し、閾値 float32 {f32(d0.meta['threshold']):.9g} で陽性にした）", "",
                "| セッション | 窓 | 装置 1 / PC 0 | 装置 0 / PC 1 | max abs(prob − prob_pc) | 装置: 検出 / 誤検出 | PC: 検出 / 誤検出 |",
                "|---|---|---|---|---|---|---|"]
        for d in r.used:
            s, m = r.rescored[d.name], r.per_session[d.name]
            pm = s["metrics"]
            out.append(f"| {d.name} | {s['windows']} | {s['dev1_pc0']} | {s['dev0_pc1']} | {s['max_abs_diff']:.3g} "
                       f"| {m['detected']}/{m['swallows']} / {m['false_positive_runs']} 回 "
                       f"| {pm['detected']}/{pm['swallows']} / {pm['false_positive_runs']} 回 |")
        pc_sum = sum_metrics({name: s["metrics"] for name, s in r.rescored.items()})
        dev_sum = sum_metrics(r.per_session)
        out += ["", f"- 装置の合算: {_sum_line(dev_sum)}", f"- PC の合算: {_sum_line(pc_sum)}",
                f"- 差のある窓の合計: 装置 1 / PC 0 が {sum(s['dev1_pc0'] for s in r.rescored.values())}、"
                f"装置 0 / PC 1 が {sum(s['dev0_pc1'] for s in r.rescored.values())}", ""]
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", type=Path, help="セッションフォルダの親（data/raw）")
    ap.add_argument("--sessions", nargs="+", metavar="NAME", help="評価に使うセッション名（明示したときはこれだけ）")
    ap.add_argument("--scorer", type=Path, help="score(features, meta) を持つ Python ファイル（#22）。feat.csv を再採点する")
    a = ap.parse_args(argv)
    print(format_report(run(a.root, a.sessions, a.scorer)))


if __name__ == "__main__":
    main()
