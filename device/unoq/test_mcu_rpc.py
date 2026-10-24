"""device/unoq/ のテスト（Issue #38・#44・#51。PC で合成だけ。配らない）。

.venv/bin/python -m unittest discover -s device/unoq -v
"""

import ast
import contextlib
import importlib.util
import io
import os
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import msgpack
import numpy as np

import bench_infer
import bench_runtimes
import features_np
import led_check
import mcu_rpc

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
ANALYSIS = ROOT / "analysis"
FIRMWARE = ROOT / "firmware" / "unoq_mcu"


def deploy_files():
    lines = (HERE / "DEPLOY_FILES").read_text(encoding="utf-8").splitlines()
    return [ln for ln in lines if ln.strip()]


class FakeRouter:
    """socketpair の相手側をスレッドで動かす偽の router。

    respond(req) が返したバイト列をそのまま送る（None なら何も返さない）。
    """

    def __init__(self, respond, chunk=None):
        self.a, self.b = socket.socketpair()
        self.respond = respond
        self.chunk = chunk
        self.requests = []
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()

    def _run(self):
        up = msgpack.Unpacker(raw=False)
        while True:
            try:
                data = self.b.recv(4096)
            except OSError:
                return
            if not data:
                return
            up.feed(data)
            for req in up:
                self.requests.append(req)
                out = self.respond(req)
                if out is None:
                    continue
                if self.chunk:
                    for i in range(0, len(out), self.chunk):
                        self.b.sendall(out[i : i + self.chunk])
                else:
                    self.b.sendall(out)

    def link(self, timeout_s=1.0):
        return mcu_rpc.McuLink(
            mcu_rpc._SocketTransport(self.a), timeout_s=timeout_s
        )

    def close(self):
        self.b.close()
        self.a.close()
        self._th.join(timeout=2)


def ok(req):
    return msgpack.packb([1, req[1], None, True])


class TestCodec(unittest.TestCase):
    """1. 使う型の符号化・復号を msgpack と突き合わせる（双方向）。"""

    VALUES = [
        None,
        True,
        False,
        0,
        1,
        127,
        128,
        255,
        256,
        65535,
        65536,
        2**32 - 1,
        2**32,
        2**64 - 1,
        -1,
        -32,
        -33,
        -128,
        -129,
        -32768,
        -32769,
        -(2**31),
        -(2**31) - 1,
        -(2**63),
        "",
        "zm_set_state",
        "あ" * 10,
        "x" * 31,
        "x" * 32,
        "x" * 255,
        "x" * 256,
        "x" * 70000,
        b"",
        b"\x00\xff",
        b"x" * 300,
        b"x" * 70000,
        [],
        [0, 1, "zm_set_state", [2]],
        list(range(15)),
        list(range(16)),
        list(range(70000)),
        {},
        {"a": 1, "b": [None, False]},
        {i: i for i in range(16)},
        [1, 7, None, True],
    ]

    def test_pack_matches_msgpack(self):
        for v in self.VALUES:
            with self.subTest(v=repr(v)[:40]):
                self.assertEqual(mcu_rpc.packb(v), msgpack.packb(v))

    def test_roundtrip_both_ways(self):
        for v in self.VALUES:
            with self.subTest(v=repr(v)[:40]):
                got = mcu_rpc.unpackb(msgpack.packb(v))
                self.assertEqual(got, v)
                self.assertEqual(type(got), type(v))
                back = msgpack.unpackb(
                    mcu_rpc.packb(v), raw=False, strict_map_key=False
                )
                self.assertEqual(back, v)

    def test_truncated_and_unsupported(self):
        with self.assertRaises(ValueError):
            mcu_rpc.unpackb(msgpack.packb("x" * 40)[:-1])
        with self.assertRaises(mcu_rpc.McuRpcError):
            mcu_rpc.unpackb(msgpack.packb(1.5))
        with self.assertRaises(TypeError):
            mcu_rpc.packb(1.5)


