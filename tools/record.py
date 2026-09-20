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
import argparse, csv, json, os, re, struct, subprocess, sys, threading, time, wave
from datetime import datetime
from pathlib import Path

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


def getch_loop(on_key):
    """POSIX/Windows 両対応の 1 文字入力ループ。"""
    if os.name == "nt":
        import msvcrt
        while True:
            ch = msvcrt.getwch()
            on_key(ch)
    else:
        import termios, tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while True:
                on_key(sys.stdin.read(1))
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


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
        self.ev_w.writerow([self.last_t_ms, label, note]); self.ev.flush()
        print(f"  [{self.last_t_ms} ms] {label} {note}")

    def close(self):
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
            sess.mark("o", input("note: "))

    if sys.stdin.isatty():
        print("recording... keys: s t c n q b o / Ctrl-C to stop")
        threading.Thread(target=getch_loop, args=(on_key,), daemon=True).start()
    else:
        print("recording... tty が無いのでマーカー入力は無効")
    if deadline is not None:
        print(f"  --duration {a.duration:g} 秒で自動終了する")
    try:
        read_frames(ser, sess.on_frame, stats, deadline)
    except KeyboardInterrupt:
        pass
    finally:
        ser.close(); sess.close()
        print("正常受信フレーム数:")
        for sid in sorted(stats["ok"]):
            print(f"  {STREAM_NAMES.get(sid, f'0x{sid:02X}')}: {stats['ok'][sid]}")
        print(f"XOR 不一致数: {stats['xor_err']}")


if __name__ == "__main__":
    main()
