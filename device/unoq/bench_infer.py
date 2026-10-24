"""1 窓あたりの処理時間を合成入力で測る（Issue #44。UNO Q の Linux 側と PC）。

UNO Q の上で（~/zaitaku-mealpace-device/ で）:
  env OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \\
      python3 -B bench_infer.py
  env -u OPENBLAS_NUM_THREADS -u OMP_NUM_THREADS -u MKL_NUM_THREADS \\
      python3 -B bench_infer.py
PC で（RPC を省く）: python3 -B device/unoq/bench_infer.py --no-rpc

最初に照合: 決定的な合成入力（features_np.reference_*）から間引きのタップ・
mfe40・a52・cnn_forward を計算し、features_np_ref.npz（PC の librosa 版の参照）と
比べる。ブロックごとの間引きとセッション全体の間引きの一致も見る。NG なら測らずに
1 で終わる。

測るもの（各 --n 回、測らない --warmup 回。time.perf_counter_ns）:
  (a) 間引き: 48 kHz の 1 ホップ分（0.25 秒・0.1 秒）を Decimate3.process に渡す
  (b) mfe40（16000 サンプル → 98 × 40）
  (c) CNN: 帯域ごとの正規化 ＋ cnn_forward（B = 1、乱数の重み）
  (d) a52 ＋ 標準化 ＋ ロジスティック回帰（乱数の係数）
  (e) RPC: zm_set_state(消灯) の往復（描画は含まない。--no-rpc で省く）
  (f) 全体: 1 ホップ分の間引き → 直近 16000 サンプル（メモリのリング）の特徴量
      → 正規化 → 推論 → 後処理の 1 窓分 → RPC を直列に 1 回通した時間。
      構成 J（mfe40 ＋ CNN）・構成 J＋R（＋ a52 ＋ ロジスティック回帰）×
      ホップ 0.25 秒・0.1 秒。
250 ms・100 ms と比べるのは (f) の数字だけ。部品の和は参考として並べる。

入力は乱数と正弦波（数式の信号）だけ。音声のデバイス・ファイルを開かない。
ファイルを書かない。標準出力は集計の行だけ（特徴量・確率・波形の値を出さない）。
重みは学習済みのものではない（時間を測るための同じ形の乱数）。
"""

import argparse
import contextlib
import os
import platform
import sys
import time
from pathlib import Path

import numpy as np

import features_np as fnp
import led_check
import mcu_rpc
import postprocess

TOL = 1e-5  # 照合の許容差（PC の librosa 版の参照との差の絶対値）
HOPS = ((0.25, 12000), (0.1, 4800))  # (秒, 48 kHz のサンプル数)
CONFIGS = ("J", "J＋R")
LIMIT_MS = {0.25: 250.0, 0.1: 100.0}
ENV_VARS = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")
# 48 kHz の合成ブロック（乱数）の RMS。解析の目盛り（int16 ÷ 32768）に合わせる
BLOCK_RMS = 0.05


# --- 照合 ---


def load_ref(path=fnp.REF_PATH) -> dict:
    with np.load(path, allow_pickle=False) as z:
        return {k: z[k] for k in z.files}


def verify(ref) -> tuple:
    """(OK か, 最大の差)。形が合わない・項目が無いときは NG（差は inf）。"""
    try:
        x = fnp.reference_signal()
        params, M = fnp.reference_cnn()
        pairs = [
            (fnp.TAPS, ref["taps"]),
            (fnp.mfe40(x), ref["mfe40"]),
            (fnp.a52(x), ref["a52"]),
            (fnp.cnn_forward(params, M), ref["cnn"]),
        ]
        x48 = fnp.reference_input48k(60000)
        full = fnp.decimate3(x48)
        dec = fnp.Decimate3()
        outs, pos, k = [], 0, 0
        sizes = (4800, 12000, 7, 1181)
        while pos < len(x48):
            s = sizes[k % len(sizes)]
            outs.append(dec.process(x48[pos : pos + s]))
            pos, k = pos + s, k + 1
        y = np.concatenate(outs)
        pairs.append((y, full[: len(y)]))
        diffs = []
        for a, b in pairs:
            a = np.asarray(a, dtype=np.float64)
            b = np.asarray(b, dtype=np.float64)
            if a.shape != b.shape:
                return False, float("inf")
            diffs.append(float(np.abs(a - b).max()))
    except (KeyError, ValueError):
        return False, float("inf")
    m = max(diffs)
    return bool(np.isfinite(m) and m <= TOL), m


# --- 計測の部品 ---


# compare.measure_timings と同じ作り方の乱数の重み（同じ形）。本体は
# features_np に移した（Issue #51。変換・テスト・bench_runtimes と共有する）
random_cnn = fnp.random_cnn


