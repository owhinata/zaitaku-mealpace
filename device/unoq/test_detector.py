"""detector.py・audio_in.py のテスト（Issue #47、plan #47 の 14 節。PC で合成だけ。
配らない）。

.venv/bin/python -m unittest discover -s device/unoq -v

合成の PCM（正弦波・雑音・振幅の大きい区間。48 kHz・2 ch・60 秒）を別のプロセスの
detector.py --replay の標準入力に流し、tools/unoq_replay_check.py の PC の再計算と
比べる。子プロセスは配る一覧（DEPLOY_FILES）だけを tempdir に写した平らな
ディレクトリ（UNO Q の ~/zaitaku-mealpace-device/ と同じ形）で動かす。
RPC・標準出力の詰まり・ライブの経路は、偽の McuLink・偽の arecord（PC の python3 が
PCM を出す）で同じプロセスの中で確かめる。data/ は使わない。
"""

import ast
import contextlib
import io
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import numpy as np

import audio_in
import detector
import features_np as fnp
import m4_infer
import mcu_rpc
import postprocess

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
for _p in (ROOT / "tools", ROOT / "analysis"):
    if str(_p) not in sys.path:
        sys.path.append(str(_p))
import record  # noqa: E402
import unoq_replay_check as urc  # noqa: E402

CFG = (
    "set=mfe40 classifier=cnn seeds=0,1,2 hard_negative=none post=hyst"
    " threshold=- enter=0.85 exit=0.65 cap_s=none refractory_s=0.5"
)
M4_SHA = "c786086f069c55c55b27eea860963c3c950c04b728fe65d52bc35a7a4a806d90"
SECONDS = 60
# 振幅の大きい区間（秒）。A と B の間は 1.0 秒: 窓が A → 無音 → B と並び、
# 不応期で B の塊が抑えられる（plan #47 の 14 節「不応期の経路」）
BURSTS = ((10.0, 10.25), (11.25, 12.25), (30.0, 31.0), (45.0, 45.5))


def deploy_files():
    lines = (HERE / "DEPLOY_FILES").read_text(encoding="utf-8").splitlines()
    return [ln for ln in lines if ln.strip()]


def synth_pcm(seconds=SECONDS, seed=0) -> np.ndarray:
    """int16 の (N, 2)。L は正弦波＋雑音＋振幅の大きい区間、R は別の信号（L だけを
    使うことの確かめ）。音声ではない。"""
    n = 48000 * seconds
    t = np.arange(n) / 48000
    rng = np.random.default_rng(seed)
    x = 200 * np.sin(2 * np.pi * 300 * t) + rng.normal(0, 60, n)
    s = slice(20 * 48000, 25 * 48000)
    x[s] += 2500 * np.sin(2 * np.pi * 1000 * t[s])
    for a, b in BURSTS:
        i, j = int(a * 48000), int(b * 48000)
        x[i:j] += rng.normal(0, 8000, j - i)
    L = np.clip(np.round(x), -32768, 32767).astype(np.int16)
    R = np.clip(np.round(0.3 * x + rng.normal(0, 500, n)), -32768, 32767)
    return np.stack([L, R.astype(np.int16)], 1)


def synthetic_arrays(wav) -> dict:
    """陽性が出るように作った同じ形の合成の重み（学習済みではない）。中心の
    1 つのタップだけを通す畳み込み 3 段 → 平均 → 大きい音ほど確率が上がる。"""
    x16 = (fnp.decimate3(wav[:, 0].astype(float)) / 32768).astype(np.float32)
    quiet = np.stack(
        [fnp.mfe40(x16[4000 * k : 4000 * k + 16000]) for k in range(20)]
    )
    mean = quiet.reshape(-1, 40).mean(0).astype(np.float64) + 3.0
    a = {"config": np.array(CFG), "format": np.array(1)}
    for s in (0, 1, 2):
        for k, (ci, co) in enumerate(fnp.CNN_CONV):
            W = np.zeros((co, ci, 3, 3), np.float32)
            W[0, 0, 1, 1] = 1.0
            a[f"s{s}/conv{k + 1}.weight"] = W
            a[f"s{s}/conv{k + 1}.bias"] = np.zeros(co, np.float32)
        fw = np.zeros((1, 32), np.float32)
        fw[0, 0] = 4.0
        a[f"s{s}/fc.weight"] = fw
        a[f"s{s}/fc.bias"] = np.array([-4.0], np.float32)
        a[f"s{s}/mean"] = mean
        a[f"s{s}/std"] = np.ones(40)
    m4_infer._check_arrays(a)
    return a


# PC にはディスクのスワップがあることがあるので、テストの間は zram だけの
# /proc/swaps の写しを見せる（検出器の確かめそのものは SwapDumpTest で見る）
_SWAP_TMP = tempfile.TemporaryDirectory()
ZRAM_SWAPS = pathlib.Path(_SWAP_TMP.name) / "swaps"
ZRAM_SWAPS.write_text(
    "Filename\tType\tSize\tUsed\tPriority\n/dev/zram0 partition 100 0 100\n"
)
DISK_SWAPS = pathlib.Path(_SWAP_TMP.name) / "swaps-disk"
DISK_SWAPS.write_text(
    "Filename\tType\tSize\tUsed\tPriority\n/swap.img file 100 0 -2\n"
)
_REAL_SWAPS_PATH = audio_in.SWAPS_PATH
_REAL_CORE_PATH = audio_in.CORE_PATTERN_PATH
_REAL_SYS_BLOCK = audio_in.SYS_BLOCK
# core_pattern の写し（PC は apport のパイプのことがある。UNO Q は `core`）と、書き戻しの無い zram0
CORE_FILE = pathlib.Path(_SWAP_TMP.name) / "core_pattern"
CORE_FILE.write_text("core\n")
SYS_BLOCK = pathlib.Path(_SWAP_TMP.name) / "block"
(SYS_BLOCK / "zram0").mkdir(parents=True)
(SYS_BLOCK / "zram0" / "backing_dev").write_text("none\n")


