"""fixed_eval.py の M3・M4 の一覧と検査を確かめる（plan #46 F1〜F5、0037）。

実行: python -m unittest discover -s analysis -v
合成の名前だけを使う。data/ は使わない。読むファイルはリポジトリの
docs/decisions/ の 0034 と 0037 だけ（本文の行の照合）。印は一時フォルダに置く。
M4_EXPECTED_TRAIN・M4_EXPECTED_EVAL は mock.patch.object で差し替える。実物の
M4_EXPECTED_TRAIN は 2026-10-28 に名前が入ったので、合成の名前の検査は None に
差し替えて動かし、実物の照合（test_real_0037_matches）だけ実物の値で動かす。
"""

from __future__ import annotations
import tempfile, unittest
from pathlib import Path
from unittest import mock

import fixed_eval

M3_21 = fixed_eval.EXPECTED_TRAIN + fixed_eval.EXPECTED_EVAL
REAL_TRAIN = fixed_eval.M4_EXPECTED_TRAIN  # 実物（0037 の M4-TRAIN）
MEAL_TRAIN = (
    "20261025-120000_self_meal",
    "20261027-190000_self_meal",
    "20261028-123000_self_meal",
)
EVAL_3 = (
    "20261101-120000_self_meal",
    "20261101-190000_self_meal",
    "20261103-120000_self_meal",
)


def _decision_lines(rel: str, prefix: str) -> list:
    """final_eval.check_decision と同じ読み方で、prefix で始まる行を並べる。"""
    text = (fixed_eval.REPO_ROOT / rel).read_text(encoding="utf-8")
    return [
        s.strip() for s in text.splitlines() if s.strip().startswith(prefix)
    ]


class F1M3Lists(unittest.TestCase):
    def test_m3_constants_match_0034(self):
        got = _decision_lines(fixed_eval.DECISION, "FINAL-")
        self.assertIn(
            "FINAL-TRAIN: " + " ".join(fixed_eval.EXPECTED_TRAIN), got
        )
        self.assertIn("FINAL-EVAL: " + " ".join(fixed_eval.EXPECTED_EVAL), got)
        self.assertEqual(len(fixed_eval.EXPECTED_TRAIN), 14)
        self.assertEqual(len(fixed_eval.EXPECTED_EVAL), 7)

    def test_m4_train_m3_is_21(self):
        self.assertEqual(len(fixed_eval.M4_TRAIN_M3), 21)
        self.assertEqual(len(set(fixed_eval.M4_TRAIN_M3)), 21)
        self.assertTrue(
            set(fixed_eval.EXPECTED_EVAL) <= set(fixed_eval.M4_TRAIN_M3)
        )


class F2AssertM4Train(unittest.TestCase):
    def setUp(self):
        # 実物の M4_EXPECTED_TRAIN には 2026-10-28 に名前が入った。合成の名前のテストは
        # 一覧の固定の前の状態（None）で動かす
        p = mock.patch.object(fixed_eval, "M4_EXPECTED_TRAIN", None)
        p.start()
        self.addCleanup(p.stop)

    def test_passes_21_plus_meal_before_cutoff(self):
        fixed_eval.assert_m4_train(M3_21 + MEAL_TRAIN)
        fixed_eval.assert_m4_train(M3_21)

    def test_stops_after_cutoff(self):
        for late in (
            "20261029-000000_self_meal",
            "20261101-120000_self_meal",
        ):
            with self.subTest(late=late):
                with self.assertRaises(SystemExit) as cm:
                    fixed_eval.assert_m4_train(M3_21 + MEAL_TRAIN + (late,))
                self.assertIn(late, str(cm.exception))

    def test_stops_on_m4_eval_name(self):
        # 締めより前の日付でも、M4-EVAL の名前なら止まる
        ev = ("20261028-200000_self_meal",)
        with mock.patch.object(fixed_eval, "M4_EXPECTED_EVAL", ev):
            with self.assertRaises(SystemExit) as cm:
                fixed_eval.assert_m4_train(M3_21 + ev)
        self.assertIn("M4-EVAL", str(cm.exception))

    def test_stops_when_m3_21_missing(self):
        for drop in (
            fixed_eval.EXPECTED_TRAIN[0],
            fixed_eval.EXPECTED_EVAL[-1],
        ):
            with self.subTest(drop=drop):
                names = tuple(n for n in M3_21 if n != drop) + MEAL_TRAIN
                with self.assertRaises(SystemExit) as cm:
                    fixed_eval.assert_m4_train(names)
                self.assertIn(drop, str(cm.exception))

    def test_expected_list(self):
        want = M3_21 + MEAL_TRAIN
        with mock.patch.object(fixed_eval, "M4_EXPECTED_TRAIN", want):
            fixed_eval.assert_m4_train(tuple(reversed(want)))  # 順は問わない
            for names in (M3_21 + MEAL_TRAIN[:2], want + ("20261026-1_x",)):
                with self.subTest(n=len(names)):
                    with self.assertRaises(SystemExit) as cm:
                        fixed_eval.assert_m4_train(names)
                    self.assertIn("M4-TRAIN", str(cm.exception))

    def test_stops_on_name_without_date(self):
        with self.assertRaises(SystemExit):
            fixed_eval.assert_m4_train(M3_21 + ("self_meal",))