class Model:
    """時間を測るための乱数の重み・正規化の統計量・係数（学習済みではない）。"""

    def __init__(self, rng):
        self.cnn = random_cnn(rng)
        self.mel_mean = rng.normal(0.0, 1.0, fnp.N_MEL_CNN).astype(np.float32)
        self.mel_std = (1.0 + rng.random(fnp.N_MEL_CNN)).astype(np.float32)
        n = 4 * fnp.N_MFCC
        self.a_mean = rng.normal(0.0, 1.0, n)
        self.a_std = 1.0 + rng.random(n)
        self.lr_w = rng.normal(0.0, 0.1, n)
        self.lr_b = 0.0

    def cnn_prob(self, M) -> float:
        Z = (M - self.mel_mean) / self.mel_std
        return float(fnp.cnn_forward(self.cnn, Z[None])[0])

    def lr_prob(self, f) -> float:
        z = ((f - self.a_mean) / self.a_std) @ self.lr_w + self.lr_b
        return float(1.0 / (1.0 + np.exp(-z)))


def hyst_step(on: bool, prob: float, valid: bool, enter, exit) -> bool:
    """postprocess.hysteresis と同じ規則で状態を 1 窓進める（計測用。#47 の
    本実装の代わりではない）。"""
    if on:
        return bool(valid) and prob >= exit
    return bool(valid) and prob >= enter


class Ring:
    """16 kHz の直近 WINDOW_SAMPLES サンプル（メモリだけ）。"""

    def __init__(self):
        self.buf = np.zeros(fnp.WINDOW_SAMPLES)
        self.w = 0
        self.filled = 0

    def push(self, y) -> None:
        R, n = len(self.buf), len(y)
        if n >= R:
            self.buf[:] = y[-R:]
            self.w = 0
        else:
            first = min(n, R - self.w)
            self.buf[self.w : self.w + first] = y[:first]
            self.buf[: n - first] = y[first:]
            self.w = (self.w + n) % R
        self.filled = min(R, self.filled + n)

    def latest(self) -> np.ndarray:
        return np.concatenate((self.buf[self.w :], self.buf[: self.w]))


class Pipeline:
    """(f) の 1 回分: 1 ホップの間引き → 特徴量 → 正規化 → 推論 → 後処理 → RPC。"""

    def __init__(self, config, hop_samples, model, link, rng):
        self.config = config
        self.model = model
        self.link = link
        self.blocks = [
            rng.normal(0.0, BLOCK_RMS, hop_samples) for _ in range(8)
        ]
        self.i = 0
        self.dec = fnp.Decimate3()
        self.ring = Ring()
        self.enter, self.exit = postprocess.HYST_DEFAULT
        self.on = {"J": False, "R": False}
        while self.ring.filled < fnp.WINDOW_SAMPLES:
            self.ring.push(self.dec.process(self._block()))

    def _block(self):
        b = self.blocks[self.i % len(self.blocks)]
        self.i += 1
        return b

    def step(self) -> None:
        self.ring.push(self.dec.process(self._block()))
        seg = self.ring.latest()
        valid = self.ring.filled >= fnp.WINDOW_SAMPLES
        p = self.model.cnn_prob(fnp.mfe40(seg))
        self.on["J"] = hyst_step(self.on["J"], p, valid, self.enter, self.exit)
        if self.config == "J＋R":
            q = self.model.lr_prob(fnp.a52(seg))
            self.on["R"] = hyst_step(
                self.on["R"], q, valid, self.enter, self.exit
            )
        if self.link is not None:
            self.link.set_state(mcu_rpc.STATE_OFF)


def measure(fn, n, warmup, clock=time.perf_counter_ns) -> dict:
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(n):
        t0 = clock()
        fn()
        samples.append(clock() - t0)
    return led_check.summarize(samples)


# --- 環境 ---


def _read(path) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "読めない"


def thread_count() -> str:
    """このプロセスのスレッド数（/proc/self/status の Threads:。読むだけ）。"""
    for line in _read("/proc/self/status").splitlines():
        if line.startswith("Threads:"):
            return line.split(":", 1)[1].strip()
    return "読めない"


def blas_name() -> str:
    try:
        cfg = np.show_config(mode="dicts")
        blas = cfg["Build Dependencies"]["blas"]
        return f"{blas.get('name', '不明')} {blas.get('version', '')}".strip()
    except Exception:  # 版によって形が違う。名前が取れなければそう書く
        return "不明"


def env_lines() -> list:
    envs = "、".join(f"{k}={os.environ.get(k, 'なし')}" for k in ENV_VARS)
    cpu = "/sys/devices/system/cpu/cpu0/cpufreq/"
    return [
        f"環境: Python {platform.python_version()}、numpy {np.__version__}、"
        f"numpy の BLAS {blas_name()}、os.cpu_count() {os.cpu_count()}",
        f"環境: {envs}",
        f"環境: cpu0 の scaling_governor {_read(cpu + 'scaling_governor')}、"
        f"scaling_cur_freq {_read(cpu + 'scaling_cur_freq')}",
    ]


# --- まとめ ---


def fmt(s) -> str:
    return (
        f"件数 {s['n']}、中央値 {s['median_ms']:.3f} ms、"
        f"95 パーセンタイル {s['p95_ms']:.3f} ms、最大 {s['max_ms']:.3f} ms"
    )


