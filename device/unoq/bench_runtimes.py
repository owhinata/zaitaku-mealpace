"""CNN の段の推論ランタイムを替えて 1 窓あたりの処理時間を比べる（Issue #51。
UNO Q の Linux 側と PC。計測だけ。装置の推論は numpy のまま）。

UNO Q の上で（~/zaitaku-mealpace-device/ で。計測用の venv の python）:
  B=~/zaitaku-mealpace-bench-venv/bin/python
  env OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \\
      RUSTICL_ENABLE=freedreno MESA_SHADER_CACHE_DISABLE=true \\
      $B -B bench_runtimes.py --only ncnn-vulkan --threads 1 --overlap
  $B -B bench_runtimes.py --list        （バックエンドの名前の一覧）
  $B -B bench_runtimes.py --only idle   （何もしない間の CPU 使用率）
PC で（RPC を省く）:
  .venv/bin/python -B device/unoq/bench_runtimes.py --no-rpc --sustain-s 0

bench_infer.py（#44）の部品をそのまま使い、(c)「CNN 1 窓」と (f)「全体・構成 J」
の CNN の段だけを差し替える。同じ乱数の重み・正規化の統計量・合成入力
（default_rng(0) の順も bench_infer.run と同じ）。

1 つのバックエンドの手順: import（無ければ「未導入」）→ 初期化を 1 回測る →
環境の行 → 1 回目の実行（照合の前）→ 照合（乱数の重みの cnn_forward と。numpy の
書き方は reference_cnn() の重みと入力で features_np_ref.npz とも）→ 照合 OK のとき
だけ 1 回目の値、(c)・(f) の壁時計と CPU 時間（process_time_ns）、持続的な負荷
（--sustain-s 秒、実際の周期）、重ね合わせ（--overlap、GPU だけ）、スレッド数。
ランタイムの import はバックエンドの中で遅らせて行う（import でプロセスごと落ちる
ものがあるため）。

入力は乱数と正弦波（数式の信号）だけ。音声のデバイス・ファイルを開かない。
ファイルを書かない。環境変数を書き換えない（GPU やスレッドの変数は計測のコマンドで
渡す）。標準出力は集計の行だけ（特徴量・確率・波形の値を出さない。照合は差の最大
だけ）。重みは学習済みのものではない（時間を測るための同じ形の乱数）。
"""

import argparse
import concurrent.futures
import contextlib
import importlib.metadata
import os
import platform
import resource
import sys
import time
from pathlib import Path

import numpy as np

import bench_infer as bi
import features_np as fnp
import led_check
import mcu_rpc

HERE = Path(__file__).resolve().parent
# ランタイムのモジュール（import はバックエンドの load の中だけ）
RUNTIME_MODULES = ("onnxruntime", "ai_edge_litert", "pyarmnn", "ncnn")
MODEL_ONNX = HERE / "cnn_rand0.onnx"
MODEL_TFLITE = HERE / "cnn_rand0.tflite"
MODEL_NCNN = (HERE / "cnn_rand0.ncnn.param", HERE / "cnn_rand0.ncnn.weights")
NCNN_IN, NCNN_OUT = "in0", "out0"  # pnnx が付ける名前
TOL_NP = 1e-6  # numpy の書き方の照合
TOL_FP32 = 1e-5  # fp32 のランタイム（確率の差）
TOL_FP16 = 5e-3  # fp16 の経路（確率の差。docs の選択肢 6）
GPU_ENV = ("RUSTICL_ENABLE", "MESA_SHADER_CACHE_DISABLE")
CONFIG = "J"


class Unavailable(Exception):
    """入っているが、この経路では動かない（理由はメッセージ）。"""


def sigmoid(z) -> np.ndarray:
    z = np.asarray(z, dtype=np.float64).reshape(-1)
    return 1.0 / (1.0 + np.exp(-z))


def _version(dist, mod=None) -> str:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return str(getattr(mod, "__version__", "不明"))


def _bid(b) -> str:
    """pyarmnn の BackendId → 名前。"""
    get = getattr(b, "Get", None)
    return str(get() if callable(get) else b)


# --- numpy の書き方（計測用。features_np は変えない） ---


def _conv_einsum(optimize):
    def conv(x, W, b):
        xp = np.pad(x, ((0, 0), (0, 0), (1, 1), (1, 1)))
        cols = np.lib.stride_tricks.sliding_window_view(xp, (3, 3), axis=(2, 3))
        out = np.einsum("bchwij,ocij->bohw", cols, W, optimize=optimize)
        return out + b[None, :, None, None]

    return conv


def _conv_im2col(x, W, b):
    """パディング → sliding_window_view → reshape → W.reshape(Co, -1) @ cols。"""
    B, Ci, H, Wd = x.shape
    Co = W.shape[0]
    xp = np.pad(x, ((0, 0), (0, 0), (1, 1), (1, 1)))
    cols = np.lib.stride_tricks.sliding_window_view(xp, (3, 3), axis=(2, 3))
    # (B, Ci, H, W, 3, 3) → (B, Ci × 9, H × W)。W の (Ci, 3, 3) と同じ並び
    cols = cols.transpose(0, 1, 4, 5, 2, 3).reshape(B, Ci * 9, H * Wd)
    out = W.reshape(Co, -1) @ cols
    return out.reshape(B, Co, H, Wd) + b[None, :, None, None]


