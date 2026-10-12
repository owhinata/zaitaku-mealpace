"""final_eval.py を合成データで検証する（plan #40 F1〜F13、docs/decisions/0034）。

実行: python -m unittest discover -s analysis -v
合成の咽喉マイクのセッションは tempfile に作る（test_throat_compare.build_root。
2 収集日 × 3 本）。data/ とリポジトリの docs/log/ は使わない。印・図・合成の 0034 は
一時フォルダ（final_eval.REPO_ROOT と visualize.LOG_ROOT を差し替える）に置く。
git の検査は F1〜F12 では train_eval._git を差し替え、F13 では一時フォルダの
git リポジトリで確かめる（このリポジトリの git は使わない）。

ファイル名は test_ei_upload.py より後に並ぶ（test_throat_compare.py の注を参照）。
"""

from __future__ import annotations
import contextlib, io, os, shutil, subprocess, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import numpy as np

import compare, evaluate, features, final_eval, fixed_eval, postprocess
import split
import train_eval, visualize
from test_throat_compare import FORBIDDEN_WORDS, assert_model_equal, build_root
from test_train_eval import synth_throat

NOW = datetime(2026, 10, 13, 10, 0, 0, tzinfo=timezone(timedelta(hours=9)))
HEAD = "0123456789abcdef0123456789abcdef01234567"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# 0034 の FINAL-CONFIG の行（コードから作らずに書く）
CONFIG_LINES = (
    "FINAL-CONFIG J: set=mfe40 classifier=cnn seed=0 post=hyst enter=0.95 exit=0.85",
    "FINAL-CONFIG R: set=a52 classifier=lr post=hyst enter=0.85 exit=0.65",
)
DATA_FNS = (
    (features, "_throat_windows"),
    (evaluate, "load_events"),
    (evaluate, "throat_session_span"),
)
SELECTION_FNS = (
    (train_eval, "threshold_table"),
    (train_eval, "pick_threshold"),
    (train_eval, "cross_validate"),
    (compare, "hysteresis_table"),
    (compare, "pick_hysteresis"),
    (compare, "pick_row"),
    (compare, "make_rows"),
    (compare, "cnn_cross_validate"),
)


def decision_text(train, eval_, config_lines=CONFIG_LINES) -> str:
    return (
        "# 0034（合成）\n\n```\n"
        + "\n".join(config_lines)
        + "\nFINAL-TRAIN: "
        + " ".join(train)
        + "\nFINAL-EVAL: "
        + " ".join(eval_)
        + "\n```\n"
    )


def fake_git(
    tracked_changes: str = "",
    path_changes: str = "",
    decision_tracked: bool = True,
):
    def g(*args):
        if args[:2] == ("rev-parse", "--short"):
            return HEAD[:7] + "\n"
        if args[0] == "rev-parse":
            return HEAD + "\n"
        if args[0] == "status" and "--untracked-files=no" in args:
            return tracked_changes
        if args[0] == "status":
            return path_changes
        if args[0] == "ls-files":
            return args[-1] + "\n" if decision_tracked else None
        raise AssertionError(f"想定していない git の呼び出し: {args}")

    return g


def files_under(p: Path) -> list:
    return sorted(str(x.relative_to(p)) for x in p.rglob("*"))


