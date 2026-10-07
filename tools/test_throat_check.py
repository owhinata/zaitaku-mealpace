"""tools/throat_check.py を合成のセッションで検証する（Issue #36 plan 第 4.4 節・
第 10.3 節 k1〜k7）。

実行: python -m unittest discover -s tools -v
合成: 48 kHz・2 ch、白色雑音（RMS 6 LSB）、叩き = 減衰する 2 ms のパルス（振幅
2000 LSB）。到着 = 6000 フレームごと、各ピリオドの終端の取り込みの時刻 + 遅れ
（最小 2 ms + 指数分布 平均 8 ms、1% は 100〜300 ms の外れ）。クロックの差は
PC の時計に対するサンプルの時刻の伸び s（τ(i) = 1000 i/fs × (1 + s) + c）。
書くのは tempfile の下だけで、data/raw/ は読み書きしない。

20 ms は合成データの条件での要求（テストの合否の基準）で、実機の時刻の誤差を示す
ものではない。
"""

from __future__ import annotations
import csv, json, tempfile, unittest, wave
from pathlib import Path

import numpy as np

import throat_check as tc

FS = 48000
PERIOD = 6000
KEY_DELAY_MS = 3.0


def make_session(
    d: Path,
    seconds: float = 180.0,
    s_true: float = -8e-6,
    tap_times_s=(1.0, 4.0, 7.0, 10.0, 13.0),
    o_delay_ms=KEY_DELAY_MS,
    drop_at_s: float | None = None,
    drop_s: float = 0.25,
    r_zero: bool = False,
    extra_events=(),
    seed: int = 0,
    bursts=(),
) -> dict:
    """合成のセッションを d に書く。返り値は本当の時刻の関数など。

    時刻はすべて「取り込みの時刻」の PC の時計（ms。t0 を引く前）。
    bursts: (時刻 s, 長さ s, 振幅 LSB) の帯域の雑音の塊（集計の確かめ用）。
    o_delay_ms はスカラーか、叩きごとの列。
    """
    rng = np.random.default_rng(seed)
    n_true = int(seconds * FS)
    x = rng.normal(0.0, 6.0, n_true)
    tap_len = int(0.002 * FS)
    pulse = 2000.0 * np.exp(-np.arange(tap_len) / (0.0005 * FS))
    for t in tap_times_s:
        i = int(t * FS)
        x[i : i + tap_len] += pulse
    for t, length, amp in bursts:
        i, n = int(t * FS), int(length * FS)
        win = np.hanning(n)
        x[i : i + n] += amp * win * rng.normal(0.0, 1.0, n)
    x = np.clip(np.round(x), -32768, 32767).astype("<i2")
    true_idx = np.arange(n_true)
    if drop_at_s is not None:
        a, b = int(drop_at_s * FS), int((drop_at_s + drop_s) * FS)
        keep = np.ones(n_true, bool)
        keep[a:b] = False
        x = x[keep]
        true_idx = true_idx[keep]
    n = len(x)
    c_true = 1234.5  # サンプル 0 の取り込みの時刻（任意）

    def cap(ti):  # 本当のサンプル番号 → 取り込みの PC の時刻 ms
        return c_true + 1000.0 * np.asarray(ti, float) / FS * (1.0 + s_true)

    stereo = np.stack([x, np.zeros_like(x) if r_zero else x], axis=1)
    with wave.open(str(d / "throat.wav"), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(FS)
        w.writeframes(stereo.tobytes())
    starts = np.arange(0, n, PERIOD)
    ends = np.minimum(starts + PERIOD, n) - 1
    delay = 2.0 + rng.exponential(8.0, len(starts))
    out = rng.random(len(starts)) < 0.01
    delay[out] = rng.uniform(100.0, 300.0, out.sum())
    arr = cap(true_idx[ends]) + 1000.0 / FS + delay
    arr = np.maximum.accumulate(arr)
    t0 = arr[0]
    with open(d / "throat_chunks.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["t_ms", "sample_index"])
        for t, s0 in zip(arr, starts):
            w.writerow([int(t - t0), int(s0)])
    od = np.broadcast_to(np.asarray(o_delay_ms, float), (len(tap_times_s),))
    ev = [
        (int(cap(t * FS) + od[k] - t0), "o", "tap")
        for k, t in enumerate(tap_times_s)
    ]
    ev += [(int(cap(t * FS) - t0), lab, note) for t, lab, note in extra_events]
    ev.sort()
    with open(d / "events.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["t_ms", "label", "note"])
        w.writerows(ev)
    meta = {
        "subject": "self",
        "cond": "water",
        "fw": "pc-throat",
        "sample_rates": {"imu_hz": 0, "audio_hz": FS, "analog_hz": 0},
        "sensors": [
            {
                "id": "throat",
                "part": "SH-12JK",
                "iface": "sh12jk-wired-unoq-usbaudio",
            }
        ],
        "throat": {
            "overrun_lines": 0,
            "est_diff_frames": 0,
            "stop": "duration",
        },
    }
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return {
        "t0": t0,
        "true_time": lambda i: cap(true_idx[np.asarray(i)]) - t0,
        "n": n,
    }


class ThroatCheckTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_k1_map_error_within_20ms(self):
        for s_true in (-8e-6, 50e-6):
            with self.subTest(s_true=s_true):
                truth = make_session(self.dir, s_true=s_true, seed=1)
                ses = tc.load_session(self.dir)
                c0, s = tc.sample_time_map(ses)
                i = np.linspace(0, truth["n"] - 1, 2000).astype(int)
                err = np.abs(
                    tc.sample_to_time(i, c0, s, FS) - truth["true_time"](i)
                )
                self.assertLessEqual(
                    err.max(), 20.0, f"最大 {err.max():.2f} ms"
                )
                print(
                    f"\n  k1 合成 s={s_true * 1e6:+.0f} ppm: 対応の誤差 最大 {err.max():.2f} ms、"
                    f"推定 {s * 1e6:+.1f} ppm"
                )
                r = tc.check(ses)
                self.assertEqual(r["steps"], [])
                self.assertEqual(r["flags"], [])

    def test_k2_taps_found_and_delta(self):
        make_session(self.dir, seed=2)
        r = tc.check(tc.load_session(self.dir))
        self.assertEqual(r["taps_found"], 5)
        self.assertEqual(r["tap_flags"], [])
        for d in r["tap_deltas_ms"]:
            # plan 第 10.3 節 k2 の書き方（|Δ − 3 ms| ≤ 20 ms）と、
            # Δ = 立ち上がり − マーカーの符号どおりの本当の値 −3 ms からの差の両方
            self.assertLessEqual(
                abs(d - KEY_DELAY_MS), 20.0, r["tap_deltas_ms"]
            )
            self.assertLessEqual(
                abs(d + KEY_DELAY_MS), 20.0, r["tap_deltas_ms"]
            )
        print(
            f"\n  k2 合成: 叩きの Δ {['%+.1f' % d for d in r['tap_deltas_ms']]} ms"
        )

    def test_k3_overrun_step_and_est_diff(self):
        make_session(self.dir, drop_at_s=30.0, drop_s=0.25, seed=3)
        r = tc.check(tc.load_session(self.dir))
        self.assertEqual(len(r["steps"]), 1, r["steps"])
        self.assertAlmostEqual(r["est_diff_ms"], 250.0, delta=20.0)
        self.assertTrue(any("段差" in f for f in r["flags"]))
        self.assertTrue(any("推定差" in f for f in r["flags"]))
        self.assertIn("使わない目安に当たる", tc.report(r))
        print(
            f"\n  k3 合成: 推定差 {r['est_diff_ms']:+.1f} ms、段差 {r['steps']}"
        )

    def test_k4_no_taps(self):
        make_session(self.dir, tap_times_s=(), seed=4)
        r = tc.check(tc.load_session(self.dir))
        self.assertEqual((r["taps_total"], r["taps_found"]), (0, 0))
        self.assertIsNotNone(r["c0_ms"])
        self.assertIn("0 / 0 回", tc.report(r))

    def test_k5_short_record(self):
        make_session(self.dir, seconds=15.0, tap_times_s=(2.0,), seed=5)
        ses = tc.load_session(self.dir)
        c0, s = tc.sample_time_map(ses)
        self.assertEqual(s, 0.0)
        self.assertEqual(c0, float(tc.anchors(ses)[1].min()))

    def test_k6_r_zero(self):
        make_session(self.dir, seconds=30.0, r_zero=True, seed=6)
        r = tc.check(tc.load_session(self.dir))
        self.assertIsNone(r["lr_corr"])
        self.assertEqual(r["lr_rms_ratio"], 0.0)
        tc.report(r)

    def test_k7_finger_mode(self):
        taps = (5.0, 15.0, 25.0, 35.0, 45.0)
        make_session(
            self.dir,
            seconds=60.0,
            tap_times_s=taps,
            o_delay_ms=(2000.0, 3000.0, 4000.0, 2500.0, 3500.0),
            seed=7,
        )
        r = tc.check(tc.load_session(self.dir), tap_mode="finger")
        self.assertEqual(r["taps_found"], 5)
        self.assertEqual(r["tap_deltas_ms"], [])
        self.assertEqual(r["tap_flags"], [])
        text = tc.report(r)
        self.assertIn("参考の件数 5 / 5", text)
        self.assertIn("1 対 1 の対応は確かめていない", text)
        self.assertNotIn("Δ 中央値", text)
        # enter のまま読むと見つからない（窓 ±300 ms の外）
        self.assertEqual(tc.check(tc.load_session(self.dir))["taps_found"], 0)


if __name__ == "__main__":
    unittest.main()
