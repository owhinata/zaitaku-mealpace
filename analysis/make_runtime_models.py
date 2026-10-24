"""乱数の重みの CNN を ONNX・TFLite・ncnn に書き出す（Issue #51。PC だけ。装置には
配らない。torch・onnx・pnnx・litert-torch を使う）。

重みは features_np.random_cnn(np.random.default_rng(0))（bench_infer の (c) と
同じ重み。学習済みではない）。torch のモジュールは compare._make_net（M3 の学習と
同じ定義）にこの重みを載せたもの。入力は (1, 98, 40) float32、出力はロジット (1,)
（sigmoid は装置で numpy で掛ける）。

作るもの（analysis/ の直下。名前の rand0 は「乱数の種 0 の重み」の意。どれも音声・
学習の成果・特徴量ではない）:
  cnn_rand0.onnx          torch.onnx.export（dynamo、opset 17、外部データなし）。
                          ir_version を UNO Q の ONNX Runtime が読める値に下げる。
  cnn_rand0.ncnn.param    pnnx が一時フォルダに書いた ncnn の 2 つ。重みの名前は
  cnn_rand0.ncnn.weights  .gitignore の *.bin を避けて .ncnn.weights にする。
  cnn_rand0.tflite        litert_torch.convert（float32、量子化しない）。
                          変換用の venv（requirements-convert-litert.txt）で。

使い方:
  .venv/bin/python analysis/make_runtime_models.py --only onnx,ncnn [--write]
  ~/.venvs/zaitaku-mealpace-litert-convert/bin/python \\
      analysis/make_runtime_models.py --only tflite [--write]
既定は作り直した物の出力が、置いてある物の出力と同じかを比べるだけ（違えば・無ければ
1 で止まる）。変換は版によってバイト列が変わるので、比べるのは出力。--write のとき
だけ上の固定のパスに書く。
"""

from __future__ import annotations
import argparse, shutil, sys, tempfile
from pathlib import Path

import numpy as np

import features_np as fnp

HERE = Path(__file__).resolve().parent
SEED = 0
INPUT_SHAPE = (1, fnp.N_FRAMES, fnp.N_MEL_CNN)
ONNX_OPSET = 17
ONNX_IR_MAX = 10  # UNO Q の Debian の ONNX Runtime 1.21 も読める上限
NCNN_IN, NCNN_OUT = "in0", "out0"  # pnnx が付ける名前
TOL = 1e-5  # 確率の差（cnn_forward・置いてある物との比較）
FORMATS = ("onnx", "ncnn", "tflite")
OUTPUTS = {
    "onnx": ("cnn_rand0.onnx",),
    "ncnn": ("cnn_rand0.ncnn.param", "cnn_rand0.ncnn.weights"),
    "tflite": ("cnn_rand0.tflite",),
}


def weights() -> dict:
    """書き出す重み（bench_infer.Model の (c) と同じ）。"""
    return fnp.random_cnn(np.random.default_rng(SEED))


def check_inputs() -> list:
    """比べるための (名前, (1, 98, 40) float32) の一覧。合成だけ。"""
    _, M = fnp.reference_cnn()
    rng = np.random.default_rng(1)
    out = [("reference_cnn の M", M)]
    for k in range(3):
        x = rng.standard_normal(INPUT_SHAPE).astype(np.float32)
        out.append((f"乱数 {k + 1}", x))
    out.append(("0", np.zeros(INPUT_SHAPE, np.float32)))
    i = np.arange(fnp.N_FRAMES)[:, None]
    m = np.arange(fnp.N_MEL_CNN)[None, :]
    big = np.where((i + m) % 2 == 0, 10.0, -10.0)[None].astype(np.float32)
    out.append(("±10", big))
    return out


def sigmoid(z) -> np.ndarray:
    z = np.asarray(z, dtype=np.float64).reshape(-1)
    return 1.0 / (1.0 + np.exp(-z))


# --- 書き出し（torch） ---


def make_net():
    """(torch, 重みを載せた eval のモジュール)。"""
    import torch

    import compare

    net = compare._make_net(torch)
    state = {k: torch.from_numpy(v.copy()) for k, v in weights().items()}
    net.load_state_dict(state, strict=True)
    net.eval()
    return torch, net


