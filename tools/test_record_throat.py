"""record.py の咽喉マイクだけのセッション（--throat、Issue #36 plan 第 10.1 節
h1〜h11、h10b）を偽の子プロセスで検証する。

実行: python -m unittest discover -s tools -v
偽の子 = tempdir に書く小さな Python スクリプト（FAKE_CHILD）。引数の JSON で
バイト数・刻み・標準エラーの行・途中で終わるかを受ける。record.throat_argv と
record.mixer_argv を差し替えて起動させる。実機・ssh は使わない。出力先は
tempfile（record.RAW_DIR を差し替える）で、data/raw/ は読み書きしない。
"""

from __future__ import annotations
import contextlib, csv, io, json, os, signal, subprocess, sys
import tempfile, time, unittest, wave
from unittest import mock
from pathlib import Path

import record

POSIX = os.name == "posix"

# 偽の子。argv[1] の JSON:
#   pidfile: 自分の pid を書くファイル
#   stderr_before / stderr_after: 標準エラーに出す行
#   delay: 最初のバイトの前に待つ秒数
#   sizes: 1 回の書き込みのバイト数（繰り返す）、interval: 書き込みの間の秒数
#   total: 出すバイト数（null なら止められるまで出し続ける）
#   exit_code: 出し終えた後の終了コード
# バイトの中身は通し番号 j から (j * 7 + 3) & 0xFF（pattern() と同じ）
FAKE_CHILD = r"""
import json, os, sys, time
cfg = json.loads(sys.argv[1])
if cfg.get("pidfile"):
    with open(cfg["pidfile"], "w") as f:
        f.write(str(os.getpid()))
for line in cfg.get("stderr_before", []):
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
time.sleep(cfg.get("delay", 0))
out = sys.stdout.buffer
sizes = cfg.get("sizes", [24000])
interval = cfg.get("interval", 0.01)
total = cfg.get("total")
sent = 0
i = 0
try:
    while total is None or sent < total:
        n = sizes[i % len(sizes)]
        if total is not None:
            n = min(n, total - sent)
        out.write(bytes(((sent + k) * 7 + 3) & 0xFF for k in range(n)))
        out.flush()
        sent += n
        i += 1
        time.sleep(interval)
except BrokenPipeError:
    sys.exit(141)
for line in cfg.get("stderr_after", []):
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
sys.exit(cfg.get("exit_code", 0))
"""


def pattern(n: int) -> bytes:
    return bytes((j * 7 + 3) & 0xFF for j in range(n))