class F3AssertM4Eval(unittest.TestCase):
    def test_stops_when_list_is_none(self):
        self.assertIsNone(fixed_eval.M4_EXPECTED_EVAL)
        with self.assertRaises(SystemExit) as cm:
            fixed_eval.assert_m4_eval(EVAL_3)
        self.assertIn("M4-EVAL", str(cm.exception))

    def test_passes_three_or_more(self):
        one_day = (
            "20261102-080000_self_meal",
            "20261102-120000_self_meal",
            "20261102-190000_self_meal",
        )
        edges = (
            "20261101-000000_self_meal",
            "20261102-120000_self_meal",
            "20261104-235959_self_meal",
        )
        for names in (EVAL_3, one_day, edges):  # 2 日・1 日・窓の両端
            with self.subTest(names=names):
                with mock.patch.object(fixed_eval, "M4_EXPECTED_EVAL", names):
                    fixed_eval.assert_m4_eval(tuple(reversed(names)))

    def test_stops_on_mismatch(self):
        with mock.patch.object(fixed_eval, "M4_EXPECTED_EVAL", EVAL_3):
            for names in (EVAL_3[:2], EVAL_3 + ("20261104-120000_self_meal",)):
                with self.subTest(n=len(names)):
                    with self.assertRaises(SystemExit) as cm:
                        fixed_eval.assert_m4_eval(names)
                    self.assertIn("一致しません", str(cm.exception))

    def test_stops_outside_window(self):
        for out in (
            "20261031-235959_self_meal",
            "20261105-000000_self_meal",
        ):
            names = EVAL_3[:2] + (out,)
            with self.subTest(out=out):
                with mock.patch.object(fixed_eval, "M4_EXPECTED_EVAL", names):
                    with self.assertRaises(SystemExit) as cm:
                        fixed_eval.assert_m4_eval(names)
                self.assertIn(out, str(cm.exception))

    def test_stops_under_three(self):
        names = EVAL_3[:2]
        with mock.patch.object(fixed_eval, "M4_EXPECTED_EVAL", names):
            with self.assertRaises(SystemExit) as cm:
                fixed_eval.assert_m4_eval(names)
        self.assertIn("3 本以上", str(cm.exception))


SYNTH_0037 = """# 0037 合成

本文の `M4-` の説明の行は、行頭が M4- ではないので数えない。

```
M4-EVAL-WINDOW: from=2026-11-01 to=2026-11-04
M4-TRAIN-CUTOFF: 2026-10-28
M4-TRAIN: （学習用の記録の締めの後に追記）
M4-EVAL: （評価用の記録の打ち切りと採否の後に追記）
```
"""


