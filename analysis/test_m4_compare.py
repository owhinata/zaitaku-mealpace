"""m4_compare.py を合成データで検証する（plan #45 の 12 節）。

実行: python -m unittest discover -s analysis -v
合成の data/raw は一時フォルダに作る（meta.json と空のファイルだけ。特徴量は
差し替えた読み込みの関数が合成の SessionData を返す）。data/ とリポジトリの
docs/log/ は使わない。CNN の学習は偽の関数に差し替える（compare.cnn_fit の
sample_weight と weighted_loss だけは本物の torch で確かめる）。
"""

from __future__ import annotations
import contextlib, io, json, tempfile, unittest
from pathlib import Path
from unittest import mock

import numpy as np

import compare, features, features_np, fixed_eval, m4_compare as mc
import postprocess, split, train_eval
from train_eval import NEGATIVE, POSITIVE, Scored, SessionData

M3_21 = fixed_eval.M4_TRAIN_M3
MEAL_1 = "20261025-120000_self_meal"
LATE = "20261029-120000_self_meal"
EVAL_NAME = "20261101-120000_self_meal"
M1_NAME = "20260921-104059_self_quiet"
DET_NAME = "20261002-203031_self_meal"
P1_NAME = "20261011-120000_p1_water"
IFACE = split.THROAT_IFACE


def make_dir(root: Path, name: str, meta=None, files=None, raw_meta=None):
    d = root / name
    d.mkdir()
    if raw_meta is not None:
        (d / "meta.json").write_text(raw_meta, encoding="utf-8")
    else:
        m = {
            "subject": name.split("_")[1],
            "cond": name.rsplit("_", 1)[-1],
            "fw": "pc-throat",
            "sensors": [{"id": "throat", "iface": IFACE}],
        }
        m.update(meta or {})
        m = {k: v for k, v in m.items() if v is not None}
        (d / "meta.json").write_text(json.dumps(m), encoding="utf-8")
    if files is None:
        files = (
            split.THROAT_FILES
            if (meta or {}).get("fw", "pc-throat") == "pc-throat"
            else ()
        )
    for f in files:
        (d / f).write_bytes(b"")
    return d


def build_raw(base: Path, extra_meal=(MEAL_1,), late=True) -> Path:
    """M3 の 21 本・学習用 meal・締めの後・評価側の窓・M1・detector・p1・trial-throat。"""
    root = base / "raw"
    root.mkdir()
    for n in M3_21:
        make_dir(root, n)
    for n in extra_meal:
        make_dir(root, n)
    if late:
        make_dir(root, LATE)
        # 評価側の窓: meta.json を読むと止まる中身（開かないことを確かめる）
        make_dir(root, EVAL_NAME, raw_meta="{not json", files=("detect.csv",))
    make_dir(root, M1_NAME, meta={"fw": None}, files=("imu.csv", "audio.wav"))
    make_dir(root, DET_NAME, meta={"fw": "detector"}, files=("detect.csv",))
    make_dir(root, P1_NAME)
    (root / "trial-throat").mkdir()
    return root


class ListingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def test_names(self):
        root = build_raw(self.base)
        opened = []
        real = split._meta

        def spy(p):
            opened.append(p.name)
            return real(p)

        with mock.patch.object(split, "_meta", side_effect=spy):
            L = mc.list_train(root)
        self.assertEqual(L.train, sorted(M3_21 + (MEAL_1,)))
        self.assertEqual(L.after_cutoff, [LATE])
        self.assertEqual(L.eval_window, [EVAL_NAME])
        self.assertEqual(
            sorted(L.excluded), sorted([M1_NAME, DET_NAME, P1_NAME])
        )
        self.assertEqual(L.skipped, ["trial-throat"])
        self.assertNotIn(LATE, opened)
        self.assertNotIn(EVAL_NAME, opened)
        self.assertIn(MEAL_1, opened)

    def test_late_folders_are_not_checked(self):
        # 締めより後の名前は、検査に外れる中身でも開かないので止まらない
        root = build_raw(self.base, late=False)
        make_dir(
            root,
            LATE,
            meta={"subject": "p1", "fw": None},
            files=("throat.wav", "detect.csv"),
        )
        make_dir(root, EVAL_NAME, raw_meta="[]")
        L = mc.list_train(root)
        self.assertEqual(L.after_cutoff, [LATE])

    def test_stops_on_checks(self):
        cases = {
            "subject": dict(meta={"subject": "p1"}),
            "throat_wav": dict(meta={"fw": None}, files=("throat.wav",)),
            "detect_csv": dict(meta={"fw": None}, files=("detect.csv",)),
            "iface": dict(meta={"sensors": [{"iface": "other"}]}),
            "missing": dict(files=("throat.wav",)),
            "forbidden": dict(
                files=("throat.wav", "throat_chunks.csv", "imu.csv")
            ),
            "no_meta": dict(raw_meta=None, files=()),
            "bad_json": dict(raw_meta="{x"),
        }
        for k, kw in cases.items():
            with self.subTest(k):
                with tempfile.TemporaryDirectory() as d:
                    root = build_raw(Path(d), extra_meal=())
                    if k == "no_meta":
                        (root / MEAL_1).mkdir()
                    else:
                        make_dir(root, MEAL_1, **kw)
                    with self.assertRaises(SystemExit) as cm:
                        mc.list_train(root)
                    self.assertIn(MEAL_1, str(cm.exception))

    def test_stops_on_symlink_and_bad_name(self):
        root = build_raw(self.base)
        (root / "20261026-120000_self_meal").symlink_to(root / MEAL_1)
        with self.assertRaises(SystemExit):
            mc.list_train(root)
        (root / "20261026-120000_self_meal").unlink()
        (root / "20261026_self_meal").mkdir()
        with self.assertRaises(SystemExit):
            mc.list_train(root)

    def test_assert_m4_train_four_stops(self):
        root = build_raw(self.base)
        # (i) 締めより後の名前は list_train が採らないので、検査そのものを確かめる
        with self.assertRaises(SystemExit):
            fixed_eval.assert_m4_train(list(M3_21) + [LATE])
        # (ii) M4-EVAL の名前
        with mock.patch.object(fixed_eval, "M4_EXPECTED_EVAL", (MEAL_1,)):
            with self.assertRaises(SystemExit) as cm:
                mc.list_train(root)
            self.assertIn("M4-EVAL", str(cm.exception))
        # (iii) M3 の 21 本の欠け
        with tempfile.TemporaryDirectory() as d:
            r2 = build_raw(Path(d))
            for f in (r2 / M3_21[3]).iterdir():
                f.unlink()
            (r2 / M3_21[3]).rmdir()
            with self.assertRaises(SystemExit) as cm:
                mc.list_train(r2)
            self.assertIn(M3_21[3], str(cm.exception))
        # (iv) M4_EXPECTED_TRAIN と違う
        with mock.patch.object(fixed_eval, "M4_EXPECTED_TRAIN", M3_21):
            with self.assertRaises(SystemExit) as cm:
                mc.list_train(root)
            self.assertIn("M4-TRAIN", str(cm.exception))
        with mock.patch.object(
            fixed_eval, "M4_EXPECTED_TRAIN", M3_21 + (MEAL_1,)
        ):
            mc.list_train(root)


# --- 合成の SessionData と偽の CNN の学習 ---


def synth(name: str, seed: int, n: int = 60, dims=(2,)) -> tuple:
    """(CNN 用, LR 用) の SessionData。時刻・valid・ラベルは同じ。"""
    rng = np.random.default_rng(seed)
    t = 0.25 * np.arange(n)
    meal = mc.is_meal(name)
    sw = (
        [3.0, 7.0, 11.0]
        if (meal or "water" in name or "saliva" in name)
        else []
    )
    labels = train_eval.window_labels(t, sw)
    valid = rng.uniform(size=n) > 0.03
    near = np.zeros(n, bool)
    for s in sw:
        near |= (t + 0.5 >= s) & (t + 0.5 <= s + 1.0)
    noise = rng.uniform(size=n) < 0.08
    out = []
    for d in (dims, (52,)):
        X = rng.normal(0, 0.5, (n, *d)).astype(np.float32)
        X[near, 0] += 2.5
        X[noise, 0] += 2.0
        f = features.WindowFeatures(session=name, t_start_s=t, X=X, valid=valid)
        out.append(
            SessionData(
                name=name,
                feats=f,
                swallows=list(sw),
                t0_s=0.0,
                duration_s=n * 0.25 + 0.75,
                labels=labels,
            )
        )
    return tuple(out)


def synth_info(name: str, seed: int) -> mc.Info:
    rng = np.random.default_rng(seed + 1000)
    return mc.Info(
        cond=mc.cond_of(name),
        markers={
            "t": sorted(rng.uniform(0, 15, 2).tolist()),
            "c": [],
            "n": [5.0] if "neck" in name else [],
        },
        conv=mc.is_meal(name) and seed % 2 == 0,
    )


def make_loader(dims=(2,)):
    def loader(root, names, lr=True):
        cnn, lrd, info = [], [], {}
        for k, n in enumerate(names):
            a, b = synth(n, k, dims=dims)
            cnn.append(a)
            lrd.append(b)
            info[n] = synth_info(n, k)
        return cnn, lrd, info

    return loader


class FakeModel:
    def __init__(self, sessions, n_fit_rows, bump, weighted):
        self.sessions = sessions
        self.n_fit_rows = n_fit_rows
        self.bump = bump
        self.weighted = weighted


def fake_fit(calls: list):
    def fit(data, where, seed, sample_weight=None):
        calls.append(
            (seed, sample_weight is not None, tuple(d.name for d in data))
        )
        n = sum(mc.fit_rows(d) for d in data)
        hn = 0.0
        if sample_weight is not None:
            hn = float(sum((w != 1).sum() for w in sample_weight))
        return FakeModel(
            tuple(d.name for d in data),
            n,
            0.1 * seed
            + (0.3 if sample_weight is not None else 0.0)
            + 1e-4 * hn,
            sample_weight is not None,
        )

    return fit