def read_csv(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # ゾンビ（待たれていない）も残っていない扱いにはしない
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split()[2] != "Z"
    except OSError:
        return True


MIXER_OK = "Simple mixer control 'Mic',0\n  Front Left: Capture 58 [100%] [4.00dB] [on]\n"


@unittest.skipUnless(POSIX, "POSIX のみ")
class ThroatMainTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.raw = self.tmp / "raw"
        self.raw.mkdir()
        self.fake = self.tmp / "fake_child.py"
        self.fake.write_text(FAKE_CHILD, encoding="utf-8")
        self.pidfile = self.tmp / "child.pid"
        old_term = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, old_term)
        self.argv_seen = []

    def child_argv(self, **cfg) -> list[str]:
        cfg.setdefault("pidfile", str(self.pidfile))
        return [sys.executable, "-I", str(self.fake), json.dumps(cfg)]

    def run_main(
        self,
        *args: str,
        child: dict | None = None,
        mixer: list[str] | None = None,
        start_wait: float = 5.0,
        throat_argv=None,
        asound_code: int = 0,
    ) -> tuple[int, str]:
        if throat_argv is None:
            cargv = self.child_argv(**(child or {}))

            def throat_argv(host, device):
                self.argv_seen.append((host, device))
                return cargv

        if mixer is None:
            mixer = [sys.executable, "-I", "-c", f"print({MIXER_OK!r}, end='')"]
        argv = ["record.py", *args]
        out = io.StringIO()
        code = 0
        with (
            mock.patch.object(record, "RAW_DIR", self.raw),
            mock.patch.object(record, "THROAT_START_WAIT_S", start_wait),
            mock.patch.object(record, "throat_argv", throat_argv),
            mock.patch.object(record, "mixer_argv", lambda h, d: mixer),
            mock.patch.object(
                record,
                "asoundrc_check_argv",
                lambda h: [
                    sys.executable,
                    "-I",
                    "-c",
                    f"import sys; sys.exit({asound_code})",
                ],
            ),
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(sys, "stdin", io.StringIO()),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(out),
        ):
            try:
                record.main()
            except SystemExit as e:
                if e.code is None:
                    code = 0
                elif isinstance(e.code, int):
                    code = e.code
                else:
                    print(e.code)
                    code = 1
        return code, out.getvalue()

    def sessions(self) -> list[Path]:
        return sorted(p for p in self.raw.iterdir() if p.is_dir())

    def one_session(self) -> Path:
        dirs = self.sessions()
        self.assertEqual(len(dirs), 1, dirs)
        return dirs[0]

    def child_pid(self) -> int:
        return int(self.pidfile.read_text())

    def meta(self, sd: Path) -> dict:
        return json.loads((sd / "meta.json").read_text(encoding="utf-8"))

    def wav_bytes(self, sd: Path) -> tuple[wave._wave_params, bytes]:
        with wave.open(str(sd / "throat.wav"), "rb") as w:
            return w.getparams(), w.readframes(w.getnframes())

    W = ("--throat", "sh12jk-wired-unoq-usbaudio")

    def test_h1_files_and_meta(self):
        code, out = self.run_main(*self.W, "--cond", "quiet", "--duration", "1")
        self.assertEqual(code, 0, out)
        sd = self.one_session()
        self.assertEqual(
            {p.name for p in sd.iterdir()},
            {"throat.wav", "throat_chunks.csv", "events.csv", "meta.json"},
        )
        params, data = self.wav_bytes(sd)
        self.assertEqual(
            (params.nchannels, params.sampwidth, params.framerate),
            (2, 2, 48000),
        )
        self.assertGreater(params.nframes, 0)
        self.assertEqual(data, pattern(len(data)))
        rows = read_csv(sd / "throat_chunks.csv")
        self.assertEqual(rows[0], ["t_ms", "sample_index"])
        self.assertEqual(rows[1], ["0", "0"])
        idx = [int(r[1]) for r in rows[1:]]
        t = [int(r[0]) for r in rows[1:]]
        self.assertTrue(all(b > a for a, b in zip(idx, idx[1:])))
        self.assertTrue(all(b >= a for a, b in zip(t, t[1:])))
        self.assertEqual(
            read_csv(sd / "events.csv"), [["t_ms", "label", "note"]]
        )
        m = self.meta(sd)
        self.assertEqual(m["fw"], "pc-throat")
        self.assertEqual(m["subject"], "self")
        self.assertEqual(m["cond"], "quiet")
        self.assertEqual(
            m["sensors"],
            [
                {
                    "id": "throat",
                    "part": "SH-12JK",
                    "iface": "sh12jk-wired-unoq-usbaudio",
                }
            ],
        )
        self.assertEqual(
            m["sample_rates"], {"imu_hz": 0, "audio_hz": 48000, "analog_hz": 0}
        )
        self.assertNotIn("imu_hz", m)
        self.assertNotIn("audio_hz", m)
        th = m["throat"]
        self.assertEqual(th["frames"], params.nframes)
        self.assertEqual(th["stop"], "duration")
        self.assertEqual(th["host"], "arduino@unoq.local")
        self.assertEqual(th["device"], "hw:CARD=Audio,DEV=0")
        self.assertEqual(
            (th["format"], th["channels"], th["rate_hz"]), ("S16_LE", 2, 48000)
        )
        self.assertEqual(th["mixer"], MIXER_OK)
        self.assertNotIn("mixer_error", th)
        self.assertEqual(th["overrun_lines"], 0)
        self.assertIn("咽喉マイク: フレーム", out)
        self.assertIn("overrun の行 0", out)

    def test_h2_odd_read_sizes_keep_frames(self):
        code, out = self.run_main(
            *self.W,
            "--duration",
            "0.6",
            child={"sizes": [3, 5, 7, 1, 2, 9], "interval": 0.002},
        )
        self.assertEqual(code, 0, out)
        sd = self.one_session()
        _, data = self.wav_bytes(sd)
        self.assertEqual(len(data) % 4, 0)
        self.assertEqual(data, pattern(len(data)))
        rows = read_csv(sd / "throat_chunks.csv")[1:]
        idx = [int(r[1]) for r in rows]
        self.assertEqual(idx[0], 0)
        self.assertTrue(all(b > a for a, b in zip(idx, idx[1:])))

    def test_h3_overrun_lines_and_alsa_sizes(self):
        code, out = self.run_main(
            *self.W,
            "--duration",
            "0.8",
            child={
                "stderr_before": [
                    "Recording raw data '-' : Signed 16 bit Little Endian, Rate 48000 Hz, Stereo",
                    "  buffer_size  : 96000",
                    "  period_size  : 6000",
                ],
                "total": 48000 * 4 // 4,
                "stderr_after": [
                    "overrun!!! (at least 12.345 ms long)",
                    "overrun!!! (at least 12.345 ms long)",
                ],
                "exit_code": 0,
            },
        )
        sd = self.one_session()
        th = self.meta(sd)["throat"]
        self.assertEqual(th["overrun_lines"], 2, out)
        self.assertEqual(th["alsa_buffer_size"], 96000)
        self.assertEqual(th["alsa_period_size"], 6000)
        self.assertIn("overrun の行 2", out)
        self.assertIn("最初の overrun の行: overrun!!!", out)

    def test_h4_no_first_byte_within_wait(self):
        code, out = self.run_main(*self.W, child={"delay": 30}, start_wait=0.3)
        self.assertEqual(code, 1, out)
        self.assertIn("0.3 秒以内に届きませんでした", out)
        self.assertEqual(self.sessions(), [])
        self.assertFalse(pid_alive(self.child_pid()))

    def test_h5_child_exits_immediately(self):
        code, out = self.run_main(
            *self.W,
            child={
                "stderr_before": [
                    "arecord: main: audio open error: No such device"
                ],
                "total": 0,
                "exit_code": 1,
            },
        )
        self.assertEqual(code, 1, out)
        self.assertIn("子プロセスが最初の音声のバイトの前に終わりました", out)
        self.assertIn("No such device", out)
        self.assertEqual(self.sessions(), [])

    def test_h6_child_exits_midway(self):
        code, out = self.run_main(
            *self.W,
            child={"total": 24000 * 4, "sizes": [24000], "exit_code": 0},
        )
        self.assertEqual(code, 1, out)
        self.assertIn("警告: 子プロセスが記録の途中で終わりました", out)
        sd = self.one_session()
        th = self.meta(sd)["throat"]
        self.assertEqual(th["stop"], "child-exit")
        self.assertEqual(th["frames"], 24000)
        self.assertEqual(th["child_returncode"], 0)
        _, data = self.wav_bytes(sd)
        self.assertEqual(data, pattern(24000 * 4))

    def test_h7_duration_stops_child_group(self):
        code, out = self.run_main(*self.W, "--duration", "0.5")
        self.assertEqual(code, 0, out)
        th = self.meta(self.one_session())["throat"]
        self.assertEqual(th["stop"], "duration")
        self.assertFalse(pid_alive(self.child_pid()))
        self.assertIn("SIGTERM で止めた", out)

    def test_h9_argument_checks(self):
        bad = [
            ("--subject", "p1"),
            ("--cond", "meal"),
            ("--indicator", "/fake/indicator"),
            ("--throat", "sh12jk-unknown"),
            ("--throat-host", "local"),  # unoq の iface に local
            ("--throat", "sh12jk-wired-pc"),  # PC の iface にリモート
            ("--throat", "sh12jk-nz210c-rx-usbaudio"),
            ("--throat-host", "a;b"),
            ("--throat-host", "a b"),
            ("--throat-host", "$HOST"),
            ("--throat-host", "a/b"),
            ("--throat-host", "a>b"),
            ("--throat-device", "hw:0;rm"),
            ("--throat-device", "hw 0"),
            ("--throat-device", "$X"),
            ("--throat-device", "/dev/snd"),
            ("--throat-device", "hw>f"),
            # ALSA のファイル出力などのプラグイン・設定済みの名前は受けない（0028 決定 2）
            ("--throat-device", "tee:SLAVE=hw:0,FILE=throat.raw"),
            ("--throat-device", "file:throat.raw"),
            ("--throat-device", "default"),
            ("--throat-device", "dsnoop:CARD=Audio,DEV=0"),
            ("--throat-device", "plug:hw:0"),
            ("--throat-host=-oProxyCommand=x",),
            ("--throat-device=-Dfoo",),
        ]
        for extra in bad:
            with self.subTest(extra=extra):

                def boom(host, device):
                    raise AssertionError("子を起動した")

                # 後の --throat が前の値を置き換える
                code, out = self.run_main(*self.W, *extra, throat_argv=boom)
                self.assertEqual(code, 1, out)
                self.assertNotIn("子を起動した", out)
                self.assertEqual(self.sessions(), [])

    def test_h9c_hw_and_plughw_device_names_are_accepted(self):
        import argparse

        for dev in (
            "hw:CARD=Audio,DEV=0",
            "plughw:CARD=Audio,DEV=0",
            "hw:0,0",
            "hw:1",
        ):
            with self.subTest(device=dev):
                a = argparse.Namespace(
                    subject="self",
                    cond="water",
                    indicator=None,
                    throat="sh12jk-wired-unoq-usbaudio",
                    throat_host="arduino@unoq.local",
                    throat_device=dev,
                )
                self.assertIsNone(record.throat_arg_error(a))

    def test_h9d_alsa_user_config_stops_before_child(self):
        """~/.asoundrc か /etc/asound.conf があれば（検査が 1）、子を起動せず
        フォルダも作らない。検査が失敗（255 など）しても始めない。"""
        for rc, text in (
            (1, "ALSA のユーザー設定"),
            (255, "確かめられませんでした"),
        ):
            with self.subTest(rc=rc):

                def boom(host, device):
                    raise AssertionError("子を起動した")

                code, out = self.run_main(
                    *self.W, throat_argv=boom, asound_code=rc
                )
                self.assertEqual(code, 1, out)
                self.assertIn(text, out)
                self.assertNotIn("子を起動した", out)
                self.assertEqual(self.sessions(), [])

    def test_h9b_local_pc_iface_is_accepted(self):
        code, out = self.run_main(
            "--throat",
            "sh12jk-wired-pc",
            "--throat-host",
            "local",
            "--throat-device",
            "hw:CARD=Generic_1,DEV=0",
            "--duration",
            "0.3",
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argv_seen, [("local", "hw:CARD=Generic_1,DEV=0")])
        th = self.meta(self.one_session())["throat"]
        self.assertIsNone(th["remote_command"])

    def test_h10b_mixer_failure_or_timeout_does_not_stop(self):
        cases = {
            "fail": [sys.executable, "-I", "-c", "import sys; sys.exit(1)"],
            "timeout": [
                sys.executable,
                "-I",
                "-c",
                "import time; time.sleep(5)",
            ],
        }
        for name, cmd in cases.items():
            with self.subTest(case=name):
                for p in self.sessions():
                    for f in p.iterdir():
                        f.unlink()
                    p.rmdir()
                with mock.patch.object(record, "THROAT_MIXER_TIMEOUT_S", 0.3):
                    code, out = self.run_main(
                        *self.W, "--duration", "0.3", mixer=cmd
                    )
                self.assertEqual(code, 0, out)
                th = self.meta(self.one_session())["throat"]
                self.assertIsNone(th["mixer"])
                self.assertTrue(th["mixer_error"])
                if name == "timeout":
                    self.assertIn("タイムアウト", th["mixer_error"])
                else:
                    self.assertIn("終了コード 1", th["mixer_error"])
                time.sleep(1.05)  # フォルダ名の秒を変える

    def test_h10b_no_card_does_not_run_amixer(self):
        with mock.patch.object(
            record.subprocess, "run", side_effect=AssertionError("run")
        ) as run:
            mixer, err = record.read_mixer("local", "default")
        run.assert_not_called()
        self.assertIsNone(mixer)
        self.assertIn("CARD=", err)
        # 番号で指したカードも CARD= が無いので amixer を起動しない
        self.assertIsNone(record.device_card("hw:0,0"))
        with mock.patch.object(
            record.subprocess, "run", side_effect=AssertionError("run")
        ) as run:
            self.assertIsNone(record.read_mixer("local", "hw:0,0")[0])
        run.assert_not_called()
        with mock.patch.object(
            record.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, MIXER_OK, ""),
        ):
            self.assertEqual(
                record.read_mixer("local", "hw:CARD=Audio,DEV=0"),
                (MIXER_OK, None),
            )


