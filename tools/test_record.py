"""record.py の DETECT / FEAT の受信とファイルの作り方を合成フレームで検証する（
#23、docs/decisions/0021）。

実行: python -m unittest discover -s tools -v
pty は使わない。record.Session と record.read_frames を直接呼ぶ。フレームは
firmware/logger/frame.h と同じ規則で
build_frame が作る。出力先は tempfile で、data/raw/ は読み書きしない。
r10〜r17 は表示器への転送（--indicator、Issue #29）。表示器のシリアルは偽物（
FakeWriter など）で、実機を使わない。
r18〜r24 は開始時の合図と META の待ち（Issue #33）。record.main()
を偽物の検出器（FakeDetector）で通す。record.serial.Serial と
record.RAW_DIR（tempfile）と sys.argv・sys.stdin（tty でない）を差し替え、
record.META_WAIT_S を短くする。
r25〜r25c は SYNC の途中で read() が切れてもフレームを失わないこと（Issue
#34）。ChunkFakeSerial で read() の切れ目を作る。
"""

from __future__ import annotations
import contextlib, csv, io, json, os, serial, signal, struct, sys
import tempfile, threading, time, unittest, wave
from unittest import mock
from pathlib import Path

import record

N_FEAT = 5


def build_frame(sid: int, t_ms: int, payload: bytes) -> bytes:
    """[A5 5A][id u8][len u16 LE][t_ms u32 LE][payload][xor u8]。xor は
    id〜payload の XOR（frame.h と同じ）。"""
    body = bytes([sid]) + struct.pack("<HI", len(payload), t_ms) + payload
    x = 0
    for b in body:
        x ^= b
    return record.SYNC + body + bytes([x])


def detect_payload(
    window_t_ms: int, prob: float, positive: int, led: int
) -> bytes:
    return struct.pack("<IfBB", window_t_ms, prob, positive, led)


def feat_payload(window_t_ms: int, values: list[float]) -> bytes:
    return struct.pack(f"<I{len(values)}f", window_t_ms, *values)


def f32(x: float) -> float:
    return struct.unpack("<f", struct.pack("<f", x))[0]


