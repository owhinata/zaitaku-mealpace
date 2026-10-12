"""postprocess.py を合成の確率で検証する（plan #43 T6）。

実行: python -m unittest discover -s analysis -v
"""

from __future__ import annotations
import unittest

import numpy as np

import evaluate, postprocess as pp

T = 0.25 * np.arange(6)


def grid(n: int) -> np.ndarray:
    return 0.25 * np.arange(n)


class HysteresisTest(unittest.TestCase):
    def test_t6_enter_and_exit(self):
        prob = [0.1, 0.8, 0.5, 0.5, 0.3, 0.9]
        out = pp.hysteresis(prob, [True] * 6, 0.7, 0.4, T)
        self.assertEqual(out.tolist(), [False, True, True, True, False, True])

    def test_t6_equal_enter_exit_is_threshold(self):
        rng = np.random.default_rng(0)
        prob = rng.uniform(size=200)
        valid = rng.uniform(size=200) > 0.1
        for thr in (0.05, 0.3, 0.5, 0.95):
            np.testing.assert_array_equal(
                pp.hysteresis(prob, valid, thr, thr, grid(200)),
                pp.threshold(prob, valid, thr),
            )

    def test_t6_invalid_window_exits(self):
        prob = [0.9, 0.6, 0.6, 0.6, 0.8]
        valid = [True, True, False, True, True]
        out = pp.hysteresis(prob, valid, 0.7, 0.4, T[:5])
        # 無効な窓で出て、enter 以上まで入らない
        self.assertEqual(out.tolist(), [True, True, False, False, True])

    def test_t6_value_errors(self):
        for enter, exit_ in ((0.4, 0.7), (0.7, 0.0), (1.1, 0.5), (0.5, -0.1)):
            with self.subTest((enter, exit_)):
                with self.assertRaises(ValueError):
                    pp.hysteresis([0.5] * 6, [True] * 6, enter, exit_, T)
        bad = T.copy()
        bad[3] += 0.1
        with self.assertRaises(ValueError):
            pp.hysteresis([0.5] * 6, [True] * 6, 0.7, 0.4, bad)

    def test_t6_grid(self):
        self.assertEqual(len(pp.HYST_GRID), 45)
        self.assertIn((0.7, 0.4), pp.HYST_GRID)
        self.assertIn(pp.HYST_DEFAULT, pp.HYST_GRID)
        for e, x in pp.HYST_GRID:
            self.assertGreaterEqual(x, 0.05)
            self.assertIn(round(e - x, 2), (0.1, 0.2, 0.3))

    def score(self, prob, swallows, mask_fn):
        t = grid(len(prob))
        m = mask_fn(np.asarray(prob), np.ones(len(prob), bool), t)
        return evaluate.event_metrics(
            swallows, t[m].tolist(), duration_s=t[-1] + 1.0, t0_s=0.0
        )

    def thr(self, p, v, t):
        return pp.threshold(p, v, 0.7)

    def hyst(self, p, v, t):
        return pp.hysteresis(p, v, 0.7, 0.4, t)

    def test_t6_false_positives_can_decrease(self):
        # 非嚥下区間で途切れた 2 つの塊が 1 つになる
        prob = np.full(120, 0.1)
        prob[80:82] = 0.9  # t_start 20.0, 20.25
        prob[82] = 0.5
        prob[83:85] = 0.9
        a = self.score(prob, [5.0], self.thr)
        b = self.score(prob, [5.0], self.hyst)
        self.assertEqual(a["false_positive_runs"], 2)
        self.assertEqual(b["false_positive_runs"], 1)

    def test_t6_false_positives_can_increase(self):
        # 嚥下（10 秒）の区間の中だけだった塊が、exit で非嚥下区間まで延びる
        prob = np.full(120, 0.1)
        prob[38:40] = 0.9  # t_start 9.5, 9.75（中心 10.0, 10.25）
        prob[40:48] = 0.5  # t_start 10.0〜11.75（中心 10.5〜12.25）
        a = self.score(prob, [10.0], self.thr)
        b = self.score(prob, [10.0], self.hyst)
        self.assertEqual((a["detected"], a["false_positive_runs"]), (1, 0))
        self.assertEqual((b["detected"], b["false_positive_runs"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
