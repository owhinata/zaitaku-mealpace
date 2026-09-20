#!/usr/bin/env python3
"""装置からのバイナリフレームを受けて、セッションフォルダに書く（docs/data-schema.md）。

キー入力でマーカーを打つ（tty があるときだけ）:
  s 嚥下  t 発話  c 咳  n 首の動き  q 静止  b 一口  o 観察（次の行を note に）  Ctrl-C 終了

--duration <秒> を付けると、その秒数で自分から終了する（Ctrl-C と同じ後始末を通る）。
SUBJECT / COND / POSITION / BAND / DURATION の環境変数を、対応する引数の既定値として読む。
出力先はリポジトリの data/raw/ 固定。変える引数は置かない（docs/decisions/0007）。

フレーム: [A5 5A][id u8][len u16 LE][t_ms u32 LE][payload][xor u8]
  id 0x01 IMU (float32 x6)  0x02 AUDIO (int16 x N)  0x03 ANALOG (uint16 x N)  0x7F META (JSON)
"""
from __future__ import annotations
import argparse, atexit, codecs, contextlib, csv, json, os, re, signal, struct, subprocess, sys, threading, time, wave
from datetime import datetime
from pathlib import Path

if os.name != "nt":
    import select, termios, tty

import serial

SYNC = b"\xa5\x5a"
ID_IMU, ID_AUDIO, ID_ANALOG, ID_META = 0x01, 0x02, 0x03, 0x7F
AUDIO_HZ, IMU_HZ = 16000, 104
STREAM_NAMES = {ID_IMU: "IMU", ID_AUDIO: "AUDIO", ID_ANALOG: "ANALOG", ID_META: "META"}
RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"


def env_default(name: str, fallback: str) -> str:
    """環境変数を引数の既定値にする。空文字列は未指定として扱う（CMake から渡る場合用）。"""
    v = os.environ.get(name, "")
    return v if v else fallback


class TtyMode:
    """端末の設定を保存し、メインのスレッドから戻す（POSIX）。

    入力スレッドは daemon で、プロセス終了時に finally を実行せずに打ち切られる。
    cbreak の解除をそのスレッドに任せると端末が cbreak のまま残るので、保存と復元は
    メインで持つ。`restore()` は `enter()` の前でも、何度呼んでもよい。
    """

    def __init__(self, fd: int):
        self.fd = fd
        self.closed = False       # 終了処理が始まった印。入力スレッドはこれを見て抜ける
        self._old = None          # enter() の前の設定
        self._lock = threading.Lock()

    def enter(self) -> None:
        """今の設定を保存して cbreak にする。メインのスレッドで呼ぶ。"""
        with self._lock:
            if self.closed or self._old is not None:
                return
            self._old = termios.tcgetattr(self.fd)
            # 既定の TCSAFLUSH は先行入力を捨てるので TCSADRAIN にする。
            tty.setcbreak(self.fd, termios.TCSADRAIN)

    def restore(self) -> None:
        """終了処理の印を立てて、保存した設定に戻す。何度呼んでもよい。"""
        with self._lock:
            self.closed = True
            self._to_old()

    def _to_old(self) -> None:
        if self._old is None:
            return
        try:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self._old)
        except Exception:
            pass

    @contextlib.contextmanager
    def cooked(self):
        """元の設定（行の編集とエコー）に戻して yield し、終了処理が始まっていなければ cbreak に戻す。"""
        with self._lock:
            self._to_old()
        try:
            yield
        finally:
            with self._lock:
                if not self.closed and self._old is not None:
                    try:
                        tty.setcbreak(self.fd, termios.TCSADRAIN)
                    except Exception:
                        pass


class KeyReader:
    """POSIX の 1 文字入力ループ。select で待つので、終了の印を見て自分で止まれる。"""

    def __init__(self, fd: int, mode: TtyMode, on_key, on_note):
        self.fd = fd
        self.mode = mode
        self.on_key = on_key
        self.on_note = on_note
        self._dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buf = ""            # 読んだが、まだ処理していない文字
        self._eof = False

    def _stop(self) -> bool:
        return self.mode.closed or self._eof

    def _fill(self, timeout: float = 0.1) -> None:
        """読めるものがあれば読んで _buf に足す。無ければ timeout 秒で戻る。"""
        try:
            if not select.select([self.fd], [], [], timeout)[0]:
                return
            data = os.read(self.fd, 1024)
        except (OSError, ValueError):
            self._eof = True
            return
        if not data:
            self._eof = True
            return
        self._buf += self._dec.decode(data)

    def run(self) -> None:
        while not self._stop():
            if not self._buf:
                self._fill()
                continue
            ch, self._buf = self._buf[0], self._buf[1:]
            if ch == "o":
                self._read_note()
            else:
                self.on_key(ch)

    def _read_note(self) -> None:
        """o の note を1行読む。元の設定の間は、端末が行の編集とエコーを受け持つ。"""
        with self.mode.cooked():
            print("note: ", end="", flush=True)
            while "\n" not in self._buf:
                if self._stop():
                    return        # 打ち終える前に終了した。この o は記録しない
                self._fill()
            note, _, self._buf = self._buf.partition("\n")   # 改行より後ろは通常のキーに回す
            self.on_note(note.rstrip("\r"))                  # 時刻は打ち終えた時点（今までと同じ）


