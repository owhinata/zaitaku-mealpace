"""train_eval.py を合成セッションで検証する（手順の定義は docs/decisions/0014）。

実行: python -m unittest discover -s analysis -v
合成セッションは tempfile に作る。data/ と実データは使わない。乱数は seed を固定する。
「嚥下」は、マーカー t から 0.8 秒のあいだ IMU の Y 軸に 8 Hz の振動、音に 300 Hz のバーストを入れたもの。
"""
from __future__ import annotations
import contextlib, dataclasses, io, shutil, tempfile, unittest
from pathlib import Path
from unittest import mock

import numpy as np

import evaluate, features, split, train_eval
from test_features import HZ, make_session

DURATION_S = 60
BURST_S = 0.8
DAYS = ("20260901", "20260902", "20260903")
# (時刻, cond, 嚥下のマーカーの最初の時刻)。water は 10 秒おきに 5 回
SLOTS = (("100000", "water", 8.0), ("101000", "water", 6.5), ("102000", "quiet", None))
EVAL_FILES = ("imu.csv", "audio.wav", "audio_chunks.csv", "events.csv")


def swallow_times(first: float | None) -> list[float]:
    return [] if first is None else [first + 10.0 * k for k in range(5)]


def synth_session(root: Path, name: str, cond: str, swallows: list[float], seed: int) -> Path:
    rng = np.random.default_rng(seed)
    audio_noise = 0.01 * rng.standard_normal(DURATION_S * HZ)
    imu_noise = 0.002 * rng.standard_normal((DURATION_S * 1000, 3))   # IMU の行数より多めに取る

    def burst(t):
        return np.any([(t >= s) & (t < s + BURST_S) for s in swallows], axis=0) if swallows else np.zeros(len(t), bool)

    def acc(t):
        out = imu_noise[:len(t)].copy()
        out[:, 1] += np.where(burst(t), 0.1 * np.sin(2 * np.pi * 8 * t), 0.0)
        return out

    def audio(t):
        return audio_noise[:len(t)] + np.where(burst(t), 0.3 * np.sin(2 * np.pi * 300 * t), 0.0)

    d = make_session(root, name, imu_dur_ms=DURATION_S * 1000, n_samples=DURATION_S * HZ,
                     acc=acc, audio=audio, meta={"cond": cond})
    write_events(d, swallows)
    return d


def write_events(d: Path, swallows: list[float]) -> None:
    rows = "".join(f"{int(round(s * 1000))},s,\n" for s in swallows)
    (d / "events.csv").write_text("t_ms,label,note\n" + rows, encoding="utf-8")


def fake_scored(prob_at: dict[float, float], swallows: list[float], *, invalid: tuple = (),
                name: str = "20260901-100000_self_water") -> train_eval.Scored:
    """手で作った確率の列。窓は 0〜59 秒の 0.25 秒刻み、記録の範囲は 0〜60 秒。指定の無い窓の確率は 0。"""
    t = 0.25 * np.arange(237)
    prob = np.zeros(len(t))
    valid = np.ones(len(t), dtype=bool)
    for w, p in prob_at.items():
        prob[int(round(w / 0.25))] = p
    for w in invalid:
        valid[int(round(w / 0.25))] = False
    f = features.WindowFeatures(session=name, t_start_s=t, X=np.zeros((len(t), features.N_FEATURES), np.float32),
                                valid=valid)
    d = train_eval.SessionData(name=name, feats=f, swallows=swallows, t0_s=0.0, duration_s=60.0,
                               labels=train_eval.window_labels(t, swallows))
    return train_eval.Scored(data=d, prob=prob)


