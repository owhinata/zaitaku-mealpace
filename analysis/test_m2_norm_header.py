"""m2_norm_header.py を合成の m2_norm.json で検証する（Issue #22 第 10 節）。

実行: python -m unittest discover -s analysis -v
合成の JSON とヘッダは tempfile に作る。data/ と実データ、analysis/m2_norm.json、firmware/detector/m2_norm.h は使わない。g++ は要らない。
"""
from __future__ import annotations
import contextlib, io, json, struct, tempfile, unittest
from pathlib import Path

import numpy as np

import ei_upload, features_m2
import m2_norm_header as mh


def synth_doc(seed: int = 22, *, std_zero_at: int | None = None) -> dict:
    """乱数（seed 固定）の mean / std。std は正。"""
    rng = np.random.default_rng(seed)
    mean = rng.standard_normal(features_m2.N_FEATURES) * 3.0
    std = np.exp(rng.standard_normal(features_m2.N_FEATURES))
    if std_zero_at is not None:
        std[std_zero_at] = 0.0
    return {"feature_set": features_m2.FEATURE_SET, "feature_names": list(features_m2.FEATURE_NAMES),
            "mean": [float(v) for v in mean], "std": [float(v) for v in std],
            "stats_sessions": ["20260921-100000_self_quiet", "20260922-100000_self_water", "20260923-100000_self_talk"],
            "n_windows": 1234, "commit": "abc1234"}


def bits(v: float) -> bytes:
    return struct.pack("<d", v)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.norm = self.root / "m2_norm.json"
        self.header = self.root / "detector" / "m2_norm.h"

    def write_doc(self, doc: dict) -> None:
        self.norm.write_text(json.dumps(doc, indent=1), encoding="utf-8")


