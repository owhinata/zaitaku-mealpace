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


if __name__ == "__main__":
    unittest.main()