class SynthCase(unittest.TestCase):
    """3 収集日 × 3 セッション（water ×2、quiet）、各 60 秒を1回だけ作る。ほかの構成はここからのコピー。"""

    @classmethod
    def setUpClass(cls):
        if SynthCase.tmp is None:   # クラスをまたいで1回だけ作り、モジュールの終わりに消す
            tmp = tempfile.TemporaryDirectory()
            unittest.addModuleCleanup(tmp.cleanup)
            SynthCase.tmp = Path(tmp.name)
            SynthCase.root = SynthCase.tmp / "base"
            SynthCase.root.mkdir()
            SynthCase.names = []
            for i, day in enumerate(DAYS):
                for j, (hms, cond, first) in enumerate(SLOTS):
                    name = f"{day}-{hms}_self_{cond}"
                    synth_session(SynthCase.root, name, cond, swallow_times(first), seed=100 + 10 * i + j)
                    SynthCase.names.append(name)
            SynthCase.train_names, SynthCase.eval_names = SynthCase.names[:3], SynthCase.names[3:]

    tmp = None

    @classmethod
    def copy_root(cls, label: str, mapping: dict[str, str]) -> Path:
        """{新しいフォルダ名: 元のフォルダ名} で、base のセッションを別の構成に並べる。"""
        root = cls.tmp / label
        root.mkdir()
        for new, old in mapping.items():
            shutil.copytree(cls.root / old, root / new)
        return root

    @classmethod
    def eval_part(cls) -> dict[str, str]:
        """評価側の 2 収集日（3 セッション）。"""
        return {"20260902-100000_self_water": cls.names[3], "20260902-102000_self_quiet": cls.names[5],
                "20260903-100000_self_water": cls.names[6]}


class LabelTest(unittest.TestCase):
    T = 0.25 * np.arange(80)

    def starts(self, labels, value) -> list[float]:
        return self.T[labels == value].tolist()

    def test_1_single_swallow(self):
        labels = train_eval.window_labels(self.T, [10.0])
        self.assertEqual(self.starts(labels, train_eval.POSITIVE), [9.5, 9.75, 10.0, 10.25, 10.5])
        self.assertEqual(self.starts(labels, train_eval.UNUSED), [9.0, 9.25, 10.75, 11.0])
        self.assertEqual(int((labels == train_eval.NEGATIVE).sum()), 80 - 9)

    def test_1_positive_wins_over_unused(self):
        labels = train_eval.window_labels(self.T, [10.0, 11.0])
        self.assertEqual(self.starts(labels, train_eval.POSITIVE), (9.5 + 0.25 * np.arange(9)).tolist())   # 9.5〜11.5
        self.assertEqual(self.starts(labels, train_eval.UNUSED), [9.0, 9.25, 11.75, 12.0])
        # 順番を逆に渡しても同じ
        np.testing.assert_array_equal(labels, train_eval.window_labels(self.T, [11.0, 10.0]))

    def test_1_no_swallow(self):
        self.assertTrue((train_eval.window_labels(self.T, []) == train_eval.NEGATIVE).all())


