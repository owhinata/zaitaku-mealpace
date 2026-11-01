"""M4（検出器・咽喉マイク版）の評価側を 1 回だけ採点する（Issue #49、
docs/decisions/0041）。

使い方: python analysis/m4_final_eval.py <root>

  引数は root だけ。構成・閾値・後処理・対象・記録の範囲を選ぶ引数は無い。
  再実行の引数（--reproduce など）も無い。

対象は docs/decisions/0037 の M4-EVAL の 4 本（fixed_eval.M4_EXPECTED_EVAL。
evaluate_detector.scan_root は使わない）。構成は 0038（M4-CONFIG・M4-MODEL）。
採点するのは装置の detect.csv の positive（0038 の後処理の出力）で、PC で
採点し直さない。

手順（前の手順で止まったら、後の手順に進まない。plan #49 の 7 節）:
  7.1 実行前の検査（データを 1 つも開かない）: git（追跡ファイルの変更、
      analysis・tools・device・docs/decisions・docs/evaluation.md の未追跡
      ファイル）、0037・0038・0041 が追跡されていて機械が読む行が一致、
      assert_m4_eval、印が無い、依存、重みの sha256、手元の code_sha256。
  7.2 対象のフォルダの確かめ（lstat だけ。中は開かない）。
  7.3 印 docs/log/m4-final-49.txt を O_EXCL で排他作成し、書き込み・fsync・
      close（親のディレクトリも fsync）の後に次へ進む。印は、この後の手順で
      止まっても消さない。
  7.4 読み込みと検査（印の後、数字を出す前。全部済むまで何も出さない）。
  7.5 報告（標準出力に Markdown。最後に「完了:」の行）。

記録の範囲の始まりは events.csv の o start（0031・0037 の 5、0041）。採点に入れる
窓は開始が o start 以上の窓。終わりは 0021 のまま。

ファイルに書くのは印だけ。報告に波形、特徴量（FEAT）の値と集計、個々の窓の
prob・行の中身は出さない。feat.csv は各行の先頭 2 列（t_ms・window_t_ms）のほかを
保持も変換もしない。印の後に止まったときは、標準エラーに「未完了:」の行だけを
出す（値・行の中身・例外のメッセージ・トレースバックを出さない）。
"""

from __future__ import annotations
import argparse, csv, dataclasses, importlib, json, numbers, os, stat
import statistics, sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

import evaluate, features_np, fixed_eval, led_rule, postprocess, split
import train_eval
import evaluate_detector as ed
import m4_compare, m4_infer
from evaluate_detector import DetectRow, SessionData

# --- 定数（0041。評価側を開く前に固定。実行時に 0041 と照合） ---
PROC_DECISION = "docs/decisions/0041-m4-final-eval-procedure.md"
# 0041 の機械が読む行（一字一句同じ。7.1）
PROC_LINE = (
    "M4-EVAL-PROC: range=o-start windows=start>=o-start"
    " device-agree=poststepper,ledstepper"
    " breakdown=classify_run,cough_linked,fn_near3s,run_length"
    " feat=unused prob-breakdown=none"
)
PROC_PREFIX = "M4-EVAL-PROC:"
DECISIONS = ("0037", "0038", "0041")
# 未追跡のファイルも含めて変更が無いことを確かめる場所（final_eval に device を足す）
GIT_PATHS = (
    "analysis",
    "tools",
    "device",
    "docs/decisions",
    "docs/evaluation.md",
)
DEPENDENCIES = ("m4_compare", "m4_infer", "evaluate_detector")
DEPLOY_LIST = "device/unoq/DEPLOY_FILES"
DEPLOY_DIR = "device/unoq"
SUBJECT = "self"
COND = "meal"
FW = split.UNOQ_DETECTOR_FW  # "unoq-detector"
WINDOW_MS = ed.WINDOW_MS  # 1000
HOP_MS = int(round(evaluate.HOP_S * 1000))  # 250
MAX_GAP_MS = evaluate.MAX_GAP_MS  # 1000（0021）
MAX_SEND_LAG_MS = ed.MAX_SEND_LAG_MS  # 1000（0021）
DETECT_HEADER = list(ed.DETECT_HEADER)
N_FEAT = len(features_np.FEAT82_NAMES)  # 82
FEAT_HEADER = ["t_ms", "window_t_ms"] + [f"f{i}" for i in range(N_FEAT)]
EVENTS_HEADER = ["t_ms", "label", "note"]
START_NOTE = "start"
CONV_NOTE = ed.CONV_NOTE
END_GUARD_S = 1.5  # 終わりの 1.5 秒以内の s で止める（0037 の 5 の 6）
ROUND_TOL = 1e-8  # 丸めの候補の窓: |prob − enter| か |prob − exit| がこれ以下
ROUND_TOL_TEXT = "1e-8"
FN_NEAR_S = 3.0  # 見逃しの近く: s の [t − 3.0, t + 3.0] に陽性窓の中心
PASS_DETECTION_RATE = (
    0.85  # docs/evaluation.md 29 行目（M2 の行）を M4 に当てる
)
PASS_FP_PER_MIN = 1.0
MIN_EVAL_DAYS = 2  # 0037 の 1「収集日は 2 日以上」（止めない。注記）
COMPLETE_LINE = "完了: 報告はここまで（m4_final_eval.py が最後まで走った）"
MARKER_HEAD = (
    "# #49 M4 の評価側の採点の印（docs/decisions/0037 の 7・0041）。"
    "この印があれば analysis/m4_final_eval.py は止まる"
)

# M2・M3・学習側 CV の数字（0027・0036・0038 の本文から写した。テストで本文と照らす）
M2_REF = dict(det="0.150", tp=21, n=140, fp_rate="2.03", fp=89, min="43.86")
M3_REF = dict(det="1.000", tp=30, n=30, fp_rate="1.19", fp=23, min="19.36")
CV_REF = dict(det="0.955", tp=338, n=354, fp_rate="0.86")
CV_MEAL_REF = dict(det="0.973", tp=250, n=257, fp_rate="1.25")


class Stop(Exception):
    """自前の検査で止まる。メッセージは診断（ファイル名・行番号・列名・条件の
    名前）だけで、値・行の中身を入れない。"""


def _repo() -> Path:
    return fixed_eval.REPO_ROOT


def _now() -> datetime:
    return datetime.now().astimezone()


def _open_text(path: Path):
    """データのファイルを開く唯一の場所。"""
    return open(path, encoding="utf-8", newline="")


# --- 7.1 実行前の検査（データを 1 つも開かない） ---


@dataclass(frozen=True)
class Pre:
    commit: str  # HEAD の 40 桁
    cfg: dict  # m4_infer.parse_config(M4_CONFIG)
    model_sha256: str
    code_sha256: str  # 手元の DEPLOY_FILES の code_sha256
    names: tuple  # M4_EXPECTED_EVAL（この順）


