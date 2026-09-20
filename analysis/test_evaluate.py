"""evaluate.py を合成データで検証する（解釈は docs/decisions/0011）。

実行: python -m unittest discover -s analysis -v
合成データは tempfile に作る。data/ と実データは使わない。
"""
from __future__ import annotations
import tempfile, unittest
from pathlib import Path

import evaluate

DURATION_S = 60.0


# 関数が無い版の evaluate.py でも、他のケースが個別に結果を出せるよう、呼ぶ時点で引く
def event_metrics(*args, **kwargs):
    return evaluate.event_metrics(*args, **kwargs)


def load_events(*args, **kwargs):
    return evaluate.load_events(*args, **kwargs)


def session_span(*args, **kwargs):
    return evaluate.session_span(*args, **kwargs)


def aggregate(*args, **kwargs):
    return evaluate.aggregate(*args, **kwargs)


def grid(start: float, stop: float, step: float = 0.25) -> list[float]:
    """start〜stop（両端を含む）を step 刻みで返す。"""
    n = int(round((stop - start) / step))
    return [start + i * step for i in range(n + 1)]


class EventMetricsTest(unittest.TestCase):
    def check(self, m, detected=None, fp=None, non_swallow_s=None, fp_per_min=None):
        if detected is not None:
            self.assertEqual(m["detected"], detected)
        if fp is not None:
            self.assertEqual(m["false_positive_runs"], fp)
        if fp_per_min is not None:
            self.assertAlmostEqual(m["false_positives_per_min"], fp_per_min, places=4)
        if non_swallow_s is not None:
            self.assertAlmostEqual(m["non_swallow_min"] * 60.0, non_swallow_s, places=4)

    def test_ev1_window_at_marker(self):
        m = event_metrics([10.0], [10.0], DURATION_S)
        self.check(m, detected=1, fp=0, non_swallow_s=58.0)
        self.assertEqual(m["swallows"], 1)
        self.assertEqual(m["detection_rate"], 1.0)

    def test_ev2_center_on_upper_edge(self):
        self.check(event_metrics([10.0], [11.0], DURATION_S), detected=1, fp=0)

    def test_ev3_center_past_upper_edge(self):
        self.check(event_metrics([10.0], [11.25], DURATION_S), detected=0, fp=1)

    def test_ev4_center_on_lower_edge(self):
        # 同じ陽性窓を検出と誤検出の両方に数えない
        self.check(event_metrics([10.0], [9.0], DURATION_S), detected=1, fp=0)

    def test_ev5_center_before_lower_edge(self):
        self.check(event_metrics([10.0], [8.75], DURATION_S), detected=0, fp=1)

    def test_ev6_consecutive_windows_count_once(self):
        m = event_metrics([], [5.0, 5.25, 5.5], DURATION_S)
        self.check(m, fp=1, fp_per_min=1.0)
        self.assertIsNone(m["detection_rate"])

    def test_ev7_gap_of_one_window_splits_runs(self):
        self.check(event_metrics([], [5.0, 5.5], DURATION_S), fp=2, fp_per_min=2.0)

    def test_ev8_duplicates_and_unsorted(self):
        self.check(event_metrics([], [5.0, 5.0, 5.25], DURATION_S), fp=1)
        self.check(event_metrics([], [20.0, 5.0], DURATION_S), fp=2)

    def test_ev9_run_across_swallow_is_one_run(self):
        windows = grid(8.0, 12.0)
        self.assertEqual(len(windows), 17)
        self.check(event_metrics([10.0], windows, DURATION_S), detected=1, fp=1)

    def test_ev9b_run_inside_swallow(self):
        self.check(event_metrics([10.0], [9.5, 9.75, 10.0], DURATION_S), detected=1, fp=0)

    def test_ev9c_run_partly_outside_swallow(self):
        self.check(event_metrics([10.0], [8.5, 8.75, 9.0], DURATION_S), detected=1, fp=1)

    def test_ev10_overlapping_swallows_use_union(self):
        m = event_metrics([10.0, 11.0], [30.0], DURATION_S)
        self.check(m, fp=1, non_swallow_s=57.0, fp_per_min=1.0526)

    def test_ev11_clip_at_record_edges(self):
        m = event_metrics([0.25, 59.5], [30.0], DURATION_S)
        self.check(m, fp=1, non_swallow_s=57.25, fp_per_min=1.0480)

    def test_ev12_millis_origin(self):
        m = event_metrics([1000.25], [1030.0], DURATION_S, t0_s=1000.0)
        self.check(m, detected=0, fp=1, non_swallow_s=58.25, fp_per_min=1.0300)

    def test_ev13_mixed(self):
        m = event_metrics([10.0, 20.0, 30.0, 40.0], [10.0, 30.5, 50.0, 50.25], DURATION_S)
        self.check(m, detected=2, fp=1, non_swallow_s=52.0, fp_per_min=1.1538)
        self.assertEqual(m["swallows"], 4)
        self.assertEqual(m["detection_rate"], 0.5)

    def test_ev14_swallow_covers_whole_record(self):
        m = event_metrics([0.5], [], 2.0)
        self.check(m, fp=0, non_swallow_s=0.0)
        self.assertIsNone(m["false_positives_per_min"])

    def test_ev14b_clipped_swallow_leaves_half_second(self):
        m = event_metrics([1.0], [], 2.0)
        self.check(m, fp=0, non_swallow_s=0.5)
        self.assertEqual(m["false_positives_per_min"], 0.0)

    def test_ev15_same_marker_twice(self):
        self.check(event_metrics([10.0, 10.0], [10.0], DURATION_S), detected=2, non_swallow_s=58.0)

    def test_ev16_always_positive(self):
        # 常時陽性の挙動を固定する（弱点の記録は docs/decisions/0011）
        m = event_metrics([30.0], grid(0.0, 59.0), DURATION_S)
        self.check(m, detected=1, fp=1, fp_per_min=1.0345)

    def test_ev17_marker_outside_record(self):
        with self.assertRaises(ValueError):
            event_metrics([70.0], [], DURATION_S)
        with self.assertRaises(ValueError):
            event_metrics([-0.25], [], DURATION_S)

    def test_ev17b_marker_on_record_edges(self):
        self.check(event_metrics([0.0], [], DURATION_S), non_swallow_s=58.5)
        self.check(event_metrics([60.0], [], DURATION_S), non_swallow_s=59.5)


