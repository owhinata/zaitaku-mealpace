"""m4_infer.py（装置と PC で共有する M4 の推論、Issue #47）を合成データで検証する。

実行: python -m unittest discover -s analysis -v
重みは analysis/models/m4-0038.npz（読むだけ）と、同じ形の合成の重み。data/ は
使わない。言えるのは「1 窓ずつの経路がバッチの経路（model_prob・post_mask・
led_states）と一致する」まで。性能の数字は出さない。
"""

from __future__ import annotations
import ast, hashlib, sys, tempfile, unittest
from pathlib import Path

import numpy as np

import features_np as fnp
import led_rule, m4_compare, m4_infer, postprocess

HERE = Path(__file__).resolve().parent
M4_MODEL_PATH = HERE / "models" / "m4-0038.npz"
M4_SHA256 = "c786086f069c55c55b27eea860963c3c950c04b728fe65d52bc35a7a4a806d90"
CFG = (
    "set=mfe40 classifier=cnn seeds=0,1,2 hard_negative=none post=hyst"
    " threshold=- enter=0.85 exit=0.65 cap_s=none refractory_s=0.5"
)


def synthetic_model(config: str = CFG, seed: int = 0) -> dict:
    """load_model と同じ形の合成の重み（学習済みではない）。"""
    cfg = m4_infer.parse_config(config)
    rng = np.random.default_rng(seed)
    a = {"config": np.array(config), "format": np.array(1)}
    for s in cfg["seeds"]:
        p = fnp.random_cnn(rng)
        for k in m4_infer.CNN_SHAPES:
            a[f"s{s}/{k}"] = p[k]
        a[f"s{s}/fc.bias"] = np.array([0.5], np.float32)
        a[f"s{s}/fc.weight"] = (p["fc.weight"] * 30).astype(np.float32)
        a[f"s{s}/mean"] = rng.normal(-8, 1, 40)
        a[f"s{s}/std"] = 1 + rng.random(40)
    m4_infer._check_arrays(a)
    return {"config": config, "cfg": cfg, "arrays": a}