def build_onnx(torch, net, x, out_dir: Path) -> None:
    import onnx

    prog = torch.onnx.export(
        net,
        (x,),
        dynamo=True,
        external_data=False,
        opset_version=ONNX_OPSET,
    )
    proto = prog.model_proto
    _opset18_to_17(onnx, proto)
    proto.ir_version = min(proto.ir_version, ONNX_IR_MAX)
    onnx.checker.check_model(proto, full_check=True)
    (out_dir / OUTPUTS["onnx"][0]).write_bytes(proto.SerializeToString())


# opset 17 と 18 で定義が同じ演算子（この CNN に出るもの）
_SAME_17_18 = {"Unsqueeze", "Conv", "Relu", "MaxPool", "Gemm", "Gather"}


def _opset18_to_17(onnx, proto) -> None:
    """dynamo の書き出しは opset 18 で作り、17 への自動の変換は ReduceMean の
    axes（18 で入力、17 で属性）で失敗する。この CNN に出る演算子のうち 17 と
    18 で違うのは ReduceMean だけなので、axes を属性に戻して opset を 17 にする。
    知らない演算子があれば止める。"""
    (imp,) = [o for o in proto.opset_import if o.domain in ("", "ai.onnx")]
    if imp.version == ONNX_OPSET:
        return
    if imp.version != 18:
        raise SystemExit(f"opset {imp.version} は想定していない")
    g = proto.graph
    inits = {t.name: t for t in g.initializer}
    used = set()
    for n in g.node:
        if n.op_type in _SAME_17_18:
            continue
        if n.op_type != "ReduceMean" or len(n.input) != 2:
            raise SystemExit(f"opset 17 に戻せない演算子: {n.op_type}")
        axes = onnx.numpy_helper.to_array(inits[n.input[1]])
        noop = [a for a in n.attribute if a.name == "noop_with_empty_axes"]
        if noop and noop[0].i != 0:
            raise SystemExit("noop_with_empty_axes が 0 でない")
        keep = [a for a in n.attribute if a.name != "noop_with_empty_axes"]
        used.add(n.input[1])
        del n.attribute[:]
        n.attribute.extend(keep)
        n.attribute.append(
            onnx.helper.make_attribute("axes", [int(a) for a in axes])
        )
        del n.input[1]
    still = {i for n in g.node for i in n.input}
    for name in used - still:
        g.initializer.remove(inits[name])
    imp.version = ONNX_OPSET


def _drop_scalar_reshape(text: str) -> str:
    """pnnx の param の最後の Reshape（0 次元への形の変更。6="0n"）を外し、
    その入力の blob を NCNN_OUT にする。ncnn はこの層を実行できず extract が
    失敗する（1 要素の出力はその前の Slice で出ている）。"""
    lines = text.splitlines()
    t = lines[-1].split()
    if t[0] != "Reshape" or t[-1] != '6="0n"' or t[5] != NCNN_OUT:
        raise SystemExit(f"想定と違う最後の層: {lines[-1]}")
    src = t[4]
    # 層の行: 種類 名前 入力の数 出力の数 入力の blob… 出力の blob… 引数…
    prev = lines[-2].split()
    nin, nout = int(prev[2]), int(prev[3])
    outs = range(4 + nin, 4 + nin + nout)
    hit = [i for i in outs if prev[i] == src]
    if len(hit) != 1:
        raise SystemExit("最後の層の入力が前の層の出力でない")
    prev[hit[0]] = NCNN_OUT
    lines[-2] = " ".join(prev)
    n_layer, n_blob = (int(v) for v in lines[1].split())
    lines[1] = f"{n_layer - 1} {n_blob - 1}"
    return "\n".join(lines[:-1]) + "\n"


def build_ncnn(torch, net, x, out_dir: Path) -> None:
    import pnnx

    with tempfile.TemporaryDirectory() as tmp:
        pt = Path(tmp) / "cnn_rand0.pt"
        # fp16=False: 既定は ncnn の重みを fp16 で書く（fp32 で比べるため）
        pnnx.export(net, str(pt), (x,), fp16=False)
        param, bin_ = OUTPUTS["ncnn"]
        text = (Path(tmp) / "cnn_rand0.ncnn.param").read_text(encoding="utf-8")
        (out_dir / param).write_text(
            _drop_scalar_reshape(text), encoding="utf-8"
        )
        shutil.copyfile(Path(tmp) / "cnn_rand0.ncnn.bin", out_dir / bin_)


def build_tflite(torch, net, x, out_dir: Path) -> None:
    import litert_torch

    edge = litert_torch.convert(net, (x,))
    edge.export(str(out_dir / OUTPUTS["tflite"][0]))


BUILDERS = {"onnx": build_onnx, "ncnn": build_ncnn, "tflite": build_tflite}


