"""compare.py を合成データで検証する（plan #43 T7・T7b・T8）。

実行: python -m unittest discover -s analysis -v
合成の咽喉マイクのセッションは tempfile に作る（test_train_eval.synth_throat）。
data/ は使わない。

ファイル名は test_ei_upload.py より後に並ぶようにしてある。test_ei_upload.py は
import の時点で一時フォルダの片付けを addModuleCleanup に登録し、unittest は
どのモジュールが終わっても登録済みの片付けを全部実行するため、それより前に並ぶ
テストのモジュールがあると test_ei_upload.py の一時フォルダが先に消える。
"""

from __future__ import annotations
import contextlib, io, shutil, tempfile, unittest
from pathlib import Path
from unittest import mock

import numpy as np

import compare, evaluate, features, features_v2, postprocess, train_eval
from train_eval import NEGATIVE, POSITIVE, UNUSED, SessionData
from test_train_eval import THROAT_EVAL_FILES, THROAT_SWALLOWS_S, synth_throat

DAYS = ("20261011", "20261012")
SLOTS = (("100000", "water"), ("101000", "water"), ("102000", "quiet"))
FORBIDDEN_WORDS = ("誤嚥", "aspiration", "risk score", "リスク")


def build_root(base: Path) -> tuple:
    """2 収集日 × 3 セッション。収集日 1 が学習側、収集日 2 が評価側。"""
    root = base / "base"
    root.mkdir()
    names = []
    for i, day in enumerate(DAYS):
        for j, (hms, cond) in enumerate(SLOTS):
            name = f"{day}-{hms}_self_{cond}"
            sw = THROAT_SWALLOWS_S if cond == "water" else ()
            synth_throat(root, name, sw, seed=300 + 10 * i + j)
            names.append(name)
    (root / "trial-throat").mkdir()
    return root, names[:3], names[3:]


def strip_report(text: str) -> str:
    return text.split(compare.TIMING_HEADING)[0]


class CompareCase(unittest.TestCase):
    tmp = None

    @classmethod
    def setUpClass(cls):
        if CompareCase.tmp is None:
            tmp = tempfile.TemporaryDirectory()
            unittest.addModuleCleanup(tmp.cleanup)
            CompareCase.tmp = Path(tmp.name)
            r = build_root(CompareCase.tmp)
            CompareCase.root, CompareCase.train, CompareCase.eval = r
            CompareCase.result = compare.run(CompareCase.root, 3)