def _forward_with(conv):
    """fnp.cnn_forward と同じ順伝播で、畳み込みだけを conv にしたもの。"""

    def forward(params, M):
        x = np.asarray(M, dtype=np.float32)[:, None, :, :]
        for k in range(3):
            x = conv(
                x, params[f"conv{k + 1}.weight"], params[f"conv{k + 1}.bias"]
            )
            x = np.maximum(x, 0.0)
            if k < 2:
                x = fnp._maxpool2(x)
        g = x.mean(axis=(2, 3))
        z = g @ params["fc.weight"].T[:, 0] + params["fc.bias"][0]
        return 1.0 / (1.0 + np.exp(-z.astype(np.float64)))

    return forward


# --- バックエンド ---


class Backend:
    """CNN の段の差し替え。run(Z) は正規化済みの (B, 98, 40) float32 → 確率
    (B,)。fp16 が真なら照合の許容差を TOL_FP16 にする。"""

    gpu = False
    fp16 = False
    forward = None  # numpy の書き方だけ: (params, M) → 確率

    def __init__(self, name, model):
        self.name = name
        self.model = model

    def load(self) -> None:
        """ランタイムの import（無ければ ModuleNotFoundError → 未導入）。"""

    def init(self, threads) -> None:
        """セッションの作成・モデルの読み込み・GPU の準備。"""

    def env(self) -> list:
        return []

    def run(self, Z) -> np.ndarray:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def tol(self) -> float:
        if self.forward is not None:
            return TOL_NP
        return TOL_FP16 if self.fp16 else TOL_FP32


class NumpyBackend(Backend):
    def __init__(self, name, model, forward, note):
        super().__init__(name, model)
        self.forward = forward
        self.note = note

    def env(self):
        return [f"numpy {np.__version__}（{self.note}。スレッドは環境変数で）"]

    def run(self, Z):
        return self.forward(self.model.cnn, Z)


class OrtBackend(Backend):
    def __init__(self, name, model, provider):
        super().__init__(name, model)
        self.provider = provider

    def load(self):
        import onnxruntime

        self.ort = onnxruntime

    def init(self, threads):
        ort = self.ort
        self.avail = ort.get_available_providers()
        if self.provider not in self.avail:
            raise Unavailable(
                f"{self.provider} が get_available_providers() に無い"
                f"（{', '.join(self.avail)}）"
            )
        if not MODEL_ONNX.is_file():
            raise Unavailable(f"モデルのファイルが無い（{MODEL_ONNX.name}）")
        so = ort.SessionOptions()
        if threads == 1:
            so.intra_op_num_threads = 1
            so.inter_op_num_threads = 1
        provs = [self.provider]
        if self.provider != "CPUExecutionProvider":
            opt = {"intra_op_num_threads": 1} if threads == 1 else {}
            provs = [(self.provider, opt), "CPUExecutionProvider"]
        self.sess = ort.InferenceSession(
            str(MODEL_ONNX), sess_options=so, providers=provs
        )
        self.in_name = self.sess.get_inputs()[0].name

    def env(self):
        return [
            f"onnxruntime {self.ort.__version__}（{self.ort.__file__}）、"
            f"使われた providers {', '.join(self.sess.get_providers())}、"
            f"get_available_providers {', '.join(self.avail)}"
        ]

    def run(self, Z):
        return sigmoid(self.sess.run(None, {self.in_name: Z})[0])

    def close(self):
        self.sess = None


class LiteRtCpuBackend(Backend):
    def load(self):
        import ai_edge_litert
        from ai_edge_litert.interpreter import Interpreter

        self.mod = ai_edge_litert
        self.Interpreter = Interpreter

    def init(self, threads):
        if not MODEL_TFLITE.is_file():
            raise Unavailable(f"モデルのファイルが無い（{MODEL_TFLITE.name}）")
        self.it = self.Interpreter(
            model_path=str(MODEL_TFLITE),
            num_threads=1 if threads == 1 else None,
        )
        self.it.allocate_tensors()
        self.i = self.it.get_input_details()[0]["index"]
        self.o = self.it.get_output_details()[0]["index"]

    def env(self):
        th = "num_threads 1" if self.threads == 1 else "num_threads 既定"
        return [
            f"ai-edge-litert {_version('ai-edge-litert', self.mod)}"
            f"（{self.mod.__file__}）、Interpreter（既定の XNNPACK）、{th}"
        ]

    def run(self, Z):
        self.it.set_tensor(self.i, Z)
        self.it.invoke()
        return sigmoid(self.it.get_tensor(self.o))

    def close(self):
        self.it = None


