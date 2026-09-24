"""m2_threshold.py を合成データと偽の実行ファイルで検証する（Issue #22 第 10 節）。

実行: python -m unittest discover -s analysis -v
合成の M1 形式のセッション（収集日1〜4 × 7 本。check_split を通す最小の構成。3〜8 秒）は test_ei_upload.build_root で tempfile に作る。
特徴量は偽の抽出（窓ごとの 1 番目の値 = 狙った確率）に差し替え、偽の実行ファイル（test_m2_scorer）が 1 番目の値を swallow の確率にする。
data/ と実データ、analysis/m2_norm.json の書き換え、firmware/detector/ への書き込みはしない。git は git_dirty を差し替える。g++ は要らない。
"""
from __future__ import annotations
import contextlib, io, json, shutil, tempfile, unittest
from pathlib import Path
from unittest import mock

import numpy as np

import ei_upload, evaluate, features, features_m2, m2_norm_header, m2_scorer, m2_threshold as mt, train_eval
from test_ei_upload import build_root, session_name
from test_m2_scorer import write_fake_bin

DUR = {"20260921": 3, "20260922": 3, "20260923": 3, "20260924": 8}
N_WINDOWS = 20                    # 0.0〜4.75 秒（収集日4 のセッションは 8 秒）
MARKER = 0.123456789              # 報告に出てはいけない窓の確率
# cond → {窓の開始（秒）: swallow の確率}。build_root の events: water / saliva は s@1.0、cough は c@1.0、meal は s@1.0 と c@2.0
SPEC = {
    "water": {1.0: 0.9, 4.0: 0.3},        # 検出（thr ≤ 0.9）と誤検出（thr ≤ 0.3）
    "saliva": {1.0: 0.6},                 # 検出（thr ≤ 0.6）
    "meal": {1.0: 0.4, 4.5: 0.55},        # 検出（thr ≤ 0.4）。4.5 は c@2.0 に紐づく誤検出（thr ≤ 0.55）
    "quiet": {3.0: 0.95},                 # 誤検出（全候補）
    "cough": {1.0: MARKER},               # c@1.0 に紐づく誤検出（thr ≤ 0.1）
    "talk": {2.0: 1.0},                   # 無効な窓（陽性にならない）
    "neck": {},
}
INVALID = {"talk": {2.0}}


def fake_extract(session_dir: Path) -> features.WindowFeatures:
    name = Path(session_dir).name
    cond = name.rsplit("_", 1)[-1]
    t = 0.25 * np.arange(N_WINDOWS)
    X = np.zeros((N_WINDOWS, features.N_FEATURES), dtype=np.float32)
    valid = np.ones(N_WINDOWS, dtype=bool)
    for w, p in SPEC.get(cond, {}).items():
        X[int(round(w / 0.25)), 0] = p
    for w in INVALID.get(cond, ()):
        valid[int(round(w / 0.25))] = False
    return features.WindowFeatures(session=name, t_start_s=t, X=X, valid=valid)


class Base(unittest.TestCase):
    """合成データ・定数・偽の実行ファイルはクラスをまたいで 1 回だけ作る。"""
    tmp = None

    @classmethod
    def setUpClass(cls):
        if Base.tmp is None:
            tmp = tempfile.TemporaryDirectory()
            unittest.addModuleCleanup(tmp.cleanup)
            Base.tmp = Path(tmp.name)
            Base.root = build_root(Base.tmp / "raw", DUR, invalid=False, p1=False)
            doc = json.loads(m2_scorer.NORM_PATH.read_text(encoding="utf-8"))
            doc["stats_sessions"] = [session_name(d, k) for d in ("20260921", "20260922", "20260923") for k in range(7)]
            doc["n_windows"] = 999
            Base.norm = Base.tmp / "m2_norm.json"
            Base.norm.write_text(json.dumps(doc), encoding="utf-8")
            Base.norm_header = Base.tmp / "m2_norm.h"
            m2_norm_header.run(Base.norm, Base.norm_header)
            Base.bin = write_fake_bin(Base.tmp, norm_path=Base.norm)
            Base.eval_names = [session_name("20260924", k) for k in range(7)]

    def setUp(self):
        self._case = tempfile.TemporaryDirectory()
        self.addCleanup(self._case.cleanup)
        self.case = Path(self._case.name)
        self.thr_header = self.case / "m2_threshold.h"

    def run_(self, freeze=False, *, root=None, norm=None, norm_header=None, git_dirty=lambda: False, fake=True, today="2026-09-30"):
        ctx = mock.patch.object(features_m2, "extract_session", side_effect=fake_extract) if fake else contextlib.nullcontext()
        with ctx:
            return mt.run(root or self.root, freeze, argv=["data/raw"] + (["--freeze"] if freeze else []), scorer_bin=self.bin,
                          norm_path=norm or self.norm, norm_header_path=norm_header or self.norm_header,
                          threshold_header_path=self.thr_header, git_dirty=git_dirty, today=today)


