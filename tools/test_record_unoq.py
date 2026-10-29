"""record.py --unoq-detector（Issue #47、plan #47 の 7 節）と
unoq_session_check.py（9 節）を、偽の ssh の子と合成のフォルダで検証する。

実行: python -m unittest discover -s tools -v
偽の子 = tempdir に書く小さな Python スクリプト（FAKE_DET）。H・F・D・T・E の行を
出し、標準入力の EOF で E を出して終わる。往復時間の cat・消灯（led_check.py off）・
起動前の確かめも偽のコマンドに差し替える。実機・ssh は使わない。出力先は
tempfile（record.RAW_DIR を差し替える）で、data/raw/ は読み書きしない。
"""

from __future__ import annotations
import contextlib, csv, io, json, os, signal, sys, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import record
import unoq_session_check

POSIX = os.name == "posix"

# 偽の検出器。argv[1] の JSON:
#   pidfile、header（H の辞書。null なら H を出さない）、pre・after_h（H の前・
#   後の行）、n（窓の数。null なら標準入力の EOF まで）、interval、feat、
#   lag（t_ms − window_t_ms − 1000）、bad（窓の番号 → その後に出す行）、
#   pause（窓の番号 → その前に黙る秒数）、end（E を出すか）、exit_code、
#   wait_eof（窓を出し終えた後、標準入力の EOF を待つか）
FAKE_DET = r"""
import json, os, sys, threading, time
cfg = json.loads(sys.argv[1])
if cfg.get("pidfile"):
    with open(cfg["pidfile"], "w") as f:
        f.write(str(os.getpid()))
eof = threading.Event()
def rd():
    log = cfg.get("stdin_log")
    while True:
        try:
            d = os.read(0, 4096)
        except OSError:
            d = b""
        if not d:
            eof.set()
            return
        if log:
            with open(log, "a") as f:
                f.write("%r %d\n" % (time.monotonic(), len(d)))
threading.Thread(target=rd, daemon=True).start()
def w(line):
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
time.sleep(cfg.get("delay", 0))
for ln in cfg.get("pre", []):
    w(ln)
if cfg.get("header") is not None:
    w("H " + json.dumps(cfg["header"]))
for ln in cfg.get("after_h", []):
    w(ln)
time.sleep(cfg.get("first_delay", 0.05))
n = cfg.get("n")
k = 0
while (n is None or k < n) and not eof.is_set():
    time.sleep(cfg.get("pause", {}).get(str(k), 0))
    wms = 250 * k + cfg.get("w0", 0)
    if cfg.get("feat"):
        w("F %d " % wms + " ".join(["4321.5678"] * 82))
    w("D %d %d 0.777777777 %d %d" % (wms, wms + 1000 + cfg.get("lag", 30), k % 2, 1 + k % 2))
    w("T %d 3000 4000 1 0" % wms)
    for ln in cfg.get("bad", {}).get(str(k), []):
        w(ln)
    k += 1
    time.sleep(cfg.get("interval", 0.01))
if cfg.get("wait_eof", True):
    eof.wait(cfg.get("eof_wait", 20))
if cfg.get("end", True):
    w("E " + json.dumps({"windows": k, "stop": "eof", "rpc": False,
        "overrun_lines": 0, "rpc_errors": 0, "anchor_spread_ms": 1.5,
        "clock_drift_min_ms": -0.5, "clock_drift_max_ms": 2.0,
        "queue_max": 2, "cpu_s": 0.1, "arecord_cpu_s": 0.01, "wall_s": 1.0}))
sys.exit(cfg.get("exit_code", 0))
"""

ECHO = (
    "import sys\n"
    "for line in sys.stdin:\n"
    "    sys.stdout.write(line)\n"
    "    sys.stdout.flush()\n"
)
MIXER_OK = "Simple mixer control 'Mic',0\n  Front Left: Capture 58 [100%]\n"


def py(code: str) -> list[str]:
    return [sys.executable, "-I", "-c", code]


