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
#   hang: 出し終えた後に黙って待つ秒数
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
# hang: 出し終えた後、終わらずに黙っている秒数（受信の止まりの確かめ、h16）
time.sleep(cfg.get("hang", 0))
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

# --- 構成 B（a2dp）の偽の pw-dump（plan #38 第 9.1 節。アドレスは架空） ---
A2DP = ("--throat", "sh12jk-nz210c-a2dp-unoq")
BT_NODE = "bluez_input.00_11_22_33_44_55.2"
BT_NODE2 = "bluez_input.00_11_22_33_44_66.3"
SINK = "alsa_output.usb-foo.analog-stereo"
# 実物（10/10 の E5）と同じ形: コメント 1 行と入れ子の 5 行
NO_SEAT_OK = (
    "# arduino の WirePlumber の bluez の監視を seat に結ばない\n"
    "\n"
    "wireplumber.profiles = {\n"
    "  main = {\n"
    "    monitor.bluez.seat-monitoring = disabled\n"
    "  }\n"
    "}\n"
)


def pw_node(
    nid,
    name,
    mclass="Stream/Output/Audio",
    profile="a2dp-source",
    mute=False,
    rate=44100,
    driver=None,
):
    """実物（10/10 の E1）の形: bluez のノードは media.class が
    Stream/Output/Audio、api.bluez5.* はノードの props、形式は 44100 Hz。"""
    props = {"node.name": name, "media.class": mclass}
    if profile is not None:
        props["api.bluez5.profile"] = profile
        props["api.bluez5.codec"] = "sbc"
    if driver is not None:
        props["node.driver-id"] = driver
    params = {
        "Props": [{"mute": mute, "channelVolumes": [1.0, 1.0]}],
        "Format": [{"format": "S16LE", "rate": rate, "channels": 2}],
    }
    return {
        "id": nid,
        "type": "PipeWire:Interface:Node",
        "info": {"props": props, "params": params},
    }


def pw_link(lid, out, inp):
    return {
        "id": lid,
        "type": "PipeWire:Interface:Link",
        "info": {"output-node-id": out, "input-node-id": inp, "props": {}},
    }


def pw_module(mid, name):
    return {
        "id": mid,
        "type": "PipeWire:Interface:Module",
        "info": {"name": name},
    }


def pw_before(*extra) -> list:
    """起動前の pw-dump（bluez のノード → 再生側への自動リンク）。"""
    return [
        pw_module(1, "libpipewire-module-protocol-native"),
        pw_node(50, BT_NODE, driver=60),
        pw_node(60, SINK, mclass="Audio/Sink", profile=None),
        pw_link(81, 50, 60),
        *extra,
    ]


def pw_after(*extra, streams=1, src=50) -> list:
    """起動後の pw-dump（zm-throat-record のストリームが src につながる）。"""
    objs = pw_before()
    for k in range(streams):
        objs.append(
            pw_node(
                70 + k,
                record.PW_STREAM_NAME,
                mclass="Stream/Input/Audio",
                profile=None,
            )
        )
        objs.append(pw_link(90 + k, src, 70 + k))
    return objs + list(extra)