class ThroatArgvTest(unittest.TestCase):
    def test_h10c_asoundrc_check_command_is_fixed(self):
        self.assertEqual(
            record.asoundrc_check_argv("arduino@unoq.local"),
            [
                *record.throat_argv("arduino@unoq.local", "hw:0")[:11],
                "exec sh -c 'test ! -e ~/.asoundrc -a ! -e /etc/asound.conf'",
            ],
        )
        self.assertEqual(
            record.asoundrc_check_argv("local"),
            ["sh", "-c", "test ! -e ~/.asoundrc -a ! -e /etc/asound.conf"],
        )

    def test_h10_command_strings_are_fixed(self):
        remote = record.throat_argv("arduino@unoq.local", "hw:CARD=Audio,DEV=0")
        self.assertEqual(
            remote,
            [
                "ssh",
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=5",
                "-o",
                "ServerAliveInterval=2",
                "-o",
                "ServerAliveCountMax=3",
                "arduino@unoq.local",
                "exec arecord -D hw:CARD=Audio,DEV=0 -f S16_LE -r 48000 -c 2 -t raw -B 2000000 -F 125000 -v -",
            ],
        )
        local = record.throat_argv("local", "hw:CARD=Generic_1,DEV=0")
        self.assertEqual(
            local,
            [
                "arecord",
                "-D",
                "hw:CARD=Generic_1,DEV=0",
                "-f",
                "S16_LE",
                "-r",
                "48000",
                "-c",
                "2",
                "-t",
                "raw",
                "-B",
                "2000000",
                "-F",
                "125000",
                "-v",
                "-",
            ],
        )
        for argv in (remote, local):
            joined = " ".join(argv)
            self.assertNotIn("-q", argv)
            self.assertNotIn(" -q", joined)
            for bad in (">", "tee", "|", ".wav", ".raw", "/tmp"):
                self.assertNotIn(bad, joined)
            self.assertEqual(joined.split()[-1], "-")
        self.assertEqual(
            record.mixer_argv("arduino@unoq.local", "hw:CARD=Audio,DEV=0"),
            [*remote[:11], "exec amixer -c Audio sget Mic"],
        )
        self.assertEqual(
            record.mixer_argv("local", "hw:CARD=Generic_1,DEV=0"),
            ["amixer", "-c", "Generic_1", "sget", "Mic"],
        )
        self.assertIsNone(record.mixer_argv("local", "default"))
        for argv in (
            record.mixer_argv("arduino@unoq.local", "hw:CARD=Audio,DEV=0"),
            record.mixer_argv("local", "hw:CARD=Generic_1,DEV=0"),
        ):
            self.assertNotIn("sset", " ".join(argv))
            self.assertNotIn("cset", " ".join(argv))


