"""ei_testing_result.py を偽の応答（ページ分割）で検証する（Issue #22 第 10 節）。

実行: python -m unittest discover -s analysis -v
送受信は偽の関数に差し替え、ネットワークに出ない。PC 側は偽の抽出と偽の実行ファイル（test_m2_scorer）。data/ と実データは使わない。
ファイル名が test_m2_ で始まるのは、test_ei_upload.py が import 時に addModuleCleanup で合成データを登録しており、unittest はその後始末を
最初のモジュールの終わりで全部実行するため（test_ei_upload より前に並ぶ名前だと test_ei_upload の合成データが消える）。
"""
from __future__ import annotations
import contextlib, io, json, tempfile, unittest
from pathlib import Path
from unittest import mock

import numpy as np

import ei_testing_result as er
import features, features_m2, m2_norm_header, m2_scorer, m2_threshold
from test_ei_upload import build_root, session_name
from test_m2_scorer import write_fake_bin, FAKE_MODEL

KEY = "ei_test_key_0123456789abcdef"
PID = str(FAKE_MODEL[0])
N_WINDOWS = 12


def item(session: str, t_ms: int, probs: dict, label: str | None = "other", *, as_list: bool = False, name: str | None = None) -> dict:
    """仮定している応答の 1 項目。as_list は result を [{"label", "value"}] の形にする（1 クラスだけ）。"""
    sample = {"id": t_ms, "name": name if name is not None else f"{session}_{t_ms}.json",
              "metadata": {"session": session, "t_ms": str(t_ms), "subject": "self"}}
    if label is not None:
        sample["label"] = label
    result = [{"label": k, "value": v} for k, v in probs.items()] if as_list else [dict(probs)]
    return {"sample": sample, "classifications": [{"result": result, "minimumConfidenceRating": 0.6}]}


class FakeFetch:
    def __init__(self, pages: dict, *, fail: dict | None = None):
        self.pages = pages                    # variant → 項目の並び
        self.fail = fail or {}                # variant → (status, body)
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(url).query)
        variant, limit, offset = q["variant"][0], int(q["limit"][0]), int(q["offset"][0])
        if variant in self.fail:
            return self.fail[variant]
        items = self.pages[variant][offset:offset + limit]
        return 200, json.dumps({"success": True, "result": items})


def ei_from_pc(pc, variant_shift: float = 0.0):
    return [er.EIItem(session=p.session, t_ms=p.t_ms, label=p.label,
                      probs={k: v + (variant_shift if k == "swallow" else -variant_shift / 2) for k, v in p.probs.items()})
            for p in pc]


class ParseTest(unittest.TestCase):
    def test_e1_parse_item_forms(self):
        probs = {"cough": 0.1, "other": 0.7, "swallow": 0.2}
        a = er.parse_item(item("20260924-100000_self_water", 1250, probs, "swallow"))
        self.assertEqual((a.session, a.t_ms, a.label), ("20260924-100000_self_water", 1250, "swallow"))
        self.assertEqual(a.probs, probs)
        b = er.parse_item(item("s", 5, {"swallow": 0.9}, as_list=True))
        self.assertEqual(b.probs, {"swallow": 0.9})
        # metadata が無ければ名前から
        it = item("20260924-100000_self_water", 2000, probs)
        del it["sample"]["metadata"]
        c = er.parse_item(it)
        self.assertEqual((c.session, c.t_ms), ("20260924-100000_self_water", 2000))
        with self.assertRaises(ValueError):
            er.parse_item({"sample": {"name": "noname"}, "classifications": [{"result": [probs]}]})
        with self.assertRaises(ValueError):                                   # 窓が 2 つ
            er.parse_item({"sample": {"name": "a_1"}, "classifications": [{"result": [probs, probs]}]})
        with self.assertRaises(ValueError):
            er.parse_page(json.dumps({"success": False, "error": "nope"}))

    def test_e2_fetch_all_paginates(self):
        items = [item("s", 1000 * k, {"cough": 0.0, "other": 1.0, "swallow": 0.0}) for k in range(5)]
        fetch = FakeFetch({"int8": items})
        out = er.fetch_all(fetch, PID, KEY, "int8", limit=2)
        self.assertEqual([e.t_ms for e in out], [0, 1000, 2000, 3000, 4000])
        self.assertEqual(len(fetch.calls), 3)
        self.assertTrue(all(h["x-api-key"] == KEY for _, h in fetch.calls))
        self.assertIn("variant=int8", fetch.calls[0][0])
        self.assertIn("offset=4", fetch.calls[2][0])
        self.assertEqual(er.fetch_all(FakeFetch({"int8": []}), PID, KEY, "int8", limit=2), [])
        self.assertEqual(len(er.fetch_all(fetch, PID, KEY, "int8", limit=5)), 5)   # ちょうど 1 ページ → 2 回目が空

    def test_e3_http_error_redacts_key_and_id(self):
        fetch = FakeFetch({"int8": []}, fail={"int8": (401, f"bad key {KEY} for project {PID}")})
        with self.assertRaises(SystemExit) as cm:
            er.fetch_all(fetch, PID, KEY, "int8")
        self.assertNotIn(KEY, str(cm.exception))
        self.assertNotIn(PID, str(cm.exception))
        self.assertIn("401", str(cm.exception))


