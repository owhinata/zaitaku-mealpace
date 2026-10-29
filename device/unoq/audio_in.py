"""検出器（detector.py）の音声の入力（Issue #47、plan #47 の 6.1）。

ALSA と音声の入力を扱うのはこのモジュールだけ（docs/decisions/0025・0035）。

ライブ（LiveInput）: arecord を子プロセスで起動し、標準出力のパイプをメモリで
  読む。arecord の出力先は標準出力（-）だけで、引数は ARECORD_ARGS に固定する
  （tools/record.py の ARECORD_ARGS と同じ値）。PCM 名は hw / plughw の
  カード・デバイス指定だけ（PCM_RE。tee: や file: のプラグインは入力をファイル
  にも複製できるので受けない。0028 決定 2）。標準エラーは daemon のスレッドで
  行ごとに読み、overrun の行を数える（中身は音声ではない）。
再生（ReplayInput）: 標準入力から同じ形（S16_LE、48 kHz、2 ch のインター
  リーブ）のバイト列を読む。EOF で終わる。ファイルを開かない（パスを受けない）。

コアダンプの抑止（disable_dumps）とディスクのスワップの確かめ（disk_swaps）もここに置く
（音声をメモリに持つプロセスの守り。ctypes を使うのはこのモジュールだけ）。
どちらも read() が返すのは「48 kHz の L（ch0）を float64 にした 1 ブロック
（array('d')）」と、そのブロックを読み終えた時刻だけ。4 バイトに満たない端は次に
持ち越す。バイト列・配列をどこにも書かない・出さない。標準ライブラリだけ（numpy
を import しない）。
"""

import collections
import ctypes
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
from array import array

RATE_HZ = 48000
CHANNELS = 2
FRAME_BYTES = 4  # S16_LE × 2 ch
READ_BYTES = 65536
# tools/record.py の THROAT_DEVICE_RE と同じ
PCM_RE = r"(plug)?hw:(CARD=[A-Za-z0-9_]+(,DEV=[0-9]+)?|[0-9]+(,[0-9]+)?)"
DEFAULT_DEVICE = "hw:CARD=Audio,DEV=0"
ARECORD = "arecord"
# 出力先は標準出力（-）だけ。-q は付けない（overrun の行を出させる）。
# tools/record.py の ARECORD_ARGS と同じ値（-B 2 秒のバッファ、-F 125 ms）
ARECORD_ARGS = (
    "-f", "S16_LE", "-r", "48000", "-c", "2", "-t", "raw",
    "-B", "2000000", "-F", "125000", "-v", "-",
)  # fmt: skip
STOP_WAIT_S = 2.0
STDERR_TAIL = 8
# 検出器の E 行で子プロセスの CPU 時間を出す鍵（plan #47 の 6.2）。子の名前を
# detector.py に書かないため、ここに置く（6.4 の静的な検査）
CPU_KEY = ARECORD + "_cpu_s"


PR_SET_DUMPABLE = 4  # <linux/prctl.h>
SWAPS_PATH = "/proc/swaps"


def disable_dumps():
    """このプロセスのコアダンプを止める（prctl(PR_SET_DUMPABLE, 0)）。RLIMIT_CORE
    0 だけでは core_pattern がパイプ（apport など）のとき kernel が
    メモリを収集のプログラムに渡すので、dumpable を落としてダンプ自体を作らせない
    （adversarial-review の指摘、#47）。成功したら True。"""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


SYS_BLOCK = "/sys/block"
CORE_PATTERN_PATH = "/proc/sys/kernel/core_pattern"


def _zram_writeback(name):
    """zram のスワップがディスクに書き戻せる設定か、確かめられないなら True
    （安全側）。/sys/block/<zram>/ が見えないなら True。その中に backing_dev が
    無い（書き戻しを持たない kernel）なら False、none 以外なら True。"""
    dev = name.rsplit("/", 1)[-1]
    if not os.path.isdir(f"{SYS_BLOCK}/{dev}"):
        return True
    try:
        with open(f"{SYS_BLOCK}/{dev}/backing_dev", "r") as f:
            return f.read().strip() not in ("", "none")
    except FileNotFoundError:
        return False
    except OSError:
        return True


def disk_swaps(path=None):
    """ディスクに出うるスワップの名前の並び（無ければ空）。読めなければ None。
    zram は書き戻し（backing_dev）が無いときだけ受ける。メモリの音声がディスクの
    スワップに出ないように、入力を開く前に確かめる。"""
    try:
        with open(SWAPS_PATH if path is None else path, "r") as f:
            lines = f.read().splitlines()[1:]
        names = [ln.split()[0] for ln in lines if ln.strip()]
        return [
            n
            for n in names
            if not n.startswith("/dev/zram") or _zram_writeback(n)
        ]
    except OSError:
        return None