class LiteRtGpuBackend(Backend):
    """既定の設定（GpuOptions.enforce_f32 が偽）は fp16 で計算しうるので、
    ncnn-vulkan（既定）と同じく fp16 の経路の許容差で照合する。enforce_f32 が
    真のもの（litert-gpu-fp32）は fp32 の許容差（ncnn-vulkan-fp32 と同じ対）。"""

    gpu = True

    def __init__(self, name, model, enforce_f32=False):
        super().__init__(name, model)
        self.enforce_f32 = enforce_f32
        self.fp16 = not enforce_f32

    def load(self):
        import ai_edge_litert
        from ai_edge_litert import compiled_model, hardware_accelerator
        from ai_edge_litert import options

        self.mod = ai_edge_litert
        self.cm = compiled_model
        self.ha = hardware_accelerator
        self.opts = options

    def init(self, threads):
        if not MODEL_TFLITE.is_file():
            raise Unavailable(f"モデルのファイルが無い（{MODEL_TFLITE.name}）")
        # GPU | CPU を渡すと黙って CPU に落ちうるので GPU だけを渡す
        gpu = self.ha.HardwareAccelerator.GPU
        if self.enforce_f32:
            opt = self.opts.Options(
                hardware_accelerators=gpu,
                gpu_options=self.opts.GpuOptions(enforce_f32=True),
            )
            self.m = self.cm.CompiledModel.from_file(
                str(MODEL_TFLITE), options=opt
            )
        else:
            self.m = self.cm.CompiledModel.from_file(
                str(MODEL_TFLITE), hardware_accel=gpu
            )
        self.full = bool(self.m.is_fully_accelerated())
        if not self.full:
            raise Unavailable(
                "is_fully_accelerated() が偽（GPU で動いていない）"
            )
        self.ins = self.m.create_input_buffers(0)
        self.outs = self.m.create_output_buffers(0)

    def env(self):
        return [
            f"ai-edge-litert {_version('ai-edge-litert', self.mod)}、"
            "CompiledModel（HardwareAccelerator.GPU だけ）、"
            f"is_fully_accelerated {self.full}、"
            + (
                "GpuOptions の enforce_f32 真（fp32）"
                if self.enforce_f32
                else "GpuOptions は既定（enforce_f32 偽。fp16 を使いうる）"
            )
        ]

    def run(self, Z):
        self.ins[0].write(np.ascontiguousarray(Z))
        self.m.run_by_index(0, self.ins, self.outs)
        return sigmoid(self.outs[0].read(len(Z), np.float32))

    def close(self):
        self.ins = self.outs = self.m = None


class ArmnnBackend(Backend):
    def __init__(self, name, model, backend_id, fp16=False):
        super().__init__(name, model)
        self.backend_id = backend_id
        self.gpu = backend_id == "GpuAcc"
        self.fp16 = fp16

    def load(self):
        import pyarmnn

        self.ann = pyarmnn

    def init(self, threads):
        ann = self.ann
        self.rt = ann.IRuntime(ann.CreationOptions())
        # 登録されたバックエンド（pyarmnn 33 には BackendRegistryInstance が
        # 無いので、ランタイムの DeviceSpec から読む）
        self.registered = sorted(
            _bid(b) for b in self.rt.GetDeviceSpec().GetSupportedBackends()
        )
        if self.backend_id not in self.registered:
            raise Unavailable(
                f"バックエンドが無い（{self.backend_id}。登録 "
                f"{', '.join(self.registered) or 'なし'}）"
            )
        if not MODEL_TFLITE.is_file():
            raise Unavailable(f"モデルのファイルが無い（{MODEL_TFLITE.name}）")
        parser = ann.ITfLiteParser()
        net = parser.CreateNetworkFromBinaryFile(str(MODEL_TFLITE))
        (in_name,) = parser.GetSubgraphInputTensorNames(0)
        (out_name,) = parser.GetSubgraphOutputTensorNames(0)
        self.in_info = parser.GetNetworkInputBindingInfo(0, in_name)
        self.out_info = parser.GetNetworkOutputBindingInfo(0, out_name)
        opt = ann.OptimizerOptions()
        opt.m_ReduceFp32ToFp16 = bool(self.fp16)
        # 1 つだけを渡す（ほかのバックエンドへ黙って落ちない。割り当てられ
        # なければ Optimize が失敗する）
        opt_net, msgs = ann.Optimize(
            net, [ann.BackendId(self.backend_id)], self.rt.GetDeviceSpec(), opt
        )
        self.msgs = [str(m) for m in msgs]
        self.net_id, _ = self.rt.LoadNetwork(opt_net)
        self.out_tensors = ann.make_output_tensors([self.out_info])

    def env(self):
        fp = "ReduceFp32ToFp16 真" if self.fp16 else "fp32"
        return [
            f"pyarmnn {_version('pyarmnn', self.ann)}（{self.ann.__file__}）、"
            f"登録されたバックエンド {', '.join(self.registered)}、"
            f"割り当て {self.backend_id} だけ（ほかへ落とさない）、{fp}、"
            f"Optimize のメッセージ {len(self.msgs)} 件"
            + (f"（1 件目: {self.msgs[0][:80]}）" if self.msgs else "")
        ]

    def run(self, Z):
        ann = self.ann
        ins = ann.make_input_tensors([self.in_info], [np.ascontiguousarray(Z)])
        self.rt.EnqueueWorkload(self.net_id, ins, self.out_tensors)
        return sigmoid(ann.workload_tensors_to_ndarray(self.out_tensors)[0])

    def close(self):
        with contextlib.suppress(Exception):
            self.rt.UnloadNetwork(self.net_id)
        self.rt = None