class F4M4LinesMatch(unittest.TestCase):
    def setUp(self):
        # 実物の M4_EXPECTED_TRAIN には 2026-10-28 に名前が入った。合成の名前のテストは
        # 一覧の固定の前の状態（None）で動かす
        p = mock.patch.object(fixed_eval, "M4_EXPECTED_TRAIN", None)
        p.start()
        self.addCleanup(p.stop)

    def test_synthetic_match(self):
        self.assertTrue(fixed_eval.m4_lines_match(SYNTH_0037))

    def test_synthetic_one_char_off(self):
        for old, new in (
            ("to=2026-11-04", "to=2026-11-05"),
            ("CUTOFF: 2026-10-28", "CUTOFF: 2026-10-29"),
            ("締めの後に追記", "締めの後で追記"),
        ):
            with self.subTest(new=new):
                text = SYNTH_0037.replace(old, new)
                self.assertNotEqual(text, SYNTH_0037)
                self.assertFalse(fixed_eval.m4_lines_match(text))

    def test_missing_or_extra_line(self):
        self.assertFalse(
            fixed_eval.m4_lines_match(
                SYNTH_0037.replace("M4-TRAIN-CUTOFF: 2026-10-28\n", "")
            )
        )
        self.assertFalse(fixed_eval.m4_lines_match(SYNTH_0037 + "M4-NOTE: x\n"))

    def test_lists_filled(self):
        train = M3_21 + MEAL_TRAIN
        text = SYNTH_0037.replace(
            fixed_eval.M4_TRAIN_PENDING, " ".join(train)
        ).replace(fixed_eval.M4_EVAL_PENDING, " ".join(EVAL_3))
        with (
            mock.patch.object(fixed_eval, "M4_EXPECTED_TRAIN", train),
            mock.patch.object(fixed_eval, "M4_EXPECTED_EVAL", EVAL_3),
        ):
            self.assertTrue(fixed_eval.m4_lines_match(text))
            self.assertFalse(fixed_eval.m4_lines_match(SYNTH_0037))
        self.assertFalse(fixed_eval.m4_lines_match(text))

    def test_real_0037_matches(self):
        text = (fixed_eval.REPO_ROOT / fixed_eval.M4_DECISION).read_text(
            encoding="utf-8"
        )
        with mock.patch.object(fixed_eval, "M4_EXPECTED_TRAIN", REAL_TRAIN):
            self.assertTrue(fixed_eval.m4_lines_match(text))
            self.assertEqual(
                _decision_lines(fixed_eval.M4_DECISION, "M4-"),
                fixed_eval.m4_expected_lines(),
            )
            # 2026-10-28 の固定: M3 の 21 本と学習用 meal 5 本
            self.assertEqual(len(REAL_TRAIN), 26)
            self.assertEqual(REAL_TRAIN[:21], M3_21)
            self.assertTrue(all(n.endswith("_self_meal") for n in REAL_TRAIN[21:]))
            self.assertTrue(all(n[:8] <= fixed_eval.M4_TRAIN_CUTOFF for n in REAL_TRAIN))


CONFIG = (
    "set=mfe40 classifier=cnn seeds=0 hard_negative=none post=hyst "
    "threshold=- enter=0.95 exit=0.85 cap_s=2.0 refractory_s=none"
)
SYNTH_0038 = """# 0038 合成

```
M4-CONFIG: {config}
M4-MODEL: {model}
```
"""