def fake_score(m, d):
    z = 2.0 * d.feats.X[:, 0] - 2.0 + m.bump
    return Scored(data=d, prob=1.0 / (1.0 + np.exp(-z.astype(np.float64))))


def names_for(days: dict) -> list:
    """{日付: 本数} → 名前（cond は water・quiet・neck・meal の順に回す）。"""
    conds = ("water", "quiet", "neck", "saliva")
    out = []
    for day, k in days.items():
        for i in range(k):
            cond = "meal" if day >= "20261025" else conds[i % len(conds)]
            out.append(f"{day}-1{i:02d}000_self_{cond}")
    return sorted(out)


def data_for(names) -> list:
    return [synth(n, k)[0] for k, n in enumerate(names)]


class PlanTrainingsTest(unittest.TestCase):
    def run_cv(self, data):
        calls = []
        tr = mc.Trainer(fake_fit(calls), fake_score)
        for seed in mc.SEEDS:
            mc.cnn_cv(tr, data, seed, False)
        for seed in mc.SEEDS:
            mc.cnn_cv(tr, data, seed, True)
        return tr, calls

    def test_matches_actual_calls(self):
        for days, want in (
            ({"20261011": 4, "20261012": 3}, 2 * 3 + (4 + 3 + 2) * 3),
            (
                {"20261011": 3, "20261012": 2, "20261025": 1},
                3 * 3 + (3 + 3) * 3,
            ),
            (
                {
                    "20261011": 2,
                    "20261012": 2,
                    "20261025": 1,
                    "20261026": 1,
                    "20261027": 1,
                },
                5 * 3 + 15 * 3,
            ),
        ):
            with self.subTest(days=days):
                names = names_for(days)
                data = data_for(names)
                tr, calls = self.run_cv(data)
                p = mc.plan_trainings(names)
                self.assertEqual(set(tr.calls), p["all"])
                self.assertEqual(len(tr.calls), len(set(tr.calls)))
                self.assertEqual(len(calls), len(p["all"]))
                self.assertEqual(len(p["all"]), want)
                # 同じ鍵は 1 回だけ（入れ子の内側の使い回し）
                self.assertEqual(len(calls), len(set(calls)))

    def test_inner_reuse_key_is_the_removed_set(self):
        names = names_for({"20261011": 2, "20261012": 2, "20261025": 2})
        p = mc.plan_trainings(names)
        days = sorted({n[:8] for n in names})
        for seed in mc.SEEDS:
            inner = {k[2] for k in p["inner"] if k[0] == seed}
            # 外側 k・内側 j と 外側 j・内側 k は同じ「2 日を除いた」集合
            self.assertEqual(
                inner,
                {frozenset(n for n in names if n[:8] == d) for d in days},
            )


class NestedHardNegativeTest(unittest.TestCase):
    def test_outer_validation_never_used_for_its_hard_negative(self):
        names = names_for({"20261011": 3, "20261012": 2, "20261025": 2})
        data = data_for(names)
        for gname, val in mc.outer_groups(data):
            vn = {d.name for d in val}
            S = [d for d in data if d.name not in vn]
            for seed in mc.SEEDS:
                tr = mc.Trainer(fake_fit([]), fake_score)
                tr.hard(S, seed, "t")
                for fs in tr.fit_sets:
                    self.assertFalse(set(fs) & vn, (gname, fs))
                for _, n in tr.score_sets:
                    self.assertNotIn(n, vn)
                # 重み付きの学習は S だけ
                self.assertEqual(set(tr.fit_sets[-1]), {d.name for d in S})

    def test_hn_windows_are_negative_valid_and_not_meal(self):
        names = names_for({"20261011": 3, "20261012": 2, "20261025": 2})
        data = data_for(names)
        tr = mc.Trainer(fake_fit([]), fake_score)
        w, n = mc.hn_weights(tr, data, 0, "t")
        self.assertGreater(n, 0)
        total = 0
        for d, ww in zip(data, w):
            hit = ww != 1.0
            total += int(hit.sum())
            self.assertTrue(np.all(ww[hit] == mc.HN_WEIGHT))
            self.assertTrue(np.all(d.labels[hit] == NEGATIVE))
            self.assertTrue(np.all(d.feats.valid[hit]))
            if mc.is_meal(d.name):
                self.assertFalse(hit.any())
        self.assertEqual(total, n)

    def test_hn_windows_are_false_positive_runs_at_fixed_op(self):
        d = synth("20261011-100000_self_water", 3)[0]
        prob = np.full(len(d.labels), 0.1)
        prob[40:44] = (
            0.96  # 非嚥下（t_start 10.0〜10.75。嚥下 11.0 の区間に掛かる）
        )
        prob[50:53] = 0.9  # 入り 0.95 に届かない
        prob[5:8] = 0.97  # t_start 1.25〜1.75（中心 1.75〜2.25）: 非嚥下
        idx = mc.hn_windows(prob, d)
        m = postprocess.hysteresis(
            prob, d.feats.valid, 0.95, 0.85, d.feats.t_start_s
        )
        self.assertTrue(set(idx) <= set(np.flatnonzero(m)))
        self.assertTrue(set(idx) & {5, 6, 7})
        self.assertFalse(set(idx) & set(range(50, 53)))