class TestMcuLink(unittest.TestCase):
    def test_set_state_sends_request_and_returns(self):
        """2. 要求の形（[0, msgid, "zm_set_state", [n]]）と、同じ msgid の応答で戻ること。"""
        r = FakeRouter(ok)
        try:
            link = r.link()
            self.assertIsNone(link.set_state(mcu_rpc.STATE_WAVE))
            link.set_state(mcu_rpc.STATE_SMILE)
            link.set_state(mcu_rpc.STATE_OFF)
        finally:
            r.close()
        self.assertEqual(
            r.requests,
            [
                [0, 0, "zm_set_state", [1]],
                [0, 1, "zm_set_state", [2]],
                [0, 2, "zm_set_state", [0]],
            ],
        )
        self.assertEqual(
            (mcu_rpc.STATE_OFF, mcu_rpc.STATE_WAVE, mcu_rpc.STATE_SMILE),
            (0, 1, 2),
        )

    def test_out_of_range_is_not_sent(self):
        """範囲外（-1、3、True、1.0、None）は ValueError で、何も送らない。"""
        r = FakeRouter(ok)
        try:
            link = r.link()
            for bad in (-1, 3, 256, True, False, 1.0, None, "1"):
                with self.subTest(bad=bad):
                    with self.assertRaises(ValueError):
                        link.set_state(bad)
            link.set_state(0)  # この 1 件だけが届く
        finally:
            r.close()
        self.assertEqual(r.requests, [[0, 0, "zm_set_state", [0]]])

    def test_mcu_returns_false(self):
        """MCU が範囲外として false を返したら McuRpcError。"""
        r = FakeRouter(lambda req: msgpack.packb([1, req[1], None, False]))
        try:
            with self.assertRaises(mcu_rpc.McuRpcError) as cm:
                r.link().set_state(2)
            self.assertNotIsInstance(cm.exception, mcu_rpc.McuRpcTimeout)
        finally:
            r.close()

    def test_skips_other_msgid_and_reassembles_bytes(self):
        """3. 違う msgid と通知を読み飛ばす。1 バイトずつ届いても組み立てる。"""

        def respond(req):
            return (
                msgpack.packb([1, req[1] + 100, None, False])
                + msgpack.packb([2, "note", [1]])
                + msgpack.packb([1, req[1], None, True])
            )

        r = FakeRouter(respond, chunk=1)
        try:
            r.link().set_state(2)
        finally:
            r.close()

    def test_error_response(self):
        """4. error が nil でなければ McuRpcError。"""
        r = FakeRouter(
            lambda req: msgpack.packb([1, req[1], "method not found", None])
        )
        try:
            with self.assertRaises(mcu_rpc.McuRpcError) as cm:
                r.link().set_state(2)
            self.assertNotIsInstance(cm.exception, mcu_rpc.McuRpcTimeout)
        finally:
            r.close()

    def test_result_not_true(self):
        r = FakeRouter(lambda req: msgpack.packb([1, req[1], None, None]))
        try:
            with self.assertRaises(mcu_rpc.McuRpcError):
                r.link().set_state(2)
        finally:
            r.close()

    def test_timeout(self):
        """4. 応答が来なければ timeout_s で McuRpcTimeout。"""
        r = FakeRouter(lambda req: None)
        try:
            with self.assertRaises(mcu_rpc.McuRpcTimeout):
                r.link(timeout_s=0.2).set_state(2)
        finally:
            r.close()

    def test_closed_socket(self):
        """4. ソケットが閉じたら例外（時間切れではない）。"""
        a, b = socket.socketpair()
        b.close()
        link = mcu_rpc.McuLink(mcu_rpc._SocketTransport(a), timeout_s=1.0)
        try:
            with self.assertRaises(mcu_rpc.McuRpcError) as cm:
                link.set_state(2)
            self.assertNotIsInstance(cm.exception, mcu_rpc.McuRpcTimeout)
        except BrokenPipeError:
            pass  # 送る時点で閉じていると分かる場合
        finally:
            a.close()

    def test_late_response_after_timeout_is_skipped(self):
        """時間切れの後に遅れて来た古い応答は、次の呼び出しで読み飛ばす。"""
        held = []

        def respond(req):
            if req[1] == 0:
                held.append(req)
                return None
            return msgpack.packb([1, 0, None, True]) + ok(req)

        r = FakeRouter(respond)
        try:
            link = r.link(timeout_s=0.2)
            with self.assertRaises(mcu_rpc.McuRpcTimeout):
                link.set_state(2)
            link.set_state(0)
        finally:
            r.close()