class RunTest(CompareCase):
    def test_t8_default_run_does_not_open_eval_sessions(self):
        # 評価側のファイルを消し（split のための空のファイルだけ置く）、読み込みの
        # 入口に評価側の名前が来たら落ちる見張りを付けて実行する
        root = self.tmp / "no-eval-files"
        root.mkdir()
        for n in self.train + self.eval:
            shutil.copytree(self.root / n, root / n)
        for n in self.eval:
            for f in THROAT_EVAL_FILES:
                (root / n / f).unlink()
            for f in ("throat.wav", "throat_chunks.csv"):
                (root / n / f).write_bytes(b"")
        calls: dict = {}
        stack = contextlib.ExitStack()
        for mod, fn in (
            (features, "_throat_windows"),
            (evaluate, "load_events"),
            (evaluate, "throat_session_span"),
        ):
            real = getattr(mod, fn)

            def guard(session, *a, _real=real, _fn=fn, **kw):
                name = Path(session).name
                if name in self.eval:
                    raise AssertionError(f"評価側で {_fn} が呼ばれた: {name}")
                calls.setdefault(_fn, set()).add(name)
                return _real(session, *a, **kw)

            stack.enter_context(mock.patch.object(mod, fn, side_effect=guard))
        with stack:
            r = compare.run(root, 3)
        self.assertEqual(
            sorted(calls),
            ["_throat_windows", "load_events", "throat_session_span"],
        )
        for fn in calls:
            self.assertEqual(calls[fn], set(self.train), fn)
        self.assertEqual(r.train, self.train)
        self.assertEqual(r.eval, self.eval)
        # 同じ入力で報告が一字一句同じ（時間の実測の節を除く）
        self.assertEqual(
            strip_report(compare.format_report(r)),
            strip_report(compare.format_report(self.result)),
        )

    def test_t8_no_final_argument(self):
        for extra in (["--final"], ["--eval-days", "1"]):
            with (
                self.subTest(extra),
                self.assertRaises(SystemExit),
                contextlib.redirect_stderr(io.StringIO()),
                mock.patch.object(compare, "run") as run,
            ):
                compare.main([str(self.root), "--eval-min", "3", *extra])
            run.assert_not_called()
        with (
            self.assertRaises(SystemExit),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            compare.main([str(self.root)])  # --eval-min は必須

    def test_t8_main_prints_report_and_writes_nothing(self):
        before = sorted(p.relative_to(self.root) for p in self.root.rglob("*"))
        buf = io.StringIO()
        with (
            mock.patch.object(compare, "run", return_value=self.result),
            contextlib.redirect_stdout(buf),
        ):
            compare.main([str(self.root), "--eval-min", "3"])
        self.assertEqual(
            buf.getvalue(), compare.format_report(self.result) + "\n"
        )
        self.assertEqual(
            sorted(p.relative_to(self.root) for p in self.root.rglob("*")),
            before,
        )

    def test_t8_figures_outside_docs_log_stops_before_computing(self):
        with (
            mock.patch.object(compare, "run") as run,
            self.assertRaises(SystemExit),
        ):
            compare.main(
                [str(self.root), "--eval-min", "3", "--figures", str(self.tmp)]
            )
        run.assert_not_called()

    def test_t8_folds_use_only_train_side(self):
        r = self.result
        self.assertEqual(set(r.folds), set(compare.SIMPLE_ORDER))
        for fs, folds in r.folds.items():
            with self.subTest(fs):
                self.assertEqual(
                    [f.val_sessions for f in folds], [(n,) for n in self.train]
                )
                for f in folds:
                    ss = set(f.standardizer_sessions)
                    self.assertEqual(ss, set(self.train) - set(f.val_sessions))
                    self.assertFalse(ss & set(self.eval))

    def test_t8_rows_and_selection(self):
        r = self.result
        self.assertEqual(len(r.rows), 10)
        self.assertEqual(
            [(x.set, x.post) for x in r.rows],
            [(s, p) for s in compare.SIMPLE_ORDER for p in compare.POSTS],
        )
        self.assertEqual(sum(x.candidate for x in r.rows), 8)
        self.assertTrue(r.selected.candidate)
        self.assertEqual(r.selected.classifier, "LR")
        for x in r.rows:
            self.assertEqual(len(x.table), 19 if x.post == "none" else 45)
        self.assertEqual(sorted(r.ref_rows), list(compare.CNN_REF_SEEDS))

    def test_t8_report_contents(self):
        text = compare.format_report(self.result)
        for n in self.train + self.eval:
            self.assertIn(n, text)
        self.assertIn("評価側は採点していない", text)
        self.assertIn("（空欄。#40 で選んだ組について 1 回だけ）", text)
        self.assertIn("いいえ（参考）", text)
        self.assertIn("0.5 秒", text)
        self.assertIn("librosa ", text)
        self.assertIn("numba==", text)
        self.assertIn("評価側の収集日: 1", text)
        self.assertNotIn("混同行列", text)
        self.assertTrue(text.rstrip().split("\n")[-1].startswith("- "))
        self.assertGreater(
            text.index(compare.TIMING_HEADING), text.index("## 付録")
        )
        for names in features_v2.FEATURE_NAMES.values():
            for name in names:
                self.assertNotIn(f"{name} ", text)
        for w in FORBIDDEN_WORDS:
            self.assertNotIn(w, text)


def fake_row(fs, post, det, fp, candidate=True):
    return compare.Row(
        set=fs,
        post=post,
        dims=1,
        classifier="LR" if candidate else "CNN",
        candidate=candidate,
        op=(0.5,) if post == "none" else (0.7, 0.4),
        rule="",
        metrics={"detection_rate": det, "false_positives_per_min": fp},
        positive_windows=0,
        windows=1,
        table=[],
    )


class PickTest(unittest.TestCase):
    def test_t8_pick_row_tie_goes_to_simpler(self):
        rows = [
            fake_row("a52_mw", "none", 0.8, 1.0),
            fake_row("a52", "hyst", 0.8, 2.0),
            fake_row("a52", "none", 0.8, 2.9),
            fake_row("base15", "none", 0.7, 0.1),
            fake_row("mfe40", "none", 0.99, 0.0, candidate=False),
        ]
        best, rule = compare.pick_row(rows)
        self.assertIs(best, rows[2])
        self.assertIn("検出率が最大", rule)

    def test_t8_pick_row_limit_is_inclusive_and_cnn_excluded(self):
        rows = [
            fake_row("base15", "none", 0.6, 3.0),
            fake_row("a52", "none", 0.9, 3.01),
            fake_row("mfe40", "hyst", 1.0, 0.5, candidate=False),
        ]
        self.assertIs(compare.pick_row(rows)[0], rows[0])

    def test_t8_pick_row_without_rows_under_limit(self):
        rows = [
            fake_row("a52", "none", 0.9, 4.0),
            fake_row("base15_mw", "hyst", 0.5, 3.5),
            fake_row("base15_mw", "none", 0.4, 3.5),
            fake_row("mfe40", "none", 1.0, 0.1, candidate=False),
        ]
        best, rule = compare.pick_row(rows)
        self.assertIs(best, rows[2])
        self.assertIn("誤検出率が最小", rule)

    def test_t8_pick_hysteresis(self):
        def r(e, x, det, fp):
            return {
                "enter": e,
                "exit": x,
                "detection_rate": det,
                "false_positives_per_min": fp,
            }

        table = [
            r(0.5, 0.3, 0.9, 3.5),
            r(0.6, 0.4, 0.8, 2.0),
            r(0.7, 0.4, 0.8, 1.0),
            r(0.7, 0.5, 0.8, 3.0),
            r(0.7, 0.6, 0.7, 0.5),
        ]
        self.assertEqual(compare.pick_hysteresis(table)[0], (0.7, 0.5))
        over = [
            r(0.5, 0.3, 0.9, 4.0),
            r(0.6, 0.4, 0.5, 3.5),
            r(0.6, 0.5, 0.4, 3.5),
        ]
        op, rule = compare.pick_hysteresis(over)
        self.assertEqual(op, (0.6, 0.5))
        self.assertIn("誤検出率が最小", rule)


# --- CNN（T7・T7b） ---


def cnn_data(seed: int = 0, n_sessions: int = 4, n: int = 40) -> list:
    rng = np.random.default_rng(seed)
    out = []
    for k in range(n_sessions):
        t = 100.0 + 0.25 * np.arange(n)
        labels = np.full(n, NEGATIVE, dtype=np.int8)
        labels[8:12] = POSITIVE
        labels[6:8] = UNUSED
        X = rng.normal(0.0, 1.0, (n, 98, 40)).astype(np.float32)
        X[labels == POSITIVE, :, :10] += 1.5
        name = f"20261011-10{k:02d}00_self_water"
        f = features.WindowFeatures(
            session=name, t_start_s=t, X=X, valid=np.ones(n, bool)
        )
        out.append(
            SessionData(
                name=name,
                feats=f,
                swallows=[102.0],
                t0_s=100.0,
                duration_s=0.25 * n + 1.0,
                labels=labels,
            )
        )
    return out


def assert_model_equal(tc, a, b):
    tc.assertEqual(a.mean.tobytes(), b.mean.tobytes())
    tc.assertEqual(a.std.tobytes(), b.std.tobytes())
    tc.assertEqual(a.pos_weight, b.pos_weight)
    tc.assertEqual(sorted(a.params), sorted(b.params))
    for k in a.params:
        tc.assertEqual(a.params[k].tobytes(), b.params[k].tobytes(), k)


class CnnTest(unittest.TestCase):
    def test_t7_numpy_forward_matches_torch(self):
        torch = compare._torch_setup(5)
        net = compare._make_net(torch)
        x = (
            np.random.default_rng(1)
            .normal(0, 1, (6, 98, 40))
            .astype(np.float32)
        )
        with torch.no_grad():
            ref = torch.sigmoid(net(torch.from_numpy(x))).numpy()
        params = {k: v.numpy() for k, v in net.state_dict().items()}
        np.testing.assert_allclose(
            features_v2.cnn_forward(params, x), ref, atol=1e-5
        )
        n = sum(p.numel() for p in net.parameters())
        self.assertEqual(n, features_v2.cnn_param_count())
        self.assertEqual(n, 5921)

    def test_t7_training_is_deterministic(self):
        data = cnn_data()
        a = compare.cnn_fit(data, "t7")
        b = compare.cnn_fit(data, "t7")
        assert_model_equal(self, a, b)
        c = compare.cnn_fit(data, "t7", seed=1)
        self.assertNotEqual(
            a.params["conv1.weight"].tobytes(),
            c.params["conv1.weight"].tobytes(),
        )
        # 陰性 34 ÷ 陽性 4（「使わない」2 窓は数えない）
        self.assertEqual(a.pos_weight, (4 * 34) / (4 * 4))

    def test_t7b_validation_side_does_not_leak(self):
        data = cnn_data()
        _, _, models = compare.cnn_cross_validate(data)
        changed = cnn_data()
        d0 = changed[0]
        X = d0.feats.X.copy() * 10.0 + 3.0
        labels = np.full(len(d0.labels), POSITIVE, dtype=np.int8)
        changed[0] = SessionData(
            name=d0.name,
            feats=features.WindowFeatures(
                session=d0.name,
                t_start_s=d0.feats.t_start_s,
                X=X,
                valid=d0.feats.valid,
            ),
            swallows=d0.swallows,
            t0_s=d0.t0_s,
            duration_s=d0.duration_s,
            labels=labels,
        )
        folds2, _, models2 = compare.cnn_cross_validate(changed)
        # fold 0 の検証側 = セッション 0
        self.assertEqual(folds2[0].val_sessions, (d0.name,))
        self.assertNotIn(d0.name, models2[0].sessions)
        assert_model_equal(self, models[0], models2[0])
        # 他の fold は学習側に入るので変わる
        self.assertNotEqual(models[1].mean.tobytes(), models2[1].mean.tobytes())

    def test_t7b_stops_without_positive_or_valid_windows(self):
        data = cnn_data()
        for d in data[1:]:
            d.labels[d.labels == POSITIVE] = NEGATIVE
        with self.assertRaises(SystemExit) as cm:
            compare.cnn_cross_validate(data)
        msg = str(cm.exception)
        self.assertIn("陽性と陰性の両方", msg)
        self.assertIn("検証側 = セッション 20261011-100000_self_water", msg)
        self.assertIn(data[1].name, msg)
        data = cnn_data()
        nv = [
            SessionData(
                name=d.name,
                feats=features.WindowFeatures(
                    session=d.name,
                    t_start_s=d.feats.t_start_s,
                    X=d.feats.X,
                    valid=np.zeros(len(d.labels), bool),
                ),
                swallows=d.swallows,
                t0_s=d.t0_s,
                duration_s=d.duration_s,
                labels=d.labels,
            )
            for d in data
        ]
        with self.assertRaises(SystemExit) as cm:
            compare.cnn_cross_validate(nv)
        self.assertIn("有効な窓が1つもありません", str(cm.exception))

    def test_torch_is_imported_lazily(self):
        # torch は CNN の学習の関数の中でだけ読む（モジュールの先頭で読まない）
        here = Path(__file__).resolve().parent
        for name in ("compare.py", "features_v2.py", "postprocess.py"):
            src = (here / name).read_text(encoding="utf-8")
            head = src.split("\ndef ", 1)[0]
            self.assertNotIn("import torch", head, name)
        self.assertNotIn(
            "import torch",
            (here / "features_v2.py").read_text(encoding="utf-8"),
        )
        self.assertEqual(len(postprocess.HYST_GRID), 45)
        self.assertEqual(len(train_eval.THRESHOLDS), 19)


if __name__ == "__main__":
    unittest.main()