class F6M4Config(unittest.TestCase):
    def setUp(self):
        # 実物の M4_EXPECTED_TRAIN には 2026-10-28 に名前が入った。合成の名前のテストは
        # 一覧の固定の前の状態（None）で動かす
        p = mock.patch.object(fixed_eval, "M4_EXPECTED_TRAIN", None)
        p.start()
        self.addCleanup(p.stop)
        # 実物の M4_CONFIG には 2026-10-28 に 0038 の値が入った。合成の値のテストは
        # 0038 を書く前の状態（None）から動かす
        q = mock.patch.object(fixed_eval, "M4_CONFIG", None)
        q.start()
        self.addCleanup(q.stop)
        # 実物の M4_MODEL には 2026-10-29 に 0038 の値（ステップ 3）が入った。
        # 合成の値のテストは同じく None から動かす
        r = mock.patch.object(fixed_eval, "M4_MODEL", None)
        r.start()
        self.addCleanup(r.stop)

    """plan #45 の 9・12 節: 0038 の機械が読む行の定数と照合。"""

    def test_none_expected_lines(self):
        self.assertIsNone(fixed_eval.M4_CONFIG)
        self.assertIsNone(fixed_eval.M4_MODEL)
        self.assertEqual(
            fixed_eval.m4_config_expected_lines(),
            [
                "M4-CONFIG: （ステップ 2 で追記）",
                "M4-MODEL: （ステップ 3 で追記）",
            ],
        )
        text = SYNTH_0038.format(
            config=fixed_eval.M4_CONFIG_PENDING,
            model=fixed_eval.M4_MODEL_PENDING,
        )
        self.assertTrue(fixed_eval.m4_config_lines_match(text))

    def test_config_filled(self):
        text = SYNTH_0038.format(
            config=CONFIG, model=fixed_eval.M4_MODEL_PENDING
        )
        self.assertFalse(fixed_eval.m4_config_lines_match(text))
        with mock.patch.object(fixed_eval, "M4_CONFIG", CONFIG):
            self.assertTrue(fixed_eval.m4_config_lines_match(text))
            self.assertFalse(
                fixed_eval.m4_config_lines_match(
                    text.replace("enter=0.95", "enter=0.90")
                )
            )
            self.assertFalse(
                fixed_eval.m4_config_lines_match(text + "M4-CONFIG: x\n")
            )
        model = "path=analysis/models/m4-0038.npz sha256=" + "0" * 64
        text2 = SYNTH_0038.format(config=CONFIG, model=model)
        with (
            mock.patch.object(fixed_eval, "M4_CONFIG", CONFIG),
            mock.patch.object(fixed_eval, "M4_MODEL", model),
        ):
            self.assertTrue(fixed_eval.m4_config_lines_match(text2))
            self.assertFalse(fixed_eval.m4_config_lines_match(text))

    def test_0037_lines_are_not_config_lines(self):
        # 0037 の M4- の行は 0038 の照合に入らない（逆も）
        self.assertTrue(fixed_eval.m4_lines_match(SYNTH_0037))
        both = SYNTH_0037 + SYNTH_0038.format(
            config=fixed_eval.M4_CONFIG_PENDING,
            model=fixed_eval.M4_MODEL_PENDING,
        )
        self.assertTrue(fixed_eval.m4_config_lines_match(both))
        self.assertFalse(fixed_eval.m4_lines_match(both))

    def test_file_matches(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with mock.patch.object(fixed_eval, "REPO_ROOT", root):
                self.assertFalse(fixed_eval.m4_config_file_matches())
                p = root / fixed_eval.M4_CONFIG_DECISION
                p.parent.mkdir(parents=True)
                p.write_text(
                    SYNTH_0038.format(
                        config=CONFIG, model=fixed_eval.M4_MODEL_PENDING
                    ),
                    encoding="utf-8",
                )
                self.assertFalse(fixed_eval.m4_config_file_matches())
                with mock.patch.object(fixed_eval, "M4_CONFIG", CONFIG):
                    self.assertTrue(fixed_eval.m4_config_file_matches())


class F5M3CheckUnchanged(unittest.TestCase):
    def test_m3_eval_still_stops_m3_path(self):
        with self.assertRaises(SystemExit) as cm:
            fixed_eval.assert_fixed_eval_not_in(fixed_eval.M4_TRAIN_M3)
        for n in fixed_eval.EXPECTED_EVAL:
            self.assertIn(n, str(cm.exception))
        fixed_eval.assert_fixed_eval_not_in(fixed_eval.EXPECTED_TRAIN)

    def test_m4_marker_note(self):
        # 印のパスは一時フォルダで確かめる（リポジトリの docs/log/ は使わない）
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with mock.patch.object(fixed_eval, "REPO_ROOT", root):
                self.assertIsNone(fixed_eval.m4_marker_note())
                self.assertIsNone(fixed_eval.marker_note())
                p = root / fixed_eval.M4_MARKER
                p.parent.mkdir(parents=True)
                p.write_text("x\n", encoding="utf-8")
                self.assertIn(fixed_eval.M4_MARKER, fixed_eval.m4_marker_note())
                self.assertIsNone(fixed_eval.marker_note())  # M3 の印とは別


if __name__ == "__main__":
    unittest.main()
