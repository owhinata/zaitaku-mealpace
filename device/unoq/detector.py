"""M4 の検出器（UNO Q の Linux 側。Issue #47、plan #47 の 6.2）。

起動（UNO Q の上。tools/record.py --unoq-detector が ssh で渡す）:
  cd ~/zaitaku-mealpace-device && exec env OPENBLAS_NUM_THREADS=1 \\
      OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 python3 -u -B detector.py \\
      --live [--device <PCM 名>] [--feat] [--no-rpc] \\
      --expect-config '<M4-CONFIG>' --expect-sha256 <sha256>
  再生（一致の確かめ。PC の学習用の記録の音声を標準入力で受ける。書かない）:
      ... detector.py --replay --no-rpc [--feat] --expect-config ... --expect-sha256 ...
  照合だけ: ... detector.py --selftest --expect-config ... --expect-sha256 ...

手順: 照合（features_np_ref.npz との差 1e-5、im2col の新旧の差 1e-6）→ 重み
（m4-0038.npz）の sha256 と構成の照合（違えば消灯のまま 2 で終わる）→ 試しの推論
1 回 → H 行 → RPC を開いて消灯（--no-rpc なら開かない）→ 入力を開く → 回る。
1 窓: 48 kHz の L（ch0）→ Decimate3 → / 32768 → float32 → 16 kHz のリング
（16000 サンプル、メモリだけ）→ 4000 サンプルごとに m4_infer.Detector.step →
行をキューに入れる → zm_set_state(led)（毎窓。--no-rpc なら送らない）。
窓の結果が 1.0 秒出なければ消灯を 1 回送る。終わるとき（EOF・例外・SIGTERM・
SIGHUP・標準出力の詰まり）も消灯を送ってから RPC を閉じる。

標準出力（1 行 = 空白区切り。行を作るのは emit() だけ、書くのは writer のスレッド
だけ）:
  H <JSON>   最初に 1 回（fw・m4_config・model_sha256・code_sha256・版など）
  F <window_t_ms> <f0> … <f81>   --feat のときだけ、D 行の直前（FEAT の 82 次元）
  D <window_t_ms> <t_ms> <prob> <positive> <led>   窓ごと
  T <window_t_ms> <proc_us> <lat_us> <queue> <backlog>   窓ごと（RPC の後）
  E <JSON>   終わりに 1 回（窓の数・止まり方・時計の診断値など）
数の配列を出すのは F 行だけ（長さ 82）。波形・mfe40・CNN の中間の値は出さない。
キューの行が 2 秒分（8 窓分）を超えたら消灯して終わる（stop = stdout-stall、
終了コード 4）。標準出力が閉じたら同じ後始末で終わる（stdout-closed、5）。

時刻は検出器の起動からの ms（CLOCK_MONOTONIC）。ライブの窓 k の window_t_ms は
round(anchor + 250 k)。anchor は最初の窓を出すまでに届いたブロックの
「到着時刻 − 累計の標本数 ÷ 48 kHz」の最小（下側の包絡）。再生では anchor = 0。

終了コード: 0 EOF・シグナル、1 誤り、2 照合・構成・重みの不一致、3 overrun、
4 stdout-stall、5 stdout-closed、6 heartbeat-timeout。

ハートビート（ライブだけ。人の決定 10/29、plan #47 の追記）: record.py は標準入力に
HEARTBEAT_S（0.5 秒）ごとに 1 バイト（改行。音声ではない）を書く。検出器は
HEARTBEAT_TIMEOUT_S（2.0 秒）以上何も届かなければ消灯を送り、stop =
heartbeat-timeout（6）で終わる（UNO Q の sshd は ClientAliveInterval 0 で切断に
気づかず、stdout-stall は ssh・TCP のバッファが埋まるまで効かないため）。EOF は今まで
どおり正常の停止。再生では標準入力が音声なのでハートビートを見ない。
ファイルを書かない（open は読むモードだけ）。コアダンプを無効にする（RLIMIT_CORE 0 と
PR_SET_DUMPABLE 0。落ちたときにメモリの中身がディスクに書かれないように）。zram 以外の
スワップがあれば始めない（ライブ・再生とも。2）。音声の入力は audio_in.py だけが扱う。
"""