class ThresholdRuleTest(unittest.TestCase):
    SWALLOWS = [10.0, 30.0]              # 非嚥下 = 60 − 4 = 56 秒
    HIT_1 = {9.5: 0.9, 9.75: 0.9, 10.0: 0.9, 10.25: 0.9, 10.5: 0.9}
    HIT_2 = {29.5: 0.5, 29.75: 0.5, 30.0: 0.5, 30.25: 0.5, 30.5: 0.5}

    def test_5_candidates(self):
        self.assertEqual(train_eval.THRESHOLDS, (0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5,
                                                 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95))

    def test_5_max_detection_under_limit_then_higher(self):
        # 離れた単発の陽性（確率 0.3）が 4 つ。閾値 0.30 以下は誤検出 4 回 = 4.29 回/分で基準を外れる。
        # 0.35〜0.50 は 2/2・0 回（同率 → 高いほうの 0.50）。0.55〜0.90 は 1/2。0.95 は 0/2
        s = fake_scored({**self.HIT_1, **self.HIT_2, 20.0: 0.3, 40.0: 0.3, 45.0: 0.3, 50.0: 0.3}, self.SWALLOWS)
        table = train_eval.threshold_table([s])
        row = {r["threshold"]: r for r in table}
        self.assertEqual([row[0.3]["detected"], row[0.3]["false_positive_runs"]], [2, 4])
        self.assertAlmostEqual(row[0.3]["false_positives_per_min"], 4 / (56 / 60))
        self.assertEqual([row[0.35]["detected"], row[0.35]["false_positive_runs"]], [2, 0])
        self.assertEqual([row[0.5]["detected"], row[0.5]["false_positive_runs"]], [2, 0])
        self.assertEqual([row[0.55]["detected"], row[0.9]["detected"], row[0.95]["detected"]], [1, 1, 0])
        threshold, rule = train_eval.pick_threshold(table)
        self.assertEqual(threshold, 0.5)
        self.assertIn("検出率が最大", rule)

    def test_5_limit_is_inclusive(self):
        # ちょうど 3.0 回/分は基準の内側、わずかに超えたら外側（表を直接渡す）
        table = [{"threshold": 0.4, "detection_rate": 1.0, "false_positives_per_min": 3.0 + 1e-9},
                 {"threshold": 0.5, "detection_rate": 0.8, "false_positives_per_min": 3.0},
                 {"threshold": 0.6, "detection_rate": 0.6, "false_positives_per_min": 0.0}]
        self.assertEqual(train_eval.pick_threshold(table)[0], 0.5)

    def test_5_no_candidate_gives_min_false_positive_then_higher(self):
        # 確率 1.0 の単発が 20・40、確率 0.6 の単発が 25・35・45、50.0〜50.5 は 1.0・0.8・1.0（高い閾値で 2 つに分かれる）。
        # 誤検出: 0.60 以下 6 回、0.65〜0.80 は 3 回 = 3.21 回/分、0.85 以上は 4 回。どれも 3.0 を超える → 最小の 3 回で高いほうの 0.80
        s = fake_scored({**self.HIT_1, **self.HIT_2, 20.0: 1.0, 40.0: 1.0, 25.0: 0.6, 35.0: 0.6, 45.0: 0.6,
                         50.0: 1.0, 50.25: 0.8, 50.5: 1.0}, self.SWALLOWS)
        table = train_eval.threshold_table([s])
        row = {r["threshold"]: r for r in table}
        self.assertEqual([row[t]["false_positive_runs"] for t in (0.05, 0.6, 0.65, 0.8, 0.85, 0.95)],
                         [6, 6, 3, 3, 4, 4])
        self.assertAlmostEqual(row[0.8]["false_positives_per_min"], 3 / (56 / 60))
        self.assertTrue(all(r["false_positives_per_min"] > 3.0 for r in table))
        threshold, rule = train_eval.pick_threshold(table)
        self.assertEqual(threshold, 0.8)
        self.assertIn("誤検出率が最小", rule)

    def test_5_sum_over_sessions(self):
        # 合算は回数の和: 嚥下 2 + 0、非嚥下 56 + 60 秒
        a = fake_scored({**self.HIT_1, 20.0: 0.9}, self.SWALLOWS)
        b = fake_scored({20.0: 0.9}, [], name="20260901-102000_self_quiet")
        row = {r["threshold"]: r for r in train_eval.threshold_table([a, b])}[0.5]
        self.assertEqual([row["swallows"], row["detected"], row["false_positive_runs"]], [2, 1, 2])
        self.assertAlmostEqual(row["false_positives_per_min"], 2 / (116 / 60))

    def test_6b_no_swallow_in_sum_stops(self):
        b = fake_scored({20.0: 0.9}, [], name="20260901-102000_self_quiet")
        with self.assertRaises(SystemExit) as cm:
            train_eval.threshold_table([b])
        self.assertIn("嚥下が 0 件", str(cm.exception))
        self.assertIn("20260901-102000_self_quiet", str(cm.exception))


class InvalidWindowTest(unittest.TestCase):
    RUN = {20.0 + 0.25 * k: 0.9 for k in range(9)}   # 非嚥下の区間で 20.0〜22.0 の 9 窓が続けて陽性

    def test_7_invalid_windows_split_a_run(self):
        whole = fake_scored(self.RUN, [10.0])
        self.assertEqual(train_eval.session_metrics(whole, 0.5)["false_positive_runs"], 1)
        # 途中の 20.75 と 21.0 が無効 → 20.5 と 21.25 の差 0.75 秒 > HOP_S × 1.5 → 2 回
        cut = fake_scored(self.RUN, [10.0], invalid=(20.75, 21.0))
        self.assertEqual(train_eval.positive_windows(cut, 0.5), [20.0, 20.25, 20.5, 21.25, 21.5, 21.75, 22.0])
        self.assertEqual(train_eval.session_metrics(cut, 0.5)["false_positive_runs"], 2)

    def test_7_invalid_window_is_never_positive(self):
        # 嚥下の区間の陽性が全部無効なら、検出されない
        hit = {9.5: 0.9, 9.75: 0.9, 10.0: 0.9}
        self.assertEqual(train_eval.session_metrics(fake_scored(hit, [10.0]), 0.5)["detected"], 1)
        s = fake_scored(hit, [10.0], invalid=(9.5, 9.75, 10.0))
        self.assertEqual(train_eval.positive_windows(s, 0.5), [])
        self.assertEqual(train_eval.session_metrics(s, 0.5)["detected"], 0)
        self.assertEqual(train_eval.reference([s], 0.5)["positive_windows"], 0)


