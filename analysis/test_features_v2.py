"""features_v2.py と features._throat_windows を合成データで検証する（plan #43 T1〜T5）。

実行: python -m unittest discover -s analysis -v
合成のセッションは tempfile に作る（test_features.make_throat_session）。data/ は
使わない。
"""

from __future__ import annotations
import dataclasses, tempfile, unittest
from pathlib import Path

import numpy as np

import features, features_v2 as v2
from test_features import HZ, make_throat_session, sine

TOL = dict(rtol=1e-6, atol=1e-6)  # D6


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)


class ThroatWindowsTest(TmpCase):
    def test_t1_extract_throat_session_uses_throat_windows(self):
        # 切り出しの前後のバイト一致は HEAD の値と 1 回比べた（docs/log）。ここでは
        # extract_throat_session が _throat_windows の格子と valid をそのまま返すこと
        info = make_throat_session(self.root, noise_lsb=30.0)
        x, sample_t, t_start, idx, valid, t0, t1 = features._throat_windows(
            info["dir"]
        )
        f = features.extract_throat_session(info["dir"])
        np.testing.assert_array_equal(f.t_start_s, t_start)
        np.testing.assert_array_equal(f.valid, valid)
        self.assertEqual(len(sample_t), len(x) + 1)
        self.assertEqual(t_start[0], t0)
        self.assertLessEqual(t_start[-1] + 1.0, t1)
        for k in (0, len(idx) // 2, len(idx) - 1):
            ref = features._audio_features(x[idx[k] : idx[k] + HZ])
            np.testing.assert_array_equal(f.X[k], ref.astype(np.float32))


class Base15Test(unittest.TestCase):
    def signals(self):
        rng = np.random.default_rng(1)
        t = np.arange(HZ) / HZ
        return {
            "sine": sine(1000.0)(t),
            "noise": rng.normal(0.0, 0.1, HZ),
            "silence": np.zeros(HZ),
            "small": 1e-4 * rng.normal(0.0, 1.0, HZ),
            "dc_sine": 0.2 + 0.3 * np.sin(2 * np.pi * 300 * t),
        }

    def test_t3_matches_audio_features(self):
        for name, x in self.signals().items():
            with self.subTest(name):
                x = x.astype(np.float32)
                ref = features._audio_features(x)
                np.testing.assert_allclose(v2.base15_segment(x), ref, **TOL)
                np.testing.assert_allclose(
                    v2.features_one({1.0: x}, "base15"), ref, **TOL
                )

    def test_t3_dc_offset_zcr_uses_mean_removed_signal(self):
        x = self.signals()["dc_sine"]
        zcr = v2.base15_segment(x)[-1]
        # 300 Hz の正弦波: 1 秒に 600 回の符号の変化
        self.assertAlmostEqual(zcr * (HZ - 1), 600, delta=2)

    def test_t3_short_window_is_zero(self):
        out = v2.features_one({1.0: np.ones(HZ - 1)}, "base15")
        np.testing.assert_array_equal(out, np.zeros(15))


class A52Test(unittest.TestCase):
    def test_t4_dims_and_names(self):
        self.assertEqual(v2.N_DIMS["a52"], 52)
        names = v2.FEATURE_NAMES["a52"]
        self.assertEqual(names[0], "a_mean_0")
        self.assertEqual(names[13], "a_max_0")
        self.assertEqual(names[26], "a_std_0")
        self.assertEqual(names[39], "a_dabs_0")
        self.assertEqual(names[-1], "a_dabs_12")
        self.assertEqual(len(set(names)), 52)
        self.assertEqual(
            {k: v2.N_DIMS[k] for k in v2.FEATURE_SETS},
            {"base15": 15, "a52": 52, "base15_mw": 45, "a52_mw": 156},
        )
        self.assertEqual(v2.A_FMAX_HZ, 4000.0)

    def test_t4_constant_coefficients(self):
        C = np.tile(np.linspace(-3.0, 5.0, 13), (98, 1))
        out = v2.a_stats(C)
        mean, mx, std, dabs = out[:13], out[13:26], out[26:39], out[39:]
        np.testing.assert_allclose(mx, mean)
        np.testing.assert_allclose(std, 0.0, atol=1e-12)
        np.testing.assert_allclose(dabs, 0.0, atol=1e-12)

    def test_t4_silence(self):
        out = v2.a52_segment(np.zeros(HZ))
        mean, mx, std, dabs = out[:13], out[13:26], out[26:39], out[39:]
        np.testing.assert_allclose(mx, mean)
        np.testing.assert_allclose(std, 0.0, atol=1e-9)
        np.testing.assert_allclose(dabs, 0.0, atol=1e-9)
        # c0 は log(1e-10) 由来
        self.assertLess(mean[0], -50.0)

    def test_t4_sine_above_fmax_is_near_floor(self):
        t = np.arange(HZ) / HZ
        e = {}
        for f in (1000.0, 6000.0):
            x = 0.5 * np.sin(2 * np.pi * f * t)
            p = v2._power(v2._preemph(x))
            e[f] = float((p @ v2._MEL_A.T).sum())
        # Hamming の漏れの分だけ残る（−30 dB 未満）
        self.assertLess(e[6000.0], 1e-3 * e[1000.0])
        # 基準（0〜8000 Hz）のメルには 6000 Hz が入る
        p = v2._power(v2._preemph(0.5 * np.sin(2 * np.pi * 6000.0 * t)))
        self.assertGreater(float((p @ v2._MEL_BASE.T).sum()), 1e3 * e[6000.0])


class MultiWindowTest(TmpCase):
    def test_t5_centers_are_aligned(self):
        for i in (20000, 50000):
            centers = []
            for L in v2.MW_LENGTHS_S:
                a, b = v2.segment_bounds(i, L, 0, 10**9)
                self.assertEqual(b - a, int(round(L * HZ)))
                centers.append((a + b) / 2)
            self.assertEqual(centers, [i + HZ / 2] * 3)

    def test_t5_never_reads_outside_lo_hi(self):
        rng = np.random.default_rng(2)
        x = rng.normal(0.0, 0.1, 10 * HZ)
        lo, hi = 2 * HZ, 8 * HZ
        x[:lo] = np.nan
        x[hi:] = np.nan
        starts = [lo, lo + 4000, hi - HZ - 4000, hi - HZ]
        for fs in v2.FEATURE_SETS:
            with self.subTest(fs):
                X = v2.window_features(x, starts, fs, lo, hi)
                self.assertTrue(np.isfinite(X).all())
        # 最初の窓の 2.0 秒は [lo, lo + 1.5 秒) に切り詰める
        self.assertEqual(v2.segment_bounds(lo, 2.0, lo, hi), (lo, lo + 24000))

    def test_t5_truncated_long_window_uses_available_samples(self):
        rng = np.random.default_rng(3)
        x = rng.normal(0.0, 0.1, 6 * HZ)
        lo, hi = HZ, 5 * HZ
        X = v2.window_features(x, [lo], "base15_mw", lo, hi)[0]
        ref = v2.base15_segment(x[lo : lo + 24000])
        np.testing.assert_allclose(X[30:], ref.astype(np.float32), rtol=1e-6)

    def test_t2_and_t5_session_grid_and_valid(self):
        info = make_throat_session(self.root, noise_lsb=30.0)
        ref = features.extract_throat_session(info["dir"])
        for fs in v2.FEATURE_SETS:
            with self.subTest(fs):
                f = v2.extract(info["dir"], fs)
                np.testing.assert_array_equal(f.t_start_s, ref.t_start_s)
                np.testing.assert_array_equal(f.valid, ref.valid)
                self.assertEqual(f.X.shape, (len(ref.X), v2.N_DIMS[fs]))
                self.assertEqual(f.session, ref.session)
                self.assertTrue(np.isfinite(f.X).all())
        f = v2.extract(info["dir"], "base15")
        np.testing.assert_allclose(
            f.X.astype(np.float64), ref.X.astype(np.float64), **TOL
        )
        # 1.0 秒の部分は単一窓の組と同じ
        mw = v2.extract(info["dir"], "base15_mw")
        np.testing.assert_array_equal(mw.X[:, 15:30], f.X)
        t, M, valid = v2.extract_mfe(info["dir"])
        np.testing.assert_array_equal(t, ref.t_start_s)
        np.testing.assert_array_equal(valid, ref.valid)
        self.assertEqual(M.shape, (len(t), 98, 40))
        self.assertEqual(M.dtype, np.float32)
        # 戻り値に波形を含めない
        self.assertEqual(
            [fl.name for fl in dataclasses.fields(f)],
            ["session", "t_start_s", "X", "valid"],
        )

    def test_t5_sound_before_ostart_does_not_change_features(self):
        # o start（3.0 秒）より 0.5 秒以上前だけに大きな音
        def loud(t):
            return np.where(
                (t >= 1.0) & (t < 2.5), 0.8 * np.sin(2 * np.pi * 700 * t), 0.0
            )

        quiet = make_throat_session(
            self.root, name="20261011-100000_self_water", noise_lsb=30.0
        )
        noisy = make_throat_session(
            self.root,
            name="20261011-110000_self_water",
            noise_lsb=30.0,
            signal=loud,
        )
        for fs in v2.FEATURE_SETS:
            with self.subTest(fs):
                a = v2.extract(quiet["dir"], fs)
                b = v2.extract(noisy["dir"], fs)
                np.testing.assert_array_equal(a.t_start_s, b.t_start_s)
                np.testing.assert_array_equal(a.X, b.X)
        np.testing.assert_array_equal(
            v2.extract_mfe(quiet["dir"])[1], v2.extract_mfe(noisy["dir"])[1]
        )
        # 大きな音は 16 kHz の信号の lo より前に入っている
        x, _, _, idx, _, _, _ = features._throat_windows(noisy["dir"])
        self.assertGreater(np.abs(x[: idx[0]]).max(), 0.3)
        self.assertGreater(v2.segment_bounds(idx[0], 2.0, 0, len(x))[0], 0)

    def test_unknown_set_stops(self):
        with self.assertRaises(ValueError):
            v2.window_features(np.zeros(HZ), [0], "base16", 0, HZ)


CACHE_VARS = (
    "LIBROSA_CACHE_DIR",
    "LIBROSA_CACHE_VERBOSE",
    "LIBROSA_CACHE_LEVEL",
)
# 案A の計算（librosa.feature.delta を通る）を合成の入力で呼ぶ
A52_SCRIPT = (
    "import numpy as np\n"
    "{pre}"
    "import features_v2\n"
    "x = np.random.default_rng(0).normal(0, 0.1, 16000)\n"
    "out = features_v2.a52_segment(x)\n"
    "print('ok', out.shape)\n"
)


class LibrosaCacheTest(TmpCase):
    """librosa のキャッシュ（joblib）が有効なら止まる（0033。特徴量を
    ディスク・標準出力に出さない）。"""

    def run_py(self, env_extra: dict, pre: str = ""):
        import os, subprocess, sys

        env = {k: v for k, v in os.environ.items() if k not in CACHE_VARS}
        env.update(env_extra)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent)
        work = self.root / "cwd"  # 空の保存先は作業フォルダの joblib/ になる
        work.mkdir(exist_ok=True)
        return subprocess.run(
            [sys.executable, "-c", A52_SCRIPT.format(pre=pre)],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )

    def files(self, d: Path) -> list:
        return [p for p in d.rglob("*") if p.is_file()]

    def test_cache_dir_stops_and_writes_nothing(self):
        for pre in ("", "import librosa\n"):
            with self.subTest(librosa_imported_first=bool(pre)):
                cache = self.root / f"cache{len(pre)}"
                cache.mkdir()
                r = self.run_py(
                    {
                        "LIBROSA_CACHE_DIR": str(cache),
                        "LIBROSA_CACHE_LEVEL": "50",
                    },
                    pre,
                )
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("LIBROSA_CACHE_DIR が設定されています", r.stderr)
                self.assertNotIn("ok", r.stdout)
                self.assertEqual(self.files(cache), [])

    def test_empty_cache_dir_also_stops(self):
        # librosa は空の値も保存先ありと扱い、作業フォルダの joblib/ に書く
        r = self.run_py({"LIBROSA_CACHE_DIR": "", "LIBROSA_CACHE_LEVEL": "50"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("LIBROSA_CACHE_DIR が設定されています", r.stderr)
        self.assertEqual(self.files(self.root / "cwd"), [])

    def test_cache_verbose_stops(self):
        r = self.run_py({"LIBROSA_CACHE_VERBOSE": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("LIBROSA_CACHE_VERBOSE", r.stderr)
        self.assertNotIn("ok", r.stdout)

    def test_cache_enabled_on_imported_librosa_stops(self):
        # 環境変数なしで import した後に、キャッシュの保存先を付けた場合
        cache = self.root / "mem"
        pre = (
            "import joblib, librosa\n"
            f"librosa.cache.memory = joblib.Memory({str(cache)!r})\n"
        )
        r = self.run_py({}, pre)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("import 済みの librosa のキャッシュが有効です", r.stderr)
        self.assertNotIn("ok", r.stdout)
        # joblib が保存先を作るときの .gitignore 以外は書かれない
        self.assertEqual(
            [p.name for p in self.files(cache) if p.name != ".gitignore"], []
        )

    def test_cache_enabled_after_import_stops_in_process(self):
        import joblib, librosa
        from unittest import mock

        cache = self.root / "late"
        mem = joblib.Memory(str(cache))
        before = self.files(cache) if cache.exists() else []
        with mock.patch.object(librosa.cache, "memory", mem):
            with self.assertRaises(SystemExit):
                v2.a52_segment(np.zeros(HZ))
        self.assertEqual(self.files(cache) if cache.exists() else [], before)
        v2.a52_segment(np.zeros(HZ))  # 元に戻せば通る

    def test_without_cache_settings_runs(self):
        r = self.run_py({})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ok (52,)", r.stdout)
        self.assertEqual(self.files(self.root / "cwd"), [])
        r = self.run_py({"LIBROSA_CACHE_VERBOSE": "0"})
        self.assertEqual(r.returncode, 0, r.stderr)


class ImportTest(unittest.TestCase):
    def test_existing_modules_do_not_import_new_dependencies(self):
        here = Path(__file__).resolve().parent
        for name in ("features.py", "evaluate.py", "split.py", "train_eval.py"):
            src = (here / name).read_text(encoding="utf-8")
            for dep in ("librosa", "matplotlib", "torch"):
                self.assertNotIn(f"import {dep}", src, name)


if __name__ == "__main__":
    unittest.main()