import os

# numpy の import より前に BLAS のスレッドを 1 つにする（docs/decisions/0040
# 決定 3。起動の文字列の env にも同じ 3 つを書く）
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"

import resource

# 落ちたときにメモリの中身（音声を含む）がコアダンプとして書かれないように
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

import audio_in  # 標準ライブラリだけ（numpy より前に読める）

# core_pattern がパイプのときも kernel がダンプを作らないように（RLIMIT_CORE は
# パイプの収集には効かない）。失敗したら main が音声の入力を開かない
DUMPS_OFF = audio_in.disable_dumps()

import argparse
import json
import queue
import select
import signal
import sys
import threading
import time
from pathlib import Path

import numpy as np

import bench_infer
import features_np as fnp
import m4_infer
import mcu_rpc

HERE = Path(__file__).resolve().parent
MODEL_PATH = HERE / "m4-0038.npz"
FW = "unoq-detector"
WINDOW = fnp.WINDOW_SAMPLES  # 16000
HOP16 = 4000  # 0.25 秒（16 kHz）
WINDOW_MS, HOP_MS = 1000, 250
IN_HZ = fnp.IN_HZ
SCALE = 32768.0
REF_TOL = 1e-5  # features_np_ref.npz との差（bench_infer.TOL と同じ）
IM2COL_TOL = 1e-6  # 0040 決定 1
STALL_WINDOWS = 8  # 2 秒分
IDLE_OFF_S = 1.0
HEARTBEAT_S = 0.5  # record.py が書く間隔（H 行に出す。検出器は使わない）
HEARTBEAT_TIMEOUT_S = 2.0
SELECT_S = 0.1
RPC_TIMEOUT_S = 0.1
RPC_RETRY_S = 1.0
WRITER_JOIN_S = 2.0
FMT = ".9g"
EXIT_OK, EXIT_ERROR, EXIT_MISMATCH, EXIT_OVERRUN = 0, 1, 2, 3
EXIT_STALL, EXIT_CLOSED, EXIT_HEARTBEAT = 4, 5, 6


class StdoutStall(Exception):
    """キューの行が 2 秒分を超えた。"""


class StopSignal(Exception):
    """SIGTERM・SIGHUP。"""


class RpcOpenError(Exception):
    """起動のときに RPC を開けない・消灯を送れない。"""


