"""evaluate_detector.py を合成データで検証する（#23。記録の範囲の読み替えは docs/decisions/0021）。

実行: python -m unittest discover -s analysis -v
合成の detect.csv・feat.csv・events.csv・meta.json は tempfile に作る（60 秒、240 窓）。data/ と実データは使わない。
"""
from __future__ import annotations
import json, struct, tempfile, unittest
from pathlib import Path

import evaluate
import evaluate_detector as ed
from train_eval import sum_metrics

N_WINDOWS = 240
LAG_MS = 30              # 送信時刻 − 窓の終端（合成の値）
N_FEAT = 5
THRESHOLD = 0.9
MODEL = {"source": "edge-impulse", "project_id": 0, "deploy_version": 0}
FEATURE_NAMES = [f"x{i}" for i in range(N_FEAT)]
F32_09 = ed.f32(THRESHOLD)                                                  # 0.899999976
F32_09_BELOW = struct.unpack("<f", struct.pack("<I", struct.unpack("<I", struct.pack("<f", THRESHOLD))[0] - 1))[0]


def meta(**over) -> dict:
    m = {"subject": "self", "cond": "meal", "fw": "detector", "imu_hz": 104, "audio_hz": 16000,
         "window_ms": 1000, "hop_ms": 250, "threshold": THRESHOLD, "model": dict(MODEL), "feature_set": "0020",
         "feature_names": list(FEATURE_NAMES)}
    m.update(over)
    return m


def windows(n: int = N_WINDOWS, start_ms: int = 1000) -> list[int]:
    return [start_ms + 250 * i for i in range(n)]


