"""split.py を合成データで検証する（解釈は docs/decisions/0011）。

実行: python -m unittest discover -s analysis -v
合成セッションは tempfile に作る。data/ と実データは使わない。
"""

from __future__ import annotations
import inspect, json, tempfile, unittest
from pathlib import Path

from split import split_sessions

NO_META = object()  # meta.json を置かない印


def make_session(root: Path, name: str, meta=None) -> None:
    """空のセッションフォルダを作る。meta を省くと、フォルダ名の subject を
    meta.json に書く。"""
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
        make_session(
            self.root,
            "20260927-120000_p1_retake_self_water",
            meta={"subject": "p1"},
        )
        with self.assertRaises(SystemExit):
            split_sessions(self.root, eval_min=3, subject="self")

    def test_sp7_stray_directory_stops(self):
        self.make(names("self", 4))
        (self.root / "20260930-000000_self_water").write_text(
            "", encoding="utf-8"
        )
        (self.root / "notes_self_x").mkdir()
        with self.assertRaises(SystemExit):
            split_sessions(self.root, eval_min=3, subject="self")

    def test_sp7_stray_file_is_ignored(self):
        ns = names("self", 4)
        self.make(ns)
        (self.root / "20260930-000000_self_water").write_text(
            "", encoding="utf-8"
        )
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
        make_session(
            self.root, "20260927-120000_self_water2", meta={"subject": "self"}
        )
        with self.assertRaises(SystemExit):
            split_sessions(self.root, eval_min=3, subject="self")

    def test_sp8c_symlinked_session_stops(self):
        """同じセッションを指す別名のリンクで、学習側と評価側に同じ実体を入れさ
        せない。"""
        ns = names("self", 4)
        self.make(ns[:1])
        for n in ns[1:]:
            (self.root / n).symlink_to(
                self.root / ns[0], target_is_directory=True
            )
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
        ns = [
            f"{day}-19{i:02d}00_self_{c}"
            for day in days
            for i, c in enumerate(conds)
        ]
        self.assertEqual(len(ns), 21)
        self.make(ns)
        r = split_sessions(self.root, eval_min=14)
        self.assertEqual(r["eval"], ns[7:])
        self.assertEqual(r["train"], ns[:7])
        self.assertFalse([n for n in r["eval"] if n.startswith(days[0])])

    def test_sp11_no_window_level_split(self):
        params = list(inspect.signature(split_sessions).parameters)
        self.assertEqual(params, ["root", "eval_min", "subject", "kind"])

    # --- 検出器のセッション（docs/decisions/0021） ---

    def test_sp12_detector_sessions_are_excluded(self):
        ns = names("self", 4)
        self.make(ns)
        det = [f"20261005-12{i:02d}00_self_meal" for i in range(3)]
        self.make(det, meta={"subject": "self", "fw": "detector"})
        r = split_sessions(self.root, eval_min=3)
        self.assertEqual(r["train"], ns[:1])
        self.assertEqual(r["eval"], ns[1:])
        self.assertEqual(r["detector"], det)
        self.assertFalse(set(det) & set(r["train"] + r["eval"]))
        # eval_min の検査は除外後の数で行う（記録ファームウェアが 3 本 + 検出器
        # 3 本 → 学習側が空）
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for n in ns[:3]:
                make_session(root, n)
            for n in det:
                make_session(
                    root, n, meta={"subject": "self", "fw": "detector"}
                )
            with self.assertRaises(SystemExit):
                split_sessions(root, eval_min=3)

    def test_sp12b_detector_files_without_detector_fw_stop(self):
        ns = names("self", 4)
        for meta in ({"subject": "self"}, {"subject": "self", "fw": "logger"}):
            for fname in ("detect.csv", "feat.csv"):
                with (
                    self.subTest(meta=meta, file=fname),
                    tempfile.TemporaryDirectory() as d,
                ):
                    root = Path(d)
                    self_names = ns[:3] + ["20261005-120000_self_meal"]
                    for n in self_names[:3]:
                        make_session(root, n)
                    make_session(root, self_names[3], meta=meta)
                    (root / self_names[3] / fname).write_text(
                        "t_ms,window_t_ms\n", encoding="utf-8"
                    )
                    with self.assertRaises(SystemExit):
                        split_sessions(root, eval_min=3)

    def test_sp12c_detector_sessions_do_not_change_sp10(self):
        conds = ["quiet", "water", "talk", "saliva", "neck", "water", "cough"]
        days = ["20260922", "20260924", "20260926"]
        ns = [
            f"{day}-19{i:02d}00_self_{c}"
            for day in days
            for i, c in enumerate(conds)
        ]
        self.make(ns)
        det = [f"20261005-12{i:02d}00_self_meal" for i in range(3)]
        self.make(det, meta={"subject": "self", "fw": "detector"})
        r = split_sessions(self.root, eval_min=14)
        self.assertEqual(r["eval"], ns[7:])
        self.assertEqual(r["train"], ns[:7])
        self.assertEqual(r["detector"], det)

    def test_sp12d_logger_session_without_fw_is_still_split(self):
        ns = names("self", 4)
        self.make(ns[:2])
        # fw が無い（META が届かなかった）
        make_session(self.root, ns[2], meta={"subject": "self"})
        make_session(self.root, ns[3], meta={"subject": "self", "fw": "logger"})
        r = split_sessions(self.root, eval_min=3)
        self.assertEqual(r["train"], ns[:1])
        self.assertEqual(r["eval"], ns[1:])
        self.assertEqual(r["detector"], [])
        # p1 の検出器のセッションは self の detector に列挙しない
        make_session(
            self.root,
            "20261005-120000_p1_meal",
            meta={"subject": "p1", "fw": "detector"},
        )
        self.assertEqual(split_sessions(self.root, eval_min=3)["detector"], [])