class FakeLink:
    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at

    def set_state(self, state):
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            self.calls.append(state)
            raise mcu_rpc.McuRpcTimeout("合成")
        self.calls.append(state)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestLedCheck(unittest.TestCase):
    def test_bench_stats_and_final_off(self):
        """5. 件数 100、中央値・95 パーセンタイルが numpy と一致、最後に消灯。"""
        durations = [(i * 37 % 101 + 1) * 1000 for i in range(100)]
        ticks = []
        t = 0
        for d in durations:
            ticks += [t, t + d]
            t += d + 5
        it = iter(ticks)
        link = FakeLink()
        s = led_check.run_bench(link, 100, 5, clock=lambda: next(it))
        ms = np.array(durations) / 1e6
        self.assertEqual(s["n"], 100)
        self.assertAlmostEqual(s["median_ms"], float(np.median(ms)))
        self.assertAlmostEqual(s["p95_ms"], float(np.percentile(ms, 95)))
        self.assertAlmostEqual(s["max_ms"], float(ms.max()))
        self.assertEqual(len(link.calls), 5 + 100 + 1)
        self.assertEqual(link.calls[-1], 0)
        self.assertEqual(link.calls[:4], [2, 0, 2, 0])
        self.assertTrue(set(link.calls) <= {0, 2})

    def test_bench_sends_off_on_exception(self):
        link = FakeLink(fail_at=10)
        with self.assertRaises(mcu_rpc.McuRpcTimeout):
            led_check.run_bench(link, 100, 5)
        self.assertEqual(link.calls[-1], 0)
        self.assertEqual(len(link.calls), 12)

    def test_cli_wave_smile_off_bench(self):
        for cmd, want_calls in (
            (["wave"], [1]),
            (["smile"], [2]),
            (["off"], [0]),
            (["bench", "--n", "10"], None),
        ):
            with self.subTest(cmd=cmd):
                link = FakeLink()
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = led_check.main(cmd, open_link=lambda link=link: link)
                self.assertEqual(rc, 0)
                text = out.getvalue()
                self.assertNotIn("判定", text)
                if want_calls is not None:
                    self.assertEqual(link.calls, want_calls)
                else:
                    self.assertEqual(link.calls[-1], 0)
                    self.assertIn("件数 10", text)
                    self.assertIn("描画は含まない", text)

    def test_cli_rejects_old_names(self):
        for cmd in (["on"], ["ok"], ["wait"]):
            with self.subTest(cmd=cmd):
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    led_check.main(cmd, open_link=FakeLink)