class CompareTest(unittest.TestCase):
    def pc(self):
        rng = np.random.default_rng(3)
        out = []
        for k in range(20):
            p = rng.dirichlet([1, 1, 1])
            label = ("swallow", "cough", "other")[k % 3]
            out.append(er.PCItem(session="20260924-100000_self_water", t_ms=250 * k, label=label,
                                 probs={"swallow": float(p[0]), "cough": float(p[1]), "other": float(p[2])}))
        return out

    def test_e4_identical_is_match(self):
        pc = self.pc()
        c = er.compare(ei_from_pc(pc), pc, 0.5)
        self.assertTrue(c["match"])
        self.assertEqual((c["n_common"], c["n_gt0"], c["n_gt_step"], c["n_argmax"], c["n_threshold"], c["label_mismatch"]), (20, 0, 0, 0, 0, 0))
        self.assertEqual(c["max_abs"], {"swallow": 0.0, "cough": 0.0, "other": 0.0})
        self.assertTrue(c["confusion_equal"])
        self.assertEqual(sum(sum(r) for r in c["confusion_pc"]), 20)

    def test_e5_differences_are_counted(self):
        pc = self.pc()
        ei = ei_from_pc(pc)
        # 1 窓を 1/256 だけ動かす（> 0 だが > 1/256 ではない）
        e0 = ei[0]
        ei[0] = er.EIItem(e0.session, e0.t_ms, e0.label, {**e0.probs, "swallow": e0.probs["swallow"] + 1 / 256})
        # 1 窓の argmax を変える（大きく動かす）
        e1 = ei[1]
        big = {"swallow": 0.0, "cough": 0.0, "other": 0.0}
        big[min(e1.probs, key=e1.probs.get)] = 1.0
        ei[1] = er.EIItem(e1.session, e1.t_ms, e1.label, big)
        # 1 窓の閾値の側だけ変える: swallow を閾値ちょうどに（PC は閾値未満、argmax は変えない）
        e2 = ei[2]
        thr = 0.999
        ei[2] = er.EIItem(e2.session, e2.t_ms, "swallow", {**e2.probs, "swallow": thr})
        c = er.compare(ei, pc, thr)
        self.assertFalse(c["match"])
        self.assertEqual(c["n_gt0"], 3)
        self.assertEqual(c["n_gt_step"], 2)
        self.assertGreaterEqual(c["n_argmax"], 1)
        self.assertEqual(c["n_threshold"], 1)
        self.assertEqual(c["label_mismatch"], 1 if pc[2].label != "swallow" else 0)
        self.assertAlmostEqual(c["max_abs"]["swallow"], max(1 / 256, abs(thr - pc[2].probs["swallow"]),
                                                            abs(big["swallow"] - pc[1].probs["swallow"])), places=12)
        self.assertFalse(c["confusion_equal"])

    def test_e6_missing_windows(self):
        pc = self.pc()
        ei = ei_from_pc(pc)[:-2]
        c = er.compare(ei, pc, 0.5)
        self.assertEqual((c["n_ei"], c["n_pc"], c["n_common"], c["missing_in_ei"], c["missing_in_pc"]), (18, 20, 18, 2, 0))
        self.assertFalse(c["match"])
        with self.assertRaises(SystemExit):
            er.compare(ei + ei[:1], pc, 0.5)                                   # 重複
        cf = er.compare_float32(ei_from_pc(pc, 0.01), pc)
        self.assertEqual(cf["n_common"], 20)
        self.assertAlmostEqual(cf["max_abs"]["swallow"], 0.01, places=12)

    def test_e7_threshold_compared_as_float32(self):
        f32 = m2_threshold.f32
        pc = [er.PCItem("s", 0, "other", {"swallow": f32(0.85), "cough": 0.0, "other": 1 - f32(0.85)})]
        ei = [er.EIItem("s", 0, "other", {"swallow": 0.85, "cough": 0.0, "other": 1 - 0.85})]
        self.assertEqual(er.compare(ei, pc, 0.85)["n_threshold"], 0)        # 0.85 と f32(0.85) はどちらも閾値以上
        self.assertGreater(er.compare(ei, pc, 0.85)["max_abs"]["swallow"], 0)


