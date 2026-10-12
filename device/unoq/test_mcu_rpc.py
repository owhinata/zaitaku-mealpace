"""device/unoq/ のテスト（Issue #38。PC で合成だけ。UNO Q には配らない）。

.venv/bin/python -m unittest discover -s device/unoq -v
"""

import ast
import contextlib
import io
import pathlib
import re
import socket
import threading
import unittest

import msgpack
import numpy as np

import led_check
import mcu_rpc

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
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
        "zm_set_led",
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
        [0, 1, "zm_set_led", [True]],
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
    def test_set_led_sends_request_and_returns(self):
        """2. 要求の形と、同じ msgid の応答で戻ること。"""
        r = FakeRouter(ok)
        try:
            link = r.link()
            self.assertIsNone(link.set_led(True))
            link.set_led(False)
        finally:
            r.close()
        self.assertEqual(
            r.requests,
            [[0, 0, "zm_set_led", [True]], [0, 1, "zm_set_led", [False]]],
        )

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
            r.link().set_led(True)
        finally:
            r.close()

    def test_error_response(self):
        """4. error が nil でなければ McuRpcError。"""
        r = FakeRouter(
            lambda req: msgpack.packb([1, req[1], "method not found", None])
        )
        try:
            with self.assertRaises(mcu_rpc.McuRpcError) as cm:
                r.link().set_led(True)
            self.assertNotIsInstance(cm.exception, mcu_rpc.McuRpcTimeout)
        finally:
            r.close()

    def test_result_not_true(self):
        r = FakeRouter(lambda req: msgpack.packb([1, req[1], None, None]))
        try:
            with self.assertRaises(mcu_rpc.McuRpcError):
                r.link().set_led(True)
        finally:
            r.close()

    def test_timeout(self):
        """4. 応答が来なければ timeout_s で McuRpcTimeout。"""
        r = FakeRouter(lambda req: None)
        try:
            with self.assertRaises(mcu_rpc.McuRpcTimeout):
                r.link(timeout_s=0.2).set_led(True)
        finally:
            r.close()

    def test_closed_socket(self):
        """4. ソケットが閉じたら例外（時間切れではない）。"""
        a, b = socket.socketpair()
        b.close()
        link = mcu_rpc.McuLink(mcu_rpc._SocketTransport(a), timeout_s=1.0)
        try:
            with self.assertRaises(mcu_rpc.McuRpcError) as cm:
                link.set_led(True)
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
                link.set_led(True)
            link.set_led(False)
        finally:
            r.close()


class FakeLink:
    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at

    def set_led(self, on):
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            self.calls.append(on)
            raise mcu_rpc.McuRpcTimeout("合成")
        self.calls.append(on)

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
        self.assertEqual(link.calls[-1], False)
        self.assertEqual(link.calls[:4], [True, False, True, False])

    def test_bench_sends_off_on_exception(self):
        link = FakeLink(fail_at=10)
        with self.assertRaises(mcu_rpc.McuRpcTimeout):
            led_check.run_bench(link, 100, 5)
        self.assertEqual(link.calls[-1], False)
        self.assertEqual(len(link.calls), 12)

    def test_cli_on_off_bench(self):
        for cmd, want_last in (
            (["on"], True),
            (["off"], False),
            (["bench", "--n", "10"], False),
        ):
            with self.subTest(cmd=cmd):
                link = FakeLink()
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = led_check.main(cmd, open_link=lambda link=link: link)
                self.assertEqual(rc, 0)
                self.assertEqual(link.calls[-1], want_last)
                text = out.getvalue()
                self.assertNotIn("判定", text)
                if cmd[0] == "on":
                    self.assertEqual(link.calls, [True])
                if cmd[0] == "bench":
                    self.assertIn("件数 10", text)


FORBIDDEN_IMPORTS = {
    "wave",
    "alsaaudio",
    "sounddevice",
    "pyaudio",
    "subprocess",
    "soundfile",
    "numpy",
}


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
        """6. 配るファイルは音声・subprocess・numpy を import せず、書き込みが無い。"""
        for name in deploy_files():
            with self.subTest(name=name):
                names, tree = imported_modules(HERE / name)
                self.assertFalse(names & FORBIDDEN_IMPORTS, names)
                for node in ast.walk(tree):
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

    def test_deploy_list(self):
        """7. DEPLOY_FILES は device/unoq/ の中の実在のファイルだけ。"""
        files = deploy_files()
        self.assertEqual(sorted(files), ["led_check.py", "mcu_rpc.py"])
        for name in files:
            with self.subTest(name=name):
                self.assertFalse(name.startswith("/"))
                self.assertNotIn("..", name)
                self.assertNotRegex(name, r"(^|/)data/")
                self.assertFalse(
                    pathlib.PurePosixPath(name).name[:5] == "test_"
                )
                p = (HERE / name).resolve()
                self.assertTrue(p.is_file())
                self.assertEqual(p.parent, HERE)

    def test_rpc_confined_to_one_module(self):
        """8. RPC は Linux 側・MCU 側とも 1 つのモジュールの中だけ。"""
        self.assertRegex(
            (FIRMWARE / "rpc_server.cpp").read_text(encoding="utf-8"),
            r"(?m)^#include <Arduino_RouterBridge\.h>",
        )
        for path in sorted(FIRMWARE.iterdir()):
            if path.name == "rpc_server.cpp":
                continue
            with self.subTest(path=path.name):
                self.assertNotRegex(
                    path.read_text(encoding="utf-8"),
                    r'(?m)^\s*#\s*include\s*[<"]Arduino_RouterBridge',
                )
        for path in sorted(HERE.glob("*.py")):
            # テストは配らない（偽の router に socketpair を使う）
            if path.name == "mcu_rpc.py" or path.name.startswith("test_"):
                continue
            with self.subTest(path=path.name):
                names, _ = imported_modules(path)
                self.assertNotIn("socket", names)
                self.assertNotIn("msgpack", names)


if __name__ == "__main__":
    unittest.main()