def make_session(root: Path, name: str, *, w: list[int] | None = None, t: list[int] | None = None,
                 positives=(), probs: dict | None = None, feat: bool = True, feat_w: list[int] | None = None,
                 feat_t: list[int] | None = None, f0: dict | None = None, events=(), m: dict | None = None,
                 detect_rows: list[str] | None = None) -> Path:
    """合成セッション。positives は陽性にする窓の開始（秒）。probs / f0 は窓の開始（秒）→ 値。"""
    d = root / name
    d.mkdir()
    w = windows() if w is None else w
    t = [x + 1000 + LAG_MS for x in w] if t is None else t
    pos = {round(p * 1000) for p in positives}
    probs = probs or {}
    lines = ["t_ms,window_t_ms,positive,prob,led"]
    for ti, wi in zip(t, w):
        p = 1 if wi in pos else 0
        prob = probs.get(wi / 1000.0, 0.95 if p else 0.05)
        lines.append(f"{ti},{wi},{p},{prob:.9g},{1 + p}")
    if detect_rows is not None:
        lines = ["t_ms,window_t_ms,positive,prob,led"] + detect_rows
    (d / "detect.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if feat:
        fw = w if feat_w is None else feat_w
        ft = [x + 1000 + LAG_MS + 2 for x in fw] if feat_t is None else feat_t
        f0 = f0 or {}
        flines = [",".join(["t_ms", "window_t_ms"] + [f"f{i}" for i in range(N_FEAT)])]
        for ti, wi in zip(ft, fw):
            v0 = f0.get(wi / 1000.0, 0.5)
            flines.append(",".join([str(ti), str(wi), f"{v0:.9g}"] + ["1.5"] * (N_FEAT - 1)))
        (d / "feat.csv").write_text("\n".join(flines) + "\n", encoding="utf-8")
    ev = ["t_ms,label,note"] + [f"{t_ms},{label},{note}" for t_ms, label, note in events]
    (d / "events.csv").write_text("\n".join(ev) + "\n", encoding="utf-8")
    (d / "meta.json").write_text(json.dumps(meta() if m is None else m), encoding="utf-8")
    return d


def name(i: int, cond: str = "meal", day: str = "20261005", subject: str = "self") -> str:
    return f"{day}-12{i:02d}00_{subject}_{cond}"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def load(self, d: Path) -> ed.SessionData:
        return ed.load_session(d, json.loads((d / "meta.json").read_text(encoding="utf-8")))


class SpanTest(Base):
    def test_d1_span_from_detect_and_feat(self):
        s = self.load(make_session(self.root, name(0)))
        self.assertAlmostEqual(s.t0_s, 1.0, places=6)
        self.assertAlmostEqual(s.t0_s + s.duration_s, 61.782, places=6)        # FEAT の送信時刻
        s = self.load(make_session(self.root, name(1), feat=False))
        self.assertAlmostEqual(s.t0_s + s.duration_s, 61.78, places=6)         # DETECT の送信時刻

    def test_d1b_window_end_after_send_time(self):
        w = windows()
        s = self.load(make_session(self.root, name(0), w=w, t=[x + 500 for x in w], feat=False))
        self.assertAlmostEqual(s.t0_s + s.duration_s, 61.75, places=6)         # 最後の窓の終端

    def test_d2_gaps_and_backwards_stop(self):
        w = windows()
        cases = {
            "detect の window_t_ms の飛び": dict(w=w[:100] + [x + 1250 for x in w[100:]]),
            "detect の window_t_ms の逆行": dict(w=w[:50] + [w[48]] + w[51:]),
            "detect の t_ms の逆行": dict(t=[x + 1030 for x in w[:50]] + [w[48] + 1030] + [x + 1030 for x in w[51:]]),
            "feat の window_t_ms の飛び": dict(feat_w=w[:100] + [x + 1250 for x in w[100:]]),
            "feat の t_ms の逆行": dict(feat_t=[x + 1032 for x in w[:50]] + [w[48] + 1032] + [x + 1032 for x in w[51:]]),
        }
        for label, kw in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                d = make_session(Path(tmp), name(0), **kw)
                with self.assertRaises(SystemExit) as cm:
                    self.load(d)
                self.assertIn(name(0), str(cm.exception))
        # 1000 ms ちょうどは通る
        w1 = [1000 + 1000 * i for i in range(60)]
        s = self.load(make_session(self.root, name(1), w=w1))
        self.assertEqual(len(s.rows), 60)

    def test_d2c_feat_longer_than_detect_stops(self):
        d = make_session(self.root, name(0), feat_w=windows(480))               # FEAT が 120 秒まで続く
        with self.assertRaises(SystemExit):
            self.load(d)
        w = windows()
        s = self.load(make_session(self.root, name(1), feat_w=w[:100] + w[103:]))   # FEAT の取りこぼし 3 窓
        self.assertEqual(s.feat_missing, 3)
        self.assertAlmostEqual(s.t0_s + s.duration_s, 61.782, places=6)

    def test_d2b_send_lag_limit(self):
        w = windows()
        s = self.load(make_session(self.root, name(0), t=[x + 2000 for x in w], feat=False))   # 遅れ 1000 ms ちょうど
        self.assertAlmostEqual(s.t0_s + s.duration_s, 62.75, places=6)
        cases = {
            "detect の 1 行が 1001 ms": dict(t=[x + 1030 for x in w[:-1]] + [w[-1] + 2001], feat=False),
            "feat の 1 行が 1001 ms": dict(feat_t=[x + 1032 for x in w[:-1]] + [w[-1] + 2001]),
            "遅れが少しずつ増える": dict(t=[x + 1000 + 5 * i for i, x in enumerate(w)], feat=False),
        }
        for label, kw in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(SystemExit):
                    self.load(make_session(Path(tmp), name(1), **kw))

    def test_d3_marker_after_last_feat_is_inside_range(self):
        last_feat_t = windows()[-1] + 1000 + LAG_MS + 2
        s = self.load(make_session(self.root, name(0), events=[(last_feat_t, "s", "")]))
        m = ed.session_metrics(s)
        self.assertEqual(m["swallows"], 1)
        for label, t_ms in (("t_ms = 0", 0), ("最初の窓より前", 500)):
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                s = self.load(make_session(Path(tmp), name(1), events=[(t_ms, "s", "")]))
                with self.assertRaises(SystemExit):
                    ed.session_metrics(s)


class MetricsTest(Base):
    def test_d4_detected_and_false_positive(self):
        s = self.load(make_session(self.root, name(0), positives=[10.0], events=[(10000, "s", "")]))
        m = ed.session_metrics(s)
        self.assertEqual((m["detected"], m["false_positive_runs"]), (1, 0))
        self.assertEqual(m, evaluate.event_metrics([10.0], [10.0], s.duration_s, s.t0_s))
        s = self.load(make_session(self.root, name(1), positives=[30.0], events=[(10000, "s", "")]))
        m = ed.session_metrics(s)
        self.assertEqual((m["detected"], m["false_positive_runs"]), (0, 1))

    def three(self, **kw):
        for i in range(3):
            make_session(self.root, name(i), positives=[10.0, 30.0], events=[(10000, "s", ""), (20000, "s", "")], **kw)

    def test_d5_three_sessions_aggregate(self):
        self.three()
        r = ed.run(self.root)
        self.assertEqual([d.name for d in r.used], [name(0), name(1), name(2)])
        self.assertEqual(r.total["sessions"], [name(0), name(1), name(2)])
        self.assertEqual(r.total["confusion"], {"tp": 3, "fn": 3, "fp": 3, "tn": None})
        self.assertEqual(r.total["detection_rate"], 0.5)
        text = ed.format_report(r)
        self.assertIn("| 嚥下 | TP 3 | FN 3 |", text)
        self.assertIn("合格線との比較", text)

    def test_d6_fewer_than_three(self):
        for i in range(2):
            make_session(self.root, name(i), positives=[10.0], events=[(10000, "s", "")])
        with self.assertRaises(SystemExit):
            ed.run(self.root)
        r = ed.run(self.root, sessions=[name(0), name(1)])
        self.assertIsNone(r.total)
        self.assertEqual(r.per_session[name(0)]["detected"], 1)
        text = ed.format_report(r)
        self.assertIn("合算は出さない", text)
        self.assertNotIn("合格線との比較", text)

    def test_d7_meal_without_detector_fw_stops(self):
        self.three()
        for fw in (None, "logger"):
            with self.subTest(fw=fw), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                make_session(root, name(0))
                m = meta(); m.pop("fw") if fw is None else m.update(fw=fw)
                make_session(root, name(1), m=m)
                with self.assertRaises(SystemExit):
                    ed.run(root)
        make_session(self.root, name(5, cond="water"), m=meta(cond="water", fw="logger"))
        r = ed.run(self.root)                                        # 記録ファームウェアの water は対象にならない
        self.assertEqual([c.name for c in r.candidates], [name(0), name(1), name(2)])
        with self.assertRaises(SystemExit):
            ed.run(self.root, sessions=[name(5, cond="water")])

    def test_d8_firmware_mismatch_stops(self):
        for key, value in (("threshold", 0.8), ("model", {**MODEL, "deploy_version": 1}), ("feature_names", FEATURE_NAMES[:4])):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for i in range(2):
                    make_session(root, name(i))
                make_session(root, name(2), m=meta(**{key: value}))
                with self.assertRaises(SystemExit) as cm:
                    ed.run(root)
                self.assertIn(key, str(cm.exception))

    def test_d9_value_ranges(self):
        cases = {"positive": "2030,1000,2,0.5,1", "led": "2030,1000,0,0.5,3", "prob": "2030,1000,1,1.5,2"}
        for label, row in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                d = make_session(Path(tmp), name(0), detect_rows=[row, "2280,1250,0,0.5,1"])
                with self.assertRaises(SystemExit):
                    self.load(d)

    def test_d10_reference_values(self):
        # EV16 と同じ構成: 窓 0.0..59.0、記録 60 秒、嚥下 30.0、全窓が陽性
        w = [250 * i for i in range(237)]
        d = make_session(self.root, name(0), w=w, t=[x + 1000 for x in w], feat=False,
                         positives=[x / 1000 for x in w], events=[(30000, "s", "")])
        s = self.load(d)
        ref = ed.reference([s])
        a = ref["always_positive"]
        self.assertEqual((a["detected"], a["false_positive_runs"]), (1, 1))
        self.assertAlmostEqual(a["false_positives_per_min"], 1.0345, places=4)
        self.assertEqual(ref["positive_ratio"], 1.0)
        self.assertEqual(ref["mismatch"][name(0)], 0)

    def test_d10b_threshold_compared_as_float32(self):
        rows = [f"2030,1000,1,{F32_09:.9g},2", f"2280,1250,1,{F32_09_BELOW:.9g},2", "2530,1500,0,0.5,1"]
        s = self.load(make_session(self.root, name(0), detect_rows=rows, feat=False))
        self.assertLess(F32_09, THRESHOLD)                       # float64 の 0.9 と比べると偽になる値
        self.assertEqual(ed.mismatch_count(s), 1)                # 1 ulp 下の行だけ不一致

    def test_d11_cough_linked_runs(self):
        s = self.load(make_session(self.root, name(0), positives=[24.0, 25.0], events=[(20000, "c", "")]))
        c = ed.cough_linked(s)
        self.assertEqual(c, {"coughs": 1, "fp_runs": 2, "linked_runs": 1, "coughs_with_run": 1})

    def test_d12_by_day_and_conversation(self):
        ev = [(10000, "s", "")]
        make_session(self.root, name(0, day="20261005"), positives=[10.0], events=ev)
        make_session(self.root, name(1, day="20261005"), positives=[30.0], events=ev + [(2000, "o", "conv")])
        make_session(self.root, name(0, day="20261006"), positives=[10.0, 40.0], events=ev)
        r = ed.run(self.root)
        by_day = r.reference["by_day"]
        self.assertEqual(sorted(by_day), ["20261005", "20261006"])
        self.assertEqual(by_day["20261005"], sum_metrics({n: r.per_session[n] for n in (name(0), name(1))}))
        self.assertEqual(by_day["20261006"], sum_metrics({name(0, day="20261006"): r.per_session[name(0, day="20261006")]}))
        self.assertEqual(r.reference["by_conv"]["会話あり"], sum_metrics({name(1): r.per_session[name(1)]}))
        self.assertEqual(r.reference["by_conv"]["会話なし"]["detected"], 2)
        self.assertIn("| あり |", ed.format_report(r))


class ScorerTest(Base):
    def scorer(self) -> Path:
        """偽の scorer（f0 を確率にする）。root の外に置く（import で __pycache__ ができるため）。"""
        if not hasattr(self, "_scorer_dir"):
            self._scorer_dir = tempfile.TemporaryDirectory()
            self.addCleanup(self._scorer_dir.cleanup)
        p = Path(self._scorer_dir.name) / "fake_scorer.py"
        p.write_text("def score(features, meta):\n    return [f[0] for f in features]\n", encoding="utf-8")
        return p

    def test_d13_rescore_matches_device(self):
        f0 = {w / 1000: (0.95 if w in (10000, 30000) else 0.05) for w in windows()}
        for i in range(3):
            make_session(self.root, name(i), positives=[10.0, 30.0], f0=f0, events=[(10000, "s", "")])
        r = ed.run(self.root, scorer=self.scorer())
        for s in r.rescored.values():
            self.assertEqual((s["dev1_pc0"], s["dev0_pc1"], s["max_abs_diff"]), (0, 0, 0.0))
            self.assertEqual(s["metrics"], r.per_session[name(0)])
        f0[30.0] = 0.5                                          # 1 窓だけ PC で陰性
        make_session(self.root, name(3), positives=[10.0, 30.0], f0=f0, events=[(10000, "s", "")])
        r = ed.run(self.root, sessions=[name(3)], scorer=self.scorer())
        self.assertEqual((r.rescored[name(3)]["dev1_pc0"], r.rescored[name(3)]["dev0_pc1"]), (1, 0))
        self.assertAlmostEqual(r.rescored[name(3)]["max_abs_diff"], 0.45, places=6)
        self.assertEqual(r.rescored[name(3)]["metrics"]["false_positive_runs"], 0)
        self.assertIn("再採点", ed.format_report(r))

    def test_d13b_scorer_requires_matching_windows_and_names(self):
        w = windows()
        make_session(self.root, name(0), feat_w=w[:-1])                              # 集合が違う
        with self.assertRaises(SystemExit):
            ed.run(self.root, sessions=[name(0)], scorer=self.scorer())
        make_session(self.root, name(1), m=meta(feature_names=FEATURE_NAMES[:4]))    # feature_names の長さが違う
        with self.assertRaises(SystemExit):
            ed.run(self.root, sessions=[name(1)], scorer=self.scorer())
        r = ed.run(self.root, sessions=[name(1)])                                    # scorer なしなら長さは問わない
        self.assertEqual(r.used[0].dims, N_FEAT)

    def test_d10c_scorer_compares_threshold_as_float32(self):
        rows = [f"2030,1000,1,{F32_09:.9g},2", "2280,1250,0,0.5,1"]
        make_session(self.root, name(0), detect_rows=rows, feat_w=[1000, 1250], f0={1.0: F32_09, 1.25: 0.5})
        r = ed.run(self.root, sessions=[name(0)], scorer=self.scorer())
        self.assertEqual((r.rescored[name(0)]["dev1_pc0"], r.rescored[name(0)]["dev0_pc1"]), (0, 0))


class ReportTest(Base):
    def test_d14_report_has_no_window_values(self):
        marker = 0.123456789
        for i in range(3):
            make_session(self.root, name(i), positives=[10.0], f0={5.0: marker}, probs={7.0: 0.0777},
                         events=[(10000, "s", "")])
        self.assertFalse((self.root / name(0) / "imu.csv").exists())
        text = ed.format_report(ed.run(self.root))
        for word in (f"{marker:.9g}", "0.0777", "0.123456"):
            self.assertNotIn(word, text)
        self.assertIn("## 評価に使ったセッション（3）", text)

    def test_d15_excluded_sessions_listed(self):
        for i in range(3):
            make_session(self.root, name(i), positives=[10.0], events=[(10000, "s", "")])
        make_session(self.root, name(4, cond="water"), m=meta(cond="water"), positives=[10.0], events=[(10000, "s", "")])
        r = ed.run(self.root, sessions=[name(0), name(1), name(4, cond="water")])
        used = {c.name: (c.used, c.reason) for c in r.candidates}
        self.assertEqual(used[name(2)], (False, "--sessions に無い"))
        self.assertTrue(used[name(4, cond="water")][0])
        text = ed.format_report(r)
        self.assertIn(f"| {name(2)} | meal | 使わない | --sessions に無い |", text)
        self.assertIn("使わない（cond が meal でない）", text)
        r = ed.run(self.root)                                      # 既定では water は対象にならない
        self.assertEqual(used[name(4, cond="water")][0], True)
        self.assertEqual([c.name for c in r.candidates if c.used], [name(0), name(1), name(2)])
        self.assertIn("cond が meal でない", [c.reason for c in r.candidates if not c.used])


if __name__ == "__main__":
    unittest.main()