def core_pattern_ok(path=None):
    """(受けるか, core_pattern)。ファイルに書く形（先頭が | でない）だけを受ける
    （kernel が RLIMIT_CORE 0 を守るので書かない）。パイプの形はすべて受けない
    （子の arecord は exec で dumpable が戻り、RLIMIT_CORE 0 だけが効くが、パイプの
    収集には RLIMIT_CORE が効かないため）。UNO Q の実物は `core`（10/29）。"""
    try:
        with open(CORE_PATTERN_PATH if path is None else path, "r") as f:
            pat = f.read().strip()
    except OSError:
        return False, ""
    return (bool(pat) and not pat.startswith("|")), pat


class InputError(Exception):
    """入力を開けない・引数が受けられない。"""


def check_device(device):
    """PCM 名の検査。受けない値なら InputError（arecord を起動する前）。"""
    if not isinstance(device, str) or not re.fullmatch(PCM_RE, device):
        raise InputError(
            "--device は hw:CARD=<名前>[,DEV=<番号>] / hw:<番号>[,<番号>]"
            f"（plughw: も可）だけ: {device!r}"
        )
    return device


def arecord_argv(device):
    """arecord の argv（出力先は - だけ）。"""
    return [ARECORD, "-D", check_device(device), *ARECORD_ARGS]


class _Deinterleave:
    """S16_LE・2 ch のバイト列 → L（ch0）の array('d')。端は持ち越す。"""

    def __init__(self):
        self._rest = b""
        self.frames = 0

    def push(self, data):
        buf = self._rest + data
        n = len(buf) // FRAME_BYTES * FRAME_BYTES
        self._rest = buf[n:]
        if n == 0:
            return array("d")
        a = array("h")
        a.frombytes(buf[:n])
        if sys.byteorder != "little":
            a.byteswap()
        self.frames += n // FRAME_BYTES
        return array("d", a[::CHANNELS])


class _Input:
    """read(timeout) -> (L の array('d'), 到着の monotonic_ns, perf_counter_ns)
    か None（timeout の間に何も無い・端だけ）。EOF なら eof を立てて None。"""

    kind = ""

    def __init__(self, fd):
        self.fd = fd
        self.eof = False
        self._dec = _Deinterleave()

    @property
    def frames(self):
        return self._dec.frames

    def fileno(self):
        return self.fd

    def read(self, timeout):
        if self.eof:
            return None
        if (
            timeout is not None
            and not select.select([self.fd], [], [], timeout)[0]
        ):
            return None
        data = os.read(self.fd, READ_BYTES)
        mono, perf = time.monotonic_ns(), time.perf_counter_ns()
        if not data:
            self.eof = True
            return None
        block = self._dec.push(data)
        del data
        if not block:
            return None
        return block, mono, perf

    def close(self):
        pass


class ReplayInput(_Input):
    """標準入力（既定 sys.stdin.buffer の fd）から読む。ファイルを開かない。"""

    kind = "replay"

    def __init__(self, fd=None):
        super().__init__(sys.stdin.buffer.fileno() if fd is None else fd)


class LiveInput(_Input):
    """arecord の子プロセスの標準出力から読む。argv はテストのときだけ差し替える
    （PC の python3 で PCM を出す偽の arecord。CLI からは差し替えられない）。"""

    kind = "live"

    def __init__(self, device=DEFAULT_DEVICE, argv=None):
        check_device(device)
        self.device = device
        self.argv = arecord_argv(device) if argv is None else list(argv)
        try:
            self.proc = subprocess.Popen(
                self.argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as e:
            raise InputError(f"arecord を起動できません: {e}") from None
        super().__init__(self.proc.stdout.fileno())
        self.overrun_lines = 0
        self.tail = collections.deque(maxlen=STDERR_TAIL)
        self._err = threading.Thread(target=self._read_stderr, daemon=True)
        self._err.start()

    def _read_stderr(self):
        try:
            for raw in self.proc.stderr:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if "overrun" in line.lower():
                    self.overrun_lines += 1
                self.tail.append(line)
        except (OSError, ValueError):
            pass

    def stderr_tail(self):
        self._err.join(timeout=0.5)
        return list(self.tail)

    def cpu_s(self):
        """arecord の CPU 時間（utime + stime、秒）。/proc/<pid>/stat を読む
        だけ。読めなければ None。"""
        try:
            with open(f"/proc/{self.proc.pid}/stat", "r") as f:
                text = f.read()
            fields = text.rsplit(")", 1)[1].split()
            ticks = int(fields[11]) + int(fields[12])
            return ticks / os.sysconf("SC_CLK_TCK")
        except (OSError, ValueError, IndexError):
            return None

    def close(self):
        """SIGTERM → STOP_WAIT_S 秒待って SIGKILL。何度呼んでもよい。"""
        if self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.SIGTERM)
                self.proc.wait(timeout=STOP_WAIT_S)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            except ProcessLookupError:
                pass
        for f in (self.proc.stdout, self.proc.stderr):
            try:
                f.close()
            except Exception:
                pass