# --- 咽喉マイク（docs/decisions/0031・0032。plan #39 ステップ B のテスト 1〜6） ---

THROAT_META = {
    "subject": "self",
    "fw": "pc-throat",
    "sensors": [{"id": "throat", "iface": "sh12jk-wired-unoq-usbaudio"}],
}


def make_throat(root: Path, name: str, meta=None, files=None) -> Path:
    """咽喉マイクのセッションの形（中身の無いファイル）を作る。"""
    make_session(root, name, meta=dict(THROAT_META) if meta is None else meta)
    d = root / name
    for f in (
        ("throat.wav", "throat_chunks.csv", "events.csv")
        if files is None
        else files
    ):
        (d / f).write_text("", encoding="utf-8")
    return d


def sp10_names() -> list[str]:
    conds = ["quiet", "water", "talk", "saliva", "neck", "water", "cough"]
    days = ["20260922", "20260924", "20260926"]
    return [
        f"{day}-19{i:02d}00_self_{c}"
        for day in days
        for i, c in enumerate(conds)
    ]


def throat_names() -> list[str]:
    """収集日 1 に 14 本（2 セット）、収集日 2 に 7 本。"""
    conds = ["quiet", "water", "talk", "saliva", "neck", "water", "cough"]
    out = []
    for hh in ("09", "18"):
        out += [f"20261011-{hh}{i:02d}00_self_{c}" for i, c in enumerate(conds)]
    out += [f"20261012-09{i:02d}00_self_{c}" for i, c in enumerate(conds)]
    return out


class ThroatSplitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def mixed_root(self):
        m1 = sp10_names()
        for n in m1:
            make_session(self.root, n)
        det = [f"20261005-12{i:02d}00_self_meal" for i in range(3)]
        for n in det:
            make_session(
                self.root, n, meta={"subject": "self", "fw": "detector"}
            )
        th = throat_names()
        for n in th:
            make_throat(self.root, n)
        return m1, det, th

    def test_t1_m1_lists_throat_sessions_separately(self):
        m1, det, th = self.mixed_root()
        r = split_sessions(self.root, eval_min=14)
        self.assertEqual(r["throat"], th)
        self.assertFalse(set(th) & set(r["train"] + r["eval"]))
        self.assertEqual(r["skipped"], [])

    def test_t2_m1_split_unchanged_by_throat_sessions(self):
        m1 = sp10_names()
        for n in m1:
            make_session(self.root, n)
        before = split_sessions(self.root, eval_min=14)
        for n in throat_names():
            make_throat(self.root, n)
        after = split_sessions(self.root, eval_min=14)
        self.assertEqual(after["train"], before["train"])
        self.assertEqual(after["eval"], before["eval"])
        self.assertEqual(after["train"], m1[:7])
        self.assertEqual(after["eval"], m1[7:])

    def test_t3_throat_kind_splits_only_throat_sessions(self):
        m1, det, th = self.mixed_root()
        # 最後の 1 収集日（20261012）の 7 本を評価側に
        r = split_sessions(self.root, eval_min=7, kind="throat")
        self.assertEqual(r["eval"], th[14:])
        self.assertEqual(r["train"], th[:14])
        self.assertTrue(all(n.startswith("20261012") for n in r["eval"]))
        self.assertTrue(all(n.startswith("20261011") for n in r["train"]))
        self.assertEqual(r["excluded"], sorted(m1 + det))
        self.assertNotIn("detector", r)
        self.assertNotIn("throat", r)

    def assert_stops(self, kind="throat"):
        with self.assertRaises(SystemExit) as cm:
            split_sessions(self.root, eval_min=3, kind=kind)
        return str(cm.exception)

    def test_t4_throat_session_checks(self):
        cases = {
            "wireless_iface": dict(
                meta={
                    **THROAT_META,
                    "sensors": [
                        {"id": "throat", "iface": "sh12jk-nz210c-a2dp-unoq"}
                    ],
                }
            ),
            "no_sensors": dict(meta={"subject": "self", "fw": "pc-throat"}),
            "no_throat_wav": dict(files=("throat_chunks.csv", "events.csv")),
            "no_chunks": dict(files=("throat.wav", "events.csv")),
            "imu_csv": dict(
                files=(
                    "throat.wav",
                    "throat_chunks.csv",
                    "events.csv",
                    "imu.csv",
                )
            ),
            "audio_wav": dict(
                files=(
                    "throat.wav",
                    "throat_chunks.csv",
                    "events.csv",
                    "audio.wav",
                )
            ),
            "feat_csv": dict(
                files=(
                    "throat.wav",
                    "throat_chunks.csv",
                    "events.csv",
                    "feat.csv",
                )
            ),
        }
        ns = throat_names()[:4]
        for label, kw in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as d:
                self.root = Path(d)
                for n in ns[:3]:
                    make_throat(self.root, n)
                make_throat(self.root, ns[3], **kw)
                self.assertIn(ns[3], self.assert_stops())
        # fw が無いのに throat.wav がある → どちらの kind でも止まる
        for kind in ("m1", "throat"):
            for meta in (
                {"subject": "self"},
                {"subject": "self", "fw": "detector"},
            ):
                with (
                    self.subTest(kind=kind, meta=meta),
                    tempfile.TemporaryDirectory() as d,
                ):
                    self.root = Path(d)
                    for n in names("self", 4):
                        make_session(self.root, n)
                    for n in ns[:4]:
                        make_throat(self.root, n)
                    bad = "20261013-090000_self_water"
                    make_throat(self.root, bad, meta=meta)
                    self.assertIn(bad, self.assert_stops(kind))

    def test_t5_only_trial_throat_is_skipped(self):
        m1 = sp10_names()
        for n in m1:
            make_session(self.root, n)
        th = throat_names()
        for n in th:
            make_throat(self.root, n)
        trial = self.root / "trial-throat"
        trial.mkdir()
        # 中身は読まない（形式外のフォルダやファイルがあっても止まらない）
        (trial / "notes_x").mkdir()
        (trial / "meta.json").write_text("{", encoding="utf-8")
        for kind, eval_min in (("m1", 14), ("throat", 7)):
            with self.subTest(kind=kind):
                r = split_sessions(self.root, eval_min=eval_min, kind=kind)
                self.assertEqual(r["skipped"], ["trial-throat"])
        for label in ("trial-throat2", "link"):
            with self.subTest(label), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                for n in th:
                    make_throat(root, n)
                if label == "link":
                    (root / "real").mkdir()
                    shutil_target = root / "real"
                    (root / "trial-throat").symlink_to(
                        shutil_target, target_is_directory=True
                    )
                else:
                    (root / label).mkdir()
                with self.assertRaises(SystemExit):
                    split_sessions(root, eval_min=7, kind="throat")

    def test_t6_throat_split_does_not_open_wav_or_events(self):
        import builtins, pathlib, wave
        from unittest import mock

        self.mixed_root()
        forbidden = {"throat.wav", "events.csv", "throat_chunks.csv"}
        real_open, real_read = builtins.open, pathlib.Path.read_text
        real_popen, real_wave = pathlib.Path.open, wave.open

        def check(p):
            if Path(p).name in forbidden:
                raise AssertionError(f"開かれた: {p}")

        def g_open(p, *a, **kw):
            check(p)
            return real_open(p, *a, **kw)

        def g_read(self_, *a, **kw):
            check(self_)
            return real_read(self_, *a, **kw)

        def g_popen(self_, *a, **kw):
            check(self_)
            return real_popen(self_, *a, **kw)

        def g_wave(p, *a, **kw):
            check(p)
            return real_wave(p, *a, **kw)

        with (
            mock.patch("builtins.open", g_open),
            mock.patch.object(pathlib.Path, "read_text", g_read),
            mock.patch.object(pathlib.Path, "open", g_popen),
            mock.patch("wave.open", g_wave),
        ):
            r = split_sessions(self.root, eval_min=7, kind="throat")
            split_sessions(self.root, eval_min=14)
        self.assertEqual(len(r["eval"]), 7)

    def test_t6b_unknown_kind_stops(self):
        self.mixed_root()
        with self.assertRaises(SystemExit):
            split_sessions(self.root, eval_min=7, kind="m2")

    def test_sp12e_unoq_detector_sessions_are_listed_like_detector(self):
        """M4 の装置のセッション（fw = unoq-detector、#47）は detector と同じ
        扱い: M1 の分割に入れず detector に、--kind throat では excluded に。
        detect.csv・feat.csv があっても止まらない（中身は開かない）。"""
        UNOQ = {"subject": "self", "fw": "unoq-detector"}
        m1 = sp10_names()
        for n in m1:
            make_session(self.root, n)
        th = throat_names()
        for n in th:
            make_throat(self.root, n)
        dev = [f"20261030-12{i:02d}00_self_water" for i in range(2)]
        for n in dev:
            make_session(self.root, n, meta=dict(UNOQ))
            for f in ("detect.csv", "feat.csv", "detect_rx.csv", "events.csv"):
                # 開けば読めない中身（開かないことの確かめ）
                (self.root / n / f).write_bytes(b"\xff\xfe")
        r = split_sessions(self.root, eval_min=14)
        self.assertEqual(r["train"], m1[:7])
        self.assertEqual(r["eval"], m1[7:])
        self.assertEqual(r["detector"], dev)
        r = split_sessions(self.root, eval_min=7, kind="throat")
        self.assertEqual(r["train"], th[:14])
        self.assertEqual(r["eval"], th[14:])
        self.assertEqual(r["excluded"], sorted(m1 + dev))
        # p1 の装置のセッションは self の detector に入れない
        make_session(
            self.root,
            "20261030-130000_p1_water",
            meta={"subject": "p1", "fw": "unoq-detector"},
        )
        self.assertEqual(
            split_sessions(self.root, eval_min=14)["detector"], dev
        )
        # 別の fw の値で detect.csv があれば今までどおり止まる
        bad = "20261030-140000_self_water"
        make_session(self.root, bad, meta={"subject": "self", "fw": "unoq"})
        (self.root / bad / "detect.csv").write_text("t_ms\n", encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            split_sessions(self.root, eval_min=14)
        self.assertIn(bad, str(cm.exception))


if __name__ == "__main__":
    unittest.main()
