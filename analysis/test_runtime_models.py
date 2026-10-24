"""analysis/ の乱数の重みの CNN（ONNX・TFLite・ncnn。Issue #51）のテスト。

.venv/bin/python -m unittest analysis/test_runtime_models.py -v
（.venv に requirements-runtimes.txt を入れたとき。無いランタイムの形式は飛ばす）

各形式を CPU（fp32）で走らせたロジットに numpy で sigmoid を掛けた値が、
features_np.cnn_forward（同じ乱数の重み）と 1e-5 以内であること、演算子が標準の
ものだけであること、大きさを確かめる。torch は使わない。GPU（fp16）の経路は
ここでは測らない（UNO Q の bench_runtimes.py の照合の行で記録する）。
"""

from __future__ import annotations
import importlib.util
import unittest

import numpy as np

import make_runtime_models as mrm

HERE = mrm.HERE
ALL_FILES = [n for f in mrm.FORMATS for n in mrm.OUTPUTS[f]]
MAX_TOTAL_BYTES = 200 * 1024


def has(*mods) -> bool:
    return all(importlib.util.find_spec(m) is not None for m in mods)


def why(*mods) -> str:
    return "未導入: " + "・".join(m for m in mods if not has(m))


class ModelCase(unittest.TestCase):
    FMT = ""

    def check_outputs(self):
        named = mrm.check_inputs()
        xs = [x for _, x in named]
        got = mrm.sigmoid(mrm.run(self.FMT, HERE, xs))
        want = mrm.expected(xs)
        self.assertEqual(got.shape, want.shape)
        for (name, _), g, w in zip(named, got, want):
            with self.subTest(input=name):
                self.assertLessEqual(abs(float(g) - float(w)), mrm.TOL)


class TestFiles(unittest.TestCase):
    def test_four_files_and_size(self):
        """4 つの生成物が analysis/ の直下にあり、合計 200 KB 以下。"""
        self.assertEqual(len(ALL_FILES), 4)
        total = 0
        for n in ALL_FILES:
            with self.subTest(name=n):
                p = HERE / n
                self.assertTrue(p.is_file() and not p.is_symlink(), n)
                total += p.stat().st_size
        self.assertLessEqual(total, MAX_TOTAL_BYTES)

    def test_inputs_are_fixed_shape_float32(self):
        for name, x in mrm.check_inputs():
            with self.subTest(input=name):
                self.assertEqual(x.shape, mrm.INPUT_SHAPE)
                self.assertEqual(x.dtype, np.float32)


ONNX_OPS = {
    "Conv",
    "Relu",
    "MaxPool",
    "ReduceMean",
    "GlobalAveragePool",
    "Gemm",
    "MatMul",
    "Add",
    # 形を変えるだけのもの（Reshape 系）
    "Reshape",
    "Unsqueeze",
    "Squeeze",
    "Flatten",
    "Transpose",
    "Slice",
    "Gather",
    "Constant",
    "Identity",
}


@unittest.skipUnless(has("onnx", "onnxruntime"), why("onnx", "onnxruntime"))
class TestOnnx(ModelCase):
    FMT = "onnx"

    def test_outputs_match_cnn_forward(self):
        self.check_outputs()

    def test_version_and_ops(self):
        import onnx

        proto = onnx.load(str(HERE / mrm.OUTPUTS["onnx"][0]))
        onnx.checker.check_model(proto, full_check=True)
        self.assertLessEqual(proto.ir_version, mrm.ONNX_IR_MAX)
        opsets = {o.domain: o.version for o in proto.opset_import}
        self.assertEqual(opsets.get("", opsets.get("ai.onnx")), mrm.ONNX_OPSET)
        self.assertGreater(len(proto.graph.initializer), 0)
        for n in proto.graph.node:
            with self.subTest(op=n.op_type):
                self.assertIn(n.domain, ("", "ai.onnx"))
                self.assertIn(n.op_type, ONNX_OPS)
        ops = {n.op_type for n in proto.graph.node}
        self.assertIn("Conv", ops)


NCNN_LAYERS = {
    "Input",
    "Convolution",
    "ReLU",
    "Pooling",
    "Reduction",
    "InnerProduct",
    "Gemm",
    "BinaryOp",
    "MemoryData",
    # 形を変えるだけのもの
    "ExpandDims",
    "Squeeze",
    "Reshape",
    "Flatten",
    "Crop",
    "Slice",
    "Split",
}


def ncnn_layers(text: str) -> list:
    """param の本文 → [(層の種類, 名前)]。1 行目は 7767517、2 行目は数。"""
    lines = [ln.split() for ln in text.splitlines() if ln.strip()]
    return [(t[0], t[1]) for t in lines[2:]]


@unittest.skipUnless(has("ncnn"), why("ncnn"))
class TestNcnn(ModelCase):
    FMT = "ncnn"

    def test_outputs_match_cnn_forward(self):
        self.check_outputs()

    def test_layers(self):
        text = (HERE / mrm.OUTPUTS["ncnn"][0]).read_text(encoding="utf-8")
        self.assertEqual(text.splitlines()[0].strip(), "7767517")
        layers = ncnn_layers(text)
        for kind, name in layers:
            with self.subTest(layer=name):
                self.assertIn(kind, NCNN_LAYERS)
        kinds = [k for k, _ in layers]
        self.assertEqual(kinds.count("Convolution"), 3)
        self.assertIn(mrm.NCNN_IN, text.split())
        self.assertIn(mrm.NCNN_OUT, text.split())


TFLITE_OPS = {
    "CONV_2D",
    "DEPTHWISE_CONV_2D",
    "MAX_POOL_2D",
    "MEAN",
    "FULLY_CONNECTED",
    "BATCH_MATMUL",
    "ADD",
    "RELU",
    # 形を変えるだけのもの
    "RESHAPE",
    "EXPAND_DIMS",
    "SQUEEZE",
    "TRANSPOSE",
    "STRIDED_SLICE",
    "SLICE",
    "GATHER",
}


@unittest.skipUnless(has("ai_edge_litert"), why("ai_edge_litert"))
class TestTflite(ModelCase):
    FMT = "tflite"

    def test_outputs_match_cnn_forward(self):
        self.check_outputs()

    def test_ops(self):
        from ai_edge_litert.interpreter import Interpreter, OpResolverType

        # 既定の XNNPACK は allocate_tensors で演算子を DELEGATE に置き換える
        # ので、置き換えない設定でモデルの演算子を見る
        it = Interpreter(
            model_path=str(HERE / mrm.OUTPUTS["tflite"][0]),
            experimental_op_resolver_type=(
                OpResolverType.BUILTIN_WITHOUT_DEFAULT_DELEGATES
            ),
        )
        it.allocate_tensors()
        ops = [d["op_name"] for d in it._get_ops_details()]
        for op in ops:
            with self.subTest(op=op):
                self.assertIn(op, TFLITE_OPS)
        # 1 層目（入力 1 チャンネル）は DEPTHWISE_CONV_2D になる
        self.assertEqual(
            ops.count("CONV_2D") + ops.count("DEPTHWISE_CONV_2D"), 3
        )
        (inp,) = it.get_input_details()
        self.assertEqual(tuple(inp["shape"]), mrm.INPUT_SHAPE)
        self.assertEqual(inp["dtype"], np.float32)


if __name__ == "__main__":
    unittest.main()