class FinalCase(unittest.TestCase):
    tmp = None

    @classmethod
    def setUpClass(cls):
        if FinalCase.tmp is not None:
            return
        tmp = tempfile.TemporaryDirectory()
        unittest.addModuleCleanup(tmp.cleanup)
        FinalCase.tmp = Path(tmp.name)
        r = build_root(FinalCase.tmp)
        FinalCase.root, FinalCase.train, FinalCase.eval = r
        FinalCase._n = 0
        FinalCase.run_base()

    # --- 置き場 ---

    @classmethod
    def make_repo(cls, label: str, text: str | None = None) -> Path:
        repo = cls.tmp / "repos" / label
        (repo / "docs" / "decisions").mkdir(parents=True)
        (repo / "docs" / "log").mkdir(parents=True)
        if text is None:
            text = decision_text(cls.train, cls.eval)
        (repo / final_eval.DECISION).write_text(text, encoding="utf-8")
        return repo

    @classmethod
    def env(cls, repo: Path, git=None, now=NOW, train=None, eval_=None):
        st = contextlib.ExitStack()
        for obj, name, val in (
            (final_eval, "REPO_ROOT", repo),
            (visualize, "LOG_ROOT", repo / "docs" / "log"),
            (final_eval, "EXPECTED_TRAIN", tuple(train or cls.train)),
            (final_eval, "EXPECTED_EVAL", tuple(eval_ or cls.eval)),
            (final_eval, "EVAL_MIN", 3),
            (train_eval, "_git", git or fake_git()),
        ):
            st.enter_context(mock.patch.object(obj, name, val))
        st.enter_context(mock.patch.object(final_eval, "_now", lambda: now))
        return st

    @staticmethod
    def data_guard(forbidden=None):
        """forbidden（None なら全部）の名前でデータの読み込みが呼ばれたら落ちる。
        呼ばれた名前を calls に集める。"""
        calls: dict = {}
        st = contextlib.ExitStack()
        for mod, fn in DATA_FNS:
            real = getattr(mod, fn)

            def guard(session, *a, _real=real, _fn=fn, **kw):
                name = Path(session).name
                if forbidden is None or name in forbidden:
                    raise AssertionError(f"{_fn} が呼ばれた: {name}")
                calls.setdefault(_fn, []).append(name)
                return _real(session, *a, **kw)

            st.enter_context(mock.patch.object(mod, fn, side_effect=guard))
        return st, calls

    # --- 基準の 1 回（main。印と図まで） ---

    @classmethod
    def run_base(cls):
        cls.repo = cls.make_repo("base")
        cls.before_root = files_under(cls.root)
        log: list = []
        rec: dict = {
            "score": {"J": [], "R": []},
            "hyst": [],
            "aggregate": [],
            "fine_s": [],
        }
        st = cls.env(cls.repo)
        for mod, fn in SELECTION_FNS:
            st.enter_context(
                mock.patch.object(
                    mod, fn, side_effect=AssertionError(f"{fn} が呼ばれた")
                )
            )
        marker = cls.repo / final_eval.MARKER

        real_tw = features._throat_windows

        def tw(session, *a, **kw):
            name = Path(session).name
            if name in cls.eval and "first_eval" not in rec:
                rec["first_eval"] = dict(
                    marker=marker.read_text(encoding="utf-8")
                    if marker.exists()
                    else None,
                    log=list(log),
                )
            log.append(("read", name))
            return real_tw(session, *a, **kw)

        real_fsync = os.fsync

        def fsync(fd):
            log.append(("fsync",))
            return real_fsync(fd)

        real_fit = final_eval.fit

        def fit(root, train, c):
            log.append(("fit", c.key))
            return real_fit(root, train, c)

        real_score, real_cnn_score = train_eval.score, compare.cnn_score

        def score(model, d):
            rec["score"]["R"].append(d.name)
            return real_score(model, d)

        def cnn_score(model, d):
            rec["score"]["J"].append(d.name)
            return real_cnn_score(model, d)

        real_hyst = postprocess.hysteresis

        def hyst(prob, valid, enter, exit, t):
            rec["hyst"].append((enter, exit))
            return real_hyst(prob, valid, enter, exit, t)

        real_agg = evaluate.aggregate

        def agg(per):
            rec["aggregate"].append(list(per))
            return real_agg(per)

        real_fig_events = visualize.fig_events

        def fig_events(path, title, items):
            rec["fine_s"].append(
                sum(len(it["x"]) for it in items) / visualize.features_v2.SR
            )
            return real_fig_events(path, title, items)

        real_run = final_eval.run

        def run(root, command=None):
            rec["result"] = real_run(root, command)
            return rec["result"]

        for obj, name, fn in (
            (features, "_throat_windows", tw),
            (os, "fsync", fsync),
            (final_eval, "fit", fit),
            (train_eval, "score", score),
            (compare, "cnn_score", cnn_score),
            (postprocess, "hysteresis", hyst),
            (evaluate, "aggregate", agg),
            (visualize, "fig_events", fig_events),
            (final_eval, "run", run),
        ):
            st.enter_context(mock.patch.object(obj, name, side_effect=fn))
        out, err = io.StringIO(), io.StringIO()
        with (
            st,
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            final_eval.main([str(cls.root)])
        cls.stdout = out.getvalue()
        cls.log, cls.rec = log, rec
        cls.result = rec["result"]
        cls.report = final_eval.format_report(cls.result)

    def assert_no_marker(self, repo: Path):
        self.assertFalse((repo / final_eval.MARKER).exists())

    def assert_stops_without_data(self, repo: Path, root=None, **kw):
        """データ（学習側・評価側とも）を開かずに止まり、印を書かない。"""
        st, _ = self.data_guard(None)
        with self.env(repo, **kw), st, self.assertRaises(SystemExit) as cm:
            final_eval.run(root or self.root)
        self.assert_no_marker(repo)
        return str(cm.exception)


class SelectionTest(FinalCase):
    def test_f1_nothing_is_selected_again(self):
        # 基準の実行は選び直しの関数が呼ばれたら落ちる差し替えの下で通った
        self.assertEqual(set(self.rec["hyst"]), {(0.95, 0.85), (0.85, 0.65)})
        self.assertEqual(len(self.rec["hyst"]), 2 * len(self.eval))
        j, r = self.result.configs
        self.assertEqual((j.config.key, j.config.op), ("J", (0.95, 0.85)))
        self.assertEqual((r.config.key, r.config.op), ("R", (0.85, 0.65)))
        self.assertEqual(j.config.seed, 0)

    def test_f2_each_eval_session_is_scored_once_per_config(self):
        for key in ("J", "R"):
            with self.subTest(key):
                self.assertEqual(self.rec["score"][key], self.eval)
        self.assertEqual(self.rec["aggregate"], [self.eval, self.eval])
        for x in self.result.configs:
            self.assertEqual(x.total["sessions"], self.eval)


class StopTest(FinalCase):
    def label(self):
        FinalCase._n += 1
        return f"stop{FinalCase._n}"

    def test_f3a_tracked_changes(self):
        repo = self.make_repo(self.label())
        msg = self.assert_stops_without_data(
            repo, git=fake_git(tracked_changes=" M analysis/compare.py\n")
        )
        self.assertIn("未コミットの変更", msg)

    def test_f3b_untracked_file_in_analysis(self):
        repo = self.make_repo(self.label())
        msg = self.assert_stops_without_data(
            repo, git=fake_git(path_changes="?? analysis/new.py\n")
        )
        self.assertIn("analysis/new.py", msg)

    def test_f3b_git_unavailable(self):
        repo = self.make_repo(self.label())
        self.assert_stops_without_data(repo, git=lambda *a: None)

    def test_f3c_decision_missing_or_untracked(self):
        repo = self.make_repo(self.label())
        msg = self.assert_stops_without_data(
            repo, git=fake_git(decision_tracked=False)
        )
        self.assertIn("追跡されていません", msg)
        (repo / final_eval.DECISION).unlink()
        msg = self.assert_stops_without_data(repo)
        self.assertIn("ありません", msg)

    def test_f3d_final_lines_differ(self):
        variants = (
            decision_text(
                self.train,
                self.eval,
                (CONFIG_LINES[0].replace("0.95", "0.90"), CONFIG_LINES[1]),
            ),
            decision_text(self.train, self.eval, CONFIG_LINES[::-1]),
            decision_text(self.train, self.eval[:2]),
            decision_text(self.train, self.eval, CONFIG_LINES[:1]),
            decision_text(self.train, self.eval)
            + "\nFINAL-CONFIG J: set=mfe40 classifier=cnn seed=1 post=hyst "
            "enter=0.95 exit=0.85\n",
        )
        for text in variants:
            with self.subTest(text[-60:]):
                repo = self.make_repo(self.label(), text)
                msg = self.assert_stops_without_data(repo)
                self.assertIn("FINAL-", msg)

    def test_f3e_f9_marker_exists(self):
        for content, now in (
            ("", NOW),
            ("壊れた中身\x00", NOW + timedelta(days=30)),
            ("started: x\n", NOW - timedelta(days=400)),
        ):
            with self.subTest(repr(content)):
                repo = self.make_repo(self.label())
                m = repo / final_eval.MARKER
                m.write_text(content, encoding="utf-8")
                st, _ = self.data_guard(None)
                with (
                    self.env(repo, now=now),
                    st,
                    self.assertRaises(SystemExit) as cm,
                ):
                    final_eval.run(self.root)
                self.assertIn("印", str(cm.exception))
                self.assertEqual(m.read_text(encoding="utf-8"), content)

    def test_f3f_figure_dir_exists(self):
        repo = self.make_repo(self.label())
        (repo / "docs" / "log" / "2026-10-13-40").mkdir()
        msg = self.assert_stops_without_data(repo)
        self.assertIn("図の出力先", msg)

    def test_f3g_split_differs_from_expected(self):
        # 学習側のセッションが 1 本消えた root と、1 本増えた root
        less = self.tmp / self.label()
        less.mkdir()
        for n in self.train[1:] + self.eval:
            shutil.copytree(self.root / n, less / n)
        more = self.tmp / self.label()
        more.mkdir()
        for n in self.train + self.eval:
            shutil.copytree(self.root / n, more / n)
        shutil.copytree(
            self.root / self.train[0], more / "20261011-103000_self_water"
        )
        for root in (less, more):
            with self.subTest(root.name):
                repo = self.make_repo(self.label())
                with mock.patch.object(
                    split, "_meta", wraps=split._meta
                ) as meta:
                    msg = self.assert_stops_without_data(repo, root=root)
                self.assertIn("一致しません", msg)
                self.assertTrue(meta.called)  # meta.json だけは読む

    def test_f10_no_other_arguments(self):
        for extra in (
            ["--final"],
            ["--reproduce"],
            ["--set", "a52"],
            ["--enter", "0.5"],
            ["--seed", "1"],
            ["--eval-min", "3"],
            ["--eval-days", "1"],
        ):
            with (
                self.subTest(extra),
                self.assertRaises(SystemExit),
                contextlib.redirect_stderr(io.StringIO()),
                mock.patch.object(final_eval, "run") as run,
            ):
                final_eval.main([str(self.root), *extra])
            run.assert_not_called()
        with (
            self.assertRaises(SystemExit),
            contextlib.redirect_stderr(io.StringIO()),
            mock.patch.object(final_eval, "run") as run,
        ):
            final_eval.main([])
        run.assert_not_called()


def dummy_fit(root, train, c):
    return final_eval.ConfigResult(
        config=c, model=None, norm_sessions=(), n_fit_rows=0, pos_weight=None
    )


class MarkerTest(FinalCase):
    def label(self):
        FinalCase._n += 1
        return f"marker{FinalCase._n}"

    def test_f4_marker_is_written_before_eval_is_opened(self):
        first = self.rec["first_eval"]
        final_text = (self.repo / final_eval.MARKER).read_text(encoding="utf-8")
        # 評価側を最初に読んだ時点で、印は中身まで書かれ、fsync され、
        # 構成 J・R の学習が済んでいる
        self.assertEqual(first["marker"], final_text)
        self.assertIn(("fit", "J"), first["log"])
        self.assertIn(("fit", "R"), first["log"])
        self.assertGreaterEqual(first["log"].count(("fsync",)), 2)
        reads = [e[1] for e in first["log"] if e[0] == "read"]
        self.assertTrue(reads)
        self.assertFalse(set(reads) & set(self.eval))
        self.assertLess(
            first["log"].index(("fit", "R")),
            first["log"].index(("fsync",)),
        )

    def test_f4_stop_while_training_writes_no_marker(self):
        root = self.tmp / self.label()
        root.mkdir()
        for n in self.train + self.eval:
            shutil.copytree(self.root / n, root / n)
        for k, n in enumerate(self.train):
            shutil.rmtree(root / n)
            synth_throat(root, n, (), seed=500 + k)
        repo = self.make_repo(self.label())
        st, calls = self.data_guard(self.eval)
        with self.env(repo), st, self.assertRaises(SystemExit) as cm:
            final_eval.run(root)
        self.assertIn("陽性と陰性", str(cm.exception))
        self.assert_no_marker(repo)
        self.assertEqual(set(calls["_throat_windows"]), set(self.train))

    def test_f4_marker_created_by_another_run(self):
        repo = self.make_repo(self.label())
        real_split = final_eval.split_and_check

        def split_then_other_run(root):
            out = real_split(root)
            (repo / final_eval.MARKER).write_text("other\n", encoding="utf-8")
            return out

        st, _ = self.data_guard(self.eval)
        with (
            self.env(repo),
            st,
            mock.patch.object(final_eval, "fit", side_effect=dummy_fit),
            mock.patch.object(
                final_eval, "split_and_check", side_effect=split_then_other_run
            ),
            mock.patch.object(final_eval, "score_eval") as score_eval,
            self.assertRaises(SystemExit) as cm,
        ):
            final_eval.run(self.root)
        self.assertIn("既にあります", str(cm.exception))
        score_eval.assert_not_called()
        self.assertEqual(
            (repo / final_eval.MARKER).read_text(encoding="utf-8"), "other\n"
        )

    def test_f4_marker_write_or_fsync_failure_stops(self):
        for fn in ("write", "fsync"):
            with self.subTest(fn):
                repo = self.make_repo(self.label())
                st, _ = self.data_guard(self.eval)
                with (
                    self.env(repo),
                    st,
                    mock.patch.object(final_eval, "fit", side_effect=dummy_fit),
                    mock.patch.object(
                        final_eval.os, fn, side_effect=OSError("故障")
                    ),
                    mock.patch.object(final_eval, "score_eval") as score_eval,
                    self.assertRaises(SystemExit) as cm,
                ):
                    final_eval.run(self.root)
                self.assertIn("書き込みに失敗", str(cm.exception))
                score_eval.assert_not_called()
                # 作った印は消さない
                self.assertTrue((repo / final_eval.MARKER).exists())

    def test_f4_marker_stays_after_a_later_stop(self):
        repo = self.make_repo(self.label())
        with (
            self.env(repo),
            mock.patch.object(final_eval, "fit", side_effect=dummy_fit),
            mock.patch.object(
                final_eval, "score_eval", side_effect=RuntimeError("途中")
            ),
            self.assertRaises(RuntimeError),
        ):
            final_eval.run(self.root)
        text = (repo / final_eval.MARKER).read_text(encoding="utf-8")
        self.assertIn(f"commit: {HEAD}", text)


class ModelTest(FinalCase):
    def test_f5_models_depend_only_on_train_side(self):
        root = self.tmp / "other-eval"
        root.mkdir()
        for n in self.train + self.eval:
            shutil.copytree(self.root / n, root / n)
        for k, n in enumerate(self.eval):
            shutil.rmtree(root / n)
            synth_throat(root, n, (12.0, 20.0), seed=900 + k)
        repo = self.make_repo("other-eval")
        with self.env(repo):
            other = final_eval.run(root)
        a, b = self.result.configs, other.configs
        assert_model_equal(self, a[0].model, b[0].model)
        self.assertEqual(a[0].norm_sessions, tuple(self.train))
        self.assertEqual(b[0].norm_sessions, tuple(self.train))
        ra, rb = a[1].model, b[1].model
        for x, y in (
            (ra.standardizer.mean, rb.standardizer.mean),
            (ra.standardizer.std, rb.standardizer.std),
            (ra.clf.coef_, rb.clf.coef_),
            (ra.clf.intercept_, rb.clf.intercept_),
        ):
            self.assertEqual(x.tobytes(), y.tobytes())
        self.assertEqual(ra.standardizer.sessions, tuple(self.train))
        # 評価側は入れ替わったので、評価側の数字は変わる
        self.assertNotEqual(a[1].total["swallows"], b[1].total["swallows"])

    def test_f6_same_input_gives_same_report(self):
        repo = self.make_repo("again")
        with self.env(repo):
            again = final_eval.run(self.root)
        assert_model_equal(
            self, again.configs[0].model, self.result.configs[0].model
        )
        self.assertEqual(final_eval.format_report(again), self.report)


class ReportTest(FinalCase):
    def test_f7_report_contents(self):
        text = self.report
        self.assertEqual(self.stdout[: len(text) + 1], text + "\n")
        self.assertIn(final_eval.TIMING_HEADING, self.stdout)
        for s in (
            "# M3 咽喉マイク: 評価側の採点（1 回だけ。#40、docs/decisions/0034）",
            "## 実行の条件",
            "## 学習に使ったセッション（3）",
            "## 評価に使ったセッション（3）",
            "## 構成 J（判定に使う）: mfe40 CNN 種 0、ヒステリシス 入り 0.95・出 0.85",
            "## 構成 R（M1 と同じ手法の参照）: a52 ロジスティック回帰、ヒステリシス 入り 0.85・出 0.65",
            "評価側の収集日: 1",
            "広げた読み",
            "docs/decisions/0034",
            "そのまま比べない",
            "検出率 0.783（54/69）",
            "## 注記",
            f"- コミット: {HEAD[:7]}",
            "numba==",
            CONFIG_LINES[0],
            CONFIG_LINES[1],
        ):
            self.assertIn(s, text)
        for s in (
            "### 混同行列",
            "TP ",
            "FN ",
            "FP ",
            "### 合格線との比較",
            "### セッションごと",
            "### 参考値",
        ):
            self.assertEqual(text.count(s), 2, s)
        for n in self.train + self.eval:
            self.assertIn(n, text)
        self.assertRegex(
            text, r"（合格線 0\.70 以上）: (満たす|満たさない|出ない)\n"
        )
        for w in (*FORBIDDEN_WORDS, "達成", "合格した", "通過"):
            self.assertNotIn(w, self.stdout)
        self.assertNotIn("array(", self.stdout)
        # 確率の並び（小数が窓の数より多く並ぶ行）が無い
        n_windows = min(len(s.prob) for s in self.result.configs[0].scored)
        for line in self.stdout.splitlines():
            if line.startswith("- `.venv` の全パッケージ"):
                continue  # 版の一覧
            self.assertLess(line.count("0."), n_windows, line[:80])

    def test_f8_writes_only_marker_and_png(self):
        self.assertEqual(files_under(self.root), self.before_root)
        out = self.repo / "docs" / "log" / "2026-10-13-40"
        files = files_under(self.repo)
        expected_other = [
            "docs",
            "docs/decisions",
            final_eval.DECISION,
            "docs/log",
            final_eval.MARKER,
            "docs/log/2026-10-13-40",
        ]
        rest = [f for f in files if f not in expected_other]
        self.assertTrue(rest)
        for f in rest:
            self.assertTrue(f.startswith("docs/log/2026-10-13-40/"), f)
            self.assertTrue(f.endswith(".png"), f)
            self.assertEqual((self.repo / f).read_bytes()[:8], PNG_MAGIC)
        names = sorted(p.name for p in out.iterdir())
        self.assertIn("final.png", names)
        self.assertEqual(
            [n for n in names if n.startswith("session_")],
            sorted(f"session_{n}.png" for n in self.eval),
        )
        text = (self.repo / final_eval.MARKER).read_text(encoding="utf-8")
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("# #40"))
        self.assertEqual(
            [s.split(":", 1)[0] for s in lines[1:]],
            ["started", "commit", "decision", "command"],
        )
        self.assertIn(f"started: {NOW.isoformat(timespec='seconds')}", text)
        self.assertIn(f"commit: {HEAD}", text)
        self.assertIn(f"decision: {final_eval.DECISION}", text)
        self.assertIn("command: python analysis/final_eval.py ", text)
        self.assertNotIn("[", text)