def windows(n: int, seed: int) -> np.ndarray:
    """16 kHz の n 窓（float32、/ 32768 の後の目盛り）。振幅の大きい区間を含む。"""
    rng = np.random.default_rng(seed)
    t = np.arange(16000) / 16000
    out = []
    for i in range(n):
        amp = 0.3 if (i // 5) % 3 == 1 else 0.003
        x = amp * np.sin(2 * np.pi * (200 + 37 * i) * t) + rng.normal(
            0, amp / 3, 16000
        )
        out.append(x.astype(np.float32))
    return np.stack(out)


class ReexportTest(unittest.TestCase):
    NAMES = (
        "CONFIG_KEYS",
        "CNN_SHAPES",
        "N_MEL",
        "N_LR",
        "MODEL_FORMAT",
        "CNN_SET",
        "LR_SET",
        "HN_WEIGHT",
        "parse_config",
        "config_mask",
        "_expected_keys",
        "_check_arrays",
        "load_model",
        "model_prob",
    )

    def test_m4_compare_reexports_the_same_objects(self):
        for n in self.NAMES:
            with self.subTest(n):
                self.assertIs(getattr(m4_compare, n), getattr(m4_infer, n))

    def test_load_model_m4_0038(self):
        m = m4_infer.load_model(M4_MODEL_PATH)
        self.assertEqual(m["config"], CFG)
        self.assertEqual(m["cfg"]["seeds"], (0, 1, 2))
        self.assertEqual(m4_infer.sha256_file(M4_MODEL_PATH), M4_SHA256)
        self.assertEqual(
            m4_infer.sha256_file(M4_MODEL_PATH),
            hashlib.sha256(M4_MODEL_PATH.read_bytes()).hexdigest(),
        )

    def test_imports_and_writes(self):
        """numpy・features_np・postprocess・led_rule と標準ライブラリだけ。
        print・書き込みの open・save が無い。"""
        tree = ast.parse(Path(m4_infer.__file__).read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
            elif isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name):
                    self.assertNotEqual(f.id, "print")
                    if f.id == "open":
                        mode = node.args[1].value if len(node.args) > 1 else "r"
                        self.assertNotRegex(mode, "[wax+]")
                if isinstance(f, ast.Attribute):
                    self.assertNotIn(
                        f.attr,
                        {"save", "savez", "savez_compressed", "tofile"},
                    )
        others = names - {
            "numpy",
            "features_np",
            "postprocess",
            "led_rule",
            "__future__",
        }
        self.assertTrue(others <= set(sys.stdlib_module_names), others)


class CodeShaTest(unittest.TestCase):
    def test_code_sha256(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "b.py").write_text("x = 1\n", encoding="utf-8")
            (d / "a.npy").write_bytes(b"abc")
            (d / "c.onnx").write_bytes(b"skip")
            want = hashlib.sha256(
                (
                    f"a.npy {hashlib.sha256(b'abc').hexdigest()}\n"
                    f"b.py {hashlib.sha256(b'x = 1' + chr(10).encode()).hexdigest()}\n"
                ).encode()
            ).hexdigest()
            got = m4_infer.code_sha256(sorted(d.iterdir()))
            self.assertEqual(got, want)
            # 順番に依らない・.onnx は入らない
            self.assertEqual(
                m4_infer.code_sha256(sorted(d.iterdir(), reverse=True)), want
            )
            (d / "b.py").write_text("x = 2\n", encoding="utf-8")
            self.assertNotEqual(m4_infer.code_sha256(d.iterdir()), want)
            with self.assertRaises(ValueError):
                m4_infer.code_sha256([d / "b.py", d / "b.py"])


class DetectorTest(unittest.TestCase):
    def run_stepwise(self, model, X, feat=False, w0=1000):
        det = m4_infer.Detector(model, feat=feat)
        out = [det.step(x, w0 + 250 * k) for k, x in enumerate(X)]
        return out

    def batch(self, model, X, w0=1000, per_window=False):
        M = np.stack([fnp.mfe40(x) for x in X])
        if per_window:
            prob = np.array([m4_infer.model_prob(model, m[None])[0] for m in M])
        else:
            prob = m4_infer.model_prob(model, M)
        t = 0.25 * np.arange(len(X))
        cfg = model["cfg"]
        pos = postprocess.post_mask(
            prob,
            np.ones(len(X), bool),
            t,
            cfg["enter"],
            cfg["exit"],
            cfg["cap_s"],
            cfg["refractory_s"],
        )
        led = led_rule.led_states(
            [w0 + 250 * k for k in range(len(X))], pos.astype(int)
        )
        return prob, pos, led

    def check(self, model, X, feat=False):
        """model_prob を窓 1 つずつ呼んだ値と 1e-12 以内（同じ式）、まとめて
        呼んだ値（バッチ）と 1e-6 以内。float32 の行列の積は B によって和の
        順が変わり、バッチとの差は 0 にならない（einsum の版でも同じ。
        1e-8 程度）。positive・led はどちらとも完全に一致。"""
        got = self.run_stepwise(model, X, feat=feat)
        p = np.array([g[0] for g in got])
        for per_window, tol in ((True, 1e-12), (False, 1e-6)):
            prob, pos, led = self.batch(model, X, per_window=per_window)
            np.testing.assert_allclose(p, prob, rtol=0, atol=tol)
            self.assertEqual([g[1] for g in got], [int(v) for v in pos])
            self.assertEqual([g[2] for g in got], led)
        for (_, _, _, f), x in zip(got, X):
            if feat:
                np.testing.assert_array_equal(f, fnp.mfe_and_feat(x)[1])
            else:
                self.assertIsNone(f)
        return p, pos, led

    def test_m4_0038_matches_batch(self):
        model = m4_infer.load_model(M4_MODEL_PATH)
        self.check(model, windows(40, 1))
        self.check(model, windows(12, 2), feat=True)

    def test_synthetic_weights_go_positive_and_back(self):
        """合成の重みで陽性・陰性の両方と緑（2）を通る（経路の確かめ）。"""
        model = synthetic_model()
        X = windows(60, 3)
        p, pos, led = self.check(model, X)
        self.assertTrue(pos.any() and not pos.all(), p.round(2))
        self.assertIn(2, led)
        self.assertIn(1, led)

    def test_random_prob_sequences_through_post_stepper(self):
        """乱数の確率列で、Detector と同じ後処理の進め方が post_mask と一致
        （不応期で塊を抑える経路を含む）。"""
        cfg = m4_infer.parse_config(CFG)
        rng = np.random.default_rng(5)
        suppressed = False
        for trial in range(30):
            prob = rng.random(80) ** (0.3 + trial % 3)
            t = 0.25 * np.arange(80)
            want = postprocess.post_mask(
                prob, np.ones(80, bool), t, 0.85, 0.65, None, 0.5
            )
            st = postprocess.PostStepper(
                cfg["enter"], cfg["exit"], cfg["cap_s"], cfg["refractory_s"]
            )
            got = [st.step(v, True, 0.25 * k) for k, v in enumerate(prob)]
            self.assertEqual(got, list(want))
            hyst = postprocess.hysteresis(
                prob, np.ones(80, bool), 0.85, 0.65, t
            )
            suppressed |= bool((hyst & ~want).any())
        self.assertTrue(suppressed)

    def test_rejects_non_cnn_config(self):
        lr = m4_infer.parse_config(
            "set=a52 classifier=lr seeds=- hard_negative=none post=none"
            " threshold=0.50 enter=- exit=- cap_s=none refractory_s=none"
        )
        with self.assertRaises(ValueError):
            m4_infer.Detector({"cfg": lr, "arrays": {}})

    def test_window_order_is_checked(self):
        det = m4_infer.Detector(synthetic_model())
        X = windows(2, 4)
        det.step(X[0], 1000)
        with self.assertRaises(ValueError):
            det.step(X[1], 1000)


if __name__ == "__main__":
    unittest.main()
