"""record.py の終了後に端末が元に戻ることを pty で検証する（#16）。

実行: python -m unittest discover -s tools -v
子プロセスの stdin/stdout/stderr を pty の slave につなぎ、シリアルは偽物に差し替えて
`record.main()` を動かす。出力先は tempfile で、`data/raw/` は読み書きしない。
POSIX 以外では skip（Windows の経路は端末の設定を変えない）。
"""
from __future__ import annotations
import csv, os, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path

POSIX = os.name == "posix"
if POSIX:
    import pty, select, signal, termios

import record

TOOLS_DIR = Path(__file__).resolve().parent
EXIT_DEADLINE = 5.0     # 終了の期限（秒）
WAIT_DEADLINE = 5.0     # 出力・端末の設定を待つ期限（秒）

# 子プロセスの中身。record.serial と record.RAW_DIR を差し替えてから main() を呼ぶ。
CHILD = r'''
import os, sys, time, types
from pathlib import Path
sys.path.insert(0, os.environ["RECORD_DIR"])
import record

FAIL_AT = int(os.environ.get("FAIL_AT", "0"))
FAIL_KIND = os.environ.get("FAIL_KIND", "os")


class FakeSerial:
    """read() は少し待って空のバイト列を返すだけ。実機は要らない。"""

    def __init__(self, port, baud, timeout=0.05):
        if FAIL_KIND == "init":
            raise OSError("fake: port not found")
        self.n = 0

    def read(self, n):
        self.n += 1
        if FAIL_AT and self.n >= FAIL_AT:
            if FAIL_KIND == "exit":
                raise SystemExit(3)
            raise OSError("fake: serial gone")
        time.sleep(0.02)
        return b""

    def close(self):
        pass


record.serial = types.SimpleNamespace(Serial=FakeSerial)
record.RAW_DIR = Path(os.environ["RAW_DIR"])
record.main()
'''