FORBIDDEN_IMPORTS = {
    "wave",
    "alsaaudio",
    "sounddevice",
    "pyaudio",
    "subprocess",
    "soundfile",
    "scipy",
    "librosa",
    "torch",
    "sklearn",
    "threadpoolctl",
}
# numpy を import してよい配るファイル（実物に合わせる。evaluate.py は今も
# numpy を読まない）
NUMPY_ALLOWED = {
    "features_np.py",
    "bench_infer.py",
    "bench_runtimes.py",
    "postprocess.py",
}
# 推論ランタイムのモジュール（Issue #51 の計測だけ）。import してよいのは
# bench_runtimes.py だけ
RUNTIME_MODULES = {"onnxruntime", "ai_edge_litert", "pyarmnn", "ncnn"}
RUNTIME_ALLOWED = {"bench_runtimes.py"}
# ファイルを書く呼び出し（属性の名前）
WRITE_ATTRS = {
    "save",
    "savez",
    "savez_compressed",
    "savetxt",
    "tofile",
    "write_text",
    "write_bytes",
}
EXPECTED_DEPLOY = [
    "bench_infer.py",
    "bench_runtimes.py",
    "cnn_rand0.ncnn.param",
    "cnn_rand0.ncnn.weights",
    "cnn_rand0.onnx",
    "cnn_rand0.tflite",
    "evaluate.py",
    "features_np.py",
    "features_np_ref.npz",
    "led_check.py",
    "mcu_rpc.py",
    "mel26_htk_0_4000.npy",
    "mel40_htk_0_4000.npy",
    "postprocess.py",
]