class RunTest(unittest.TestCase):
    tmp = None

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        unittest.addModuleCleanup(tmp.cleanup)
        cls.tmp = Path(tmp.name)
        cls.root = build_root(cls.tmp / "raw", {"20260921": 3, "20260922": 3, "20260923": 3, "20260924": 4}, invalid=False, p1=False)
        cls.norm = m2_scorer.NORM_PATH                       # 実物の集計値（self）。書き換えない
        cls.bin = write_fake_bin(cls.tmp)
        cls.thr_header = cls.tmp / "m2_threshold.h"
        cls.thr_header.write_text(m2_threshold.render_threshold_header(0.85, "abc1234", 7, "2026-09-30", "規則"), encoding="utf-8")
        cls.eval_names = [session_name("20260924", k) for k in range(7)]

    @staticmethod
    def fake_extract(session_dir: Path) -> features.WindowFeatures:
        name = Path(session_dir).name
        t = 0.25 * np.arange(N_WINDOWS)
        rng = np.random.default_rng(abs(hash(name)) % 1000)
        X = np.zeros((N_WINDOWS, features.N_FEATURES), dtype=np.float32)
        X[:, 0] = rng.uniform(0, 1, N_WINDOWS).astype(np.float32)
        valid = np.ones(N_WINDOWS, dtype=bool)
        valid[3] = False
        return features.WindowFeatures(session=name, t_start_s=t, X=X, valid=valid)

    def pc(self):
        with mock.patch.object(features_m2, "extract_session", side_effect=self.fake_extract):
            scorer = m2_scorer.Scorer(self.bin, norm_path=self.norm)
            return er.pc_items(self.root, scorer)

    def run_(self, pages, argv_extra=(), environ=None, extract=None):
        fetch = FakeFetch(pages)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch.object(features_m2, "extract_session", side_effect=self.fake_extract):
            c = er.run([str(self.root), "--project-id", PID, *argv_extra], fetch=fetch,
                       environ={er.API_KEY_ENV: KEY} if environ is None else environ, scorer_bin=self.bin, norm_path=self.norm,
                       threshold_header_path=self.thr_header)
        return c, buf.getvalue(), fetch

    def test_e8_pc_items_are_uploaded_windows_only(self):
        pc = self.pc()
        self.assertEqual(sorted({p.session for p in pc}), self.eval_names)
        # 無効な窓（3 番目）と境目の窓は入らない。quiet / talk / neck は境目が無いので 11 窓
        per = {n: sum(1 for p in pc if p.session == n) for n in self.eval_names}
        self.assertEqual(per[session_name("20260924", 0)], N_WINDOWS - 1)
        self.assertLess(per[session_name("20260924", 1)], N_WINDOWS - 1)        # water は s@1.0 の境目がある
        self.assertTrue(all(set(p.probs) == {"swallow", "cough", "other"} for p in pc))

    def test_e9_run_match_and_report(self):
        pc = self.pc()
        pages = {"int8": [item(p.session, p.t_ms, p.probs, p.label) for p in pc],
                 "float32": [item(p.session, p.t_ms, {k: v + (0.01 if k == "swallow" else -0.005) for k, v in p.probs.items()}, p.label)
                             for p in pc]}
        c, out, fetch = self.run_(pages, ["--limit", "7"])
        self.assertTrue(c["match"])
        self.assertIn("一致", out)
        self.assertIn(f"対応づいた窓: {len(pc)}", out)
        self.assertIn("float32（EI）と int8（PC）", out)
        self.assertNotIn(KEY, out)
        self.assertNotIn(PID, out)
        self.assertIn("<project-id>", out)
        self.assertIn("deploy version 7", out)
        self.assertTrue(all(h["x-api-key"] == KEY for _, h in fetch.calls))
        # 項目ごとの確率は出ない
        for p in pc[:5]:
            self.assertNotIn(f"{p.probs['swallow']:.9g}", out)

    def test_e10_count_mismatch_stops(self):
        pc = self.pc()
        pages = {"int8": [item(p.session, p.t_ms, p.probs, p.label) for p in pc[:-1]], "float32": []}
        with self.assertRaises(SystemExit) as cm:
            self.run_(pages)
        self.assertIn("一致しません", str(cm.exception))

    def test_e11_project_id_and_key_checks(self):
        pages = {"int8": [], "float32": []}
        with self.assertRaises(SystemExit) as cm:
            self.run_(pages, environ={})
        self.assertIn(er.API_KEY_ENV, str(cm.exception))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch.object(features_m2, "extract_session", side_effect=self.fake_extract), \
             self.assertRaises(SystemExit) as cm:
            er.run([str(self.root), "--project-id", "999999"], fetch=FakeFetch(pages), environ={er.API_KEY_ENV: KEY},
                   scorer_bin=self.bin, norm_path=self.norm, threshold_header_path=self.thr_header)
        self.assertNotIn("999999", str(cm.exception))
        self.assertNotIn(PID, str(cm.exception))
        with self.assertRaises(SystemExit):                                        # 凍結した閾値が無い
            er.run([str(self.root), "--project-id", PID], fetch=FakeFetch(pages), environ={er.API_KEY_ENV: KEY},
                   scorer_bin=self.bin, norm_path=self.norm, threshold_header_path=self.tmp / "missing.h")

    def test_e12_read_threshold(self):
        self.assertEqual(er.read_threshold(self.thr_header), 0.85)


if __name__ == "__main__":
    unittest.main()
