"""split.py を合成データで検証する（解釈は docs/decisions/0011）。

実行: python -m unittest discover -s analysis -v
合成セッションは tempfile に作る。data/ と実データは使わない。
"""
from __future__ import annotations
import inspect, json, tempfile, unittest
from pathlib import Path

from split import split_sessions

NO_META = object()   # meta.json を置かない印


def make_session(root: Path, name: str, meta=None) -> None:
    """空のセッションフォルダを作る。meta を省くと、フォルダ名の subject を meta.json に書く。"""
    d = root / name
    d.mkdir()
    if meta is NO_META:
        return
    if meta is None:
        meta = {"subject": name.split("_")[1]}
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def names(subject: str, n: int, cond: str = "water") -> list[str]:
    """同じ収集日の n セッション（名前順 = 時刻順）。"""
    return [f"20260922-19{i:02d}00_{subject}_{cond}" for i in range(n)]


class SplitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def make(self, session_names, **kwargs):
        for n in session_names:
            make_session(self.root, n, **kwargs)

    def test_sp1_newest_three_are_eval(self):
        ns = names("self", 5)
        self.make(ns)
        r = split_sessions(self.root, eval_min=3)
        self.assertEqual(r["eval"], ns[2:])
        self.assertEqual(r["train"], ns[:2])
        self.assertFalse(set(r["train"]) & set(r["eval"]))
        self.assertEqual(sorted(r["train"] + r["eval"]), ns)

    def test_sp2_train_would_be_empty(self):
        self.make(names("self", 3))
        with self.assertRaises(SystemExit):
            split_sessions(self.root, eval_min=3)

    def test_sp3_four_sessions(self):
        ns = names("self", 4)
        self.make(ns)
        r = split_sessions(self.root, eval_min=3)
        self.assertEqual(r["train"], ns[:1])
        self.assertEqual(r["eval"], ns[1:])

    def test_sp4_eval_min_below_three(self):
        self.make(names("self", 5))
        for eval_min in (2, 0, -1):
            with self.subTest(eval_min=eval_min), self.assertRaises(SystemExit):
                split_sessions(self.root, eval_min=eval_min)

    def test_sp5_subjects_do_not_mix(self):
        self.make(names("self", 4))
        self.make(names("p1", 4))
        for subject, other in (("self", "p1"), ("p1", "self")):
            with self.subTest(subject=subject):
                r = split_sessions(self.root, eval_min=3, subject=subject)
                got = r["train"] + r["eval"]
                self.assertEqual(sorted(got), names(subject, 4))
                self.assertFalse([n for n in got if f"_{other}_" in n])

    def test_sp6_name_with_two_subjects(self):
        self.make(names("self", 4))
        make_session(self.root, "20260927-120000_p1_retake_self_water", meta={"subject": "p1"})
        with self.assertRaises(SystemExit):
            split_sessions(self.root, eval_min=3, subject="self")

    def test_sp7_stray_directory_stops(self):
        self.make(names("self", 4))
        (self.root / "20260930-000000_self_water").write_text("", encoding="utf-8")
        (self.root / "notes_self_x").mkdir()
        with self.assertRaises(SystemExit):
            split_sessions(self.root, eval_min=3, subject="self")

    def test_sp7_stray_file_is_ignored(self):
        ns = names("self", 4)
        self.make(ns)
        (self.root / "20260930-000000_self_water").write_text("", encoding="utf-8")
        r = split_sessions(self.root, eval_min=3, subject="self")
        self.assertEqual(sorted(r["train"] + r["eval"]), ns)

    def test_sp8_bad_meta(self):
        ns = names("self", 4)
        for meta in ({"subject": "p1"}, NO_META, [], {"subject": "x"}):
            with self.subTest(meta=meta), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                for n in ns[:3]:
                    make_session(root, n)
                make_session(root, ns[3], meta=meta)
                with self.assertRaises(SystemExit):
                    split_sessions(root, eval_min=3, subject="self")

    def test_sp8b_cond_outside_enum(self):
        self.make(names("self", 4))
        make_session(self.root, "20260927-120000_self_water2", meta={"subject": "self"})
        with self.assertRaises(SystemExit):
            split_sessions(self.root, eval_min=3, subject="self")

    def test_sp8c_symlinked_session_stops(self):
        """同じセッションを指す別名のリンクで、学習側と評価側に同じ実体を入れさせない。"""
        ns = names("self", 4)
        self.make(ns[:1])
        for n in ns[1:]:
            (self.root / n).symlink_to(self.root / ns[0], target_is_directory=True)
        with self.assertRaises(SystemExit):
            split_sessions(self.root, eval_min=3)

    def test_sp9_unknown_subject(self):
        self.make(names("self", 4))
        for subject in ("other", ""):
            with self.subTest(subject=subject), self.assertRaises(SystemExit):
                split_sessions(self.root, eval_min=3, subject=subject)

    def test_sp10_last_two_collection_days(self):
        conds = ["quiet", "water", "talk", "saliva", "neck", "water", "cough"]
        days = ["20260922", "20260924", "20260926"]
        ns = [f"{day}-19{i:02d}00_self_{c}" for day in days for i, c in enumerate(conds)]
        self.assertEqual(len(ns), 21)
        self.make(ns)
        r = split_sessions(self.root, eval_min=14)
        self.assertEqual(r["eval"], ns[7:])
        self.assertEqual(r["train"], ns[:7])
        self.assertFalse([n for n in r["eval"] if n.startswith(days[0])])

    def test_sp11_no_window_level_split(self):
        params = list(inspect.signature(split_sessions).parameters)
        self.assertEqual(params, ["root", "eval_min", "subject"])


if __name__ == "__main__":
    unittest.main()
