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


# --- M4 の後処理（plan #45 の 5.4・12 節） ---


def runs_of(mask) -> list:
    """mask の真の連続の (始まり, 終わり + 1) の並び。"""
    out, k, m = [], 0, list(mask)
    while k < len(m):
        if m[k]:
            j = k
            while j < len(m) and m[j]:
                j += 1
            out.append((k, j))
            k = j
        else:
            k += 1
    return out


class PostMaskTest(unittest.TestCase):
    def rand(self, seed, n=300):
        rng = np.random.default_rng(seed)
        # 塊ができるよう、なめらかな確率にする
        prob = np.clip(
            np.convolve(rng.uniform(size=n + 4), np.ones(5) / 5, "valid"),
            0,
            1,
        )
        valid = rng.uniform(size=n) > 0.05
        return prob, valid, grid(n)

    def test_none_is_bitwise_hysteresis(self):
        for seed in range(3):
            prob, valid, t = self.rand(seed)
            for e, x in pp.HYST_GRID:
                with self.subTest(seed=seed, enter=e, exit=x):
                    np.testing.assert_array_equal(
                        pp.post_mask(prob, valid, t, e, x),
                        pp.hysteresis(prob, valid, e, x, t),
                    )

    def test_equal_enter_exit_is_threshold(self):
        prob, valid, t = self.rand(5)
        for thr in (0.05, 0.3, 0.5, 0.95):
            np.testing.assert_array_equal(
                pp.post_mask(prob, valid, t, thr, thr),
                pp.threshold(prob, valid, thr),
            )

    def test_subset_of_hysteresis_and_invalid_negative(self):
        prob, valid, t = self.rand(7)
        for e, x in ((0.5, 0.3), (0.45, 0.35), (0.6, 0.3)):
            h = pp.hysteresis(prob, valid, e, x, t)
            for cap in (None, *pp.CAP_GRID_S):
                for ref in (None, pp.REFRACTORY_S):
                    m = pp.post_mask(prob, valid, t, e, x, cap, ref)
                    self.assertFalse(np.any(m & ~h))
                    self.assertFalse(np.any(m & ~valid))

    def test_causal_and_stepper_matches(self):
        prob, valid, t = self.rand(11, 200)
        for cap, ref in ((None, None), (2.0, None), (None, 0.5), (1.5, 0.5)):
            full = pp.post_mask(prob, valid, t, 0.5, 0.35, cap, ref)
            st = pp.PostStepper(0.5, 0.35, cap, ref)
            stepped = [st.step(p, v, s) for p, v, s in zip(prob, valid, t)]
            self.assertEqual(full.tolist(), stepped)
            for k in (0, 50, 120, 199):
                p2 = prob.copy()
                v2 = valid.copy()
                rng = np.random.default_rng(k)
                p2[k + 1 :] = rng.uniform(size=len(p2) - k - 1)
                v2[k + 1 :] = rng.uniform(size=len(v2) - k - 1) > 0.5
                m2 = pp.post_mask(p2, v2, t, 0.5, 0.35, cap, ref)
                np.testing.assert_array_equal(m2[: k + 1], full[: k + 1])

    def test_cap(self):
        prob = np.zeros(30)
        prob[2:14] = 0.9  # h の塊 12 窓
        prob[16:20] = 0.9  # 切れてから再び入る
        m = pp.post_mask(prob, [True] * 30, grid(30), 0.7, 0.4, cap_s=2.0)
        self.assertEqual(m[2:10].tolist(), [True] * 8)
        self.assertEqual(m[10:14].tolist(), [False] * 4)
        self.assertEqual(m[16:20].tolist(), [True] * 4)
        self.assertEqual(runs_of(m), [(2, 10), (16, 20)])
        for cap, n in ((1.5, 6), (2.5, 10)):
            m = pp.post_mask(prob, [True] * 30, grid(30), 0.7, 0.4, cap_s=cap)
            self.assertEqual(int(m[2:14].sum()), n)

    def test_refractory(self):
        def mask(prob, valid=None, cap=None):
            n = len(prob)
            v = [True] * n if valid is None else valid
            return pp.post_mask(prob, v, grid(n), 0.7, 0.4, cap, 0.5)

        p = np.zeros(20)
        p[2:5] = 0.9
        p[6:8] = 0.9  # 陰性 1 窓を挟む → 塊ごと抑える
        self.assertEqual(runs_of(mask(p)), [(2, 5)])
        p = np.zeros(20)
        p[2:5] = 0.9
        p[7:9] = 0.9  # 陰性 2 窓を挟む → 残る
        self.assertEqual(runs_of(mask(p)), [(2, 5), (7, 9)])
        p = np.zeros(20)
        p[2:4] = 0.9
        p[5:7] = 0.9
        p[8:10] = 0.9  # 連鎖: 2 つ目と 3 つ目を抑える
        self.assertEqual(runs_of(mask(p)), [(2, 4)])
        p = np.zeros(20)
        p[2:5] = 0.9
        p[5] = 0.9
        p[6:8] = 0.9
        valid = [True] * 20
        valid[5] = False  # valid = False の 1 窓を挟んでも同じ
        self.assertEqual(runs_of(mask(p, valid)), [(2, 5)])
        # 上限で切られた塊（h は 2〜13）の後の判定は最後の h の窓（13）から
        p = np.zeros(30)
        p[2:14] = 0.9
        p[15:17] = 0.9  # 13 と 15 の差 0.5 秒 → 抑える
        p[20:22] = 0.9  # 16 と 20 の差 1.0 秒 → 残る
        self.assertEqual(runs_of(mask(p, cap=2.0)), [(2, 10), (20, 22)])

    def score(self, prob, swallows, **kw):
        t = grid(len(prob))
        m = pp.post_mask(prob, np.ones(len(prob), bool), t, 0.7, 0.4, **kw)
        return evaluate.event_metrics(
            swallows, t[m].tolist(), duration_s=t[-1] + 1.0, t0_s=0.0
        )

    def test_effects_on_event_metrics(self):
        # 不応期: 非嚥下区間の 2 つの塊（陰性 1 窓を挟む）が 1 回になる
        prob = np.full(120, 0.1)
        prob[80:82] = 0.9
        prob[83:85] = 0.9
        self.assertEqual(self.score(prob, [5.0])["false_positive_runs"], 2)
        self.assertEqual(
            self.score(prob, [5.0], refractory_s=0.5)["false_positive_runs"],
            1,
        )
        # 上限: 嚥下（10 秒）の直後の延びが、上限 2.0 秒で区間の中に収まる
        prob = np.full(120, 0.1)
        prob[37:52] = 0.9  # t_start 9.25〜12.75（中心 9.75〜13.25）
        a = self.score(prob, [10.0])
        b = self.score(prob, [10.0], cap_s=2.0)  # 9.25〜11.0（中心〜11.5）
        self.assertEqual((a["detected"], a["false_positive_runs"]), (1, 1))
        self.assertEqual((b["detected"], b["false_positive_runs"]), (1, 0))
        # 開始の差 1.5 秒の 2 回の嚥下は、不応期 0.5 秒でも両方検出される
        prob = np.full(120, 0.1)
        prob[38:42] = 0.9  # 嚥下 10.0 の塊（t_start 9.5〜10.25）
        prob[44:48] = 0.9  # 嚥下 11.5 の塊（t_start 11.0〜11.75）
        m = self.score(prob, [10.0, 11.5], refractory_s=0.5)
        self.assertEqual((m["detected"], m["false_positive_runs"]), (2, 0))

    def test_value_errors(self):
        t = grid(6)
        for kw in (
            dict(cap_s=1.6),
            dict(cap_s=0.0),
            dict(cap_s=-0.5),
            dict(refractory_s=0.25),
            dict(refractory_s=1.0),
        ):
            with self.subTest(kw):
                with self.assertRaises(ValueError):
                    pp.post_mask([0.5] * 6, [True] * 6, t, 0.7, 0.4, **kw)
                with self.assertRaises(ValueError):
                    pp.PostStepper(0.7, 0.4, **kw)
        for enter, exit_ in ((0.4, 0.7), (0.7, 0.0), (1.1, 0.5)):
            with self.assertRaises(ValueError):
                pp.post_mask([0.5] * 6, [True] * 6, t, enter, exit_)
        bad = t.copy()
        bad[3] += 0.1
        with self.assertRaises(ValueError):
            pp.post_mask([0.5] * 6, [True] * 6, bad, 0.7, 0.4, 2.0, 0.5)
        st = pp.PostStepper(0.7, 0.4)
        st.step(0.5, True, 0.0)
        with self.assertRaises(ValueError):
            st.step(0.5, True, 0.5)
        with self.assertRaises(ValueError):
            pp.post_mask([0.5] * 5, [True] * 6, t, 0.7, 0.4)
        self.assertEqual(pp.CAP_GRID_S, (1.5, 2.0, 2.5))
        self.assertEqual(pp.REFRACTORY_S, 0.5)

    def test_ensemble_mean(self):
        a, b, c = (
            np.array([0.1, 0.4]),
            np.array([0.3, 0.4]),
            np.array([0.2, 1.0]),
        )
        np.testing.assert_allclose(pp.ensemble_mean([a, b, c]), [0.2, 0.6])
        with self.assertRaises(ValueError):
            pp.ensemble_mean([])
        with self.assertRaises(ValueError):
            pp.ensemble_mean([a, np.zeros(3)])

    def test_imports_only_numpy_and_evaluate(self):
        import ast
        from pathlib import Path

        src = (Path(__file__).resolve().parent / "postprocess.py").read_text(
            encoding="utf-8"
        )
        names = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        self.assertEqual(names, {"__future__", "numpy", "evaluate"})


if __name__ == "__main__":
    unittest.main()