class RuleTest(unittest.TestCase):
    def table(self, seed: int) -> list:
        rng = np.random.default_rng(seed)
        rows = []
        for thr in train_eval.THRESHOLDS:
            det = int(rng.integers(0, 11))
            fp = int(rng.integers(0, 6))
            rows.append({"threshold": thr, "swallows": 10, "detected": det, "detection_rate": det / 10,
                         "false_positive_runs": fp, "non_swallow_min": 1.25, "false_positives_per_min": fp / 1.25})
        return rows

    def test_r1_same_as_train_eval_at_3(self):
        for seed in range(30):
            t = self.table(seed)
            self.assertEqual(mt.pick_threshold(t, 3.0)[0], train_eval.pick_threshold(t)[0], seed)

    def test_r2_limit_is_1_and_inclusive(self):
        self.assertEqual(mt.FP_PER_MIN_LIMIT, 1.0)
        rows = [{"threshold": 0.5, "detection_rate": 0.9, "false_positives_per_min": 1.0},
                {"threshold": 0.6, "detection_rate": 0.8, "false_positives_per_min": 0.5}]
        self.assertEqual(mt.pick_threshold(rows, 1.0)[0], 0.5)          # 1.0 ちょうどは候補
        rows[0]["false_positives_per_min"] = 1.01
        self.assertEqual(mt.pick_threshold(rows, 1.0)[0], 0.6)

    def test_r3_no_candidate_gives_min_fp_then_higher(self):
        rows = [{"threshold": 0.5, "detection_rate": 0.9, "false_positives_per_min": 2.0},
                {"threshold": 0.6, "detection_rate": 0.5, "false_positives_per_min": 1.5},
                {"threshold": 0.7, "detection_rate": 0.1, "false_positives_per_min": 1.5}]
        thr, reason = mt.pick_threshold(rows, 1.0)
        self.assertEqual(thr, 0.7)
        self.assertIn("候補が無い", reason)

    def test_r4_tie_on_detection_picks_higher(self):
        rows = [{"threshold": 0.3, "detection_rate": 1.0, "false_positives_per_min": 0.2},
                {"threshold": 0.4, "detection_rate": 1.0, "false_positives_per_min": 0.9}]
        self.assertEqual(mt.pick_threshold(rows, 1.0)[0], 0.4)