class RenderTest(Base):
    def test_h1_values_round_trip_bit_exact(self):
        doc = synth_doc()
        self.write_doc(doc)
        text = mh.run(self.norm, self.header)
        self.assertTrue(self.header.is_file())
        self.assertEqual(self.header.read_text(encoding="utf-8"), text)
        h = mh.parse_header(text)
        self.assertEqual(h["feature_set"], "m2-0020")
        self.assertEqual(h["n_features"], 29)
        self.assertEqual(h["feature_names"], list(features_m2.FEATURE_NAMES))
        for key in ("mean", "std"):
            self.assertEqual(len(h[key]), 29)
            for a, b in zip(h[key], doc[key]):
                self.assertEqual(bits(a), bits(b))
        self.assertEqual(mh.differences(doc, text), [])

    def test_h1b_format_is_17g(self):
        v = 0.1 + 0.2   # 0.30000000000000004: %.17g で往復、%.16g では落ちる
        self.assertEqual(mh.fmt_double(v), "0.30000000000000004")
        self.assertEqual(float(mh.fmt_double(v)), v)
        self.assertEqual(mh.fmt_double(1.0), "1.0")
        self.assertEqual(mh.fmt_double(-2.0), "-2.0")
        self.assertEqual(float(mh.fmt_double(1e-300)), 1e-300)

    def test_h2_header_content(self):
        doc = synth_doc()
        text = mh.render(doc)
        self.assertEqual([l for l in text.splitlines() if l.startswith("#include")], ["#include <stdint.h>"])
        self.assertNotIn("Arduino.h", text)
        self.assertIn('#define M2_FEATURE_SET "m2-0020"', text)
        self.assertIn("#define M2_N_FEATURES 29", text)
        self.assertIn("static const double M2_NORM_MEAN[M2_N_FEATURES]", text)
        self.assertIn("static const double M2_NORM_STD[M2_N_FEATURES]", text)
        self.assertIn("z[i] = (float)(((double)x[i] - M2_NORM_MEAN[i]) / M2_NORM_STD[i]);", text)
        # コメントは集計値だけ（セッション名は書かない）
        self.assertIn("stats_sessions 3 本", text)
        self.assertIn("valid な窓 1234", text)
        self.assertIn("commit abc1234", text)
        for name in doc["stats_sessions"]:
            self.assertNotIn(name, text)

    def test_h3_std_zero_stops(self):
        self.write_doc(synth_doc(std_zero_at=5))
        with self.assertRaises(SystemExit) as cm:
            mh.run(self.norm, self.header)
        self.assertIn("std に 0", str(cm.exception))
        self.assertFalse(self.header.exists())

    def test_h3b_non_finite_stops(self):
        doc = synth_doc()
        doc["mean"][3] = float("nan")
        with self.assertRaises(SystemExit):
            mh.render(doc)

    def test_h4_existing_different_stops_same_passes(self):
        doc = synth_doc()
        self.write_doc(doc)
        mh.run(self.norm, self.header)
        mh.run(self.norm, self.header)                         # 同じ内容なら通る
        self.write_doc(synth_doc(seed=23))
        with self.assertRaises(SystemExit) as cm:
            mh.run(self.norm, self.header)
        self.assertIn("内容が違います", str(cm.exception))
        self.assertEqual(mh.parse_header(self.header.read_text(encoding="utf-8"))["mean"][0], doc["mean"][0])   # 上書きされていない

    def test_h5_check(self):
        doc = synth_doc()
        self.write_doc(doc)
        with self.assertRaises(SystemExit):                    # 無い
            mh.run(self.norm, self.header, check=True)
        mh.run(self.norm, self.header)
        mh.run(self.norm, self.header, check=True)             # 一致
        text = self.header.read_text(encoding="utf-8")
        # 1 つの値の最後の桁を変える
        old = mh.fmt_double(doc["std"][7])
        new = mh.fmt_double(np.nextafter(doc["std"][7], np.inf))
        self.assertNotEqual(old, new)
        self.header.write_text(text.replace(old, new, 1), encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            mh.run(self.norm, self.header, check=True)
        self.assertIn("std がビット一致しません", str(cm.exception))
        # コメントだけの変更も差分
        self.header.write_text(text.replace("手で編集しない", "編集した"), encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            mh.run(self.norm, self.header, check=True)
        self.assertIn("本文が生成結果と違います", str(cm.exception))
        # 名前の入れ替え
        self.header.write_text(text.replace('"acc_ptp_x"', '"acc_ptp_q"', 1), encoding="utf-8")
        self.assertTrue(any("feature_names" in d for d in mh.differences(doc, self.header.read_text(encoding="utf-8"))))

    def test_h6_read_norm_checks_shape(self):
        doc = synth_doc()
        doc["feature_set"] = "0012"
        self.write_doc(doc)
        with self.assertRaises(SystemExit):
            mh.run(self.norm, self.header)

    def test_h7_selfcheck_input_matches_transform_order(self):
        """m2_scorer.SELFCHECK_INPUT を Standardizer.transform した値は float32。ヘッダの式（double → float32）と同じ順序であることは
        g++ が要るのでここでは見ない（手順は plan 第 11 節）。"""
        import m2_scorer
        doc = synth_doc()
        z = ei_upload.standardizer_from_norm(doc).transform(m2_scorer.SELFCHECK_INPUT)
        self.assertEqual(z.dtype, np.float32)
        self.assertEqual(z.shape, (29,))
        # 同じ式を numpy で double → float32 で計算した値と一致
        ref = ((m2_scorer.SELFCHECK_INPUT.astype(np.float64) - np.asarray(doc["mean"])) / np.asarray(doc["std"])).astype(np.float32)
        np.testing.assert_array_equal(z.view(np.uint32), ref.view(np.uint32))

    def test_h8_main_check_output(self):
        """main は実物の analysis/m2_norm.json と firmware/detector/m2_norm.h を見るので、ここでは引数の形だけ。"""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf), self.assertRaises(SystemExit):
            mh.main(["--bogus"])


if __name__ == "__main__":
    unittest.main()