class ThroatSessionClockTest(unittest.TestCase):
    def test_h8_marker_t_ms_is_pc_clock(self):
        now = [100.0]
        with tempfile.TemporaryDirectory() as d:
            with contextlib.redirect_stdout(io.StringIO()):
                sess = record.ThroatSession(
                    Path(d),
                    "self",
                    "water",
                    "pos",
                    "band",
                    "sh12jk-wired-unoq-usbaudio",
                    100.0,
                    clock=lambda: now[0],
                )
                sess.feed(b"\x00" * 8, 100.0)
                now[0] = 101.2345
                sess.mark("s")
                now[0] = 102.5
                sess.mark("o", "tap")
                sess.close({})
                sess.mark("t")
                sess.feed(b"\x00" * 8, 103.0)
            rows = read_csv(sess.dir / "events.csv")
            self.assertEqual(
                rows,
                [
                    ["t_ms", "label", "note"],
                    ["1234", "s", ""],
                    ["2500", "o", "tap"],
                ],
            )
            self.assertEqual(sess.frames, 2)


class NoThroatTest(unittest.TestCase):
    def test_h11_default_does_not_popen(self):
        from test_record import (
            FakeDetector,
            DET_PORT,
            build_frame,
            meta_bytes,
            DET_META,
        )

        with tempfile.TemporaryDirectory() as d:
            det = FakeDetector(
                [build_frame(record.ID_META, 100, meta_bytes(**DET_META))]
            )
            out = io.StringIO()
            old_term = signal.getsignal(signal.SIGTERM)
            self.addCleanup(signal.signal, signal.SIGTERM, old_term)
            with (
                mock.patch.object(record.serial, "Serial", lambda *a, **k: det),
                mock.patch.object(record, "RAW_DIR", Path(d)),
                mock.patch.object(record, "git_sha", lambda: "test"),
                mock.patch.object(
                    record.subprocess,
                    "Popen",
                    side_effect=AssertionError("Popen"),
                ) as popen,
                mock.patch.object(
                    sys,
                    "argv",
                    ["record.py", "--port", DET_PORT, "--duration", "0.2"],
                ),
                mock.patch.object(sys, "stdin", io.StringIO()),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(out),
            ):
                record.main()
            popen.assert_not_called()
            self.assertIn("META を受けました", out.getvalue())


if __name__ == "__main__":
    unittest.main()