def imported_modules(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names, tree


class TestStatic(unittest.TestCase):
    def test_deployed_files_no_audio_no_writes(self):
        """6. 配る .py は音声・subprocess・scipy などを import せず、書き込みが
        無い。numpy は NUMPY_ALLOWED だけ。"""
        py = [n for n in deploy_files() if n.endswith(".py")]
        self.assertEqual(len(py), 7)
        for name in py:
            with self.subTest(name=name):
                names, tree = imported_modules(HERE / name)
                self.assertFalse(names & FORBIDDEN_IMPORTS, names)
                if name not in NUMPY_ALLOWED:
                    self.assertNotIn("numpy", names)
                if name not in RUNTIME_ALLOWED:
                    self.assertFalse(names & RUNTIME_MODULES, names)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call) and isinstance(
                        node.func, ast.Attribute
                    ):
                        self.assertNotIn(
                            node.func.attr,
                            WRITE_ATTRS,
                            f"{name}: {node.func.attr}",
                        )
                    if not (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "open"
                    ):
                        continue
                    modes = [
                        a.value
                        for a in list(node.args[1:2])
                        + [k.value for k in node.keywords if k.arg == "mode"]
                        if isinstance(a, ast.Constant)
                    ]
                    self.assertFalse(
                        any(re.search("[wax+]", str(m)) for m in modes),
                        f"{name}: open の書き込み {modes}",
                    )

    def test_deployed_files_do_not_write_environment(self):
        """配る .py は環境変数を書き換えない（os.environ への代入・削除・
        setdefault・update・pop・clear、os.putenv・os.unsetenv が無い）。"""

        def is_environ(node):
            return (
                isinstance(node, ast.Attribute)
                and node.attr == "environ"
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
            )

        py = [n for n in deploy_files() if n.endswith(".py")]
        for name in py:
            with self.subTest(name=name):
                _, tree = imported_modules(HERE / name)
                for node in ast.walk(tree):
                    targets = []
                    if isinstance(node, ast.Assign):
                        targets = node.targets
                    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                        targets = [node.target]
                    elif isinstance(node, ast.Delete):
                        targets = node.targets
                    for t in targets:
                        self.assertFalse(
                            isinstance(t, ast.Subscript)
                            and is_environ(t.value),
                            f"{name}: os.environ への書き込み",
                        )
                        self.assertFalse(is_environ(t), f"{name}: os.environ")
                    if isinstance(node, ast.Call) and isinstance(
                        node.func, ast.Attribute
                    ):
                        f = node.func
                        self.assertFalse(
                            is_environ(f.value)
                            and f.attr
                            in {"setdefault", "update", "pop", "clear"},
                            f"{name}: os.environ.{f.attr}",
                        )
                        self.assertFalse(
                            isinstance(f.value, ast.Name)
                            and f.value.id == "os"
                            and f.attr in {"putenv", "unsetenv"},
                            f"{name}: os.{f.attr}",
                        )

    def test_deploy_list(self):
        """7. DEPLOY_FILES は device/unoq/ の中の実在のファイルか、analysis/ の
        直下の普通のファイルを指すリンクだけ。"""
        files = deploy_files()
        self.assertEqual(sorted(files), EXPECTED_DEPLOY)
        for name in files:
            with self.subTest(name=name):
                self.assertFalse(name.startswith("/"))
                self.assertNotIn("..", name)
                self.assertNotIn("/", name)
                self.assertNotRegex(name, r"(^|/)data/")
                self.assertFalse(
                    pathlib.PurePosixPath(name).name[:5] == "test_"
                )
                link = HERE / name
                p = link.resolve()
                self.assertTrue(p.is_file())
                self.assertFalse(p.is_symlink())
                self.assertNotIn("data", p.relative_to(ROOT).parts)
                self.assertFalse(p.name.startswith("test_"))
                if link.is_symlink():
                    self.assertFalse(os.path.isabs(os.readlink(link)))
                    self.assertEqual(p.parent, ANALYSIS)
                    self.assertEqual(p.name, name)
                else:
                    self.assertEqual(p.parent, HERE)

    def test_rpc_confined_to_one_module(self):
        """8. RPC は Linux 側・MCU 側とも 1 つのモジュールの中だけ。"""
        self.assertRegex(
            (FIRMWARE / "rpc_server.cpp").read_text(encoding="utf-8"),
            r"(?m)^#include <Arduino_RouterBridge\.h>",
        )
        sources = sorted(p for p in FIRMWARE.rglob("*") if p.is_file())
        names = {p.relative_to(FIRMWARE).as_posix() for p in sources}
        # リンク（→ indicator・detector）も読めること
        for link in ("shapes.h", "shape_seq.h", "shape_seq.cpp", "led_rule.h"):
            self.assertIn(link, names)
            self.assertTrue((FIRMWARE / link).is_symlink())
        self.assertIn("host/test_unoq_mcu.cpp", names)
        self.assertNotIn("shapes_unoq.h", names)
        for path in sources:
            rel = path.relative_to(FIRMWARE).as_posix()
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=rel):
                if rel != "rpc_server.cpp":
                    self.assertNotRegex(
                        text, r'(?m)^\s*#\s*include\s*[<"]Arduino_RouterBridge'
                    )
                if rel != "led_out.cpp":
                    self.assertNotRegex(
                        text, r'(?m)^\s*#\s*include\s*[<"]Arduino_LED_Matrix'
                    )
                if rel.startswith("matrix_frame"):
                    self.assertNotRegex(
                        text, r'(?m)^\s*#\s*include\s*[<"]Arduino\.h'
                    )
        for path in sorted(HERE.glob("*.py")):
            # テストは配らない（偽の router に socketpair を使う）
            if path.name == "mcu_rpc.py" or path.name.startswith("test_"):
                continue
            with self.subTest(path=path.name):
                names, _ = imported_modules(path)
                self.assertNotIn("socket", names)
                self.assertNotIn("msgpack", names)


def snapshot(*dirs):
    """ディレクトリの下のすべてのパス（__pycache__ を含む）。"""
    return {p for d in dirs for p in pathlib.Path(d).rglob("*")}


COMBOS = [(c, h) for c in bench_infer.CONFIGS for h, _ in bench_infer.HOPS]