class RunTest(Base):
    def test_t1_table_rule_and_aggregate(self):
        r = self.run_()
        self.assertEqual(r.eval_names, self.eval_names)
        self.assertEqual([s.name for s in r.scored], self.eval_names)
        self.assertEqual(list(r.per_session), self.eval_names)
        self.assertEqual(r.total["sessions"], self.eval_names)
        by = {row["threshold"]: row for row in r.table}
        self.assertEqual(len(by), 19)
        self.assertEqual((by[0.5]["swallows"], by[0.5]["detected"], by[0.5]["false_positive_runs"]), (3, 2, 2))   # quiet、meal
        self.assertEqual((by[0.95]["detected"], by[0.95]["false_positive_runs"]), (0, 1))
        self.assertEqual((by[0.1]["detected"], by[0.1]["false_positive_runs"]), (3, 4))                            # + water、cough
        self.assertEqual(r.threshold, mt.pick_threshold(r.table, 1.0)[0])
        # 8 秒 × 7 本で非嚥下は約 50 秒。誤検出 1 回でも 1.0 回/分を超えるので、規則は「候補が無い → 誤検出率最小、同率なら高いほう」
        self.assertEqual(r.threshold, 0.95)
        self.assertIn("候補が無い", r.rule)
        self.assertEqual(r.total["confusion"], {"tp": 0, "fn": 3, "fp": 1, "tn": None})
        self.assertIsNone(r.header_text)

    def test_t2_invalid_window_is_never_positive(self):
        r = self.run_()
        talk = next(s for s in r.scored if s.cond == "talk")
        self.assertFalse(talk.valid[8])
        self.assertEqual(mt.positive_windows(talk, 0.05), [])
        self.assertEqual(r.per_session[talk.name]["false_positive_runs"], 0)
        self.assertEqual(r.reference["invalid_windows"], 1)

    def test_t3_reference_and_window_confusion(self):
        r = self.run_()
        ref = r.reference
        self.assertEqual(ref["windows"], 7 * N_WINDOWS)
        self.assertEqual(ref["positive_windows"], 1)                               # quiet の 0.95 だけ
        self.assertEqual(ref["always_positive"]["detected"], 3)
        linked = ref["cough_linked"]
        meal = next(v for k, v in linked.items() if k.endswith("meal"))
        self.assertEqual((meal["coughs"], meal["fp_runs"], meal["linked_runs"]), (1, 0, 0))      # 0.95 では meal の 0.55 は陰性
        # 0.5 で数えると meal の 4.5 秒の塊（中心 5.0 − t_c 2.0 = 3.0 秒）が c に紐づく
        self.assertEqual(mt.cough_linked(next(s for s in r.scored if s.cond == "meal"), 0.5)["linked_runs"], 1)
        w = r.window
        n_upload = sum(int(s.upload_mask.sum()) for s in r.scored)
        self.assertEqual(w["windows"], n_upload)
        self.assertEqual(sum(sum(row) for row in w["matrix"]), n_upload)
        # 偽の実行ファイルの argmax は p > 0.5 で swallow、それ以外は other。swallow の窓（中心が [1.0, 2.0]）で p > 0.5 は water 1.0（0.9）と saliva 1.0（0.6）
        i_s, i_o = mt.CLASS_ORDER.index("swallow"), mt.CLASS_ORDER.index("other")
        self.assertEqual(w["matrix"][i_s][i_s], 2)
        self.assertEqual(w["matrix"][i_o][i_s], 2)                                 # quiet の 3.0（0.95）と meal の 4.5（0.55）。talk の 2.0 は無効
        self.assertEqual(w["per_class"]["swallow"], 3 * 5)                          # s 1 回 = 中心 [1.0, 2.0] の 5 窓 × 3 本

    def test_t4_stats_sessions_with_day4_stops(self):
        doc = json.loads(self.norm.read_text(encoding="utf-8"))
        doc["stats_sessions"][0] = self.eval_names[0]
        norm = self.case / "m2_norm.json"
        norm.write_text(json.dumps(doc), encoding="utf-8")
        header = self.case / "m2_norm.h"
        m2_norm_header.run(norm, header)
        with self.assertRaises(SystemExit) as cm:
            self.run_(norm=norm, norm_header=header)
        self.assertIn("収集日4", str(cm.exception))

    def test_t5_norm_header_mismatch_stops(self):
        text = self.norm_header.read_text(encoding="utf-8")
        doc = json.loads(self.norm.read_text(encoding="utf-8"))
        old = m2_norm_header.fmt_double(doc["mean"][2])
        header = self.case / "m2_norm.h"
        header.write_text(text.replace(old, m2_norm_header.fmt_double(doc["mean"][2] + 1e-9), 1), encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            self.run_(norm_header=header)
        self.assertIn("一致しません", str(cm.exception))
        with self.assertRaises(SystemExit):                                        # 無い
            self.run_(norm_header=self.case / "missing.h")

    def test_t6_split_check_stops(self):
        root = self.case / "raw"
        shutil.copytree(self.root, root)
        shutil.rmtree(root / self.eval_names[0])
        with self.assertRaises(SystemExit):
            self.run_(root=root)

    def test_t7_freeze_dirty_stops(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_(freeze=True, git_dirty=lambda: True)
        self.assertIn("未コミット", str(cm.exception))
        self.assertFalse(self.thr_header.exists())
        self.run_(freeze=False, git_dirty=lambda: True)                            # --freeze 無しは止めない

    def test_t8_freeze_writes_header_and_rerun_matches(self):
        r = self.run_(freeze=True)
        self.assertTrue(r.header_written)
        text = self.thr_header.read_text(encoding="utf-8")
        self.assertEqual(text, r.header_text)
        self.assertIn("static const float M2_THRESHOLD = 0.95f;", text)
        self.assertIn("#define M2_THRESHOLD_FP_PER_MIN_LIMIT 1.0", text)
        self.assertIn("0.949999988", text)                                          # float32 の 9 桁
        self.assertIn("2026-09-30", text)
        self.assertIn("deploy version 7", text)
        self.assertIn("#27", text)
        self.assertNotIn("Arduino", text)
        r2 = self.run_(freeze=True)
        self.assertFalse(r2.header_written)
        self.assertEqual(r2.header_text, text)
        self.thr_header.write_text(text.replace("0.95f", "0.9f"), encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            self.run_(freeze=True)
        self.assertIn("内容が違います", str(cm.exception))
        # 別の日に同じ閾値を凍結し直すと本文が違う（人が消してから作り直す）
        with self.assertRaises(SystemExit):
            self.run_(freeze=True, today="2026-10-01")

    def test_t9_report_has_no_window_values(self):
        r = self.run_(freeze=True)
        text = mt.format_report(r)
        for word in (f"{MARKER:.9g}", "0.1234", "0.123456"):
            self.assertNotIn(word, text)
        for word in ("誤嚥", "aspiration", "risk", "LED", "VE の代替"):
            self.assertNotIn(word, text)
        self.assertIn("参考値", text)
        self.assertIn("#27", text)
        self.assertIn("| 0.95 ← |", text)
        self.assertIn("TP 0、FN 3、FP 1", text)
        for name in self.eval_names:
            self.assertIn(name, text)
        self.assertIn("M2_THRESHOLD = 0.95f", text)
        # ID は出さない（偽の実行ファイルの project_id 123456）
        self.assertNotIn("123456", text)
        self.assertIn("deploy version 7", text)
        # main の経路
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch.object(mt, "run", return_value=r):
            mt.main(["data/raw"])
        self.assertIn("採用した閾値", buf.getvalue())

    def test_t10_days_1_to_3_are_not_opened(self):
        seen_extract, seen_span = [], []
        orig_span = evaluate.session_span

        def rec_extract(d):
            seen_extract.append(Path(d).name)
            return fake_extract(d)

        def rec_span(d):
            seen_span.append(Path(d).name)
            return orig_span(d)

        with mock.patch.object(features_m2, "extract_session", side_effect=rec_extract), \
             mock.patch.object(mt.evaluate, "session_span", side_effect=rec_span):
            mt.run(self.root, False, scorer_bin=self.bin, norm_path=self.norm, norm_header_path=self.norm_header,
                   threshold_header_path=self.thr_header, git_dirty=lambda: False)
        self.assertEqual(seen_extract, self.eval_names)
        self.assertEqual(seen_span, self.eval_names)

    def test_t11_real_extractor_runs(self):
        """偽の抽出を使わず features_m2.extract_session で収集日4 の合成セッションを読む（値は見ない。経路が通ることだけ）。"""
        r = self.run_(fake=False)
        self.assertEqual(len(r.scored), 7)
        self.assertTrue(all(len(s.prob) > 0 for s in r.scored))
        self.assertEqual(len(r.table), 19)


if __name__ == "__main__":
    unittest.main()
