"""visualize.py を合成データで検証する（plan #43 T9）。

実行: python -m unittest discover -s analysis -v
PNG は一時フォルダの docs/log/ に当たる場所（visualize.LOG_ROOT を差し替える）に
書く。data/ とリポジトリの docs/log/ は使わない。
"""

from __future__ import annotations
import tempfile, unittest
from pathlib import Path
from unittest import mock

import numpy as np

import compare, visualize
from test_throat_compare import build_root

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class VisualizeCase(unittest.TestCase):
    tmp = None

    @classmethod
    def setUpClass(cls):
        if VisualizeCase.tmp is None:
            tmp = tempfile.TemporaryDirectory()
            unittest.addModuleCleanup(tmp.cleanup)
            VisualizeCase.tmp = Path(tmp.name)
            r = build_root(VisualizeCase.tmp)
            VisualizeCase.root, VisualizeCase.train, VisualizeCase.eval = r
            VisualizeCase.log_root = VisualizeCase.tmp / "docs" / "log"
            VisualizeCase.log_root.mkdir(parents=True)
            _, _, scored = compare.cv_scored(
                VisualizeCase.root, VisualizeCase.train, "base15"
            )
            VisualizeCase.scored = scored
            VisualizeCase.rows = compare.make_rows("base15", scored)

    def setUp(self):
        p = mock.patch.object(visualize, "LOG_ROOT", self.log_root)
        p.start()
        self.addCleanup(p.stop)


class VisualizeTest(VisualizeCase):
    def test_t9_fine_budget(self):
        b = visualize.FineBudget()
        b.add(4.0)
        b.add(4.0)
        b.add(2.0)
        with self.assertRaises(ValueError):
            b.add(0.01)
        self.assertEqual(visualize.FINE_LIMIT_S, 10.0)

    def test_t9_event_figure_over_10_seconds_stops(self):
        item = dict(
            kind="検出",
            session="x",
            center=0.0,
            t0=0.0,
            x=np.zeros(16000 * 4),
            x_t0=-2.0,
            t_start=np.zeros(0),
            prob=np.zeros(0),
            mask=np.zeros(0, bool),
        )
        out = self.log_root / "over"
        out.mkdir()
        with self.assertRaises(ValueError):
            visualize.fig_events(out / "e.png", "t", [item, item, item])
        self.assertFalse((out / "e.png").exists())
        self.assertLessEqual(
            visualize.EVENTS_PER_FIG * 2 * visualize.EVENT_HALF_S,
            visualize.FINE_LIMIT_S,
        )

    def test_t9_out_dir_must_be_under_docs_log(self):
        for bad in (
            self.tmp,
            self.tmp / "docs",
            self.log_root,
            self.tmp / "x" / "log",
        ):
            with self.subTest(str(bad)), self.assertRaises(SystemExit):
                visualize.check_out_dir(bad)
        visualize.check_out_dir(self.log_root / "2026-10-13-43")
        with self.assertRaises(SystemExit):
            visualize.check_out_dir(self.log_root / ".." / "elsewhere")

    def test_t9_only_train_side_self(self):
        with self.assertRaises(SystemExit) as cm:
            visualize.check_sessions(self.root, 3, [self.eval[0]])
        self.assertIn("評価側", str(cm.exception))
        with self.assertRaises(SystemExit):
            visualize.check_sessions(self.root, 3, ["20261011-100000_p1_water"])
        with self.assertRaises(SystemExit):
            visualize.check_sessions(
                self.root, 3, ["20261011-990000_self_water"]
            )
        visualize.check_sessions(self.root, 3, self.train)

    def test_t9_draw_rejects_eval_side_scored(self):
        fake = [
            type(s)(
                data=type(s.data)(**{**s.data.__dict__, "name": self.eval[0]}),
                prob=s.prob,
            )
            for s in self.scored[:1]
        ]
        with self.assertRaises(SystemExit):
            visualize.draw(
                self.root,
                3,
                fake,
                [np.zeros(len(fake[0].prob), bool)],
                self.log_root / "ev",
                "t",
            )
        self.assertFalse((self.log_root / "ev").exists())

    def test_t9_writes_three_kinds_of_png(self):
        row = self.rows[0]
        out = self.log_root / "2026-10-13-43"
        paths = visualize.draw(
            self.root,
            3,
            self.scored,
            compare.masks(self.scored, row.post, row.op),
            out,
            "base15・なし",
            self.rows,
            row,
        )
        names = sorted(p.name for p in paths)
        self.assertEqual(
            [n for n in names if n.startswith("session_")],
            sorted(f"session_{n}.png" for n in self.train),
        )
        self.assertIn("compare.png", names)
        self.assertTrue(any(n.startswith("events_") for n in names))
        for p in paths:
            self.assertEqual(p.parent, out.resolve())
            self.assertEqual(p.read_bytes()[:8], PNG_MAGIC)
        self.assertEqual(sorted(x.name for x in out.iterdir()), names)

    def test_t9_classify_matches_event_metrics(self):
        import evaluate

        sw = [10.0, 20.0]
        pos = [9.5, 9.75, 30.0, 30.25, 40.0]
        c = visualize.classify(sw, pos)
        m = evaluate.event_metrics(sw, pos, 60.0, 0.0)
        self.assertEqual(len(c["検出"]), m["detected"])
        self.assertEqual(c["見逃し"], [20.0])
        self.assertEqual(len(c["誤検出"]), m["false_positive_runs"])