def run(link, n, warmup) -> list:
    """計測して集計の行を返す。"""
    rng = np.random.default_rng(0)
    model = Model(rng)
    t = np.arange(fnp.WINDOW_SAMPLES) / fnp.SR
    seg = 0.1 * np.sin(2 * np.pi * 700.0 * t)
    seg = seg + rng.normal(0.0, 0.05, fnp.WINDOW_SAMPLES)
    M = fnp.mfe40(seg)
    lines, parts = [], {}
    for hop_s, hop_n in HOPS:
        dec = fnp.Decimate3()
        blocks = [rng.normal(0.0, BLOCK_RMS, hop_n) for _ in range(8)]
        it = iter(range(10**9))

        def dec_once(dec=dec, blocks=blocks, it=it):
            dec.process(blocks[next(it) % len(blocks)])

        s = measure(dec_once, n, warmup)
        parts[("dec", hop_s)] = s
        lines.append(
            f"(a) 間引き {hop_s:g} 秒分（{hop_n} サンプル → "
            f"{hop_n // fnp.DECIMATE}）: {fmt(s)}"
        )
    parts["mfe40"] = measure(lambda: fnp.mfe40(seg), n, warmup)
    lines.append(
        f"(b) mfe40（16000 サンプル → 98 × 40）: {fmt(parts['mfe40'])}"
    )
    parts["cnn"] = measure(lambda: model.cnn_prob(M), n, warmup)
    lines.append(
        f"(c) CNN（正規化 ＋ cnn_forward、B = 1）: {fmt(parts['cnn'])}"
    )
    parts["lr"] = measure(lambda: model.lr_prob(fnp.a52(seg)), n, warmup)
    lines.append(f"(d) a52 ＋ 標準化 ＋ ロジスティック回帰: {fmt(parts['lr'])}")
    if link is not None:
        parts["rpc"] = measure(
            lambda: link.set_state(mcu_rpc.STATE_OFF), n, warmup
        )
        lines.append(
            "(e) RPC（zm_set_state(消灯) の往復。描画は含まない）: "
            f"{fmt(parts['rpc'])}"
        )
        tail = " → RPC"
    else:
        lines.append("(e) RPC: 省いた（--no-rpc）")
        tail = "。RPC は除く"
    sums = []
    for config in CONFIGS:
        for hop_s, hop_n in HOPS:
            pipe = Pipeline(config, hop_n, model, link, rng)
            s = measure(pipe.step, n, warmup)
            label = f"構成 {config}・ホップ {hop_s:g} 秒"
            lines.append(
                f"(f) 全体 {label}（間引き → 特徴量 → 正規化 → 推論 → "
                f"後処理{tail}）: {fmt(s)}"
            )
            lim = LIMIT_MS[hop_s]
            fits = "、".join(
                f"{name} {'収まる' if s[key] < lim else '収まらない'}"
                for name, key in (
                    ("中央値", "median_ms"),
                    ("95 パーセンタイル", "p95_ms"),
                    ("最大", "max_ms"),
                )
            )
            lines.append(f"(f) {label}: {lim:g} ms 未満か: {fits}")
            keys = [("dec", hop_s), "mfe40", "cnn"]
            if config == "J＋R":
                keys.append("lr")
            if link is not None:
                keys.append("rpc")
            med = sum(parts[k]["median_ms"] for k in keys)
            p95 = sum(parts[k]["p95_ms"] for k in keys)
            sums.append(
                f"参考（部品の和。全体の分布の値ではない）: {label} "
                f"中央値の和 {med:.3f} ms、95 パーセンタイルの和 {p95:.3f} ms"
            )
    lines.append(
        f"このプロセスのスレッド数（/proc/self/status、(f) の直後）: "
        f"{thread_count()}"
    )
    return lines + sums


def main(argv=None, open_link=mcu_rpc.McuLink.open_default, ref=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-rpc", action="store_true", help="RPC を省く（PC）")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--warmup", type=int, default=5)
    args = ap.parse_args(argv)
    if args.n < 2 or args.warmup < 0:
        ap.error("--n は 2 以上、--warmup は 0 以上")
    for line in env_lines():
        print(line)
    ok, diff = verify(load_ref() if ref is None else ref)
    print(
        f"照合: {'OK' if ok else 'NG'}（features_np_ref.npz との最大の差 "
        f"{diff:.1e}、許容 {TOL:g}。タップ・mfe40・a52・cnn_forward・"
        "ブロックの間引き）"
    )
    if not ok:
        print("照合が NG なので測らない")
        return 1
    cm = contextlib.nullcontext(None) if args.no_rpc else open_link()
    with cm as link:
        try:
            lines = run(link, args.n, args.warmup)
        finally:
            if link is not None:
                link.set_state(mcu_rpc.STATE_OFF)
    for line in lines:
        print(line)
    if link is not None:
        print("表示を消しました（確かめ）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