class FigureTest(FinalCase):
    def test_f11_eval_side_draw_accepts_only_eval_names(self):
        repo = self.make_repo("fig")
        j = self.result.configs[0]

        def renamed(name):
            s = j.scored[0]
            d = type(s.data)(**{**s.data.__dict__, "name": name})
            return [type(s)(data=d, prob=s.prob)]

        with self.env(repo):
            for name in (
                self.train[0],
                "20261012-100000_p1_water",
                "20261012-990000_self_water",
            ):
                with self.subTest(name), self.assertRaises(SystemExit):
                    visualize.draw(
                        self.root,
                        3,
                        renamed(name),
                        j.masks[:1],
                        repo / "docs" / "log" / "x-40",
                        "t",
                        side="eval",
                    )
            self.assertFalse((repo / "docs" / "log" / "x-40").exists())
            # 学習側の図は評価側の名前を受け付けない（今のまま）
            with self.assertRaises(SystemExit):
                visualize.draw(
                    self.root,
                    3,
                    renamed(self.eval[0]),
                    j.masks[:1],
                    repo / "docs" / "log" / "y-43",
                    "t",
                )
            with self.assertRaises(ValueError):
                visualize.draw(
                    self.root,
                    3,
                    j.scored[:1],
                    j.masks[:1],
                    repo / "docs" / "log" / "z-40",
                    "t",
                    side="other",
                )
            # 評価側は出力先が既にあれば上書きしない
            exists = repo / "docs" / "log" / "w-40"
            exists.mkdir()
            with self.assertRaises(FileExistsError):
                visualize.draw(
                    self.root,
                    3,
                    j.scored[:1],
                    j.masks[:1],
                    exists,
                    "t",
                    side="eval",
                )
            self.assertEqual(list(exists.iterdir()), [])

    def test_f11_fine_display_within_budget(self):
        self.assertTrue(self.rec["fine_s"])
        for s in self.rec["fine_s"]:
            self.assertLessEqual(s, visualize.FINE_LIMIT_S)


