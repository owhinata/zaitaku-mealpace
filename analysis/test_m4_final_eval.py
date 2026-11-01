"""m4_final_eval.py を合成データで検証する（plan #49 の 10 節 E1〜E10、
docs/decisions/0041）。

実行: python -m unittest discover -s analysis -v
合成のリポジトリ（0037・0038・0041 の文、重みの代わりのファイル、DEPLOY_FILES）と
root（M4-EVAL の 4 本の名前の合成のセッション: meta.json・events.csv・detect.csv・
feat.csv。tools/record.py の UnoqSession と同じヘッダ）を一時フォルダに作る。
fixed_eval.REPO_ROOT・fixed_eval.M4_MODEL・train_eval._git を差し替える
（test_final_eval.py の流儀。このリポジトリの git・docs/log/・data/ は使わない）。
実物のリポジトリから読むのは docs/decisions の 0027・0036・0037・0038・0041 の本文だけ。
"""

from __future__ import annotations
import builtins, contextlib, hashlib, io, json, os, subprocess, sys
import tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import evaluate, evaluate_detector as ed, features_np, fixed_eval, led_rule
import m4_compare, m4_infer, postprocess, train_eval
import m4_final_eval as mfe

HERE = Path(__file__).resolve().parent
REAL_REPO = HERE.parent
HEAD = "0123456789abcdef0123456789abcdef01234567"
NOW = datetime(2026, 11, 3, 10, 0, 0, tzinfo=timezone(timedelta(hours=9)))
# 0041 の機械が読む行（コードから作らずに書く）
PROC_LINE = (
    "M4-EVAL-PROC: range=o-start windows=start>=o-start device-agree=poststepper,ledstepper"
    " breakdown=classify_run,cough_linked,fn_near3s,run_length feat=unused prob-breakdown=none"
)
NAMES = fixed_eval.M4_EXPECTED_EVAL
CONV = dict(zip(NAMES, (True, True, False, False)))
RICH = NAMES[0]  # 誤検出の主分類が全部そろう合成のセッション
CFG = m4_infer.parse_config(fixed_eval.M4_CONFIG)
# 目印の値（出力に出てはいけない）
PROB_LOW = 0.1012345678  # .9g → 0.101234568
PROB_HIGH = 0.9512345678  # .9g → 0.951234568
PROB_MARKS = ("0.101234568", "0.951234568")
FEAT_MARK = "0.123456789"
FORBIDDEN = ("達成", "誤嚥", "risk score", "aspiration", "Traceback")
NUMBER_WORDS = ("検出率", "誤検出率", "TP ", "混同行列")

N_WIN = 400
W0 = 1000
LAG = 20
O_START = 3000
# 既定の陽性の塊（窓の開始 ms の [始め, 終わり]）。範囲の前の塊・o start の直後の
# c に紐づく塊・3 つの s・誤検出 1 つ
BASE_RUNS = [
    (2000, 2500),
    (3250, 3750),
    (19500, 20500),
    (35000, 35500),
    (49500, 50500),
    (79500, 80500),
]
BASE_S = [20000, 50000, 80000]
# RICH に足す: 嚥下の直後の延び・直前・動作（n）・見逃しの近く
RICH_RUNS = [(59500, 61500), (68000, 70000), (85000, 85250), (92000, 92250)]
RICH_S = [60000, 70000, 90000, 97000]


def window_ms(k: int) -> int:
    return W0 + 250 * k


def default_events(name: str) -> list:
    ev = [(O_START, "o", "start")]
    if CONV[name]:
        ev.append((O_START + 100, "o", "conv"))
    ev.append((3250, "c", ""))
    s = BASE_S + (RICH_S if name == RICH else [])
    ev += [(t, "s", "") for t in s]
    if name == RICH:
        ev.append((85000, "n", ""))
    return sorted(ev, key=lambda e: e[0])


def default_runs(name: str) -> list:
    return BASE_RUNS + (RICH_RUNS if name == RICH else [])


def probs_for(runs, n=N_WIN, override=None) -> list:
    out = []
    for k in range(n):
        w = window_ms(k)
        p = PROB_HIGH if any(a <= w <= b for a, b in runs) else PROB_LOW
        out.append((override or {}).get(k, p))
    return out


def device_rows(probs) -> list:
    """装置（m4_infer.Detector.step）と同じ呼び方で positive・led を作り、
    detect.csv の行（prob は .9g の文字列）にする。"""
    st = postprocess.PostStepper(
        CFG["enter"], CFG["exit"], CFG["cap_s"], CFG["refractory_s"]
    )
    led = led_rule.LedStepper()
    rows = []
    for k, p in enumerate(probs):
        w = window_ms(k)
        pos = int(st.step(p, True, k * m4_infer.HOP_S))
        rows.append(
            [
                str(w + 1000 + LAG),
                str(w),
                str(pos),
                f"{p:.9g}",
                str(led.step(w, pos)),
            ]
        )
    return rows