class SplitCheckTest(SynthCase):
    def assert_stops(self, eval_min: int, text: str):
        with self.assertRaises(SystemExit) as cm:
            train_eval.run(self.root, eval_min)
        self.assertIn(text, str(cm.exception))

    def test_2_eval_has_one_day(self):
        self.assert_stops(3, "評価側の収集日が 2 日ではありません")

    def test_2_eval_has_three_days(self):
        self.assert_stops(7, "評価側の収集日が 2 日ではありません")

    def test_2_days_overlap(self):
        self.assert_stops(4, "収集日が重なっています")
        self.assert_stops(5, "収集日が重なっています")

    def test_2_train_is_empty(self):
        self.assert_stops(9, "セッションが足りません")
        with self.assertRaises(SystemExit) as cm:
            train_eval.check_collection_days([], self.eval_names)
        self.assertIn("学習側の収集日がありません", str(cm.exception))

    def test_2_correct_split_passes(self):
        train_eval.check_collection_days(self.train_names, self.eval_names)

    def test_2_stops_before_reading_sessions(self):
        with mock.patch.object(features, "extract_session", side_effect=AssertionError("読まれた")):
            self.assert_stops(4, "収集日が重なっています")


class RunTest(SynthCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.default = train_eval.run(cls.root, 6)
        cls.final = train_eval.run(cls.root, 6, final=True)

    def guards(self, forbidden: list[str]):
        """forbidden のセッションに対して呼ばれたら例外を出す差し替え。呼ばれたセッション名を calls に集める。"""
        calls: dict[str, list[str]] = {}
        stack = contextlib.ExitStack()
        for mod, fn in ((features, "extract_session"), (evaluate, "load_events"), (evaluate, "session_span")):
            real = getattr(mod, fn)

            def guard(session, *a, _real=real, _fn=fn, **kw):
                name = Path(session).name
                if name in forbidden:
                    raise AssertionError(f"評価側のセッションで {_fn} が呼ばれた: {name}")
                calls.setdefault(_fn, []).append(name)
                return _real(session, *a, **kw)
            stack.enter_context(mock.patch.object(mod, fn, side_effect=guard))
        return stack, calls

    def test_3_default_run_does_not_open_eval_sessions(self):
        stack, calls = self.guards(self.eval_names)
        with stack:
            r = train_eval.run(self.root, 6)
        for fn in ("extract_session", "load_events", "session_span"):
            self.assertEqual(calls[fn], self.train_names, fn)
        self.assertEqual(r.threshold, self.default.threshold)

    def test_3_default_run_works_without_eval_files(self):
        # 評価側は meta.json だけ（split_sessions が subject の検査に読む）でも、既定の実行は通る
        root = self.copy_root("no-eval-files", {n: n for n in self.names})
        for n in self.eval_names:
            for f in EVAL_FILES:
                (root / n / f).unlink()
        r = train_eval.run(root, 6)
        self.assertEqual(r.cv_table, self.default.cv_table)
        with self.assertRaises(OSError):
            train_eval.run(root, 6, final=True)

    def test_3_final_run_opens_eval_sessions(self):
        stack, calls = self.guards([])
        with stack:
            train_eval.run(self.root, 6, final=True)
        for fn in ("extract_session", "load_events", "session_span"):
            self.assertEqual(calls[fn], self.train_names + self.eval_names, fn)
        stack, _ = self.guards(self.eval_names)
        with stack, self.assertRaises(AssertionError):
            train_eval.run(self.root, 6, final=True)

    def test_4_threshold_depends_only_on_train_side(self):
        # 評価側の中身を入れ替える: water の嚥下のマーカーを消し、quiet に音のバーストだけのセッションを置く
        root = self.copy_root("other-eval", {n: n for n in self.names})
        for n in self.eval_names:
            if n.endswith("_water"):
                write_events(root / n, [])
            else:
                shutil.rmtree(root / n)
                synth_session(root, n, "quiet", [5.0, 25.0], seed=999)
                write_events(root / n, [])
        other = train_eval.run(root, 6, final=True)
        self.assertEqual(other.threshold, self.final.threshold)
        self.assertEqual(other.cv_table, self.final.cv_table)
        self.assertEqual(other.cv_reference, self.final.cv_reference)
        np.testing.assert_array_equal(other.model.standardizer.mean, self.final.model.standardizer.mean)
        np.testing.assert_array_equal(other.model.clf.coef_, self.final.model.clf.coef_)
        self.assertNotEqual(other.eval_total, self.final.eval_total)   # 評価側の中身は確かに違う
        self.assertEqual(other.eval_total["swallows"], 0)
        self.assertGreater(other.eval_total["false_positive_runs"], 0)

    def test_6_one_train_day_gives_one_fold_per_session(self):
        folds = self.default.folds
        self.assertEqual([f.val_sessions for f in folds], [(n,) for n in self.train_names])
        for f in folds:
            self.assertEqual(f.name, f"セッション {f.val_sessions[0]}")
            self.assertEqual(set(f.standardizer_sessions), set(self.train_names) - set(f.val_sessions))
            self.assertEqual(f.standardizer_sessions, f.train_sessions)
            self.assertFalse(set(f.standardizer_sessions) & set(self.eval_names))

    def test_6_two_train_days_give_one_fold_per_day(self):
        mapping = {n: n for n in self.names}
        mapping.update({n.replace("20260901", "20260831"): n for n in self.train_names})
        r = train_eval.run(self.copy_root("four-days", mapping), 6)
        self.assertEqual(len(r.train), 6)
        self.assertEqual([f.name for f in r.folds], ["収集日 20260831", "収集日 20260901"])
        for f, day in zip(r.folds, ("20260831", "20260901")):
            self.assertEqual(len(f.val_sessions), 3)
            self.assertTrue(all(n.startswith(day) for n in f.val_sessions))
            self.assertFalse(set(f.standardizer_sessions) & set(f.val_sessions))
            self.assertTrue(all(not n.startswith(day) for n in f.standardizer_sessions))
            self.assertEqual(len(f.standardizer_sessions), 3)

    def test_6b_quiet_only_validation_fold_does_not_stop(self):
        quiet_folds = [f for f in self.default.folds if f.val_sessions[0].endswith("_quiet")]
        self.assertEqual(len(quiet_folds), 1)

    def test_6b_single_train_session_stops(self):
        root = self.copy_root("one-train", {"20260901-100000_self_water": self.names[0], **self.eval_part()})
        with self.assertRaises(SystemExit) as cm:
            train_eval.run(root, 3)
        self.assertIn("グループが 2 つ未満", str(cm.exception))
        self.assertIn("20260901-100000_self_water", str(cm.exception))

    def test_6b_fold_without_positive_stops(self):
        # 嚥下のあるセッションは 100000_self_water だけ。それが検証側に回る fold の学習側は陰性だけ
        root = self.copy_root("one-water", {"20260901-100000_self_water": self.names[0],
                                            "20260901-102000_self_quiet": self.names[2], **self.eval_part()})
        with self.assertRaises(SystemExit) as cm:
            train_eval.run(root, 3)
        msg = str(cm.exception)
        self.assertIn("陽性と陰性の両方がそろっていません", msg)
        self.assertIn("検証側 = セッション 20260901-100000_self_water", msg)
        self.assertIn("学習に使うセッション: 20260901-102000_self_quiet", msg)
        self.assertIn("陽性 0", msg)

    def test_6b_no_swallow_in_train_stops(self):
        root = self.copy_root("all-quiet", {"20260901-100000_self_quiet": self.names[2],
                                            "20260901-102000_self_quiet": self.names[5], **self.eval_part()})
        with self.assertRaises(SystemExit) as cm:
            train_eval.run(root, 3)
        self.assertIn("fold", str(cm.exception))
        self.assertIn("20260901-100000_self_quiet", str(cm.exception))

    def test_6c_unused_windows_are_standardized_but_not_fitted(self):
        data = [train_eval.load_session(self.root / n) for n in self.train_names]
        n_valid = sum(int(d.feats.valid.sum()) for d in data)
        n_unused = sum(int((d.feats.valid & (d.labels == train_eval.UNUSED)).sum()) for d in data)
        self.assertEqual(n_unused, 2 * 5 * 4)   # water 2 セッション × 嚥下 5 回 × 4 窓
        shapes = []

        class Recording(train_eval.LogisticRegression):
            def fit(self, X, y, *a, **kw):
                shapes.append((X.shape, int((y == 1).sum())))
                return super().fit(X, y, *a, **kw)

        with mock.patch.object(train_eval, "LogisticRegression", Recording):
            model = train_eval.fit_model(data, "テスト")
        self.assertEqual(shapes, [((n_valid - n_unused, 29), 2 * 5 * 5)])
        self.assertEqual(model.n_fit_rows, n_valid - n_unused)
        all_valid = np.concatenate([d.feats.X[d.feats.valid] for d in data]).astype(np.float64)
        np.testing.assert_allclose(model.standardizer.mean, all_valid.mean(axis=0))
        self.assertEqual(model.standardizer.sessions, tuple(self.train_names))
        self.assertEqual(self.default.model.n_fit_rows, n_valid - n_unused)

    def test_7_invalid_windows_are_not_used_for_training(self):
        data = [train_eval.load_session(self.root / n) for n in self.train_names]
        self.assertEqual(train_eval.count_windows(data)[0], 0)
        valid = data[2].feats.valid.copy()
        valid[40:50] = False   # quiet の 10 窓（全部陰性）
        data[2] = dataclasses.replace(data[2], feats=dataclasses.replace(data[2].feats, valid=valid))
        self.assertEqual(train_eval.count_windows(data), (10, sum(len(d.labels) for d in data)))
        model = train_eval.fit_model(data, "テスト")
        self.assertEqual(model.n_fit_rows, self.default.model.n_fit_rows - 10)
        kept = np.concatenate([d.feats.X[d.feats.valid] for d in data]).astype(np.float64)
        np.testing.assert_allclose(model.standardizer.mean, kept.mean(axis=0))

    def test_7_invalid_window_count_is_reported(self):
        # 学習側の quiet の IMU を 0.5 秒ぶん抜く → 飛びの区間に掛かる窓が無効になり、数が報告に出る
        # （evaluate.session_span は 1 秒を超える飛びを止めるので、それより短くする）
        root = self.copy_root("imu-gap", {n: n for n in self.names})
        path = root / self.train_names[2] / "imu.csv"
        lines = path.read_text(encoding="utf-8").splitlines()
        kept = [lines[0]] + [ln for ln in lines[1:] if not 20000 <= int(ln.split(",")[0]) < 20500]
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        r = train_eval.run(root, 6)
        n_invalid, n_all = r.train_windows
        self.assertEqual(n_all, self.default.train_windows[1])
        # 飛びの区間 (抜いた行の直前の t_ms, 直後の t_ms) と交わる窓 [s, s + 1.0)。開始 19.0〜20.5 の 7 窓
        t_ms = [int(ln.split(",")[0]) for ln in lines[1:]]
        g0 = max(t for t in t_ms if t < 20000) / 1000.0
        g1 = min(t for t in t_ms if t >= 20500) / 1000.0
        starts = 0.25 * np.arange(n_all // 3)
        self.assertEqual(n_invalid, int(((g0 < starts + 1.0) & (g1 > starts)).sum()))
        self.assertEqual(n_invalid, 7)
        self.assertIn(f"{n_invalid} / {n_all}", train_eval.format_report(r))
        self.assertIn("0 / ", train_eval.format_report(self.default))
        self.assertEqual(r.model.n_fit_rows, self.default.model.n_fit_rows - n_invalid)

    def test_8_final_metrics(self):
        t = self.final.eval_total
        self.assertEqual(t["swallows"], 20)
        self.assertGreaterEqual(t["detection_rate"], 0.9)
        self.assertLessEqual(t["false_positives_per_min"], 3.0)
        self.assertEqual(t["confusion"]["tp"] + t["confusion"]["fn"], 20)
        self.assertIsNone(t["confusion"]["tn"])
        self.assertIsNone(self.default.eval_total)
        self.assertIsNone(self.default.eval_per_session)

    def test_8_aggregate_gets_exactly_the_eval_list(self):
        seen = []
        real = evaluate.aggregate

        def spy(per_session):
            seen.append(list(per_session))
            return real(per_session)
        with mock.patch.object(evaluate, "aggregate", side_effect=spy):
            r = train_eval.run(self.root, 6, final=True)
        expected = split.split_sessions(self.root, 6, subject="self")["eval"]
        self.assertEqual(seen, [expected])
        self.assertEqual(r.eval, expected)
        self.assertEqual(r.eval_total["sessions"], expected)
        with mock.patch.object(evaluate, "aggregate", side_effect=spy):
            train_eval.run(self.root, 6)
        self.assertEqual(len(seen), 1)   # 既定の実行は aggregate を呼ばない

    def test_8_final_report_contents(self):
        text = train_eval.format_report(self.final)
        self.assertIn("## 学習に使ったセッション（3）", text)
        self.assertIn("## 評価に使ったセッション（6）", text)
        for n in self.names:
            self.assertIn(n, text)
        for word in ("混同行列", "TP ", "FN ", "FP ", "常時陽性の場合", "陽性窓の割合", "合格線との比較",
                     "`--eval-min`: 6", "コミット: ", "scikit-learn ", "numpy ", "無効な窓", "| 0.95 |"):
            self.assertIn(word, text)
        self.assertNotIn("TN ", text.replace("TN は定義できない", ""))
        self.assertNotIn("評価側は採点していない", text.replace("既定（学習側だけ。評価側は採点していない）", ""))
        self.assert_no_window_values(text)

    def test_8_default_report_contents(self):
        text = train_eval.format_report(self.default)
        self.assertIn("評価側は採点していない", text)
        self.assertNotIn("評価に使ったセッション", text)
        self.assertNotIn("混同行列", text)
        self.assertNotIn("合格線との比較", text)
        for n in self.names:
            self.assertIn(n, text)
        self.assert_no_window_values(text)

    def assert_no_window_values(self, text: str):
        """特徴量の値や確率の列が無い: 特徴量の名前が出ない、窓の数だけ並ぶ行や表が無い。"""
        for name in features.FEATURE_NAMES:
            self.assertNotIn(name, text)
        for word in ("t_start", "prob", "coef", "mfcc"):
            self.assertNotIn(word, text)
        lines = text.splitlines()
        self.assertLess(len(lines), 120)   # 窓は 1 セッション 237 個。窓ごとの行があれば超える
        self.assertLess(max(len(ln) for ln in lines), 250)
        table_rows = [ln for ln in lines if ln.startswith("| ")]
        self.assertLessEqual(len(table_rows), 1 + len(train_eval.THRESHOLDS) + 1 + len(self.eval_names) + 3)

    def test_9_same_input_gives_identical_report(self):
        again = train_eval.run(self.root, 6, final=True)
        self.assertEqual(train_eval.format_report(again), train_eval.format_report(self.final))
        self.assertEqual(train_eval.format_report(train_eval.run(self.root, 6)),
                         train_eval.format_report(self.default))

    def test_9_main_prints_the_report_and_writes_nothing(self):
        before = sorted(p.relative_to(self.root) for p in self.root.rglob("*"))
        buf = io.StringIO()
        with mock.patch.object(train_eval, "git_dirty", return_value=False), contextlib.redirect_stdout(buf):
            train_eval.main([str(self.root), "--eval-min", "6", "--final"])
            expected = train_eval.format_report(self.final) + "\n"
        self.assertEqual(buf.getvalue(), expected)
        self.assertEqual(sorted(p.relative_to(self.root) for p in self.root.rglob("*")), before)

    def test_9_final_stops_on_uncommitted_changes(self):
        """未コミットの変更があると、--final は採点せずに止まる。既定の実行は止まらず、コミットに -dirty が付く。"""
        with mock.patch.object(train_eval, "git_dirty", return_value=True):
            with mock.patch.object(train_eval, "run") as run, self.assertRaises(SystemExit):
                train_eval.main([str(self.root), "--eval-min", "6", "--final"])
            run.assert_not_called()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                train_eval.main([str(self.root), "--eval-min", "6"])
            self.assertRegex(buf.getvalue(), r"- コミット: \S+-dirty\n")
        with mock.patch.object(train_eval, "git_dirty", return_value=False):
            self.assertNotIn("-dirty", train_eval.git_commit())

    def test_9_git_unavailable_counts_as_dirty(self):
        with mock.patch.object(train_eval, "_git", return_value=None):
            self.assertTrue(train_eval.git_dirty())
            self.assertEqual(train_eval.git_commit(), "unknown-dirty")

    def test_9_eval_min_is_required(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            train_eval.main([str(self.root)])


if __name__ == "__main__":
    unittest.main()