def _log(text):
    """標準エラーに短い文を 1 行（配列の値を含めない）。"""
    try:
        sys.stderr.write(f"detector: {text}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


# --- 標準出力 ---


def _num(v):
    return format(float(v), FMT)


def emit(kind, *fields):
    """1 行の文字列（改行なし）。H・E は JSON の辞書 1 つ、D・T・F は数。"""
    if kind in ("H", "E"):
        (obj,) = fields
        return f"{kind} " + json.dumps(
            obj, ensure_ascii=False, separators=(",", ":")
        )
    if kind == "D":
        w, t, prob, pos, led = fields
        return f"D {int(w)} {int(t)} {_num(prob)} {int(pos)} {int(led)}"
    if kind == "T":
        return "T " + " ".join(str(int(v)) for v in fields)
    if kind == "F":
        w, feat = fields
        if len(feat) != len(fnp.FEAT82_NAMES):
            raise ValueError("FEAT の次元が 82 ではない")
        return f"F {int(w)} " + " ".join(_num(v) for v in feat)
    raise ValueError(f"行の種類が違う: {kind}")


class Output:
    """有限のキューと writer のスレッド。put は待たない。"""

    def __init__(self, lines_per_window, stdout=None):
        self.limit = STALL_WINDOWS * lines_per_window
        self.q = queue.Queue(maxsize=self.limit + 8)
        self.failed = False
        self.qmax = 0
        self._th = threading.Thread(
            target=_writer, args=(self.q, self, stdout), daemon=True
        )
        self._th.start()

    def qsize(self):
        return self.q.qsize()

    def put(self, line, check=True):
        try:
            self.q.put_nowait(line)
        except queue.Full:
            if check:
                raise StdoutStall() from None
            return
        n = self.q.qsize()
        self.qmax = max(self.qmax, n)
        if check and n > self.limit:
            raise StdoutStall()

    def finish(self, timeout=WRITER_JOIN_S):
        """終わりの印を入れて writer を待つ。書き切れたら True。"""
        try:
            self.q.put_nowait(None)
        except queue.Full:
            return False
        self._th.join(timeout=timeout)
        return not self._th.is_alive() and not self.failed


def _writer(q, state, out=None):
    """キューから行を取り出して標準出力に書く（標準出力に書くのはここだけ）。"""
    if out is None:
        out = sys.stdout
    while True:
        line = q.get()
        if line is None:
            return
        try:
            out.write(line + "\n")
            out.flush()
        except (OSError, ValueError):
            state.failed = True  # BrokenPipeError を含む
            return


# --- 1 窓ずつの処理（入力のブロック → 窓 → 行） ---


class Pipeline:
    """ブロックを受けて窓を作り、行をキューに入れる。

    clock_ns: proc_us・lat_us の時計（既定 perf_counter_ns）。mono_ns: 行の
    t_ms の時計（既定 monotonic_ns）。t0_ns は mono_ns の起動の時刻。
    decim は Decimate3 と同じ process(x) を持つもの（テストで差し替える）。
    send_led(led) は RPC（--no-rpc なら何もしない）。

    ホップ k の proc_us = 窓 k で新しく入った 4000 サンプル（16 kHz のサンプル
    番号 [4000 k + 12000, 4000 k + 16000)）の入力処理（/ 32768・間引き・リング
    への書き込み。ブロックが 2 つのホップにまたがるときはサンプルの数の割合で
    分ける）＋ 窓 k の処理（特徴量・CNN・後処理・LED・行の投入）＋ RPC の往復。
    lat_us = 窓 k の最後のサンプルを運んだブロックの到着から T 行の投入まで。
    """

    def __init__(
        self,
        det,
        out,
        send_led,
        live,
        t0_ns,
        clock_ns=time.perf_counter_ns,
        mono_ns=time.monotonic_ns,
        decim=None,
    ):
        self.det = det
        self.out = out
        self.send_led = send_led
        self.live = live
        self.t0 = t0_ns
        self.clock = clock_ns
        self.mono = mono_ns
        self.decim = fnp.Decimate3() if decim is None else decim
        self.ring = np.zeros(WINDOW, dtype=np.float32)
        self.n16 = 0  # 16 kHz の累計のサンプル数
        self.k = 0  # 次の窓の番号
        self.frames = 0  # 48 kHz の累計のフレーム数
        self.anchor = None if live else 0.0
        self.c_first = []  # 最初の窓までのブロックの c_i（ms）
        self.c_min = self.c_max = None
        self.in_acc = 0.0  # 今のホップに割り当てた入力処理（ns）
        self.last_window_ns = None  # 最後に窓を出した mono の時刻

    def window_t_ms(self, k):
        return int(round(self.anchor + HOP_MS * k))

    def feed(self, block, arr_mono_ns, arr_perf_ns):
        c0 = self.clock()
        x = np.frombuffer(block, dtype=np.float64)
        y = (self.decim.process(x) / SCALE).astype(np.float32)
        dt = self.clock() - c0
        self.frames += len(x)
        c = (arr_mono_ns - self.t0) / 1e6 - 1000.0 * self.frames / IN_HZ
        self.c_min = c if self.c_min is None else min(self.c_min, c)
        self.c_max = c if self.c_max is None else max(self.c_max, c)
        if self.k == 0:
            self.c_first.append(c)
        m = len(y)
        if m == 0:
            self.in_acc += dt
            return
        per = dt / m
        pos = 0
        while pos < m:
            b = WINDOW + HOP16 * self.k
            take = min(m - pos, b - self.n16)
            w0 = self.clock()
            i = self.n16 % WINDOW
            first = min(take, WINDOW - i)
            self.ring[i : i + first] = y[pos : pos + first]
            if take > first:
                self.ring[: take - first] = y[pos + first : pos + take]
            dw = self.clock() - w0
            lo = max(self.n16, b - HOP16)
            hi = self.n16 + take
            if hi > lo:
                self.in_acc += (per + dw / take) * (hi - lo)
            self.n16 += take
            pos += take
            if self.n16 == b:
                self._window(arr_perf_ns, m - pos)
        del x, y

    def _window(self, arr_perf_ns, backlog):
        if self.anchor is None:
            # 最初の窓を出す時点で固定する（以後は動かさない）
            self.anchor = min(self.c_first)
        p0 = self.clock()
        i = self.n16 % WINDOW
        seg = np.concatenate((self.ring[i:], self.ring[:i]))
        w = self.window_t_ms(self.k)
        prob, pos, led, feat = self.det.step(seg, w)
        del seg
        if feat is not None:
            self.out.put(emit("F", w, feat))
        t_ms = int(round((self.mono() - self.t0) / 1e6))
        self.out.put(emit("D", w, t_ms, prob, pos, led))
        p1 = self.clock()
        self.send_led(led)
        p2 = self.clock()
        proc = self.in_acc + (p1 - p0) + (p2 - p1)
        lat = self.clock() - arr_perf_ns
        self.out.put(
            emit(
                "T",
                w,
                round(proc / 1000),
                round(lat / 1000),
                self.out.qsize(),
                backlog,
            )
        )
        self.in_acc = 0.0
        self.k += 1
        self.last_window_ns = self.mono()

    def clock_stats(self):
        if not self.c_first or self.anchor is None:
            return 0.0, 0.0, 0.0
        return (
            max(self.c_first) - min(self.c_first),
            self.c_min - self.anchor,
            self.c_max - self.anchor,
        )


# --- ハートビート ---


class Heartbeat:
    """最後に標準入力のバイトが届いてから timeout_s 秒たったか。clock は
    テストで差し替える（既定 time.monotonic）。始めた時刻から数える。"""

    def __init__(self, timeout_s, clock=time.monotonic):
        self.timeout_s = timeout_s
        self.clock = clock
        self.last = clock()

    def beat(self):
        self.last = self.clock()

    def expired(self):
        return self.clock() - self.last >= self.timeout_s


# --- RPC ---


class Rpc:
    """McuLink.set_state だけを使う。失敗は数え、次の窓で 1 回つなぎ直す
    （RPC_RETRY_S に 1 回まで）。"""

    def __init__(self, open_link):
        self.open_link = open_link
        self.link = None
        self.errors = 0
        self.next_try = 0.0
        self.shown = None

    def open(self):
        """起動のとき。開けない・消灯を送れなければ例外（始めない）。"""
        self.link = self.open_link()
        try:
            self.link.set_state(mcu_rpc.STATE_OFF)
        except BaseException:
            self._drop()
            raise
        self.shown = mcu_rpc.STATE_OFF

    def set(self, state):
        now = time.monotonic()
        if self.link is None:
            if now < self.next_try:
                return
            self.next_try = now + RPC_RETRY_S
            try:
                self.link = self.open_link()
            except (mcu_rpc.McuRpcError, OSError):
                self.errors += 1
                return
        try:
            self.link.set_state(int(state))
            self.shown = int(state)
        except (mcu_rpc.McuRpcError, OSError):
            self.errors += 1
            self._drop()
            self.next_try = now + RPC_RETRY_S

    def _drop(self):
        link, self.link = self.link, None
        if link is not None:
            try:
                link.close()
            except Exception:
                pass

    def close(self):
        """消灯を送ってから閉じる（失敗しても閉じる）。つなぎが落ちていれば
        1 回だけ開き直して送る。"""
        if self.link is None:
            try:
                self.link = self.open_link()
            except (mcu_rpc.McuRpcError, OSError):
                self.errors += 1
        if self.link is not None:
            try:
                self.link.set_state(mcu_rpc.STATE_OFF)
                self.shown = mcu_rpc.STATE_OFF
            except (mcu_rpc.McuRpcError, OSError):
                self.errors += 1
        self._drop()


def default_open_link():
    return mcu_rpc.McuLink.open_default(timeout_s=RPC_TIMEOUT_S)


# --- 照合 ---


def _conv3x3_einsum(x, W, b):
    """置き換える前の features_np._conv3x3（einsum 版）。照合だけに使う。"""
    xp = np.pad(x, ((0, 0), (0, 0), (1, 1), (1, 1)))
    cols = np.lib.stride_tricks.sliding_window_view(xp, (3, 3), axis=(2, 3))
    out = np.einsum("bchwij,ocij->bohw", cols, W, optimize=True)
    return out + b[None, :, None, None]


def _cnn_einsum(params, M):
    x = np.asarray(M, dtype=np.float32)[:, None, :, :]
    for k in range(3):
        x = _conv3x3_einsum(
            x, params[f"conv{k + 1}.weight"], params[f"conv{k + 1}.bias"]
        )
        x = np.maximum(x, 0.0)
        if k < 2:
            x = fnp._maxpool2(x)
    g = x.mean(axis=(2, 3))
    z = g @ params["fc.weight"].T[:, 0] + params["fc.bias"][0]
    return 1.0 / (1.0 + np.exp(-z.astype(np.float64)))


def im2col_diff():
    """乱数の重み（random_cnn）と乱数の入力で、新旧の cnn_forward の差の最大。"""
    worst = 0.0
    for s in range(3):
        params = fnp.random_cnn(np.random.default_rng(s))
        for B in (1, 7):
            M = (
                np.random.default_rng(100 + s)
                .normal(0, 1, (B, fnp.N_FRAMES, fnp.N_MEL_CNN))
                .astype(np.float32)
            )
            d = np.abs(fnp.cnn_forward(params, M) - _cnn_einsum(params, M))
            worst = max(worst, float(d.max()))
    return worst


def verify(expect_config, expect_sha256, model_path):
    """(model | None, 文の並び)。どれかが通らなければ model は None。"""
    lines = []
    ok_ref, ref_diff = bench_infer.verify(bench_infer.load_ref())
    lines.append(
        f"照合（features_np_ref.npz）: {'OK' if ok_ref else 'NG'}"
        f"（差の最大 {ref_diff:.2e}、許容 {REF_TOL:g}）"
    )
    d = im2col_diff()
    ok_im = d <= IM2COL_TOL
    lines.append(
        f"照合（im2col の新旧）: {'OK' if ok_im else 'NG'}"
        f"（差の最大 {d:.2e}、許容 {IM2COL_TOL:g}）"
    )
    sha = m4_infer.sha256_file(model_path)
    ok_sha = sha == expect_sha256
    lines.append(f"重みの sha256: {'一致' if ok_sha else '不一致'}（{sha}）")
    try:
        model = m4_infer.load_model(model_path)
    except (SystemExit, ValueError, OSError) as e:
        lines.append(f"重みを読めない: {type(e).__name__}")
        return None, lines
    ok_cfg = model["config"] == expect_config
    lines.append(f"構成（M4-CONFIG）: {'一致' if ok_cfg else '不一致'}")
    if not (ok_ref and ok_im and ok_sha and ok_cfg):
        return None, lines
    return model, lines


# --- 環境（読むだけ） ---


def thread_count():
    try:
        return len(os.listdir("/proc/self/task"))
    except OSError:
        return -1


def mapped_blas():
    names = set()
    try:
        with open("/proc/self/maps", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 6:
                    base = os.path.basename(parts[-1]).lower()
                    if "blas" in base or "lapack" in base:
                        names.add(os.path.basename(parts[-1]))
    except OSError:
        pass
    return ",".join(sorted(names)) or "unknown"


def code_sha256():
    """このディレクトリの .py・.npy・.npz の m4_infer.code_sha256。"""
    return m4_infer.code_sha256(
        p for p in sorted(HERE.iterdir()) if p.is_file()
    )


def header(model_sha, a, model):
    h = {
        "fw": FW,
        "m4_config": model["config"],
        "model_sha256": model_sha,
        "code_sha256": code_sha256(),
        "window_ms": WINDOW_MS,
        "hop_ms": HOP_MS,
        "input": "live" if a.live else "replay",
        "device": a.device if a.live else "stdin",
        "rpc": not a.no_rpc,
        "python": sys.version.split()[0],
        "numpy": np.__version__,
        "blas": mapped_blas(),
        "threads": thread_count(),
    }
    if a.live:
        h["heartbeat_s"] = HEARTBEAT_S
        h["heartbeat_timeout_s"] = HEARTBEAT_TIMEOUT_S
    if a.feat:
        h["feature_set"] = fnp.FEAT82_SET
        h["feature_names"] = list(fnp.FEAT82_NAMES)
    return h


# --- 本体 ---


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--replay", action="store_true")
    mode.add_argument("--selftest", action="store_true")
    ap.add_argument("--device", default=None)
    ap.add_argument("--feat", action="store_true")
    ap.add_argument("--no-rpc", action="store_true")
    ap.add_argument("--expect-config", required=True)
    ap.add_argument("--expect-sha256", required=True)
    a = ap.parse_args(argv)
    if a.device is not None and not a.live:
        ap.error("--device は --live のときだけ")
    if a.live and a.device is None:
        a.device = audio_in.DEFAULT_DEVICE
    return a


def _raise_stop(signum, frame):
    raise StopSignal()


def main(
    argv=None,
    open_link=default_open_link,
    live_argv=None,
    stdin_fd=None,
    stdout=None,
    model_path=MODEL_PATH,
    swaps_path=None,
):
    """終了コードを返す（sys.exit しない）。open_link・live_argv・stdin_fd・
    stdout・model_path・swaps_path はテストのときだけ差し替える（CLI からは
    変えられない）。"""
    t0_ns = time.monotonic_ns()
    a = parse_args(argv)
    if a.live:
        try:
            audio_in.check_device(a.device)
        except audio_in.InputError as e:
            _log(str(e))
            return EXIT_MISMATCH
    model, lines = verify(a.expect_config, a.expect_sha256, model_path)
    for ln in lines:
        _log(ln)
    if model is None:
        _log("照合が通らないので始めない（消灯のまま）")
        return EXIT_MISMATCH
    if a.selftest:
        _log("照合: OK")
        return EXIT_OK
    # 音声を受ける前の守り（adversarial-review の指摘、#47）: ダンプを止められない、
    # またはディスクのスワップがあれば始めない（消灯のまま）
    if not DUMPS_OFF:
        _log("コアダンプを止められない（prctl）ので始めない（消灯のまま）")
        return EXIT_MISMATCH
    ok_core, pat = audio_in.core_pattern_ok()
    if not ok_core:
        _log(
            "core_pattern がこの検出器の受ける形ではないので始めない（消灯のまま）: "
            f"{pat[:120] or '読めない'}"
        )
        return EXIT_MISMATCH
    swaps = audio_in.disk_swaps(swaps_path)
    if swaps is None or swaps:
        _log(
            "ディスクに出うるスワップがある（または読めない）ので始めない"
            f"（消灯のまま）: {', '.join(swaps or []) or '読めない'}"
        )
        return EXIT_MISMATCH
    model_sha = m4_infer.sha256_file(model_path)
    det = m4_infer.Detector(model, feat=a.feat)
    # 試しの推論（測らない。最初の窓の遅れを避ける。状態は進めない）
    zeros = np.zeros(WINDOW, dtype=np.float32)
    det.prob(det.features(zeros)[0])
    del zeros

    if stdin_fd is None:
        stdin_fd = sys.stdin.buffer.fileno()
    # スレッド数は writer のスレッドを起こす前に数える（BLAS のスレッドの確かめ）
    head = header(model_sha, a, model)
    out = Output(3 if a.feat else 2, stdout=stdout)
    rpc = None if a.no_rpc else Rpc(open_link)
    src = None
    stop, code = "error", EXIT_ERROR
    pipe = None
    old = {}
    if threading.current_thread() is threading.main_thread():
        for s in (signal.SIGTERM, signal.SIGHUP):
            old[s] = signal.signal(s, _raise_stop)
    try:
        out.put(emit("H", head), check=False)
        if rpc is not None:
            try:
                rpc.open()
            except (mcu_rpc.McuRpcError, OSError) as e:
                raise RpcOpenError(f"{type(e).__name__}: {e}") from None
        if a.live:
            src = audio_in.LiveInput(a.device, argv=live_argv)
        else:
            src = audio_in.ReplayInput(stdin_fd)
        send = rpc.set if rpc is not None else (lambda led: None)
        pipe = Pipeline(det, out, send, a.live, t0_ns)
        fds = [src.fileno()] + ([stdin_fd] if a.live else [])
        idle_sent = False
        hb = Heartbeat(HEARTBEAT_TIMEOUT_S) if a.live else None
        while True:
            r = select.select(fds, [], [], SELECT_S)[0]
            if out.failed:
                stop, code = "stdout-closed", EXIT_CLOSED
                break
            if a.live and stdin_fd in r:
                if not os.read(stdin_fd, 4096):
                    stop, code = "eof", EXIT_OK
                    break
                hb.beat()
            if hb is not None and hb.expired():
                _log(
                    f"ハートビートが {HEARTBEAT_TIMEOUT_S:g} 秒届かないので止める（消灯）"
                )
                stop, code = "heartbeat-timeout", EXIT_HEARTBEAT
                break
            if src.fileno() in r:
                item = src.read(None)
                if item is not None:
                    pipe.feed(*item)
                    idle_sent = False
                elif src.eof:
                    if a.live:
                        _log("入力が途中で終わった")
                        stop, code = "error", EXIT_ERROR
                    else:
                        stop, code = "eof", EXIT_OK
                    break
            if a.live and src.overrun_lines > 0:
                _log("overrun の行が来たので止める（窓の格子が崩れる）")
                stop, code = "overrun", EXIT_OVERRUN
                break
            if (
                rpc is not None
                and not idle_sent
                and pipe.last_window_ns is not None
                and time.monotonic_ns() - pipe.last_window_ns
                >= IDLE_OFF_S * 1e9
            ):
                rpc.set(mcu_rpc.STATE_OFF)
                idle_sent = True
    except StdoutStall:
        stop, code = "stdout-stall", EXIT_STALL
        _log("標準出力が詰まったので止める（消灯）")
    except (StopSignal, KeyboardInterrupt):
        stop, code = "signal", EXIT_OK
    except RpcOpenError as e:
        _log(f"RPC を開けない（始めない）: {str(e)[:200]}")
        stop, code = "error", EXIT_ERROR
    except audio_in.InputError as e:
        _log(str(e))
        stop, code = "error", EXIT_ERROR
    except Exception as e:
        _log(f"止まった: {type(e).__name__}: {str(e)[:200]}")
        stop, code = "error", EXIT_ERROR
    finally:
        # 後始末の間に届いた SIGTERM・SIGHUP で後始末を途中で抜けない
        for s in old:
            signal.signal(s, signal.SIG_IGN)
        if rpc is not None:
            rpc.close()
        cpu_child = 0.0
        overrun = 0
        if isinstance(src, audio_in.LiveInput):
            cpu_child = src.cpu_s() or 0.0
            overrun = src.overrun_lines
            src.close()
            if code == EXIT_ERROR:
                for ln in src.stderr_tail():
                    _log(f"入力の標準エラー: {ln[:200]}")
        for s, h in old.items():
            signal.signal(s, h)
    spread, dmin, dmax = pipe.clock_stats() if pipe else (0.0, 0.0, 0.0)
    tms = os.times()
    end = {
        "windows": pipe.k if pipe else 0,
        "stop": stop,
        "rpc": not a.no_rpc,
        "overrun_lines": overrun,
        "rpc_errors": rpc.errors if rpc is not None else 0,
        "anchor_spread_ms": round(spread, 3),
        "clock_drift_min_ms": round(dmin, 3),
        "clock_drift_max_ms": round(dmax, 3),
        "queue_max": out.qmax,
        "cpu_s": round(tms.user + tms.system, 3),
        audio_in.CPU_KEY: round(cpu_child, 3),
        "wall_s": round((time.monotonic_ns() - t0_ns) / 1e9, 3),
    }
    out.put(emit("E", end), check=False)
    if code != EXIT_STALL:
        out.finish()
    _log(f"終わり（{stop}）")
    return code


if __name__ == "__main__":
    rc = main()
    # 標準出力が詰まったままのとき、終了処理で writer のスレッドを待って
    # 止まらないように、ここで終える（行は書くたびに flush 済み）
    try:
        sys.stderr.flush()
    except (OSError, ValueError):
        pass
    os._exit(rc)