class SessionFilesTest(unittest.TestCase):
    def test_ev18_load_events(self):
        with tempfile.TemporaryDirectory() as d:
            session = Path(d)
            (session / "events.csv").write_text(
                "t_ms,label,note\n1500,s,\n2000,t,\n4250,s,\n5000,c,\n", encoding="utf-8")
            self.assertEqual(load_events(session), [1.5, 4.25])

    def test_ev19_session_span(self):
        with tempfile.TemporaryDirectory() as d:
            session = Path(d)
            imu = ["t_ms,ax,ay,az,gx,gy,gz"]
            imu += [f"{t},0.0,0.0,1.0,0.0,0.0,0.0" for t in range(1000, 60001, 1000)]
            (session / "imu.csv").write_text("\n".join(imu) + "\n", encoding="utf-8")
            chunks = ["t_ms,sample_index"]
            chunks += [f"{t},{i * 8000}" for i, t in enumerate(range(500, 61001, 500))]
            (session / "audio_chunks.csv").write_text("\n".join(chunks) + "\n", encoding="utf-8")
            t0_s, duration_s = session_span(session)
            self.assertAlmostEqual(t0_s, 0.5, places=4)
            self.assertAlmostEqual(duration_s, 60.5, places=4)

    def write_session(self, session: Path, imu_t: list[int], chunk_t: list[int]) -> None:
        imu = ["t_ms,ax,ay,az,gx,gy,gz"] + [f"{t},0.0,0.0,1.0,0.0,0.0,0.0" for t in imu_t]
        (session / "imu.csv").write_text("\n".join(imu) + "\n", encoding="utf-8")
        chunks = ["t_ms,sample_index"] + [f"{t},{i * 64}" for i, t in enumerate(chunk_t)]
        (session / "audio_chunks.csv").write_text("\n".join(chunks) + "\n", encoding="utf-8")

    def test_ev20_outlier_t_ms_stops(self):
        """外れた t_ms が1行あると記録が長く見える。水増しさせずに止める。"""
        good = list(range(1000, 61000, 10))
        cases = {
            "IMU の末尾に外れ値": (good + [3_600_000], good),
            "音声チャンクの途中に外れ値": (good, good[:100] + [3_600_000] + good[100:]),
            "IMU の逆行": (good[:50] + [good[10]] + good[50:], good),
        }
        for label, (imu_t, chunk_t) in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as d:
                self.write_session(Path(d), imu_t, chunk_t)
                with self.assertRaises(ValueError):
                    session_span(Path(d))

    def test_ev20b_gap_of_one_second_is_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            self.write_session(Path(d), [1000, 2000, 3000], [1000, 2000, 3000])
            self.assertEqual(session_span(Path(d)), (1.0, 2.0))


class AggregateTest(unittest.TestCase):
    def ev13(self):
        return event_metrics([10.0, 20.0, 30.0, 40.0], [10.0, 30.5, 50.0, 50.25], DURATION_S)

    def test_ag1_three_sessions(self):
        a = aggregate({f"s{i}": self.ev13() for i in range(3)})
        self.assertEqual(a["swallows"], 12)
        self.assertEqual(a["detected"], 6)
        self.assertEqual(a["detection_rate"], 0.5)
        self.assertEqual(a["false_positive_runs"], 3)
        self.assertAlmostEqual(a["false_positives_per_min"], 1.1538, places=4)
        self.assertEqual(a["confusion"], {"tp": 6, "fn": 6, "fp": 3, "tn": None})
        self.assertEqual(len(a["sessions"]), 3)
        self.assertEqual(sorted(a["sessions"]), ["s0", "s1", "s2"])

    def test_ag2_two_sessions(self):
        with self.assertRaises(ValueError):
            aggregate({f"s{i}": self.ev13() for i in range(2)})

    def test_ag3_sum_of_counts(self):
        a = aggregate({
            "a": self.ev13(),
            "b": event_metrics([], [5.0, 5.25, 5.5], DURATION_S),
            "c": event_metrics([10.0], [10.0], DURATION_S),
        })
        self.assertEqual(a["swallows"], 5)
        self.assertEqual(a["detected"], 3)
        self.assertAlmostEqual(a["detection_rate"], 0.6, places=4)
        self.assertEqual(a["false_positive_runs"], 2)
        self.assertAlmostEqual(a["non_swallow_min"] * 60.0, 170.0, places=4)
        self.assertAlmostEqual(a["false_positives_per_min"], 0.7059, places=4)
        self.assertEqual(a["confusion"], {"tp": 3, "fn": 2, "fp": 2, "tn": None})


if __name__ == "__main__":
    unittest.main()