def setUpModule():
    audio_in.SWAPS_PATH = str(ZRAM_SWAPS)
    audio_in.CORE_PATTERN_PATH = str(CORE_FILE)
    audio_in.SYS_BLOCK = str(SYS_BLOCK)


# 別のプロセスの detector.py: audio_in.SWAPS_PATH だけを差し替えて __main__ として動かす
WRAP = (
    "import runpy, sys\n"
    "dev, swaps = sys.argv[1], sys.argv[2]\n"
    "sys.path.insert(0, dev)\n"
    "import audio_in\n"
    "audio_in.SWAPS_PATH = swaps\n"
    "audio_in.CORE_PATTERN_PATH, audio_in.SYS_BLOCK = sys.argv[3], sys.argv[4]\n"
    "sys.argv = [dev + '/detector.py'] + sys.argv[5:]\n"
    "runpy.run_path(dev + '/detector.py', run_name='__main__')\n"
)


def detector_cmd(dev, swaps=None, core=None):
    return [
        sys.executable,
        "-B",
        "-c",
        WRAP,
        str(dev),
        str(ZRAM_SWAPS if swaps is None else swaps),
        str(CORE_FILE if core is None else core),
        str(SYS_BLOCK),
    ]


def snapshot(*dirs):
    return {p for d in dirs for p in pathlib.Path(d).rglob("*")}


class Fixture:
    """配る一覧を写した平らなディレクトリ（クラスで 1 回作る）。"""

    @classmethod
    def build(cls, tmp: pathlib.Path):
        cls.wav = synth_pcm()
        cls.pcm = cls.wav.tobytes()
        cls.dev = tmp / "dev"
        cls.dev.mkdir()
        for n in deploy_files():
            shutil.copyfile(HERE / n, cls.dev / n)
        cls.syn = tmp / "syn"
        cls.syn.mkdir()
        for n in deploy_files():
            shutil.copyfile(HERE / n, cls.syn / n)
        arrays = synthetic_arrays(cls.wav)
        with open(cls.syn / "m4-0038.npz", "wb") as f:
            np.savez(f, **arrays)
        cls.syn_sha = m4_infer.sha256_file(cls.syn / "m4-0038.npz")
        cls.syn_model = m4_infer.load_model(cls.syn / "m4-0038.npz")
        cls.model = m4_infer.load_model(HERE / "m4-0038.npz")


_FIX = None


def fixture():
    global _FIX
    if _FIX is None:
        tmp = tempfile.TemporaryDirectory()
        Fixture.build(pathlib.Path(tmp.name))
        Fixture._tmp = tmp
        _FIX = Fixture
    return _FIX


def tearDownModule():
    audio_in.SWAPS_PATH = _REAL_SWAPS_PATH
    audio_in.CORE_PATTERN_PATH = _REAL_CORE_PATH
    audio_in.SYS_BLOCK = _REAL_SYS_BLOCK
    if _FIX is not None:
        _FIX._tmp.cleanup()
    _SWAP_TMP.cleanup()


def run_child(dev, pcm, *extra, sha=M4_SHA, stdout=subprocess.PIPE, cwd=None):
    """別のプロセスの detector.py --replay。(returncode, stdout の行, stderr)。"""
    env = dict(os.environ)
    tmpdir = tempfile.mkdtemp()
    env["TMPDIR"] = tmpdir
    work = tempfile.mkdtemp()
    before = snapshot(dev, work, tmpdir, HERE)
    try:
        r = subprocess.run(
            [
                *detector_cmd(dev),
                "--replay",
                "--no-rpc",
                *extra,
                "--expect-config",
                CFG,
                "--expect-sha256",
                sha,
            ],
            input=pcm,
            stdout=stdout,
            stderr=subprocess.PIPE,
            cwd=cwd or work,
            env=env,
            timeout=600,
        )
        after = snapshot(dev, work, tmpdir, HERE)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)
    out = (r.stdout or b"").decode("utf-8").splitlines()
    return r.returncode, out, r.stderr.decode("utf-8"), before, after


