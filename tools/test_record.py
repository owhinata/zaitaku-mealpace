"""record.py の DETECT / FEAT の受信とファイルの作り方を合成フレームで検証する（#23、docs/decisions/0021）。

実行: python -m unittest discover -s tools -v
pty は使わない。record.Session と record.read_frames を直接呼ぶ。フレームは firmware/logger/frame.h と同じ規則で
build_frame が作る。出力先は tempfile で、data/raw/ は読み書きしない。
"""
from __future__ import annotations
import contextlib, csv, io, json, struct, tempfile, time, unittest, wave
from pathlib import Path

import record

N_FEAT = 5


def build_frame(sid: int, t_ms: int, payload: bytes) -> bytes:
    """[A5 5A][id u8][len u16 LE][t_ms u32 LE][payload][xor u8]。xor は id〜payload の XOR（frame.h と同じ）。"""
    body = bytes([sid]) + struct.pack("<HI", len(payload), t_ms) + payload
    x = 0
    for b in body:
        x ^= b
    return record.SYNC + body + bytes([x])


def detect_payload(window_t_ms: int, prob: float, positive: int, led: int) -> bytes:
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
        positives = [0, 1, 1]           # 装置は float32 で比べるので f32(0.9) >= 0.9f は真
        for i, (p, pos) in enumerate(zip(probs, positives)):
            self.sess.on_frame(record.ID_DETECT, 2030 + 250 * i, detect_payload(1000 + 250 * i, p, pos, 1 + pos))
        self.sess.close()
        rows = read_csv(self.sess.dir / "detect.csv")
        self.assertEqual(rows[0], ["t_ms", "window_t_ms", "positive", "prob", "led"])
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[1], ["2030", "1000", "0", f"{f32(0.1):.9g}", "1"])
        self.assertEqual(rows[3][:3], ["2530", "1500", "1"])
        for row, p in zip(rows[1:], probs):
            self.assertEqual(f32(float(row[3])), f32(p))       # .9g は float32 を往復する
        self.assertEqual(self.files(), {"detect.csv", "events.csv", "meta.json"})

    def test_r2_feat_frame_writes_feat_csv(self):
        values = [0.0123, -3.5, 4321.0, 1e-3, 0.333333]
        self.sess.on_frame(record.ID_FEAT, 2032, feat_payload(1000, values))
        self.sess.close()
        rows = read_csv(self.sess.dir / "feat.csv")
        self.assertEqual(rows[0], ["t_ms", "window_t_ms", "f0", "f1", "f2", "f3", "f4"])
        self.assertEqual(rows[1][:2], ["2032", "1000"])
        for s, v in zip(rows[1][2:], values):
            self.assertEqual(f32(float(s)), f32(v))
        self.assertEqual(self.files(), {"feat.csv", "events.csv", "meta.json"})

    def test_r3_feat_with_different_n_is_counted_not_written(self):
        self.sess.on_frame(record.ID_FEAT, 2032, feat_payload(1000, [1.0] * N_FEAT))
        self.sess.on_frame(record.ID_FEAT, 2282, feat_payload(1250, [1.0] * (N_FEAT + 1)))
        self.sess.on_frame(record.ID_FEAT, 2532, feat_payload(1500, [2.0] * N_FEAT))
        self.sess.close()
        rows = read_csv(self.sess.dir / "feat.csv")
        self.assertEqual([r[1] for r in rows[1:]], ["1000", "1500"])
        self.assertEqual(self.sess.bad_len, {record.ID_FEAT: 1})

    def test_r4_marker_uses_header_t_ms_not_window_t_ms(self):
        self.sess.on_frame(record.ID_DETECT, 2030, detect_payload(1000, 0.5, 0, 1))
        self.sess.mark("s")
        self.sess.on_frame(record.ID_FEAT, 2032, feat_payload(1000, [0.0] * N_FEAT))
        self.sess.mark("c")
        self.sess.close()
        rows = read_csv(self.sess.dir / "events.csv")
        self.assertEqual(rows[1:], [["2030", "s", ""], ["2032", "c", ""]])

    def test_r5_meta_merge_and_feature_names_warning(self):
        meta = {"fw": "detector", "threshold": 0.9, "feature_names": [f"x{i}" for i in range(N_FEAT)]}
        self.sess.on_frame(record.ID_META, 100, json.dumps(meta).encode("utf-8"))
        self.sess.on_frame(record.ID_FEAT, 2032, feat_payload(1000, [0.0] * N_FEAT))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.sess.close()
        got = json.loads((self.sess.dir / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(got["fw"], "detector")
        self.assertEqual(got["feature_names"], meta["feature_names"])
        self.assertEqual(got["subject"], "self")
        self.assertIn("sample_rates", got)            # PC 側のキーは残る（docs/decisions/0006）
        self.assertNotIn("警告", out.getvalue())

    def test_r5b_feature_names_length_mismatch_warns(self):
        meta = {"fw": "detector", "feature_names": ["a", "b"]}
        self.sess.on_frame(record.ID_META, 100, json.dumps(meta).encode("utf-8"))
        self.sess.on_frame(record.ID_FEAT, 2032, feat_payload(1000, [0.0] * N_FEAT))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.sess.close()
        self.assertIn("警告", out.getvalue())
        got = json.loads((self.sess.dir / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(got["feature_names"], ["a", "b"])    # 装置の値のまま。直さない

    def test_r6_no_frames_leaves_events_and_meta_only(self):
        self.sess.mark("s")
        self.sess.close()
        self.assertEqual(self.files(), {"events.csv", "meta.json"})
        self.assertEqual(read_csv(self.sess.dir / "events.csv"), [["t_ms", "label", "note"], ["0", "s", ""]])

    def test_r7_imu_and_audio_files_keep_their_format(self):
        self.sess.on_frame(record.ID_IMU, 1000, struct.pack("<6f", 0.01, -0.02, 1.0, 1.5, -2.25, 3.125))
        self.sess.on_frame(record.ID_AUDIO, 1004, struct.pack("<64h", *range(64)))
        self.sess.on_frame(record.ID_AUDIO, 1008, struct.pack("<64h", *range(64)))
        self.sess.close()
        self.assertEqual(self.files(), {"imu.csv", "audio.wav", "audio_chunks.csv", "events.csv", "meta.json"})
        imu = read_csv(self.sess.dir / "imu.csv")
        self.assertEqual(imu[0], ["t_ms", "ax", "ay", "az", "gx", "gy", "gz"])
        self.assertEqual(imu[1], ["1000", "0.0100", "-0.0200", "1.0000", "1.5000", "-2.2500", "3.1250"])
        chunks = read_csv(self.sess.dir / "audio_chunks.csv")
        self.assertEqual(chunks, [["t_ms", "sample_index"], ["1004", "0"], ["1008", "64"]])
        with wave.open(str(self.sess.dir / "audio.wav"), "rb") as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()), (1, 2, 16000, 128))

    def test_r8_read_frames_counts_streams_and_xor_errors(self):
        data = build_frame(record.ID_DETECT, 2030, detect_payload(1000, 0.5, 0, 1))
        data += build_frame(record.ID_FEAT, 2032, feat_payload(1000, [1.0] * N_FEAT))
        data += build_frame(0x06, 2040, b"\x01\x02\x03")                       # 未知の ID
        bad = bytearray(build_frame(record.ID_DETECT, 2280, detect_payload(1250, 0.5, 0, 1)))
        bad[-1] ^= 0xFF                                                       # XOR 不一致
        data += bytes(bad)
        data += build_frame(record.ID_DETECT, 2530, detect_payload(1500, 0.95, 1, 2))
        stats = {"ok": {}, "xor_err": 0}
        record.read_frames(FakeSerial(data), self.sess.on_frame, stats, deadline=time.monotonic() + 0.05)
        self.sess.close()
        self.assertEqual(stats["ok"], {record.ID_DETECT: 2, record.ID_FEAT: 1, 0x06: 1})
        self.assertEqual(stats["xor_err"], 1)
        rows = read_csv(self.sess.dir / "detect.csv")
        self.assertEqual([r[1] for r in rows[1:]], ["1000", "1500"])
        self.assertEqual(self.files(), {"detect.csv", "feat.csv", "events.csv", "meta.json"})

    def test_r9_wrong_length_frames_are_counted_not_written(self):
        self.sess.on_frame(record.ID_DETECT, 2030, detect_payload(1000, 0.5, 0, 1) + b"\x00")
        self.sess.on_frame(record.ID_DETECT, 2280, b"\x00" * 9)
        self.sess.on_frame(record.ID_IMU, 1000, b"\x00" * 20)
        self.sess.on_frame(record.ID_FEAT, 2032, b"\x00" * 7)
        self.sess.on_frame(record.ID_FEAT, 2032, b"\x00" * 9)
        self.sess.close()
        self.assertEqual(self.sess.bad_len, {record.ID_DETECT: 2, record.ID_IMU: 1, record.ID_FEAT: 2})
        self.assertEqual(self.files(), {"events.csv", "meta.json"})


if __name__ == "__main__":
    unittest.main()