class TestBenchInfer(unittest.TestCase):
    """9. bench_infer.py（Issue #44）。合成入力だけ。ファイルを書かない。"""

    MAX_LINES = 30

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = pathlib.Path(self._tmp.name)

    def check_output(self, text):
        lines = text.splitlines()
        self.assertLessEqual(len(lines), self.MAX_LINES)
        self.assertIn("照合: OK", text)
        self.assertNotIn("判定", text)
        for c, h in COMBOS:
            label = f"構成 {c}・ホップ {h:g} 秒"
            self.assertEqual(
                sum(ln.startswith(f"(f) 全体 {label}（") for ln in lines), 1
            )
            lim = bench_infer.LIMIT_MS[h]
            self.assertEqual(
                sum(
                    ln.startswith(f"(f) {label}: {lim:g} ms 未満か: ")
                    for ln in lines
                ),
                1,
            )
            self.assertEqual(
                sum(
                    f"部品の和。全体の分布の値ではない）: {label} " in ln
                    for ln in lines
                ),
                1,
            )
        return lines

    def test_subprocess_no_rpc_writes_nothing(self):
        before = snapshot(HERE, ANALYSIS, self.work)
        r = subprocess.run(
            [
                sys.executable,
                "-B",
                str(HERE / "bench_infer.py"),
                "--no-rpc",
                "--n",
                "3",
                "--warmup",
                "0",
            ],
            cwd=self.work,
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = self.check_output(r.stdout)
        self.assertIn("(e) RPC: 省いた（--no-rpc）", lines)
        self.assertTrue(any("件数 3、" in ln for ln in lines))
        self.assertEqual(snapshot(HERE, ANALYSIS, self.work), before)

    def test_fake_link_sends_only_off_and_writes_nothing(self):
        before = snapshot(HERE, ANALYSIS, self.work)
        link = FakeLink()
        out = io.StringIO()
        with contextlib.chdir(self.work), contextlib.redirect_stdout(out):
            rc = bench_infer.main(
                ["--n", "3", "--warmup", "0"], open_link=lambda: link
            )
        self.assertEqual(rc, 0)
        lines = self.check_output(out.getvalue())
        self.assertTrue(any(ln.startswith("(e) RPC（") for ln in lines))
        # (e) 3 回、(f) 4 つの組み合わせ × 3 回、最後の消灯 1 回
        self.assertEqual(len(link.calls), 3 + 4 * 3 + 1)
        self.assertEqual(set(link.calls), {mcu_rpc.STATE_OFF})
        self.assertEqual(snapshot(HERE, ANALYSIS, self.work), before)

    def test_ng_returns_1_without_measuring(self):
        ref = bench_infer.load_ref()
        broken = dict(ref, mfe40=ref["mfe40"] + 1.0)
        missing = {k: v for k, v in ref.items() if k != "a52"}
        reshaped = dict(ref, cnn=np.zeros(2))
        for name, bad in (
            ("壊した値", broken),
            ("項目が無い", missing),
            ("形が違う", reshaped),
        ):
            with self.subTest(name):
                opened = []
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = bench_infer.main(
                        ["--n", "3", "--warmup", "0"],
                        open_link=lambda: opened.append(1) or FakeLink(),
                        ref=bad,
                    )
                self.assertEqual(rc, 1)
                text = out.getvalue()
                self.assertIn("照合: NG", text)
                self.assertNotIn("中央値", text)
                self.assertEqual(opened, [])

    def test_verify_ok_on_pc(self):
        ok, diff = bench_infer.verify(bench_infer.load_ref())
        self.assertTrue(ok)
        self.assertLessEqual(diff, 1e-9)

    def test_ring_and_hysteresis_step(self):
        ring = bench_infer.Ring()
        x = np.arange(40000, dtype=float)
        for a, b in ((0, 4000), (4000, 5600), (5600, 17000), (17000, 40000)):
            ring.push(x[a:b])
            got = ring.latest()
            want = np.concatenate((np.zeros(16000), x[:b]))[-16000:]
            np.testing.assert_array_equal(got, want)
        # postprocess.hysteresis と同じ規則
        import postprocess

        prob = [0.1, 0.75, 0.5, 0.35, 0.8, 0.9, 0.2]
        valid = [True, True, True, True, False, True, True]
        t = [0.25 * k for k in range(len(prob))]
        want = postprocess.hysteresis(prob, valid, 0.7, 0.4, t)
        on, got = False, []
        for p, v in zip(prob, valid):
            on = bench_infer.hyst_step(on, p, v, 0.7, 0.4)
            got.append(on)
        self.assertEqual(got, list(want))


RT_ONLY = [
    "numpy",
    "numpy-einsum-noopt",
    "numpy-im2col",
    "ort-cpu",
    "litert-cpu",
    "ncnn-cpu",
    "armnn-cpuacc",
]
NUMPY_BACKENDS = RT_ONLY[:3]
RT_MODULE = {
    "ort-cpu": "onnxruntime",
    "litert-cpu": "ai_edge_litert",
    "ncnn-cpu": "ncnn",
    "armnn-cpuacc": "pyarmnn",
}


def installed(mod):
    return importlib.util.find_spec(mod) is not None


class FakeGpu(bench_runtimes.Backend):
    """少し眠ってから numpy の cnn_forward を返す偽の GPU の経路。"""

    gpu = True

    def __init__(self, name, model, bias=0.0):
        super().__init__(name, model)
        self.bias = bias

    def env(self):
        return ["偽の GPU（テスト）"]

    def run(self, Z):
        time.sleep(0.001)
        return features_np.cnn_forward(self.model.cnn, Z) + self.bias


class TestBenchRuntimes(unittest.TestCase):
    """10. bench_runtimes.py（Issue #51）。合成入力だけ。ファイルを書かない。"""

    LINES_PER_BACKEND = 20

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = pathlib.Path(self._tmp.name)

    def common(self, text, n_backends):
        lines = text.splitlines()
        self.assertLessEqual(
            len(lines), 12 + self.LINES_PER_BACKEND * n_backends
        )
        self.assertIn("照合（#44）: OK", text)
        self.assertNotIn("判定", text)
        return lines

    def check_measured(self, lines, name):
        p = f"[{name}] "
        self.assertIn(f"{p}照合: OK", "\n".join(lines))
        want = [
            f"{p}1 回目の実行（",
            f"{p}(c) CNN 1 窓（",
            f"{p}(c) CPU 時間: ",
        ]
        for h, _ in bench_infer.HOPS:
            label = f"構成 J・ホップ {h:g} 秒"
            lim = bench_infer.LIMIT_MS[h]
            want += [
                f"{p}(f) 全体 {label}（",
                f"{p}(f) 全体 {label} CPU 時間: ",
                f"{p}(f) {label}: {lim:g} ms 未満か: ",
                f"{p}持続 {label}・",
            ]
        want.append(f"{p}このプロセスのスレッド数")
        for w in want:
            with self.subTest(name=name, line=w):
                self.assertEqual(sum(ln.startswith(w) for ln in lines), 1)

    def test_subprocess_no_rpc_writes_nothing(self):
        before = snapshot(HERE, ANALYSIS, self.work)
        r = subprocess.run(
            [
                sys.executable,
                "-B",
                str(HERE / "bench_runtimes.py"),
                "--no-rpc",
                "--n",
                "3",
                "--warmup",
                "0",
                "--sustain-s",
                "1",
                "--only",
                ",".join(RT_ONLY),
            ],
            cwd=self.work,
            capture_output=True,
            text=True,
            timeout=600,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = self.common(r.stdout, len(RT_ONLY))
        for name in NUMPY_BACKENDS:
            self.check_measured(lines, name)
            self.assertIn(f"[{name}] 照合（PC の参照）: OK", r.stdout)
        for name, mod in RT_MODULE.items():
            with self.subTest(name=name):
                if installed(mod):
                    self.check_measured(lines, name)
                else:
                    self.assertIn(f"[{name}] 未導入（{mod}）", lines)
        if not installed("pyarmnn"):
            self.assertIn("[armnn-cpuacc] 未導入（pyarmnn）", lines)
        self.assertEqual(snapshot(HERE, ANALYSIS, self.work), before)

    def test_list_does_not_measure(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = bench_runtimes.main(["--list"])
        self.assertEqual(rc, 0)
        names = out.getvalue().split()
        self.assertEqual(names, list(bench_runtimes.default_backends()))
        self.assertNotIn(bench_runtimes.IDLE, names)
        self.assertEqual(len(names), 14)
        self.assertIn("litert-gpu-fp32", names)

    def test_overlap_with_fake_gpu_and_fake_link(self):
        before = snapshot(HERE, ANALYSIS, self.work)
        link = FakeLink()
        out = io.StringIO()
        with contextlib.chdir(self.work), contextlib.redirect_stdout(out):
            rc = bench_runtimes.main(
                [
                    "--n",
                    "3",
                    "--warmup",
                    "0",
                    "--sustain-s",
                    "0",
                    "--overlap",
                    "--only",
                    "fake-gpu",
                ],
                open_link=lambda: link,
                backends={"fake-gpu": lambda m: FakeGpu("fake-gpu", m)},
            )
        self.assertEqual(rc, 0)
        lines = self.common(out.getvalue(), 2)
        p = "[fake-gpu] "
        self.assertEqual(
            sum(
                ln.startswith(f"{p}重ね合わせ: 非同期の API なし")
                for ln in lines
            ),
            1,
        )
        for h, _ in bench_infer.HOPS:
            tag = f"{p}重ね合わせ ホップ {h:g} 秒（"
            for form in (
                "直列 壁時計",
                "直列 CPU 時間",
                "スレッドで投入 壁時計",
            ):
                with self.subTest(hop=h, form=form):
                    self.assertEqual(
                        sum(
                            ln.startswith(tag) and f"）{form}: 件数 3、" in ln
                            for ln in lines
                        ),
                        1,
                    )
            self.assertEqual(
                sum(
                    ln.startswith(f"{p}重ね合わせ ホップ {h:g} 秒: 中央値の差")
                    for ln in lines
                ),
                1,
            )
        self.assertIn(f"{p}持続: 省いた（--sustain-s 0）", lines)
        # (f) 2 つのホップ × 3 回、最後の消灯 1 回。消灯だけを送る
        self.assertEqual(len(link.calls), 2 * 3 + 1)
        self.assertEqual(set(link.calls), {mcu_rpc.STATE_OFF})
        self.assertEqual(snapshot(HERE, ANALYSIS, self.work), before)

    def test_ng_returns_1_without_measuring(self):
        ref = bench_infer.load_ref()
        broken = dict(ref, cnn=ref["cnn"] + 0.5)
        opened = []
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = bench_runtimes.main(
                ["--n", "3", "--warmup", "0", "--sustain-s", "0"],
                open_link=lambda: opened.append(1) or FakeLink(),
                ref=broken,
            )
        self.assertEqual(rc, 1)
        text = out.getvalue()
        self.assertIn("照合（#44）: NG", text)
        self.assertNotIn("中央値", text)
        self.assertFalse(any(ln.startswith("[") for ln in text.splitlines()))
        self.assertEqual(opened, [])

    def test_backend_ng_is_not_measured(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = bench_runtimes.main(
                ["--no-rpc", "--n", "3", "--warmup", "0", "--overlap"],
                backends={"bad": lambda m: FakeGpu("bad", m, bias=0.01)},
            )
        self.assertEqual(rc, 0)
        lines = out.getvalue().splitlines()
        self.assertTrue(any(ln.startswith("[bad] 照合: NG") for ln in lines))
        self.assertIn("[bad] 照合が NG なので測らない", lines)
        self.assertFalse(any("[bad] 1 回目" in ln for ln in lines))
        self.assertFalse(any("中央値" in ln for ln in lines))


if __name__ == "__main__":
    unittest.main()