class NcnnBackend(Backend):
    def __init__(self, name, model, vulkan, fp32=True):
        super().__init__(name, model)
        self.gpu = vulkan
        self.fp32 = fp32

    def load(self):
        import ncnn

        self.ncnn = ncnn

    def init(self, threads):
        ncnn = self.ncnn
        for p in MODEL_NCNN:
            if not p.is_file():
                raise Unavailable(f"モデルのファイルが無い（{p.name}）")
        net = ncnn.Net()
        net.opt.use_vulkan_compute = bool(self.gpu)
        if self.fp32:
            net.opt.use_fp16_packed = False
            net.opt.use_fp16_storage = False
            net.opt.use_fp16_arithmetic = False
            net.opt.use_bf16_storage = False
        if threads == 1:
            net.opt.num_threads = 1
        self.count = self.dev = None
        if self.gpu:
            self.count = ncnn.get_gpu_count()
            if self.count < 1:
                raise Unavailable("get_gpu_count() が 0")
            try:
                self.dev = ncnn.get_gpu_info(0).device_name()
            except Exception as e:  # 版によって名前の取り方が違う
                self.dev = f"読めない（{type(e).__name__}）"
            if "llvmpipe" in str(self.dev).lower():
                raise Unavailable(f"device 0 が llvmpipe（{self.dev}）")
            net.set_vulkan_device(0)
        net.load_param(str(MODEL_NCNN[0]))
        net.load_model(str(MODEL_NCNN[1]))
        self.fp16 = self.gpu and any(
            (
                net.opt.use_fp16_packed,
                net.opt.use_fp16_storage,
                net.opt.use_fp16_arithmetic,
            )
        )
        self.net = net
        self.threads_opt = net.opt.num_threads

    def env(self):
        o = self.net.opt
        dev = (
            f"Vulkan、get_gpu_count {self.count}、device 0 {self.dev}"
            if self.gpu
            else "CPU（Vulkan を切る）"
        )
        return [
            f"ncnn {_version('ncnn', self.ncnn)}（{self.ncnn.__file__}）、"
            f"{dev}、use_fp16_packed/storage/arithmetic {o.use_fp16_packed}/"
            f"{o.use_fp16_storage}/{o.use_fp16_arithmetic}、"
            f"use_bf16_storage {o.use_bf16_storage}、"
            f"num_threads {self.threads_opt}"
        ]

    def run(self, Z):
        ex = self.net.create_extractor()
        ex.input(NCNN_IN, self.ncnn.Mat(np.ascontiguousarray(Z[0])))
        _, out = ex.extract(NCNN_OUT)
        return sigmoid(np.array(out))

    def close(self):
        self.net.clear()
        self.net = None
        if self.gpu:
            with contextlib.suppress(Exception):
                self.ncnn.destroy_gpu_instance()


def default_backends() -> dict:
    """名前 → (model を受け取って Backend を作る関数)。--list の順。"""
    return {
        "numpy": lambda m: NumpyBackend(
            "numpy", m, fnp.cnn_forward, "features_np.cnn_forward（#44 と同じ）"
        ),
        "numpy-einsum-noopt": lambda m: NumpyBackend(
            "numpy-einsum-noopt",
            m,
            _forward_with(_conv_einsum(False)),
            "einsum、optimize=False",
        ),
        "numpy-im2col": lambda m: NumpyBackend(
            "numpy-im2col", m, _forward_with(_conv_im2col), "im2col ＋ @"
        ),
        "ort-cpu": lambda m: OrtBackend("ort-cpu", m, "CPUExecutionProvider"),
        "ort-xnnpack": lambda m: OrtBackend(
            "ort-xnnpack", m, "XnnpackExecutionProvider"
        ),
        "litert-cpu": lambda m: LiteRtCpuBackend("litert-cpu", m),
        "armnn-cpuacc": lambda m: ArmnnBackend("armnn-cpuacc", m, "CpuAcc"),
        "ncnn-cpu": lambda m: NcnnBackend("ncnn-cpu", m, vulkan=False),
        "armnn-gpuacc": lambda m: ArmnnBackend("armnn-gpuacc", m, "GpuAcc"),
        "armnn-gpuacc-fp16": lambda m: ArmnnBackend(
            "armnn-gpuacc-fp16", m, "GpuAcc", fp16=True
        ),
        "ncnn-vulkan": lambda m: NcnnBackend(
            "ncnn-vulkan", m, vulkan=True, fp32=False
        ),
        "ncnn-vulkan-fp32": lambda m: NcnnBackend(
            "ncnn-vulkan-fp32", m, vulkan=True
        ),
        "litert-gpu": lambda m: LiteRtGpuBackend("litert-gpu", m),
        "litert-gpu-fp32": lambda m: LiteRtGpuBackend(
            "litert-gpu-fp32", m, enforce_f32=True
        ),
    }


