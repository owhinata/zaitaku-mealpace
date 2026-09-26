#!/usr/bin/env python3
"""装置からのバイナリフレームを受けて、セッションフォルダに書く（docs/data-schema.md）。

キー入力でマーカーを打つ（tty があるときだけ）:
  s 嚥下  t 発話  c 咳  n 首の動き  q 静止  b 一口  o 観察（次の行を note に）  Ctrl-C 終了

--duration <秒> を付けると、その秒数で自分から終了する（Ctrl-C と同じ後始末を通る）。
SUBJECT / COND / POSITION / BAND / DURATION の環境変数を、対応する引数の既定値として読む。
出力先はリポジトリの data/raw/ 固定。変える引数は置かない（docs/decisions/0007）。

フレーム: [A5 5A][id u8][len u16 LE][t_ms u32 LE][payload][xor u8]
  id 0x01 IMU (float32 x6)  0x02 AUDIO (int16 x N)  0x03 ANALOG (uint16 x N)  0x7F META (JSON)
  id 0x04 DETECT (window_t_ms u32, prob float32, positive u8, led u8)  → detect.csv（検出器のみ）
  id 0x05 FEAT (window_t_ms u32, float32 x N。正規化前の特徴量)         → feat.csv（検出器のみ）
ストリームのファイルは、そのストリームの最初のフレームが届いたときに作る。events.csv と meta.json は常に作る
（docs/decisions/0021）。

--indicator <port> を付けると、DETECT を受けるたびに led（0 消灯 / 1 黄 / 2 緑）の 1 バイトだけを表示器
（firmware/indicator/、Issue #29、docs/decisions/0018 の追記）へ送る。時刻・確率・特徴量は送らない。
記録を始める前に表示器のポートが開けなければ、記録を始めない（セッションのフォルダを作らない）。
記録を始めた後に送れなくなっても（USB が抜けたなど）記録は止めない（終了時に失敗の回数を表示する）。
送信は別のスレッドで行い、フレームの読み取りとマーカーは表示器への書き込みを待たない。送った記録はファイルに残さず、
書くファイル・列・出力先は --indicator の有無で変わらない。
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
ID_DETECT, ID_FEAT = 0x04, 0x05
AUDIO_HZ, IMU_HZ = 16000, 104
STREAM_NAMES = {ID_IMU: "IMU", ID_AUDIO: "AUDIO", ID_ANALOG: "ANALOG", ID_DETECT: "DETECT", ID_FEAT: "FEAT",
                ID_META: "META"}
IMU_PAYLOAD_LEN, DETECT_PAYLOAD_LEN = 24, 10
FLOAT_FMT = ".9g"   # float32 が往復で一致する桁数（prob は閾値との比較を PC 側で丸めなしに再現するため）
RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
INDICATOR_BAUD = 115200            # 表示器のポート。USB CDC では値は使われない。1200 は使わない（ブートローダに入る）
INDICATOR_WRITE_TIMEOUT_S = 0.02   # 表示器が詰まったとき、送信のスレッドの 1 回の書き込みが止まる時間の上限


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
        # events.csv は常に作る。ストリームのファイルは最初のフレームが届いたときに作る（docs/decisions/0021）。
        self.ev = open(self.dir / "events.csv", "w", newline="", encoding="utf-8")
        self.ev_w = csv.writer(self.ev); self.ev_w.writerow(["t_ms", "label", "note"])
        self.imu = self.chunks = self.wav = self.analog = self.detect = self.feat = None
        self.n_feat = None            # FEAT の次元数 N。最初の FEAT フレームで決まる
        self.bad_len = {}             # stream_id → 長さが合わずに書かなかったフレーム数
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

    def _open_csv(self, name: str, header: list[str]):
        f = open(self.dir / name, "w", newline="", encoding="utf-8")
        w = csv.writer(f); w.writerow(header)
        return f, w

    def _open_imu(self):
        self.imu, self.imu_w = self._open_csv("imu.csv", ["t_ms", "ax", "ay", "az", "gx", "gy", "gz"])

    def _open_audio(self):
        self.chunks, self.chunks_w = self._open_csv("audio_chunks.csv", ["t_ms", "sample_index"])
        self.wav = wave.open(str(self.dir / "audio.wav"), "wb")
        self.wav.setnchannels(1); self.wav.setsampwidth(2); self.wav.setframerate(AUDIO_HZ)

    def _open_analog(self, n: int):
        self.analog, self.analog_w = self._open_csv("analog.csv", ["t_ms"] + [f"ch{i}" for i in range(n)])

    def _open_detect(self):
        self.detect, self.detect_w = self._open_csv("detect.csv", ["t_ms", "window_t_ms", "positive", "prob", "led"])

    def _open_feat(self, n: int):
        self.n_feat = n
        self.feat, self.feat_w = self._open_csv("feat.csv", ["t_ms", "window_t_ms"] + [f"f{i}" for i in range(n)])

    def _count_bad_len(self, sid: int):
        self.bad_len[sid] = self.bad_len.get(sid, 0) + 1

    def on_frame(self, sid: int, t_ms: int, payload: bytes):
        self.last_t_ms = t_ms     # マーカーの時刻。stream_id を問わず、直前に届いたフレームの送信時刻
        if sid == ID_IMU:
            if len(payload) != IMU_PAYLOAD_LEN:
                self._count_bad_len(sid); return
            if self.imu is None:
                self._open_imu()
            self.imu_w.writerow([t_ms] + [f"{v:.4f}" for v in struct.unpack("<6f", payload)])
        elif sid == ID_AUDIO:
            if self.wav is None:
                self._open_audio()
            self.chunks_w.writerow([t_ms, self.n_audio])
            self.wav.writeframes(payload)
            self.n_audio += len(payload) // 2
        elif sid == ID_ANALOG:
            if self.analog is None:
                self._open_analog(len(payload) // 2)
            self.analog_w.writerow([t_ms] + list(struct.unpack(f"<{len(payload)//2}H", payload)))
        elif sid == ID_DETECT:
            if len(payload) != DETECT_PAYLOAD_LEN:
                self._count_bad_len(sid); return
            window_t_ms, prob, positive, led = struct.unpack("<IfBB", payload)
            if self.detect is None:
                self._open_detect()
            self.detect_w.writerow([t_ms, window_t_ms, positive, f"{prob:{FLOAT_FMT}}", led])
        elif sid == ID_FEAT:
            if len(payload) < 8 or (len(payload) - 4) % 4 != 0:
                self._count_bad_len(sid); return
            n = (len(payload) - 4) // 4
            if self.feat is None:
                self._open_feat(n)
            elif n != self.n_feat:
                self._count_bad_len(sid); return
            values = struct.unpack(f"<I{n}f", payload)
            self.feat_w.writerow([t_ms, values[0]] + [f"{v:{FLOAT_FMT}}" for v in values[1:]])
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
            for f in (self.imu, self.ev, self.chunks, self.analog, self.detect, self.feat, self.wav):
                if f: f.close()
        (self.dir / "meta.json").write_text(json.dumps(self.meta, ensure_ascii=False, indent=2), encoding="utf-8")
        names = self.meta.get("feature_names")
        if self.n_feat is not None and isinstance(names, list) and len(names) != self.n_feat:
            # meta.json は装置の値のまま書く。直さない
            print(f"警告: meta.json の feature_names は {len(names)} 個ですが、feat.csv の次元数は {self.n_feat} です")
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


def detect_led(payload: bytes) -> int | None:
    """DETECT のペイロードから led を取り出す。長さが違う、または 0〜2 でなければ None（送らない）。"""
    if len(payload) != DETECT_PAYLOAD_LEN:
        return None
    led = payload[DETECT_PAYLOAD_LEN - 1]     # "<IfBB" の最後のバイト
    return led if led in (0, 1, 2) else None


class IndicatorForwarder:
    """DETECT の led を表示器へ 1 バイトずつ送る（Issue #29）。書き込みは送信のスレッド（daemon）だけが行う。

    読み取りのループから呼ぶ offer() はロックの中で最新の値を置いて Event を立てるだけで、シリアルに触らない
    （読み取りのループとマーカーが書き込みを待つ経路をなくす）。送れなくても例外を外に出さない（記録を止めない）。
    送るのは led の値だけで、時刻・確率・特徴量は送らない。送った記録はファイルに残さない。
    件数（summary_lines）は close() の後に読む。
    """

    def __init__(self, ser):
        self.ser = ser
        self.sent = 0          # 送れた回数
        self.failed = 0        # 例外、または書けたバイト数が 1 でなかった回数
        self.skipped = 0       # 送らなかった DETECT（長さ違い、led が 0〜2 でない）
        self.replaced = 0      # 送る前に次の値で置き換えた回数（送信が詰まっていた間の古い値。最新の値だけを送る）
        self.first_error = None
        self.thread_stopped = True   # close() の join の後も送信のスレッドが生きていれば False（件数は確定しない）
        self._lock = threading.Lock()
        self._pending = None         # 送っていない最新の値か None
        self._event = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        """送信のスレッドを始める（daemon）。"""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def offer(self, payload: bytes) -> None:
        """読み取りのループから呼ぶ。DETECT のペイロードから led を取り、最新の値として置く。シリアルには触らない。"""
        led = detect_led(payload)
        with self._lock:
            if led is None:
                self.skipped += 1
                return
            if self._pending is not None:
                self.replaced += 1
            self._pending = led
        self._event.set()

    def _run(self) -> None:
        """送信のスレッド。Event を待ち（0.1 秒ごとに止める印を見る）、最新の値を取り出して書く。"""
        while not self._stop.is_set():
            if not self._event.wait(timeout=0.1):
                continue
            with self._lock:
                self._event.clear()
                led, self._pending = self._pending, None
            if led is None or self._stop.is_set():
                continue
            try:
                n = self.ser.write(bytes((led,)))
                if n == 1:
                    self.sent += 1
                else:
                    self._fail(f"書けたバイト数が {n}")
            except (serial.SerialException, OSError) as e:
                self._fail(e)

    def _fail(self, e) -> None:
        self.failed += 1
        if self.first_error is None:
            self.first_error = str(e)
            print(f"警告: 表示器に送れませんでした（記録は続けます）: {e}")

    def close(self) -> None:
        """送信のスレッドを止めて（join は 1.0 秒まで。止まらなくても終了は妨げない）、ポートを閉じる。
        止める時点で送っていない値は送らない（終了時に何も送らない。表示器は 1.0 秒で消灯する）。"""
        self._stop.set()
        self._event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self.thread_stopped = not self._thread.is_alive()
        try:
            self.ser.close()
        except Exception:
            pass

    def summary_lines(self) -> list[str]:
        """終了時の表示。close() の後に 1 回だけ読む。"""
        lines = [f"表示器への転送（DETECT の led）: 送った {self.sent} / 失敗 {self.failed} / "
                 f"送らなかった {self.skipped} / 置き換えた {self.replaced}"]
        if self.failed > 0:
            lines.append(f"  最初の失敗: {self.first_error}")
        if not self.thread_stopped:
            lines.append("  送信のスレッドが止まりませんでした（件数は終了時点のもの）")
        return lines


def with_indicator(on_frame, fwd):
    """fwd が None なら on_frame をそのまま返す（--indicator 無しは今までと同じ呼び出し）。
    あれば、on_frame(sid, t_ms, payload) を呼んだ後、DETECT なら fwd.offer(payload) を呼ぶ関数を返す。"""
    if fwd is None:
        return on_frame

    def on_frame_and_offer(sid: int, t_ms: int, payload: bytes):
        on_frame(sid, t_ms, payload)
        if sid == ID_DETECT:
            fwd.offer(payload)
    return on_frame_and_offer


def indicator_port_error(port: str, indicator: str | None) -> str | None:
    """--indicator が --port と同じ実体を指すならエラーの文を返す（検出器にバイトを送らないため）。問題なければ None。"""
    if indicator is None:
        return None
    if os.path.realpath(port) == os.path.realpath(indicator):
        return f"--indicator と --port が同じポートを指しています: {indicator}"
    return None


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
    ap.add_argument("--indicator", default=None,
                    help="表示器のポート（例 /dev/serial/by-id/…）。DETECT の led を 1 バイトずつ送る。既定は送らない。"
                         "記録を始める前に開けなければ記録を始めない。始めた後に送れなくなっても記録は止めない")
    a = ap.parse_args()
    if a.subject not in ("self", "p1"):
        sys.exit("subject は self か p1")
    if not re.fullmatch(r"[A-Za-z0-9-]+", a.cond):
        sys.exit("cond は英数字とハイフンのみ")
    err = indicator_port_error(a.port, a.indicator)
    if err:
        sys.exit(err)

    fwd = None
    if a.indicator is not None:
        # セッションのフォルダを作る前に開く。開けなければ記録を始めない（フォルダが空で残らない）
        try:
            ind_ser = serial.Serial(a.indicator, INDICATOR_BAUD, timeout=0, write_timeout=INDICATOR_WRITE_TIMEOUT_S)
        except (serial.SerialException, OSError) as e:
            sys.exit(f"表示器のポートを開けません（記録を始めません）: {e}")
        fwd = IndicatorForwarder(ind_ser)
        print(f"表示器: {a.indicator} へ LED の状態（DETECT の led）を送る")

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
        if fwd is not None:
            fwd.start()
        read_frames(ser, with_indicator(sess.on_frame, fwd), stats, deadline)
    except KeyboardInterrupt:
        pass
    finally:
        if tty_mode is not None:
            tty_mode.restore()                   # 印を立てて端末を戻す
            if thread is not None:
                thread.join(timeout=1.0)         # 止まらなくても終了は妨げない（daemon のまま）
        ser.close(); sess.close()
        if fwd is not None:
            fwd.close()                          # 送信のスレッドを止めて表示器のポートを閉じる（何も送らない）
        print("正常受信フレーム数:")
        for sid in sorted(stats["ok"]):
            print(f"  {STREAM_NAMES.get(sid, f'0x{sid:02X}')}: {stats['ok'][sid]}")
        print(f"XOR 不一致数: {stats['xor_err']}")
        if sess.bad_len:
            print("長さが合わないフレーム（書いていない）:")
            for sid in sorted(sess.bad_len):
                print(f"  {STREAM_NAMES.get(sid, f'0x{sid:02X}')}: {sess.bad_len[sid]}")
        if fwd is not None:
            for line in fwd.summary_lines():
                print(line)


if __name__ == "__main__":
    main()