class SampleWeightTest(unittest.TestCase):
    def test_weighted_loss_equals_current_loss(self):
        torch = compare._torch_setup(0)
        g = torch.Generator().manual_seed(3)
        logits = torch.randn(64, generator=g)
        y = (torch.rand(64, generator=g) > 0.7).float()
        pw = 4.25
        ref = torch.nn.BCEWithLogitsLoss(
            pos_weight=torch.tensor(pw, dtype=torch.float32)
        )(logits, y)
        got = compare.weighted_loss(torch, logits, y, pw, torch.ones(64))
        self.assertLess(abs(float(ref) - float(got)), 1e-6)

    def test_cnn_fit_with_ones_matches_and_bad_length_stops(self):
        from test_throat_compare import cnn_data

        data = cnn_data(n=24)
        a = compare.cnn_fit(data, "t")
        b = compare.cnn_fit(
            data, "t", sample_weight=[np.ones(len(d.labels)) for d in data]
        )
        self.assertEqual(a.pos_weight, b.pos_weight)
        # 損失は 1e-6 で一致（上のテスト）。20 エポックで丸めの差が数 1e-6 たまる
        for k in a.params:
            np.testing.assert_allclose(a.params[k], b.params[k], atol=1e-4)
        w = [np.ones(len(d.labels)) for d in data]
        w[1][:5] = 3.0
        c = compare.cnn_fit(data, "t", sample_weight=w)
        self.assertEqual(c.pos_weight, a.pos_weight)  # 重みでは数えない
        self.assertFalse(
            np.array_equal(a.params["fc.weight"], c.params["fc.weight"])
        )
        with self.assertRaises(ValueError):
            compare.cnn_fit(data, "t", sample_weight=w[:-1])


# --- 本番・予行の実行 ---


class RunCase(unittest.TestCase):
    tmp = None

    @classmethod
    def setUpClass(cls):
        if RunCase.tmp is None:
            tmp = tempfile.TemporaryDirectory()
            unittest.addModuleCleanup(tmp.cleanup)
            RunCase.tmp = Path(tmp.name)
            RunCase.root = build_raw(RunCase.tmp)
            RunCase.names = mc.list_train(RunCase.root).train
            RunCase.calls = []
            RunCase.trainer = mc.Trainer(fake_fit(RunCase.calls), fake_score)
            with production_ok(RunCase.names):
                RunCase.result = mc.run(
                    RunCase.root,
                    loader=make_loader(),
                    trainer=RunCase.trainer,
                )


@contextlib.contextmanager
def production_ok(names):
    with (
        mock.patch.object(fixed_eval, "M4_EXPECTED_TRAIN", tuple(names)),
        mock.patch.object(fixed_eval, "m4_lines_match", return_value=True),
        mock.patch.object(train_eval, "git_dirty", return_value=False),
        mock.patch.object(train_eval, "_git", return_value="abc1234\n"),
        contextlib.redirect_stderr(io.StringIO()),
    ):
        yield


class RunTest(RunCase):
    def test_rows_and_refs(self):
        r = self.result
        self.assertEqual(
            [(x.cls, x.post) for x in r.rows],
            [(c, p) for c in mc.CLS_ORDER for p in mc.POST_ORDER],
        )
        sizes = {"P0": 19, "P1": 45, "P2": 45, "P3": 135, "P4": 135}
        for x in r.rows:
            self.assertEqual(len(x.table), sizes[x.post])
        self.assertTrue(all(not x.candidate for x in r.ref_rows))
        self.assertEqual(r.ref_rows[0].op, mc.Op("P1", enter=0.95, exit=0.85))
        self.assertEqual(len(r.ref_rows), 5)
        self.assertIn(r.selected, r.rows)
        self.assertEqual(
            set(self.trainer.calls), mc.plan_trainings(self.names)["all"]
        )
        self.assertEqual(r.n_trainings, len(self.trainer.calls))

    def test_ensemble_rows_average_three_seeds(self):
        r = self.result
        tr = mc.Trainer(fake_fit([]), fake_score)
        cnn, _, _ = make_loader()(self.root, self.names)
        per = [mc.cnn_cv(tr, cnn, s, False)[1] for s in mc.SEEDS]
        for k, s in enumerate(r.scored["C4"]):
            want = (per[0][k].prob + per[1][k].prob + per[2][k].prob) / 3
            np.testing.assert_allclose(s.prob, want, rtol=0, atol=1e-12)
        self.assertEqual(
            [s.data.name for s in r.scored["C4"]],
            [s.data.name for s in r.scored["C2"]],
        )

    def test_breakdown_sums(self):
        r = self.result
        for key, row in (("R0", r.ref_rows[0]), ("selected", r.selected)):
            b = r.breakdown[key]
            self.assertEqual(
                sum(sum(v.values()) for v in b["main"].values()),
                row.metrics["false_positive_runs"],
            )

    def test_report_contents(self):
        with production_ok(self.names):
            text = mc.format_report(self.result)
        for n in self.names + [LATE, EVAL_NAME]:
            self.assertIn(n, text)
        for s in (
            "TP / FN / FP",
            "常時陽性の場合",
            "楽観側",
            "M4-CONFIG: ",
            "R0 構成 J のまま",
            "学習側の fold だけ",
            "abc1234",
            "#49",
        ):
            self.assertIn(s, text)
        for w in ("誤嚥", "aspiration", "risk score", "リスク", "判定"):
            self.assertNotIn(w, text)

    def test_folds_use_only_train_side(self):
        for f in self.result.folds:
            ss = set(f.standardizer_sessions)
            self.assertEqual(ss, set(f.train_sessions))
            self.assertFalse(ss & set(f.val_sessions))
        days = sorted({n[:8] for n in self.names})
        self.assertEqual(
            [f.name for f in self.result.folds], [f"収集日 {d}" for d in days]
        )


