#!/usr/bin/env python3
"""装置からのバイナリフレームを受けて、セッションフォルダに書く（
docs/data-schema.md）。

キー入力でマーカーを打つ（tty があるときだけ）:
  s 嚥下  t 発話  c 咳  n 首の動き  q 静止  b 一口
  o 観察（次の行を note に）  Ctrl-C 終了

--duration <秒> を付けると、その秒数で自分から終了する（Ctrl-C
と同じ後始末を通る）。
SUBJECT / COND / POSITION / BAND / DURATION の環境変数を、
対応する引数の既定値として読む。
出力先はリポジトリの data/raw/ 固定。変える引数は置かない（
docs/decisions/0007）。

フレーム: [A5 5A][id u8][len u16 LE][t_ms u32 LE][payload][xor u8]
  id 0x01 IMU (float32 x6)  0x02 AUDIO (int16 x N)  0x03 ANALOG (uint16 x N)
  id 0x7F META (JSON)
  id 0x04 DETECT (window_t_ms u32, prob float32, positive u8, led u8)
      → detect.csv（検出器のみ）
  id 0x05 FEAT (window_t_ms u32, float32 x N。正規化前の特徴量)
      → feat.csv（検出器のみ）
ストリームのファイルは、そのストリームの最初のフレームが届いたときに作る。
events.csv と meta.json は常に作る
（docs/decisions/0021）。

--indicator <port> を付けると、DETECT を受けるたびに led（0 消灯 / 1
まだ確認していない目安 / 2 確認した目安）の 1 バイトだけを表示器
（firmware/indicator/、Issue #29、docs/decisions/0018 の追記）へ送る。
時刻・確率・特徴量は送らない。
記録を始める前に表示器のポートが開けなければ、記録を始めない（セッションの
フォルダを作らない）。
記録を始めた後に送れなくなっても（USB が抜けたなど）記録は止めない（終了時に失敗
の回数を表示する）。
送信は別のスレッドで行い、フレームの読み取りとマーカーは表示器への書き込みを待た
ない。送った記録はファイルに残さず、
書くファイル・列・出力先は --indicator の有無で変わらない。

開始の手順（Issue #33、docs/decisions/0006 の追記）: 検出器のポートを開いたら合
図 1 バイト `M`（META_REQUEST）を 1 回書き、
最長 META_WAIT_S 秒、次の条件を満たす META（0x7F）を待つ。
受けてからセッションのフォルダを作り、記録を始める。
  - XOR が合い、ペイロードが UTF-8 の JSON の辞書で、fw が空でない文字列。
    満たさない META は届かない扱いで待ち続ける。
  - fw が "detector" なら threshold（数値）・model（辞書）
    ・feature_names（空でないリスト）がそろっている。欠けていれば止まる。
  - --cond meal なら fw が "detector"。違えば止まる（meal は検出器で録る）。
待つ間に届いた META 以外のフレームは記録の開始前のもので、書かない・数えない。
META の後ろに同じ読み取りで届いたバイトは
記録に引き継ぐ。次のときは記録を始めずに終了コード 1 で止まり、
フォルダを作らない: 検出器のポートが開けない、合図が書けない、
META が META_WAIT_S 秒以内に届かない、上の条件で止まる、待つ間に Ctrl-C。
記録ファームウェア（firmware/logger/）は合図に応えないので、logger
で録るときは記録の前に USB を挿し直す（起動時の META を受けるため）。
meta.json に合図で受けたことは書かない（形式とキーは変わらない）。
挿し直した直後は起動時の META と合図への META の 2 つが届きうる
（同じ内容で merge されるので meta.json は同じ。終了時の表示が META: 2
になる）。

咽喉マイクだけのセッション（--throat <iface>、Issue #36、docs/decisions/0028・
0029）: 装置（シリアル）をつながない。--port・--baud は使わず、合図も送らず、
META も待たない。子プロセス（`arecord`。既定は ssh 越しに UNO Q の
`hw:CARD=Audio,DEV=0`、--throat-host local なら PC の上）が標準出力に出す
S16_LE・48 kHz・2 ch の生の PCM を受けて、変換せずに throat.wav に書く。
読み取りごとに throat_chunks.csv（t_ms, sample_index）を 1 行書く。時刻は PC の
単調時計の、最初の PCM のバイトが届いた時刻からの ms（events.csv も同じ時計）。
最初のバイトが THROAT_START_WAIT_S 秒以内に届かなければ、記録を始めずに終了コー
ド 1（フォルダを作らない）。子が記録の途中で終わったらファイルを残して終了コード
1。終了時に overrun の行の数と推定差（診断値）を表示し、meta.json にも書く。
--throat のときは --subject p1・--cond meal・--indicator を受けない。
UNO Q には何も書かない（コマンドの出力先は標準出力だけ）。ミキサーは `amixer
sget` で読むだけ。

構成 B（--throat sh12jk-nz210c-a2dp-unoq、Issue #38、docs/decisions/0030）: 子は
ssh 越しの `pw-record`（UNO Q の PipeWire の bluez_input のノードから標準出力へ。
-P で node.dont-reconnect・node.dont-fallback・node.name = zm-throat-record）。
--throat-device はこの iface では bluez_input.<XX_XX_XX_XX_XX_XX>.<番号> だけを
受け（hw / plughw は USB の iface だけ）、省けば pw-dump から条件に合うノードが
ちょうど 1 つのときだけそれを使う。子を起動する前に 1 回の ssh で PipeWire・
WirePlumber のユーザー設定と pw-dump（ノード・プロファイル・ミュート・他の取り込み・
pipe-tunnel）を読み取りで確かめ、当たれば記録を始めない（asoundrc の検査と
amixer は行わない）。最初のバイトの後に pw-dump をもう 1 回読み、zm-throat-record
のストリームが目的のノードにだけつながっているかを meta.json の
throat.pipewire.link_check に残す。受信が THROAT_STALL_S 秒止まったら記録を止める
（stop = stall、終了コード 1）。
"""

from __future__ import annotations
import argparse, atexit, codecs, collections, contextlib, csv, json, os, re
import shlex, signal, struct, subprocess, sys, threading, time, wave
from datetime import datetime
from pathlib import Path

if os.name != "nt":
    import select, termios, tty

import serial

SYNC = b"\xa5\x5a"
ID_IMU, ID_AUDIO, ID_ANALOG, ID_META = 0x01, 0x02, 0x03, 0x7F
ID_DETECT, ID_FEAT = 0x04, 0x05
AUDIO_HZ, IMU_HZ = 16000, 104
STREAM_NAMES = {
    ID_IMU: "IMU",
    ID_AUDIO: "AUDIO",
    ID_ANALOG: "ANALOG",
    ID_DETECT: "DETECT",
    ID_FEAT: "FEAT",
    ID_META: "META",
}
IMU_PAYLOAD_LEN, DETECT_PAYLOAD_LEN = 24, 10
# float32 が往復で一致する桁数（prob は閾値との比較を PC
# 側で丸めなしに再現するため）
FLOAT_FMT = ".9g"
RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
# 表示器のポート。表示器（UNO R4 WiFi）の Serial はブリッジ経由の UART
# で値が効くので、indicator.ino の Serial.begin(115200) と揃える。1200
# は使わない（ブートローダに入る）
INDICATOR_BAUD = 115200
# 表示器が詰まったとき、送信のスレッドの 1 回の書き込みが止まる時間の上限
INDICATOR_WRITE_TIMEOUT_S = 0.02
# 検出器に META を送り直させる合図（firmware/detector/detector_meta.h の
# DETECTOR_META_REQUEST、#33）
META_REQUEST = b"M"
# 合図の後に META を待つ最長の秒数。届かなければ記録を始めない
META_WAIT_S = 5.0
# 検出器のポートへの合図の書き込みが止まる時間の上限
DETECTOR_WRITE_TIMEOUT_S = 1.0


def env_default(name: str, fallback: str) -> str:
    """環境変数を引数の既定値にする。空文字列は未指定として扱う（CMake
    から渡る場合用）。"""
    v = os.environ.get(name, "")
    return v if v else fallback


class TtyMode:
    """端末の設定を保存し、メインのスレッドから戻す（POSIX）。

    入力スレッドは daemon で、プロセス終了時に finally を実行せずに打ち切られ
    る。
    cbreak の解除をそのスレッドに任せると端末が cbreak のまま残るので、
    保存と復元は
    メインで持つ。`restore()` は `enter()` の前でも、何度呼んでもよい。
    """

    def __init__(self, fd: int):
        self.fd = fd
        # 終了処理が始まった印。入力スレッドはこれを見て抜ける
        self.closed = False
        self._old = None  # enter() の前の設定
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
        """元の設定（行の編集とエコー）に戻して yield し、
        終了処理が始まっていなければ cbreak に戻す。"""
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
    """POSIX の 1 文字入力ループ。select で待つので、終了の印を見て自分で止まれ
    る。"""

    def __init__(self, fd: int, mode: TtyMode, on_key, on_note):
        self.fd = fd
        self.mode = mode
        self.on_key = on_key
        self.on_note = on_note
        self._dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buf = ""  # 読んだが、まだ処理していない文字
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
        """o の note を1行読む。元の設定の間は、端末が行の編集とエコーを受け持
        つ。"""
        with self.mode.cooked():
            print("note: ", end="", flush=True)
            while "\n" not in self._buf:
                if self._stop():
                    return  # 打ち終える前に終了した。この o は記録しない
                self._fill()
            # 改行より後ろは通常のキーに回す
            note, _, self._buf = self._buf.partition("\n")
            # 時刻は打ち終えた時点（今までと同じ）
            self.on_note(note.rstrip("\r"))


