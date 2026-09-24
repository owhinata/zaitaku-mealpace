"""m2_scorer.py を偽の実行ファイル（一時フォルダに置いた Python スクリプト）で検証する（Issue #22 第 10 節）。

実行: python -m unittest discover -s analysis -v
偽の実行ファイルは見出しを出し、各行の 1 番目の値を swallow の確率にして LABEL_COUNT 列を返す。M2_SCORE_BIN で差し替える。
自己検査の期待値は analysis/m2_norm.json（self の集計値）か、渡された合成の JSON から作る。data/ と実データは使わず、g++ は要らない。
"""
from __future__ import annotations
import json, os, stat, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

import numpy as np

import evaluate_detector as ed
import features_m2, m2_scorer

ANALYSIS = Path(__file__).resolve().parent
SCORER_PATH = ANALYSIS / "m2_scorer.py"
FAKE_MODEL = (123456, 7)
LABELS = ("other", "swallow", "cough")          # 実行ファイルの並びは EI が決める。名前で引くことを確かめるため swallow を 2 番目に
FEATURE_NAMES = list(features_m2.FEATURE_NAMES)

FAKE_SCRIPT = """#!{python}
import sys
HEADER = {header!r}
LABELS = {labels!r}
N = {n_features}
DROP_LAST = {drop_last!r}
for h in HEADER:
    print(h)
rows = [l for l in sys.stdin.read().splitlines() if l.strip() and not l.lstrip().startswith("#")]
if DROP_LAST and rows:
    rows = rows[:-1]
for line in rows:
    v = [float(t) for t in line.split()]
    if len(v) != N:
        sys.exit("bad row")
    p = v[0]
    probs = {{"swallow": p, "cough": 0.0, "other": 1.0 - p}}
    print(" ".join("%.9g" % probs.get(l, 0.0) for l in LABELS))
"""


def selfcheck_text(norm_path: Path = m2_scorer.NORM_PATH, flip_bit: bool = False) -> str:
    z = m2_scorer.expected_selfcheck(norm_path).copy()
    if flip_bit:
        u = z.view(np.uint32)
        u[0] ^= 1
    return " ".join("%.9g" % float(v) for v in z)