class SwitchTest(RunCase):
    def test_production_stops_before_reading(self):
        loader = mock.Mock(side_effect=AssertionError("読んだ"))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(fixed_eval.M4_EXPECTED_TRAIN)
            with self.assertRaises(SystemExit):
                mc.run(self.root, loader=loader)
            with (
                mock.patch.object(
                    fixed_eval, "M4_EXPECTED_TRAIN", tuple(self.names)
                ),
                mock.patch.object(train_eval, "git_dirty", return_value=False),
            ):
                with self.assertRaises(SystemExit) as cm:
                    mc.run(self.root, loader=loader)  # 実物の 0037 は一致しない
                self.assertIn("M4-", str(cm.exception))
            with (
                mock.patch.object(
                    fixed_eval, "M4_EXPECTED_TRAIN", tuple(self.names)
                ),
                mock.patch.object(
                    fixed_eval, "m4_lines_match", return_value=True
                ),
                mock.patch.object(train_eval, "git_dirty", return_value=True),
            ):
                with self.assertRaises(SystemExit) as cm:
                    mc.run(self.root, loader=loader)
                self.assertIn("未コミット", str(cm.exception))
        loader.assert_not_called()

    def test_main_stops_before_run(self):
        with mock.patch.object(mc, "run") as run, self.assertRaises(SystemExit):
            mc.main([str(self.root)])
        run.assert_not_called()
        with (
            mock.patch.object(mc, "smoke") as sm,
            self.assertRaises(SystemExit),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            mc.main([str(self.root), "--smoke", "--figures", str(self.tmp)])
        sm.assert_not_called()

    def smoke(self, **kw):
        calls = []
        with mock.patch.object(train_eval, "_git", return_value="abc1234\n"):
            r = mc.smoke(
                self.root, fake_fit(calls), fake_score, make_loader(), **kw
            )
            return r, calls, mc.format_smoke(r)

    def test_smoke_runs_three_trainings_and_no_numbers(self):
        r, calls, text = self.smoke()
        self.assertEqual(len(calls), 3)
        self.assertEqual([c[1] for c in calls], [False, False, True])
        first_val = set(r.outer[0][1])
        for c in calls:
            self.assertFalse(set(c[2]) & first_val)
        for w in mc.SMOKE_FORBIDDEN:
            self.assertNotIn(w, text)
        for n in self.names + [LATE, EVAL_NAME]:
            self.assertIn(n, text)
        self.assertIn("窓 1,000 あたり", text)
        self.assertIn("最大の常駐メモリ", text)
        self.assertIn("収集日", text)
        # 3 通りの見積もり（meal 1 本が 10/25 にあるので 1〜3 日とも作れる）
        self.assertEqual([s[0] for s in r.scenarios], [1, 2, 3])
        self.assertTrue(all(s[1] is not None for s in r.scenarios))
        self.assertEqual(
            r.plan_now["all"], len(mc.plan_trainings(self.names)["all"])
        )

    def test_smoke_stops_when_list_fixed(self):
        with mock.patch.object(
            fixed_eval, "M4_EXPECTED_TRAIN", tuple(self.names)
        ):
            with self.assertRaises(SystemExit):
                self.smoke()

    def test_main_smoke_writes_nothing(self):
        before = sorted(p.relative_to(self.tmp) for p in self.tmp.rglob("*"))
        buf = io.StringIO()
        with (
            mock.patch.object(mc, "load_all", make_loader()),
            mock.patch.object(compare, "cnn_fit", fake_fit([])),
            mock.patch.object(compare, "cnn_score", fake_score),
            mock.patch.object(train_eval, "_git", return_value="abc1234\n"),
            contextlib.redirect_stdout(buf),
        ):
            mc.main([str(self.root), "--smoke"])
        self.assertIn("予行", buf.getvalue())
        after = sorted(p.relative_to(self.tmp) for p in self.tmp.rglob("*"))
        self.assertEqual(before, after)

    def test_scenarios(self):
        three = [
            "20261025-1_self_meal",
            "20261025-2_self_meal",
            "20261027-1_self_meal",
        ]
        s = mc.scenario_names(list(M3_21) + three)
        self.assertEqual([x[1] for x in s], [None, [], None])
        s = mc.scenario_names(list(M3_21))
        self.assertEqual(
            [sorted({n[:8] for n in x[1]}) for x in s],
            [
                ["20261025"],
                ["20261025", "20261026"],
                ["20261025", "20261026", "20261027"],
            ],
        )
        self.assertTrue(all(len(x[1]) == 3 for x in s))


# --- 選び方（6 節） ---


def fake_row(cls, post, det, fp):
    return mc.M4Row(
        cls=cls,
        post=post,
        op=mc.Op(post, threshold=0.5)
        if post == "P0"
        else mc.Op(post, enter=0.7, exit=0.4),
        rule="",
        metrics={"detection_rate": det, "false_positives_per_min": fp},
        per_session={},
        positive_windows=0,
        windows=1,
        table=[],
    )


class PickTest(unittest.TestCase):
    def test_pick_row(self):
        rows = [
            fake_row("C3", "P0", 0.8, 0.5),
            fake_row("C2", "P4", 0.8, 1.0),
            fake_row("C2", "P2", 0.8, 0.9),
            fake_row("C1", "P1", 0.7, 0.1),
            fake_row("C5", "P1", 0.99, 1.01),
        ]
        best, rule = mc.pick_row(rows)
        self.assertIs(best, rows[2])  # 同率は分類器を先に比べ、次に後処理
        self.assertIn("検出率が最大", rule)
        over = [
            fake_row("C4", "P0", 0.9, 1.5),
            fake_row("C2", "P3", 0.5, 1.2),
            fake_row("C2", "P1", 0.4, 1.2),
        ]
        best, rule = mc.pick_row(over)
        self.assertIs(best, over[2])
        self.assertIn("誤検出率が最小", rule)
        ref = fake_row("C1", "P0", 1.0, 0.0)
        ref.candidate = False
        self.assertIs(mc.pick_row(rows + [ref])[0], rows[2])

    def test_pick_point(self):
        def m(det, fp):
            return {"detection_rate": det, "false_positives_per_min": fp}

        table = [
            (mc.Op("P3", enter=0.7, exit=0.5, cap_s=1.5), m(0.8, 0.9)),
            (mc.Op("P3", enter=0.7, exit=0.5, cap_s=2.5), m(0.8, 1.0)),
            (mc.Op("P3", enter=0.7, exit=0.4, cap_s=2.5), m(0.8, 0.2)),
            (mc.Op("P3", enter=0.6, exit=0.5, cap_s=2.5), m(0.8, 0.2)),
            (mc.Op("P3", enter=0.9, exit=0.8, cap_s=2.5), m(0.9, 1.01)),
        ]
        self.assertEqual(mc.pick_point(table)[0], table[1][0])
        over = [
            (mc.Op("P1", enter=0.5, exit=0.3), m(0.9, 2.0)),
            (mc.Op("P1", enter=0.6, exit=0.4), m(0.5, 1.5)),
            (mc.Op("P1", enter=0.6, exit=0.5), m(0.4, 1.5)),
        ]
        op, rule = mc.pick_point(over)
        self.assertEqual(op, over[2][0])
        self.assertIn("誤検出率が最小", rule)
        p0 = [
            (mc.Op("P0", threshold=0.4), m(0.8, 0.5)),
            (mc.Op("P0", threshold=0.6), m(0.8, 0.5)),
        ]
        self.assertEqual(mc.pick_point(p0)[0].threshold, 0.6)

    def test_grids_and_constants(self):
        self.assertEqual(len(mc.post_grid("P3")), 135)
        self.assertEqual(len(mc.post_grid("P4")), 135)
        self.assertEqual(mc.M4_SELECT_FP_PER_MIN, 1.0)
        self.assertEqual(mc.HN_WEIGHT, 3.0)
        self.assertEqual(mc.HN_OP, (0.95, 0.85))
        self.assertEqual(mc.SEEDS, (0, 1, 2))


# --- 誤検出の中身（7.1） ---


class BreakdownTest(unittest.TestCase):
    def info(self, cond="water", conv=False, **markers):
        mk = {lb: markers.get(lb, []) for lb in mc.MOTION_MARKERS}
        return mc.Info(cond=cond, markers=mk, conv=conv)

    def test_order_of_main_categories(self):
        sw = [10.0]
        i = self.info()
        # 区間 [9.5, 11.5]。中心 = 開始 + 0.5
        self.assertEqual(
            mc.classify_run([10.5, 10.75, 11.0, 11.25], sw, i)[0], "after"
        )
        self.assertEqual(
            mc.classify_run([8.5, 8.75, 9.0, 9.25], sw, i)[0], "before"
        )
        # 前後の両方に出る塊は「直後の延び」が先
        self.assertEqual(
            mc.classify_run(
                [8.75, 9.0, 9.25, 9.5, 10.0, 10.25, 10.5, 10.75, 11.0, 11.25],
                sw,
                i,
            )[0],
            "after",
        )
        far = [20.0, 20.25]
        self.assertEqual(
            mc.classify_run(far, sw, self.info(t=[22.0]))[0], "motion"
        )
        self.assertEqual(
            mc.classify_run(far, sw, self.info(t=[22.3]))[0], "other"
        )
        self.assertEqual(
            mc.classify_run(far, sw, self.info(c=[19.0]))[0], "motion"
        )
        self.assertEqual(
            mc.classify_run(far, sw, self.info("meal", True))[0], "meal_conv"
        )
        self.assertEqual(
            mc.classify_run(far, sw, self.info("meal", False))[0], "meal_quiet"
        )
        # 動作のマーカーは meal より先
        self.assertEqual(
            mc.classify_run(far, sw, self.info("meal", True, c=[21.0]))[0],
            "motion",
        )
        # 複数の嚥下の区間に掛かる塊: どれか 1 つで当たればよい
        self.assertEqual(
            mc.classify_run([9.0, 9.25, 9.5, 9.75], [8.0, 10.0], i)[0], "after"
        )

    def test_sum_equals_false_positive_runs_and_sub_table(self):
        d = synth("20261011-100000_neck", 1, n=120)[0]
        d = SessionData(d.name, d.feats, [10.0], 0.0, d.duration_s, d.labels)
        prob = np.zeros(120)
        prob[42:48] = 0.9  # 10.5〜11.75: 直後の延び
        prob[80:82] = 0.9  # 20.0〜20.25: t と n がある
        prob[100:101] = 0.9  # 25.0: その他
        valid = np.ones(120, bool)
        d = SessionData(
            d.name,
            features.WindowFeatures(
                d.name, d.feats.t_start_s, d.feats.X, valid
            ),
            [10.0],
            0.0,
            d.duration_s,
            d.labels,
        )
        info = {d.name: self.info("neck", t=[21.0], n=[19.5])}
        op = mc.Op("P0", threshold=0.5)
        b = mc.fp_breakdown([Scored(d, prob)], op, info)
        self.assertEqual(b["total"], 3)
        self.assertEqual(b["main"]["after"], {"neck": 1})
        self.assertEqual(b["main"]["motion"], {"neck": 1})
        self.assertEqual(b["main"]["other"], {"neck": 1})
        self.assertEqual(b["sub"], {"t": 1, "c": 0, "n": 1})


# --- 最終モデル（9.1） ---


CFG_CNN1 = "set=mfe40 classifier=cnn seeds=0 hard_negative=none post=hyst threshold=- enter=0.70 exit=0.40 cap_s=2.0 refractory_s=0.5"
CFG_CNN3 = "set=mfe40 classifier=cnn seeds=0,1,2 hard_negative=w3.0 post=hyst threshold=- enter=0.95 exit=0.85 cap_s=none refractory_s=none"
CFG_LR = "set=a52 classifier=lr seeds=- hard_negative=none post=none threshold=0.50 enter=- exit=- cap_s=none refractory_s=none"


def cnn_fit_random(data, where, seed, sample_weight=None):
    rng = np.random.default_rng(
        seed + (100 if sample_weight is not None else 0)
    )
    return compare.CnnModel(
        mean=rng.normal(0, 0.1, 40),
        std=1.0 + rng.random(40),
        pos_weight=1.0,
        params=features_np.random_cnn(rng),
        sessions=tuple(d.name for d in data),
        n_fit_rows=sum(mc.fit_rows(d) for d in data),
    )


class FinalModelTest(RunCase):
    def final(self, config, out, **patches):
        with (
            production_ok(self.names),
            mock.patch.object(fixed_eval, "M4_CONFIG", config),
            mock.patch.object(
                fixed_eval, "m4_config_file_matches", return_value=True
            ),
        ):
            return mc.final_model(
                self.root,
                out,
                cnn_fit_random,
                compare.cnn_score,
                make_loader(dims=(98, 40)),
            )

    def test_stops(self):
        out = self.tmp / "m.npz"
        with production_ok(self.names):
            with self.assertRaises(SystemExit):
                mc.final_model(self.root, out)  # M4_CONFIG が None
            with mock.patch.object(fixed_eval, "M4_CONFIG", CFG_CNN1):
                with self.assertRaises(SystemExit):
                    mc.final_model(self.root, out)  # 0038 が無い・不一致
                with (
                    mock.patch.object(
                        fixed_eval, "m4_config_file_matches", return_value=True
                    ),
                    mock.patch.object(
                        train_eval, "git_dirty", return_value=True
                    ),
                ):
                    with self.assertRaises(SystemExit) as cm:
                        mc.final_model(self.root, out)
                    self.assertIn("未コミット", str(cm.exception))
        self.assertFalse(out.exists())

    def check_file(self, path, config):
        with np.load(path, allow_pickle=False) as z:
            a = {k: z[k] for k in z.files}
        cfg = mc.parse_config(config)
        want = mc._expected_keys(cfg)
        self.assertEqual(set(a), set(want))
        self.assertEqual(str(a["config"]), config)
        self.assertEqual(int(a["format"]), 1)
        for k, (shp, kind) in want.items():
            self.assertEqual(a[k].shape, shp, k)
        return a

    def test_cnn_one_and_three_seeds(self):
        for config, keys in (
            (CFG_CNN1, ["s0/"]),
            (CFG_CNN3, ["s0/", "s1/", "s2/"]),
        ):
            with (
                self.subTest(config=config),
                tempfile.TemporaryDirectory() as d,
            ):
                out = Path(d) / "m4.npz"
                lines = self.final(config, out)
                a = self.check_file(out, config)
                self.assertEqual(
                    sorted({k[:3] for k in a if k.startswith("s")}), keys
                )
                self.assertEqual(a["s0/conv1.weight"].dtype, np.float32)
                self.assertEqual(a["s0/mean"].dtype, np.float64)
                text = "\n".join(lines)
                self.assertIn(f"sha256={mc.sha256_of(out)}", text)
                self.assertIn("完全に一致", text)
                m = mc.load_model(out)
                X = (
                    np.random.default_rng(0)
                    .normal(0, 1, (5, 98, 40))
                    .astype(np.float32)
                )
                self.assertEqual(mc.model_prob(m, X).shape, (5,))
                with self.assertRaises(SystemExit):
                    self.final(config, out)  # 上書きしない

    def test_lr(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "m4.npz"
            self.final(CFG_LR, out)
            a = self.check_file(out, CFG_LR)
            self.assertEqual(a["coef"].dtype, np.float64)
            self.assertEqual(a["intercept"].shape, ())

    def test_lr_classes_reversed(self):
        class Clf:
            classes_ = np.array([POSITIVE, NEGATIVE])
            coef_ = np.array([np.linspace(-1, 1, 52)])
            intercept_ = np.array([0.3])

        class Std:
            mean = np.zeros(52)
            std = np.ones(52)

        m = train_eval.Model(standardizer=Std(), clf=Clf(), n_fit_rows=1)
        a = mc.model_arrays(CFG_LR, m)
        np.testing.assert_array_equal(a["coef"], -Clf.coef_[0])
        self.assertEqual(float(a["intercept"]), -0.3)
        X = np.random.default_rng(1).normal(0, 1, (4, 52))
        z = X @ Clf.coef_[0] + 0.3
        want = 1.0 - 1.0 / (1.0 + np.exp(-z))  # classes_[0]（陽性）の確率
        got = mc.model_prob({"cfg": mc.parse_config(CFG_LR), "arrays": a}, X)
        np.testing.assert_allclose(got, want, atol=1e-6)

    def test_load_model_stops_on_bad_file(self):
        rng = np.random.default_rng(0)
        m = cnn_fit_random([], "t", 0)
        good = mc.model_arrays(CFG_CNN1, {0: m})
        bad_cases = {
            "missing": {k: v for k, v in good.items() if k != "s0/fc.bias"},
            "extra": {**good, "s1/fc.bias": np.zeros(1, np.float32)},
            "shape": {**good, "s0/mean": np.zeros(39)},
            "dtype": {
                **good,
                "s0/conv1.weight": good["s0/conv1.weight"].astype(np.float64),
            },
            "format": {**good, "format": np.array(2)},
        }
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ok.npz"
            np.savez(p, **good)
            mc.load_model(p)
            for k, arrs in bad_cases.items():
                with self.subTest(k):
                    p = Path(d) / f"{k}.npz"
                    np.savez(p, **arrs)
                    with self.assertRaises(SystemExit):
                        mc.load_model(p)
        self.assertIsNotNone(rng)

    def test_config_roundtrip(self):
        for c in (CFG_CNN1, CFG_CNN3, CFG_LR):
            self.assertIsInstance(mc.parse_config(c), dict)
        row = fake_row("C5", "P4", 0.9, 0.5)
        row.op = mc.Op("P4", enter=0.75, exit=0.45, cap_s=2.0, refractory_s=0.5)
        s = mc.config_of(row)
        self.assertEqual(
            s,
            "set=mfe40 classifier=cnn seeds=0,1,2 hard_negative=w3.0 post=hyst threshold=- "
            "enter=0.75 exit=0.45 cap_s=2.0 refractory_s=0.5",
        )
        self.assertEqual(mc.parse_config(s)["seeds"], (0, 1, 2))
        row = fake_row("C1", "P0", 0.9, 0.5)
        self.assertEqual(mc.parse_config(mc.config_of(row))["threshold"], 0.5)
        for bad in (
            CFG_LR.replace("seeds=-", "seeds=0"),
            CFG_CNN1.replace("cap_s=2.0", "cap_s=1.6"),
            CFG_CNN1.replace("refractory_s=0.5", "refractory_s=1.0"),
            CFG_CNN1 + " x=1",
            CFG_LR.replace("threshold=0.50", "threshold=-"),
        ):
            with self.subTest(bad), self.assertRaises(ValueError):
                mc.parse_config(bad)


class ImportTest(unittest.TestCase):
    def test_model_functions_use_numpy_only(self):
        # load_model・model_prob の本体が numpy・features_np・postprocess だけを使う
        import inspect

        for fn in (mc.load_model, mc.model_prob, mc._check_arrays):
            src = inspect.getsource(fn)
            for mod in ("torch", "sklearn", "librosa", "scipy", "compare."):
                self.assertNotIn(mod, src, fn.__name__)


if __name__ == "__main__":
    unittest.main()