def getch_loop_nt(on_key):
    """Windows の 1 文字入力ループ。端末の設定は変えない。"""
    import msvcrt

    while True:
        on_key(msvcrt.getwch())


def raise_system_exit(signum, frame):
    """SIGTERM を SystemExit に変える。main の finally（端末の復元）
    を通してから終わる。"""
    raise SystemExit(128 + signum)


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:
        return "unknown"


class Session:
    def __init__(
        self, out: Path, subject: str, cond: str, position: str, band: str
    ):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = out / f"{stamp}_{subject}_{cond}"
        # セッションは out の直下にだけ作る。cond に ../
        # が入っても外に出さない（docs/decisions/0007）。
        if self.dir.resolve().parent != out.resolve():
            sys.exit(
                f"セッションフォルダが {out} の直下になりません: {self.dir}"
            )
        self.dir.mkdir(parents=True, exist_ok=False)
        # events.csv は常に作る。ストリームのファイルは最初のフレームが届いたと
        # きに作る（docs/decisions/0021）。
        self.ev = open(
            self.dir / "events.csv", "w", newline="", encoding="utf-8"
        )
        self.ev_w = csv.writer(self.ev)
        self.ev_w.writerow(["t_ms", "label", "note"])
        self.imu = self.chunks = self.wav = self.analog = self.detect = (
            self.feat
        ) = None
        self.n_feat = None  # FEAT の次元数 N。最初の FEAT フレームで決まる
        self.bad_len = {}  # stream_id → 長さが合わずに書かなかったフレーム数
        self.n_audio = 0
        self.last_t_ms = 0
        # mark()（入力スレッド）と close()（メイン）を守る
        self._lock = threading.Lock()
        self._closed = False
        self.meta = {
            "subject": subject,
            "cond": cond,
            "position": position,
            "band": band,
            "firmware_sha": git_sha(),
            "sample_rates": {
                "imu_hz": IMU_HZ,
                "audio_hz": AUDIO_HZ,
                "analog_hz": 0,
            },
            "sensors": [
                {"id": "imu", "part": "LSM6DSOX", "iface": "onboard"},
                {"id": "mic", "part": "MP34DT06JTR", "iface": "onboard-pdm"},
            ],
            "notes": "",
        }

    def _open_csv(self, name: str, header: list[str]):
        f = open(self.dir / name, "w", newline="", encoding="utf-8")
        w = csv.writer(f)
        w.writerow(header)
        return f, w

    def _open_imu(self):
        self.imu, self.imu_w = self._open_csv(
            "imu.csv", ["t_ms", "ax", "ay", "az", "gx", "gy", "gz"]
        )

    def _open_audio(self):
        self.chunks, self.chunks_w = self._open_csv(
            "audio_chunks.csv", ["t_ms", "sample_index"]
        )
        self.wav = wave.open(str(self.dir / "audio.wav"), "wb")
        self.wav.setnchannels(1)
        self.wav.setsampwidth(2)
        self.wav.setframerate(AUDIO_HZ)

    def _open_analog(self, n: int):
        self.analog, self.analog_w = self._open_csv(
            "analog.csv", ["t_ms"] + [f"ch{i}" for i in range(n)]
        )

    def _open_detect(self):
        self.detect, self.detect_w = self._open_csv(
            "detect.csv", ["t_ms", "window_t_ms", "positive", "prob", "led"]
        )

    def _open_feat(self, n: int):
        self.n_feat = n
        self.feat, self.feat_w = self._open_csv(
            "feat.csv", ["t_ms", "window_t_ms"] + [f"f{i}" for i in range(n)]
        )

    def _count_bad_len(self, sid: int):
        self.bad_len[sid] = self.bad_len.get(sid, 0) + 1

    def on_frame(self, sid: int, t_ms: int, payload: bytes):
        # マーカーの時刻。stream_id を問わず、直前に届いたフレームの送信時刻
        self.last_t_ms = t_ms
        if sid == ID_IMU:
            if len(payload) != IMU_PAYLOAD_LEN:
                self._count_bad_len(sid)
                return
            if self.imu is None:
                self._open_imu()
            self.imu_w.writerow(
                [t_ms] + [f"{v:.4f}" for v in struct.unpack("<6f", payload)]
            )
        elif sid == ID_AUDIO:
            if self.wav is None:
                self._open_audio()
            self.chunks_w.writerow([t_ms, self.n_audio])
            self.wav.writeframes(payload)
            self.n_audio += len(payload) // 2
        elif sid == ID_ANALOG:
            if self.analog is None:
                self._open_analog(len(payload) // 2)
            self.analog_w.writerow(
                [t_ms] + list(struct.unpack(f"<{len(payload) // 2}H", payload))
            )
        elif sid == ID_DETECT:
            if len(payload) != DETECT_PAYLOAD_LEN:
                self._count_bad_len(sid)
                return
            window_t_ms, prob, positive, led = struct.unpack("<IfBB", payload)
            if self.detect is None:
                self._open_detect()
            self.detect_w.writerow(
                [t_ms, window_t_ms, positive, f"{prob:{FLOAT_FMT}}", led]
            )
        elif sid == ID_FEAT:
            if len(payload) < 8 or (len(payload) - 4) % 4 != 0:
                self._count_bad_len(sid)
                return
            n = (len(payload) - 4) // 4
            if self.feat is None:
                self._open_feat(n)
            elif n != self.n_feat:
                self._count_bad_len(sid)
                return
            values = struct.unpack(f"<I{n}f", payload)
            self.feat_w.writerow(
                [t_ms, values[0]] + [f"{v:{FLOAT_FMT}}" for v in values[1:]]
            )
        elif sid == ID_META:
            try:
                self.meta.update(json.loads(payload.decode("utf-8")))
            except Exception:
                pass

    def mark(self, label: str, note: str = ""):
        """マーカーを1行書く。close() の後は何もしない（入力スレッドが遅れて呼ぶ
        ことがある）。"""
        with self._lock:
            if self._closed:
                return
            self.ev_w.writerow([self.last_t_ms, label, note])
            self.ev.flush()
            print(f"  [{self.last_t_ms} ms] {label} {note}")

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for f in (
                self.imu,
                self.ev,
                self.chunks,
                self.analog,
                self.detect,
                self.feat,
                self.wav,
            ):
                if f:
                    f.close()
        (self.dir / "meta.json").write_text(
            json.dumps(self.meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        names = self.meta.get("feature_names")
        if (
            self.n_feat is not None
            and isinstance(names, list)
            and len(names) != self.n_feat
        ):
            # meta.json は装置の値のまま書く。直さない
            print(
                f"警告: meta.json の feature_names は {len(names)} 個ですが、feat.csv の次元数は {self.n_feat} です"
            )
        print(f"saved: {self.dir}")


def parse_frames(buf: bytearray, on_frame, stats: dict) -> bool:
    """buf の先頭から完全なフレームを順に取り出して on_frame に渡し、
    取り出した分を buf から消す
    （read_frames と wait_meta で共通の解析）。

    stats の数え方は read_frames と同じ。on_frame が真を返したら、
    そのフレームの直後で止めて True を返す
    （後ろのバイトは buf に残り、次の段が続きから読む）。読み切ったら
    False（途中までのフレームは buf に残る）。
    """
    while True:
        i = buf.find(SYNC)
        if i < 0:
            # 末尾が SYNC の前半（0xA5）なら残す（次の読み取りの先頭に 0x5A
            # が来るとフレームになる）。他は捨てる
            if buf and buf[-1] == SYNC[0]:
                del buf[:-1]
            else:
                buf.clear()
            return False
        if len(buf) < i + 9:
            del buf[:i]
            return False
        sid = buf[i + 2]
        ln, t_ms = struct.unpack_from("<HI", buf, i + 3)
        end = i + 9 + ln + 1
        if len(buf) < end:
            del buf[:i]
            return False
        payload = bytes(buf[i + 9 : i + 9 + ln])
        x = 0
        for b in buf[i + 2 : i + 9 + ln]:
            x ^= b
        stop = False
        if x == buf[end - 1]:
            stats["ok"][sid] = stats["ok"].get(sid, 0) + 1
            stop = bool(on_frame(sid, t_ms, payload))
        else:
            stats["xor_err"] += 1
        del buf[:end]
        if stop:
            return True


def read_frames(
    ser: serial.Serial,
    on_frame,
    stats: dict,
    deadline: float | None = None,
    buf: bytearray | None = None,
):
    """フレームを読み続ける。

    stats["ok"][stream_id] に**正常受信フレーム数**（xor 検証を通って on_frame
    に渡った数）、
    stats["xor_err"] に **XOR 不一致数**（SYNC と長さと xor バイトまで読めたが
    xor が合わずに
    捨てたフレーム候補の数）を数える。deadline（time.monotonic の値）
    を過ぎたら戻る。
    buf を渡すと、その中身（wait_meta が META の後ろに残したバイト）
    の続きから読む。
    """
    if buf is None:
        buf = bytearray()
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            return
        buf += ser.read(4096)
        parse_frames(buf, on_frame, stats)


class MetaRejected(Exception):
    """記録を始めない META を受けた（送り直しても中身は同じなので、
    待ち続けずに止まる）。文は表示する理由。"""


DETECTOR_META_KEYS = ("threshold", "model", "feature_names")


def start_meta(payload: bytes, cond: str) -> dict | None:
    """記録を始めてよい META かを見る（値の正しさは評価のときに analysis/
    が見る。ここでは見ない）。

    始めてよければ JSON の辞書を返す。届かない扱い（JSON の辞書でない、fw
    が空でない文字列でない）なら None。
    検出器の META に threshold・model・feature_names が欠けている、または
    --cond meal で fw が detector でなければ
    MetaRejected を出す。
    """
    try:
        meta = json.loads(payload.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(meta, dict):
        return None
    fw = meta.get("fw")
    if not isinstance(fw, str) or not fw:
        return None
    if cond == "meal" and fw != "detector":
        raise MetaRejected(
            f"meal は検出器で録ります（fw={fw}）。記録を始めません"
        )
    if fw == "detector":
        missing = []
        th = meta.get("threshold")
        if not isinstance(th, (int, float)) or isinstance(th, bool):
            missing.append("threshold")
        if not isinstance(meta.get("model"), dict):
            missing.append("model")
        names = meta.get("feature_names")
        if not isinstance(names, list) or not names:
            missing.append("feature_names")
        if missing:
            raise MetaRejected(
                f"検出器の META に {', '.join(missing)} がありません。記録を始めません"
            )
    return meta


def wait_meta(ser, timeout_s: float, cond: str, stats: dict, buf: bytearray):
    """合図の後、最長 timeout_s 秒、記録を始めてよい META を待つ（Issue #33）。

    受けたら (t_ms, payload, meta) を返し、stats["ok"][ID_META] に 1 を足す。
    buf には META の後ろのバイトが残る
    （read_frames に渡して続きから読む）。届かなければ None。待つ間の META
    以外のフレームと、届かない扱いの META と
    XOR 不一致は記録の開始前のもので、on_frame に渡さず stats にも数えない。
    止まる META は MetaRejected を出す。
    """
    found = []

    def on_frame(sid: int, t_ms: int, payload: bytes) -> bool:
        if sid != ID_META:
            return False
        meta = start_meta(payload, cond)
        if meta is None:
            return False
        found.append((t_ms, payload, meta))
        return True

    before = {"ok": {}, "xor_err": 0}  # 開始前の数（表示しない）
    deadline = time.monotonic() + timeout_s
    while True:
        if parse_frames(buf, on_frame, before):
            stats["ok"][ID_META] = stats["ok"].get(ID_META, 0) + 1
            return found[0]
        if time.monotonic() >= deadline:
            return None
        buf += ser.read(4096)


def detect_led(payload: bytes) -> int | None:
    """DETECT のペイロードから led を取り出す。長さが違う、または 0〜2
    でなければ None（送らない）。"""
    if len(payload) != DETECT_PAYLOAD_LEN:
        return None
    led = payload[DETECT_PAYLOAD_LEN - 1]  # "<IfBB" の最後のバイト
    return led if led in (0, 1, 2) else None


class IndicatorForwarder:
    """DETECT の led を表示器へ 1 バイトずつ送る（Issue #29）。
    書き込みは送信のスレッド（daemon）だけが行う。

    読み取りのループから呼ぶ offer() はロックの中で最新の値を置いて Event
    を立てるだけで、シリアルに触らない
    （読み取りのループとマーカーが書き込みを待つ経路をなくす）。
    送れなくても例外を外に出さない（記録を止めない）。
    送るのは led の値だけで、時刻・確率・特徴量は送らない。
    送った記録はファイルに残さない。
    件数（summary_lines）は close() の後に読む。
    """

    def __init__(self, ser):
        self.ser = ser
        self.sent = 0  # 送れた回数
        self.failed = 0  # 例外、または書けたバイト数が 1 でなかった回数
        self.skipped = 0  # 送らなかった DETECT（長さ違い、led が 0〜2 でない）
        # 送る前に次の値で置き換えた回数（送信が詰まっていた間の古い値。
        # 最新の値だけを送る）
        self.replaced = 0
        self.first_error = None
        # close() の join の後も送信のスレッドが生きていれば
        # False（件数は確定しない）
        self.thread_stopped = True
        self._lock = threading.Lock()
        self._pending = None  # 送っていない最新の値か None
        self._event = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        """送信のスレッドを始める（daemon）。"""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def offer(self, payload: bytes) -> None:
        """読み取りのループから呼ぶ。DETECT のペイロードから led を取り、
        最新の値として置く。シリアルには触らない。"""
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
        """送信のスレッド。Event を待ち（0.1 秒ごとに止める印を見る）、
        最新の値を取り出して書く。"""
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
        """送信のスレッドを止めて（join は 1.0 秒まで。止まらなくても終了は妨げ
        ない）、ポートを閉じる。
        止める時点で送っていない値は送らない（終了時に何も送らない。表示器は
        1.0 秒で消灯する）。"""
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
        lines = [
            f"表示器への転送（DETECT の led）: 送った {self.sent} / 失敗 {self.failed} / "
            f"送らなかった {self.skipped} / 置き換えた {self.replaced}"
        ]
        if self.failed > 0:
            lines.append(f"  最初の失敗: {self.first_error}")
        if not self.thread_stopped:
            lines.append(
                "  送信のスレッドが止まりませんでした（件数は終了時点のもの）"
            )
        return lines


def with_indicator(on_frame, fwd):
    """fwd が None なら on_frame をそのまま返す（--indicator
    無しは今までと同じ呼び出し）。
    あれば、on_frame(sid, t_ms, payload) を呼んだ後、DETECT なら
    fwd.offer(payload) を呼ぶ関数を返す。"""
    if fwd is None:
        return on_frame

    def on_frame_and_offer(sid: int, t_ms: int, payload: bytes):
        on_frame(sid, t_ms, payload)
        if sid == ID_DETECT:
            fwd.offer(payload)

    return on_frame_and_offer


def indicator_port_error(port: str, indicator: str | None) -> str | None:
    """--indicator が --port と同じ実体を指すならエラーの文を返す（検出器に
    バイトを送らないため）。問題なければ None。"""
    if indicator is None:
        return None
    if os.path.realpath(port) == os.path.realpath(indicator):
        return f"--indicator と --port が同じポートを指しています: {indicator}"
    return None


# --- 咽喉マイクだけのセッション（#36、docs/decisions/0028・0029） ---
THROAT_RATE_HZ, THROAT_CHANNELS, THROAT_SAMPWIDTH = 48000, 2, 2
THROAT_FRAME_BYTES = THROAT_CHANNELS * THROAT_SAMPWIDTH
# 最初の PCM のバイトを待つ最長の秒数（実測の最大 3.96 秒の約 2.5 倍）
THROAT_START_WAIT_S = 10.0
# 止めた後に子の標準出力を読み切る最長の秒数、子の終了を待つ最長の秒数
THROAT_DRAIN_S = 2.0
THROAT_WAIT_S = 2.0
# ミキサーの読み取りのタイムアウト
THROAT_MIXER_TIMEOUT_S = 5.0
# arecord の 1 ピリオド（-F 125000）。推定差の目安に使う
THROAT_PERIOD_FRAMES = 6000
THROAT_DEFAULT_HOST = "arduino@unoq.local"
THROAT_DEFAULT_DEVICE = "hw:CARD=Audio,DEV=0"
THROAT_PART = "SH-12JK"
# 受ける iface（docs/data-schema.md、docs/decisions/0029・0030）。値は
# （リモート（UNO Q）か、子の種類）。alsa = arecord、a2dp = pw-record
THROAT_IFACES = {
    "sh12jk-wired-unoq-usbaudio": (True, "alsa"),
    "sh12jk-nz210c-rx-unoq-usbaudio": (True, "alsa"),
    "sh12jk-nz210c-a2dp-unoq": (True, "a2dp"),
    "sh12jk-wired-pc": (False, "alsa"),
    "sh12jk-nz210c-rx-usbaudio": (False, "alsa"),
}
# 出力先は標準出力（-）だけ。-q は付けない（overrun の行を出させる）。-v で
# buffer_size・period_size を標準エラーに出させる
ARECORD_ARGS = [
    "-f", "S16_LE", "-r", "48000", "-c", "2", "-t", "raw",
    "-B", "2000000", "-F", "125000", "-v", "-",
]  # fmt: skip
SSH_OPTS = [
    "-T",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=5",
    "-o", "ServerAliveInterval=2",
    "-o", "ServerAliveCountMax=3",
]  # fmt: skip
# 先頭は英数字（先頭の - をオプションとして読ませない）
THROAT_HOST_RE = r"[A-Za-z0-9][A-Za-z0-9_.@-]*"
# PCM 名は hw / plughw のカード・デバイス指定だけ。tee: や file: などの
# プラグインは入力をファイルにも複製できるので受けない（0028 決定 2）
THROAT_DEVICE_RE = (
    r"(plug)?hw:(CARD=[A-Za-z0-9_]+(,DEV=[0-9]+)?|[0-9]+(,[0-9]+)?)"
)
# 構成 B（a2dp）で受けるノード名。PipeWire の bluez の入力ノードだけ（0030
# 決定 3）。大文字の 16 進 6 組と .番号
THROAT_BLUEZ_NODE_RE = r"bluez_input\.[0-9A-F]{2}(_[0-9A-F]{2}){5}\.[0-9]+"
# pw-record のストリームの props（固定）。目的のノードが無いとき既定の入力に
# 落ちない、途中で切れたとき別の入力につながり直さない、起動後の確認で名前で
# 特定する（plan #38 第 3.2 節）
PW_STREAM_NAME = "zm-throat-record"
PW_PROPS = (
    "{ node.dont-reconnect = true node.dont-fallback = true"
    f" node.name = {PW_STREAM_NAME} }}"
)
# ssh の非対話のセッションでは XDG_RUNTIME_DIR が要る（10/8）
PW_ENV = "env XDG_RUNTIME_DIR=/run/user/$(id -u)"
# 起動前の確認（1 回の ssh）・起動後のリンクの確認のタイムアウト、終了時に
# リンクの確認を待つ最長の秒数
THROAT_PW_TIMEOUT_S = 10.0
THROAT_LINK_TIMEOUT_S = 10.0
THROAT_LINK_WAIT_S = 10.0
# a2dp でこの秒数バイトが来なければ記録を止める（stall）。10/7 の到着の間隔の
# 最大 166 ms の 10 倍以上
THROAT_STALL_S = 2.0
A2DP_MIXER_NOTE = "A2DP の経路ではミキサーを読まない"
NO_BLUEZ_NODE_GUIDE = "手順は docs/unoq-setup.md の『構成 B の接続』"
# WirePlumber の断片で許す 1 つと、その中身で許す 1 行（コメントと空行は除く）
WP_CONF_D_ALLOWED = ["90-bluez-no-seat.conf"]
WP_NO_SEAT_LINE_RE = (
    r"\s*wireplumber\.profiles\.main\.monitor\.bluez\.seat-monitoring"
    r"\s*=\s*disabled\s*"
)


def throat_arg_error(a) -> str | None:
    """--throat の引数の検査。記録を始めない理由の文か None。"""
    if a.subject == "p1":
        return "--throat は subject self のみ（docs/decisions/0028）"
    if a.cond == "meal":
        return "--throat では meal を録りません（meal は検出器で録る）"
    if a.indicator is not None:
        return "--throat では --indicator を使いません（DETECT が来ない）"
    if a.throat not in THROAT_IFACES:
        return f"--throat の iface は次のどれか: {', '.join(THROAT_IFACES)}"
    host, device = a.throat_host, a.throat_device
    if host != "local" and not re.fullmatch(THROAT_HOST_RE, host):
        return f"--throat-host に使えない文字があります: {host!r}"
    remote, kind = THROAT_IFACES[a.throat]
    if kind == "a2dp":
        # None は「pw-dump から自動で探す」。見つけた名前も同じ検査に通す
        if device is not None and not re.fullmatch(
            THROAT_BLUEZ_NODE_RE, device
        ):
            return (
                f"iface {a.throat} の --throat-device は PipeWire の"
                " bluez_input.<XX_XX_XX_XX_XX_XX>.<番号> だけ"
                f"（hw / plughw は USB の iface だけ）: {device!r}"
            )
    elif device is None or not re.fullmatch(THROAT_DEVICE_RE, device):
        return (
            "--throat-device は hw:CARD=<名前>[,DEV=<番号>] / hw:<番号>[,<番号>]"
            f"（plughw: も可）だけ: {device!r}"
        )
    if remote and host == "local":
        return f"iface {a.throat} は UNO Q の経路です。--throat-host local は使えません"
    if not remote and host != "local":
        return f"iface {a.throat} は PC の経路です。--throat-host local にしてください"
    return None


def remote_arecord(device: str) -> str:
    """ssh で UNO Q に渡す文字列（標準出力へ出すだけ）。"""
    return f"exec arecord -D {shlex.quote(device)} {' '.join(ARECORD_ARGS)}"


def resolve_throat_device(a) -> tuple[str | None, str]:
    """(device, 出どころ)。引数 > 環境変数 THROAT_DEVICE > iface ごとの既定。
    a2dp の既定は None（起動前の pw-dump から自動で探す。出どころ auto）。
    空文字列の環境変数は未指定として扱う（env_default と同じ）。"""
    if a.throat_device is not None:
        return a.throat_device, "arg"
    env = os.environ.get("THROAT_DEVICE", "")
    if env:
        return env, "env"
    if THROAT_IFACES.get(a.throat, (True, "alsa"))[1] == "a2dp":
        return None, "auto"
    return THROAT_DEFAULT_DEVICE, "default"


def remote_pw_record(device: str) -> str:
    """ssh で UNO Q に渡す pw-record の文字列（標準出力へ出すだけ）。"""
    return (
        f"exec {PW_ENV} pw-record --target {shlex.quote(device)}"
        " --rate 48000 --channels 2 --format s16"
        f" -P {shlex.quote(PW_PROPS)} --raw -"
    )


def throat_argv(host: str, device: str, kind: str = "alsa") -> list[str]:
    """生の PCM を標準出力に出す子プロセスの argv。テストで差し替える口。
    kind = a2dp は ssh 越しの pw-record だけ（local は無い）。"""
    if kind == "a2dp":
        if host == "local":
            raise ValueError("a2dp の経路は UNO Q（ssh）だけ")
        return ["ssh", *SSH_OPTS, host, remote_pw_record(device)]
    if host == "local":
        return ["arecord", "-D", device, *ARECORD_ARGS]
    return ["ssh", *SSH_OPTS, host, remote_arecord(device)]


def device_card(device: str) -> str | None:
    """ALSA の PCM 名の CARD= の値。無ければ None。"""
    m = re.search(r"(?:^|[:,])CARD=([A-Za-z0-9_]+)", device)
    return m.group(1) if m else None


def mixer_argv(host: str, device: str) -> list[str] | None:
    """入力のゲインを読む（sget のみ。値は変えない）argv。CARD= が無ければ
    None。"""
    card = device_card(device)
    if card is None:
        return None
    if host == "local":
        return ["amixer", "-c", card, "sget", "Mic"]
    return [
        "ssh",
        *SSH_OPTS,
        host,
        f"exec amixer -c {shlex.quote(card)} sget Mic",
    ]


ASOUNDRC_TEST = "test ! -e ~/.asoundrc -a ! -e /etc/asound.conf"


def asoundrc_check_argv(host: str) -> list[str]:
    """ALSA のユーザー設定が無いことを確かめる argv（読み取りのみ）。設定が無
    ければ終了コード 0、あれば 1。"""
    if host == "local":
        return ["sh", "-c", ASOUNDRC_TEST]
    return ["ssh", *SSH_OPTS, host, f"exec sh -c '{ASOUNDRC_TEST}'"]


def asoundrc_error(host: str) -> str | None:
    """子を起動する前の検査。記録を始めない理由の文か None。"""
    try:
        r = subprocess.run(
            asoundrc_check_argv(host),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=THROAT_MIXER_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return "ALSA の設定を確かめられませんでした（タイムアウト）。記録を始めません"
    except OSError as e:
        return f"ALSA の設定を確かめられませんでした（{e}）。記録を始めません"
    if r.returncode == 0:
        return None
    if r.returncode == 1:
        return (
            "ALSA のユーザー設定（~/.asoundrc または /etc/asound.conf）があるので"
            "記録を始めません（docs/decisions/0029）"
        )
    return (
        f"ALSA の設定を確かめられませんでした（終了コード {r.returncode}: "
        f"{r.stderr.strip()[-200:]}）。記録を始めません"
    )


def read_mixer(host: str, device: str) -> tuple[str | None, str | None]:
    """(標準出力, 失敗の理由)。失敗しても記録は始める。"""
    argv = mixer_argv(host, device)
    if argv is None:
        return None, "--throat-device に CARD= が無いので読んでいない"
    try:
        r = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=THROAT_MIXER_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return None, f"タイムアウト（{THROAT_MIXER_TIMEOUT_S:g} 秒）"
    except OSError as e:
        return None, f"起動できない: {e}"
    if r.returncode != 0:
        return None, f"終了コード {r.returncode}: {r.stderr.strip()[-200:]}"
    return r.stdout, None


# --- 構成 B（a2dp）の PipeWire の確認（plan #38 第 3.3・3.4 節。読み取りのみ）
# 1 回の ssh で、ユーザーの設定の有無と中身、/etc の一覧、pw-dump を区切りの行
# （@@<名前>）の後ろに出す。pw-dump は最後で、その終了コードが全体の終了コード
PW_SECTIONS = ("pipewire_dir", "wireplumber_conf", "conf_d", "no_seat", "etc")
PW_CHECK_SCRIPT = "; ".join(
    [
        "echo @@pipewire_dir",
        "test -e ~/.config/pipewire && echo present",
        "echo @@wireplumber_conf",
        "test -e ~/.config/wireplumber/wireplumber.conf && echo present",
        "echo @@conf_d",
        "ls -A ~/.config/wireplumber/wireplumber.conf.d 2>/dev/null",
        "echo @@no_seat",
        "cat ~/.config/wireplumber/wireplumber.conf.d/90-bluez-no-seat.conf"
        " 2>/dev/null",
        "echo @@etc",
        "ls -AR /etc/pipewire /etc/wireplumber 2>/dev/null",
        "echo @@pw_dump",
        f"exec {PW_ENV} pw-dump",
    ]
)


def pipewire_check_argv(host: str) -> list[str]:
    """起動前の確認の argv（読み取りのみ）。テストで差し替える口。"""
    return [
        "ssh",
        *SSH_OPTS,
        host,
        f"exec sh -c {shlex.quote(PW_CHECK_SCRIPT)}",
    ]


def pw_dump_argv(host: str) -> list[str]:
    """起動後のリンクの確認の argv（読み取りのみ）。テストで差し替える口。"""
    return ["ssh", *SSH_OPTS, host, f"exec {PW_ENV} pw-dump"]


def split_pw_check(text: str) -> dict | None:
    """PW_CHECK_SCRIPT の出力を区切りごとに分ける。pw_dump は残り全部の文字列、
    他は行のリスト。区切りがそろわなければ None。"""
    secs: dict = {}
    cur = None
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line == "@@pw_dump":
            secs["pw_dump"] = "\n".join(lines[i + 1 :])
            break
        if line.startswith("@@") and line[2:] in PW_SECTIONS:
            cur = line[2:]
            secs[cur] = []
        elif cur is not None:
            secs[cur].append(line)
    if "pw_dump" not in secs or any(k not in secs for k in PW_SECTIONS):
        return None
    return secs


def _ls_r_paths(lines: list[str]) -> list[str]:
    """ls -AR の出力をパスの一覧にする（ディレクトリも含む）。"""
    out, cur = [], None
    for line in lines:
        if not line.strip():
            continue
        if line.endswith(":") and line.startswith("/"):
            cur = line[:-1]
            continue
        out.append(f"{cur}/{line}" if cur else line)
    return out


def pipewire_config_error(secs: dict) -> tuple[str | None, dict]:
    """ユーザーの PipeWire・WirePlumber の設定の検査。(止める理由か None,
    throat.pipewire.config)。/etc は一覧を残すだけで止めない（Q8）。"""
    conf_d = [s for s in secs["conf_d"] if s.strip()]
    config = {
        "wireplumber_conf_d": conf_d,
        "etc": _ls_r_paths(secs["etc"]),
    }
    why = "記録を始めません（docs/decisions/0030）"
    if any(s.strip() == "present" for s in secs["pipewire_dir"]):
        return f"UNO Q に ~/.config/pipewire があるので{why}", config
    if any(s.strip() == "present" for s in secs["wireplumber_conf"]):
        return (
            f"UNO Q に ~/.config/wireplumber/wireplumber.conf があるので{why}",
            config,
        )
    if conf_d != WP_CONF_D_ALLOWED:
        return (
            "UNO Q の ~/.config/wireplumber/wireplumber.conf.d の中身が"
            f" {WP_CONF_D_ALLOWED} だけではないので{why}: {conf_d}",
            config,
        )
    body = [
        s
        for s in secs["no_seat"]
        if s.strip() and not s.strip().startswith("#")
    ]
    if len(body) != 1 or not re.fullmatch(WP_NO_SEAT_LINE_RE, body[0]):
        return (
            f"UNO Q の {WP_CONF_D_ALLOWED[0]} に seat-monitoring を止める 1 行"
            f"以外の行があるので{why}",
            config,
        )
    return None, config


def _info(o: dict) -> dict:
    return o.get("info") or {}


def _props(o: dict) -> dict:
    return _info(o).get("props") or {}


def _param(o: dict, key: str) -> list:
    p = (_info(o).get("params") or {}).get(key) or []
    return [x for x in p if isinstance(x, dict)]


def _by_type(objs: list, t: str) -> list[dict]:
    return [
        o
        for o in objs
        if isinstance(o, dict) and o.get("type") == f"PipeWire:Interface:{t}"
    ]


def _link_ends(link: dict) -> tuple:
    """(出力側のノードの id, 入力側のノードの id)。"""
    i, p = _info(link), _props(link)
    out = i.get("output-node-id", p.get("link.output.node"))
    inp = i.get("input-node-id", p.get("link.input.node"))
    return out, inp


def _bluez_prop(node: dict, devices: dict, key: str):
    """api.bluez5.* をノードの props から、無ければデバイスの props から。"""
    v = _props(node).get(key)
    if v is None:
        dev = devices.get(_props(node).get("device.id"))
        if dev is not None:
            v = _props(dev).get(key)
    return v


def pipewire_state(
    objs: list, device: str | None, config: dict
) -> tuple[str | None, str | None, dict]:
    """pw-dump の結果の検査。(止める理由か None, 使うノード名, throat.pipewire)。
    device が None なら条件に合うノードがちょうど 1 つのときだけそれを使う。"""
    nodes = _by_type(objs, "Node")
    devices = {d.get("id"): d for d in _by_type(objs, "Device")}
    by_id = {n.get("id"): n for n in nodes}
    info: dict = {}
    for m in _by_type(objs, "Module"):
        if "pipe-tunnel" in str(_info(m).get("name", "")):
            return (
                "PipeWire に libpipewire-module-pipe-tunnel が読み込まれている"
                "ので記録を始めません（docs/decisions/0030）",
                None,
                info,
            )

    def name(n):
        return str(_props(n).get("node.name", ""))

    def is_a2dp_source(n):
        return (
            _props(n).get("media.class") == "Audio/Source"
            and _bluez_prop(n, devices, "api.bluez5.profile") == "a2dp-source"
        )

    no_node = "送信機が接続されていません（bluez_input のノードがありません）。"
    if device is None:
        found = [
            n
            for n in nodes
            if re.fullmatch(THROAT_BLUEZ_NODE_RE, name(n)) and is_a2dp_source(n)
        ]
        if not found:
            return no_node + NO_BLUEZ_NODE_GUIDE, None, info
        if len(found) > 1:
            names = "、".join(sorted(name(n) for n in found))
            return (
                f"条件に合う bluez_input のノードが {len(found)} 個あります（{names}）。"
                "--throat-device で指定してください",
                None,
                info,
            )
        node = found[0]
        device = name(node)
        if not re.fullmatch(THROAT_BLUEZ_NODE_RE, device):
            return f"見つけたノード名の形が違います: {device!r}", None, info
    else:
        hit = [n for n in nodes if name(n) == device]
        if not hit:
            return (
                f"送信機が接続されていません（{device} のノードがありません）。"
                + NO_BLUEZ_NODE_GUIDE,
                None,
                info,
            )
        node = hit[0]
    mclass = _props(node).get("media.class")
    profile = _bluez_prop(node, devices, "api.bluez5.profile")
    if mclass != "Audio/Source":
        return (
            f"{device} の media.class が Audio/Source ではありません: {mclass!r}",
            None,
            info,
        )
    if profile != "a2dp-source":
        return (
            f"{device} のプロファイルが a2dp-source ではありません: {profile!r}",
            None,
            info,
        )
    mute = vols = None
    for p in _param(node, "Props"):
        if "mute" in p and mute is None:
            mute = p.get("mute")
        if "channelVolumes" in p and vols is None:
            vols = p.get("channelVolumes")
    if mute is True:
        return (
            f"{device} がミュートなので記録を始めません（全部 0 になる）",
            None,
            info,
        )
    links = []
    nid = node.get("id")
    for lk in _by_type(objs, "Link"):
        out, inp = _link_ends(lk)
        if out != nid:
            continue
        dst = by_id.get(inp)
        dclass = str(_props(dst).get("media.class", "")) if dst else ""
        if dclass.startswith("Stream/Input/Audio"):
            return (
                f"{device} に他の取り込み（{name(dst)}）がつながっているので"
                "記録を始めません（record.py の外で録っている疑い）",
                None,
                info,
            )
        links.append(name(dst) if dst else str(inp))
    fmt = (_param(node, "Format") or [{}])[0]
    driver = by_id.get(_props(node).get("node.driver-id"))
    info = {
        "profile": profile,
        "codec": _bluez_prop(node, devices, "api.bluez5.codec"),
        "node_rate_hz": fmt.get("rate"),
        "node_channels": fmt.get("channels"),
        "node_format": fmt.get("format"),
        "codec_rate_hz": None,
        "codec_rate_note": "SBC のレートは確かめていない",
        "mute": mute,
        "channel_volumes": vols,
        "driver": name(driver) if driver else None,
        "links": links,
        "config": config,
    }
    return None, device, info


def pipewire_check(
    host: str, device: str | None
) -> tuple[str | None, str | None, dict]:
    """子を起動する前の確認（1 回の ssh）。(止める理由か None, 使うノード名,
    throat.pipewire)。確かめられなければ止める。"""
    try:
        r = subprocess.run(
            pipewire_check_argv(host),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=THROAT_PW_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return (
            "PipeWire の状態を確かめられませんでした"
            f"（タイムアウト {THROAT_PW_TIMEOUT_S:g} 秒）。記録を始めません",
            None,
            {},
        )
    except OSError as e:
        return (
            f"PipeWire の状態を確かめられませんでした（{e}）。記録を始めません",
            None,
            {},
        )
    if r.returncode != 0:
        return (
            f"PipeWire の状態を確かめられませんでした（終了コード {r.returncode}: "
            f"{r.stderr.strip()[-200:]}）。記録を始めません",
            None,
            {},
        )
    secs = split_pw_check(r.stdout)
    if secs is None:
        return (
            "PipeWire の状態を確かめられませんでした（出力の形が違う）。記録を始めません",
            None,
            {},
        )
    err, config = pipewire_config_error(secs)
    if err:
        return err, None, {}
    try:
        objs = json.loads(secs["pw_dump"])
    except ValueError:
        objs = None
    if not isinstance(objs, list):
        return (
            "pw-dump の JSON が読めませんでした。記録を始めません",
            None,
            {},
        )
    return pipewire_state(objs, device, config)


def link_check_result(objs: list, target: str) -> tuple[str, str]:
    """起動後の確認。zm-throat-record のストリームがちょうど 1 つで、その入力が
    目的のノードにだけつながっていれば ok。"""
    nodes = _by_type(objs, "Node")
    by_id = {n.get("id"): n for n in nodes}
    tids = {n.get("id") for n in nodes if _props(n).get("node.name") == target}
    streams = [n for n in nodes if _props(n).get("node.name") == PW_STREAM_NAME]
    if len(streams) != 1:
        return "ng", f"{PW_STREAM_NAME} のストリームが {len(streams)} 個"
    sid = streams[0].get("id")
    srcs = set()
    for lk in _by_type(objs, "Link"):
        out, inp = _link_ends(lk)
        if inp == sid:
            srcs.add(out)
    if not srcs:
        return "ng", f"{PW_STREAM_NAME} がどのノードにもつながっていない"
    if not tids or srcs != tids:
        names = sorted(
            str(_props(by_id[s]).get("node.name", s)) if s in by_id else str(s)
            for s in srcs
        )
        return (
            "ng",
            f"{PW_STREAM_NAME} が目的のノード以外につながっている: {names}",
        )
    return "ok", ""


class LinkCheck:
    """起動後のリンクの確認を別のスレッドで 1 回だけ行う（記録の読み取りを
    止めない）。finish() は最長 THROAT_LINK_WAIT_S 秒待ち、必ず ok / ng /
    error のどれかを返す。"""

    def __init__(self, host: str, target: str):
        self.host = host
        self.target = target
        self.result: tuple[str, str] | None = None
        self.proc = None
        self._lock = threading.Lock()
        self._gave_up = False
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self) -> None:
        res = self._check()
        with self._lock:
            if self._gave_up:
                return
            self.result = res
        if res[0] == "ng":
            print(
                f"警告: 起動後のリンクの確認が ng（記録は続ける）: {res[1]}",
                flush=True,
            )

    def _check(self) -> tuple[str, str]:
        try:
            with self._lock:
                if self._gave_up:
                    return "error", "確認を打ち切った"
                self.proc = subprocess.Popen(
                    pw_dump_argv(self.host),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            try:
                out, err = self.proc.communicate(timeout=THROAT_LINK_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.communicate()
                return (
                    "error",
                    f"pw-dump のタイムアウト（{THROAT_LINK_TIMEOUT_S:g} 秒）",
                )
        except (OSError, ValueError) as e:
            return "error", f"pw-dump を実行できない: {e}"
        if self.proc.returncode != 0:
            return (
                "error",
                f"pw-dump の終了コード {self.proc.returncode}: {err.strip()[-200:]}",
            )
        try:
            objs = json.loads(out)
        except ValueError:
            objs = None
        if not isinstance(objs, list):
            return "error", "pw-dump の JSON が読めない"
        return link_check_result(objs, self.target)

    def finish(self) -> tuple[str, str]:
        self._t.join(timeout=THROAT_LINK_WAIT_S)
        with self._lock:
            if self.result is not None:
                return self.result
            self._gave_up = True
            proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        return (
            "error",
            f"確認が記録の終了までに終わらなかった（{THROAT_LINK_WAIT_S:g} 秒待った）",
        )


class ThroatStream:
    """生の PCM を標準出力に出す子プロセス。標準エラーは daemon のスレッドで
    行ごとに読み、overrun の行を数え、buffer_size・period_size を拾う（標準エ
    ラーに音声は流れない）。"""

    def __init__(self, argv: list[str]):
        self.argv = argv
        # 新しいセッションにする: 端末の Ctrl-C は record.py だけが受け、
        # 子の止め方と順序は record.py が決める
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.fd = self.proc.stdout.fileno()
        self.eof = False
        self.terminated = False  # record.py が SIGTERM を送った
        self.overrun_lines = 0
        self.first_overrun = None
        self.buffer_size = self.period_size = None
        self.tail = collections.deque(maxlen=8)
        self._err = threading.Thread(target=self._read_stderr, daemon=True)
        self._err.start()

    def _read_stderr(self) -> None:
        try:
            for raw in self.proc.stderr:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if "overrun" in line.lower():
                    self.overrun_lines += 1
                    if self.first_overrun is None:
                        self.first_overrun = line
                m = re.match(r"\s*(buffer_size|period_size)\s*:\s*(\d+)", line)
                if m and getattr(self, m.group(1)) is None:
                    setattr(self, m.group(1), int(m.group(2)))
                self.tail.append(line)
        except (OSError, ValueError):
            pass

    def read(self, timeout: float) -> bytes | None:
        """読めた分を返す。timeout 秒で何も無ければ None。EOF なら b"" を返し
        eof を立てる。"""
        if self.eof:
            return b""
        if not select.select([self.fd], [], [], timeout)[0]:
            return None
        data = os.read(self.fd, 65536)
        if not data:
            self.eof = True
        return data

    def stderr_tail(self) -> str:
        self._err.join(timeout=0.5)
        return "\n".join(f"  {s}" for s in self.tail) or "  （なし）"

    def _killpg(self, sig) -> None:
        try:
            os.killpg(self.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def stop(self, on_data) -> None:
        """子のプロセスグループに SIGTERM → 標準出力を EOF まで最長
        THROAT_DRAIN_S 秒読み切る（on_data に渡す）→ 最長 THROAT_WAIT_S 秒待ち、
        残れば SIGKILL。何度呼んでもよい。"""
        if self.proc.poll() is None:
            self.terminated = True
            self._killpg(signal.SIGTERM)
        end = time.monotonic() + THROAT_DRAIN_S
        while not self.eof and time.monotonic() < end:
            try:
                d = self.read(0.05)
            except (OSError, ValueError):
                break
            if d:
                on_data(d)
        try:
            self.proc.wait(timeout=THROAT_WAIT_S)
        except subprocess.TimeoutExpired:
            self._killpg(signal.SIGKILL)
            self.proc.wait()
        try:
            self.proc.stdout.close()
        except Exception:
            pass
        self._err.join(timeout=1.0)


class ThroatSession:
    """咽喉マイクだけのセッション（fw = pc-throat）のファイル。

    時刻は clock（既定 time.monotonic）の t0（最初の PCM のバイトが届いた時刻）
    からの ms（整数）。throat.wav は受けたバイト列を変換せずに書く。1 フレーム
    （4 B）に満たない端は次の読み取りに持ち越す。"""

    def __init__(
        self,
        out: Path,
        subject: str,
        cond: str,
        position: str,
        band: str,
        iface: str,
        t0: float,
        clock=time.monotonic,
    ):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = out / f"{stamp}_{subject}_{cond}"
        # Session と同じく out の直下にだけ作る（docs/decisions/0007）
        if self.dir.resolve().parent != out.resolve():
            sys.exit(
                f"セッションフォルダが {out} の直下になりません: {self.dir}"
            )
        self.dir.mkdir(parents=True, exist_ok=False)
        self.t0 = t0
        self.clock = clock
        self.wav = wave.open(str(self.dir / "throat.wav"), "wb")
        self.wav.setnchannels(THROAT_CHANNELS)
        self.wav.setsampwidth(THROAT_SAMPWIDTH)
        self.wav.setframerate(THROAT_RATE_HZ)
        self.chunks = open(
            self.dir / "throat_chunks.csv", "w", newline="", encoding="utf-8"
        )
        self.chunks_w = csv.writer(self.chunks)
        self.chunks_w.writerow(["t_ms", "sample_index"])
        self.ev = open(
            self.dir / "events.csv", "w", newline="", encoding="utf-8"
        )
        self.ev_w = csv.writer(self.ev)
        self.ev_w.writerow(["t_ms", "label", "note"])
        self.frames = 0
        self.reads = 0  # throat_chunks.csv の行数
        self.first_read_frames = None
        self.first_arrival = self.last_arrival = None
        self._rest = b""
        self._lock = threading.Lock()
        self._closed = False
        self.meta = {
            "subject": subject,
            "cond": cond,
            "position": position,
            "band": band,
            "firmware_sha": git_sha(),
            "sample_rates": {
                "imu_hz": 0,
                "audio_hz": THROAT_RATE_HZ,
                "analog_hz": 0,
            },
            "sensors": [{"id": "throat", "part": THROAT_PART, "iface": iface}],
            "notes": "",
            "fw": "pc-throat",
        }

    def t_ms(self, now: float) -> int:
        return int((now - self.t0) * 1000)

    def feed(self, data: bytes, now: float) -> None:
        """子の標準出力の 1 回の読み取り。now はその読み取りが戻った時刻。"""
        with self._lock:
            if self._closed:
                return
            buf = self._rest + data
            n = len(buf) // THROAT_FRAME_BYTES * THROAT_FRAME_BYTES
            self._rest = buf[n:]
            if n == 0:
                return  # 書いたフレームが 0 の読み取りは行にしない
            k = n // THROAT_FRAME_BYTES
            self.chunks_w.writerow([self.t_ms(now), self.frames])
            self.wav.writeframes(buf[:n])
            self.frames += k
            self.reads += 1
            if self.first_read_frames is None:
                self.first_read_frames = k
                self.first_arrival = now
            self.last_arrival = now

    def elapsed_s(self) -> float:
        """最初の到着から最後の到着までの PC の時間。"""
        if self.first_arrival is None:
            return 0.0
        return self.last_arrival - self.first_arrival

    def est_diff_frames(self) -> int:
        """推定差（診断値）= (T × 48000 + 最初の読み取りのフレーム数) − N。"""
        if self.first_read_frames is None:
            return 0
        return (
            round(self.elapsed_s() * THROAT_RATE_HZ + self.first_read_frames)
            - self.frames
        )

    def mark(self, label: str, note: str = ""):
        """マーカーを1行書く。t_ms はキーを受けた時刻（PC の時計）。close()
        の後は何もしない。"""
        with self._lock:
            if self._closed:
                return
            t = self.t_ms(self.clock())
            self.ev_w.writerow([t, label, note])
            self.ev.flush()
            print(f"  [{t} ms] {label} {note}")

    def close(self, throat: dict | None = None):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for f in (self.ev, self.chunks, self.wav):
                f.close()
        self.meta["throat"] = throat or {}
        (self.dir / "meta.json").write_text(
            json.dumps(self.meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"saved: {self.dir}")


def run_throat(a) -> None:
    """--throat の記録（第 3 節）。異常は sys.exit で終わる。"""
    if os.name == "nt":
        sys.exit("--throat は POSIX のみ")
    a.throat_device, device_source = resolve_throat_device(a)
    err = throat_arg_error(a)
    if err:
        sys.exit(err)
    host, device = a.throat_host, a.throat_device
    kind = THROAT_IFACES[a.throat][1]
    pw_info = None
    if kind == "a2dp":
        # asoundrc の検査と amixer の代わりに PipeWire の状態を確かめる
        err, device, pw_info = pipewire_check(host, device)
        if err:
            sys.exit(err)
        if device_source == "auto":
            print(f"bluez のノードを自動で選んだ: {device}")
        mixer, mixer_error = None, A2DP_MIXER_NOTE
    else:
        err = asoundrc_error(host)
        if err:
            sys.exit(err)
        mixer, mixer_error = read_mixer(host, device)
    # SIGTERM でも下の後始末（子を止める）を通る
    signal.signal(signal.SIGTERM, raise_system_exit)
    if kind == "a2dp":
        argv = throat_argv(host, device, "a2dp")
    else:
        argv = throat_argv(host, device)
    t_launch = time.monotonic()
    try:
        stream = ThroatStream(argv)
    except OSError as e:
        sys.exit(f"子プロセスを起動できません（記録を始めません）: {e}")

    # 最初のバイトを待つ。届くまでフォルダを作らない
    first = None
    why = ""
    try:
        deadline = t_launch + THROAT_START_WAIT_S
        while True:
            d = stream.read(0.05)
            if d:
                first, t0 = d, time.monotonic()
                break
            if stream.eof:
                why = "子プロセスが最初の音声のバイトの前に終わりました"
                break
            if time.monotonic() >= deadline:
                why = f"最初の音声のバイトが {THROAT_START_WAIT_S:g} 秒以内に届きませんでした"
                break
    except KeyboardInterrupt:
        why = "最初の音声のバイトを待つ間に中断しました"
    except BaseException:
        stream.stop(lambda d: None)
        raise
    if first is None:
        stream.stop(lambda d: None)
        sys.exit(
            f"{why}。記録を始めません（子の終了コード {stream.proc.returncode}）\n"
            f"子の標準エラーの末尾:\n{stream.stderr_tail()}"
        )

    sess = ThroatSession(
        RAW_DIR, a.subject, a.cond, a.position, a.band, a.throat, t0
    )
    sess.feed(first, t0)
    # 起動後のリンクの確認（a2dp だけ。別のスレッドで 1 回）
    link = LinkCheck(host, device) if kind == "a2dp" else None
    stall_s = THROAT_STALL_S if kind == "a2dp" else None
    print(f"咽喉マイク: {a.throat}（{host}、{device}）")
    deadline = t0 + a.duration if a.duration > 0 else None

    def on_key(ch):
        if ch in "stcnqb":
            sess.mark(ch)

    is_tty = sys.stdin.isatty()
    tty_mode = TtyMode(sys.stdin.fileno()) if is_tty else None
    if tty_mode is not None:
        atexit.register(tty_mode.restore)  # 端末を変える前に登録する
    if is_tty:
        print("recording... keys: s t c n q b o / Ctrl-C to stop")
    else:
        print("recording... tty が無いのでマーカー入力は無効")
    if deadline is not None:
        print(f"  --duration {a.duration:g} 秒で自動終了する")

    thread = None
    stop = "error"
    try:
        if tty_mode is not None:
            tty_mode.enter()
            reader = KeyReader(
                sys.stdin.fileno(),
                tty_mode,
                on_key,
                lambda note: sess.mark("o", note),
            )
            thread = threading.Thread(target=reader.run, daemon=True)
            thread.start()
        last_data = t0
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                stop = "duration"
                break
            d = stream.read(0.05)
            if d:
                last_data = time.monotonic()
                sess.feed(d, last_data)
            elif stream.eof:
                stop = "child-exit"
                break
            elif (
                stall_s is not None and time.monotonic() - last_data >= stall_s
            ):
                stop = "stall"
                break
    except KeyboardInterrupt:
        stop = "interrupt"
    except SystemExit:
        stop = "interrupt"  # SIGTERM
        raise
    finally:
        stream.stop(lambda d: sess.feed(d, time.monotonic()))
        if tty_mode is not None:
            tty_mode.restore()
            if thread is not None:
                thread.join(timeout=1.0)
        est = sess.est_diff_frames()
        if link is not None:
            # ThroatSession.close() の前に確認を待つ（必ず ok / ng / error）
            link_res, link_note = link.finish()
            pw_info = dict(pw_info or {})
            pw_info["link_check"] = link_res
            pw_info["link_check_note"] = link_note
        info = {"host": host, "device": device}
        if kind == "a2dp":
            info["device_source"] = device_source
        info |= {
            "format": "S16_LE",
            "channels": THROAT_CHANNELS,
            "rate_hz": THROAT_RATE_HZ,
            "remote_command": None if host == "local" else argv[-1],
            "alsa_buffer_size": stream.buffer_size,
            "alsa_period_size": stream.period_size,
            "first_byte_wait_s": round(t0 - t_launch, 3),
            "frames": sess.frames,
            "elapsed_s": round(sess.elapsed_s(), 3),
            "est_diff_frames": est,
            "overrun_lines": stream.overrun_lines,
            "stop": stop,
            "child_returncode": stream.proc.returncode,
            "mixer": mixer,
        }
        if mixer_error is not None:
            info["mixer_error"] = mixer_error
        if kind == "a2dp":
            # pw-record は overrun の行を出さない（0 と書くと取りこぼし 0 と
            # 読まれる）
            info["overrun_lines"] = None
            info["pipewire"] = pw_info
        sess.close(info)
        how = "SIGTERM で止めた" if stream.terminated else "子が自分で終わった"
        overrun = (
            "overrun なし（pw-record は出さない）"
            if kind == "a2dp"
            else f"overrun の行 {stream.overrun_lines}"
        )
        print(
            f"咽喉マイク: フレーム {sess.frames:,}（{sess.frames / THROAT_RATE_HZ:.3f} 秒）"
            f" / 経過 {sess.elapsed_s():.2f} 秒"
            f" / 推定差（診断値）{est:+,} フレーム（{1000 * est / THROAT_RATE_HZ:+.0f} ms）"
            f" / {overrun}"
            f" / 子の終了コード {stream.proc.returncode}（{how}）"
        )
        print(
            f"  推定差は到着の揺れ・滞留・クロックの差を含む診断値。1 ピリオド"
            f"（{1000 * THROAT_PERIOD_FRAMES // THROAT_RATE_HZ} ms）未満は取りこぼしと言わない目安"
        )
        if stream.first_overrun is not None and kind != "a2dp":
            print(f"  最初の overrun の行: {stream.first_overrun}")
        if mixer_error is not None and kind != "a2dp":
            print(
                f"  ミキサーを読めませんでした（記録は続けた）: {mixer_error}"
            )
        if kind == "a2dp":
            note = (
                f"（{pw_info['link_check_note']}）"
                if pw_info.get("link_check_note")
                else ""
            )
            print(
                f"  ノードの形式のレート {pw_info.get('node_rate_hz')} Hz"
                f" / 起動後のリンクの確認 {pw_info.get('link_check')}{note}"
                f" / 止まり方 {stop}"
            )
            print("  0 の区間は tools/throat_check.py で確かめる")
    if stop == "child-exit":
        sys.exit(
            "警告: 子プロセスが記録の途中で終わりました"
            f"（終了コード {stream.proc.returncode}、標準エラーの末尾）\n"
            f"{stream.stderr_tail()}"
        )
    if stop == "stall":
        sys.exit(
            f"警告: 受信が {THROAT_STALL_S:g} 秒止まったので記録を止めました"
            "（stop = stall。この記録は使わない）"
        )


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--port", default=os.environ.get("PORT", "/dev/ttyACM0"))
    ap.add_argument("--baud", type=int, default=2000000)
    ap.add_argument("--subject", default=env_default("SUBJECT", "self"))
    ap.add_argument("--cond", default=env_default("COND", "water"))
    ap.add_argument(
        "--position", default=env_default("POSITION", "midline-below-thyroid")
    )
    ap.add_argument("--band", default=env_default("BAND", "elastic-25mm"))
    ap.add_argument(
        "--duration",
        type=float,
        default=float(env_default("DURATION", "0")),
        help="秒。0 なら Ctrl-C で止めるまで回る",
    )
    ap.add_argument(
        "--indicator",
        default=None,
        help="表示器のポート（例 /dev/serial/by-id/…）。DETECT の led を 1 バイトずつ送る。既定は送らない。"
        "記録を始める前に開けなければ記録を始めない。始めた後に送れなくなっても記録は止めない",
    )
    ap.add_argument(
        "--throat",
        default=None,
        metavar="IFACE",
        help="咽喉マイクだけのセッションにする（装置をつながない。#36）。値は "
        + " / ".join(THROAT_IFACES),
    )
    ap.add_argument(
        "--throat-host",
        default=env_default("THROAT_HOST", THROAT_DEFAULT_HOST),
        help="arecord / pw-record を動かす ssh の宛先（user@host）か local。--throat のときだけ効く",
    )
    ap.add_argument(
        "--throat-device",
        default=None,
        help="USB の iface では ALSA の PCM 名（hw / plughw）、"
        "sh12jk-nz210c-a2dp-unoq では bluez_input.<XX_XX_XX_XX_XX_XX>.<番号>。"
        "省けば環境変数 THROAT_DEVICE、無ければ USB は "
        f"{THROAT_DEFAULT_DEVICE}、a2dp は pw-dump から自動で探す。--throat のときだけ効く",
    )
    a = ap.parse_args()
    if a.subject not in ("self", "p1"):
        sys.exit("subject は self か p1")
    if not re.fullmatch(r"[A-Za-z0-9-]+", a.cond):
        sys.exit("cond は英数字とハイフンのみ")
    if a.throat is not None:
        # 咽喉マイクだけのセッション。既定の動作の通り道は変えない
        run_throat(a)
        return
    err = indicator_port_error(a.port, a.indicator)
    if err:
        sys.exit(err)

    fwd = None
    ind_ser = None
    if a.indicator is not None:
        # セッションのフォルダを作る前に開く。開けなければ記録を始めない（
        # フォルダが空で残らない）
        try:
            ind_ser = serial.Serial(
                a.indicator,
                INDICATOR_BAUD,
                timeout=0,
                write_timeout=INDICATOR_WRITE_TIMEOUT_S,
            )
        except (serial.SerialException, OSError) as e:
            sys.exit(f"表示器のポートを開けません（記録を始めません）: {e}")
        fwd = IndicatorForwarder(ind_ser)
        print(f"表示器: {a.indicator} へ LED の状態（DETECT の led）を送る")

    def stop_before_start(msg: str, ser=None):
        """記録を始めずに止まる。ポートを閉じ、フォルダは作らない。終了コード
        1。"""
        for s in (ser, ind_ser):
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
        sys.exit(msg)

    # 検出器のポートを開き、合図を送って META を待つ（#33）。
    # 受けるまでフォルダを作らない
    try:
        ser = serial.Serial(
            a.port, a.baud, timeout=0.05, write_timeout=DETECTOR_WRITE_TIMEOUT_S
        )
    except (serial.SerialException, OSError) as e:
        stop_before_start(
            f"検出器のポートを開けません（記録を始めません）: {e}"
        )
    try:
        n = ser.write(META_REQUEST)
    except (serial.SerialException, OSError) as e:
        stop_before_start(
            f"検出器に合図を書けません（記録を始めません）: {e}", ser
        )
    if n != len(META_REQUEST):
        stop_before_start(
            f"検出器に合図を書けません（書けたバイト数が {n}）。記録を始めません",
            ser,
        )
    stats = {"ok": {}, "xor_err": 0}
    buf = bytearray()
    try:
        got = wait_meta(ser, META_WAIT_S, a.cond, stats, buf)
    except MetaRejected as e:
        stop_before_start(str(e), ser)
    except KeyboardInterrupt:
        stop_before_start("META を待つ間に中断しました。記録を始めません", ser)
    if got is None:
        stop_before_start(
            f"META が {META_WAIT_S:g} 秒以内に届きませんでした。記録を始めません。\n"
            "  考えられる原因:\n"
            "  - 検出器のファームウェアが #33 より前のもの（合図に応えない）\n"
            "  - 記録ファームウェア（logger）は合図に応えない。USB を挿し直してから始める（docs/decisions/0006）\n"
            f"  - ポートの取り違え（--port {a.port}）",
            ser,
        )
    meta_t_ms, meta_payload, meta = got

    sess = Session(RAW_DIR, a.subject, a.cond, a.position, a.band)
    sess.on_frame(ID_META, meta_t_ms, meta_payload)
    print(f"META を受けました（fw={meta['fw']}）")
    deadline = time.monotonic() + a.duration if a.duration > 0 else None

    def on_key(ch):
        if ch in "stcnqb":
            sess.mark(ch)
        elif ch == "o":
            # POSIX の o は KeyReader が扱う。ここを通るのは Windows
            # の経路だけ。
            sess.mark("o", input("note: "))

    is_tty = sys.stdin.isatty()
    tty_mode = (
        TtyMode(sys.stdin.fileno()) if is_tty and os.name != "nt" else None
    )
    if tty_mode is not None:
        atexit.register(tty_mode.restore)  # 端末を変える前に登録する
    # SIGTERM でも下の finally を通る
    signal.signal(signal.SIGTERM, raise_system_exit)

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
            reader = KeyReader(
                sys.stdin.fileno(),
                tty_mode,
                on_key,
                lambda note: sess.mark("o", note),
            )
            thread = threading.Thread(target=reader.run, daemon=True)
            thread.start()
        elif is_tty:
            thread = threading.Thread(
                target=getch_loop_nt, args=(on_key,), daemon=True
            )
            thread.start()
        if fwd is not None:
            fwd.start()
        # META の後ろに残ったバイトから続ける
        read_frames(
            ser, with_indicator(sess.on_frame, fwd), stats, deadline, buf
        )
    except KeyboardInterrupt:
        pass
    finally:
        if tty_mode is not None:
            tty_mode.restore()  # 印を立てて端末を戻す
            if thread is not None:
                # 止まらなくても終了は妨げない（daemon のまま）
                thread.join(timeout=1.0)
        ser.close()
        sess.close()
        if fwd is not None:
            # 送信のスレッドを止めて表示器のポートを閉じる（何も送らない）
            fwd.close()
        print("正常受信フレーム数:")
        for sid in sorted(stats["ok"]):
            print(
                f"  {STREAM_NAMES.get(sid, f'0x{sid:02X}')}: {stats['ok'][sid]}"
            )
        print(f"XOR 不一致数: {stats['xor_err']}")
        if sess.bad_len:
            print("長さが合わないフレーム（書いていない）:")
            for sid in sorted(sess.bad_len):
                print(
                    f"  {STREAM_NAMES.get(sid, f'0x{sid:02X}')}: {sess.bad_len[sid]}"
                )
        if fwd is not None:
            for line in fwd.summary_lines():
                print(line)


if __name__ == "__main__":
    main()