def read_csv(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


class FakeSerial:
    """最初の read() でバイト列を全部返し、以後は空。"""

    def __init__(self, data: bytes):
        self.data = data

    def read(self, n: int) -> bytes:
        d, self.data = self.data, b""
        if not d:
            time.sleep(0.005)
        return d


def split_before_sync_second_byte(data: bytes) -> list[bytes]:
    """data を各フレームの SYNC（A5 5A）の間で切る（#34 r25）。
    返す要素をそのまま順に read() させると、
    1 回の read() が …A5 で終わり、次が 5A… で始まる（SYNC
    の途中で読み取りが切れる状況を再現する）。"""
    cuts = []
    i = 0
    while True:
        j = data.find(record.SYNC, i)
        if j < 0:
            break
        cuts.append(j + 1)
        i = j + 2
    chunks, prev = [], 0
    for c in cuts:
        chunks.append(data[prev:c])
        prev = c
    chunks.append(data[prev:])
    return chunks


class ChunkFakeSerial:
    """chunks を 1 回の read() に 1 つずつ返す（#34 r25・r25b）。尽きたら
    FakeSerial と同じく少し待って空を返す。"""

    def __init__(self, chunks):
        self.chunks = list(chunks)

    def read(self, n: int) -> bytes:
        if self.chunks:
            return self.chunks.pop(0)
        time.sleep(0.005)
        return b""


class FakeWriter:
    """表示器の代わり。書いたバイトを控え、len を返す。"""

    def __init__(self):
        self.written = bytearray()
        self.closed = False

    def write(self, data: bytes) -> int:
        self.written += data
        return len(data)

    def close(self):
        self.closed = True


class FailingWriter(FakeWriter):
    """毎回 serial.SerialException を出す（USB を抜いた表示器の代わり）。"""

    def write(self, data: bytes) -> int:
        raise serial.SerialException("fake: 書けない")


class ShortWriter(FakeWriter):
    """書けたバイト数 0 を返す。"""

    def write(self, data: bytes) -> int:
        return 0


class BlockingWriter(FakeWriter):
    """write に入ったら entered を立て、release が立つまで write
    の中で待つ（詰まった表示器の代わり）。"""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def write(self, data: bytes) -> int:
        self.entered.set()
        self.release.wait()
        return super().write(data)


class FakeDetector:
    """検出器の代わり（#33）。pre は合図の前から届いていたバイト（1 回の read()
    で返す）。合図 `M` を書かれた後は
    chunks を 1 回の read() に 1 つずつ返し、尽きたら少し待って空を返す。
    write_error / write_result で合図の失敗を真似る。"""

    def __init__(
        self, chunks=(), pre: bytes = b"", write_error=None, write_result=None
    ):
        self.pre = pre
        self.chunks = list(chunks)
        self.write_error = write_error
        self.write_result = write_result
        self.written = bytearray()
        self.requested = False
        self.closed = False
        self.kwargs = {}

    def write(self, data: bytes) -> int:
        if self.write_error is not None:
            raise self.write_error
        self.written += data
        if data == record.META_REQUEST:
            self.requested = True
        return len(data) if self.write_result is None else self.write_result

    def read(self, n: int) -> bytes:
        if self.pre:
            d, self.pre = self.pre, b""
            return d
        if self.requested and self.chunks:
            return self.chunks.pop(0)
        time.sleep(0.005)
        return b""

    def close(self):
        self.closed = True


DET_PORT, IND_PORT = "/fake/detector", "/fake/indicator"


def meta_bytes(**kw) -> bytes:
    return json.dumps(kw).encode("utf-8")


DET_META = {
    "fw": "detector",
    "threshold": 0.95,
    "model": {"source": "edge-impulse", "project_id": 1, "deploy_version": 2},
    "feature_set": "m2-0020",
    "feature_names": [f"x{i}" for i in range(N_FEAT)],
}


def det(w: int, led: int = 1) -> bytes:
    return build_frame(
        record.ID_DETECT, w + 1205, detect_payload(w, 0.5, 0, led)
    )


def feat(w: int) -> bytes:
    return build_frame(
        record.ID_FEAT, w + 1206, feat_payload(w, [float(w)] * N_FEAT)
    )


def wait_until(pred, timeout: float = 1.0) -> bool:
    """pred が真になるのを最大 timeout 秒待つ。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.002)
    return pred()


class RecordSessionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.sess = record.Session(self.root, "self", "meal", "pos", "band")

    def files(self) -> set[str]:
        return {p.name for p in self.sess.dir.iterdir()}

    def test_r1_detect_frames_write_detect_csv_only(self):
        probs = [0.1, 0.9, f32(0.9)]
        # 装置は float32 で比べるので f32(0.9) >= 0.9f は真
        positives = [0, 1, 1]
        for i, (p, pos) in enumerate(zip(probs, positives)):
            self.sess.on_frame(
                record.ID_DETECT,
                2030 + 250 * i,
                detect_payload(1000 + 250 * i, p, pos, 1 + pos),
            )
        self.sess.close()
        rows = read_csv(self.sess.dir / "detect.csv")
        self.assertEqual(
            rows[0], ["t_ms", "window_t_ms", "positive", "prob", "led"]
        )
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[1], ["2030", "1000", "0", f"{f32(0.1):.9g}", "1"])
        self.assertEqual(rows[3][:3], ["2530", "1500", "1"])
        for row, p in zip(rows[1:], probs):
            # .9g は float32 を往復する
            self.assertEqual(f32(float(row[3])), f32(p))
        self.assertEqual(
            self.files(), {"detect.csv", "events.csv", "meta.json"}
        )

    def test_r2_feat_frame_writes_feat_csv(self):
        values = [0.0123, -3.5, 4321.0, 1e-3, 0.333333]
        self.sess.on_frame(record.ID_FEAT, 2032, feat_payload(1000, values))
        self.sess.close()
        rows = read_csv(self.sess.dir / "feat.csv")
        self.assertEqual(
            rows[0], ["t_ms", "window_t_ms", "f0", "f1", "f2", "f3", "f4"]
        )
        self.assertEqual(rows[1][:2], ["2032", "1000"])
        for s, v in zip(rows[1][2:], values):
            self.assertEqual(f32(float(s)), f32(v))
        self.assertEqual(self.files(), {"feat.csv", "events.csv", "meta.json"})

    def test_r3_feat_with_different_n_is_counted_not_written(self):
        self.sess.on_frame(
            record.ID_FEAT, 2032, feat_payload(1000, [1.0] * N_FEAT)
        )
        self.sess.on_frame(
            record.ID_FEAT, 2282, feat_payload(1250, [1.0] * (N_FEAT + 1))
        )
        self.sess.on_frame(
            record.ID_FEAT, 2532, feat_payload(1500, [2.0] * N_FEAT)
        )
        self.sess.close()
        rows = read_csv(self.sess.dir / "feat.csv")
        self.assertEqual([r[1] for r in rows[1:]], ["1000", "1500"])
        self.assertEqual(self.sess.bad_len, {record.ID_FEAT: 1})

    def test_r4_marker_uses_header_t_ms_not_window_t_ms(self):
        self.sess.on_frame(
            record.ID_DETECT, 2030, detect_payload(1000, 0.5, 0, 1)
        )
        self.sess.mark("s")
        self.sess.on_frame(
            record.ID_FEAT, 2032, feat_payload(1000, [0.0] * N_FEAT)
        )
        self.sess.mark("c")
        self.sess.close()
        rows = read_csv(self.sess.dir / "events.csv")
        self.assertEqual(rows[1:], [["2030", "s", ""], ["2032", "c", ""]])

    def test_r5_meta_merge_and_feature_names_warning(self):
        meta = {
            "fw": "detector",
            "threshold": 0.9,
            "feature_names": [f"x{i}" for i in range(N_FEAT)],
        }
        self.sess.on_frame(
            record.ID_META, 100, json.dumps(meta).encode("utf-8")
        )
        self.sess.on_frame(
            record.ID_FEAT, 2032, feat_payload(1000, [0.0] * N_FEAT)
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.sess.close()
        got = json.loads(
            (self.sess.dir / "meta.json").read_text(encoding="utf-8")
        )
        self.assertEqual(got["fw"], "detector")
        self.assertEqual(got["feature_names"], meta["feature_names"])
        self.assertEqual(got["subject"], "self")
        # PC 側のキーは残る（docs/decisions/0006）
        self.assertIn("sample_rates", got)
        self.assertNotIn("警告", out.getvalue())

    def test_r5b_feature_names_length_mismatch_warns(self):
        meta = {"fw": "detector", "feature_names": ["a", "b"]}
        self.sess.on_frame(
            record.ID_META, 100, json.dumps(meta).encode("utf-8")
        )
        self.sess.on_frame(
            record.ID_FEAT, 2032, feat_payload(1000, [0.0] * N_FEAT)
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.sess.close()
        self.assertIn("警告", out.getvalue())
        got = json.loads(
            (self.sess.dir / "meta.json").read_text(encoding="utf-8")
        )
        # 装置の値のまま。直さない
        self.assertEqual(got["feature_names"], ["a", "b"])

    def test_r6_no_frames_leaves_events_and_meta_only(self):
        self.sess.mark("s")
        self.sess.close()
        self.assertEqual(self.files(), {"events.csv", "meta.json"})
        self.assertEqual(
            read_csv(self.sess.dir / "events.csv"),
            [["t_ms", "label", "note"], ["0", "s", ""]],
        )

    def test_r7_imu_and_audio_files_keep_their_format(self):
        self.sess.on_frame(
            record.ID_IMU,
            1000,
            struct.pack("<6f", 0.01, -0.02, 1.0, 1.5, -2.25, 3.125),
        )
        self.sess.on_frame(
            record.ID_AUDIO, 1004, struct.pack("<64h", *range(64))
        )
        self.sess.on_frame(
            record.ID_AUDIO, 1008, struct.pack("<64h", *range(64))
        )
        self.sess.close()
        self.assertEqual(
            self.files(),
            {
                "imu.csv",
                "audio.wav",
                "audio_chunks.csv",
                "events.csv",
                "meta.json",
            },
        )
        imu = read_csv(self.sess.dir / "imu.csv")
        self.assertEqual(imu[0], ["t_ms", "ax", "ay", "az", "gx", "gy", "gz"])
        self.assertEqual(
            imu[1],
            [
                "1000",
                "0.0100",
                "-0.0200",
                "1.0000",
                "1.5000",
                "-2.2500",
                "3.1250",
            ],
        )
        chunks = read_csv(self.sess.dir / "audio_chunks.csv")
        self.assertEqual(
            chunks, [["t_ms", "sample_index"], ["1004", "0"], ["1008", "64"]]
        )
        with wave.open(str(self.sess.dir / "audio.wav"), "rb") as w:
            self.assertEqual(
                (
                    w.getnchannels(),
                    w.getsampwidth(),
                    w.getframerate(),
                    w.getnframes(),
                ),
                (1, 2, 16000, 128),
            )

    def test_r8_read_frames_counts_streams_and_xor_errors(self):
        data = build_frame(
            record.ID_DETECT, 2030, detect_payload(1000, 0.5, 0, 1)
        )
        data += build_frame(
            record.ID_FEAT, 2032, feat_payload(1000, [1.0] * N_FEAT)
        )
        data += build_frame(0x06, 2040, b"\x01\x02\x03")  # 未知の ID
        bad = bytearray(
            build_frame(record.ID_DETECT, 2280, detect_payload(1250, 0.5, 0, 1))
        )
        bad[-1] ^= 0xFF  # XOR 不一致
        data += bytes(bad)
        data += build_frame(
            record.ID_DETECT, 2530, detect_payload(1500, 0.95, 1, 2)
        )
        stats = {"ok": {}, "xor_err": 0}
        record.read_frames(
            FakeSerial(data),
            self.sess.on_frame,
            stats,
            deadline=time.monotonic() + 0.05,
        )
        self.sess.close()
        self.assertEqual(
            stats["ok"], {record.ID_DETECT: 2, record.ID_FEAT: 1, 0x06: 1}
        )
        self.assertEqual(stats["xor_err"], 1)
        rows = read_csv(self.sess.dir / "detect.csv")
        self.assertEqual([r[1] for r in rows[1:]], ["1000", "1500"])
        self.assertEqual(
            self.files(), {"detect.csv", "feat.csv", "events.csv", "meta.json"}
        )

    def test_r9_wrong_length_frames_are_counted_not_written(self):
        self.sess.on_frame(
            record.ID_DETECT, 2030, detect_payload(1000, 0.5, 0, 1) + b"\x00"
        )
        self.sess.on_frame(record.ID_DETECT, 2280, b"\x00" * 9)
        self.sess.on_frame(record.ID_IMU, 1000, b"\x00" * 20)
        self.sess.on_frame(record.ID_FEAT, 2032, b"\x00" * 7)
        self.sess.on_frame(record.ID_FEAT, 2032, b"\x00" * 9)
        self.sess.close()
        self.assertEqual(
            self.sess.bad_len,
            {record.ID_DETECT: 2, record.ID_IMU: 1, record.ID_FEAT: 2},
        )
        self.assertEqual(self.files(), {"events.csv", "meta.json"})

    def test_r25_sync_split_between_a5_and_5a_does_not_lose_frames(self):
        data = b"".join(det(w) + feat(w) for w in (1000, 1250, 1500))
        chunks = split_before_sync_second_byte(data)
        for ch in chunks[:-1]:
            self.assertEqual(ch[-1], record.SYNC[0])
        for ch in chunks[1:]:
            self.assertEqual(ch[0], record.SYNC[1])
        stats = {"ok": {}, "xor_err": 0}
        record.read_frames(
            ChunkFakeSerial(chunks),
            self.sess.on_frame,
            stats,
            deadline=time.monotonic() + 0.05,
        )
        self.sess.close()
        self.assertEqual(stats["ok"], {record.ID_DETECT: 3, record.ID_FEAT: 3})
        self.assertEqual(stats["xor_err"], 0)
        self.assertEqual(
            [r[1] for r in read_csv(self.sess.dir / "detect.csv")[1:]],
            ["1000", "1250", "1500"],
        )
        self.assertEqual(
            [r[1] for r in read_csv(self.sess.dir / "feat.csv")[1:]],
            ["1000", "1250", "1500"],
        )

    def test_r25b_sync_split_byte_by_byte_does_not_lose_frames(self):
        data = b"".join(det(w) + feat(w) for w in (1000, 1250, 1500))
        chunks = [data[i : i + 1] for i in range(len(data))]
        stats = {"ok": {}, "xor_err": 0}
        record.read_frames(
            ChunkFakeSerial(chunks),
            self.sess.on_frame,
            stats,
            deadline=time.monotonic() + 0.05,
        )
        self.sess.close()
        self.assertEqual(stats["ok"], {record.ID_DETECT: 3, record.ID_FEAT: 3})
        self.assertEqual(stats["xor_err"], 0)
        self.assertEqual(
            [r[1] for r in read_csv(self.sess.dir / "detect.csv")[1:]],
            ["1000", "1250", "1500"],
        )
        self.assertEqual(
            [r[1] for r in read_csv(self.sess.dir / "feat.csv")[1:]],
            ["1000", "1250", "1500"],
        )

    def test_r25c_garbage_without_trailing_sync_byte_is_discarded(self):
        # SYNC が無く、末尾も 0xA5 ではない
        buf = bytearray(b"\x00\x01\x02\xff\x10")
        stats = {"ok": {}, "xor_err": 0}
        stop = record.parse_frames(buf, lambda *a: False, stats)
        self.assertFalse(stop)
        self.assertEqual(buf, bytearray())
        self.assertEqual(stats, {"ok": {}, "xor_err": 0})


class IndicatorForwarderTest(unittest.TestCase):
    """表示器への転送（Issue #29 plan 第 6.4 節）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def new_session(self, cond: str) -> record.Session:
        return record.Session(self.root, "self", cond, "pos", "band")

    def test_r10_forwarder_sends_detect_led_only(self):
        sess = self.new_session("water")
        w = FakeWriter()
        fwd = record.IndicatorForwarder(w)
        fwd.start()
        on_frame = record.with_indicator(sess.on_frame, fwd)
        data = build_frame(
            record.ID_DETECT, 2030, detect_payload(1000, 0.95, 1, 1)
        )
        data += build_frame(
            record.ID_FEAT, 2032, feat_payload(1000, [1.0] * N_FEAT)
        )
        data += build_frame(
            record.ID_META, 100, json.dumps({"fw": "detector"}).encode("utf-8")
        )
        data += build_frame(
            record.ID_IMU, 1000, struct.pack("<6f", 0, 0, 1, 0, 0, 0)
        )
        # 長さ違い
        data += build_frame(
            record.ID_DETECT, 2280, detect_payload(1250, 0.5, 0, 1) + b"\x00"
        )
        # led = 3
        data += build_frame(
            record.ID_DETECT, 2530, detect_payload(1500, 0.5, 0, 3)
        )
        bad = bytearray(
            build_frame(record.ID_DETECT, 2780, detect_payload(1750, 0.5, 0, 2))
        )
        bad[-1] ^= 0xFF  # XOR 不一致
        data += bytes(bad)
        stats = {"ok": {}, "xor_err": 0}
        record.read_frames(
            FakeSerial(data), on_frame, stats, deadline=time.monotonic() + 0.05
        )
        self.assertTrue(wait_until(lambda: fwd.sent == 1))
        self.assertEqual(bytes(w.written), b"\x01")
        self.assertEqual(fwd.skipped, 2)
        self.assertEqual(fwd.failed, 0)
        record.read_frames(
            FakeSerial(
                build_frame(
                    record.ID_DETECT, 3030, detect_payload(2000, 0.95, 1, 2)
                )
            ),
            on_frame,
            stats,
            deadline=time.monotonic() + 0.05,
        )
        self.assertTrue(wait_until(lambda: fwd.sent == 2))
        self.assertEqual(bytes(w.written), b"\x01\x02")
        fwd.close()
        sess.close()
        self.assertEqual((fwd.sent, fwd.skipped, fwd.failed), (2, 2, 0))
        self.assertTrue(fwd.thread_stopped)
        self.assertTrue(w.closed)

    def test_r11_files_same_with_and_without_indicator(self):
        def run(sess, on_frame):
            for i in range(8):
                on_frame(
                    record.ID_DETECT,
                    2030 + 250 * i,
                    detect_payload(1000 + 250 * i, 0.1 * i, i % 2, 1 + i % 2),
                )
                on_frame(
                    record.ID_FEAT,
                    2032 + 250 * i,
                    feat_payload(1000 + 250 * i, [float(i)] * N_FEAT),
                )
                sess.mark("s")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            plain = self.new_session("water")
            run(plain, record.with_indicator(plain.on_frame, None))
            plain.close()

            with_ind = self.new_session("meal")
            w = BlockingWriter()
            fwd = record.IndicatorForwarder(w)
            fwd.start()
            on_frame = record.with_indicator(with_ind.on_frame, fwd)
            run(with_ind, on_frame)  # 最初の write の中で詰まったまま流す
            self.assertTrue(w.entered.wait(1.0))
            with_ind.close()
            w.release.set()
            fwd.close()
        for name in ("detect.csv", "feat.csv", "events.csv"):
            self.assertEqual(
                read_csv(plain.dir / name), read_csv(with_ind.dir / name), name
            )
        self.assertEqual(
            {p.name for p in plain.dir.iterdir()},
            {p.name for p in with_ind.dir.iterdir()},
        )

    def test_r16_blocked_writer_does_not_delay_on_frame(self):
        sess = self.new_session("water")
        w = BlockingWriter()
        fwd = record.IndicatorForwarder(w)
        fwd.start()
        on_frame = record.with_indicator(sess.on_frame, fwd)
        on_frame(record.ID_DETECT, 2030, detect_payload(1000, 0.95, 1, 2))
        # 送信のスレッドが 1 本目の write の中で止まる
        self.assertTrue(w.entered.wait(1.0))
        t0 = time.perf_counter()
        for i in range(1, 20):
            on_frame(
                record.ID_DETECT,
                2030 + 250 * i,
                detect_payload(1000 + 250 * i, 0.1, 0, 1),
            )
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, record.INDICATOR_WRITE_TIMEOUT_S)
        self.assertEqual(fwd.replaced, 18)
        w.release.set()
        self.assertTrue(wait_until(lambda: fwd.sent == 2))
        fwd.close()
        sess.close()
        # 詰まっていた 1 本と最後の 1 本
        self.assertEqual(bytes(w.written), b"\x02\x01")
        self.assertEqual(len(read_csv(sess.dir / "detect.csv")), 21)

    def test_r12_failures_do_not_stop_recording(self):
        sess = self.new_session("water")
        fwd = record.IndicatorForwarder(FailingWriter())
        fwd.start()
        on_frame = record.with_indicator(sess.on_frame, fwd)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for i in range(3):
                on_frame(
                    record.ID_DETECT,
                    2030 + 250 * i,
                    detect_payload(1000 + 250 * i, 0.1, 0, 1),
                )
                self.assertTrue(wait_until(lambda: fwd.failed == i + 1))
            fwd.close()
            sess.close()
        self.assertEqual(len(read_csv(sess.dir / "detect.csv")), 4)
        self.assertEqual((fwd.sent, fwd.failed), (0, 3))
        self.assertEqual(
            out.getvalue().count("警告: 表示器に送れませんでした"), 1
        )

        fwd2 = record.IndicatorForwarder(ShortWriter())
        fwd2.start()
        with contextlib.redirect_stdout(io.StringIO()):
            fwd2.offer(detect_payload(1000, 0.1, 0, 1))
            self.assertTrue(wait_until(lambda: fwd2.failed == 1))
            fwd2.close()
        self.assertEqual((fwd2.sent, fwd2.failed), (0, 1))

    def test_r13_without_indicator_is_unchanged(self):
        sess = self.new_session("water")
        # 束縛メソッドは参照のたびに別の物になるので 1 回だけ取る
        on_frame = sess.on_frame
        self.assertIs(record.with_indicator(on_frame, None), on_frame)
        sess.close()

    def test_r14_indicator_port_error(self):
        target = self.root / "ttyFAKE0"
        target.write_bytes(b"")
        link = self.root / "by-id-link"
        os.symlink(target, link)
        other = self.root / "ttyFAKE1"
        self.assertIsNotNone(
            record.indicator_port_error(str(target), str(target))
        )
        self.assertIsNotNone(
            record.indicator_port_error(str(target), str(link))
        )
        self.assertIsNone(record.indicator_port_error(str(target), str(other)))
        self.assertIsNone(record.indicator_port_error(str(target), None))

    def test_r15_summary_lines(self):
        fwd = record.IndicatorForwarder(FakeWriter())
        fwd.sent, fwd.failed, fwd.skipped, fwd.replaced = 239, 0, 0, 0
        self.assertEqual(
            fwd.summary_lines(),
            [
                "表示器への転送（DETECT の led）: 送った 239 / 失敗 0 / 送らなかった 0 / 置き換えた 0"
            ],
        )
        fwd.sent, fwd.failed, fwd.skipped, fwd.replaced = 120, 5, 1, 2
        fwd.first_error = "fake: 書けない"
        lines = fwd.summary_lines()
        self.assertEqual(
            lines[0],
            "表示器への転送（DETECT の led）: 送った 120 / 失敗 5 / 送らなかった 1 / 置き換えた 2",
        )
        self.assertEqual(lines[1], "  最初の失敗: fake: 書けない")
        self.assertEqual(len(lines), 2)
        fwd.thread_stopped = False
        self.assertIn(
            "  送信のスレッドが止まりませんでした（件数は終了時点のもの）",
            fwd.summary_lines(),
        )

    def test_r17_close_stops_thread(self):
        w = FakeWriter()
        fwd = record.IndicatorForwarder(w)
        fwd.start()
        fwd.close()
        self.assertFalse(fwd._thread.is_alive())
        self.assertTrue(fwd.thread_stopped)
        self.assertTrue(w.closed)

        bw = BlockingWriter()
        fwd2 = record.IndicatorForwarder(bw)
        fwd2.start()
        fwd2.offer(detect_payload(1000, 0.1, 0, 1))
        self.assertTrue(bw.entered.wait(1.0))
        t0 = time.monotonic()
        fwd2.close()
        elapsed = time.monotonic() - t0
        try:
            self.assertLess(elapsed, 1.5)
            self.assertFalse(fwd2.thread_stopped)
            self.assertTrue(fwd2._thread.daemon)
            self.assertIn(
                "  送信のスレッドが止まりませんでした（件数は終了時点のもの）",
                fwd2.summary_lines(),
            )
        finally:
            bw.release.set()  # スレッドを終わらせる
            fwd2._thread.join(timeout=1.0)
        self.assertFalse(fwd2._thread.is_alive())