# --- 実行（CPU、fp32。テストからも使う。torch は使わない） ---


def run_onnx(path: Path, xs) -> np.ndarray:
    import onnxruntime as ort

    sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    name = sess.get_inputs()[0].name
    return np.concatenate(
        [sess.run(None, {name: x})[0].reshape(-1) for x in xs]
    )


def run_tflite(path: Path, xs) -> np.ndarray:
    from ai_edge_litert.interpreter import Interpreter

    it = Interpreter(model_path=str(path))
    it.allocate_tensors()
    i = it.get_input_details()[0]["index"]
    o = it.get_output_details()[0]["index"]
    out = []
    for x in xs:
        it.set_tensor(i, x)
        it.invoke()
        out.append(np.array(it.get_tensor(o)).reshape(-1))
    return np.concatenate(out)


def ncnn_cpu_net(param: Path, weights_path: Path, threads: int = 0):
    """fp32 の CPU の ncnn.Net（Vulkan・fp16・bf16 を切る）。"""
    import ncnn

    net = ncnn.Net()
    net.opt.use_vulkan_compute = False
    net.opt.use_fp16_packed = False
    net.opt.use_fp16_storage = False
    net.opt.use_fp16_arithmetic = False
    net.opt.use_bf16_storage = False
    if threads:
        net.opt.num_threads = threads
    net.load_param(str(param))
    net.load_model(str(weights_path))
    return net


def ncnn_extract(net, x) -> np.ndarray:
    import ncnn

    ex = net.create_extractor()
    ex.input(NCNN_IN, ncnn.Mat(np.ascontiguousarray(x[0])))
    _, out = ex.extract(NCNN_OUT)
    return np.array(out).reshape(-1)


def run_ncnn(param: Path, weights_path: Path, xs) -> np.ndarray:
    net = ncnn_cpu_net(param, weights_path)
    try:
        return np.concatenate([ncnn_extract(net, x) for x in xs])
    finally:
        net.clear()


def run(fmt: str, d: Path, xs) -> np.ndarray:
    """d の中の fmt の物を CPU で走らせたロジット。"""
    names = [d / n for n in OUTPUTS[fmt]]
    if fmt == "onnx":
        return run_onnx(names[0], xs)
    if fmt == "tflite":
        return run_tflite(names[0], xs)
    return run_ncnn(names[0], names[1], xs)


def expected(xs) -> np.ndarray:
    w = weights()
    return np.concatenate([fnp.cnn_forward(w, x) for x in xs])


# --- まとめ ---


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--write", action="store_true", help="固定のパスに書く")
    ap.add_argument(
        "--only",
        default=",".join(FORMATS),
        help=f"作る形式（カンマ区切り。{','.join(FORMATS)}）",
    )
    a = ap.parse_args(argv)
    fmts = [f for f in a.only.split(",") if f]
    bad = [f for f in fmts if f not in FORMATS]
    if bad or not fmts:
        ap.error(f"--only は {','.join(FORMATS)} から: {bad}")
    torch, net = make_net()
    x = torch.from_numpy(np.zeros(INPUT_SHAPE, np.float32))
    xs = [v for _, v in check_inputs()]
    want = expected(xs)
    rc = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for fmt in fmts:
            BUILDERS[fmt](torch, net, x, tmp)
            new = sigmoid(run(fmt, tmp, xs))
            d_ref = float(np.abs(new - want).max())
            names = "・".join(OUTPUTS[fmt])
            if not d_ref <= TOL:
                print(f"NG: {names} が cnn_forward と違う（差 {d_ref:.1e}）")
                rc = 1
                continue
            if a.write:
                for n in OUTPUTS[fmt]:
                    shutil.copyfile(tmp / n, HERE / n)
                size = sum((HERE / n).stat().st_size for n in OUTPUTS[fmt])
                print(
                    f"書いた: {names}（{size} バイト。cnn_forward との差 "
                    f"{d_ref:.1e}）"
                )
                continue
            missing = [n for n in OUTPUTS[fmt] if not (HERE / n).exists()]
            if missing:
                print(f"無い（--write で作る）: {'・'.join(missing)}")
                rc = 1
                continue
            d_old = float(np.abs(new - sigmoid(run(fmt, HERE, xs))).max())
            if d_old <= TOL:
                print(f"一致: {names}（置いてある物との差 {d_old:.1e}）")
            else:
                print(f"違う（--write で作り直す）: {names}（差 {d_old:.1e}）")
                rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