def check_git() -> None:
    if train_eval.git_dirty():
        raise Stop(
            "追跡しているファイルに未コミットの変更があります（または git が使えません）。"
            "コミットしてから実行する（0041）"
        )
    out = train_eval._git("status", "--porcelain", "--", *GIT_PATHS)
    if out is None:
        raise Stop("git が使えません（0041）")
    if out.strip():
        raise Stop(
            f"{'・'.join(GIT_PATHS)} に未コミットの変更か未追跡のファイルがあります。"
            "コミットするか消してから実行する:\n" + out.rstrip()
        )


def _tracked_text(rel: str) -> str:
    path = _repo() / rel
    if not path.is_file():
        raise Stop(
            f"{rel} がありません（評価側の採点の前に書いてコミットする）"
        )
    if train_eval._git("ls-files", "--error-unmatch", "--", rel) is None:
        raise Stop(
            f"{rel} が git に追跡されていません（コミットしてから実行する）"
        )
    return path.read_text(encoding="utf-8")


def proc_lines(text: str) -> list:
    return [
        s.strip()
        for s in text.splitlines()
        if s.strip().startswith(PROC_PREFIX)
    ]


def check_decisions() -> None:
    if not fixed_eval.m4_lines_match(_tracked_text(fixed_eval.M4_DECISION)):
        raise Stop(
            f"{fixed_eval.M4_DECISION} の M4- の行が fixed_eval.py の定数と一致しません"
        )
    _tracked_text(fixed_eval.M4_CONFIG_DECISION)
    if not fixed_eval.m4_config_file_matches():
        raise Stop(
            f"{fixed_eval.M4_CONFIG_DECISION} の M4-CONFIG・M4-MODEL の行が"
            " fixed_eval.py の定数と一致しません"
        )
    if proc_lines(_tracked_text(PROC_DECISION)) != [PROC_LINE]:
        raise Stop(
            f"{PROC_DECISION} の {PROC_PREFIX} の行が analysis/m4_final_eval.py の"
            " PROC_LINE と一致しません（ちょうど 1 行、一字一句）"
        )


def check_eval_list() -> tuple:
    names = fixed_eval.M4_EXPECTED_EVAL
    if names is None:
        raise Stop(
            f"{fixed_eval.M4_DECISION} の評価側の一覧（M4-EVAL）がまだありません"
        )
    try:
        fixed_eval.assert_m4_eval(names)
    except SystemExit as e:
        raise Stop(str(e)) from None
    return tuple(names)


def marker_path() -> Path:
    return _repo() / fixed_eval.M4_MARKER


def check_marker() -> None:
    p = marker_path()
    if p.exists() or p.is_symlink():
        raise Stop(
            f"印 {fixed_eval.M4_MARKER} があります。評価側は採点済み（または採点を始めた"
            "実行がある）ので、採点し直しません（0037 の 7・0041）"
        )


def check_dependencies() -> None:
    for name in DEPENDENCIES:
        try:
            importlib.import_module(name)
        except ImportError:
            raise Stop(f"{name} を import できません") from None


def check_model() -> tuple:
    """(cfg, 重みの sha256)。"""
    if fixed_eval.M4_CONFIG is None or fixed_eval.M4_MODEL is None:
        raise Stop("fixed_eval.M4_CONFIG・M4_MODEL がまだありません")
    try:
        cfg = m4_infer.parse_config(fixed_eval.M4_CONFIG)
    except ValueError:
        raise Stop("fixed_eval.M4_CONFIG の形が違います") from None
    if cfg["post"] != "hyst" or cfg["kind"] != "cnn":
        raise Stop("fixed_eval.M4_CONFIG が後処理 hyst の CNN ではありません")
    try:
        fields = dict(p.split("=", 1) for p in fixed_eval.M4_MODEL.split(" "))
    except ValueError:
        fields = {}
    if set(fields) != {"path", "sha256"}:
        raise Stop("fixed_eval.M4_MODEL の形が違います")
    try:
        got = m4_infer.sha256_file(_repo() / fields["path"])
    except OSError:
        raise Stop(f"重みのファイル {fields['path']} を読めません") from None
    if got != fields["sha256"]:
        raise Stop(
            f"重みのファイル {fields['path']} の sha256 が {fixed_eval.M4_CONFIG_DECISION}"
            " の M4-MODEL と違います"
        )
    return cfg, fields["sha256"]


def local_code_sha256() -> str:
    """tools/record.py の local_code_sha256() と同じ計算（DEPLOY_FILES の行に
    m4_infer.code_sha256 を当てる。リンクは指す先の中身）。"""
    try:
        text = (_repo() / DEPLOY_LIST).read_text(encoding="utf-8")
        names = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return m4_infer.code_sha256(_repo() / DEPLOY_DIR / n for n in names)
    except (OSError, ValueError):
        raise Stop(f"{DEPLOY_LIST} の code_sha256 を計算できません") from None


def check_folders(root: Path, names: Sequence[str]) -> None:
    """7.2。各名前が split.NAME_RE に合い、root / 名前 が lstat でディレクトリで
    シンボリックリンクでないこと。中は開かない。"""
    for n in names:
        if split.NAME_RE.fullmatch(n) is None:
            raise Stop(f"フォルダ名が形式に合いません: {n}")
        try:
            st = os.lstat(Path(root) / n)
        except OSError:
            raise Stop(f"評価側のフォルダがありません: {n}") from None
        if stat.S_ISLNK(st.st_mode):
            raise Stop(f"評価側のフォルダがシンボリックリンクです: {n}")
        if not stat.S_ISDIR(st.st_mode):
            raise Stop(f"評価側のフォルダがディレクトリではありません: {n}")


def preflight(root: Path) -> Pre:
    """7.1・7.2。"""
    check_git()
    check_decisions()
    names = check_eval_list()
    check_marker()
    check_dependencies()
    cfg, model_sha = check_model()
    code = local_code_sha256()
    head = (train_eval._git("rev-parse", "HEAD") or "").strip()
    if len(head) != 40:
        raise Stop("HEAD のコミットを取れません")
    check_folders(root, names)
    return Pre(
        commit=head,
        cfg=cfg,
        model_sha256=model_sha,
        code_sha256=code,
        names=names,
    )


# --- 7.3 印 ---


def marker_text(started: str, commit: str, command: str, names) -> str:
    return (
        f"{MARKER_HEAD}\n"
        f"started: {started}\n"
        f"commit: {commit}\n"
        f"decisions: {' '.join(DECISIONS)}\n"
        f"command: {command}\n"
        f"sessions: {' '.join(names)}\n"
    )