def pw_check_text(
    objs,
    pipewire_dir=False,
    wp_conf=False,
    conf_d=("90-bluez-no-seat.conf",),
    no_seat=NO_SEAT_OK,
    etc="/etc/pipewire:\npipewire.conf.d\n\n/etc/pipewire/pipewire.conf.d:\n10-qcom.conf\n",
) -> str:
    """PW_CHECK_SCRIPT と同じ区切りの出力。"""
    parts = ["@@pipewire_dir"]
    if pipewire_dir:
        parts.append("present")
    parts.append("@@wireplumber_conf")
    if wp_conf:
        parts.append("present")
    parts.append("@@conf_d")
    parts += list(conf_d)
    parts.append("@@no_seat")
    parts.append(no_seat.rstrip("\n"))
    parts.append("@@etc")
    parts.append(etc.rstrip("\n"))
    parts.append("@@pw_dump")
    parts.append(json.dumps(objs, indent=2))
    return "\n".join(parts) + "\n"


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
        pw_check: list[str] | None = None,
        pw_dump: list[str] | None = None,
        env: dict | None = None,
        no_alsa_checks: bool = False,
    ) -> tuple[int, str]:
        if throat_argv is None:
            cargv = self.child_argv(**(child or {}))

            def throat_argv(host, device, *kind):
                self.argv_seen.append((host, device, *kind))
                return cargv

        if mixer is None:
            mixer = [sys.executable, "-I", "-c", f"print({MIXER_OK!r}, end='')"]

        def mixer_argv(h, d):
            if no_alsa_checks:
                raise AssertionError("amixer を起動した")
            return mixer

        def asoundrc_check_argv(h):
            if no_alsa_checks:
                raise AssertionError("asoundrc の検査を起動した")
            return [
                sys.executable,
                "-I",
                "-c",
                f"import sys; sys.exit({asound_code})",
            ]

        def pipewire_check_argv(h):
            self.pw_check_calls += 1
            if pw_check is None:
                raise AssertionError("PipeWire の確認を起動した")
            return pw_check

        def pw_dump_argv(h):
            self.pw_dump_calls += 1
            return pw_dump if pw_dump is not None else self.pw_dump_ok()

        self.pw_check_calls = self.pw_dump_calls = 0
        environ = {k: v for k, v in os.environ.items() if k != "THROAT_DEVICE"}
        environ.update(env or {})
        argv = ["record.py", *args]
        out = io.StringIO()
        code = 0
        with (
            mock.patch.object(record, "RAW_DIR", self.raw),
            mock.patch.object(record, "THROAT_START_WAIT_S", start_wait),
            mock.patch.object(record, "throat_argv", throat_argv),
            mock.patch.object(record, "mixer_argv", mixer_argv),
            mock.patch.object(
                record, "asoundrc_check_argv", asoundrc_check_argv
            ),
            mock.patch.object(
                record, "pipewire_check_argv", pipewire_check_argv
            ),
            mock.patch.object(record, "pw_dump_argv", pw_dump_argv),
            mock.patch.dict(os.environ, environ, clear=True),
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

    # --- 構成 B（a2dp。plan #38 第 9.1 節 h12〜h17） ---
    def fake_cmd(self, text: str, code: int = 0, delay: float = 0.0):
        """text を標準出力に出して code で終わる偽の ssh（delay 秒待ってから）。"""
        self._n_fake = getattr(self, "_n_fake", 0) + 1
        p = self.tmp / f"fake_out_{self._n_fake}.txt"
        p.write_text(text, encoding="utf-8")
        src = (
            f"import sys, time; time.sleep({delay}); "
            f"sys.stdout.write(open({str(p)!r}, encoding='utf-8').read()); "
            f"sys.exit({code})"
        )
        return [sys.executable, "-I", "-c", src]

    def pw_ok(self, *extra):
        return self.fake_cmd(pw_check_text(pw_before(*extra)))

    def pw_dump_ok(self):
        return self.fake_cmd(json.dumps(pw_after()))

    def fresh_raw(self) -> None:
        """subTest ごとに出力先を分ける（フォルダ名の秒の重なりを避ける）。"""
        self._n_raw = getattr(self, "_n_raw", 0) + 1
        self.raw = self.tmp / f"raw{self._n_raw}"
        self.raw.mkdir()

    def boom(self, host, device, *kind):
        raise AssertionError("子を起動した")

    def test_h12_iface_and_device_form(self):
        import argparse

        for dev in (BT_NODE, "bluez_input.00_11_22_33_44_55.10"):
            with self.subTest(accept=dev):
                a = argparse.Namespace(
                    subject="self",
                    cond="water",
                    indicator=None,
                    throat="sh12jk-nz210c-a2dp-unoq",
                    throat_host="arduino@unoq.local",
                    throat_device=dev,
                )
                self.assertIsNone(record.throat_arg_error(a))
        bad = [
            (*A2DP, "--throat-device", "hw:CARD=Audio,DEV=0"),
            (*A2DP, "--throat-device", "plughw:0,0"),
            (*self.W, "--throat-device", BT_NODE),
            (*A2DP, "--throat-device", "bluez_output.00_11_22_33_44_55.1"),
            (*A2DP, "--throat-device", "alsa_input.usb-foo"),
            (*A2DP, "--throat-device", "bluez_input.00_11_22_33_44_55"),
            (*A2DP, "--throat-device", "bluez_input.00_11_22_33_44.2"),
            (*A2DP, "--throat-device", "bluez_input.aa_bb_cc_dd_ee_ff.2"),
            (*A2DP, "--throat-device", "bluez_input.00_11_22_33_44_55.2;rm"),
            (*A2DP, "--throat-device", "bluez_input.00_11_22_33_44_55.2 x"),
            (*A2DP, "--throat-device=-Dfoo"),
            (*A2DP, "--throat-device", "tee:SLAVE=hw:0,FILE=x"),
            (*A2DP, "--throat-device", BT_NODE, "--throat-host", "local"),
        ]
        for args in bad:
            with self.subTest(reject=args):
                code, out = self.run_main(
                    *args, throat_argv=self.boom, pw_check=self.pw_ok()
                )
                self.assertEqual(code, 1, out)
                self.assertNotIn("子を起動した", out)
                self.assertEqual(self.pw_check_calls, 0, out)
                self.assertEqual(self.sessions(), [])
        # 環境変数に hw の値が入ったまま構成 B を選ぶと形の検査で止まる
        code, out = self.run_main(
            *A2DP,
            throat_argv=self.boom,
            pw_check=self.pw_ok(),
            env={"THROAT_DEVICE": "hw:CARD=Audio,DEV=0"},
        )
        self.assertEqual(code, 1, out)
        self.assertIn("bluez_input", out)
        self.assertEqual(self.sessions(), [])

    def test_h14_pipewire_checks_stop_before_child(self):
        def replace50(**kw):
            return [
                o if o.get("id") != 50 else pw_node(50, BT_NODE, **kw)
                for o in pw_before()
            ]

        tunnel = pw_module(9, "libpipewire-module-pipe-tunnel")
        other = [
            pw_node(71, "other-rec", mclass="Stream/Input/Audio", profile=None),
            pw_link(82, 50, 71),
        ]
        no_node = [o for o in pw_before() if o.get("id") != 50]
        ok = pw_check_text(pw_before())
        cases = {
            "no-node": (
                pw_check_text(no_node),
                0,
                0.0,
                "送信機が接続されていません",
            ),
            "profile": (
                pw_check_text(replace50(profile="hfp-hf")),
                0,
                0.0,
                "a2dp-source ではありません",
            ),
            "class": (
                pw_check_text(replace50(mclass="Audio/Sink")),
                0,
                0.0,
                "ではありません: 'Audio/Sink'",
            ),
            "mute": (pw_check_text(replace50(mute=True)), 0, 0.0, "ミュート"),
            "other-capture": (
                pw_check_text(pw_before(*other)),
                0,
                0.0,
                "他の取り込み",
            ),
            "pipe-tunnel": (
                pw_check_text(pw_before(tunnel)),
                0,
                0.0,
                "pipe-tunnel",
            ),
            "pipewire-dir": (
                pw_check_text(pw_before(), pipewire_dir=True),
                0,
                0.0,
                "~/.config/pipewire",
            ),
            "wireplumber-conf": (
                pw_check_text(pw_before(), wp_conf=True),
                0,
                0.0,
                "wireplumber.conf があるので",
            ),
            "conf-d-extra": (
                pw_check_text(
                    pw_before(),
                    conf_d=("90-bluez-no-seat.conf", "99-file.conf"),
                ),
                0,
                0.0,
                "wireplumber.conf.d の中身",
            ),
            "conf-d-empty": (
                pw_check_text(pw_before(), conf_d=()),
                0,
                0.0,
                "wireplumber.conf.d の中身",
            ),
            "no-seat-extra-line": (
                pw_check_text(
                    pw_before(),
                    no_seat=NO_SEAT_OK + "context.modules = [ ]\n",
                ),
                0,
                0.0,
                "seat-monitoring を止める設定だけではない",
            ),
            # 10/9 の見込みの 1 行の形は受けない（実物の中身との一致だけ）
            "no-seat-one-line": (
                pw_check_text(
                    pw_before(),
                    no_seat="wireplumber.profiles.main.monitor.bluez"
                    ".seat-monitoring = disabled\n",
                ),
                0,
                0.0,
                "seat-monitoring を止める設定だけではない",
            ),
            "no-seat-enabled": (
                pw_check_text(
                    pw_before(),
                    no_seat=NO_SEAT_OK.replace("disabled", "enabled"),
                ),
                0,
                0.0,
                "seat-monitoring を止める設定だけではない",
            ),
            "exit-code": (ok, 255, 0.0, "終了コード 255"),
            "timeout": (ok, 0, 5.0, "タイムアウト"),
            "broken-json": (ok[:-20], 0, 0.0, "JSON が読めません"),
            "no-sections": ("[]\n", 0, 0.0, "出力の形が違う"),
        }
        for name, (text, rc, delay, msg) in cases.items():
            with self.subTest(case=name):
                with mock.patch.object(record, "THROAT_PW_TIMEOUT_S", 0.5):
                    code, out = self.run_main(
                        *A2DP,
                        "--throat-device",
                        BT_NODE,
                        throat_argv=self.boom,
                        pw_check=self.fake_cmd(text, rc, delay),
                        no_alsa_checks=True,
                    )
                self.assertEqual(code, 1, out)
                self.assertIn(msg, out)
                if name == "no-node":
                    self.assertIn("docs/unoq-setup.md", out)
                self.assertNotIn("子を起動した", out)
                self.assertEqual(self.pw_dump_calls, 0)
                self.assertEqual(self.sessions(), [])
        # コメントと空行は許す（NO_SEAT_OK に含む）。空白の違いは正規化する
        for no_seat in (
            NO_SEAT_OK,
            "wireplumber.profiles  =  {\n\tmain = {\n"
            "monitor.bluez.seat-monitoring = disabled }\n\n  }\n# c\n",
        ):
            with self.subTest(no_seat=no_seat):
                secs = record.split_pw_check(
                    pw_check_text(pw_before(), no_seat=no_seat)
                )
                self.assertIsNone(record.pipewire_config_error(secs)[0])

    def test_h14_normal_records_pipewire_meta(self):
        code, out = self.run_main(
            *A2DP,
            "--throat-device",
            BT_NODE,
            "--duration",
            "0.6",
            pw_check=self.pw_ok(),
            no_alsa_checks=True,
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(
            self.argv_seen, [("arduino@unoq.local", BT_NODE, "a2dp")]
        )
        th = self.meta(self.one_session())["throat"]
        self.assertEqual(th["device"], BT_NODE)
        self.assertEqual(th["device_source"], "arg")
        self.assertIsNone(th["mixer"])
        self.assertEqual(th["mixer_error"], "A2DP の経路ではミキサーを読まない")
        self.assertIsNone(th["overrun_lines"])
        self.assertIsNone(th["alsa_buffer_size"])
        self.assertIsNone(th["alsa_period_size"])
        self.assertEqual(th["stop"], "duration")
        pw = th["pipewire"]
        self.assertEqual(pw["profile"], "a2dp-source")
        self.assertEqual(pw["codec"], "sbc")
        self.assertEqual(pw["node_rate_hz"], 44100)
        self.assertEqual(pw["node_channels"], 2)
        self.assertEqual(pw["node_format"], "S16LE")
        self.assertIsNone(pw["codec_rate_hz"])
        self.assertTrue(pw["codec_rate_note"])
        self.assertIs(pw["mute"], False)
        self.assertEqual(pw["channel_volumes"], [1.0, 1.0])
        self.assertEqual(pw["driver"], SINK)
        self.assertEqual(pw["links"], [SINK])
        self.assertEqual(
            pw["config"]["wireplumber_conf_d"], ["90-bluez-no-seat.conf"]
        )
        self.assertIn(
            "/etc/pipewire/pipewire.conf.d/10-qcom.conf", pw["config"]["etc"]
        )
        self.assertEqual(pw["link_check"], "ok")
        self.assertEqual(pw["link_check_note"], "")
        self.assertNotIn("api.bluez5.address", json.dumps(th))
        self.assertIn("overrun なし（pw-record は出さない）", out)
        self.assertIn("0 の区間は tools/throat_check.py で確かめる", out)
        self.assertNotIn("ミキサーを読めませんでした", out)

    def test_h15b_audio_source_class_is_accepted(self):
        objs = [
            o
            if o.get("id") != 50
            else pw_node(50, BT_NODE, mclass="Audio/Source", driver=60)
            for o in pw_before()
        ]
        code, out = self.run_main(
            *A2DP,
            "--duration",
            "0.3",
            pw_check=self.fake_cmd(pw_check_text(objs)),
            no_alsa_checks=True,
        )
        self.assertEqual(code, 0, out)
        th = self.meta(self.one_session())["throat"]
        self.assertEqual((th["device"], th["device_source"]), (BT_NODE, "auto"))

    def test_h15_auto_find_node(self):
        no_node = [o for o in pw_before() if o.get("id") != 50]
        cases = [
            ("one", pw_before(), 0, None),
            ("zero", no_node, 1, "送信機が接続されていません"),
            (
                "two",
                pw_before(pw_node(51, BT_NODE2)),
                1,
                "--throat-device で指定してください",
            ),
        ]
        for name, objs, want, msg in cases:
            with self.subTest(case=name):
                self.fresh_raw()
                code, out = self.run_main(
                    *A2DP,
                    "--duration",
                    "0.3",
                    pw_check=self.fake_cmd(pw_check_text(objs)),
                    throat_argv=None if want == 0 else self.boom,
                    no_alsa_checks=True,
                )
                self.assertEqual(code, want, out)
                if want == 0:
                    th = self.meta(self.one_session())["throat"]
                    self.assertEqual(th["device"], BT_NODE)
                    self.assertEqual(th["device_source"], "auto")
                else:
                    self.assertIn(msg, out)
                    self.assertEqual(self.sessions(), [])
                    if name == "two":
                        self.assertIn(BT_NODE, out)
                        self.assertIn(BT_NODE2, out)
        # 環境変数で指したとき
        self.fresh_raw()
        code, out = self.run_main(
            *A2DP,
            "--duration",
            "0.3",
            pw_check=self.pw_ok(),
            env={"THROAT_DEVICE": BT_NODE},
            no_alsa_checks=True,
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(
            self.meta(self.one_session())["throat"]["device_source"], "env"
        )
        # 有線の iface は今までどおり hw:CARD=Audio,DEV=0（pw-dump を起動しない）
        self.fresh_raw()
        self.argv_seen = []
        code, out = self.run_main(*self.W, "--duration", "0.3")
        self.assertEqual(code, 0, out)
        self.assertEqual(
            self.argv_seen, [("arduino@unoq.local", "hw:CARD=Audio,DEV=0")]
        )
        self.assertEqual((self.pw_check_calls, self.pw_dump_calls), (0, 0))
        th = self.meta(self.one_session())["throat"]
        self.assertNotIn("device_source", th)
        self.assertNotIn("pipewire", th)
        self.assertEqual(th["overrun_lines"], 0)

    def test_h16_stall_stops_a2dp_only(self):
        # 0.5 秒分出した後、終わらずに黙る
        child = {
            "sizes": [19200],
            "interval": 0.1,
            "total": 19200 * 5,
            "hang": 30,
        }
        with mock.patch.object(record, "THROAT_STALL_S", 0.3):
            code, out = self.run_main(
                *A2DP,
                "--throat-device",
                BT_NODE,
                child=child,
                pw_check=self.pw_ok(),
            )
        self.assertEqual(code, 1, out)
        self.assertIn("stall", out)
        sd = self.one_session()
        th = self.meta(sd)["throat"]
        self.assertEqual(th["stop"], "stall")
        self.assertEqual(th["frames"], 19200 * 5 // 4)
        self.assertTrue((sd / "throat.wav").exists())
        self.assertFalse(pid_alive(self.child_pid()))
        # 有線の iface では止まらない（--duration で止まる。今の動作）
        self.fresh_raw()
        with mock.patch.object(record, "THROAT_STALL_S", 0.3):
            code, out = self.run_main(*self.W, "--duration", "1.2", child=child)
        self.assertEqual(code, 0, out)
        th = self.meta(self.one_session())["throat"]
        self.assertEqual(th["stop"], "duration")
        self.assertFalse(pid_alive(self.child_pid()))

    def test_h17_link_check_after_start(self):
        other_src = pw_node(52, "alsa_input.usb-bar", profile=None)
        cases = {
            "ok": (json.dumps(pw_after()), 0, 0.0, "ok"),
            "other-node": (
                json.dumps(pw_after(other_src, src=52)),
                0,
                0.0,
                "ng",
            ),
            "two-streams": (json.dumps(pw_after(streams=2)), 0, 0.0, "ng"),
            "zero-streams": (json.dumps(pw_after(streams=0)), 0, 0.0, "ng"),
            "pw-dump-fails": ("", 1, 0.0, "error"),
            "late": (json.dumps(pw_after()), 0, 3.0, "error"),
        }
        for name, (text, rc, delay, want) in cases.items():
            with self.subTest(case=name):
                self.fresh_raw()
                with mock.patch.object(record, "THROAT_LINK_WAIT_S", 0.5):
                    code, out = self.run_main(
                        *A2DP,
                        "--throat-device",
                        BT_NODE,
                        "--duration",
                        "0.8" if name != "late" else "0.2",
                        pw_check=self.pw_ok(),
                        pw_dump=self.fake_cmd(text, rc, delay),
                    )
                # ng でも記録は止めない
                self.assertEqual(code, 0, out)
                th = self.meta(self.one_session())["throat"]
                self.assertEqual(th["stop"], "duration")
                pw = th["pipewire"]
                self.assertEqual(pw["link_check"], want, pw)
                self.assertEqual(self.pw_dump_calls, 1)
                if want == "ok":
                    self.assertEqual(pw["link_check_note"], "")
                else:
                    self.assertTrue(pw["link_check_note"])
                if want == "ng":
                    self.assertIn("警告: 起動後のリンクの確認が ng", out)
                if name == "late":
                    self.assertIn("終わらなかった", pw["link_check_note"])


class ThroatArgvTest(unittest.TestCase):
    def test_h13_pw_record_command_is_fixed(self):
        argv = record.throat_argv("arduino@unoq.local", BT_NODE, "a2dp")
        self.assertEqual(
            argv,
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
                "exec env XDG_RUNTIME_DIR=/run/user/$(id -u) pw-record"
                " --target bluez_input.00_11_22_33_44_55.2 --rate 48000"
                " --channels 2 --format s16 -P '{ node.dont-reconnect = true"
                " node.dont-fallback = true node.name = zm-throat-record }'"
                " --raw -",
            ],
        )
        self.assertEqual(
            record.PW_PROPS,
            "{ node.dont-reconnect = true node.dont-fallback = true"
            " node.name = zm-throat-record }",
        )
        remote = argv[-1]
        self.assertEqual(remote.split()[-1], "-")
        self.assertIn("--raw", remote.split())
        for bad in (">", "tee", "|", ".wav", ".raw", "/tmp"):
            self.assertNotIn(bad, remote)
        for want in (
            "node.dont-reconnect = true",
            "node.dont-fallback = true",
            "node.name = zm-throat-record",
        ):
            self.assertIn(want, remote)
        # a2dp に local は無い
        with self.assertRaises(ValueError):
            record.throat_argv("local", BT_NODE, "a2dp")
        # 既定の kind は alsa（h10 の文字列のまま）
        self.assertEqual(
            record.throat_argv("arduino@unoq.local", "hw:CARD=Audio,DEV=0"),
            record.throat_argv(
                "arduino@unoq.local", "hw:CARD=Audio,DEV=0", "alsa"
            ),
        )
        # 確認のコマンドも読み取りだけで、ファイルに書かない
        for a in (
            record.pipewire_check_argv("arduino@unoq.local"),
            record.pw_dump_argv("arduino@unoq.local"),
        ):
            self.assertEqual(a[:11], argv[:11])
            text = a[-1].replace("2>/dev/null", "")
            for bad in (
                ">",
                "tee",
                "|",
                ".wav",
                ".raw",
                "/tmp",
                "rm ",
                "pw-record",
            ):
                self.assertNotIn(bad, text)

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