IDLE = (
    "idle"  # 何もしない間の CPU 使用率（--only idle だけ。--list に出さない）
)


# --- 計測の部品 ---


def measure2(fn, n, warmup) -> tuple:
    """(壁時計の集計, CPU 時間の集計, (user 秒, system 秒))。同じ 1 回の前後で
    perf_counter_ns と process_time_ns（プロセスの全スレッド）を取る。"""
    for _ in range(warmup):
        fn()
    wall, cpu = [], []
    r0 = resource.getrusage(resource.RUSAGE_SELF)
    for _ in range(n):
        t0 = time.perf_counter_ns()
        c0 = time.process_time_ns()
        fn()
        c1 = time.process_time_ns()
        t1 = time.perf_counter_ns()
        wall.append(t1 - t0)
        cpu.append(c1 - c0)
    r1 = resource.getrusage(resource.RUSAGE_SELF)
    us = (r1.ru_utime - r0.ru_utime, r1.ru_stime - r0.ru_stime)
    return led_check.summarize(wall), led_check.summarize(cpu), us


def _read(path) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def proc_self_ticks():
    """/proc/self/stat の utime ＋ stime（クロックの刻み）。読めなければ None。"""
    s = _read("/proc/self/stat")
    if ")" not in s:
        return None
    rest = s[s.rindex(")") + 2 :].split()
    return int(rest[11]) + int(rest[12])


def proc_stat_cpu():
    """/proc/stat の 1 行目 → (全体, idle ＋ iowait)。読めなければ None。"""
    for line in _read("/proc/stat").splitlines():
        if line.startswith("cpu "):
            v = [int(x) for x in line.split()[1:9]]
            return sum(v), v[3] + v[4]
    return None


def clk_tck() -> int:
    try:
        return int(os.sysconf("SC_CLK_TCK"))
    except (ValueError, OSError):
        return 100


def usage(fn_run, dur_s) -> tuple:
    """fn_run(終わりの時刻) の間のこのプロセスと全体の CPU 使用率（%）。
    (プロセス, 全体, 経過秒, fn_run の戻り値)。読めなければ None。"""
    p0, s0 = proc_self_ticks(), proc_stat_cpu()
    t0 = time.monotonic()
    out = fn_run(t0 + dur_s)
    now = time.monotonic()
    if now < t0 + dur_s:
        time.sleep(t0 + dur_s - now)
    el = time.monotonic() - t0
    p1, s1 = proc_self_ticks(), proc_stat_cpu()
    pu = su = None
    if p0 is not None and p1 is not None:
        pu = 100.0 * (p1 - p0) / clk_tck() / el
    if s0 is not None and s1 is not None and s1[0] > s0[0]:
        tot = s1[0] - s0[0]
        su = 100.0 * (tot - (s1[1] - s0[1])) / tot
    return pu, su, el, out