def write_fake_bin(dir_: Path, *, labels=LABELS, model=FAKE_MODEL, feature_set: str = features_m2.FEATURE_SET,
                   n_features: int = 29, selfcheck: str | None = None, drop_last: bool = False,
                   norm_path: Path = m2_scorer.NORM_PATH, name: str = "score_windows") -> Path:
    header = [f"model {model[0]} {model[1]}", f"feature_set {feature_set}", f"n_features {n_features}",
              "labels " + " ".join(labels), "input_datatype int8", "quantized 1",
              "selfcheck " + (selfcheck_text(norm_path) if selfcheck is None else selfcheck)]
    p = Path(dir_) / name
    p.write_text(FAKE_SCRIPT.format(python=sys.executable, header=header, labels=list(labels), n_features=n_features,
                                    drop_last=drop_last), encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


def meta(**over) -> dict:
    m = {"subject": "self", "cond": "meal", "fw": "detector", "imu_hz": 104, "audio_hz": 16000, "window_ms": 1000, "hop_ms": 250,
         "threshold": 0.5, "model": {"source": "edge-impulse", "project_id": FAKE_MODEL[0], "deploy_version": FAKE_MODEL[1]},
         "feature_set": features_m2.FEATURE_SET, "feature_names": list(FEATURE_NAMES)}
    m.update(over)
    return m


def rows(values0, n_features: int = 29) -> list[list[float]]:
    return [[float(v)] + [1.5] * (n_features - 1) for v in values0]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        m2_scorer._SCORER = None
        self.addCleanup(setattr, m2_scorer, "_SCORER", None)

    def use_bin(self, path: Path):
        patcher = mock.patch.dict(os.environ, {m2_scorer.SCORE_BIN_ENV: str(path)})
        patcher.start()
        self.addCleanup(patcher.stop)


class ScorerTest(Base):
    def test_s1_header_and_score_rows(self):
        s = m2_scorer.Scorer(write_fake_bin(self.root))
        self.assertEqual(s.model, FAKE_MODEL)
        self.assertEqual(s.deploy_version, 7)
        self.assertEqual(s.labels, LABELS)
        self.assertEqual(s.swallow_index, 1)
        self.assertEqual((s.n_features, s.feature_set, s.input_datatype, s.quantized), (29, "m2-0020", "int8", 1))
        p = s.score_rows(rows([0.1, 0.9, 0.5]))
        self.assertEqual(p.shape, (3, 3))
        np.testing.assert_allclose(p[:, 1], [0.1, 0.9, 0.5])
        np.testing.assert_allclose(p[:, 0], [0.9, 0.1, 0.5])
        self.assertEqual(s.score_rows([]).shape, (0, 3))

    def test_s1b_values_are_sent_as_float32_9g(self):
        text = m2_scorer.format_rows([[np.float32(0.1)] * 29])
        self.assertEqual(text.split()[0], "0.100000001")
        with self.assertRaises(SystemExit):
            m2_scorer.format_rows([[0.1] * 28])

    def test_s2_score_entry_returns_swallow_column(self):
        self.use_bin(write_fake_bin(self.root))
        out = m2_scorer.score(rows([0.2, 0.8]), meta())
        self.assertEqual(out, [0.2, 0.8])
        self.assertIsInstance(out[0], float)
        # labels の並びが違っても名前で引く
        m2_scorer._SCORER = None
        self.use_bin(write_fake_bin(self.root, labels=("swallow", "other", "cough"), name="b2"))
        self.assertEqual(m2_scorer.score(rows([0.3]), meta()), [0.3])

    def test_s3_meta_mismatch_stops(self):
        self.use_bin(write_fake_bin(self.root))
        cases = {
            "feature_set": meta(feature_set="0020"),
            "feature_names": meta(feature_names=FEATURE_NAMES[:28] + ["x"]),
            "feature_names の長さ": meta(feature_names=FEATURE_NAMES[:28]),
            "model": meta(model={"source": "edge-impulse", "project_id": FAKE_MODEL[0], "deploy_version": 8}),
        }
        for label, m in cases.items():
            with self.subTest(label), self.assertRaises(SystemExit):
                m2_scorer.score(rows([0.5]), m)
        with self.assertRaises(SystemExit):               # 次元
            m2_scorer.score(rows([0.5], n_features=28), meta())
        # model が無い meta は通る（照合しない）
        m = meta()
        del m["model"]
        self.assertEqual(m2_scorer.score(rows([0.5]), m), [0.5])

    def test_s3b_model_mismatch_message_has_no_id(self):
        self.use_bin(write_fake_bin(self.root))
        with self.assertRaises(SystemExit) as cm:
            m2_scorer.score(rows([0.5]), meta(model={"project_id": 999999, "deploy_version": 1}))
        self.assertNotIn("123456", str(cm.exception))
        self.assertNotIn("999999", str(cm.exception))

    def test_s4_row_count_mismatch_stops(self):
        s = m2_scorer.Scorer(write_fake_bin(self.root, drop_last=True))
        with self.assertRaises(SystemExit) as cm:
            s.score_rows(rows([0.1, 0.2]))
        self.assertIn("行数", str(cm.exception))

    def test_s5_missing_bin_stops_with_build_command(self):
        with self.assertRaises(SystemExit) as cm:
            m2_scorer.Scorer(self.root / "nope")
        self.assertIn("build.sh", str(cm.exception))
        self.use_bin(self.root / "nope2")
        with self.assertRaises(SystemExit):
            m2_scorer.score(rows([0.5]), meta())

    def test_s6_selfcheck_one_bit_off_stops(self):
        with self.assertRaises(SystemExit) as cm:
            m2_scorer.Scorer(write_fake_bin(self.root, selfcheck=selfcheck_text(flip_bit=True)))
        self.assertIn("selfcheck", str(cm.exception))
        with self.assertRaises(SystemExit):
            m2_scorer.Scorer(write_fake_bin(self.root, selfcheck="0 1 2", name="short"))

    def test_s6b_selfcheck_against_synthetic_norm(self):
        """渡した m2_norm.json で期待値を作る（m2_threshold のテストと同じ使い方）。"""
        doc = json.loads(m2_scorer.NORM_PATH.read_text(encoding="utf-8"))
        doc["mean"] = [v + 1.0 for v in doc["mean"]]
        norm = self.root / "m2_norm.json"
        norm.write_text(json.dumps(doc), encoding="utf-8")
        s = m2_scorer.Scorer(write_fake_bin(self.root, norm_path=norm), norm_path=norm)
        self.assertEqual(s.labels, LABELS)
        with self.assertRaises(SystemExit):
            m2_scorer.Scorer(write_fake_bin(self.root, name="b2"), norm_path=norm)   # 実物の JSON の期待値とは食い違う

    def test_s7_bad_header_stops(self):
        for label, kw in {"feature_set": dict(feature_set="0012"), "n_features": dict(n_features=28),
                          "labels": dict(labels=("other", "cough", "noise"))}.items():
            with self.subTest(label), self.assertRaises(SystemExit):
                m2_scorer.Scorer(write_fake_bin(self.root, name=label, **kw))

    def test_s8_nonzero_exit_stops(self):
        p = self.root / "crash"
        p.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(9)\n", encoding="utf-8")
        p.chmod(0o755)
        with self.assertRaises(SystemExit) as cm:
            m2_scorer.Scorer(p)
        self.assertIn("9", str(cm.exception))


def make_detector_session(root: Path, name: str, f0: dict, positives, events=(), n_windows: int = 40) -> Path:
    """検出器のセッション（detect.csv・feat.csv・events.csv・meta.json）。f0 は窓の開始（秒）→ 1 番目の特徴量。29 次元。"""
    d = root / name
    d.mkdir()
    w = [1000 + 250 * i for i in range(n_windows)]
    pos = {round(p * 1000) for p in positives}
    lines = ["t_ms,window_t_ms,positive,prob,led"]
    flines = [",".join(["t_ms", "window_t_ms"] + [f"f{i}" for i in range(29)])]
    for wi in w:
        p = 1 if wi in pos else 0
        v0 = f0.get(wi / 1000.0, 0.05)
        lines.append(f"{wi + 1030},{wi},{p},{v0:.9g},{1 + p}")
        flines.append(",".join([str(wi + 1032), str(wi), f"{v0:.9g}"] + ["1.5"] * 28))
    (d / "detect.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (d / "feat.csv").write_text("\n".join(flines) + "\n", encoding="utf-8")
    ev = ["t_ms,label,note"] + [f"{t_ms},{label}," for t_ms, label in events]
    (d / "events.csv").write_text("\n".join(ev) + "\n", encoding="utf-8")
    (d / "meta.json").write_text(json.dumps(meta()), encoding="utf-8")
    return d


class EvaluateDetectorTest(Base):
    def test_s9_evaluate_detector_run_with_m2_scorer(self):
        self.use_bin(write_fake_bin(self.root))
        data = self.root / "raw"
        data.mkdir()
        f0 = {5.0: 0.95, 8.0: 0.95}
        for i in range(3):
            make_detector_session(data, f"20261005-12{i:02d}00_self_meal", f0, positives=[5.0, 8.0], events=[(5000, "s")])
        r = ed.run(data, scorer=SCORER_PATH)
        self.assertIsNotNone(r.rescored)
        for s in r.rescored.values():
            self.assertEqual((s["windows"], s["dev1_pc0"], s["dev0_pc1"], s["max_abs_diff"]), (40, 0, 0, 0.0))
        self.assertEqual(r.total["swallows"], 3)
        # PC で 1 窓だけ陰性にする（装置 1 / PC 0 = 1）
        f0[8.0] = 0.2
        make_detector_session(data, "20261005-121000_self_meal", f0, positives=[5.0, 8.0], events=[(5000, "s")])
        r = ed.run(data, sessions=["20261005-121000_self_meal"], scorer=SCORER_PATH)
        s = r.rescored["20261005-121000_self_meal"]
        self.assertEqual((s["dev1_pc0"], s["dev0_pc1"]), (1, 0))
        text = ed.format_report(r)
        self.assertIn("再採点", text)
        self.assertNotIn("0.95", text)


if __name__ == "__main__":
    unittest.main()