class TrainEvalStopTest(FinalCase):
    def test_f12_run_stops_before_split(self):
        st, _ = self.data_guard(None)
        with (
            st,
            mock.patch.object(
                split, "split_sessions", side_effect=AssertionError("split")
            ),
            self.assertRaises(SystemExit) as cm,
        ):
            train_eval.run(self.root, 3, final=True, throat=True)
        self.assertIn("final_eval.py", str(cm.exception))

    def test_f12_main_stops_without_run(self):
        with (
            mock.patch.object(train_eval, "git_dirty", return_value=False),
            mock.patch.object(train_eval, "run") as run,
            self.assertRaises(SystemExit),
        ):
            train_eval.main(
                [str(self.root), "--eval-min", "3", "--throat", "--final"]
            )
        run.assert_not_called()

    def test_f12_other_paths_unchanged(self):
        for extra, final, throat in (
            (["--throat"], False, True),
            (["--final"], True, False),
            ([], False, False),
        ):
            with (
                self.subTest(extra),
                mock.patch.object(train_eval, "git_dirty", return_value=False),
                mock.patch.object(train_eval, "run") as run,
                mock.patch.object(train_eval, "format_report", return_value=""),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                train_eval.main([str(self.root), "--eval-min", "3", *extra])
            run.assert_called_once_with(self.root, 3, final, throat)
        with mock.patch.object(
            train_eval, "_run_impl", return_value="x"
        ) as impl:
            self.assertEqual(train_eval.run(self.root, 3, final=True), "x")
            self.assertEqual(train_eval.run(self.root, 3, throat=True), "x")
        self.assertEqual(
            impl.call_args_list,
            [
                mock.call(self.root, 3, True, False),
                mock.call(self.root, 3, False, True),
            ],
        )


class ConstantTest(unittest.TestCase):
    def test_constants_match_decision_0034(self):
        # リポジトリの 0034 の FINAL-* の行（文書だけを読む。データは開かない）
        path = final_eval.REPO_ROOT / final_eval.DECISION
        got = [
            s.strip()
            for s in path.read_text(encoding="utf-8").splitlines()
            if s.strip().startswith("FINAL-")
        ]
        self.assertEqual(got, final_eval.expected_lines())
        self.assertEqual(got[:2], list(CONFIG_LINES))

    def test_constants(self):
        self.assertEqual(len(final_eval.EXPECTED_TRAIN), 14)
        self.assertEqual(len(final_eval.EXPECTED_EVAL), 7)
        self.assertEqual(final_eval.EVAL_MIN, len(final_eval.EXPECTED_EVAL))
        for names, day in (
            (final_eval.EXPECTED_TRAIN, "20261011"),
            (final_eval.EXPECTED_EVAL, "20261012"),
        ):
            self.assertEqual(list(names), sorted(names))
            for n in names:
                self.assertTrue(n.startswith(day + "-"), n)
                self.assertIn("_self_", n)
        self.assertEqual(final_eval.MARKER, "docs/log/m3-final-40.txt")
        self.assertEqual(
            final_eval.REPO_ROOT, Path(final_eval.__file__).resolve().parents[1]
        )
        j, r = final_eval.CONFIGS
        self.assertEqual(j.set, compare.CNN_SET)
        self.assertEqual(j.seed, compare.CNN_SEED)
        self.assertIn(r.set, compare.LR_SETS)
        self.assertEqual(train_eval.GIT_ROOT, final_eval.REPO_ROOT)


# --- 0034 の評価側の 7 本を学習側に入れない（adversarial-review の指摘） ---


class FixedEvalTest(FinalCase):
    """fixed_eval.EXPECTED_EVAL を合成の評価側の名前に差し替える。"""

    def fixed(self, repo_root: Path | None = None):
        st = contextlib.ExitStack()
        st.enter_context(
            mock.patch.object(fixed_eval, "EXPECTED_EVAL", tuple(self.eval))
        )
        st.enter_context(
            mock.patch.object(
                fixed_eval, "REPO_ROOT", repo_root or self.tmp / "no-marker"
            )
        )
        return st

    def test_fixed_eval_in_train_side_stops_before_reading(self):
        # 後日の収集日（20261013）を 3 本足すと、0034 の評価側が学習側に入る
        root = self.tmp / "later-day"
        root.mkdir()
        for n in self.train + self.eval:
            shutil.copytree(self.root / n, root / n)
        for n in self.eval:
            shutil.copytree(
                self.root / n, root / n.replace("20261012", "20261013")
            )
        log_root = self.tmp / "later-log" / "docs" / "log"
        log_root.mkdir(parents=True)
        calls = (
            ("train_eval", lambda: train_eval.run(root, 3, throat=True)),
            ("compare", lambda: compare.run(root, 3)),
            (
                "visualize",
                lambda: visualize.main(
                    [
                        str(root),
                        "--eval-min",
                        "3",
                        "--set",
                        "base15",
                        "--post",
                        "none",
                        "--out",
                        str(log_root / "x-43"),
                    ]
                ),
            ),
        )
        for label, fn in calls:
            with self.subTest(label):
                st, _ = self.data_guard(None)
                with (
                    self.fixed(),
                    st,
                    mock.patch.object(visualize, "LOG_ROOT", log_root),
                    self.assertRaises(SystemExit) as cm,
                ):
                    fn()
                msg = str(cm.exception)
                self.assertIn("0034", msg)
                self.assertIn("学習・交差検証に使わない", msg)
                self.assertIn(self.eval[0], msg)
        self.assertEqual(list(log_root.iterdir()), [])

    def test_fixed_eval_not_in_train_side_passes(self):
        with self.fixed():
            fixed_eval.assert_fixed_eval_not_in(self.train)
            visualize.check_sessions(self.root, 3, self.train)
            r = train_eval.run(self.root, 3, throat=True)
        self.assertIsNone(r.marker_note)
        self.assertNotIn("評価側は採点済み", train_eval.format_report(r))
        # 実際の 0034 の一覧のまま（合成の名前と重ならない）でも通る
        fixed_eval.assert_fixed_eval_not_in(self.train + self.eval)

    def test_marker_note_in_train_side_reports(self):
        repo = self.tmp / "marker-note"
        (repo / "docs" / "log").mkdir(parents=True)
        (repo / fixed_eval.MARKER).write_text("x\n", encoding="utf-8")
        with (
            self.fixed(repo),
            mock.patch.object(compare, "CNN_REF_SEEDS", ()),
        ):
            reports = [
                train_eval.format_report(
                    train_eval.run(self.root, 3, throat=True)
                ),
                compare.format_report(compare.run(self.root, 3)),
            ]
        for text in reports:
            lines = text.split("\n")
            self.assertEqual(lines[1:4], ["", fixed_eval.MARKER_NOTE, ""])
            self.assertIn("この実行は学習側だけ", text)


# --- F13: git の検査の実物（一時フォルダの git リポジトリ） ---


def _sh(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


class GitTest(unittest.TestCase):
    """F13。git が無い環境でも skip しない（このリポジトリの開発環境には git
    がある）。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.repo = self.base / "repo"
        for d in ("analysis", "tools", "docs/decisions", "docs/log"):
            (self.repo / d).mkdir(parents=True)
        (self.repo / "analysis" / "a.py").write_text("x = 1\n")
        (self.repo / "tools" / "t.py").write_text("y = 1\n")
        (self.repo / "docs" / "evaluation.md").write_text("# e\n")
        (self.repo / "docs" / "log" / "keep.md").write_text("k\n")
        self.train = ("20261011-100000_self_water",)
        self.eval = ("20261012-100000_self_water",)
        (self.repo / final_eval.DECISION).write_text(
            decision_text(self.train, self.eval), encoding="utf-8"
        )
        _sh(self.repo, "init", "-q")
        _sh(self.repo, "add", "-A")
        _sh(self.repo, "commit", "-q", "-m", "init")
        st = contextlib.ExitStack()
        for obj, name, val in (
            (train_eval, "GIT_ROOT", self.repo),
            (final_eval, "REPO_ROOT", self.repo),
            (visualize, "LOG_ROOT", self.repo / "docs" / "log"),
            (final_eval, "EXPECTED_TRAIN", self.train),
            (final_eval, "EXPECTED_EVAL", self.eval),
        ):
            st.enter_context(mock.patch.object(obj, name, val))
        self.addCleanup(st.close)

    def preflight(self):
        return final_eval.preflight(NOW)

    def assert_stops(self):
        with self.assertRaises(SystemExit) as cm:
            self.preflight()
        self.assertFalse((self.repo / final_eval.MARKER).exists())
        return str(cm.exception)

    def test_f13_clean_repository_passes(self):
        commit, out = self.preflight()
        self.assertEqual(
            commit, _sh(self.repo, "rev-parse", "--short", "HEAD").strip()
        )
        self.assertEqual(out, self.repo / "docs" / "log" / "2026-10-13-40")

    def test_f13a_tracked_change(self):
        (self.repo / "analysis" / "a.py").write_text("x = 2\n")
        self.assertIn("未コミットの変更", self.assert_stops())

    def test_f13b_untracked_in_analysis(self):
        (self.repo / "analysis" / "new.py").write_text("")
        self.assertIn("analysis/new.py", self.assert_stops())

    def test_f13c_untracked_in_tools(self):
        (self.repo / "tools" / "new.py").write_text("")
        self.assertIn("tools/new.py", self.assert_stops())

    def test_f13d_decision_untracked(self):
        _sh(self.repo, "rm", "-q", "--cached", final_eval.DECISION)
        _sh(self.repo, "commit", "-q", "-m", "untrack")
        self.assert_stops()
        # docs/decisions の未追跡を見ない場合でも ls-files で止まる
        with mock.patch.object(final_eval, "GIT_PATHS", ("analysis",)):
            self.assertIn("追跡されていません", self.assert_stops())

    def test_f13e_deleted_committed_marker(self):
        m = self.repo / final_eval.MARKER
        m.write_text("started: x\n")
        _sh(self.repo, "add", final_eval.MARKER)
        _sh(self.repo, "commit", "-q", "-m", "marker")
        m.unlink()
        self.assertTrue(train_eval.git_dirty())
        root = self.base / "raw"
        root.mkdir()
        st = contextlib.ExitStack()
        for mod, fn in DATA_FNS:
            st.enter_context(
                mock.patch.object(mod, fn, side_effect=AssertionError(fn))
            )
        with (
            st,
            mock.patch.object(
                split, "split_sessions", side_effect=AssertionError("split")
            ),
            mock.patch.object(final_eval, "_now", lambda: NOW),
            self.assertRaises(SystemExit) as cm,
        ):
            final_eval.run(root)
        self.assertIn("未コミットの変更", str(cm.exception))
        self.assertFalse(m.exists())

    def test_f13_git_root_is_fixed(self):
        expected = _sh(self.repo, "rev-parse", "--short", "HEAD").strip()
        (self.repo / "tools" / "t.py").write_text("y = 2\n")
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        results = []
        for cwd in (self.base, self.repo / "analysis", self.repo):
            os.chdir(cwd)
            results.append((train_eval.git_dirty(), train_eval.git_commit()))
        self.assertEqual(results, [(True, f"{expected}-dirty")] * 3)


if __name__ == "__main__":
    unittest.main()