def periodic(step, hop_s, t_end) -> tuple:
    """期限 t0 ＋ k × hop_s ごとに step を 1 回。遅れたら次の期限へ進む（追い
    つくための連続実行はしない）。(回数, 遅れた回数, 1 回の壁時計 ns の列)。"""
    t0 = time.monotonic()
    k = runs = late = 0
    walls = []
    while True:
        d = t0 + k * hop_s
        if d >= t_end:
            break
        now = time.monotonic()
        if k > 0 and now > d:  # 前の回が次の期限を越えた
            late += 1
            k = int((now - t0) // hop_s) + 1
            continue
        if now < d:
            time.sleep(d - now)
        a = time.perf_counter_ns()
        step()
        walls.append(time.perf_counter_ns() - a)
        runs += 1
        k += 1
    return runs, late, walls


def _pct(v) -> str:
    return "読めない" if v is None else f"{v:.1f}%"


# --- 環境 ---


def mapped_blas() -> list:
    """/proc/self/maps に読み込まれた BLAS・LAPACK のパス（読むだけ）。"""
    paths = set()
    for line in _read("/proc/self/maps").splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        base = os.path.basename(parts[-1]).lower()
        if "blas" in base or "lapack" in base:
            paths.add(parts[-1])
    return sorted(paths)


def alternatives() -> str:
    arch = {"aarch64": "aarch64", "x86_64": "x86_64"}.get(
        platform.machine(), platform.machine()
    )
    out = []
    for lib in ("libblas.so.3", "liblapack.so.3"):
        p = f"/etc/alternatives/{lib}-{arch}-linux-gnu"
        try:
            out.append(f"{p} → {os.readlink(p)}")
        except OSError:
            out.append(f"{p} は無い")
    return "、".join(out)


def env_lines() -> list:
    gpu = "、".join(f"{k}={os.environ.get(k, 'なし')}" for k in GPU_ENV)
    blas = "、".join(mapped_blas()) or "見つからない"
    return bi.env_lines() + [
        f"環境: sys.executable {sys.executable}、sys.prefix {sys.prefix}、"
        f"numpy {np.__file__}",
        f"環境: 読み込んだ BLAS・LAPACK（/proc/self/maps）: {blas}",
        f"環境: {alternatives()}",
        f"環境: {gpu}、SC_CLK_TCK {clk_tck()}",
    ]


# --- 1 つのバックエンド ---


class Wrapped:
    """bi.Pipeline に渡す model。CNN の段だけをバックエンドに替える。"""

    def __init__(self, model, backend):
        self.model = model
        self.backend = backend

    def cnn_prob(self, M) -> float:
        Z = (M - self.model.mel_mean) / self.model.mel_std
        return float(self.backend.run(Z[None])[0])

    def lr_prob(self, f) -> float:
        return self.model.lr_prob(f)


def _fits(s, lim) -> str:
    return "、".join(
        f"{name} {'収まる' if s[key] < lim else '収まらない'}"
        for name, key in (
            ("中央値", "median_ms"),
            ("95 パーセンタイル", "p95_ms"),
            ("最大", "max_ms"),
        )
    )


def _ratio(w, c, us) -> str:
    r = c["median_ms"] / w["median_ms"] if w["median_ms"] > 0 else float("nan")
    return (
        f"CPU 時間 ÷ 壁時計（中央値どうし）{r:.2f}、{w['n']} 回の合計 "
        f"user {us[0]:.3f} 秒・system {us[1]:.3f} 秒（getrusage）"
    )


def verify_lines(be, ctx) -> tuple:
    """(OK か, 行)。乱数の重みの cnn_forward と比べ、numpy の書き方は
    reference_cnn() の重みと入力で features_np_ref.npz の cnn とも比べる。"""
    want = np.concatenate([fnp.cnn_forward(be.model.cnn, Z) for Z in ctx.vin])
    got = np.concatenate([be.run(Z) for Z in ctx.vin])
    d = float(np.abs(got - want).max()) if got.shape == want.shape else np.inf
    tol = be.tol()
    ok = bool(np.isfinite(d) and d <= tol)
    fp = "fp16 の経路" if be.fp16 else "fp32"
    lines = [
        f"照合: {'OK' if ok else 'NG'}（乱数の重みの cnn_forward との確率の"
        f"最大の差 {d:.1e}、許容 {tol:g}、{fp}。入力 {len(ctx.vin)} つ）"
    ]
    if be.forward is not None:
        params, M = fnp.reference_cnn()
        ref = np.asarray(ctx.ref["cnn"], dtype=np.float64)
        out = np.asarray(be.forward(params, M), dtype=np.float64)
        d2 = (
            float(np.abs(out - ref).max()) if out.shape == ref.shape else np.inf
        )
        ok2 = bool(np.isfinite(d2) and d2 <= TOL_NP)
        ok = ok and ok2
        lines.append(
            f"照合（PC の参照）: {'OK' if ok2 else 'NG'}（reference_cnn() の"
            f"重みと入力で features_np_ref.npz の cnn との差 {d2:.1e}、"
            f"許容 {TOL_NP:g}）"
        )
    return ok, lines


def overlap_lines(be, ctx, hop_s, hop_n, n, warmup) -> list:
    """スレッドで投入する重ね合わせと、同じ仕事を直列にした形。"""
    model = ctx.model
    rng = np.random.default_rng(2)
    blocks = [rng.normal(0.0, bi.BLOCK_RMS, hop_n) for _ in range(8)]
    st = {"i": 0, "dec": fnp.Decimate3(), "ring": bi.Ring()}
    while st["ring"].filled < fnp.WINDOW_SAMPLES:
        st["ring"].push(st["dec"].process(blocks[st["i"] % 8]))
        st["i"] += 1

    def norm():
        M = fnp.mfe40(st["ring"].latest())
        return ((M - model.mel_mean) / model.mel_std)[None]

    st["Z"] = norm()

    def cpu_part():
        st["ring"].push(st["dec"].process(blocks[st["i"] % 8]))
        st["i"] += 1
        return norm()

    def serial():
        be.run(st["Z"])
        st["Z"] = cpu_part()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:

        def threaded():
            fut = ex.submit(be.run, st["Z"])
            z_next = cpu_part()
            fut.result()
            st["Z"] = z_next

        ws, cs, us_s = measure2(serial, n, warmup)
        wt, ct, us_t = measure2(threaded, n, warmup)
    tag = f"重ね合わせ ホップ {hop_s:g} 秒（CNN ＋ 次のブロックの間引き ＋ "
    tag += "mfe40。後処理と RPC は除く）"
    return [
        f"{tag}直列 壁時計: {bi.fmt(ws)}",
        f"{tag}直列 CPU 時間: {bi.fmt(cs)}。{_ratio(ws, cs, us_s)}",
        f"{tag}スレッドで投入 壁時計: {bi.fmt(wt)}",
        f"{tag}スレッドで投入 CPU 時間: {bi.fmt(ct)}。{_ratio(wt, ct, us_t)}",
        f"重ね合わせ ホップ {hop_s:g} 秒: 中央値の差（直列 − スレッド）"
        f"{ws['median_ms'] - wt['median_ms']:.3f} ms",
    ]


def run_backend(be, ctx, a) -> list:
    """1 つのバックエンドの行。照合 NG・動作せずは時間を出さない。"""
    out = []

    def say(s):
        line = f"[{be.name}] {s}"
        print(line, flush=True)
        out.append(line)

    try:
        be.load()
    except ModuleNotFoundError as e:
        root = (e.name or "").split(".")[0]
        if root not in RUNTIME_MODULES:
            say(f"動作せず（import。{_err(e)}）")
            return out
        say(f"未導入（{e.name}）")
        return out
    except Exception as e:  # import の中の別の失敗
        say(f"動作せず（import。{_err(e)}）")
        return out
    be.threads = a.threads
    t0 = time.perf_counter_ns()
    try:
        be.init(a.threads)
    except Unavailable as e:
        say(f"動作せず（{e}）")
        return out
    except Exception as e:
        say(f"動作せず（初期化。{_err(e)}）")
        return out
    say(
        f"初期化（セッションの作成・モデルの読み込み・GPU の準備）: "
        f"{(time.perf_counter_ns() - t0) / 1e6:.3f} ms"
    )
    try:
        for line in be.env():
            say(f"環境: {line}")
        t0 = time.perf_counter_ns()
        be.run(ctx.Zc)
        first_ms = (time.perf_counter_ns() - t0) / 1e6
        ok, lines = verify_lines(be, ctx)
        for line in lines:
            say(line)
        if not ok:
            say("照合が NG なので測らない")
            return out
        say(
            f"1 回目の実行（初期化の直後、照合の前。(c) と同じ入力）: "
            f"{first_ms:.3f} ms"
        )
        wrapped = Wrapped(ctx.model, be)
        w, c, us = measure2(lambda: wrapped.cnn_prob(ctx.M), a.n, a.warmup)
        say(f"(c) CNN 1 窓（正規化 ＋ バックエンド、B = 1）壁時計: {bi.fmt(w)}")
        say(f"(c) CPU 時間: {bi.fmt(c)}。{_ratio(w, c, us)}")
        tail = "RPC は除く" if ctx.link is None else "→ RPC"
        for (hop_s, hop_n), blocks_rng in zip(bi.HOPS, ctx.pipe_rngs):
            pipe = bi.Pipeline(CONFIG, hop_n, wrapped, ctx.link, blocks_rng())
            label = f"構成 {CONFIG}・ホップ {hop_s:g} 秒"
            w, c, us = measure2(pipe.step, a.n, a.warmup)
            say(f"(f) 全体 {label}（{tail}）壁時計: {bi.fmt(w)}")
            say(f"(f) 全体 {label} CPU 時間: {bi.fmt(c)}。{_ratio(w, c, us)}")
            lim = bi.LIMIT_MS[hop_s]
            say(f"(f) {label}: {lim:g} ms 未満か: {_fits(w, lim)}")
            if a.sustain_s > 0:
                pu, su, el, (runs, late, walls) = usage(
                    lambda t_end, p=pipe, h=hop_s: periodic(p.step, h, t_end),
                    a.sustain_s,
                )
                s = led_check.summarize(walls) if walls else None
                one = (
                    f"1 回の壁時計 中央値 {s['median_ms']:.3f} ms・最大 "
                    f"{s['max_ms']:.3f} ms"
                    if s
                    else "1 回も回らない"
                )
                say(
                    f"持続 {label}・{el:.1f} 秒（実際の周期）: 回数 {runs}、"
                    f"遅れた回数 {late}、{one}、このプロセスの CPU 使用率 "
                    f"{_pct(pu)}（1 コア = 100%）、全体の CPU 使用率 "
                    f"{_pct(su)}（{os.cpu_count()} コアで 100%）"
                )
        if a.sustain_s <= 0:
            say("持続: 省いた（--sustain-s 0）")
        if a.overlap and be.gpu:
            say(
                "重ね合わせ: 非同期の API なし（この経路の Python の API は"
                "同期の実行だけ）。スレッドで投入（ランタイムの非同期の"
                "API ではない。拡張が GIL を離すかで結果が変わる）。装置で"
                "この形を使うと表示が 1 ホップ遅れる"
            )
            for hop_s, hop_n in bi.HOPS:
                for line in overlap_lines(be, ctx, hop_s, hop_n, a.n, a.warmup):
                    say(line)
        elif a.overlap:
            say("重ね合わせ: 省いた（GPU の経路だけ）")
        say(
            "このプロセスのスレッド数（/proc/self/status、計測の直後）: "
            f"{bi.thread_count()}"
        )
    except Exception as e:
        say(f"動作せず（計測中。{_err(e)}）")
    finally:
        with contextlib.suppress(Exception):
            be.close()
    return out


def _err(e) -> str:
    msg = str(e).strip().splitlines()
    return f"{type(e).__name__}: {msg[0][:160] if msg else ''}"


def idle_lines(a) -> list:
    if a.sustain_s <= 0:
        return ["[idle] 省いた（--sustain-s 0）"]
    pu, su, el, _ = usage(lambda t_end: None, a.sustain_s)
    return [
        f"[idle] 何もしない {el:.1f} 秒（CNN も特徴量も回さず眠るだけ）: "
        f"このプロセスの CPU 使用率 {_pct(pu)}（1 コア = 100%）、全体の CPU "
        f"使用率 {_pct(su)}（{os.cpu_count()} コアで 100%）"
    ]


class Context:
    """bench_infer.run と同じ default_rng(0) の順の合成入力。"""

    def __init__(self, link, ref):
        rng = np.random.default_rng(0)
        self.model = bi.Model(rng)
        t = np.arange(fnp.WINDOW_SAMPLES) / fnp.SR
        seg = 0.1 * np.sin(2 * np.pi * 700.0 * t)
        seg = seg + rng.normal(0.0, 0.05, fnp.WINDOW_SAMPLES)
        self.M = fnp.mfe40(seg)
        self.Zc = ((self.M - self.model.mel_mean) / self.model.mel_std)[None]
        # bench_infer.run の (a) のブロックの分を同じ順で進める
        for _, hop_n in bi.HOPS:
            for _ in range(8):
                rng.normal(0.0, bi.BLOCK_RMS, hop_n)
        # (f) の構成 J のパイプラインと同じ乱数の状態（バックエンドごとに作り
        # 直すので、状態を写して渡す）
        self.pipe_rngs = []
        for _, hop_n in bi.HOPS:
            state = rng.bit_generator.state

            def make(state=state):
                g = np.random.default_rng()
                g.bit_generator.state = state
                return g

            self.pipe_rngs.append(make)
            for _ in range(8):
                rng.normal(0.0, bi.BLOCK_RMS, hop_n)
        _, Mref = fnp.reference_cnn()
        z = np.random.default_rng(1).standard_normal(self.Zc.shape)
        self.vin = [self.Zc, Mref, z.astype(np.float32)]
        self.link = link
        self.ref = ref


def main(
    argv=None, open_link=mcu_rpc.McuLink.open_default, ref=None, backends=None
) -> int:
    table = default_backends() if backends is None else backends
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", action="store_true", help="名前の一覧だけ")
    ap.add_argument(
        "--only", default="", help="名前（カンマ区切り。既定は全部）"
    )
    ap.add_argument(
        "--threads",
        type=int,
        choices=(0, 1),
        default=0,
        help="1 = ランタイムのスレッド数を 1 に指定、0 = 既定",
    )
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--warmup", type=int, default=5)
    ap.add_argument("--sustain-s", type=float, default=60.0)
    ap.add_argument(
        "--overlap",
        action="store_true",
        help="GPU の経路でスレッドで投入する形も測る",
    )
    ap.add_argument("--no-rpc", action="store_true", help="RPC を省く（PC）")
    a = ap.parse_args(argv)
    if a.list:
        for name in table:
            print(name)
        return 0
    if a.n < 2 or a.warmup < 0 or a.sustain_s < 0:
        ap.error("--n は 2 以上、--warmup と --sustain-s は 0 以上")
    names = [s for s in a.only.split(",") if s] or list(table)
    bad = [s for s in names if s not in table and s != IDLE]
    if bad:
        ap.error(f"知らない名前: {', '.join(bad)}（--list で一覧）")
    for line in env_lines():
        print(line)
    print(
        f"環境: --threads {a.threads}、--n {a.n}、--warmup {a.warmup}、"
        f"--sustain-s {a.sustain_s:g}、--overlap {a.overlap}"
    )
    ref = bi.load_ref() if ref is None else ref
    ok, diff = bi.verify(ref)
    print(
        f"照合（#44）: {'OK' if ok else 'NG'}（features_np_ref.npz との"
        f"最大の差 {diff:.1e}、許容 {bi.TOL:g}。タップ・mfe40・a52・cnn_forward・"
        "ブロックの間引き）",
        flush=True,
    )
    if not ok:
        print("照合が NG なので測らない")
        return 1
    cm = contextlib.nullcontext(None) if a.no_rpc else open_link()
    with cm as link:
        try:
            ctx = Context(link, ref)
            for name in names:
                if name == IDLE:
                    for line in idle_lines(a):
                        print(line, flush=True)
                    continue
                run_backend(table[name](ctx.model), ctx, a)
        finally:
            if link is not None:
                link.set_state(mcu_rpc.STATE_OFF)
    if link is not None:
        print("表示を消しました（確かめ）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