class ReplayTest(unittest.TestCase):
    """別のプロセスの再生モードの出力 = PC の再計算（8 節と同じ関数）。"""

    @classmethod
    def setUpClass(cls):
        cls.f = fixture()
        cls.runs = {}

    def run_replay(self, which, feat):
        key = (which, feat)
        if key not in self.runs:
            dev = self.f.dev if which == "m4" else self.f.syn
            sha = M4_SHA if which == "m4" else self.f.syn_sha
            extra = ("--feat",) if feat else ()
            self.runs[key] = run_child(dev, self.f.pcm, *extra, sha=sha)
        return self.runs[key]

    def check_match(self, which, feat):
        rc, lines, err, before, after = self.run_replay(which, feat)
        self.assertEqual(rc, 0, err)
        self.assertEqual(before, after)  # 配った先・作業・TMPDIR に増えない
        model = self.f.model if which == "m4" else self.f.syn_model
        dev = urc.collect(lines)
        pc = urc.recompute(self.f.wav, model, feat)
        res = urc.compare(dev, pc, feat)
        self.assertTrue(res["same_windows"], res)
        self.assertEqual(
            res["windows_device"], urc.device_windows(len(self.f.wav))
        )
        self.assertLessEqual(res["prob_max_diff"], 1e-5, res)
        self.assertEqual(res["positive_mismatch"], 0, res)
        self.assertEqual(res["led_mismatch"], 0, res)
        if feat:
            self.assertEqual(res["feat_over_tol"], 0, res)
        self.assertTrue(res["ok"], res)
        self.assertEqual(dev["E"]["windows"], res["windows_device"])
        self.assertEqual(dev["E"]["stop"], "eof")
        self.assertFalse(dev["E"]["rpc"])
        return dev, pc, res

    def test_d1_m4_0038_matches_pc(self):
        self.check_match("m4", False)
        self.check_match("m4", True)

    def test_d2_synthetic_weights_positive_face_and_refractory(self):
        dev, pc, _ = self.check_match("syn", True)
        pos = pc["positive"].astype(bool)
        self.assertTrue(pos.any())
        self.assertIn(2, list(pc["led"]))
        # 不応期で抑えた塊がある（ヒステリシスだけなら陽性の窓）
        n = len(pos)
        hyst = postprocess.hysteresis(
            pc["prob"], np.ones(n, bool), 0.85, 0.65, 0.25 * np.arange(n)
        )
        self.assertTrue((hyst & ~pos).any())
        self.check_match("syn", False)

    def test_d3_line_grammar_and_stderr(self):
        for feat in (False, True):
            with self.subTest(feat=feat):
                rc, lines, err, _, _ = self.run_replay("m4", feat)
                self.assertEqual(rc, 0, err)
                self.assertTrue(lines[0].startswith("H "))
                self.assertTrue(lines[-1].startswith("E "))
                prev = None
                for i, ln in enumerate(lines):
                    kind = ln.split(" ", 1)[0]
                    self.assertIn(kind, ("H", "D", "T", "F", "E"), ln[:40])
                    f = ln.split(" ")
                    if kind in ("H", "E"):
                        obj = json.loads(ln[2:])
                        self.assertIsInstance(obj, dict)
                        for v in obj.values():
                            ok = isinstance(v, (int, float, str, bool)) or (
                                isinstance(v, list)
                                and all(isinstance(x, str) for x in v)
                            )
                            self.assertTrue(ok, v)
                        self.assertEqual(kind == "H", i == 0)
                    elif kind == "D":
                        self.assertEqual(len(f), 6)
                        if feat:
                            self.assertTrue(prev.startswith(f"F {f[1]} "))
                    elif kind == "T":
                        self.assertEqual(len(f), 6)
                        [int(x) for x in f[1:]]
                        self.assertTrue(prev.startswith(f"D {f[1]} "))
                    elif kind == "F":
                        self.assertTrue(feat)
                        self.assertEqual(len(f), 84)
                    prev = ln
                h = json.loads(lines[0][2:])
                self.assertEqual(h["fw"], "unoq-detector")
                self.assertEqual(h["m4_config"], CFG)
                self.assertEqual(h["model_sha256"], M4_SHA)
                self.assertEqual(h["code_sha256"], record.local_code_sha256())
                self.assertEqual("feature_names" in h, feat)
                if feat:
                    self.assertEqual(h["feature_names"], list(fnp.FEAT82_NAMES))
                # 標準エラーに 10 個以上続く数が無い
                self.assertIsNone(
                    re.search(r"(?:[-+0-9.eE]+[\s,]+){9}[-+0-9.eE]+", err),
                    err,
                )

    def test_d4_mismatch_stops_with_2_and_no_header(self):
        for extra, sha in (
            ((), "0" * 64),
            (("--expect-config", CFG + " "), M4_SHA),
        ):
            with self.subTest(sha=sha[:4]):
                args = ["--replay", "--no-rpc"]
                cfg = CFG if not extra else extra[1]
                r = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        str(self.f.dev / "detector.py"),
                        *args,
                        "--expect-config",
                        cfg,
                        "--expect-sha256",
                        sha,
                    ],
                    input=b"",
                    capture_output=True,
                    timeout=120,
                )
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertEqual(r.stdout, b"")

    def test_d5_selftest(self):
        r = subprocess.run(
            [
                sys.executable,
                "-B",
                str(self.f.dev / "detector.py"),
                "--selftest",
                "--expect-config",
                CFG,
                "--expect-sha256",
                M4_SHA,
            ],
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, b"")
        err = r.stderr.decode()
        self.assertIn("照合（features_np_ref.npz）: OK", err)
        self.assertIn("照合（im2col の新旧）: OK", err)
        self.assertIn("重みの sha256: 一致", err)

    def test_d6_stdout_not_read_ends_with_stall(self):
        """標準出力を読まない子: パイプが詰まり、キューが 2 秒分を超えて 4 で
        終わる（主のループは書き込みで止まらない）。"""
        env = dict(os.environ)
        p = subprocess.Popen(
            [
                *detector_cmd(self.f.dev),
                "--replay",
                "--no-rpc",
                "--feat",
                "--expect-config",
                CFG,
                "--expect-sha256",
                M4_SHA,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            try:
                p.stdin.write(self.f.pcm)
                p.stdin.close()
            except BrokenPipeError:
                pass
            rc = p.wait(timeout=300)
        finally:
            if p.poll() is None:
                p.kill()
            with contextlib.suppress(BrokenPipeError):
                p.stdin.close()
        err = p.stderr.read().decode()
        p.stdout.close()
        p.stderr.close()
        self.assertEqual(rc, 4, err)
        self.assertIn("標準出力が詰まった", err)


class SwapDumpTest(unittest.TestCase):
    """音声を受ける前の守り: ディスクのスワップがあれば始めない、ダンプを止める。"""

    @classmethod
    def setUpClass(cls):
        cls.f = fixture()

    def test_w1_disk_swap_or_unreadable_stops_with_2_and_no_header(self):
        for swaps in (DISK_SWAPS, pathlib.Path(_SWAP_TMP.name) / "none"):
            with self.subTest(swaps=swaps.name):
                r = subprocess.run(
                    [
                        *detector_cmd(self.f.dev, swaps),
                        "--replay",
                        "--no-rpc",
                        "--expect-config",
                        CFG,
                        "--expect-sha256",
                        M4_SHA,
                    ],
                    input=self.f.pcm[: 48000 * 4 * 2],
                    capture_output=True,
                    timeout=120,
                )
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertEqual(r.stdout, b"")
                self.assertIn("スワップ", r.stderr.decode())
        self.assertEqual(audio_in.disk_swaps(DISK_SWAPS), ["/swap.img"])
        self.assertEqual(audio_in.disk_swaps(ZRAM_SWAPS), [])
        self.assertIsNone(audio_in.disk_swaps("/nonexistent/swaps"))
        # 実物の /proc/swaps も読める（PC の値は問わない）
        self.assertIsNotNone(audio_in.disk_swaps(_REAL_SWAPS_PATH))

    def test_w3_zram_writeback_and_core_pattern(self):
        wb = pathlib.Path(_SWAP_TMP.name) / "block-wb"
        (wb / "zram0").mkdir(parents=True, exist_ok=True)
        (wb / "zram0" / "backing_dev").write_text("/dev/mmcblk0p70\n")
        old = audio_in.SYS_BLOCK
        try:
            audio_in.SYS_BLOCK = str(wb)
            self.assertEqual(audio_in.disk_swaps(ZRAM_SWAPS), ["/dev/zram0"])
            # sysfs が見えないなら安全側（ディスクに出うる扱い）
            audio_in.SYS_BLOCK = str(pathlib.Path(_SWAP_TMP.name) / "nosys")
            self.assertEqual(audio_in.disk_swaps(ZRAM_SWAPS), ["/dev/zram0"])
            # デバイスは見えて backing_dev が無い（書き戻しの無い kernel）なら受ける
            nb = pathlib.Path(_SWAP_TMP.name) / "block-nobd"
            (nb / "zram0").mkdir(parents=True, exist_ok=True)
            audio_in.SYS_BLOCK = str(nb)
            self.assertEqual(audio_in.disk_swaps(ZRAM_SWAPS), [])
        finally:
            audio_in.SYS_BLOCK = old
        d = pathlib.Path(_SWAP_TMP.name)
        for text, ok in (
            ("core\n", True),
            ("/var/crash/core.%p\n", True),
            ("|/usr/lib/systemd/systemd-coredump %P %u\n", False),
            ("\n", False),
            ("|/usr/share/apport/apport -p%p -- %E\n", False),
            ("|/tmp/x\n", False),
        ):
            with self.subTest(text=text):
                f = d / "cp"
                f.write_text(text)
                self.assertEqual(audio_in.core_pattern_ok(f)[0], ok)
        self.assertEqual(audio_in.core_pattern_ok(d / "none"), (False, ""))
        # 受けない core_pattern では始めない（2、H 行なし）
        bad = d / "cp-apport"
        bad.write_text("|/usr/share/apport/apport -p%p\n")
        r = subprocess.run(
            [
                *detector_cmd(self.f.dev, core=bad),
                "--replay",
                "--no-rpc",
                "--expect-config",
                CFG,
                "--expect-sha256",
                M4_SHA,
            ],
            input=b"",
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(r.stdout, b"")
        self.assertIn("core_pattern", r.stderr.decode())

    def test_w2_process_is_not_dumpable(self):
        """detector を import したプロセスは dumpable 0（/proc/self/status の
        項目は無いので prctl(PR_GET_DUMPABLE) で読む）。子のプロセスでも同じ。"""
        import ctypes

        self.assertTrue(detector.DUMPS_OFF)
        libc = ctypes.CDLL(None)
        self.assertEqual(libc.prctl(3, 0, 0, 0, 0), 0)  # PR_GET_DUMPABLE
        code = (
            "import sys, ctypes; sys.path.insert(0, sys.argv[1]); import detector;"
            "print(detector.DUMPS_OFF, ctypes.CDLL(None).prctl(3, 0, 0, 0, 0))"
        )
        r = subprocess.run(
            [sys.executable, "-B", "-c", code, str(self.f.dev)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(r.stdout.split(), ["True", "0"], r.stderr)


class FakeLink:
    def __init__(self, log, fail=()):
        self.log = log
        self.fail = set(fail)

    def set_state(self, s):
        n = len(self.log)
        self.log.append(s)
        if n in self.fail:
            raise mcu_rpc.McuRpcTimeout("合成")

    def close(self):
        self.log.append("close")


class InProcess(unittest.TestCase):
    """同じプロセスで main() を回す（偽の McuLink・偽の arecord）。"""

    @classmethod
    def setUpClass(cls):
        cls.f = fixture()

    def args(self, *a):
        return [*a, "--expect-config", CFG, "--expect-sha256", M4_SHA]

    def feed(self, data_parts, pause=0.0, keep_open=None):
        """(読む側の fd, 書くスレッド)。data_parts を順に書き、間で pause 秒
        待つ。keep_open が Event なら、それが立つまで閉じない。"""
        r, w = os.pipe()

        def run():
            try:
                for i, part in enumerate(data_parts):
                    if i:
                        time.sleep(pause)
                    _write_all(w, part)
                if keep_open is not None:
                    keep_open.wait(30)
            except BrokenPipeError:
                pass  # 検出器が先に終わって読む側を閉じた
            finally:
                os.close(w)

        th = threading.Thread(target=run, daemon=True)
        th.start()
        return r, th

    def run_main(self, argv, data_parts, pause=0.0, links=None, **kw):
        log = [] if links is None else links
        opened = []
        fail = kw.pop("fail", ())

        def open_link():
            opened.append(1)
            return FakeLink(log, fail if len(opened) == 1 else ())

        keep_open = kw.pop("keep_open", None)
        r, th = self.feed(data_parts, pause, keep_open)
        out = io.StringIO()
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                rc = detector.main(
                    self.args(*argv),
                    open_link=open_link,
                    stdin_fd=r,
                    stdout=kw.pop("stdout", out),
                    **kw,
                )
        finally:
            if keep_open is not None:
                keep_open.set()
            os.close(r)
            th.join(timeout=30)
        lines = out.getvalue().splitlines()
        return rc, lines, log, opened, err.getvalue()

    def windows(self, lines):
        return [ln for ln in lines if ln.startswith("D ")]

    def test_r1_led_sent_each_window_and_off_at_end(self):
        pcm = self.f.pcm[: 48000 * 4 * 8]  # 8 秒
        rc, lines, log, opened, err = self.run_main(["--replay"], [pcm])
        self.assertEqual(rc, 0, err)
        D = self.windows(lines)
        leds = [int(ln.split()[5]) for ln in D]
        # 最初の消灯、窓ごとの led、最後の消灯、閉じる
        self.assertEqual(log, [0, *leds, 0, "close"])
        self.assertEqual(len(opened), 1)
        # --no-rpc でも led の列は同じ値
        rc2, lines2, log2, opened2, _ = self.run_main(
            ["--replay", "--no-rpc"], [pcm]
        )
        self.assertEqual(rc2, 0)
        self.assertEqual(opened2, [])
        self.assertEqual(log2, [])
        self.assertEqual(
            [ln.split()[5] for ln in self.windows(lines2)],
            [ln.split()[5] for ln in D],
        )
        e = json.loads(lines[-1][2:])
        self.assertTrue(e["rpc"])
        self.assertFalse(json.loads(lines2[-1][2:])["rpc"])

    def test_r2_idle_one_second_sends_off(self):
        a = self.f.pcm[: 48000 * 4 * 3]
        b = self.f.pcm[48000 * 4 * 3 : 48000 * 4 * 5]
        rc, lines, log, _, err = self.run_main(["--replay"], [a, b], pause=1.6)
        self.assertEqual(rc, 0, err)
        D = self.windows(lines)
        n_a = urc.device_windows(48000 * 3)
        leds = [int(ln.split()[5]) for ln in D]
        # 3 秒分の窓（8 窓）の後、1.0 秒の空きで消灯が 1 回入る
        self.assertEqual(n_a, 8)
        self.assertEqual(log[: 1 + n_a], [0, *leds[:n_a]])
        self.assertEqual(log[1 + n_a], 0)
        self.assertEqual(log[2 + n_a : -2], leds[n_a:])
        self.assertEqual(log[-2:], [0, "close"])

    def test_r3_rpc_failure_counted_and_recording_continues(self):
        pcm = self.f.pcm[: 48000 * 4 * 6]
        rc, lines, log, opened, err = self.run_main(
            ["--replay"], [pcm], fail=(3,)
        )
        self.assertEqual(rc, 0, err)
        e = json.loads(lines[-1][2:])
        self.assertGreaterEqual(e["rpc_errors"], 1)
        self.assertEqual(len(self.windows(lines)), e["windows"])
        self.assertEqual(e["windows"], urc.device_windows(48000 * 6))
        # 終わりの消灯は、落ちたつなぎを開き直して送る
        self.assertEqual(log[-2:], [0, "close"])

    def test_r4_sigterm_sends_off(self):
        ev = threading.Event()
        pcm = self.f.pcm[: 48000 * 4 * 3]
        timer = threading.Timer(1.5, os.kill, (os.getpid(), signal.SIGTERM))
        timer.start()
        try:
            rc, lines, log, _, err = self.run_main(
                ["--replay"], [pcm], keep_open=ev
            )
        finally:
            ev.set()
            timer.cancel()
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(lines[-1][2:])["stop"], "signal")
        self.assertEqual(log[-2:], [0, "close"])

    def test_r5_exception_sends_off(self):
        class Boom(FakeLink):
            def set_state(self, s):
                super().set_state(s)
                if len(self.log) == 4:
                    raise RuntimeError("合成")

        log = []
        r, th = self.feed([self.f.pcm[: 48000 * 4 * 4]])
        out = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()):
            rc = detector.main(
                self.args("--replay"),
                open_link=lambda: Boom(log),
                stdin_fd=r,
                stdout=out,
            )
        os.close(r)
        th.join(timeout=10)
        self.assertEqual(rc, detector.EXIT_ERROR)
        self.assertEqual(log[-2:], [0, "close"])
        self.assertEqual(
            json.loads(out.getvalue().splitlines()[-1][2:])["stop"], "error"
        )

    def test_r6_stdout_stall_in_process(self):
        """書き込みが返らない標準出力: キューが 2 秒分（8 窓分）を超えたところで
        消灯を送り 4 で終わる。主のループは止まらない。"""
        release = threading.Event()

        class Stuck:
            def write(self, s):
                release.wait(60)

            def flush(self):
                pass

        t0 = time.monotonic()
        try:
            rc, _, log, _, err = self.run_main(
                ["--replay"], [self.f.pcm[: 48000 * 4 * 10]], stdout=Stuck()
            )
        finally:
            release.set()
        self.assertEqual(rc, detector.EXIT_STALL, err)
        self.assertLess(time.monotonic() - t0, 20)
        self.assertEqual(log[-2:], [0, "close"])
        # 窓は 8 窓分を超えたところまで（9 窓目で止まる）
        self.assertLessEqual(len([s for s in log if s in (1, 2)]), 9)

    FAKE_ARECORD = (
        "import sys, time\n"
        "data = sys.stdin.buffer.read()\n"
        "o = sys.stdout.buffer\n"
        "for i in range(0, len(data), 24000):\n"
        "    o.write(data[i:i+24000]); o.flush(); time.sleep(0.002)\n"
        "time.sleep(30)\n"
    )

    def run_live_heartbeat(self, beats_s, then_close):
        """偽の arecord で --live を回す。標準入力に 0.5 秒ごとの改行を
        beats_s 秒書き、then_close なら閉じる（でなければ開いたまま黙る）。
        (rc, E 行, 偽の McuLink の記録, H 行, 所要の秒数)。"""
        pcm = self.f.pcm[: 48000 * 4 * 3]
        with tempfile.TemporaryDirectory() as d:
            pcm_path = pathlib.Path(d) / "pcm.raw"
            pcm_path.write_bytes(pcm)
            fake = [
                "sh",
                "-c",
                f'exec "{sys.executable}" -I -c "$0" < "{pcm_path}"',
                self.FAKE_ARECORD,
            ]
            r, w = os.pipe()
            done = threading.Event()

            def beat():
                try:
                    end = time.monotonic() + beats_s
                    while time.monotonic() < end and not done.is_set():
                        os.write(w, b"\n")
                        time.sleep(0.5)
                    if not then_close:
                        done.wait(30)
                except OSError:
                    pass
                finally:
                    os.close(w)

            th = threading.Thread(target=beat, daemon=True)
            th.start()
            log = []
            out = io.StringIO()
            t0 = time.monotonic()
            with contextlib.redirect_stderr(io.StringIO()):
                rc = detector.main(
                    self.args("--live"),
                    open_link=lambda: FakeLink(log),
                    live_argv=fake,
                    stdin_fd=r,
                    stdout=out,
                )
            dt = time.monotonic() - t0
            done.set()
            os.close(r)
            th.join(timeout=10)
        lines = out.getvalue().splitlines()
        return rc, json.loads(lines[-1][2:]), log, json.loads(lines[0][2:]), dt

    def test_r8_heartbeat_stops_then_off_and_exit_6(self):
        rc, e, log, h, dt = self.run_live_heartbeat(1.0, then_close=False)
        self.assertEqual(rc, detector.EXIT_HEARTBEAT)
        self.assertEqual(rc, 6)
        self.assertEqual(e["stop"], "heartbeat-timeout")
        self.assertEqual(log[-2:], [0, "close"])
        # 最後の改行（約 0.5 秒）から 2.0 秒 ＋ select の 0.1 秒ほどで止まる
        self.assertLess(dt, 1.0 + 2.0 + 1.5)
        self.assertEqual(
            (h["heartbeat_s"], h["heartbeat_timeout_s"]), (0.5, 2.0)
        )

    def test_r9_heartbeat_continuing_keeps_running_then_eof(self):
        rc, e, log, _, dt = self.run_live_heartbeat(3.5, then_close=True)
        self.assertEqual(rc, 0)
        self.assertEqual(e["stop"], "eof")
        self.assertGreater(dt, 3.0)  # 2 秒を超えても止まらなかった
        self.assertEqual(log[-2:], [0, "close"])

    def test_r10_replay_has_no_heartbeat(self):
        """再生は標準入力が音声: 2 秒を超えて何も来なくてもハートビートで止まら
        ず、EOF で 0。H 行にハートビートの設定は無い。"""
        a = self.f.pcm[: 48000 * 4 * 2]
        b = self.f.pcm[48000 * 4 * 2 : 48000 * 4 * 3]
        rc, lines, log, _, err = self.run_main(["--replay"], [a, b], pause=2.6)
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(lines[-1][2:])["stop"], "eof")
        h = json.loads(lines[0][2:])
        self.assertNotIn("heartbeat_timeout_s", h)

    def test_r7_live_fake_arecord_eof_overrun_and_device(self):
        pcm_path = None
        src = (
            "import sys, time\n"
            "data = sys.stdin.buffer.read()\n"
            "o = sys.stdout.buffer\n"
            "for i in range(0, len(data), 24000):\n"
            "    o.write(data[i:i+24000]); o.flush(); time.sleep(0.002)\n"
            "if len(sys.argv) > 1:\n"
            "    sys.stderr.write('overrun!!! (at least 1.0 ms long)\\n'); sys.stderr.flush()\n"
            "time.sleep(30)\n"
        )
        pcm = self.f.pcm[: 48000 * 4 * 3]
        with tempfile.TemporaryDirectory() as d:
            pcm_path = pathlib.Path(d) / "pcm.raw"
            pcm_path.write_bytes(pcm)
            for overrun in (False, True):
                with self.subTest(overrun=overrun):
                    fake = [
                        "sh",
                        "-c",
                        f'exec "{sys.executable}" -I -c "$0" "$@" < "{pcm_path}"',
                        src,
                        *(["x"] if overrun else []),
                    ]
                    ev = threading.Event()
                    if not overrun:
                        threading.Timer(1.5, ev.set).start()
                    log = []
                    r, th = self.feed([], keep_open=ev)
                    out = io.StringIO()
                    with contextlib.redirect_stderr(io.StringIO()):
                        rc = detector.main(
                            self.args("--live"),
                            open_link=lambda: FakeLink(log),
                            live_argv=fake,
                            stdin_fd=r,
                            stdout=out,
                        )
                    ev.set()
                    os.close(r)
                    th.join(timeout=10)
                    lines = out.getvalue().splitlines()
                    e = json.loads(lines[-1][2:])
                    h = json.loads(lines[0][2:])
                    self.assertEqual(h["input"], "live")
                    self.assertEqual(h["device"], audio_in.DEFAULT_DEVICE)
                    if overrun:
                        self.assertEqual(rc, detector.EXIT_OVERRUN)
                        self.assertEqual(e["stop"], "overrun")
                        self.assertEqual(e["overrun_lines"], 1)
                    else:
                        self.assertEqual(rc, 0)
                        self.assertEqual(e["stop"], "eof")
                        self.assertEqual(
                            e["windows"], urc.device_windows(48000 * 3)
                        )
                    self.assertEqual(log[-2:], [0, "close"])
        # --device の正規表現を通らない値は起動前に止まる（arecord を起動しない）
        for bad in ("tee:hw:0,'/tmp/x'", "file:/tmp/x", "default", "hw:0;rm"):
            with self.subTest(bad=bad):
                with contextlib.redirect_stderr(io.StringIO()):
                    rc = detector.main(
                        self.args("--live", "--device", bad),
                        open_link=lambda: self.fail("RPC を開いた"),
                        live_argv=["false"],
                    )
                self.assertEqual(rc, detector.EXIT_MISMATCH)
                with self.assertRaises(audio_in.InputError):
                    audio_in.arecord_argv(bad)
        self.assertEqual(
            audio_in.arecord_argv("plughw:CARD=Audio,DEV=0")[-1], "-"
        )


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        n = os.write(fd, view[:65536])
        view = view[n:]


class HeartbeatTest(unittest.TestCase):
    """偽の時計で Heartbeat の判定。"""

    def test_h1_expires_after_timeout_and_beat_resets(self):
        clk = SimpleClock()
        hb = detector.Heartbeat(2.0, clock=clk)
        clk.t = 1.9
        self.assertFalse(hb.expired())
        hb.beat()
        clk.t = 3.8
        self.assertFalse(hb.expired())
        clk.t = 3.9
        self.assertTrue(hb.expired())
        hb.beat()
        self.assertFalse(hb.expired())
        self.assertEqual(detector.HEARTBEAT_TIMEOUT_S, 2.0)
        self.assertEqual(detector.HEARTBEAT_S, record.UNOQ_HEARTBEAT_S)
        self.assertEqual(
            detector.HEARTBEAT_TIMEOUT_S, record.UNOQ_HEARTBEAT_TIMEOUT_S
        )


class SimpleClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeClock:
    def __init__(self):
        self.t = 0

    def __call__(self):
        return self.t


class PipelineTest(unittest.TestCase):
    """proc_us・lat_us・窓の時刻（偽の時計）。"""

    class Det:
        def __init__(self, clock, cost):
            self.clock, self.cost = clock, cost
            self.calls = []

        def step(self, seg, w):
            self.clock.t += self.cost
            self.calls.append(w)
            return 0.5, 0, 1, None

    class Decim:
        """入力 3 サンプルごとに 16 kHz の 1 サンプル。1 入力あたり 10 ns。"""

        def __init__(self, clock):
            self.clock = clock

        def process(self, x):
            self.clock.t += 10 * len(x)
            return np.zeros(len(x) // 3)

    class Out:
        def __init__(self):
            self.lines = []

        def put(self, line, check=True):
            self.lines.append(line)

        def qsize(self):
            return 0

    def make(self, live=True, cost=1_000_000, rpc=500_000):
        clk = FakeClock()
        mono = FakeClock()
        out = self.Out()
        det = self.Det(clk, cost)

        def send(led):
            clk.t += rpc

        p = detector.Pipeline(
            det,
            out,
            send,
            live,
            0,
            clock_ns=clk,
            mono_ns=mono,
            decim=self.Decim(clk),
        )
        return p, clk, mono, out

    def T(self, out):
        return [
            list(map(int, ln.split()[1:])) for ln in out.lines if ln[0] == "T"
        ]

    def block(self, frames):
        return np.zeros(frames, np.float64).tobytes()

    def test_p1_proc_splits_input_by_new_samples(self):
        p, clk, mono, out = self.make(live=False)
        # 1 ブロック 6000 フレーム（= 16 kHz の 2000）を順に。窓 0 は 8 ブロック
        # 目で、ホップ 0 の新しいサンプル [12000, 16000) は 7・8 ブロック目
        for i in range(12):
            clk.t += 7  # 到着の時刻を少しずらす
            p.feed(self.block(6000), 0, clk.t)
        T = self.T(out)
        self.assertEqual([t[0] for t in T], [0, 250, 500])
        # 入力処理: 新しい 4000 サンプル = 12000 入力 × 10 ns = 120 µs、
        # 窓の処理 1000 µs、RPC 500 µs
        for t in T:
            self.assertEqual(t[1], 120 + 1000 + 500)
            self.assertEqual(t[4], 0)  # backlog
        # 到着から T まで: そのブロックの入力処理 60 µs ＋ 窓 1000 ＋ RPC 500
        self.assertEqual(T[0][2], 60 + 1000 + 500)

    def test_p2_block_spanning_hops_and_backlog(self):
        p, clk, mono, out = self.make(live=False)
        p.feed(self.block(42000), 0, clk.t)  # 14000 サンプル（窓の手前）
        # 9000 フレーム = 3000 サンプル: 1000 はホップ 0 の残り、2000 はホップ 1
        a = clk.t
        p.feed(self.block(9000), 0, a)
        # 一度に 3 ホップ分: 12000 サンプル
        b = clk.t
        p.feed(self.block(36000), 0, b)
        T = self.T(out)
        self.assertEqual([t[0] for t in T], [0, 250, 500, 750])
        # ホップ 0: 1 ブロック目の 12000〜14000（2000 サンプル = 60 µs）＋
        # 2 ブロック目の 2000（= 60 µs）
        self.assertEqual(T[0][1], 60 + 60 + 1500)
        # ホップ 1: 2 ブロック目の残り 1000（30 µs）＋ 3 ブロック目の 3000（90 µs）
        self.assertEqual(T[1][1], 30 + 90 + 1500)
        self.assertEqual(T[2][1], 120 + 1500)
        # 3 ブロック目の窓 2・3 は前の窓の処理を待つので lat > proc、backlog > 0
        self.assertEqual(T[1][4], 9000)
        self.assertEqual(T[2][4], 5000)
        self.assertEqual(T[3][4], 1000)
        self.assertGreater(T[2][2], T[2][1])
        self.assertGreater(T[3][2], T[2][2])
        # lat の定義: 窓 3 = 3 ブロック目の入力処理全体（360 µs）＋ 窓 1〜3 の処理
        self.assertEqual(T[3][2], 360 + 3 * 1500)

    def test_p3_anchor_is_lower_envelope_of_first_second(self):
        p, clk, mono, out = self.make(live=True)
        ms = 1_000_000
        # 6000 フレーム（125 ms）ずつ。1 つ目は 300 ms 遅れて届き、2〜4 は
        # パイプに溜まって一度に（同じ時刻）、以後は 125 ms ごと + 10 ms
        arr = [300, 310, 310, 310, 510, 635, 760, 885, 1010, 1135, 1260, 1385]
        for i, a in enumerate(arr):
            mono.t = a * ms
            p.feed(self.block(6000), a * ms, clk.t)
        c = [a - 125 * (i + 1) for i, a in enumerate(arr)]
        # 最初の窓（偽の間引きは遅れ無し: 16 kHz の 16000 = 8 ブロック目）まで
        first = c[:8]
        self.assertEqual(p.anchor, min(first))
        D = [ln.split() for ln in out.lines if ln[0] == "D"]
        self.assertEqual(
            [int(d[1]) for d in D],
            [round(min(first) + 250 * k) for k in range(len(D))],
        )
        spread, dmin, dmax = p.clock_stats()
        self.assertAlmostEqual(spread, max(first) - min(first))
        self.assertAlmostEqual(dmin, min(c) - min(first))
        self.assertAlmostEqual(dmax, max(c) - min(first))
        # 再生では anchor = 0
        p2, clk2, mono2, out2 = self.make(live=False)
        for i in range(10):
            p2.feed(self.block(6000), 5_000 * ms, 0)
        self.assertEqual(p2.anchor, 0.0)
        self.assertTrue(out2.lines[0].startswith("D 0 "))


class StaticTest(unittest.TestCase):
    """6.4 の音声の出口が無いことの静的な検査。"""

    PY = [n for n in deploy_files() if n.endswith(".py")]
    AUDIO_WORDS = ("arecord", "/dev/snd", "hw:", "plughw:", "pw-record", "alsa")
    BANNED_MODULES = {"subprocess", "ctypes", "pty"}

    def tree(self, name):
        return ast.parse((HERE / name).read_text(encoding="utf-8"))

    def test_s1_only_audio_in_touches_audio(self):
        self.assertIn("audio_in.py", self.PY)
        self.assertIn("detector.py", self.PY)
        for name in self.PY:
            if name == "audio_in.py":
                continue
            with self.subTest(name=name):
                text = (HERE / name).read_text(encoding="utf-8")
                for w in self.AUDIO_WORDS:
                    self.assertNotIn(w, text)
                for node in ast.walk(self.tree(name)):
                    if isinstance(node, ast.Import):
                        mods = {a.name.split(".")[0] for a in node.names}
                    elif isinstance(node, ast.ImportFrom):
                        mods = {(node.module or "").split(".")[0]}
                    else:
                        mods = set()
                    self.assertFalse(mods & self.BANNED_MODULES, mods)
                    if (
                        isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "os"
                    ):
                        self.assertFalse(
                            node.attr in ("system", "popen")
                            or node.attr.startswith(("exec", "spawn", "fork")),
                            node.attr,
                        )

    def test_s2_no_writes(self):
        write_attrs = {
            "save",
            "savez",
            "savez_compressed",
            "savetxt",
            "tofile",
            "write_text",
            "write_bytes",
        }
        for name in self.PY:
            with self.subTest(name=name):
                for node in ast.walk(self.tree(name)):
                    if not isinstance(node, ast.Call):
                        continue
                    f = node.func
                    if isinstance(f, ast.Attribute):
                        self.assertNotIn(f.attr, write_attrs)
                        if (
                            f.attr == "open"
                            and isinstance(f.value, ast.Name)
                            and f.value.id == "os"
                        ):
                            flags = ast.unparse(node)
                            self.assertNotRegex(
                                flags, r"O_(WRONLY|RDWR|CREAT|APPEND|TRUNC)"
                            )
                    if isinstance(f, ast.Name) and f.id == "open":
                        args = list(node.args[1:2]) + [
                            k.value for k in node.keywords if k.arg == "mode"
                        ]
                        # モードを省いた open は読むモード（"r"）
                        for m in args:
                            self.assertIsInstance(
                                m,
                                ast.Constant,
                                f"{name}: open のモードが定数でない",
                            )
                            self.assertNotRegex(str(m.value), "[wax+]")

    def test_s3_arecord_argv_is_constant_and_writes_to_stdout(self):
        self.assertEqual(
            audio_in.ARECORD_ARGS,
            (
                "-f", "S16_LE", "-r", "48000", "-c", "2", "-t", "raw",
                "-B", "2000000", "-F", "125000", "-v", "-",
            ),
        )  # fmt: skip
        self.assertEqual(list(audio_in.ARECORD_ARGS), record.ARECORD_ARGS)
        self.assertEqual(audio_in.PCM_RE, record.THROAT_DEVICE_RE)
        argv = audio_in.arecord_argv("hw:CARD=Audio,DEV=0")
        self.assertEqual(argv[:3], ["arecord", "-D", "hw:CARD=Audio,DEV=0"])
        self.assertEqual(argv[-1], "-")
        self.assertEqual(sum(1 for a in argv if a == "-"), 1)
        # Popen は 1 か所だけで、stdout は PIPE
        t = (HERE / "audio_in.py").read_text(encoding="utf-8")
        self.assertEqual(t.count("subprocess.Popen("), 1)
        self.assertIn("stdout=subprocess.PIPE", t)

    def test_s4_stdout_only_in_writer_and_lines_from_emit(self):
        tree = self.tree("detector.py")
        funcs = {
            n.name: n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.ClassDef))
        }
        stdout_uses = []
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == "stdout"
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "sys"
                ):
                    stdout_uses.append(fn.name)
                if isinstance(node, ast.Call) and isinstance(
                    node.func, ast.Name
                ):
                    self.assertNotEqual(node.func.id, "print")
        self.assertEqual(stdout_uses, ["_writer"])
        # out.write( を呼ぶのは _writer だけ
        writers = set()
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "write"
                ):
                    writers.add((fn.name, ast.unparse(node.func.value)))
        self.assertEqual(writers, {("_writer", "out"), ("_log", "sys.stderr")})
        # キューに入れる行は emit(...) の戻り値だけ
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "put"
                and ast.unparse(node.func.value) in ("out", "self.out")
            ):
                arg = node.args[0]
                self.assertTrue(
                    isinstance(arg, ast.Call)
                    and isinstance(arg.func, ast.Name)
                    and arg.func.id == "emit",
                    ast.unparse(node),
                )
        # mcu_rpc の McuLink に渡すのは set_state だけ
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and ast.unparse(node.func.value) in ("self.link", "link")
        }
        self.assertEqual(calls, {"set_state", "close"})
        self.assertIn("emit", funcs)
        self.assertIn("_writer", funcs)

    def test_s5_core_dump_disabled_and_env_before_numpy(self):
        tree = self.tree("detector.py")
        body = tree.body
        first_numpy = next(
            i
            for i, n in enumerate(body)
            if isinstance(n, ast.Import)
            and any(a.name == "numpy" for a in n.names)
        )
        head = "\n".join(ast.unparse(n) for n in body[:first_numpy])
        self.assertIn("resource.setrlimit(resource.RLIMIT_CORE, (0, 0))", head)
        for k in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
            self.assertIn(f"os.environ['{k}'] = '1'", head)


if __name__ == "__main__":
    unittest.main()