def getch_loop_nt(on_key):
    """Windows の 1 文字入力ループ。端末の設定は変えない。"""
    import msvcrt
    while True:
        on_key(msvcrt.getwch())


def raise_system_exit(signum, frame):
    """SIGTERM を SystemExit に変える。main の finally（端末の復元）を通してから終わる。"""
    raise SystemExit(128 + signum)


def git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


class Session:
    def __init__(self, out: Path, subject: str, cond: str, position: str, band: str):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = out / f"{stamp}_{subject}_{cond}"
        # セッションは out の直下にだけ作る。cond に ../ が入っても外に出さない（docs/decisions/0007）。
        if self.dir.resolve().parent != out.resolve():
            sys.exit(f"セッションフォルダが {out} の直下になりません: {self.dir}")
        self.dir.mkdir(parents=True, exist_ok=False)
        self.imu = open(self.dir / "imu.csv", "w", newline="", encoding="utf-8")
        self.imu_w = csv.writer(self.imu); self.imu_w.writerow(["t_ms", "ax", "ay", "az", "gx", "gy", "gz"])
        self.ev = open(self.dir / "events.csv", "w", newline="", encoding="utf-8")
        self.ev_w = csv.writer(self.ev); self.ev_w.writerow(["t_ms", "label", "note"])
        self.chunks = open(self.dir / "audio_chunks.csv", "w", newline="", encoding="utf-8")
        self.chunks_w = csv.writer(self.chunks); self.chunks_w.writerow(["t_ms", "sample_index"])
        self.wav = wave.open(str(self.dir / "audio.wav"), "wb")
        self.wav.setnchannels(1); self.wav.setsampwidth(2); self.wav.setframerate(AUDIO_HZ)
        self.analog = None
        self.n_audio = 0
        self.last_t_ms = 0
        self._lock = threading.Lock()   # mark()（入力スレッド）と close()（メイン）を守る
        self._closed = False
        self.meta = {
            "subject": subject, "cond": cond, "position": position, "band": band,
            "firmware_sha": git_sha(),
            "sample_rates": {"imu_hz": IMU_HZ, "audio_hz": AUDIO_HZ, "analog_hz": 0},
            "sensors": [
                {"id": "imu", "part": "LSM6DSOX", "iface": "onboard"},
                {"id": "mic", "part": "MP34DT06JTR", "iface": "onboard-pdm"},
            ],
            "notes": "",
        }

    def on_frame(self, sid: int, t_ms: int, payload: bytes):
        self.last_t_ms = t_ms
        if sid == ID_IMU and len(payload) == 24:
            self.imu_w.writerow([t_ms] + [f"{v:.4f}" for v in struct.unpack("<6f", payload)])
        elif sid == ID_AUDIO:
            self.chunks_w.writerow([t_ms, self.n_audio])
            self.wav.writeframes(payload)
            self.n_audio += len(payload) // 2
        elif sid == ID_ANALOG:
            if self.analog is None:
                self.analog = open(self.dir / "analog.csv", "w", newline="", encoding="utf-8")
                self.analog_w = csv.writer(self.analog)
                n = len(payload) // 2
                self.analog_w.writerow(["t_ms"] + [f"ch{i}" for i in range(n)])
            self.analog_w.writerow([t_ms] + list(struct.unpack(f"<{len(payload)//2}H", payload)))
        elif sid == ID_META:
            try:
                self.meta.update(json.loads(payload.decode("utf-8")))
            except Exception:
                pass

    def mark(self, label: str, note: str = ""):
        """マーカーを1行書く。close() の後は何もしない（入力スレッドが遅れて呼ぶことがある）。"""
        with self._lock:
            if self._closed:
                return
            self.ev_w.writerow([self.last_t_ms, label, note]); self.ev.flush()
            print(f"  [{self.last_t_ms} ms] {label} {note}")

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for f in (self.imu, self.ev, self.chunks, self.analog):
                if f: f.close()
            self.wav.close()
        (self.dir / "meta.json").write_text(json.dumps(self.meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved: {self.dir}")


def read_frames(ser: serial.Serial, on_frame, stats: dict, deadline: float | None = None):
    """フレームを読み続ける。

    stats["ok"][stream_id] に**正常受信フレーム数**（xor 検証を通って on_frame に渡った数）、
    stats["xor_err"] に **XOR 不一致数**（SYNC と長さと xor バイトまで読めたが xor が合わずに
    捨てたフレーム候補の数）を数える。deadline（time.monotonic の値）を過ぎたら戻る。
    """
    buf = bytearray()
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            return
        buf += ser.read(4096)
        while True:
            i = buf.find(SYNC)
            if i < 0:
                buf.clear(); break
            if len(buf) < i + 9:
                del buf[:i]; break
            sid = buf[i + 2]
            ln, t_ms = struct.unpack_from("<HI", buf, i + 3)
            end = i + 9 + ln + 1
            if len(buf) < end:
                del buf[:i]; break
            payload = bytes(buf[i + 9:i + 9 + ln])
            x = 0
            for b in buf[i + 2:i + 9 + ln]:
                x ^= b
            if x == buf[end - 1]:
                stats["ok"][sid] = stats["ok"].get(sid, 0) + 1
                on_frame(sid, t_ms, payload)
            else:
                stats["xor_err"] += 1
            del buf[:end]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default=os.environ.get("PORT", "/dev/ttyACM0"))
    ap.add_argument("--baud", type=int, default=2000000)
    ap.add_argument("--subject", default=env_default("SUBJECT", "self"))
    ap.add_argument("--cond", default=env_default("COND", "water"))
    ap.add_argument("--position", default=env_default("POSITION", "midline-below-thyroid"))
    ap.add_argument("--band", default=env_default("BAND", "elastic-25mm"))
    ap.add_argument("--duration", type=float, default=float(env_default("DURATION", "0")),
                    help="秒。0 なら Ctrl-C で止めるまで回る")
    a = ap.parse_args()
    if a.subject not in ("self", "p1"):
        sys.exit("subject は self か p1")
    if not re.fullmatch(r"[A-Za-z0-9-]+", a.cond):
        sys.exit("cond は英数字とハイフンのみ")

    sess = Session(RAW_DIR, a.subject, a.cond, a.position, a.band)
    ser = serial.Serial(a.port, a.baud, timeout=0.05)
    stats = {"ok": {}, "xor_err": 0}
    deadline = time.monotonic() + a.duration if a.duration > 0 else None

    def on_key(ch):
        if ch in "stcnqb":
            sess.mark(ch)
        elif ch == "o":
            # POSIX の o は KeyReader が扱う。ここを通るのは Windows の経路だけ。
            sess.mark("o", input("note: "))

    is_tty = sys.stdin.isatty()
    tty_mode = TtyMode(sys.stdin.fileno()) if is_tty and os.name != "nt" else None
    if tty_mode is not None:
        atexit.register(tty_mode.restore)        # 端末を変える前に登録する
    signal.signal(signal.SIGTERM, raise_system_exit)   # SIGTERM でも下の finally を通る

    if is_tty:
        print("recording... keys: s t c n q b o / Ctrl-C to stop")
    else:
        print("recording... tty が無いのでマーカー入力は無効")
    if deadline is not None:
        print(f"  --duration {a.duration:g} 秒で自動終了する")

    thread = None
    try:
        if tty_mode is not None:
            tty_mode.enter()
            reader = KeyReader(sys.stdin.fileno(), tty_mode, on_key, lambda note: sess.mark("o", note))
            thread = threading.Thread(target=reader.run, daemon=True)
            thread.start()
        elif is_tty:
            thread = threading.Thread(target=getch_loop_nt, args=(on_key,), daemon=True)
            thread.start()
        read_frames(ser, sess.on_frame, stats, deadline)
    except KeyboardInterrupt:
        pass
    finally:
        if tty_mode is not None:
            tty_mode.restore()                   # 印を立てて端末を戻す
            if thread is not None:
                thread.join(timeout=1.0)         # 止まらなくても終了は妨げない（daemon のまま）
        ser.close(); sess.close()
        print("正常受信フレーム数:")
        for sid in sorted(stats["ok"]):
            print(f"  {STREAM_NAMES.get(sid, f'0x{sid:02X}')}: {stats['ok'][sid]}")
        print(f"XOR 不一致数: {stats['xor_err']}")


if __name__ == "__main__":
    main()