def write_marker(path: Path, text: str) -> Path:
    """final_eval.write_marker と同じ形（パスを引数に取る）。固定のパスに排他作成し、
    書き込み・fsync・close（親も fsync）の後に戻る。失敗したら評価側を開かずに
    止まる（作成した印は消さない）。"""
    path = Path(path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise Stop(
            f"印 {fixed_eval.M4_MARKER} が既にあります（別の実行が作った）。評価側は開いていません"
        ) from None
    except OSError as e:
        raise Stop(
            f"印 {fixed_eval.M4_MARKER} を作れません（評価側は開いていません。{type(e).__name__}）"
        ) from None
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
        raise Stop(
            f"印 {fixed_eval.M4_MARKER} の書き込みに失敗しました（印は消していません。"
            f"評価側は開いていません。{type(e).__name__}）"
        ) from None
    return path


# --- 7.4 読み込みと検査（値を出さない読み込み） ---


def _where(
    name: str, fname: str, line: int | None = None, col: str | None = None
) -> str:
    s = f"{name}/{fname}"
    if line is not None:
        s += f" 行 {line}"
    if col is not None:
        s += f" 列 {col}"
    return s


def _int(text, where: str) -> int:
    try:
        return int(text)
    except (TypeError, ValueError):
        raise Stop(f"{where}: 整数に変換できない") from None


def _rows(path: Path, name: str, fname: str, header: list):
    """(行番号, 行) を順に返す。ヘッダを 0、行を 1 始まりで数える（空行は飛ばすが
    数える）。ヘッダが違えば止まる。"""
    with _open_text(path) as f:
        reader = csv.reader(f)
        if next(reader, None) != header:
            raise Stop(
                f"{_where(name, fname, 0)}: ヘッダが {fname} の形ではない"
            )
        for i, r in enumerate(reader, 1):
            if r:
                yield i, r


def _check_times(name: str, fname: str, lines, t, w) -> None:
    """0021 と同じ条件（evaluate_detector._check_times）。診断は行番号と列名だけ。"""
    for i in range(1, len(w)):
        if w[i] <= w[i - 1] or w[i] - w[i - 1] > MAX_GAP_MS:
            raise Stop(
                f"{_where(name, fname, lines[i], 'window_t_ms')}: 逆行か {MAX_GAP_MS} ms を超える飛び"
            )
        if t[i] < t[i - 1] or t[i] - t[i - 1] > MAX_GAP_MS:
            raise Stop(
                f"{_where(name, fname, lines[i], 't_ms')}: 逆行か {MAX_GAP_MS} ms を超える飛び"
            )
    for ln, ti, wi in zip(lines, t, w):
        if ti - (wi + WINDOW_MS) > MAX_SEND_LAG_MS:
            raise Stop(
                f"{_where(name, fname, ln, 't_ms')}: 送信の遅れが {MAX_SEND_LAG_MS} ms を超える"
            )


def _num(v) -> bool:
    # 組み込みの int・float の名前を使わない（テストが変換の呼び出しを見張る）
    return isinstance(v, numbers.Real) and not isinstance(v, bool)


def load_meta(session_dir: Path, pre: Pre) -> dict:
    """7.4 の 1。診断はキーの名前と条件だけ。"""
    name = session_dir.name
    path = session_dir / "meta.json"
    try:
        with _open_text(path) as f:
            meta = json.load(f)
    except OSError:
        raise Stop(f"{_where(name, 'meta.json')}: 開けない") from None
    except ValueError:
        raise Stop(
            f"{_where(name, 'meta.json')}: JSON として読めない"
        ) from None
    if not isinstance(meta, dict):
        raise Stop(f"{_where(name, 'meta.json')}: オブジェクトではない")

    def need(key: str, ok: bool, cond: str) -> None:
        if not ok:
            raise Stop(f"{_where(name, 'meta.json')} キー {key}: {cond}")

    need("subject", meta.get("subject") == SUBJECT, f"{SUBJECT} ではない")
    need("cond", meta.get("cond") == COND, f"{COND} ではない")
    need("fw", meta.get("fw") == FW, f"{FW} ではない")
    need(
        "m4_config",
        meta.get("m4_config") == fixed_eval.M4_CONFIG,
        "0038 の M4-CONFIG と違う",
    )
    model = meta.get("model")
    need(
        "model.sha256",
        isinstance(model, dict) and model.get("sha256") == pre.model_sha256,
        "0038 の M4-MODEL の sha256 と違う",
    )
    need(
        "feature_set",
        meta.get("feature_set") == features_np.FEAT82_SET,
        f"{features_np.FEAT82_SET} ではない（FEAT が無効）",
    )
    need(
        "feature_names",
        meta.get("feature_names") == list(features_np.FEAT82_NAMES),
        "features_np.FEAT82_NAMES と違う",
    )
    need("rpc", meta.get("rpc") is False, "false ではない")
    need(
        "window_ms",
        _num(meta.get("window_ms")) and meta.get("window_ms") == WINDOW_MS,
        f"{WINDOW_MS} ではない",
    )
    need(
        "hop_ms",
        _num(meta.get("hop_ms")) and meta.get("hop_ms") == HOP_MS,
        f"{HOP_MS} ではない",
    )
    ud = meta.get("unoq_detector")
    header = ud.get("header") if isinstance(ud, dict) else None
    code = header.get("code_sha256") if isinstance(header, dict) else None
    need(
        "unoq_detector.header.code_sha256",
        isinstance(code, str),
        "文字列ではない",
    )
    need(
        "unoq_detector.header.code_sha256",
        code == pre.code_sha256,
        "手元の DEPLOY_FILES の code_sha256 と違う",
    )
    return meta


@dataclass(frozen=True)
class Events:
    o_start_ms: int
    swallows: list  # 秒
    markers: dict  # t・c・n → 秒の並び（全部）
    conv: bool


def load_events(session_dir: Path) -> Events:
    """7.4 の 2。events.csv だけで決まる o start の条件。"""
    name, fname = session_dir.name, "events.csv"
    starts, rows = [], []
    conv = False
    for i, r in _rows(session_dir / fname, name, fname, EVENTS_HEADER):
        if len(r) != len(EVENTS_HEADER):
            raise Stop(
                f"{_where(name, fname, i)}: 列数が {len(EVENTS_HEADER)} ではない"
            )
        t = _int(r[0], _where(name, fname, i, "t_ms"))
        label, note = r[1], r[2].strip()
        if label == "o" and note == START_NOTE:
            starts.append(t)
        if label == "o" and note == CONV_NOTE:
            conv = True
        rows.append((t, label))
    if len(starts) != 1:
        raise Stop(
            f"{_where(name, fname)}: o start がちょうど 1 つではない（{len(starts)} 個）"
        )
    o = starts[0]
    for lb in ("s", "t", "c"):
        if any(lab == lb and t < o for t, lab in rows):
            raise Stop(f"{_where(name, fname)}: o start より前に {lb} がある")
    return Events(
        o_start_ms=o,
        swallows=[t / 1000.0 for t, lab in rows if lab == "s"],
        markers={
            lb: [t / 1000.0 for t, lab in rows if lab == lb]
            for lb in m4_compare.MOTION_MARKERS
        },
        conv=conv,
    )


def load_detect(session_dir: Path) -> list:
    """7.4 の 3（detect.csv）と 4（窓の間隔）。値を出さない読み込み。"""
    name, fname = session_dir.name, "detect.csv"
    path = session_dir / fname
    if not path.is_file():
        raise Stop(f"{_where(name, fname)}: ファイルが無い")
    rows, lines = [], []
    for i, r in _rows(path, name, fname, DETECT_HEADER):
        if len(r) != len(DETECT_HEADER):
            raise Stop(
                f"{_where(name, fname, i)}: 列数が {len(DETECT_HEADER)} ではない"
            )
        t_ms = _int(r[0], _where(name, fname, i, "t_ms"))
        w = _int(r[1], _where(name, fname, i, "window_t_ms"))
        pos = _int(r[2], _where(name, fname, i, "positive"))
        try:
            prob = float(r[3])
        except (TypeError, ValueError):
            raise Stop(
                f"{_where(name, fname, i, 'prob')}: 浮動小数に変換できない"
            ) from None
        led = _int(r[4], _where(name, fname, i, "led"))
        if pos not in (0, 1):
            raise Stop(f"{_where(name, fname, i, 'positive')}: 0 / 1 ではない")
        if not 0.0 <= prob <= 1.0:
            raise Stop(f"{_where(name, fname, i, 'prob')}: [0, 1] の外")
        if led not in (0, 1, 2):
            raise Stop(f"{_where(name, fname, i, 'led')}: 0 / 1 / 2 ではない")
        rows.append(
            DetectRow(
                t_ms=t_ms, window_t_ms=w, positive=pos, prob=prob, led=led
            )
        )
        lines.append(i)
    if not rows:
        raise Stop(f"{_where(name, fname)}: 行が無い")
    _check_times(
        name,
        fname,
        lines,
        [r.t_ms for r in rows],
        [r.window_t_ms for r in rows],
    )
    for i in range(1, len(rows)):
        if rows[i].window_t_ms - rows[i - 1].window_t_ms != HOP_MS:
            raise Stop(
                f"{_where(name, fname, lines[i], 'window_t_ms')}: 隣との差が {HOP_MS} ms ではない"
            )
    return rows


def load_feat_times(session_dir: Path) -> tuple:
    """7.4 の 3（feat.csv）。先頭 2 列だけを整数にし、3 列目より後は保持も変換も
    しない。(t_ms の列, window_t_ms の列)。"""
    name, fname = session_dir.name, "feat.csv"
    path = session_dir / fname
    if not path.is_file():
        raise Stop(f"{_where(name, fname)}: ファイルが無い（FEAT が無効）")
    t, w, lines = [], [], []
    for i, r in _rows(path, name, fname, FEAT_HEADER):
        if len(r) != len(FEAT_HEADER):
            raise Stop(
                f"{_where(name, fname, i)}: 列数が {len(FEAT_HEADER)} ではない"
            )
        t.append(_int(r[0], _where(name, fname, i, "t_ms")))
        w.append(_int(r[1], _where(name, fname, i, "window_t_ms")))
        lines.append(i)
    _check_times(name, fname, lines, t, w)
    return t, w


@dataclass(frozen=True, eq=False)
class Loaded:
    full: SessionData  # 全行（装置と PC の一致にだけ使う）
    scored: SessionData  # 範囲の内の行だけ（採点と切り分け）
    info: object  # m4_compare.Info（t・c・n は o start 以上）
    o_start_ms: int
    n_before: int  # o start より前の窓の数


def in_range(full: SessionData, o_start_ms: int, t1_ms: int) -> SessionData:
    """範囲の内の行だけを持つ SessionData（4 節）。rows は開始が o start 以上の
    窓、coughs は o start 以上、t0_s = o start、duration_s = 終わり − o start。
    swallows は絞らない（範囲の外の s は evaluate.event_metrics が止める）。"""
    o_s = o_start_ms / 1000.0
    return dataclasses.replace(
        full,
        rows=[r for r in full.rows if r.window_t_ms >= o_start_ms],
        coughs=[t for t in full.coughs if t >= o_s],
        t0_s=o_s,
        duration_s=t1_ms / 1000.0 - o_s,
    )


def range_info(ev: Events, o_start_ms: int):
    o_s = o_start_ms / 1000.0
    return m4_compare.Info(
        cond=COND,
        markers={
            lb: [t for t in ev.markers.get(lb, ()) if t >= o_s]
            for lb in m4_compare.MOTION_MARKERS
        },
        conv=ev.conv,
    )


def load_session(session_dir: Path, meta: dict, ev: Events) -> Loaded:
    """7.4 の 3〜5（o start の残りの条件と範囲の内の SessionData）。"""
    name = session_dir.name
    rows = load_detect(session_dir)
    feat_t, feat_w = load_feat_times(session_dir)
    detect_w = {r.window_t_ms for r in rows}
    if not set(feat_w) <= detect_w:
        raise Stop(f"{_where(name, 'feat.csv')}: detect.csv に無い窓がある")
    t1_ms = max(
        max(r.t_ms for r in rows),
        max(r.window_t_ms for r in rows) + WINDOW_MS,
        max(feat_t) if feat_t else 0,
    )
    o = ev.o_start_ms
    if o < rows[0].window_t_ms:
        raise Stop(
            f"{_where(name, 'events.csv')}: o start が最初の窓の開始より前"
        )
    if o >= t1_ms:
        raise Stop(
            f"{_where(name, 'events.csv')}: o start が記録の範囲の終わり以降"
        )
    t1_s = t1_ms / 1000.0
    if any(t >= t1_s - END_GUARD_S for t in ev.swallows):
        raise Stop(
            f"{_where(name, 'events.csv')}: 終わりの {END_GUARD_S} 秒以内（か終わりより後）に s がある"
        )
    full = SessionData(
        name=name,
        dir=session_dir,
        cond=COND,
        meta=meta,
        rows=rows,
        has_feat=True,
        feat_missing=len(detect_w - set(feat_w)),
        dims=N_FEAT,
        swallows=list(ev.swallows),
        coughs=list(ev.markers.get("c", [])),
        conv=ev.conv,
        t0_s=rows[0].window_t_ms / 1000.0,
        duration_s=t1_s - rows[0].window_t_ms / 1000.0,
    )
    return Loaded(
        full=full,
        scored=in_range(full, o, t1_ms),
        info=range_info(ev, o),
        o_start_ms=o,
        n_before=sum(1 for r in rows if r.window_t_ms < o),
    )


# --- 7.4 の 5・6: 数え方（数字は 7.5 まで出さない） ---


def agreement(full: SessionData, cfg: dict) -> dict:
    """5.2。全窓で PostStepper を窓ごとに回した出力と post_mask の一括の出力を
    作り、一致しなければ止める。装置の positive・led との不一致を数える。"""
    prob = [r.prob for r in full.rows]
    t = [k * m4_infer.HOP_S for k in range(len(prob))]
    st = postprocess.PostStepper(
        cfg["enter"], cfg["exit"], cfg["cap_s"], cfg["refractory_s"]
    )
    step = [bool(st.step(p, True, tk)) for p, tk in zip(prob, t)]
    mask = postprocess.post_mask(
        prob,
        [True] * len(prob),
        t,
        cfg["enter"],
        cfg["exit"],
        cfg["cap_s"],
        cfg["refractory_s"],
    )
    if len(mask) != len(step) or any(bool(m) != s for m, s in zip(mask, step)):
        raise Stop(
            f"{full.name}: PostStepper と post_mask の出力が一致しない（コードの不整合）"
        )
    dev1_pc0 = sum(
        1 for r, s in zip(full.rows, step) if r.positive == 1 and not s
    )
    dev0_pc1 = sum(1 for r, s in zip(full.rows, step) if r.positive == 0 and s)
    near = sum(
        1
        for p in prob
        if abs(p - cfg["enter"]) <= ROUND_TOL
        or abs(p - cfg["exit"]) <= ROUND_TOL
    )
    led_st = led_rule.LedStepper()
    led_step = [led_st.step(r.window_t_ms, r.positive) for r in full.rows]
    led_step_mismatch = sum(
        1 for r, e in zip(full.rows, led_step) if r.led != e
    )
    led_states_mismatch = sum(ed.led_mismatch(full).values())
    return {
        "windows": len(prob),
        "dev1_pc0": dev1_pc0,
        "dev0_pc1": dev0_pc1,
        "round_candidates": near,
        "led_stepper": led_step_mismatch,
        "led_states": led_states_mismatch,
    }


def run_length_s(run) -> float:
    """塊の長さ（秒）= 窓の数 × HOP（= 最後の窓の開始 − 最初の窓の開始 + 0.25。
    塊の中の窓は 0.25 秒の格子で隣り合う）。"""
    return run[-1] - run[0] + evaluate.HOP_S


def breakdown(d: SessionData, info, metrics: dict) -> dict:
    """5.3。誤検出の塊の主分類・副表、塊の長さ、c に紐づく塊、見逃しの近く。
    主分類の合計が誤検出の回数と一致しなければ止める。"""
    pos = d.positive_windows()
    fp = m4_compare.fp_runs(d.swallows, pos)
    if len(fp) != metrics["false_positive_runs"]:
        raise Stop(f"{d.name}: 誤検出の内訳の合計が誤検出の回数と一致しない")
    main = {c: 0 for c in m4_compare.FP_CATS}
    sub = {lb: 0 for lb in m4_compare.MOTION_MARKERS}
    for run in fp:
        cat, kinds = m4_compare.classify_run(run, d.swallows, info)
        main[cat] += 1
        if cat == "motion":
            for lb in kinds:
                sub[lb] += 1
    if sum(main.values()) != metrics["false_positive_runs"]:
        raise Stop(f"{d.name}: 誤検出の主分類の合計が誤検出の回数と一致しない")
    runs = ed._runs(pos)
    with_s = [
        run_length_s(r)
        for r in runs
        if any(evaluate._hits(w, t) for w in r for t in d.swallows)
    ]
    fp_len = [run_length_s(r) for r in fp]
    missed = [
        t for t in d.swallows if not any(evaluate._hits(w, t) for w in pos)
    ]
    near = sum(
        1
        for t in missed
        if any(
            t - FN_NEAR_S <= w + evaluate.WINDOW_S / 2 <= t + FN_NEAR_S
            for w in pos
        )
    )
    return {
        "main": main,
        "sub": sub,
        "len_with_s": with_s,
        "len_fp": fp_len,
        "missed": len(missed),
        "missed_near": near,
        "cough": ed.cough_linked(d),
    }


@dataclass(eq=False)
class Result:
    root: Path
    pre: Pre
    sessions: list  # Loaded（M4_EXPECTED_EVAL の順）
    per_session: dict
    total: dict
    always: dict
    pos_windows: int
    all_windows: int
    by_day: dict
    by_conv: dict
    agree: dict  # 名前 → agreement
    parts: dict  # 名前 → breakdown
    days: int


def score(root: Path, pre: Pre, stage: list) -> Result:
    """7.4 の 1〜6。stage[0] に今の段の名前を入れる（例外の報告に使う）。"""
    root = Path(root)
    stage[0] = "7.4 の 1（meta.json）"
    metas = {n: load_meta(root / n, pre) for n in pre.names}
    stage[0] = "7.4 の 2（events.csv）"
    events = {n: load_events(root / n) for n in pre.names}
    stage[0] = "7.4 の 3〜5（detect.csv・feat.csv・記録の範囲）"
    sessions = [load_session(root / n, metas[n], events[n]) for n in pre.names]
    stage[0] = "7.4 の 5（evaluate.event_metrics）"
    per = {}
    for s in sessions:
        d = s.scored
        per[d.name] = evaluate.event_metrics(
            d.swallows, d.positive_windows(), d.duration_s, d.t0_s
        )
    if list(per) != list(pre.names):
        raise Stop("aggregate に渡すセッションが M4-EVAL の一覧と一致しない")
    total = evaluate.aggregate(per)
    always = train_eval.sum_metrics(
        {
            s.scored.name: evaluate.event_metrics(
                s.scored.swallows,
                s.scored.all_windows(),
                s.scored.duration_s,
                s.scored.t0_s,
            )
            for s in sessions
        }
    )
    by_day = {
        day: train_eval.sum_metrics(
            {n: m for n, m in per.items() if n[:8] == day}
        )
        for day in sorted({n[:8] for n in per})
    }
    by_conv = {
        label: train_eval.sum_metrics(
            {
                s.scored.name: per[s.scored.name]
                for s in sessions
                if s.scored.conv == flag
            }
        )
        for label, flag in (("会話あり", True), ("会話なし", False))
        if any(s.scored.conv == flag for s in sessions)
    }
    stage[0] = "7.4 の 6（装置と PC の一致・誤検出の内訳）"
    agree = {s.full.name: agreement(s.full, pre.cfg) for s in sessions}
    parts = {
        s.scored.name: breakdown(s.scored, s.info, per[s.scored.name])
        for s in sessions
    }
    return Result(
        root=root,
        pre=pre,
        sessions=sessions,
        per_session=per,
        total=total,
        always=always,
        pos_windows=sum(len(s.scored.positive_windows()) for s in sessions),
        all_windows=sum(len(s.scored.rows) for s in sessions),
        by_day=by_day,
        by_conv=by_conv,
        agree=agree,
        parts=parts,
        days=len({n[:8] for n in pre.names}),
    )


# --- 7.5 報告（計算済みの値の書式だけ） ---


def _rate(v) -> str:
    return "—" if v is None else f"{v:.3f}"


def _per_min(v) -> str:
    return "—" if v is None else f"{v:.2f}"


def _meets(ok: bool | None) -> str:
    return "出ない" if ok is None else ("満たす" if ok else "満たさない")


def _sum_line(m: dict) -> str:
    return (
        f"検出率 {_rate(m['detection_rate'])}（{m['detected']}/{m['swallows']}）、誤検出 {m['false_positive_runs']} 回、"
        f"誤検出率 {_per_min(m['false_positives_per_min'])} 回/分（非嚥下 {m['non_swallow_min']:.2f} 分）"
    )


def _len_text(v: list) -> str:
    if not v:
        return "0 個"
    return f"{len(v)} 個、中央値 {statistics.median(v):.2f} 秒・最大 {max(v):.2f} 秒"


def _meta_num(v) -> str:
    return f"{v:g}" if _num(v) else "—"


def header_lines(root: Path, pre: Pre, command: str) -> list:
    """印の後、検査の前に標準出力に出す実行の条件（数字の結果は入れない）。"""
    return [
        "# M4 検出器（咽喉マイク版）: 評価側の採点（1 回だけ。#49、docs/decisions/0041）",
        "",
        "## 実行の条件",
        "",
        f"- 実行: `{command}`",
        f"- コミット: {pre.commit}",
        f"- 印: `{fixed_eval.M4_MARKER}`（作成してから評価側を開いた）",
        f"- 手順と定義: {fixed_eval.M4_DECISION}（対象・1 回だけ）、{fixed_eval.M4_CONFIG_DECISION}（構成）、"
        f"{PROC_DECISION}（記録の範囲・出す数字・切り分け）",
        f"- `{PROC_LINE}`",
        f"- 構成: `M4-CONFIG: {fixed_eval.M4_CONFIG}`",
        f"- 重み: `M4-MODEL: {fixed_eval.M4_MODEL}`",
        f"- 手元の `code_sha256`（`{DEPLOY_LIST}`）: `{pre.code_sha256}`",
        f"- 対象（`M4-EVAL`、この順）: {', '.join(pre.names)}",
        "",
    ]


def _fp_cat_rows(r: Result, cols: list) -> list:
    """cols: [(見出し, 名前の集合)]。"""
    out = []
    for c in m4_compare.FP_CATS:
        cells = [sum(r.parts[n]["main"][c] for n in names) for _, names in cols]
        total = sum(r.parts[n]["main"][c] for n in r.parts)
        out.append(
            f"| {m4_compare.FP_CAT_LABEL[c]} | "
            + " | ".join(str(x) for x in cells)
            + f" | {total} |"
        )
    return out


def format_report(r: Result) -> str:
    t = r.total
    cm = t["confusion"]
    det, fp = t["detection_rate"], t["false_positives_per_min"]
    names = list(r.per_session)
    conv_of = {s.scored.name: s.scored.conv for s in r.sessions}
    out = [
        f"## 評価に使ったセッション（{len(names)}）",
        "",
        f"記録の範囲は `o start` から（0041）。採点に入れる窓は開始が `o start` 以上。窓 {evaluate.WINDOW_S} 秒 / ホップ {evaluate.HOP_S} 秒、"
        "数え方は docs/evaluation.md と docs/decisions/0011、終わりは docs/decisions/0021。採点したのは装置の `positive`。",
        "",
        "| セッション | 収集日 | 会話 | `s` | 検出 | 検出率 | 誤検出（回） | 非嚥下（分） | 誤検出率（回/分） "
        "| 陽性窓 / 全窓（範囲の内） | `o start` より前の窓 | FEAT に無い窓 | `marker_offset_shift_max_ms` |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in r.sessions:
        d, m = s.scored, r.per_session[s.scored.name]
        out.append(
            f"| {d.name} | {d.day} | {'あり' if d.conv else 'なし'} | {m['swallows']} | {m['detected']} "
            f"| {_rate(m['detection_rate'])} | {m['false_positive_runs']} | {m['non_swallow_min']:.2f} "
            f"| {_per_min(m['false_positives_per_min'])} | {len(d.positive_windows())} / {len(d.rows)} "
            f"| {s.n_before} | {s.full.feat_missing} | {_meta_num(d.meta.get('marker_offset_shift_max_ms'))} |"
        )
    out += [
        "",
        "## 合算（`evaluate.aggregate`。回数の合算）",
        "",
        f"- 検出率: {_rate(det)}（{t['detected']}/{t['swallows']}）",
        f"- 誤検出率: {_per_min(fp)} 回/分（{t['false_positive_runs']} 回 / {t['non_swallow_min']:.2f} 分）",
        f"- 評価に使ったセッション: {', '.join(t['sessions'])}",
        "",
        "混同行列（イベント単位。TN は定義できないので出さない）:",
        "",
        "| | 陽性と出た | 陽性と出なかった |",
        "|---|---|---|",
        f"| 嚥下 | TP {cm['tp']} | FN {cm['fn']} |",
        f"| 非嚥下区間 | FP {cm['fp']}（回） | — |",
        "",
        "## 合格線との比較（docs/evaluation.md 29 行目の合格線を M4 に当てる。docs/plan.md の M4）",
        "",
        f"- 検出率 {_rate(det)}（合格線 {PASS_DETECTION_RATE:.2f} 以上）: "
        f"{_meets(None if det is None else det >= PASS_DETECTION_RATE)}",
        f"- 誤検出率 {_per_min(fp)} 回/分（合格線 {PASS_FP_PER_MIN:.1f} 回/分以下）: "
        f"{_meets(None if fp is None else fp <= PASS_FP_PER_MIN)}",
        "",
        "## 参考値（docs/decisions/0011「既知の弱点」）",
        "",
        f"- 常時陽性の場合（範囲の内の全窓を陽性にした場合）: {_sum_line(r.always)}",
        f"- 陽性窓の割合（範囲の内の陽性窓 ÷ 全窓）: "
        f"{_rate(r.pos_windows / r.all_windows if r.all_windows else None)}（{r.pos_windows}/{r.all_windows}）",
        "- 収集日ごと（式は回数の合算。3 セッション未満でも出す）:",
        *(f"  - {day}: {_sum_line(m)}" for day, m in r.by_day.items()),
        "- 会話の有無ごと（`o` の note が `conv` のセッションが会話あり）:",
        *(f"  - {label}: {_sum_line(m)}" for label, m in r.by_conv.items()),
        f"- 評価側の収集日: {r.days} 日（0037 の 1「2 日以上」: {_meets(r.days >= MIN_EVAL_DAYS)}）",
        "",
    ]
    a = r.agree
    tot = {k: sum(v[k] for v in a.values()) for k in next(iter(a.values()))}
    out += [
        "## 装置と PC の一致（全窓。`o start` より前の窓を含む）",
        "",
        "PC の再計算は `postprocess.PostStepper` を窓ごとに回した出力（`step(prob, True, k × 0.25)`。装置の `Detector.step` と同じ呼び方）で、"
        "`post_mask` の一括の出力と一致することを確かめてから比べた。採点は装置の `positive` で行った（評価の数字は装置の出力）。",
        "",
        "| セッション | 窓 | `positive` 装置 1 / PC 0 | 装置 0 / PC 1 | 丸めの候補の窓 | `led` の不一致（`LedStepper`） | `led` の不一致（`led_states`） |",
        "|---|---|---|---|---|---|---|",
    ]
    for n, v in a.items():
        out.append(
            f"| {n} | {v['windows']} | {v['dev1_pc0']} | {v['dev0_pc1']} | {v['round_candidates']} "
            f"| {v['led_stepper']} | {v['led_states']} |"
        )
    out += [
        f"| 計 | {tot['windows']} | {tot['dev1_pc0']} | {tot['dev0_pc1']} | {tot['round_candidates']} "
        f"| {tot['led_stepper']} | {tot['led_states']} |",
        "",
        f"- 丸めの候補の窓: `|prob − {pre_enter(r)}|` か `|prob − {pre_exit(r)}|` が {ROUND_TOL_TEXT} 以下の窓"
        "（`prob` は 9 桁の有効数字で書かれているので比較が変わりうる。状態は後の窓に伝わりうる）。不一致の原因は断定しない。",
        "- `led` の行: LED は嚥下の「目安」で、評価には使わない（docs/decisions/0018）。不一致は止めずに数だけを出す。",
        "",
    ]
    out += _breakdown_lines(r, names, conv_of)
    out += _compare_lines(r)
    out += _note_lines()
    return "\n".join(out)


def pre_enter(r: Result) -> str:
    return f"{r.pre.cfg['enter']:.2f}"


def pre_exit(r: Result) -> str:
    return f"{r.pre.cfg['exit']:.2f}"


def _breakdown_lines(r: Result, names: list, conv_of: dict) -> list:
    p = r.parts
    conv_cols = [
        (label, [n for n in names if conv_of[n] == flag])
        for label, flag in (("会話あり", True), ("会話なし", False))
    ]
    sub = {
        lb: sum(p[n]["sub"][lb] for n in names)
        for lb in m4_compare.MOTION_MARKERS
    }
    fp_total = r.total["false_positive_runs"]
    cough = {
        k: sum(p[n]["cough"][k] for n in names)
        for k in ("coughs", "fp_runs", "linked_runs", "coughs_with_run")
    }
    with_s = [x for n in names for x in p[n]["len_with_s"]]
    fp_len = [x for n in names for x in p[n]["len_fp"]]
    missed = sum(p[n]["missed"] for n in names)
    near = sum(p[n]["missed_near"] for n in names)
    after = sum(p[n]["main"]["after"] for n in names)
    lo, hi = ed.COUGH_LINK_S
    out = [
        "## 届かなかった場合の切り分け（0041 で固定した定義。満たす・満たさないによらず出す）",
        "",
        f"### 誤検出の主分類（`m4_compare.classify_run`。0038 の学習側の内訳と同じ定義。{fp_total} 回）",
        "",
        "| 主分類 | " + " | ".join(names) + " | 計 |",
        "|---|" + "---|" * (len(names) + 1),
        *_fp_cat_rows(r, [(n, [n]) for n in names]),
        "",
        "| 主分類 | " + " | ".join(label for label, _ in conv_cols) + " | 計 |",
        "|---|" + "---|" * (len(conv_cols) + 1),
        *_fp_cat_rows(r, conv_cols),
        "",
        "副表（重複あり。「動作に付くもの」の塊で、範囲にあるマーカーの種類ごとの塊の数）: "
        + "、".join(f"`{lb}` {sub[lb]}" for lb in m4_compare.MOTION_MARKERS),
        "",
        "### 条件別（会話の有無）と学習側 CV",
        "",
        *(f"- {label}: {_sum_line(m)}" for label, m in r.by_conv.items()),
        f"- 参考: 学習側 CV の `meal` だけ（0038。構成の選択に使った数字で楽観側）: 検出率 {CV_MEAL_REF['det']}"
        f"（{CV_MEAL_REF['tp']}/{CV_MEAL_REF['n']}）、誤検出率 {CV_MEAL_REF['fp_rate']} 回/分",
        "",
        "### 嚥下の直後の延びと塊の長さ（集計だけ）",
        "",
        f"- 主分類「{m4_compare.FP_CAT_LABEL['after']}」: {after} 回",
        "- 陽性の塊の長さ（窓の数 × 0.25 秒）。「嚥下を含む塊」は窓の中心が `s` の `[t − 0.5, t + 1.5]` に入る窓を 1 つ以上含む塊、"
        "「誤検出の塊」は誤検出に数えた塊（両方に入る塊がある）:",
        f"  - 嚥下を含む塊: {_len_text(with_s)}",
        f"  - 誤検出の塊: {_len_text(fp_len)}",
        "",
        f"### `c` に紐づく誤検出の塊（`evaluate_detector.cough_linked`。M2 と同じ定義。塊の最初の窓の中心 − t_c が [{lo:+.1f}, {hi:+.1f}] 秒）",
        "",
        f"- `c` {cough['coughs']} 回、紐づく塊 {cough['linked_runs']} / 誤検出の塊 {cough['fp_runs']}、紐づく塊が 1 つ以上ある `c` "
        f"{cough['coughs_with_run']}/{cough['coughs']}",
        "",
        "### 見逃し（FN）",
        "",
        f"- 見逃した `s` {missed} 回のうち、`[t − {FN_NEAR_S:.1f}, t + {FN_NEAR_S:.1f}]` 秒に中心がある陽性窓（範囲の内の装置の `positive`）が"
        f"ある: {near}、無い: {missed - near}。`prob` を使った見逃しの内訳は出さない（0041）。",
        "",
        "### 装置と PC の差",
        "",
        "- 上の「装置と PC の一致」の数。#47 の再生モードの一致（学習用 `meal`、docs/log/2026-10-29.md「#47」）では `prob` 1e-5、`positive`・`led` は完全に一致していた。",
        "- 取り込み口（学習用 `meal` の `arecord` の経路と装置の ALSA）の差は #49 では調べない（FEAT を使わない。0041）。",
        "",
    ]
    return out


def _ref_rate(ref: dict) -> str:
    return f"{ref['det']}（{ref['tp']}/{ref['n']}）"


def _compare_lines(r: Result) -> list:
    t = r.total
    days = r.days
    return [
        "## M2・M3 と並べる（docs/decisions/0037 の 6）",
        "",
        "| | M2（0027） | M3 構成 J（0034・0036） | M4（この採点） | 参考: M4 学習側 CV（0038。楽観側） |",
        "|---|---|---|---|---|",
        "| センサ・基板 | 基板の IMU＋PDM、Nano RP2040 | 咽喉マイク、PC で採点 | 咽喉マイク、UNO Q の Linux 側で推論 | 咽喉マイク、PC |",
        "| 手法 | Edge Impulse int8 | CNN `mfe40` 種 0、ヒステリシス | CNN `mfe40` 種 0・1・2 の平均、ヒステリシス＋不応期 0.5 秒 | 同左 |",
        f"| 評価側 | `meal` 3 本・3 収集日 | 条件別 7 本・1 収集日 | `meal` {len(r.sessions)} 本・{days} 収集日 | 26 本・収集日ごとの CV |",
        "| `s` の決まり | 0023 | M1（すべての嚥下） | 0023 | 混在 |",
        f"| 検出率 | {_ref_rate(M2_REF)} | {_ref_rate(M3_REF)} | {_rate(t['detection_rate'])}（{t['detected']}/{t['swallows']}） | {_ref_rate(CV_REF)} |",
        f"| 誤検出率 | {M2_REF['fp_rate']} 回/分 | {M3_REF['fp_rate']} 回/分 | {_per_min(t['false_positives_per_min'])} 回/分 | {CV_REF['fp_rate']} 回/分 |",
        "| 合格線 | 85% / 1 回/分 | 70% / 3 回/分（分岐点） | 85% / 1 回/分 | （判定に使わない） |",
        "",
        "- M4 と同じ条件（`meal`・0023・同じ合格線）で比べられるのは M2。M3 は条件別のセッションで `s` の決まりも違う。",
        "- M4 の手法は `docs/evaluation.md` 29 行目（M2 の行）・28 行目・8 行目からの記録した逸脱（docs/decisions/0038）。"
        "学習側 CV は構成の選択に使った数字で楽観側（0038 の決定 2・3）。",
        "",
    ]


def _note_lines() -> list:
    return [
        "## 注記",
        "",
        "- `s` は押せた嚥下だけ（docs/decisions/0023「数字の読み方」）。打ち忘れ・打ち遅れの向きと大きさは確定できない。",
        "- ALSA・USB・arecord の中の測れない固定の遅れ（125 ms 程度までの推測）は補正していない。窓の時刻は実際より少し遅い側に刻まれる（0037 の追記 10/29）。",
        "- `events.csv` の `t_ms` は PC の受信時刻を `offset` で装置の時計に直した値（`marker_offset_shift_max_ms` は上の表。補正しない）。",
        "- 記録の範囲の始まりは `o start`（0041）。装置の後処理と LED（目安）の状態は最初の窓から続いていて、`o start` より前の窓の状態の持ち越しは補正していない。",
        "- FEAT は #49 では使っていない（0041。`feat.csv` は時刻の 2 列だけを読んだ。取り込み口の差は確かめていない）。",
        "- LED は嚥下の「目安」で、評価には使わない（docs/decisions/0018）。",
        "- 評価側の採点は 1 回だけ（印）。この後に構成・閾値・後処理を変えない（docs/decisions/0038）。",
        "",
    ]


# --- 実行 ---


def _incomplete(err, reason: str) -> None:
    """標準エラーに「未完了:」の 1 行。書き出し自体の失敗は握りつぶす
    （終了コードは呼ぶ側が 1 にする）。"""
    try:
        print(
            f"未完了: {reason}で止まった。この出力は完成した報告ではない",
            file=err,
            flush=True,
        )
    except (Exception, SystemExit):  # noqa: BLE001
        _to_devnull(err)


def _to_devnull(stream) -> None:
    """stream のファイル記述子を /dev/null に差し替える（Python の文書の
    BrokenPipeError の対処）。インタプリタの終了時の再 flush が「Exception
    ignored」を出して終了コードを 120 にするのを防ぐ。fileno() を持たない出力先
    （StringIO など）では何もしない。失敗は握りつぶす。"""
    try:
        fd = stream.fileno()
    except (Exception, SystemExit):  # noqa: BLE001
        return
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, fd)
        finally:
            os.close(devnull)
    except (Exception, SystemExit):  # noqa: BLE001
        pass


def _safe_flush(out) -> None:
    """止まるときの出力先の flush。失敗したら記述子を /dev/null に差し替える
    （残ったバッファは終了時にそこへ捨てられる）。例外は出さない。"""
    try:
        out.flush()
    except (Exception, SystemExit):  # noqa: BLE001
        _to_devnull(out)


def run(
    root: Path,
    command: str | None = None,
    out=None,
    err=None,
    _sink: list | None = None,
) -> int:
    """7.1〜7.5。終了コード（0 = 完了、1 = 止まった）。_sink は合成データの
    テストが Result を受け取るためだけの口。"""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    root = Path(root)
    command = command or f"python analysis/m4_final_eval.py {root}"
    try:
        pre = preflight(root)
        started = _now().isoformat(timespec="seconds")
        write_marker(
            marker_path(), marker_text(started, pre.commit, command, pre.names)
        )
    except Stop as e:
        _safe_flush(out)
        _incomplete(err, f"{e}（印の前。評価側は開いていない）")
        return 1
    # 印の後（実行の条件の書き出し・7.4・7.5 の書式と書き出し）は 1 つの try。
    # 自前の検査は診断だけ、それ以外は例外の型の名前だけ
    stage = ["7.3 の後（実行の条件の書き出し）"]
    try:
        print("\n".join(header_lines(root, pre, command)), file=out, flush=True)
        stage[0] = "7.4"
        result = score(root, pre, stage)
        stage[0] = "7.5（報告の書式）"
        text = format_report(result)
        stage[0] = "7.5（報告の書き出し）"
        if _sink is not None:
            _sink.append(result)
        print(text, file=out)
        print(COMPLETE_LINE, file=out, flush=True)
    except Stop as e:
        _safe_flush(out)
        _incomplete(err, str(e))
        return 1
    except (Exception, SystemExit) as e:  # noqa: BLE001 メッセージ・トレースバックを出さない
        _safe_flush(out)
        _incomplete(err, f"{stage[0]}（{type(e).__name__}）")
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "root", type=Path, help="セッションフォルダの親（data/raw）"
    )
    a = ap.parse_args(argv)
    sys.exit(run(a.root, f"python analysis/m4_final_eval.py {a.root}"))


if __name__ == "__main__":
    main()
