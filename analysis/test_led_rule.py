"""led_rule.py を検証する（#25。docs/decisions/0018）。

入力の表は firmware/detector/host/test_detector.cpp の test_led_rule と同じ（値を両方に書いてある。片方から生成しない）。
実行: python -m unittest discover -s analysis -v
"""
from __future__ import annotations
import unittest

import led_rule


CASES = {
    # 1. 起動直後: 陽性なし → 黄
    "1_start": ([1000, 1250, 1500], [0, 0, 0], [1, 1, 1]),
    # 2. 1 窓の陽性: 2000〜3750 の 8 行が緑、4000 が黄（結果 8 回分 = 2.0 秒）
    "2_single": ([2000, 2250, 2500, 2750, 3000, 3250, 3500, 3750, 4000],
                 [1, 0, 0, 0, 0, 0, 0, 0, 0],
                 [2, 2, 2, 2, 2, 2, 2, 2, 1]),
    # 3. 2 窓連続: 2000〜4000 の 9 行が緑（(k − 1) × 0.25 + 2.0 秒）、4250 が黄
    "3_two_in_a_row": ([2000, 2250, 2500, 2750, 3000, 3250, 3500, 3750, 4000, 4250],
                       [1, 1, 0, 0, 0, 0, 0, 0, 0, 0],
                       [2, 2, 2, 2, 2, 2, 2, 2, 2, 1]),
    # 4. 陽性が離れて 2 回（2000 と 3000）: 2000〜4750 が緑、5000 が黄
    "4_two_apart": ([2000, 2250, 2500, 2750, 3000, 3250, 3500, 3750, 4000, 4250, 4500, 4750, 5000],
                    [1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0],
                    [2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1]),
    # 5. 窓の飛び: 2000 陽性、次が 3750（差 1750 ちょうど）→ 緑、次が 3800（差 1800）→ 黄
    "5_gap": ([2000, 3750, 3800], [1, 0, 0], [2, 2, 1]),
    # 6. 陽性が続く 18 窓（2000〜6250）: 緑は最後の陽性（6250）から 8 行（〜8000）、8250 が黄
    "6_run18": ([2000, 2250, 2500, 2750, 3000, 3250, 3500, 3750, 4000, 4250, 4500, 4750, 5000,
                 5250, 5500, 5750, 6000, 6250, 6500, 6750, 7000, 7250, 7500, 7750, 8000, 8250],
                [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0],
                [2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1]),
}


class LedRuleTest(unittest.TestCase):
    def test_l1_table(self):
        for label, (w, pos, want) in CASES.items():
            with self.subTest(label):
                self.assertEqual(led_rule.led_states(w, pos), want)

    def test_l2_constants(self):
        self.assertEqual((led_rule.LED_OFF, led_rule.LED_YELLOW, led_rule.LED_GREEN), (0, 1, 2))
        self.assertEqual(led_rule.GREEN_HOLD_MS, 1750)

    def test_l3_never_off_and_empty(self):
        self.assertEqual(led_rule.led_states([], []), [])
        self.assertNotIn(led_rule.LED_OFF, led_rule.led_states(list(range(0, 10000, 250)), [i % 7 == 0 for i in range(40)]))

    def test_l4_not_ascending_raises(self):
        for w in ([1000, 1000], [1250, 1000], [1000, 1250, 1250]):
            with self.subTest(w=w), self.assertRaises(ValueError):
                led_rule.led_states(w, [0] * len(w))

    def test_l5_length_mismatch_raises(self):
        with self.assertRaises(ValueError):
            led_rule.led_states([1000, 1250], [0])


if __name__ == "__main__":
    unittest.main()