class M4VisualizeTest(unittest.TestCase):
    """plan #45 の 7.2・12 節: side="m4train"、meal の頁分けと細かい表示なし。"""

    QUIET = "20261011-100847_self_quiet"
    WATER = "20261011-101644_self_water"
    MEAL = "20261025-120000_self_meal"

    @classmethod
    def setUpClass(cls):
        import evaluate, features, fixed_eval, train_eval
        from test_features import make_throat_session
        from test_m4_compare import make_dir

        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        # 合成の名前は実物の M4_EXPECTED_TRAIN（2026-10-28 に固定）と違うので None で動かす
        p = mock.patch.object(fixed_eval, "M4_EXPECTED_TRAIN", None)
        p.start()
        cls.addClassCleanup(p.stop)
        cls.tmp = Path(tmp.name)
        cls.root = cls.tmp / "raw"
        cls.root.mkdir()
        cls.log_root = cls.tmp / "docs" / "log"
        cls.log_root.mkdir(parents=True)
        real = {
            cls.QUIET: (40.0, ()),
            cls.WATER: (40.0, (8.0, 16.0)),
            cls.MEAL: (320.0, (20.0, 150.0, 310.0)),
        }
        for n in fixed_eval.M4_TRAIN_M3 + (cls.MEAL,):
            if n in real:
                sec, sw = real[n]
                make_throat_session(
                    cls.root,
                    n,
                    seconds=sec,
                    noise_lsb=100.0,
                    events=[(s, "s", "") for s in sw],
                    seed=len(sw),
                )
            else:
                make_dir(cls.root, n)
        cls.scored = []
        for n in (cls.QUIET, cls.WATER, cls.MEAL):
            p = cls.root / n
            _, _, t, _, valid, _, _ = features._throat_windows(p)
            sw = evaluate.load_events(p)
            t0, dur = evaluate.throat_session_span(p)
            f = features.WindowFeatures(
                session=n, t_start_s=t, X=np.zeros((len(t), 1)), valid=valid
            )
            d = train_eval.SessionData(
                name=n,
                feats=f,
                swallows=sw,
                t0_s=t0,
                duration_s=dur,
                labels=train_eval.window_labels(t, sw),
            )
            prob = np.zeros(len(t))
            if n == cls.MEAL:
                prob[100:104] = 0.9
            cls.scored.append(compare.Scored(data=d, prob=prob))

    def setUp(self):
        p = mock.patch.object(visualize, "LOG_ROOT", self.log_root)
        p.start()
        self.addCleanup(p.stop)

    def masks(self, scored):
        return [s.prob >= 0.5 for s in scored]

    def test_m4train_rejects_names(self):
        import train_eval

        s = self.scored[1]
        for bad in ("20261101-120000_self_meal", "20261011-999999_self_water"):
            fake = [
                compare.Scored(
                    data=train_eval.SessionData(
                        **{**s.data.__dict__, "name": bad}
                    ),
                    prob=s.prob,
                )
            ]
            with self.subTest(bad), self.assertRaises(SystemExit):
                visualize.draw(
                    self.root,
                    None,
                    fake,
                    self.masks(fake),
                    self.log_root / "m4bad",
                    "t",
                    side="m4train",
                )
        self.assertFalse((self.log_root / "m4bad").exists())

    def test_meal_pages_and_no_fine_display(self):
        seen = []
        real = visualize.fig_events

        def spy(path, title, items):
            seen.extend(it["session"] for it in items)
            return real(path, title, items)

        out = self.log_root / "2026-10-29-45"
        with mock.patch.object(visualize, "fig_events", side_effect=spy):
            paths = visualize.draw(
                self.root,
                None,
                self.scored,
                self.masks(self.scored),
                out,
                "C2・P1",
                side="m4train",
            )
        names = sorted(p.name for p in paths)
        self.assertIn(f"session_{self.MEAL}_p01.png", names)
        self.assertIn(f"session_{self.MEAL}_p02.png", names)
        self.assertNotIn(f"session_{self.MEAL}.png", names)
        self.assertIn(f"session_{self.WATER}.png", names)  # 見逃しあり
        self.assertNotIn(f"session_{self.QUIET}.png", names)  # どちらも無い
        self.assertTrue(seen)
        self.assertNotIn(self.MEAL, seen)
        for p in paths:
            self.assertEqual(p.read_bytes()[:8], PNG_MAGIC)

    def test_draw_m4(self):
        import types
        import m4_compare as mc

        row = mc.make_row("C2", "P1", self.scored)
        b = {
            "main": {c: {} for c in mc.FP_CATS},
            "sub": {lb: 0 for lb in mc.MOTION_MARKERS},
            "minutes": {},
            "total": 0,
        }
        b["main"]["meal_quiet"]["meal"] = 1
        res = types.SimpleNamespace(
            selected=row,
            scored={"C2": self.scored},
            rows=[row],
            breakdown={"R0": b, "selected": b},
        )
        out = self.log_root / "2026-10-29-45-m4"
        paths = visualize.draw_m4(self.root, res, out)
        names = {p.name for p in paths}
        self.assertIn("compare.png", names)
        self.assertIn("fp_breakdown.png", names)
        for p in paths:
            self.assertEqual(p.read_bytes()[:8], PNG_MAGIC)

    def test_page_slice(self):
        ses = dict(
            t0=0.0,
            cells_t=0.25 * np.arange(8),
            rms=np.arange(8.0),
            mel=np.zeros((8, 40)),
            markers={"s": [0.1, 1.6], "t": [], "c": [], "n": []},
            t_start=0.25 * np.arange(8),
            prob=np.arange(8.0),
            mask=np.zeros(8, bool),
            cls={"検出": [0.1], "見逃し": [1.6], "誤検出": [(0.9, 1.2)]},
        )
        a = visualize.page_slice(ses, 1.0, 2.0)
        self.assertEqual(a["cells_t"].tolist(), [1.0, 1.25, 1.5, 1.75])
        self.assertEqual(a["prob"].tolist(), [4.0, 5.0, 6.0, 7.0])
        self.assertEqual(a["markers"]["s"], [1.6])
        self.assertEqual(a["cls"]["検出"], [])
        self.assertEqual(a["cls"]["誤検出"], [(0.9, 1.2)])

    def test_fig_compare_lines(self):
        import inspect, train_eval

        sig = inspect.signature(visualize.fig_compare)
        self.assertEqual(
            sig.parameters["det_line"].default, train_eval.PASS_DETECTION_RATE
        )
        self.assertEqual(
            sig.parameters["fp_line"].default, train_eval.PASS_FP_PER_MIN
        )
        lines = []
        import matplotlib.axes

        real_h = matplotlib.axes.Axes.axhline
        real_v = matplotlib.axes.Axes.axvline

        def h(ax, y=0, *a, **k):
            lines.append(("h", y))
            return real_h(ax, y, *a, **k)

        def v(ax, x=0, *a, **k):
            lines.append(("v", x))
            return real_v(ax, x, *a, **k)

        out = self.log_root / "cmp"
        out.mkdir(exist_ok=True)
        from test_m4_compare import fake_row

        rows = [fake_row("C2", "P1", 0.8, 0.5), fake_row("C1", "P0", 0.7, 1.2)]
        with (
            mock.patch.object(matplotlib.axes.Axes, "axhline", h),
            mock.patch.object(matplotlib.axes.Axes, "axvline", v),
        ):
            visualize.fig_compare(
                out / "c.png",
                rows,
                rows[0],
                det_line=0.85,
                fp_line=1.0,
                label_fn=lambda r: f"{r.cls}・{r.post}",
                marker_fn=lambda r: "o",
                group_fn=lambda r: r.cls,
            )
        self.assertEqual((out / "c.png").read_bytes()[:8], PNG_MAGIC)
        self.assertIn(("h", 0.85), lines)
        self.assertIn(("v", 1.0), lines)


if __name__ == "__main__":
    unittest.main()