def write_csv(path: Path, header, rows) -> None:
    lines = [",".join(header)] + [",".join(r) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def meta_for(name: str, model_sha: str, code: str) -> dict:
    return {
        "subject": "self",
        "cond": "meal",
        "position": "midline-below-thyroid",
        "band": "elastic-25mm",
        "firmware_sha": "abc1234",
        "notes": "",
        "fw": "unoq-detector",
        "window_ms": 1000,
        "hop_ms": 250,
        "m4_config": fixed_eval.M4_CONFIG,
        "model": {"path": "m4-0038.npz", "sha256": model_sha},
        "feature_set": features_np.FEAT82_SET,
        "feature_names": list(features_np.FEAT82_NAMES),
        "rpc": False,
        "unoq_detector": {"header": {"code_sha256": code}, "end": None},
        "marker_offset_shift_max_ms": 3,
    }


def files_under(p: Path) -> list:
    return sorted(str(x.relative_to(p)) for x in p.rglob("*"))


def fake_git(tracked: str = "", paths: str = "", untracked=()):
    def g(*args):
        if args[0] == "rev-parse":
            return HEAD + "\n"
        if args[0] == "status" and "--untracked-files=no" in args:
            return tracked
        if args[0] == "status":
            return paths
        if args[0] == "ls-files":
            return None if args[-1] in set(untracked) else args[-1] + "\n"
        raise AssertionError(f"想定していない git の呼び出し: {args}")

    return g


class Case(unittest.TestCase):
    """1 テストごとに新しい合成のリポジトリと root を作る。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = self.make_repo()
        self.root = self.tmp / "raw"
        self.root.mkdir()
        for n in NAMES:
            self.write_session(n)
        self.marker = self.repo / fixed_eval.M4_MARKER

    # --- 置き場 ---

    def make_repo(self) -> Path:
        repo = self.tmp / "repo"
        (repo / "docs" / "decisions").mkdir(parents=True)
        (repo / "docs" / "log").mkdir(parents=True)
        (repo / "analysis" / "models").mkdir(parents=True)
        (repo / "device" / "unoq").mkdir(parents=True)
        weights = repo / "analysis" / "models" / "m4-0038.npz"
        weights.write_bytes(b"synthetic weights (not a model)")
        self.model_sha = hashlib.sha256(weights.read_bytes()).hexdigest()
        self.model_value = (
            f"path=analysis/models/m4-0038.npz sha256={self.model_sha}"
        )
        (repo / "device" / "unoq" / "a.py").write_text("A = 1\n")
        (repo / "device" / "unoq" / "b.npy").write_bytes(b"\x93NUMPY synthetic")
        (repo / "device" / "unoq" / "c.txt").write_text("not counted\n")
        (repo / mfe.DEPLOY_LIST).write_text("a.py\nb.npy\nc.txt\n")
        self.code = m4_infer.code_sha256(
            [
                repo / "device" / "unoq" / "a.py",
                repo / "device" / "unoq" / "b.npy",
            ]
        )
        self.write_decisions(repo)
        return repo

    def write_decisions(self, repo, d37=None, d38=None, d41=None) -> None:
        if d37 is None:
            d37 = (
                "# 0037（合成）\n\n```\n"
                + "\n".join(fixed_eval.m4_expected_lines())
                + "\n```\n"
            )
        if d38 is None:
            d38 = (
                "# 0038（合成）\n\n```\n"
                f"M4-CONFIG: {fixed_eval.M4_CONFIG}\nM4-MODEL: {self.model_value}\n```\n"
            )
        if d41 is None:
            d41 = f"# 0041（合成）\n\n- `M4-EVAL-PROC:` の行\n\n```\n{PROC_LINE}\n```\n"
        for rel, text in (
            (fixed_eval.M4_DECISION, d37),
            (fixed_eval.M4_CONFIG_DECISION, d38),
            (mfe.PROC_DECISION, d41),
        ):
            p = repo / rel
            if text is False:
                if p.exists():
                    p.unlink()
            else:
                p.write_text(text, encoding="utf-8")

    def write_session(
        self,
        name: str,
        events=None,
        probs=None,
        meta_over=None,
        detect_edit=None,
        feat_edit=None,
    ) -> Path:
        d = self.root / name
        d.mkdir(exist_ok=True)
        meta = meta_for(name, self.model_sha, self.code)
        for k, v in (meta_over or {}).items():
            meta[k] = v
        (d / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        write_csv(
            d / "events.csv",
            ["t_ms", "label", "note"],
            [
                [str(t), lb, nt]
                for t, lb, nt in (events or default_events(name))
            ],
        )
        rows = device_rows(
            probs if probs is not None else probs_for(default_runs(name))
        )
        if detect_edit:
            detect_edit(rows)
        write_csv(
            d / "detect.csv",
            ["t_ms", "window_t_ms", "positive", "prob", "led"],
            rows,
        )
        feat = [[r[0], r[1]] + [FEAT_MARK] * 82 for r in rows]
        if feat_edit:
            feat_edit(feat)
        write_csv(
            d / "feat.csv",
            ["t_ms", "window_t_ms"] + [f"f{i}" for i in range(82)],
            feat,
        )
        return d

    def env(self, git=None, extra=()):
        st = contextlib.ExitStack()
        for obj, name, val in (
            (fixed_eval, "REPO_ROOT", self.repo),
            (fixed_eval, "M4_MODEL", self.model_value),
            (train_eval, "_git", git or fake_git()),
            (mfe, "_now", lambda: NOW),
            *extra,
        ):
            st.enter_context(mock.patch.object(obj, name, val))
        return st

    @contextlib.contextmanager
    def watch_opens(self):
        """root の中のファイルを開いたら log に記録する（builtins.open と
        io.open。pathlib は io.open を使う）。印の作成（os.open）も記録する。"""
        log: list = []
        root = self.root.resolve()
        marker = self.marker.resolve()
        real_open, real_io_open, real_os_open = builtins.open, io.open, os.open

        def rec(p):
            try:
                rp = Path(p).resolve()
            except TypeError:
                return
            if rp == root or root in rp.parents:
                log.append(("open", str(rp.relative_to(root))))

        def b_open(file, *a, **kw):
            rec(file)
            return real_open(file, *a, **kw)

        def i_open(file, *a, **kw):
            rec(file)
            return real_io_open(file, *a, **kw)

        def o_open(path, flags, *a, **kw):
            if Path(path).resolve() == marker:
                log.append(("marker",))
            return real_os_open(path, flags, *a, **kw)

        with (
            mock.patch("builtins.open", b_open),
            mock.patch("io.open", i_open),
            mock.patch("os.open", o_open),
        ):
            yield log

    def run_entry(self, git=None, extra=(), sink=None) -> tuple:
        out, err = io.StringIO(), io.StringIO()
        with self.env(git, extra):
            code = mfe.run(
                self.root,
                "python analysis/m4_final_eval.py data/raw",
                out,
                err,
                sink,
            )
        return code, out.getvalue(), err.getvalue()

    def assert_clean_output(self, out: str, err: str) -> None:
        both = out + err
        for w in (*PROB_MARKS, FEAT_MARK, *FORBIDDEN):
            self.assertNotIn(w, both)

    def assert_no_numbers(self, out: str) -> None:
        for w in NUMBER_WORDS:
            self.assertNotIn(w, out)


class E1PreflightTest(Case):
    """E1: 実行前の検査で止まる。root の中を 1 つも開かず、印を作らない。"""

    def check_stop(self, want: str, git=None, extra=()):
        with self.watch_opens() as log:
            code, out, err = self.run_entry(git, extra)
        self.assertEqual(code, 1)
        self.assertEqual(log, [])
        self.assertFalse(self.marker.exists() or self.marker.is_symlink())
        self.assertEqual(out, "")
        self.assertIn("未完了:", err)
        self.assertIn("印の前", err)
        self.assertIn(want, err)

    def test_git(self):
        self.check_stop(
            "未コミットの変更", git=fake_git(tracked=" M analysis/x.py\n")
        )
        self.check_stop("未追跡", git=fake_git(paths="?? device/unoq/new.py\n"))
        self.check_stop("git が使えません", git=fake_git(paths=None))

    def test_decisions(self):
        cases = [
            (dict(d37="# 0037\n\nM4-EVAL: x\n"), "M4- の行"),
            (dict(d38="# 0038\n\nM4-CONFIG: x\nM4-MODEL: y\n"), "M4-CONFIG"),
            (dict(d41=False), "0041"),
            (dict(d41=f"```\n{PROC_LINE} \n{PROC_LINE}\n```\n"), "PROC_LINE"),
            (
                dict(
                    d41="```\n"
                    + PROC_LINE.replace("fn_near3s", "fn_near2s")
                    + "\n```\n"
                ),
                "PROC_LINE",
            ),
        ]
        for kw, want in cases:
            with self.subTest(kw=list(kw)):
                self.write_decisions(self.repo, **kw)
                self.check_stop(want)
                self.write_decisions(self.repo)
        for rel in (
            fixed_eval.M4_DECISION,
            fixed_eval.M4_CONFIG_DECISION,
            mfe.PROC_DECISION,
        ):
            with self.subTest(untracked=rel):
                self.check_stop(
                    "追跡されていません", git=fake_git(untracked=[rel])
                )

    def test_eval_list_none(self):
        # 0037 の M4-EVAL の行も「追記の前」の文にそろえ、一覧の検査に届かせる
        with mock.patch.object(fixed_eval, "M4_EXPECTED_EVAL", None):
            d37 = (
                "```\n" + "\n".join(fixed_eval.m4_expected_lines()) + "\n```\n"
            )
        self.write_decisions(self.repo, d37=d37)
        self.check_stop(
            "（M4-EVAL）がまだありません",
            extra=[(fixed_eval, "M4_EXPECTED_EVAL", None)],
        )

    def test_marker_exists(self):
        self.marker.write_text("x")
        with self.watch_opens() as log:
            code, _, err = self.run_entry()
        self.assertEqual((code, log), (1, []))
        self.assertIn("印", err)
        self.marker.unlink()
        self.marker.symlink_to(self.tmp / "nowhere")
        with self.watch_opens() as log:
            code, _, err = self.run_entry()
        self.assertEqual((code, log), (1, []))
        self.assertTrue(self.marker.is_symlink())

    def test_model_sha(self):
        (self.repo / "analysis" / "models" / "m4-0038.npz").write_bytes(
            b"changed"
        )
        self.check_stop("sha256")

    def test_folders(self):
        target = self.root / NAMES[2]
        moved = self.tmp / "moved"
        target.rename(moved)
        self.check_stop("ありません")
        target.symlink_to(moved, target_is_directory=True)
        self.check_stop("シンボリックリンク")


class E2MarkerTest(Case):
    """E2: 印は root の中のどのファイルよりも先に作られる。中身。失敗と 2 回目。"""

    def test_marker_first_and_content(self):
        with self.watch_opens() as log:
            code, out, err = self.run_entry()
        self.assertEqual(code, 0, err)
        self.assertEqual(log[0], ("marker",))
        opened = [e[1] for e in log if e[0] == "open"]
        self.assertEqual(opened[0], f"{NAMES[0]}/meta.json")
        self.assertEqual(sum(1 for e in log if e[0] == "marker"), 1)
        text = self.marker.read_text(encoding="utf-8")
        lines = text.splitlines()
        self.assertEqual(lines[0], mfe.MARKER_HEAD)
        self.assertEqual(
            lines[1], f"started: {NOW.isoformat(timespec='seconds')}"
        )
        self.assertEqual(lines[2], f"commit: {HEAD}")
        self.assertEqual(lines[3], "decisions: 0037 0038 0041")
        self.assertEqual(
            lines[4], "command: python analysis/m4_final_eval.py data/raw"
        )
        self.assertEqual(lines[5], "sessions: " + " ".join(NAMES))
        for w in NUMBER_WORDS + ("満たす", "回/分"):
            self.assertNotIn(w, text)

    def test_marker_failure_opens_nothing(self):
        real = os.open
        marker = self.marker.resolve()

        def deny(path, flags, *a, **kw):
            if Path(path).resolve() == marker:
                raise PermissionError("denied")
            return real(path, flags, *a, **kw)

        with self.watch_opens() as log, mock.patch("os.open", deny):
            code, out, err = self.run_entry()
        self.assertEqual(code, 1)
        self.assertEqual([e for e in log if e[0] == "open"], [])
        self.assertFalse(self.marker.exists())
        self.assertIn("作れません", err)
        self.assertEqual(out, "")

    def test_second_run_stops(self):
        code, _, _ = self.run_entry()
        self.assertEqual(code, 0)
        with self.watch_opens() as log:
            code, out, err = self.run_entry()
        self.assertEqual((code, log, out), (1, [], ""))
        self.assertIn("採点し直しません", err)


class E3AfterMarkerStopTest(Case):
    """E3: 印の後・数字の前に止まる。数字の行が無く、印は固定のパスに残る。"""

    def check(self, want: str, **session_kw):
        name = session_kw.pop("name", NAMES[1])
        self.write_session(name, **session_kw)
        code, out, err = self.run_entry()
        self.assertEqual(code, 1, err)
        self.assert_no_numbers(out)
        self.assertNotIn(mfe.COMPLETE_LINE, out)
        self.assertIn("未完了:", err)
        self.assertIn(want, err)
        self.assertTrue(self.marker.is_file())
        self.assertIn(HEAD, out)  # 実行の条件は出ている
        self.assert_clean_output(out, err)
        return err

    def test_meta(self):
        cases = [
            (dict(fw="detector"), "キー fw"),
            (
                dict(m4_config=fixed_eval.M4_CONFIG.replace("0.85", "0.95")),
                "キー m4_config",
            ),
            (
                dict(model={"path": "m4-0038.npz", "sha256": "0" * 64}),
                "キー model.sha256",
            ),
            (
                dict(feature_names=list(features_np.FEAT82_NAMES)[:-1]),
                "キー feature_names",
            ),
            (dict(rpc=True), "キー rpc"),
            (dict(hop_ms=500), "キー hop_ms"),
            (
                dict(
                    unoq_detector={
                        "header": {"code_sha256": "f" * 64},
                        "end": None,
                    }
                ),
                "code_sha256",
            ),
        ]
        for over, want in cases:
            with self.subTest(over=list(over)):
                self.setUp()
                self.check(want, meta_over=over)

    def test_events(self):
        base = default_events(NAMES[1])
        no_start = [e for e in base if e[2] != "start"]
        cases = [
            (no_start, "ちょうど 1 つではない（0 個）"),
            (base + [(5000, "o", "start")], "ちょうど 1 つではない（2 個）"),
            (base + [(2500, "s", "")], "o start より前に s"),
            (base + [(2500, "t", "")], "o start より前に t"),
            (base + [(2500, "c", "")], "o start より前に c"),
            ([(500, "o", "start")] + base[1:], "最初の窓の開始より前"),
            (
                base + [(window_ms(N_WIN - 1) + 1000 - 500, "s", "")],
                "終わりの 1.5 秒以内",
            ),
        ]
        for ev, want in cases:
            with self.subTest(want=want):
                self.setUp()
                self.check(want, events=sorted(ev))

    def test_grid_and_feat_subset(self):
        def drop(rows):
            del rows[50]

        self.check("隣との差が 250 ms ではない", detect_edit=drop)
        self.setUp()

        def shift(feat):
            feat[60][1] = str(int(feat[60][1]) + 1)

        self.check("detect.csv に無い窓", feat_edit=shift)


class E4RangeTest(Case):
    """E4: 記録の範囲（o start から）。範囲の前の塊は数えない。"""

    def test_hand_counted(self):
        sink: list = []
        code, out, err = self.run_entry(sink=sink)
        self.assertEqual(code, 0, err)
        r = sink[0]
        s = next(x for x in r.sessions if x.scored.name == NAMES[1])
        m = r.per_session[NAMES[1]]
        self.assertEqual(
            (m["swallows"], m["detected"], m["false_positive_runs"]), (3, 3, 2)
        )
        t1 = window_ms(N_WIN - 1) + 1000 + LAG
        self.assertAlmostEqual(s.scored.duration_s, t1 / 1000 - O_START / 1000)
        self.assertEqual(s.scored.t0_s, O_START / 1000)
        self.assertEqual(s.n_before, 8)
        self.assertEqual(len(s.scored.rows), N_WIN - 8)
        self.assertEqual(len(s.scored.positive_windows()), 3 + 3 + 5 + 5 + 5)
        # 全行で数えると範囲の前の塊が誤検出・c に紐づく塊・動作に入る
        full = evaluate.event_metrics(
            s.full.swallows,
            s.full.positive_windows(),
            s.full.duration_s,
            s.full.t0_s,
        )
        self.assertEqual(full["false_positive_runs"], 3)
        self.assertEqual(ed.cough_linked(s.full)["linked_runs"], 2)
        p = r.parts[NAMES[1]]
        self.assertEqual(p["cough"]["linked_runs"], 1)
        self.assertEqual(p["cough"]["fp_runs"], 2)
        self.assertEqual(p["main"]["motion"], 1)
        self.assertEqual(p["main"]["meal_conv"], 1)
        # 常時陽性・陽性窓の割合は範囲の内
        always = train_eval.sum_metrics(
            {
                x.scored.name: evaluate.event_metrics(
                    x.scored.swallows,
                    x.scored.all_windows(),
                    x.scored.duration_s,
                    x.scored.t0_s,
                )
                for x in r.sessions
            }
        )
        self.assertEqual(r.always, always)
        self.assertEqual(r.all_windows, 4 * (N_WIN - 8))
        self.assertEqual(
            r.pos_windows,
            sum(len(x.scored.positive_windows()) for x in r.sessions),
        )


class E4bInRangeTest(unittest.TestCase):
    """E4b: 範囲の内の SessionData（入口を通さない）。"""

    def test_in_range(self):
        runs = [(2000, 2500), (3250, 3750), (19500, 20500)]
        probs = probs_for(runs, n=120)
        rows = [
            ed.DetectRow(
                t_ms=int(r[0]),
                window_t_ms=int(r[1]),
                positive=int(r[2]),
                prob=float(r[3]),
                led=int(r[4]),
            )
            for r in device_rows(probs)
        ]
        full = ed.SessionData(
            name="x",
            dir=Path("."),
            cond="meal",
            meta={},
            rows=rows,
            has_feat=True,
            feat_missing=0,
            dims=82,
            swallows=[20.0],
            coughs=[2.4, 3.25],
            conv=False,
            t0_s=1.0,
            duration_s=30.0,
        )
        t1_ms = window_ms(119) + 1000 + LAG
        d = mfe.in_range(full, O_START, t1_ms)
        self.assertEqual(len(full.rows), 120)
        self.assertEqual(full.coughs, [2.4, 3.25])
        self.assertEqual((full.t0_s, full.duration_s), (1.0, 30.0))
        self.assertTrue(all(r.window_t_ms >= O_START for r in d.rows))
        self.assertEqual(len(d.rows), 120 - 8)
        self.assertEqual(d.coughs, [3.25])
        self.assertEqual(d.t0_s, 3.0)
        self.assertAlmostEqual(d.duration_s, t1_ms / 1000 - 3.0)
        self.assertEqual(ed.cough_linked(full)["linked_runs"], 2)
        self.assertEqual(
            ed.cough_linked(d),
            {"coughs": 1, "fp_runs": 1, "linked_runs": 1, "coughs_with_run": 1},
        )
        ev = mfe.Events(
            o_start_ms=O_START,
            swallows=[20.0],
            markers={"t": [2.0, 4.0], "c": [2.4, 3.25], "n": []},
            conv=False,
        )
        info = mfe.range_info(ev, O_START)
        self.assertEqual(info.markers, {"t": [4.0], "c": [3.25], "n": []})
        # 範囲の前の塊は誤検出の塊に入らない（全行では 2 つ、範囲の内では c の塊だけ）
        self.assertEqual(
            len(m4_compare.fp_runs(full.swallows, full.positive_windows())), 2
        )
        runs = m4_compare.fp_runs(d.swallows, d.positive_windows())
        self.assertEqual(runs, [[3.25, 3.5, 3.75]])
        self.assertEqual(
            m4_compare.classify_run(runs[0], d.swallows, info),
            ("motion", {"c", "t"}),
        )
        always = evaluate.event_metrics(
            d.swallows, d.all_windows(), d.duration_s, d.t0_s
        )
        self.assertEqual(always["false_positive_runs"], 1)
        self.assertTrue(all(w >= 3.0 for w in d.all_windows()))


class E4cSafeReadTest(Case):
    """E4c: 値を出さない読み込み。目印の文字列と行の中身が出ない。"""

    def check(self, want: list, hidden: list, complete=False, **session_kw):
        self.write_session(NAMES[2], **session_kw)
        code, out, err = self.run_entry()
        both = self.last_output = out + err
        for h in hidden:
            self.assertNotIn(h, both)
        self.assert_clean_output(out, err)
        if complete:
            self.assertEqual(code, 0, err)
            self.assertTrue(out.rstrip().endswith(mfe.COMPLETE_LINE))
            return
        self.assertEqual(code, 1)
        self.assert_no_numbers(out)
        for w in want:
            self.assertIn(w, err)

    def test_detect(self):
        def edit(i, col=None, value=None, cut=False):
            def f(rows):
                self.row_text = ",".join(rows[i - 1])
                if cut:
                    rows[i - 1] = rows[i - 1][:4]
                else:
                    rows[i - 1][col] = value

            return f

        cases = [
            (edit(10, cut=True), ["detect.csv 行 10", "列数"], []),
            (
                edit(11, 3, "0.98765x"),
                ["detect.csv 行 11 列 prob", "浮動小数"],
                ["0.98765x", "98765"],
            ),
            (
                edit(12, 3, "1.23456789"),
                ["detect.csv 行 12 列 prob", "[0, 1] の外"],
                ["1.23456789"],
            ),
            (edit(13, 2, "7"), ["detect.csv 行 13 列 positive"], []),
            (edit(14, 4, "5"), ["detect.csv 行 14 列 led"], []),
            (
                edit(15, 1, str(window_ms(13))),
                ["detect.csv 行 15 列 window_t_ms", "逆行"],
                [],
            ),
        ]
        for f, want, hidden in cases:
            with self.subTest(want=want[0]):
                self.setUp()
                self.row_text = None
                self.check(want, hidden, detect_edit=f)
                # 元の行の中身（カンマでつないだ文字列）も出ない
                self.assertIsNotNone(self.row_text)
                self.assertNotIn(self.row_text, self.last_output)

    def test_feat(self):
        def short(feat):
            feat[30] = feat[30][:-1]

        self.check(["feat.csv 行 31", "列数"], [], feat_edit=short)
        self.setUp()

        def bad(feat):
            feat[40][5] = "zz9.87654"

        self.check([], ["zz9.87654", "9.87654"], complete=True, feat_edit=bad)

    def test_event_metrics_error_message_hidden(self):
        boom = mock.patch.object(
            evaluate,
            "event_metrics",
            side_effect=ValueError("SECRET 0.4242 at q777q"),
        )
        with boom:
            code, out, err = self.run_entry()
        self.assertEqual(code, 1)
        self.assertIn("ValueError", err)
        self.assertIn("evaluate.event_metrics", err)
        for h in ("SECRET", "0.4242", "q777q", "Traceback"):
            self.assertNotIn(h, out + err)
        self.assert_no_numbers(out)


class E5AggregateTest(Case):
    """E5: 合算は evaluate.aggregate と同じ。キーは M4_EXPECTED_EVAL の順。"""

    def test_aggregate(self):
        sink: list = []
        calls: list = []
        real = evaluate.aggregate

        def agg(per):
            calls.append(list(per))
            return real(per)

        code, out, err = self.run_entry(
            extra=[(evaluate, "aggregate", agg)], sink=sink
        )
        self.assertEqual(code, 0, err)
        r = sink[0]
        self.assertEqual(calls, [list(NAMES)])
        self.assertEqual(r.total, real(r.per_session))
        self.assertEqual(r.total["sessions"], list(NAMES))
        c = r.total["confusion"]
        self.assertIsNone(c["tn"])
        self.assertEqual((c["tp"], c["fn"], c["fp"]), (14, 2, 12))
        self.assertIn(f"| 嚥下 | TP {c['tp']} | FN {c['fn']} |", out)
        self.assertIn(f"| 非嚥下区間 | FP {c['fp']}（回） | — |", out)
        self.assertNotIn(
            "TN ", out.replace("TN は定義できないので出さない", "")
        )
        self.assertIn("- 評価に使ったセッション: " + ", ".join(NAMES), out)


class E6AgreementTest(Case):
    """E6: 装置と PC の一致（.9g の prob から再計算）。"""

    # 境の値・不応期で抑えた塊・ヒステリシスの状態の持ち越し
    OVERRIDE = {
        100: 0.85,
        101: 0.65,
        102: 0.65 - 1e-9,
        110: 0.85 - 1e-9,
        111: 0.85 + 1e-9,
        112: 0.1,
        113: 0.9,
        114: 0.9,
        120: 0.9,
        121: 0.7,
        122: 0.7,
        123: 0.5,
    }

    def probs(self):
        return probs_for(default_runs(NAMES[3]), override=self.OVERRIDE)

    def agree(self, **kw):
        self.write_session(NAMES[3], probs=self.probs(), **kw)
        sink: list = []
        code, out, err = self.run_entry(sink=sink)
        self.assertEqual(code, 0, err)
        return sink[0].agree[NAMES[3]], out

    def test_zero_mismatch_and_boundaries(self):
        rows = device_rows(self.probs())
        pos = {k: int(rows[k][2]) for k in self.OVERRIDE}
        self.assertEqual((pos[100], pos[101], pos[102]), (1, 1, 0))
        self.assertEqual((pos[110], pos[111]), (0, 1))
        self.assertEqual((pos[113], pos[114]), (0, 0))  # 不応期で抑えた
        self.assertEqual((pos[120], pos[121], pos[122], pos[123]), (1, 1, 1, 0))
        a, out = self.agree()
        self.assertEqual(
            {
                k: a[k]
                for k in ("dev1_pc0", "dev0_pc1", "led_stepper", "led_states")
            },
            {"dev1_pc0": 0, "dev0_pc1": 0, "led_stepper": 0, "led_states": 0},
        )
        self.assertEqual(a["windows"], N_WIN)
        self.assertEqual(a["round_candidates"], 5)
        self.assertIn("丸めの候補の窓", out)

    def test_flip_counts_one_each(self):
        def flip(rows, k, v):
            rows[k][2] = v

        a, _ = self.agree(detect_edit=lambda rows: flip(rows, 100, "0"))
        self.assertEqual((a["dev1_pc0"], a["dev0_pc1"]), (0, 1))
        self.setUp()
        a, _ = self.agree(detect_edit=lambda rows: flip(rows, 50, "1"))
        self.assertEqual((a["dev1_pc0"], a["dev0_pc1"]), (1, 0))
        self.setUp()

        def led(rows):
            rows[60][4] = "2" if rows[60][4] == "1" else "1"

        a, _ = self.agree(detect_edit=led)
        self.assertEqual((a["led_stepper"], a["led_states"]), (1, 1))
        self.assertEqual((a["dev1_pc0"], a["dev0_pc1"]), (0, 0))

    def test_round_candidates_counting(self):
        full = ed.SessionData(
            name="x",
            dir=Path("."),
            cond="meal",
            meta={},
            rows=[
                ed.DetectRow(
                    t_ms=2000 + 250 * k,
                    window_t_ms=1000 + 250 * k,
                    positive=0,
                    prob=p,
                    led=1,
                )
                for k, p in enumerate(
                    [
                        0.1,
                        0.85 + 9e-9,
                        0.85 - 2e-8,
                        0.65 - 9e-9,
                        0.65 + 5e-9,
                        0.2,
                    ]
                )
            ],
            has_feat=True,
            feat_missing=0,
            dims=82,
            swallows=[],
            coughs=[],
            conv=False,
            t0_s=1.0,
            duration_s=3.0,
        )
        self.assertEqual(mfe.agreement(full, CFG)["round_candidates"], 3)

    def test_stepper_post_mask_disagree_stops(self):
        real = postprocess.post_mask

        def wrong(*a, **kw):
            m = real(*a, **kw)
            m[0] = not m[0]
            return m

        code, out, err = self.run_entry(
            extra=[(postprocess, "post_mask", wrong)]
        )
        self.assertEqual(code, 1)
        self.assertIn("PostStepper と post_mask", err)
        self.assert_no_numbers(out)


class E7BreakdownTest(Case):
    """E7: 誤検出の内訳の合計 = 誤検出の回数。見逃しの近く / 無し。塊の長さ。"""

    def test_rich_session(self):
        sink: list = []
        code, out, err = self.run_entry(sink=sink)
        self.assertEqual(code, 0, err)
        r = sink[0]
        m, p = r.per_session[RICH], r.parts[RICH]
        self.assertEqual(
            (m["swallows"], m["detected"], m["false_positive_runs"]), (7, 5, 6)
        )
        self.assertEqual(
            p["main"],
            {
                "after": 1,
                "before": 1,
                "motion": 2,
                "meal_conv": 2,
                "meal_quiet": 0,
                "other": 0,
            },
        )
        self.assertEqual(sum(p["main"].values()), m["false_positive_runs"])
        self.assertEqual(p["sub"], {"t": 0, "c": 1, "n": 1})
        self.assertEqual((p["missed"], p["missed_near"]), (2, 1))
        self.assertEqual(
            sorted(p["len_with_s"]), [1.25, 1.25, 1.25, 2.25, 2.25]
        )
        self.assertEqual(
            sorted(p["len_fp"]), [0.5, 0.5, 0.75, 0.75, 2.25, 2.25]
        )
        quiet = r.parts[NAMES[2]]["main"]
        self.assertEqual((quiet["meal_quiet"], quiet["motion"]), (1, 1))
        for n in NAMES:
            self.assertEqual(
                sum(r.parts[n]["main"].values()),
                r.per_session[n]["false_positive_runs"],
            )
        self.assertIn("ある: 1、無い: 1", out)
        self.assertIn("嚥下を含む塊: 14 個", out)
        self.assertIn("誤検出の塊: 12 個", out)

    def test_breakdown_mismatch_stops(self):
        code, out, err = self.run_entry(
            extra=[(m4_compare, "fp_runs", lambda s, p: [])]
        )
        self.assertEqual(code, 1)
        self.assertIn("誤検出の内訳の合計", err)
        self.assert_no_numbers(out)


class E8ReportTest(Case):
    """E8: 報告の形・語・出さない値。"""

    def test_report(self):
        seen: list = []

        def rec(fn):
            def f(x, *a):
                seen.append(x)
                return fn(x, *a)

            return f

        extra = [
            (mfe, "float", rec(builtins.float)),
            (mfe, "int", rec(builtins.int)),
        ]
        with contextlib.ExitStack() as st:
            for obj, name, val in extra:
                st.enter_context(mock.patch.object(obj, name, val, create=True))
            code, out, err = self.run_entry()
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertEqual(out.rstrip().splitlines()[-1], mfe.COMPLETE_LINE)
        self.assertTrue("満たす" in out or "満たさない" in out)
        self.assertIn("合格線 0.85 以上", out)
        self.assertIn("合格線 1.0 回/分以下", out)
        self.assert_clean_output(out, err)
        led_lines = [ln for ln in out.splitlines() if "LED" in ln]
        self.assertTrue(led_lines)
        for ln in led_lines:
            self.assertIn("目安", ln)
            self.assertNotIn("判定", ln)
        self.assertIn("混同行列", out)
        self.assertIn("| 嚥下 | TP ", out)
        for n in NAMES:
            self.assertIn(f"| {n} |", out)
        # feat.csv の 3 列目より後は変換しない（先頭 2 列と detect.csv の値だけ）
        self.assertTrue(seen)
        self.assertNotIn(FEAT_MARK, seen)
        self.assertEqual(sum(1 for x in seen if x == FEAT_MARK), 0)
        # 順: 5.1 → 5.2 → 5.3 → 5.4 → 5.5
        heads = [
            "## 評価に使ったセッション",
            "## 合算",
            "## 合格線との比較",
            "## 参考値",
            "## 装置と PC の一致",
            "## 届かなかった場合の切り分け",
            "## M2・M3 と並べる",
            "## 注記",
        ]
        pos = [out.index(h) for h in heads]
        self.assertEqual(pos, sorted(pos))

    def test_reference_constants_match_decisions(self):
        def read(prefix):
            p = next((REAL_REPO / "docs" / "decisions").glob(f"{prefix}-*.md"))
            return p.read_text(encoding="utf-8")

        d27, d36, d38 = read("0027"), read("0036"), read("0038")
        m2, m3 = mfe.M2_REF, mfe.M3_REF
        self.assertIn(
            f"検出率 {m2['det']}（TP {m2['tp']} / FN {m2['n'] - m2['tp']}）",
            d27,
        )
        self.assertIn(
            f"誤検出率 {m2['fp_rate']} 回/分（FP {m2['fp']} 回 / 非嚥下 {m2['min']} 分）",
            d27,
        )
        self.assertIn(f"`s` 合計 {m2['n']}", d27)
        self.assertIn(f"| 検出率 | {m3['det']}（{m3['tp']}/{m3['n']}）", d36)
        self.assertIn(
            f"| 誤検出率 | {m3['fp_rate']} 回/分（{m3['fp']} 回 / {m3['min']} 分）",
            d36,
        )
        cv, cvm = mfe.CV_REF, mfe.CV_MEAL_REF
        self.assertIn(
            f"検出率 {cv['det']}（{cv['tp']}/{cv['n']}）、誤検出率 {cv['fp_rate']} 回/分",
            d38,
        )
        self.assertIn(
            f"`meal` だけ: 検出率 {cvm['det']}（{cvm['tp']}/{cvm['n']}）、誤検出率 {cvm['fp_rate']} 回/分",
            d38,
        )


class _FailingOut(io.StringIO):
    """write が limit 文字を超えたら、または flush_fail で報告の末尾を書いた後の
    flush で OSError を投げる出力先。メッセージに目印を入れる（出てはいけない）。"""

    MESSAGE = "SECRETWRITE 0.951234568 No space left"

    def __init__(self, limit=None, flush_fail=False):
        super().__init__()
        self.limit, self.flush_fail = limit, flush_fail

    def write(self, s):
        if (
            self.limit is not None
            and len(self.getvalue()) + len(s) > self.limit
        ):
            super().write(s[: max(0, self.limit - len(self.getvalue()))])
            raise OSError(28, self.MESSAGE)
        return super().write(s)

    def flush(self):
        # "first": 中身のある最初の flush（実行の条件の後）、"end": 完了の行の後
        v = self.getvalue()
        if (self.flush_fail == "first" and v) or (
            self.flush_fail == "end" and mfe.COMPLETE_LINE in v
        ):
            raise BrokenPipeError(32, self.MESSAGE)
        return super().flush()


class E8bWriteFailureTest(Case):
    """報告の書き出しの失敗（adversarial-review 1 回目の指摘）。印は残り、出力は未完了。"""

    def header_len(self) -> int:
        out = io.StringIO()
        with self.env():
            pre = mfe.preflight(self.root)
        out.write(
            "\n".join(
                mfe.header_lines(
                    self.root, pre, "python analysis/m4_final_eval.py data/raw"
                )
            )
            + "\n"
        )
        return len(out.getvalue())

    def check(self, out: _FailingOut, stage: str) -> None:
        err = io.StringIO()
        with self.env():
            code = mfe.run(
                self.root, "python analysis/m4_final_eval.py data/raw", out, err
            )
        e = err.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("未完了:", e)
        self.assertIn(stage, e)
        for h in (
            "SECRETWRITE",
            "No space",
            "Traceback",
            *PROB_MARKS,
            FEAT_MARK,
        ):
            self.assertNotIn(h, out.getvalue() + e)
        self.assertTrue(self.marker.is_file())
        self.assertNotIn(mfe.COMPLETE_LINE, out.getvalue())

    def test_write_fails_midway(self):
        out = _FailingOut(limit=self.header_len() + 500)
        self.check(out, "7.5（報告の書き出し）（OSError）")
        self.assertIn(
            "## 評価に使ったセッション", out.getvalue()
        )  # 途中まで出た

    def test_flush_fails(self):
        out = _FailingOut(flush_fail="first")
        self.check(out, "7.3 の後（実行の条件の書き出し）（BrokenPipeError）")

    def test_flush_fails_after_complete_line(self):
        out = _FailingOut(flush_fail="end")
        err = io.StringIO()
        with self.env():
            code = mfe.run(
                self.root, "python analysis/m4_final_eval.py data/raw", out, err
            )
        e = err.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("未完了: 7.5（報告の書き出し）（BrokenPipeError）", e)
        for h in ("SECRETWRITE", "Traceback", *PROB_MARKS, FEAT_MARK):
            self.assertNotIn(h, out.getvalue() + e)
        self.assertTrue(self.marker.is_file())
        # 完了の行は書かれたが flush が失敗した（受け手に届いたとは言えない）ので終了コードは 1

    def test_stderr_failure_is_swallowed(self):
        class Dead(io.StringIO):
            def write(self, s):
                raise OSError("dead stderr")

        with self.env():
            code = mfe.run(
                self.root,
                "python analysis/m4_final_eval.py data/raw",
                _FailingOut(limit=10),
                Dead(),
            )
        self.assertEqual(code, 1)
        self.assertTrue(self.marker.is_file())


# 子プロセスで入口を走らせるラッパ（合成のリポジトリ・git の差し替えを環境変数
# M4FE_CHILD の JSON で受ける）。標準出力のパイプを小さくし（Linux の
# F_SETPIPE_SZ）、受け手が閉じた後の書き出しが確実に失敗するようにする
CHILD_SCRIPT = r"""
import fcntl, json, os, sys
from pathlib import Path
c = json.loads(os.environ["M4FE_CHILD"])
sys.path.insert(0, c["analysis"])
try:
    fcntl.fcntl(1, 1031, 4096)  # F_SETPIPE_SZ
except OSError:
    pass
import fixed_eval, train_eval, m4_final_eval as mfe
fixed_eval.REPO_ROOT = Path(c["repo"])
fixed_eval.M4_MODEL = c["model"]

def git(*args):
    if args[0] == "rev-parse":
        return c["head"] + "\n"
    if args[0] == "status":
        return ""
    if args[0] == "ls-files":
        return args[-1] + "\n"
    raise AssertionError(args)

train_eval._git = git
if c["wait_close"]:
    # 受け手が閉じるまで待つ（実行の条件の書き出しがバッファに残ったまま失敗する形）
    import select
    _p = select.poll()
    _p.register(1, select.POLLERR)
    _p.poll(20000)
mfe.main([c["root"]])
"""


class E8cClosedPipeTest(Case):
    """標準出力の受け手が先に閉じた（実際のパイプ）。adversarial-review 2 回目の
    指摘。終了時の再 flush で「Exception ignored」・終了コード 120 にならない。"""

    def child(self, wait_close: bool, read_n: int) -> tuple:
        """(読めたバイト, 終了コード, 標準エラー)。read_n バイト読んでから
        標準出力のパイプを閉じる（0 なら読まずに閉じる）。"""
        script = self.tmp / "child.py"
        script.write_text(CHILD_SCRIPT, encoding="utf-8")
        env = dict(os.environ)
        env["M4FE_CHILD"] = json.dumps(
            dict(
                analysis=str(HERE),
                repo=str(self.repo),
                model=self.model_value,
                head=HEAD,
                root=str(self.root),
                wait_close=wait_close,
            )
        )
        p = subprocess.Popen(
            [sys.executable, "-I", str(script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=self.tmp,
        )
        try:
            first = p.stdout.read(read_n) if read_n else b""
            p.stdout.close()  # 受け手が先に閉じる（head -c と同じ）
            err = p.stderr.read().decode("utf-8", "replace")
            code = p.wait(timeout=120)
        finally:
            if p.poll() is None:
                p.kill()
            p.stderr.close()
        return first, code, err

    def check(self, code: int, err: str, stage: str) -> None:
        self.assertEqual(code, 1, err)  # 120（終了時の再 flush の失敗）ではない
        self.assertIn("未完了:", err)
        self.assertIn(f"{stage}（BrokenPipeError）", err)  # 型の名前だけ
        for h in (
            "Exception ignored",
            "Traceback",
            "Broken pipe",
            "Errno",
            *PROB_MARKS,
            FEAT_MARK,
        ):
            self.assertNotIn(h, err)
        self.assertTrue(self.marker.is_file())

    def test_closed_before_first_write(self):
        # 実行の条件がバッファに残ったまま flush が失敗する（記述子を /dev/null に
        # 差し替えないと、終了時に「Exception ignored」と終了コード 120 になる）
        _, code, err = self.child(wait_close=True, read_n=0)
        self.check(code, err, "7.3 の後（実行の条件の書き出し）")

    def test_reader_closes_after_some_bytes(self):
        first, code, err = self.child(wait_close=False, read_n=64)
        self.assertTrue(first)
        # 段は閉じた時点による（ふつうは 7.5 の報告の書き出し）
        self.check(code, err, "")


class E9ArgsTest(Case):
    """E9: 引数は root だけ。印のほかのファイルができない。"""

    def test_args(self):
        for argv in (
            [],
            [str(self.root), "--reproduce"],
            [str(self.root), "extra"],
            [str(self.root), "--sessions", "x"],
        ):
            with (
                self.subTest(argv=argv),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.env(), self.assertRaises(SystemExit) as cm:
                    mfe.main(argv)
                self.assertEqual(cm.exception.code, 2)
                self.assertFalse(self.marker.exists())

    def test_main_and_files(self):
        before_root, before_repo = (
            files_under(self.root),
            files_under(self.repo),
        )
        out = io.StringIO()
        with (
            self.env(),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as cm:
                mfe.main([str(self.root)])
        self.assertEqual(cm.exception.code, 0)
        self.assertTrue(out.getvalue().rstrip().endswith(mfe.COMPLETE_LINE))
        self.assertEqual(files_under(self.root), before_root)
        self.assertEqual(
            sorted(set(files_under(self.repo)) - set(before_repo)),
            [fixed_eval.M4_MARKER],
        )
        self.assertEqual(
            sorted(set(before_repo) - set(files_under(self.repo))), []
        )


class E10ProcLineTest(unittest.TestCase):
    """E10: 0041 の M4-EVAL-PROC の行と 0037 の照合。"""

    def test_constant_and_real_decision(self):
        self.assertEqual(mfe.PROC_LINE, PROC_LINE)
        text = (REAL_REPO / mfe.PROC_DECISION).read_text(encoding="utf-8")
        self.assertEqual(mfe.proc_lines(text), [PROC_LINE])
        self.assertEqual(
            [
                s.strip()
                for s in text.splitlines()
                if s.strip().startswith("M4-")
            ],
            [PROC_LINE],
        )
        self.assertFalse(fixed_eval.m4_lines_match(text))
        self.assertFalse(fixed_eval.m4_config_lines_match(text))
        real37 = (REAL_REPO / fixed_eval.M4_DECISION).read_text(
            encoding="utf-8"
        )
        self.assertNotIn(PROC_LINE, real37)
        self.assertTrue(fixed_eval.m4_lines_match(real37))


class E10EntryTest(Case):
    def test_0041_text_not_passed_to_0037_check(self):
        seen: list = []
        real = fixed_eval.m4_lines_match

        def rec(text):
            seen.append(text)
            return real(text)

        code, _, err = self.run_entry(
            extra=[(fixed_eval, "m4_lines_match", rec)]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(len(seen), 1)
        self.assertNotIn(PROC_PREFIX_TEXT, seen[0])


PROC_PREFIX_TEXT = "M4-EVAL-PROC:"


if __name__ == "__main__":
    unittest.main()