class MetaStartTest(unittest.TestCase):
    """開始時の合図と META の待ち（Issue #33 plan 2.3、4.1 r18〜r24）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.raw = Path(self._tmp.name)
        old_term = signal.getsignal(signal.SIGTERM)
        # main() が SIGTERM の扱いを変えるので戻す
        self.addCleanup(signal.signal, signal.SIGTERM, old_term)

    def run_main(
        self,
        detector: FakeDetector,
        *args: str,
        indicator: FakeWriter | None = None,
        wait_s: float = 0.3,
    ) -> tuple[int, str]:
        """record.main() を偽物で通す。(終了コード, 標準出力と標準エラー)
        を返す。"""

        def fake_serial(port, baud, **kw):
            if port == DET_PORT:
                detector.kwargs = kw
                return detector
            if port == IND_PORT and indicator is not None:
                return indicator
            raise serial.SerialException(f"fake: {port} が無い")

        argv = ["record.py", "--port", DET_PORT, *args]
        if indicator is not None:
            argv += ["--indicator", IND_PORT]
        out = io.StringIO()
        code = 0
        with (
            mock.patch.object(record.serial, "Serial", fake_serial),
            mock.patch.object(record, "RAW_DIR", self.raw),
            mock.patch.object(record, "META_WAIT_S", wait_s),
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

    def test_r18_request_once_skip_before_meta_carry_over_after(self):
        for with_ind in (False, True):
            with self.subTest(indicator=with_ind):
                for p in self.sessions():
                    for f in p.iterdir():
                        f.unlink()
                    p.rmdir()
                # META の前に DETECT・FEAT（記録の開始前）。META と同じ read()
                # の後ろに DETECT(led 2)
                d = FakeDetector(
                    [
                        det(1000, 1)
                        + feat(1000)
                        + build_frame(
                            record.ID_META, 100, meta_bytes(**DET_META)
                        )
                        + det(1250, 2)
                    ]
                )
                ind = FakeWriter() if with_ind else None
                code, out = self.run_main(d, "--duration", "0.2", indicator=ind)
                self.assertEqual(code, 0, out)
                self.assertEqual(bytes(d.written), b"M")  # 合図はちょうど 1 回
                self.assertEqual(
                    d.kwargs.get("write_timeout"),
                    record.DETECTOR_WRITE_TIMEOUT_S,
                )
                self.assertIn("META を受けました（fw=detector）", out)
                sd = self.one_session()
                rows = read_csv(sd / "detect.csv")
                # META の前の 1000 は書かない
                self.assertEqual([r[1] for r in rows[1:]], ["1250"])
                self.assertFalse((sd / "feat.csv").exists())
                self.assertIn("  META: 1", out)
                self.assertIn("  DETECT: 1", out)
                self.assertNotIn("FEAT", out)  # 開始前の FEAT は数えない
                if with_ind:
                    # 引き継いだ DETECT の led が表示器へ（C4）
                    self.assertEqual(bytes(ind.written), b"\x02")
                    self.assertTrue(ind.closed)
                    self.assertNotIn(b"M", bytes(ind.written))

    def test_r18b_wait_meta_returns_meta_and_leaves_rest(self):
        d = FakeDetector(
            [
                det(1000)
                + build_frame(record.ID_META, 100, meta_bytes(**DET_META))
                + det(1250)[:7]
            ]
        )
        d.requested = True
        stats = {"ok": {}, "xor_err": 0}
        buf = bytearray()
        got = record.wait_meta(d, 0.3, "water", stats, buf)
        self.assertIsNotNone(got)
        t_ms, payload, meta = got
        self.assertEqual((t_ms, meta["fw"]), (100, "detector"))
        self.assertEqual(json.loads(payload), DET_META)
        self.assertEqual(stats, {"ok": {record.ID_META: 1}, "xor_err": 0})
        self.assertEqual(bytes(buf), det(1250)[:7])  # 途中のフレームは次の段へ

    def test_r19_no_meta_stops_without_folder(self):
        cases = {
            "detect only": [det(1000) + feat(1000), det(1250)],
            "nothing": [],
        }
        for name, chunks in cases.items():
            for with_ind in (False, True):
                with self.subTest(name, indicator=with_ind):
                    d = FakeDetector(chunks)
                    ind = FakeWriter() if with_ind else None
                    code, out = self.run_main(
                        d, "--duration", "0.2", indicator=ind, wait_s=0.15
                    )
                    self.assertEqual(code, 1, out)
                    self.assertIn(
                        "META が 0.15 秒以内に届きませんでした。記録を始めません",
                        out,
                    )
                    self.assertIn("logger", out)
                    self.assertNotIn("recording...", out)
                    self.assertEqual(self.sessions(), [])
                    self.assertTrue(d.closed)
                    if with_ind:
                        self.assertTrue(ind.closed)
                        self.assertEqual(bytes(ind.written), b"")

    def test_r20_two_metas_same_meta_json(self):
        m = build_frame(record.ID_META, 100, meta_bytes(**DET_META))
        m2 = build_frame(record.ID_META, 900, meta_bytes(**DET_META))
        code, out = self.run_main(
            FakeDetector([m, m2 + det(1000)]), "--duration", "0.2"
        )
        self.assertEqual(code, 0, out)
        two = json.loads(
            (self.one_session() / "meta.json").read_text(encoding="utf-8")
        )
        self.assertIn("  META: 2", out)
        for p in self.sessions():
            for f in p.iterdir():
                f.unlink()
            p.rmdir()
        code, out = self.run_main(
            FakeDetector([m + det(1000)]), "--duration", "0.2"
        )
        self.assertEqual(code, 0, out)
        one = json.loads(
            (self.one_session() / "meta.json").read_text(encoding="utf-8")
        )
        self.assertIn("  META: 1", out)
        for k in ("fw", "threshold", "model", "feature_names"):
            self.assertEqual(two[k], DET_META[k])
        two.pop("firmware_sha")
        one.pop("firmware_sha")
        self.assertEqual(two, one)

    def test_r21_request_write_fails_stops(self):
        cases = {
            "exception": dict(
                write_error=serial.SerialTimeoutException("fake: write timeout")
            ),
            "oserror": dict(write_error=OSError("fake: gone")),
            "zero bytes": dict(write_result=0),
        }
        for name, kw in cases.items():
            with self.subTest(name):
                d = FakeDetector(
                    [build_frame(record.ID_META, 100, meta_bytes(**DET_META))],
                    **kw,
                )
                code, out = self.run_main(d, "--duration", "0.2")
                self.assertEqual(code, 1, out)
                self.assertIn("検出器に合図を書けません", out)
                self.assertEqual(self.sessions(), [])
                self.assertTrue(d.closed)
        # 検出器のポートが開けない
        code, out = self.run_main(
            FakeDetector(), "--port", "/fake/none", "--duration", "0.2"
        )
        self.assertEqual(code, 1, out)
        self.assertIn("検出器のポートを開けません", out)
        self.assertEqual(self.sessions(), [])

    def test_r22_unusable_meta_keeps_waiting(self):
        bad_xor = bytearray(
            build_frame(record.ID_META, 100, meta_bytes(**DET_META))
        )
        bad_xor[-1] ^= 0xFF
        bad = {
            "xor": bytes(bad_xor),
            "not json": build_frame(record.ID_META, 100, b"{fw: detector"),
            "not utf-8": build_frame(record.ID_META, 100, b"\xff\xfe"),
            "list": build_frame(record.ID_META, 100, b'["fw", "detector"]'),
            "no fw": build_frame(
                record.ID_META, 100, meta_bytes(threshold=0.9)
            ),
            "fw null": build_frame(record.ID_META, 100, meta_bytes(fw=None)),
            "fw number": build_frame(record.ID_META, 100, meta_bytes(fw=1)),
            "fw empty": build_frame(record.ID_META, 100, meta_bytes(fw="")),
        }
        for name, frame in bad.items():
            with self.subTest(name):
                d = FakeDetector([frame + det(1000)])
                code, out = self.run_main(d, "--duration", "0.2", wait_s=0.15)
                self.assertEqual(code, 1, out)
                self.assertIn("META が 0.15 秒以内に届きませんでした", out)
                self.assertEqual(self.sessions(), [])
        # 届かない扱いの META の後に使える META が来れば、そこから始まる
        d = FakeDetector(
            [
                bad["fw null"] + det(900),
                build_frame(record.ID_META, 200, meta_bytes(**DET_META))
                + det(1000),
            ]
        )
        code, out = self.run_main(d, "--duration", "0.2")
        self.assertEqual(code, 0, out)
        self.assertEqual(
            [r[1] for r in read_csv(self.one_session() / "detect.csv")[1:]],
            ["1000"],
        )
        self.assertIn("  META: 1", out)

    def test_r22b_detector_meta_keys_and_meal(self):
        for key in ("threshold", "model", "feature_names"):
            with self.subTest(missing=key):
                m = {k: v for k, v in DET_META.items() if k != key}
                code, out = self.run_main(
                    FakeDetector(
                        [build_frame(record.ID_META, 100, meta_bytes(**m))]
                    ),
                    "--duration",
                    "0.2",
                )
                self.assertEqual(code, 1, out)
                self.assertIn(
                    f"検出器の META に {key} がありません。記録を始めません",
                    out,
                )
                self.assertEqual(self.sessions(), [])
        for name, m in {
            "threshold bool": dict(DET_META, threshold=True),
            "model list": dict(DET_META, model=[]),
            "feature_names empty": dict(DET_META, feature_names=[]),
        }.items():
            with self.subTest(name):
                code, out = self.run_main(
                    FakeDetector(
                        [build_frame(record.ID_META, 100, meta_bytes(**m))]
                    ),
                    "--duration",
                    "0.2",
                )
                self.assertEqual(code, 1, out)
                self.assertIn("検出器の META に", out)
                self.assertEqual(self.sessions(), [])
        logger_meta = build_frame(
            record.ID_META, 50, meta_bytes(fw="logger", imu_hz=104)
        )
        # logger の META は --cond meal 以外ならそのまま受ける（3
        # つのキーを要求しない）
        code, out = self.run_main(
            FakeDetector([logger_meta]), "--cond", "water", "--duration", "0.2"
        )
        self.assertEqual(code, 0, out)
        self.assertIn("META を受けました（fw=logger）", out)
        got = json.loads(
            (self.one_session() / "meta.json").read_text(encoding="utf-8")
        )
        self.assertEqual(got["fw"], "logger")
        self.assertNotIn("threshold", got)
        self.assertEqual(got["cond"], "water")
        for p in self.sessions():
            for f in p.iterdir():
                f.unlink()
            p.rmdir()
        # --cond meal は fw が detector でなければ止まる
        code, out = self.run_main(
            FakeDetector([logger_meta]), "--cond", "meal", "--duration", "0.2"
        )
        self.assertEqual(code, 1, out)
        self.assertIn(
            "meal は検出器で録ります（fw=logger）。記録を始めません", out
        )
        self.assertEqual(self.sessions(), [])
        # --cond meal で検出器の META がそろっていれば始まる
        code, out = self.run_main(
            FakeDetector(
                [
                    build_frame(record.ID_META, 100, meta_bytes(**DET_META))
                    + det(1000)
                ]
            ),
            "--cond",
            "meal",
            "--duration",
            "0.2",
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(
            json.loads(
                (self.one_session() / "meta.json").read_text(encoding="utf-8")
            )["cond"],
            "meal",
        )

    def test_r23_main_end_to_end_with_and_without_indicator(self):
        stream = [
            det(500) + feat(500),
            build_frame(record.ID_META, 100, meta_bytes(**DET_META)),
            det(1000) + feat(1000),
            det(1250, 2) + feat(1250),
        ]
        dirs = []
        for with_ind in (False, True):
            ind = FakeWriter() if with_ind else None
            code, out = self.run_main(
                FakeDetector(list(stream)), "--duration", "0.2", indicator=ind
            )
            self.assertEqual(code, 0, out)
            new = [p for p in self.sessions() if p not in dirs]
            self.assertEqual(len(new), 1)
            sd = new[0]
            dirs.append(sd)
            got = json.loads((sd / "meta.json").read_text(encoding="utf-8"))
            for k in ("fw", "threshold", "model", "feature_names"):
                self.assertEqual(got[k], DET_META[k])
            self.assertEqual(
                [r[1] for r in read_csv(sd / "detect.csv")[1:]],
                ["1000", "1250"],
            )
            self.assertEqual(
                [r[1] for r in read_csv(sd / "feat.csv")[1:]], ["1000", "1250"]
            )
            self.assertIn("XOR 不一致数: 0", out)
            if with_ind:
                self.assertTrue(wait_until(lambda: len(ind.written) >= 1))
                self.assertTrue(set(bytes(ind.written)) <= {1, 2})
            # 2 つ目のセッションのフォルダ名（秒の刻み）を変える
            time.sleep(1.05)
        a, b = dirs
        self.assertEqual(
            {p.name for p in a.iterdir()}, {p.name for p in b.iterdir()}
        )
        for name in ("detect.csv", "feat.csv", "events.csv"):
            self.assertEqual(read_csv(a / name), read_csv(b / name), name)

    def test_r24_boundary_splits(self):
        meta = build_frame(record.ID_META, 100, meta_bytes(**DET_META))
        before = det(1000) + feat(1000)
        after = det(1250) + feat(1250) + det(1500) + feat(1500)
        whole = before + meta + after
        m0 = len(before)
        m1 = m0 + len(meta)
        splits = {
            "one read": [whole],
            "inside META": [
                whole[: m0 + len(meta) // 2],
                whole[m0 + len(meta) // 2 :],
            ],
            "between META and DETECT(w1)": [whole[:m1], whole[m1:]],
            "inside DETECT(w1)": [whole[: m1 + 12], whole[m1 + 12 :]],
        }
        for name, chunks in splits.items():
            with self.subTest(name):
                for p in self.sessions():
                    for f in p.iterdir():
                        f.unlink()
                    p.rmdir()
                code, out = self.run_main(
                    FakeDetector(chunks), "--duration", "0.5"
                )
                self.assertEqual(code, 0, out)
                sd = self.one_session()
                d_rows = read_csv(sd / "detect.csv")[1:]
                f_rows = read_csv(sd / "feat.csv")[1:]
                self.assertEqual([r[1] for r in d_rows], ["1250", "1500"])
                self.assertEqual([r[1] for r in f_rows], ["1250", "1500"])
                self.assertEqual(d_rows[0][1], f_rows[0][1])
                self.assertIn("XOR 不一致数: 0", out)


if __name__ == "__main__":
    unittest.main()