class Child:
    """pty の上で record.main() を動かす子プロセス。"""

    def __init__(self, test: unittest.TestCase, args=(), env_extra=None):
        self.master, self.slave = pty.openpty()
        self.tmp = tempfile.TemporaryDirectory()
        self.raw_dir = Path(self.tmp.name)
        env = dict(os.environ)
        env.update({"RECORD_DIR": str(TOOLS_DIR), "RAW_DIR": str(self.raw_dir),
                    "SUBJECT": "", "COND": "", "POSITION": "", "BAND": "", "DURATION": ""})
        env.update(env_extra or {})
        self._out = bytearray()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.proc = subprocess.Popen(
            [sys.executable, "-c", CHILD, *args],
            stdin=self.slave, stdout=self.slave, stderr=self.slave,
            start_new_session=True, env=env, cwd=str(TOOLS_DIR.parent))
        self._drain = threading.Thread(target=self._drain_loop, daemon=True)
        self._drain.start()
        test.addCleanup(self.cleanup)

    def _drain_loop(self):
        while not self._stop.is_set():
            try:
                if not select.select([self.master], [], [], 0.1)[0]:
                    continue
                data = os.read(self.master, 4096)
            except OSError:
                return
            if not data:
                return
            with self._lock:
                self._out += data

    def cleanup(self):
        self._stop.set()
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=EXIT_DEADLINE)
        self._drain.join(timeout=1.0)
        os.close(self.master)
        os.close(self.slave)
        self.tmp.cleanup()

    # --- 端末と入出力 ---
    def lflag(self) -> tuple[bool, bool]:
        """pty の (ECHO, ICANON)。"""
        lf = termios.tcgetattr(self.slave)[3]
        return bool(lf & termios.ECHO), bool(lf & termios.ICANON)

    def output(self) -> str:
        with self._lock:
            return bytes(self._out).decode("utf-8", "replace")

    def send(self, text: str) -> None:
        os.write(self.master, text.encode("utf-8"))

    def signal(self, sig: int) -> None:
        os.kill(self.proc.pid, sig)

    # --- 待つ ---
    def wait_for(self, pred, what: str, timeout: float = WAIT_DEADLINE) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return
            if self.proc.poll() is not None and not pred():
                raise AssertionError(f"{what} を待つ間に子が終了した（出力: {self.output()!r}）")
            time.sleep(0.02)
        raise AssertionError(f"{what} を {timeout} 秒待っても起きない（出力: {self.output()!r}）")

    def wait_text(self, text: str, timeout: float = WAIT_DEADLINE) -> None:
        self.wait_for(lambda: text in self.output(), f"出力 {text!r}", timeout)

    def wait_ready(self) -> None:
        """記録が始まり、端末が cbreak になるまで待つ。"""
        self.wait_text("recording...")
        self.wait_for(lambda: self.lflag() == (False, False), "cbreak")

    def wait_note_prompt(self) -> None:
        """note の入力に入り、端末が元の設定に戻るまで待つ。"""
        self.wait_text("note: ")
        self.wait_for(lambda: self.lflag() == (True, True), "元の設定（行の編集とエコー）")

    def wait_exit(self, timeout: float = EXIT_DEADLINE) -> int:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise AssertionError(f"{timeout} 秒で終わらない（出力: {self.output()!r}）")

    # --- 記録 ---
    def events(self) -> list[list[str]]:
        """セッションの events.csv（ヘッダを除く）。"""
        dirs = [d for d in self.raw_dir.iterdir() if d.is_dir()]
        if len(dirs) != 1:
            raise AssertionError(f"セッションフォルダが1つではない: {dirs}")
        with open(dirs[0] / "events.csv", newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert rows[0] == ["t_ms", "label", "note"], rows[0]
        return rows[1:]


@unittest.skipUnless(POSIX, "POSIX の経路のテスト")
class RecordTtyTest(unittest.TestCase):
    def start(self, *args, **kwargs) -> Child:
        c = Child(self, args=args, **kwargs)
        self.before = c.lflag()
        return c

    def assertTtyRestored(self, c: Child):
        self.assertEqual(c.lflag(), self.before, f"端末が戻っていない（出力: {c.output()!r}）")

    def test_t1_duration_exit_restores_tty(self):
        c = self.start("--duration", "0.5")
        self.assertEqual(c.wait_exit(), 0, c.output())
        self.assertTtyRestored(c)

    def test_t2_sigint_restores_tty(self):
        c = self.start()
        c.wait_ready()
        c.signal(signal.SIGINT)
        self.assertEqual(c.wait_exit(), 0, c.output())
        self.assertTtyRestored(c)

    def test_t3_sigterm_restores_tty(self):
        c = self.start()
        c.wait_ready()
        c.signal(signal.SIGTERM)
        self.assertEqual(c.wait_exit(), 128 + signal.SIGTERM, c.output())
        self.assertTtyRestored(c)

    def test_t4_cbreak_while_running_and_marker(self):
        c = self.start()
        c.wait_ready()                      # 実行中は cbreak（ICANON=False）
        self.assertEqual(c.lflag(), (False, False))
        c.send("s")
        c.wait_text("[0 ms] s")
        c.signal(signal.SIGINT)
        self.assertEqual(c.wait_exit(), 0, c.output())
        self.assertTtyRestored(c)
        self.assertEqual(c.events(), [["0", "s", ""]])

    def test_t5_note_then_marker(self):
        c = self.start()
        c.wait_ready()
        c.send("o")
        c.wait_note_prompt()                # note の入力中は ECHO=True・ICANON=True
        c.send("memo\n")
        c.wait_text("[0 ms] o memo")
        c.wait_for(lambda: c.lflag() == (False, False), "cbreak に戻る")
        c.send("s")
        c.wait_text("[0 ms] s")
        c.signal(signal.SIGINT)
        self.assertEqual(c.wait_exit(), 0, c.output())
        self.assertTtyRestored(c)
        self.assertEqual(c.events(), [["0", "o", "memo"], ["0", "s", ""]])

    def test_t5b_note_and_next_key_in_one_write(self):
        """note の改行より後ろの `s` を捨てない。"""
        c = self.start()
        c.wait_ready()
        c.send("o")
        c.wait_note_prompt()
        c.send("memo\ns")
        c.wait_text("[0 ms] s")
        c.signal(signal.SIGINT)
        self.assertEqual(c.wait_exit(), 0, c.output())
        self.assertEqual(c.events(), [["0", "o", "memo"], ["0", "s", ""]])

    def test_t5c_key_note_and_next_key_in_one_write(self):
        """`o` と note と次のキーが1回で届いても、順に処理する。"""
        c = self.start()
        c.wait_ready()
        c.send("omemo\ns")
        c.wait_text("[0 ms] s")
        c.signal(signal.SIGINT)
        self.assertEqual(c.wait_exit(), 0, c.output())
        self.assertTtyRestored(c)
        self.assertEqual(c.events(), [["0", "o", "memo"], ["0", "s", ""]])

    def test_t6_unfinished_note_duration(self):
        """note を打ち終える前に --duration で終わっても、止まらず端末が戻る。"""
        c = self.start("--duration", "1.5")
        c.wait_ready()
        c.send("o")
        c.wait_note_prompt()
        self.assertEqual(c.wait_exit(), 0, c.output())
        self.assertTtyRestored(c)
        self.assertEqual(c.events(), [])

    def test_t6b_unfinished_note_sigint(self):
        c = self.start()
        c.wait_ready()
        c.send("o")
        c.wait_note_prompt()
        c.signal(signal.SIGINT)
        self.assertEqual(c.wait_exit(), 0, c.output())
        self.assertTtyRestored(c)
        self.assertEqual(c.events(), [])

    def test_t7_read_frames_raises_oserror(self):
        c = self.start(env_extra={"FAIL_AT": "3", "FAIL_KIND": "os"})
        c.wait_ready()
        self.assertEqual(c.wait_exit(), 1, c.output())
        self.assertTtyRestored(c)
        self.assertIn("fake: serial gone", c.output())

    def test_t7b_read_frames_raises_systemexit(self):
        c = self.start(env_extra={"FAIL_AT": "3", "FAIL_KIND": "exit"})
        c.wait_ready()
        self.assertEqual(c.wait_exit(), 3, c.output())
        self.assertTtyRestored(c)

    def test_t8_failure_before_tty_change(self):
        """端末を変える前に失敗したら、端末は元のまま。"""
        c = self.start(env_extra={"FAIL_KIND": "init"})
        self.assertNotEqual(c.wait_exit(), 0)
        self.assertTtyRestored(c)
        self.assertNotIn("recording...", c.output())


class MarkAfterCloseTest(unittest.TestCase):
    """close() の後の mark() は書かない（pty なし）。"""

    def test_t9_mark_after_close_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            sess = record.Session(Path(d), "self", "quiet", "pos", "band")
            sess.mark("s")
            sess.close()
            ev = sess.dir / "events.csv"
            before = ev.read_text(encoding="utf-8")
            sess.mark("t")
            sess.mark("o", "note")
            self.assertEqual(ev.read_text(encoding="utf-8"), before)
            self.assertEqual(before.splitlines()[1:], ["0,s,"])
            sess.close()        # 二度目の close() も通る


if __name__ == "__main__":
    unittest.main()