def read_csv(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def good_header(feat=False, no_rpc=False) -> dict:
    exp = record.expected_unoq()
    h = {
        "fw": "unoq-detector",
        "m4_config": exp["m4_config"],
        "model_sha256": exp["model"]["sha256"],
        "code_sha256": exp["code_sha256"],
        "window_ms": 1000,
        "hop_ms": 250,
        "input": "live",
        "device": "hw:CARD=Audio,DEV=0",
        "heartbeat_s": 0.5,
        "heartbeat_timeout_s": 2.0,
        "rpc": not no_rpc,
        "numpy": "2.2.4",
        "blas": "libopenblas.so.0",
        "threads": 1,
    }
    if feat:
        h["feature_set"] = exp["feature_set"]
        h["feature_names"] = exp["feature_names"]
    return h


class UnoqMainTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.raw = self.tmp / "raw"
        self.raw.mkdir()
        self.fake = self.tmp / "fake_det.py"
        self.fake.write_text(FAKE_DET, encoding="utf-8")
        self.pidfile = self.tmp / "det.pid"
        old_term = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, old_term)
        self.calls = []

    def run_main(
        self,
        *args,
        det=None,
        led_code=0,
        confirm=True,
        busy_code=1,
        swaps="Filename\tType\tSize\tUsed\tPriority\n",
        rtt=ECHO,
        header_wait=5.0,
        detector_argv=None,
    ):
        det = dict(det or {})
        det.setdefault("pidfile", str(self.pidfile))
        if "header" not in det:
            det["header"] = good_header(
                feat="--feat" in args, no_rpc="--no-rpc" in args
            )

        def unoq_detector_argv(host, dargs):
            self.calls.append(("detector", host, list(dargs)))
            if detector_argv is not None:
                return detector_argv
            return [sys.executable, "-I", str(self.fake), json.dumps(det)]

        def led_off_argv(host):
            self.calls.append(("led_off", host))
            return py(f"import sys; sys.exit({led_code})")

        def rtt_argv(host):
            self.calls.append(("rtt", host))
            return py(rtt)

        def confirm_matrix_off():
            self.calls.append(("confirm",))
            return confirm

        argv = ["record.py", "--unoq-detector", *args]
        out = io.StringIO()
        code = 0
        with (
            mock.patch.object(record, "RAW_DIR", self.raw),
            mock.patch.object(record, "UNOQ_HEADER_WAIT_S", header_wait),
            mock.patch.object(record, "unoq_detector_argv", unoq_detector_argv),
            mock.patch.object(record, "led_off_argv", led_off_argv),
            mock.patch.object(record, "rtt_argv", rtt_argv),
            mock.patch.object(
                record,
                "busy_check_argv",
                lambda h: py(f"import sys; sys.exit({busy_code})"),
            ),
            mock.patch.object(
                record,
                "swap_check_argv",
                lambda h: py(f"print({swaps!r}, end='')"),
            ),
            mock.patch.object(
                record, "asoundrc_check_argv", lambda h: py("pass")
            ),
            mock.patch.object(
                record, "mixer_argv", lambda h, d: py(f"print({MIXER_OK!r})")
            ),
            mock.patch.object(record, "confirm_matrix_off", confirm_matrix_off),
            mock.patch.dict(os.environ, {"PORT": ""}),
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

    def sessions(self):
        return sorted(p for p in self.raw.iterdir() if p.is_dir())

    def one(self):
        d = self.sessions()
        self.assertEqual(len(d), 1, d)
        return d[0]

    def kinds(self):
        return [c[0] for c in self.calls]

    def test_u1_files_meta_and_no_values_shown(self):
        code, out = self.run_main(
            "--cond", "water", "--feat", "--duration", "1.0", det={"feat": True}
        )
        self.assertEqual(code, 0, out)
        sd = self.one()
        self.assertEqual(
            sorted(p.name for p in sd.iterdir()),
            sorted(
                [
                    "detect.csv",
                    "feat.csv",
                    "detect_rx.csv",
                    "events.csv",
                    "meta.json",
                ]
            ),
        )
        det = read_csv(sd / "detect.csv")
        self.assertEqual(
            det[0], ["t_ms", "window_t_ms", "positive", "prob", "led"]
        )
        self.assertGreater(len(det), 5)
        for r in det[1:]:
            self.assertEqual(r[3], "0.777777777")
            self.assertEqual(int(r[0]), int(r[1]) + 1030)
        feat = read_csv(sd / "feat.csv")
        self.assertEqual(feat[0][:3], ["t_ms", "window_t_ms", "f0"])
        self.assertEqual(len(feat[0]), 84)
        self.assertEqual([r[:2] for r in feat[1:]], [r[:2] for r in det[1:]])
        rx = read_csv(sd / "detect_rx.csv")
        self.assertEqual(rx[0], record.DETECT_RX_HEADER)
        self.assertEqual([r[0] for r in rx[1:]], [r[1] for r in det[1:]])
        self.assertEqual(rx[1][1], "0")  # 最初の D 行を受けた時刻が 0
        self.assertTrue(
            all(r[2:] == ["3000", "4000", "1", "0"] for r in rx[1:])
        )
        meta = json.loads((sd / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["fw"], "unoq-detector")
        self.assertEqual(
            meta["sensors"][0]["iface"], "sh12jk-wired-unoq-usbaudio"
        )
        self.assertEqual(meta["sample_rates"]["audio_hz"], 48000)
        self.assertEqual(meta["stop"], "duration")
        self.assertEqual(meta["bad_lines"], 0)
        self.assertTrue(meta["rpc"])
        self.assertNotIn("threshold", meta)
        self.assertEqual(len(meta["feature_names"]), 82)
        self.assertEqual(meta["unoq_detector"]["end"]["stop"], "eof")
        self.assertEqual(meta["unoq_detector"]["header"]["fw"], "unoq-detector")
        for k in (
            "ssh_rtt_ms",
            "rx_delay_ms",
            "e2e_ms",
            "proc_ms",
            "lat_ms",
            "queue_max",
            "backlog_max",
            "clock_offset_ms",
            "marker_offset_shift_max_ms",
            "line_gaps",
            "mixer",
            "remote_command",
            "first_line_wait_s",
            "child_returncode",
        ):
            self.assertIn(k, meta)
        self.assertEqual(meta["e2e_ms"], {"median": 30, "max": 30})
        self.assertEqual(meta["proc_ms"]["median"], 3.0)
        self.assertEqual(meta["ssh_rtt_ms"]["start"]["n"], 18)
        self.assertEqual(meta["ssh_rtt_ms"]["end"]["n"], 10)
        # 受信の遅れ: delay_upper = delay_meas + rtt_max
        meas, upper = record.rx_delays(
            [(int(r[0]), int(r[1])) for r in rx[1:]],
            meta["clock_offset_ms"],
            meta["ssh_rtt_ms"]["max_ms"],
        )
        self.assertEqual(
            meta["rx_delay_ms"]["meas"]["max"], round(max(meas), 3)
        )
        self.assertAlmostEqual(
            meta["rx_delay_ms"]["upper"]["max"]
            - meta["rx_delay_ms"]["meas"]["max"],
            meta["ssh_rtt_ms"]["max_ms"],
            places=2,
        )
        # 記録の間・終わりの表示に値・陽性の数を出さない
        self.assertNotIn("0.777777777", out)
        self.assertNotIn("4321.5678", out)
        self.assertNotIn("陽性", out)
        self.assertIn("e2e_ms", out)
        self.assertIn("proc_ms", out)
        # 順: 往復時間（起動前）→ 検出器 → 往復時間（終わり）。E が来たので消灯は送らない
        self.assertEqual(self.kinds(), ["rtt", "detector", "rtt"])
        cmd = self.calls[1][2]
        self.assertEqual(cmd[:3], ["--live", "--device", "hw:CARD=Audio,DEV=0"])
        self.assertIn("--feat", cmd)
        self.assertNotIn("--no-rpc", cmd)

    def test_u2_header_mismatch_does_not_start(self):
        good = good_header()
        for label, h in (
            ("fw", dict(good, fw="detector")),
            (
                "config",
                dict(good, m4_config=good["m4_config"].replace("0.85", "0.80")),
            ),
            ("sha", dict(good, model_sha256="0" * 64)),
            ("code", dict(good, code_sha256="1" * 64)),
            ("input", dict(good, input="replay")),
            ("rpc", dict(good, rpc=False)),
            ("heartbeat", dict(good, heartbeat_timeout_s=5.0)),
        ):
            with self.subTest(label):
                self.calls.clear()
                code, out = self.run_main("--cond", "water", det={"header": h})
                self.assertEqual(code, 1, out)
                self.assertEqual(self.sessions(), [])
                self.assertIn("記録を始めません", out)
                # 起動した検出器の後始末に消灯を送る
                self.assertIn("led_off", self.kinds())
                if label == "code":
                    self.assertIn("deploy-unoq", out)

    def test_u3_no_header_does_not_start(self):
        code, out = self.run_main(
            "--cond",
            "water",
            det={"header": None, "n": 0, "wait_eof": False},
        )
        self.assertEqual(code, 1, out)
        self.assertEqual(self.sessions(), [])
        code, out = self.run_main(
            "--cond", "water", det={"header": None, "delay": 3}, header_wait=0.5
        )
        self.assertEqual(code, 1, out)
        self.assertIn("H 行が 0.5 秒以内に届きませんでした", out)
        self.assertEqual(self.sessions(), [])

    def test_u4_argument_checks(self):
        for args, want in (
            (["--cond", "meal", "--feat"], "--no-rpc の両方"),
            (["--cond", "meal", "--no-rpc"], "--no-rpc の両方"),
            (["--cond", "water", "--subject", "p1"], "self のみ"),
            (
                ["--cond", "water", "--throat", "sh12jk-wired-unoq-usbaudio"],
                "--throat",
            ),
            (["--cond", "water", "--indicator", "/dev/x"], "--indicator"),
            (["--cond", "water", "--port", "/dev/ttyACM0"], "--port"),
            (
                ["--cond", "water", "--unoq-device", "file:/tmp/x"],
                "--unoq-device",
            ),
            (["--cond", "water", "--unoq-device", "tee:hw:0"], "--unoq-device"),
            (
                ["--cond", "water", "--unoq-host=-oProxyCommand=x"],
                "--unoq-host",
            ),
        ):
            with self.subTest(args):
                self.calls.clear()
                code, out = self.run_main(*args)
                self.assertEqual(code, 1, out)
                self.assertIn(want, out)
                self.assertEqual(self.calls, [])
                self.assertEqual(self.sessions(), [])

    def test_u4b_feat_and_no_rpc_need_unoq(self):
        out = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["record.py", "--feat"]),
            contextlib.redirect_stderr(out),
            self.assertRaises(SystemExit) as cm,
        ):
            record.main()
        self.assertIn("--unoq-detector", str(cm.exception.code))

    def test_u5_precheck_failures_do_not_start(self):
        for label, kw in (
            ("busy", dict(busy_code=0)),
            ("swap", dict(swaps="Filename Type\n/swapfile file 100 0 -2\n")),
        ):
            with self.subTest(label):
                self.calls.clear()
                code, out = self.run_main("--cond", "water", **kw)
                self.assertEqual(code, 1, out)
                self.assertEqual(self.calls, [])
                self.assertEqual(self.sessions(), [])
        # zram は受ける
        code, out = self.run_main(
            "--cond",
            "water",
            "--duration",
            "0.3",
            swaps="Filename Type\n/dev/zram0 partition 100 0 100\n",
        )
        self.assertEqual(code, 0, out)

    def test_u6_no_rpc_sends_off_first_and_waits_confirm(self):
        code, out = self.run_main(
            "--cond",
            "meal",
            "--feat",
            "--no-rpc",
            "--duration",
            "0.5",
            det={"feat": True},
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(
            self.kinds()[:4], ["led_off", "confirm", "rtt", "detector"]
        )
        self.assertIn("--no-rpc", self.calls[3][2])
        meta = json.loads(
            (self.one() / "meta.json").read_text(encoding="utf-8")
        )
        self.assertFalse(meta["rpc"])
        # 消灯が送れなければ始めない（検出器を起動しない・フォルダ無し）
        for sd in self.sessions():
            for p in sd.iterdir():
                p.unlink()
            sd.rmdir()
        self.calls.clear()
        code, out = self.run_main(
            "--cond", "meal", "--feat", "--no-rpc", led_code=3
        )
        self.assertEqual(code, 1, out)
        self.assertEqual(self.kinds(), ["led_off"])
        self.assertEqual(self.sessions(), [])
        # Enter で確かめられない（tty が無い）なら始めない
        self.calls.clear()
        code, out = self.run_main(
            "--cond", "meal", "--feat", "--no-rpc", confirm=False
        )
        self.assertEqual(code, 1, out)
        self.assertEqual(self.kinds(), ["led_off", "confirm"])
        self.assertEqual(self.sessions(), [])

    def test_u7_bad_lines_are_counted_not_written(self):
        code, out = self.run_main(
            "--cond",
            "water",
            "--duration",
            "1.0",
            det={
                "after_h": ["garbage", "D 1 2"],
                "bad": {
                    "2": ["D 9999999 0 2.5 0 1", "T 1 2 3", "X 1", "H {}"],
                    "3": ["D 500 1500 0.1 0 1"],  # 逆行
                },
            },
        )
        self.assertEqual(code, 0, out)
        meta = json.loads(
            (self.one() / "meta.json").read_text(encoding="utf-8")
        )
        self.assertEqual(meta["bad_lines"], 7)
        det = read_csv(self.one() / "detect.csv")
        w = [int(r[1]) for r in det[1:]]
        self.assertEqual(w, sorted(set(w)))

    def test_u8_child_without_end_gets_led_off(self):
        code, out = self.run_main(
            "--cond",
            "water",
            det={"n": 6, "end": False, "wait_eof": False, "exit_code": 9},
        )
        self.assertEqual(code, 1, out)
        self.assertIn("検出器が記録の途中で終わりました", out)
        self.assertEqual(self.kinds(), ["rtt", "detector", "led_off", "rtt"])
        meta = json.loads(
            (self.one() / "meta.json").read_text(encoding="utf-8")
        )
        self.assertEqual(meta["stop"], "child-exit")
        self.assertIsNone(meta["unoq_detector"]["end"])

    def test_u9_line_gap_is_shown_without_values(self):
        code, out = self.run_main(
            "--cond", "water", "--duration", "2.5", det={"pause": {"3": 1.4}}
        )
        self.assertEqual(code, 0, out)
        self.assertIn("秒届いていない", out)
        self.assertIn("届き直した（途切れ", out)
        meta = json.loads(
            (self.one() / "meta.json").read_text(encoding="utf-8")
        )
        self.assertEqual(meta["line_gaps"]["n"], 1)
        self.assertGreaterEqual(meta["line_gaps"]["max_s"], 1.0)
        self.assertNotIn("0.777777777", out)

    def test_u10_rtt_failure_does_not_start(self):
        code, out = self.run_main(
            "--cond", "water", rtt="import sys; sys.exit(0)"
        )
        self.assertEqual(code, 1, out)
        self.assertIn("往復時間を測れませんでした", out)
        self.assertNotIn("detector", self.kinds())
        self.assertEqual(self.sessions(), [])

    def test_u12_heartbeat_every_half_second(self):
        """record.py は検出器の標準入力に 0.5 秒ごとに 1 バイト（改行）を書く。"""
        log = self.tmp / "stdin.log"
        code, out = self.run_main(
            "--cond", "water", "--duration", "2.6", det={"stdin_log": str(log)}
        )
        self.assertEqual(code, 0, out)
        rows = [ln.split() for ln in log.read_text().splitlines()]
        t = [float(r[0]) for r in rows]
        self.assertGreaterEqual(len(t), 4)
        self.assertTrue(all(int(r[1]) == 1 for r in rows))
        gaps = [b - a for a, b in zip(t, t[1:])]
        self.assertTrue(all(0.3 < g < 0.8 for g in gaps), gaps)
        meta = json.loads(
            (self.one() / "meta.json").read_text(encoding="utf-8")
        )
        hb = meta["heartbeat"]
        self.assertEqual((hb["interval_s"], hb["timeout_s"]), (0.5, 2.0))
        self.assertEqual(hb["sent"], len(t))
        self.assertEqual(hb["write_errors"], 0)

    def test_u13_ssh_drop_is_child_exit_and_failed_cleanup_does_not_crash(self):
        """記録の途中で ssh の子が落ちる（Wi-Fi 切断の形）: child-exit で終わり、
        後始末の消灯と終わりの往復時間が失敗しても落ちずに meta.json を書く。"""
        code, out = self.run_main(
            "--cond",
            "water",
            det={"n": 30, "end": False, "wait_eof": False, "exit_code": 255},
            led_code=255,
        )
        self.assertEqual(code, 1, out)
        self.assertIn("検出器が記録の途中で終わりました", out)
        self.assertIn("後始末の消灯: 消灯を送れませんでした", out)
        self.assertNotIn("Traceback", out)
        meta = json.loads(
            (self.one() / "meta.json").read_text(encoding="utf-8")
        )
        self.assertEqual(meta["stop"], "child-exit")
        self.assertEqual(meta["child_returncode"], 255)
        self.assertIn("heartbeat", meta)

    def test_u11_stdin_closed_on_stop_and_child_gone(self):
        code, out = self.run_main("--cond", "water", "--duration", "0.6")
        self.assertEqual(code, 0, out)
        pid = int(self.pidfile.read_text())
        with self.assertRaises(ProcessLookupError):
            for _ in range(50):
                os.kill(pid, 0)
                time.sleep(0.02)
            raise AssertionError("子が残っている")
        meta = json.loads(
            (self.one() / "meta.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            meta["child_returncode"], 0
        )  # E を出して自分で終わった


class ReceiverTest(unittest.TestCase):
    """マーカーの時刻の直し方（pc_ms の基準、下側の包絡の offset）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.raw = Path(self._tmp.name)
        p = mock.patch.object(record, "RAW_DIR", self.raw)
        p.start()
        self.addCleanup(p.stop)
        self.a = SimpleNamespace(
            feat=False, subject="self", cond="water", position="x", band="y"
        )

    def test_r1_marker_time_uses_lower_envelope(self):
        r = record.UnoqReceiver(self.a)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            r.on_line("H " + json.dumps({"fw": "unoq-detector"}), 100.0)
            # 最初の D の前のキーは記録しない
            r.on_key("s", now=100.5)
            self.assertIn("窓がまだ届いていない", out.getvalue())
            # D 1: 装置 t 1100 を PC 101.000 で受ける（rx 0、offset −1100）
            r.on_line("D 0 1100 0.5 0 1", 101.000)
            r.on_line("T 0 1 1 0 0", 101.000)
            # D 2: 装置 t 1350 を 101.300（rx 300、差 −1050）→ offset は −1100 のまま
            r.on_line("D 250 1350 0.5 0 1", 101.300)
            # キー: pc_ms 400 → 装置 400 + 1100 = 1500
            r.on_key("s", now=101.400)
            # D 3: 装置 t 1600 を 101.450（rx 450、差 −1150）→ offset −1150
            r.on_line("D 500 1600 0.5 0 1", 101.450)
            r.on_key("c", now=101.500)  # 500 + 1150 = 1650
            r.on_key("o", "memo", now=101.600)
        st = r.st
        self.assertEqual(st["offset"], -1150)
        self.assertEqual(st["marks"], [-1100, -1150, -1150])
        sd = st["sess"].dir
        with contextlib.redirect_stdout(io.StringIO()):
            st["sess"].close({})
        ev = read_csv(sd / "events.csv")
        self.assertEqual(
            ev[1:],
            [["1500", "s", ""], ["1650", "c", ""], ["1750", "o", "memo"]],
        )
        rx = read_csv(sd / "detect_rx.csv")
        self.assertEqual(rx[1:], [["0", "0", "1", "1", "0", "0"]])

    def test_r2_t_without_matching_d_is_bad(self):
        r = record.UnoqReceiver(self.a)
        with contextlib.redirect_stdout(io.StringIO()):
            r.on_line("D 0 1100 0.5 0 1", 1.0)  # H の前
            r.on_line("H {}", 1.0)
            r.on_line("T 0 1 1 0 0", 1.0)  # 対応する D が無い
            r.on_line("D 0 1100 0.5 0 1", 1.1)
            r.on_line("T 250 1 1 0 0", 1.1)  # 窓が違う
            r.on_line("E {}", 1.2)
            r.on_line("D 250 1350 0.5 0 1", 1.3)  # E の後
        self.assertEqual(r.st["bad"], 4)
        with contextlib.redirect_stdout(io.StringIO()):
            r.st["sess"].close({})

    def test_r3_parse_grammar(self):
        P = record.parse_detector_line
        self.assertEqual(P("D 0 10 0.5 1 2"), ("D", 0, 10, "0.5", 1, 2))
        self.assertEqual(P("T 0 1 2 3 4"), ("T", 0, 1, 2, 3, 4))
        self.assertEqual(P("F 0 " + " ".join(["1"] * 82))[0], "F")
        self.assertEqual(P('H {"a":1,"b":["x"],"c":true}')[0], "H")
        for bad in (
            "D 0 10 0.5 1",
            "D 0 10 1.5 1 2",
            "D 0 10 0.5 2 2",
            "D 0 10 0.5 1 0",
            "T 0 1 2 3",
            "T 0 -1 2 3 4",
            "F 0 " + " ".join(["1"] * 81),
            "F 0 " + " ".join(["nan"] * 82),
            'H {"a":[1,2]}',
            'H {"a":{"b":1}}',
            "H [1]",
            "Z 1",
            "",
        ):
            with self.subTest(bad):
                self.assertIsNone(P(bad))


class CommandTest(unittest.TestCase):
    def test_c1_detector_command_is_fixed(self):
        exp = record.expected_unoq()
        args = record.detector_args("hw:CARD=Audio,DEV=0", True, True, exp)
        argv = record.unoq_detector_argv("arduino@unoq.local", args)
        self.assertEqual(argv[:-2], ["ssh", *record.SSH_OPTS])
        self.assertEqual(argv[-2], "arduino@unoq.local")
        cmd = argv[-1]
        self.assertTrue(
            cmd.startswith(
                "cd ~/zaitaku-mealpace-device && exec env OPENBLAS_NUM_THREADS=1"
                " OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 python3 -u -B detector.py --live"
            ),
            cmd,
        )
        self.assertIn("--device hw:CARD=Audio,DEV=0 --feat --no-rpc", cmd)
        self.assertIn(f"--expect-config '{exp['m4_config']}'", cmd)
        self.assertIn(f"--expect-sha256 {exp['model']['sha256']}", cmd)
        self.assertEqual(
            record.led_off_argv("h")[-1],
            "python3 -B ~/zaitaku-mealpace-device/led_check.py off",
        )
        self.assertEqual(record.rtt_argv("h")[-1], "exec cat")
        self.assertEqual(
            record.swap_check_argv("h")[-1], "exec cat /proc/swaps"
        )
        self.assertIn("pgrep -x arecord", record.busy_check_argv("h")[-1])

    def test_c2_expected_values_come_from_fixed_eval(self):
        fixed_eval, m4_infer, _ = record.analysis_modules()
        exp = record.expected_unoq()
        self.assertEqual(exp["m4_config"], fixed_eval.M4_CONFIG)
        self.assertEqual(
            exp["model"]["sha256"],
            m4_infer.sha256_file(record.REPO_DIR / exp["model"]["path"]),
        )
        names = record.deploy_list()
        self.assertIn("detector.py", names)
        self.assertEqual(
            exp["code_sha256"],
            m4_infer.code_sha256(record.UNOQ_DEPLOY_DIR / n for n in names),
        )

    def test_c3_confirm_needs_tty(self):
        with mock.patch.object(sys, "stdin", io.StringIO("\n")):
            self.assertFalse(record.confirm_matrix_off())


# --- unoq_session_check.py（plan #47 の 9 節） ---

MARK_PROB = "0.636363636"
MARK_FEAT = "9876.54321"


def make_device_session(
    root: Path, name="20261030-120000_self_meal", **kw
) -> Path:
    """装置のセッションの合成のフォルダ。positive・prob・led・FEAT の列に目印の
    値を入れる（出力に現れないことの確かめ）。"""
    fixed_eval, _, features_np = record.analysis_modules()
    d = root / name
    d.mkdir()
    n = kw.get("n", 40)
    skip = set(kw.get("skip", ()))
    w = [250 * k for k in range(n) if k not in skip]
    t = [x + 1040 for x in w]
    with (d / "detect.csv").open("w", newline="", encoding="utf-8") as f:
        c = csv.writer(f)
        c.writerow(["t_ms", "window_t_ms", "positive", "prob", "led"])
        for a, b in zip(t, w):
            c.writerow([a, b, 1, MARK_PROB, 2])
    if kw.get("feat", True):
        with (d / "feat.csv").open("w", newline="", encoding="utf-8") as f:
            c = csv.writer(f)
            c.writerow(["t_ms", "window_t_ms"] + [f"f{i}" for i in range(82)])
            for a, b in zip(t, w):
                c.writerow([a, b] + [MARK_FEAT] * 82)
    with (d / "detect_rx.csv").open("w", newline="", encoding="utf-8") as f:
        c = csv.writer(f)
        c.writerow(record.DETECT_RX_HEADER)
        for b in w:
            c.writerow([b, b + 40 + kw.get("rx_extra", 0), 50000, 60000, 1, 0])
    ev = kw.get(
        "events",
        [
            (500, "o", "start"),
            (2000, "s", ""),
            (3000, "c", ""),
            (4000, "o", "conv"),
        ],
    )
    with (d / "events.csv").open("w", newline="", encoding="utf-8") as f:
        c = csv.writer(f)
        c.writerow(["t_ms", "label", "note"])
        for row in ev:
            c.writerow(row)
    meta = {
        "subject": "self",
        "fw": "unoq-detector",
        "m4_config": fixed_eval.M4_CONFIG,
        "model": record.m4_model_fields(fixed_eval.M4_MODEL),
        "feature_names": list(features_np.FEAT82_NAMES),
        "rpc": False,
        "bad_lines": 0,
        "clock_offset_ms": -1000,
        "ssh_rtt_ms": {"max_ms": 12.5},
        "stop": "interrupt",
        "unoq_detector": {
            "end": {
                "stop": "eof",
                "overrun_lines": 0,
                "rpc_errors": 0,
                "anchor_spread_ms": 0.1,
                "clock_drift_min_ms": -0.5,
                "clock_drift_max_ms": 26.6,
            }
        },
        "proc_ms": {"median": 50.0, "p95": 55.0, "max": 60.0},
        "line_gaps": {"n": 0, "max_s": 0.0},
        "marker_offset_shift_max_ms": 3,
    }
    meta.update(kw.get("meta", {}))
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


class SessionCheckTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def lines(self, **kw):
        d = make_device_session(self.root, **kw)
        out = unoq_session_check.analyze(d)
        text = "\n".join(out)
        self.assertNotIn(MARK_PROB, text)
        self.assertNotIn(MARK_FEAT, text)
        self.assertNotIn("陽性", text)
        for k in ("positive", "led ", "prob"):
            self.assertNotIn(k, text)
        import shutil

        shutil.rmtree(d)
        return {ln.split(".")[0]: ln for ln in out if ln[:1].isdigit()}, out

    def test_s1_clean_session(self):
        got, out = self.lines()
        for k in "1235678":
            with self.subTest(k):
                self.assertTrue(got[k].endswith("当たらない"), got[k])
        self.assertIn("会話 あり", got["8"])
        # rx − offset − (w + 1000) = (w + 40) + 1000 − w − 1000
        self.assertIn("delay_meas 中央値 40.0", got["4"])
        self.assertIn("delay_upper 中央値 52.5", got["4"])
        self.assertIn(
            "規則: delay_meas の最大 > 1000 ms（0037 の追記）", got["4"]
        )
        self.assertTrue(got["4"].endswith("→ 当たらない"), got["4"])
        self.assertTrue(any("e2e_ms" in ln and "40" in ln for ln in out))
        clock = next(ln for ln in out if ln.startswith("時計:"))
        self.assertTrue(clock.endswith("→ 当たらない"), clock)

    def test_s5_rx_delay_rule_uses_delay_meas_only(self):
        # delay_meas 最大 = 40 + rx_extra
        got, _ = self.lines(rx_extra=961)  # 1001 > 1000
        self.assertTrue(got["4"].endswith("→ 当たる"), got["4"])
        got, _ = self.lines(rx_extra=960)  # 1000 は当たらない
        self.assertTrue(got["4"].endswith("→ 当たらない"), got["4"])
        # 往復時間が大きく delay_upper が 1000 を超えても、delay_meas で判定する
        got, _ = self.lines(meta={"ssh_rtt_ms": {"max_ms": 5000.0}})
        self.assertIn("delay_upper 中央値 5040.0", got["4"])
        self.assertTrue(got["4"].endswith("→ 当たらない"), got["4"])
        # 往復時間が無くても delay_meas で判定できる
        got, _ = self.lines(meta={"ssh_rtt_ms": {}})
        self.assertTrue(got["4"].endswith("→ 当たらない"), got["4"])

    def test_s6_clock_rule(self):
        def end(**kw):
            e = {
                "stop": "eof",
                "anchor_spread_ms": 0.1,
                "clock_drift_min_ms": -0.5,
                "clock_drift_max_ms": 26.6,
            }
            e.update(kw)
            return {"unoq_detector": {"end": e}}

        for kw, want in (
            ({}, "→ 当たらない"),
            ({"anchor_spread_ms": 250.0}, "→ 当たらない"),
            ({"anchor_spread_ms": 250.5}, "→ 当たる"),
            ({"clock_drift_max_ms": 251.0}, "→ 当たる"),
            ({"clock_drift_min_ms": -251.0}, "→ 当たる"),
            ({"clock_drift_min_ms": -250.0}, "→ 当たらない"),
            ({"clock_drift_max_ms": None}, "→ 当たる（値が無い）"),
        ):
            with self.subTest(kw=kw):
                _, out = self.lines(meta=end(**kw))
                clock = next(ln for ln in out if ln.startswith("時計:"))
                self.assertTrue(clock.endswith(want), clock)
                self.assertIn("0037 の追記", clock)
        _, out = self.lines(meta={"unoq_detector": {"end": {}}})
        clock = next(ln for ln in out if ln.startswith("時計:"))
        self.assertTrue(clock.endswith("→ 当たる（値が無い）"), clock)

    def test_s2_each_condition(self):
        cases = {
            "1": dict(n=200, skip=range(50, 55)),
            "2": dict(meta={"bad_lines": 2}),
            "5": dict(events=[(2000, "s", ""), (3000, "o", "start")]),
            "6": dict(events=[(500, "o", "start"), (10900, "s", "")]),
            "7": dict(meta={"rpc": True}),
            "8": dict(events=[(500, "o", "start"), (600, "o", "Conv")]),
        }
        for k, kw in cases.items():
            with self.subTest(k):
                got, _ = self.lines(**kw)
                self.assertTrue(got[k].endswith("→ 当たる"), got[k])
        got, _ = self.lines(feat=False)
        self.assertIn("FEAT あり", got["7"])
        got, _ = self.lines(events=[(500, "o", "start"), (500, "o", "start")])
        self.assertIn("2 個", got["5"])
        got, _ = self.lines(
            events=[
                (500, "o", "start"),
                (2000, "s", ""),
                (2100, "o", "wrong key"),
            ]
        )
        self.assertIn("直前のマーカー s 2000 ms", got["9"])
        got, _ = self.lines(n=10, events=[(-100, "o", "start")])
        self.assertIn("最初の窓より前 はい", got["5"])

    def test_s4_shifted_columns_do_not_show_values(self):
        """列がずれて prob・FEAT の値が先頭に来ても、例外の文に値を出さない。"""
        d = make_device_session(self.root)
        for name in ("detect.csv", "feat.csv"):
            rows = read_csv(d / name)
            rows[3] = rows[3][2:]  # 時刻の 2 列が欠けた行
            with (d / name).open("w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(rows)
            err = io.StringIO()
            with (
                contextlib.redirect_stderr(err),
                contextlib.redirect_stdout(err),
                self.assertRaises(SystemExit) as cm,
            ):
                unoq_session_check.analyze(d)
            msg = str(cm.exception) + err.getvalue()
            self.assertNotIn(MARK_PROB, msg)
            self.assertNotIn(MARK_FEAT, msg)
            self.assertIn("4 行目", msg)
            import shutil

            shutil.rmtree(d)
            d = make_device_session(self.root)

    def test_s3_times_rule(self):
        d = make_device_session(self.root)
        rows = read_csv(d / "detect.csv")
        rows[5][0] = "1"  # t_ms の逆行
        with (d / "detect.csv").open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
        out = unoq_session_check.analyze(d)
        line = next(ln for ln in out if ln.startswith("3."))
        self.assertTrue(line.endswith("→ 当たる"), line)


if __name__ == "__main__":
    unittest.main()
