"""ei_upload.py を合成セッションと偽の送信で検証する（plan #21 第 7.7 節）。

実行: python -m unittest discover -s analysis -v
合成セッションは test_features.make_session で tempfile に作る（収集日1〜4 × 7 本 + p1 1 本）。data/ と実データは使わない。
送信は偽の関数に差し替え、ネットワークに出ない。m2_norm.json は一時フォルダに書く（analysis/ には書かない）。
"""
from __future__ import annotations
import contextlib, io, json, tempfile, threading, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import numpy as np

import ei_upload as eu
import features, train_eval
from test_features import TmpCase, make_session, HZ

DAYS = ("20260921", "20260922", "20260923", "20260924")
CONDS = ("quiet", "water", "talk", "cough", "neck", "saliva", "meal")   # 7 本 = 1 収集日
EVENTS = {"water": [("s", 1.0)], "saliva": [("s", 1.0)], "cough": [("c", 1.0)], "meal": [("s", 1.0), ("c", 2.0)]}
INVALID_SESSION = "20260921-100000_self_quiet"
KEY = "ei_test_key_0123456789abcdef"
ARGS_TRAIN = ["--bucket", "training", "--days", "20260921,20260922,20260923", "--validation-day", "20260923"]
ARGS_TEST = ["--bucket", "testing", "--days", "20260924"]


def session_name(day: str, k: int) -> str:
    return f"{day}-10{k:02d}00_self_{CONDS[k]}"   # HHMMSS が実在する時刻（session_iat が読む）


def write_events(d: Path, events) -> None:
    rows = "".join(f"{int(round(t * 1000))},{label},\n" for label, t in events)
    (d / "events.csv").write_text("t_ms,label,note\n" + rows, encoding="utf-8")


def build_root(root: Path, dur_s: dict[str, int], *, invalid: bool = True, p1: bool = True) -> Path:
    """収集日ごとに 7 本（cond は CONDS の順）。dur_s は収集日ごとの長さ（秒）。invalid なら収集日1 の quiet に IMU の飛びを入れる。"""
    root.mkdir(exist_ok=True)
    for day in DAYS:
        n = dur_s[day]
        for k, cond in enumerate(CONDS):
            name = session_name(day, k)
            drop = (lambda t_ms: (t_ms >= 1000) & (t_ms < 1300)) if (invalid and name == INVALID_SESSION) else None
            d = make_session(root, name, imu_dur_ms=n * 1000, n_samples=n * HZ, drop_imu=drop, meta={"cond": cond})
            write_events(d, EVENTS.get(cond, []))
    if p1:
        d = make_session(root, "20260920-090000_p1_water", imu_dur_ms=3000, n_samples=3 * HZ, meta={"subject": "p1"})
        write_events(d, [("s", 1.0)])
    return root


_TMP = tempfile.TemporaryDirectory()
ROOT = build_root(Path(_TMP.name) / "raw", {"20260921": 3, "20260922": 4, "20260923": 5, "20260924": 3})
unittest.addModuleCleanup(_TMP.cleanup)


class FakeSend:
    """呼び出しを記録し、statuses を先頭から返す。尽きたら (200, 'OK')。fail_names のファイル名は常に失敗する。

    --workers で複数のスレッドから呼ばれるので、記録と statuses の取り出しは Lock で守る。
    """
    def __init__(self, statuses=(), fail_names=()):
        self.calls = []
        self.statuses = list(statuses)
        self.fail_names = set(fail_names)
        self.lock = threading.Lock()

    def __call__(self, url, headers, files):
        with self.lock:
            self.calls.append((url, dict(headers), list(files)))
            if files[0][0] in self.fail_names:
                return 503, "unavailable"
            return self.statuses.pop(0) if self.statuses else (200, "OK")


def run(argv, *, send=None, statuses=(), env=None, norm_path=None, sleep=None):
    """ei_upload.run を標準出力ごと捕まえる。戻り値 (result, 出力, send)。"""
    send = send or FakeSend(statuses)
    sleeps = []
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        res = eu.run(argv, send=send, sleep=(sleep or sleeps.append), environ={} if env is None else env,
                     norm_path=norm_path)
    return res, out.getvalue(), send


def expected_uploads(root: Path, names) -> dict[str, dict]:
    """テスト側で独立に数えた、セッションごとの投入する窓（valid かつ UNUSED でない）。"""
    out = {}
    for name in names:
        s = eu.load_session(root, name, "training", "fit")
        up = s.upload_mask
        t_ms = [int(round(float(t) * 1000)) for t in s.feats.t_start_s]
        out[name] = {"n_upload": int(up.sum()), "n_invalid": int((~s.feats.valid).sum()),
                     "n_margin": int((s.feats.valid & (s.labels == eu.UNUSED)).sum()),
                     "excluded_t_ms": {t for t, u in zip(t_ms, up) if not u},
                     "labels": {t: eu.CLASS_NAMES[int(l)] for t, l, u in zip(t_ms, s.labels, up) if u}}
    return out


def table_rows(text: str) -> tuple[list[dict], dict]:
    cols = ("name", "day", "bucket", "role", "n_windows", "n_valid", "n_upload", "n_swallow", "n_cough", "n_other",
            "n_invalid", "n_margin")
    rows, total = [], None
    for line in text.splitlines():
        if line.startswith("| 2026"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            rows.append({k: (int(v) if k.startswith("n_") else v) for k, v in zip(cols, cells)})
        elif line.startswith("| 合計"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            total = {k: int(v) for k, v in zip(cols[4:], cells[4:])}
    return rows, total


def stats_sessions_in(text: str) -> list[str]:
    """一覧の「正規化の定数（stats_sessions）」の節に並ぶセッション名。"""
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line.startswith("## 正規化の定数")
        elif inside and line.startswith("  - "):
            out.append(line[4:].strip())
    return out


class LabelsTest(unittest.TestCase):
    T = 0.25 * np.arange(41)   # 窓の開始 0〜10 秒

    def at(self, labels, start: float) -> int:
        return int(labels[int(round(start / 0.25))])

    def test_cough_windows_and_margins(self):
        labels = eu.window_labels3(self.T, [], [3.0])
        # 中心 [3.0, 4.0] → 開始 2.5〜3.5 が cough。中心 [2.5, 3.0) と (4.0, 4.5] → 開始 2.0, 2.25, 3.75, 4.0 が境目
        self.assertEqual([self.at(labels, s) for s in (2.5, 2.75, 3.0, 3.25, 3.5)], [eu.COUGH] * 5)
        self.assertEqual([self.at(labels, s) for s in (2.0, 2.25, 3.75, 4.0)], [eu.UNUSED] * 4)
        self.assertEqual([self.at(labels, s) for s in (1.75, 4.25)], [eu.OTHER] * 2)
        self.assertEqual(int((labels == eu.COUGH).sum()), 5)
        self.assertEqual(int((labels == eu.UNUSED).sum()), 4)
        self.assertEqual(int((labels == eu.SWALLOW).sum()), 0)
        self.assertEqual(labels.dtype, np.int8)

    def test_swallow_rule_overrides_cough_rule(self):
        # s 3.0 と c 3.5。c の陽性（開始 3.0〜4.0）のうち 3.0〜3.5 は swallow、3.75・4.0 は s の境目（cough にならない）
        labels = eu.window_labels3(self.T, [3.0], [3.5])
        self.assertEqual([self.at(labels, s) for s in (2.5, 2.75, 3.0, 3.25, 3.5)], [eu.SWALLOW] * 5)
        self.assertEqual([self.at(labels, s) for s in (2.0, 2.25, 3.75, 4.0)], [eu.UNUSED] * 4)
        self.assertEqual([self.at(labels, s) for s in (4.25, 4.5)], [eu.UNUSED] * 2)   # c の境目 (4.5, 5.0]
        self.assertEqual(int((labels == eu.COUGH).sum()), 0)
        # c の陽性が s の境目に掛かる形: s 3.0、c 4.5 → c の陽性（開始 4.0〜5.0）のうち 4.0 は s の境目
        labels = eu.window_labels3(self.T, [3.0], [4.5])
        self.assertEqual(self.at(labels, 4.0), eu.UNUSED)
        self.assertEqual([self.at(labels, s) for s in (4.25, 4.5, 4.75, 5.0)], [eu.COUGH] * 4)

    def test_close_swallows_positive_beats_margin(self):
        # s 3.0 の境目 (4.0, 4.5]（開始 3.75・4.0）は s 4.2 の陽性 [4.2, 5.2]（開始 3.75〜4.5）に負ける
        labels = eu.window_labels3(self.T, [3.0, 4.2], [])
        self.assertEqual([self.at(labels, s) for s in (2.5, 3.0, 3.5, 3.75, 4.0, 4.25, 4.5)], [eu.SWALLOW] * 7)
        self.assertEqual([self.at(labels, s) for s in (2.0, 2.25, 4.75, 5.0)], [eu.UNUSED] * 4)
        self.assertEqual(int((labels == eu.SWALLOW).sum()), 9)    # 開始 2.5〜4.5

    def assert_matches_train_eval(self, swallows, coughs):
        got = eu.window_labels3(self.T, swallows, coughs)
        ref = train_eval.window_labels(self.T, swallows)
        np.testing.assert_array_equal(got == eu.SWALLOW, ref == train_eval.POSITIVE)
        if not coughs:
            np.testing.assert_array_equal(got == eu.UNUSED, ref == train_eval.UNUSED)
            np.testing.assert_array_equal(got == eu.OTHER, ref == train_eval.NEGATIVE)

    def test_no_cough_equals_train_eval_labels(self):
        rng = np.random.default_rng(0)
        for _ in range(20):
            self.assert_matches_train_eval(sorted(rng.uniform(0, 10, size=rng.integers(0, 6)).tolist()), [])

    def test_swallow_set_equals_train_eval_positive_with_cough(self):
        rng = np.random.default_rng(1)
        for _ in range(20):
            s = sorted(rng.uniform(0, 10, size=rng.integers(1, 6)).tolist())
            c = sorted(rng.uniform(0, 10, size=rng.integers(1, 6)).tolist())
            self.assert_matches_train_eval(s, c)

    def test_class_names(self):
        self.assertEqual(set(eu.CLASS_NAMES.values()), {"swallow", "cough", "other"})


class SplitAndArgsTest(TmpCase):
    def norm(self) -> Path:
        return self.root / "m2_norm.json"

    def test_training_rejects_day4(self):
        with self.assertRaises(SystemExit) as cm:
            run([str(ROOT), "--bucket", "training", "--days", "20260921,20260924", "--dry-run"], norm_path=self.norm())
        self.assertIn("20260924", str(cm.exception))

    def test_validation_day_must_be_in_days(self):
        with self.assertRaises(SystemExit):
            run([str(ROOT), "--bucket", "training", "--days", "20260921,20260922", "--validation-day", "20260923",
                 "--dry-run"], norm_path=self.norm())

    def test_unknown_day_and_bad_format(self):
        with self.assertRaises(SystemExit):
            run([str(ROOT), "--bucket", "training", "--days", "20260920", "--dry-run"], norm_path=self.norm())
        with self.assertRaises(SystemExit):
            run([str(ROOT), "--bucket", "training", "--days", "2026-09-21", "--dry-run"], norm_path=self.norm())

    def test_testing_accepts_only_day4(self):
        with self.assertRaises(SystemExit):
            run([str(ROOT), "--bucket", "testing", "--days", "20260923", "--dry-run"], norm_path=self.norm())
        with self.assertRaises(SystemExit):
            run([str(ROOT), "--bucket", "testing", "--days", "20260924", "--validation-day", "20260924", "--dry-run"],
                norm_path=self.norm())

    def test_split_must_be_3_plus_1_days(self):
        # 収集日1 が 6 本しかない root は止まる
        root = build_root(self.root / "raw", {d: 3 for d in DAYS}, invalid=False, p1=False)
        import shutil
        shutil.rmtree(root / session_name("20260921", 0))
        with self.assertRaises(SystemExit):
            run([str(root), *ARGS_TRAIN, "--dry-run"], norm_path=self.norm())

    def test_equal_day_counts_stop_training(self):
        root = build_root(self.root / "raw", {d: 3 for d in DAYS}, invalid=False, p1=False)
        with self.assertRaises(SystemExit) as cm:
            run([str(root), *ARGS_TRAIN, "--dry-run"], norm_path=self.norm())
        self.assertIn("同数", str(cm.exception))
        self.assertFalse(self.norm().exists())

    def test_api_key_required_unless_dry_run(self):
        with self.assertRaises(SystemExit) as cm:
            run([str(ROOT), *ARGS_TRAIN], norm_path=self.norm())
        self.assertIn(eu.API_KEY_ENV, str(cm.exception))
        _, out, send = run([str(ROOT), *ARGS_TRAIN, "--dry-run"], norm_path=self.norm())
        self.assertEqual(send.calls, [])
        with self.assertRaises(SystemExit):
            run(["--probe"], norm_path=self.norm())


class TrainingUploadTest(TmpCase):
    def norm(self) -> Path:
        return self.root / "m2_norm.json"

    def test_dry_run_lists_without_sending_or_writing(self):
        res, out, send = run([str(ROOT), *ARGS_TRAIN, "--dry-run"], env={eu.API_KEY_ENV: KEY}, norm_path=self.norm())
        self.assertEqual(send.calls, [])
        self.assertFalse(self.norm().exists())
        self.assertEqual((res.n_requests, res.n_items), (0, 0))
        self.assertNotIn(KEY, out)
        rows, total = table_rows(out)
        self.assertEqual(len(rows), 21)
        self.assertEqual({r["day"] for r in rows}, set(DAYS[:3]))
        self.assertEqual({r["role"] for r in rows if r["day"] == "20260923"}, {"validation"})
        self.assertEqual({r["role"] for r in rows if r["day"] != "20260923"}, {"fit"})
        self.assertEqual({r["bucket"] for r in rows}, {"training"})
        for k in total:
            self.assertEqual(total[k], sum(r[k] for r in rows), k)
        for r in rows:
            self.assertEqual(r["n_upload"], r["n_swallow"] + r["n_cough"] + r["n_other"])
            self.assertEqual(r["n_windows"], r["n_upload"] + r["n_invalid"] + r["n_margin"])
        # テスト側で独立に数えた投入数と一致する。invalid・境目の数も
        exp = expected_uploads(ROOT, [r["name"] for r in rows])
        for r in rows:
            self.assertEqual((r["n_upload"], r["n_invalid"], r["n_margin"]),
                             (exp[r["name"]]["n_upload"], exp[r["name"]]["n_invalid"], exp[r["name"]]["n_margin"]), r["name"])
        self.assertGreater(next(r["n_invalid"] for r in rows if r["name"] == INVALID_SESSION), 0)
        self.assertGreater(total["n_swallow"], 0)
        self.assertGreater(total["n_cough"], 0)
        # stats_sessions は fit（収集日1・2）だけ
        stats = stats_sessions_in(out)
        self.assertEqual(len(stats), 14)
        self.assertTrue(all(n[:8] in ("20260921", "20260922") for n in stats))
        self.assertIn("収集日ごとの投入数", out)
        self.assertIn("dry-run", out)
        self.assertNotIn("_p1_", out)

    def test_upload_one_request_per_item(self):
        res, out, send = run([str(ROOT), *ARGS_TRAIN], env={eu.API_KEY_ENV: KEY}, norm_path=self.norm())
        rows, total = table_rows(out)
        self.assertEqual(len(send.calls), total["n_upload"])
        self.assertEqual((res.n_requests, res.n_items, res.n_duplicates), (total["n_upload"], total["n_upload"], 0))
        self.assertNotIn(KEY, out)
        exp = expected_uploads(ROOT, [r["name"] for r in rows])
        seen = set()
        for url, headers, files in send.calls:
            self.assertEqual(url, "https://ingestion.edgeimpulse.com/api/training/data")
            self.assertEqual(headers["x-api-key"], KEY)
            self.assertEqual(headers["x-disallow-duplicates"], "1")
            self.assertIn(headers["x-label"], ("swallow", "cough", "other"))
            meta = json.loads(headers["x-metadata"])
            self.assertEqual(set(meta), {"session", "day", "t_ms", "subject", "feature_set"})
            self.assertEqual((meta["subject"], meta["feature_set"]), ("self", "m2-0020"))
            self.assertEqual(meta["day"], meta["session"][:8])
            self.assertEqual(len(files), 1)
            name, data = files[0]
            self.assertEqual(name, f"{meta['session']}_{meta['t_ms']}.json")
            self.assertEqual(headers["x-file-name"], name)   # EI が要求するヘッダ。multipart の filename と同じ
            t_ms = int(meta["t_ms"])
            self.assertNotIn(t_ms, exp[meta["session"]]["excluded_t_ms"])
            self.assertEqual(headers["x-label"], exp[meta["session"]]["labels"][t_ms])
            doc = json.loads(data)
            self.assertEqual(doc["protected"]["alg"], "none")
            self.assertEqual(doc["signature"], "0" * 64)
            self.assertEqual(doc["payload"]["interval_ms"], 1000)
            self.assertEqual([s["name"] for s in doc["payload"]["sensors"]], list(features.FEATURE_NAMES))
            self.assertEqual(len(doc["payload"]["values"]), 1)
            self.assertEqual(len(doc["payload"]["values"][0]), 29)
            self.assertTrue(all(np.isfinite(doc["payload"]["values"][0])))
            self.assertNotIn(name, seen)
            seen.add(name)
        # 投入する窓が全部送られた（除外は送られない）
        self.assertEqual(len(seen), sum(e["n_upload"] for e in exp.values()))
        # m2_norm.json は fit セッションだけから。validation の日は入らない
        self.assertTrue(self.norm().is_file())
        doc = json.loads(self.norm().read_text(encoding="utf-8"))
        self.assertEqual(doc["feature_set"], "m2-0020")
        self.assertEqual(doc["feature_names"], list(features.FEATURE_NAMES))
        self.assertEqual(len(doc["stats_sessions"]), 14)
        self.assertTrue(all(n[:8] in ("20260921", "20260922") for n in doc["stats_sessions"]))
        self.assertEqual((len(doc["mean"]), len(doc["std"])), (29, 29))
        self.assertTrue(all(s > 0 for s in doc["std"]))
        self.assertEqual(doc["n_windows"], sum(r["n_valid"] for r in rows if r["role"] == "fit"))
        self.assertIn("commit", doc)
        self.assertIn("EI の Studio の項目数と照合", out)
        # 送信の進み具合はセッションごとに 1 行
        self.assertEqual(sum(1 for l in out.splitlines() if l.startswith("送信: ")), 21)

    def test_norm_mismatch_stops_and_same_content_passes(self):
        run([str(ROOT), *ARGS_TRAIN], env={eu.API_KEY_ENV: KEY}, norm_path=self.norm())
        before = self.norm().read_bytes()
        # 同じ内容なら通り、書き換えない（commit の違いは照合しない）
        doc = json.loads(before)
        doc["commit"] = "other"
        self.norm().write_text(json.dumps(doc), encoding="utf-8")
        run([str(ROOT), *ARGS_TRAIN, "--dry-run"], env={eu.API_KEY_ENV: KEY}, norm_path=self.norm())
        self.assertEqual(self.norm().read_text(encoding="utf-8"), json.dumps(doc))
        # mean が違えば止まる
        doc["mean"][3] += 1.0
        self.norm().write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            run([str(ROOT), *ARGS_TRAIN, "--dry-run"], env={eu.API_KEY_ENV: KEY}, norm_path=self.norm())
        self.assertIn("内容が違います", str(cm.exception))
        # stats_sessions が違っても止まる（validation の日を変えた）
        self.norm().write_bytes(before)
        with self.assertRaises(SystemExit):
            run([str(ROOT), "--bucket", "training", "--days", "20260921,20260922,20260923", "--validation-day",
                 "20260922", "--dry-run"], env={eu.API_KEY_ENV: KEY}, norm_path=self.norm())

    def test_reuse_norm_one_day_with_existing_constants(self):
        """--reuse-norm: 収集日1 だけの投入が既存の定数（収集日1〜3 の 21 本）で通り、定数ファイルが変わらない。"""
        args3 = ["--bucket", "training", "--days", "20260921,20260922,20260923"]   # validation なし → 21 本
        run([str(ROOT), *args3], env={eu.API_KEY_ENV: KEY}, norm_path=self.norm())
        before = self.norm().read_bytes()
        doc = json.loads(before)
        self.assertEqual(len(doc["stats_sessions"]), 21)
        args1 = ["--bucket", "training", "--days", "20260921"]
        with mock.patch.object(features.Standardizer, "fit", side_effect=AssertionError("fit を呼んではいけない")):
            res, out, send = run([str(ROOT), *args1, "--reuse-norm"], env={eu.API_KEY_ENV: KEY}, norm_path=self.norm())
        self.assertEqual(self.norm().read_bytes(), before)
        self.assertNotIn("書いた:", out)
        rows, total = table_rows(out)
        self.assertEqual(len(rows), 7)
        self.assertEqual({r["day"] for r in rows}, {"20260921"})
        self.assertEqual({r["role"] for r in rows}, {"fit"})
        self.assertEqual({r["bucket"] for r in rows}, {"training"})
        self.assertIn("既存の定数を使用（stats_sessions 21 本）", out)
        self.assertEqual(stats_sessions_in(out), doc["stats_sessions"])
        self.assertEqual(len(send.calls), total["n_upload"])
        self.assertEqual((res.n_items, res.n_duplicates), (total["n_upload"], 0))
        self.assertTrue(all(url == "https://ingestion.edgeimpulse.com/api/training/data" for url, _, _ in send.calls))
        self.assertEqual({json.loads(h["x-metadata"])["day"] for _, h, _ in send.calls}, {"20260921"})
        # 正規化はファイルの定数で行われている: 同じ窓を手で正規化した値と一致する
        std = eu.standardizer_from_norm(doc)
        name = session_name("20260921", 1)
        s = eu.load_session(ROOT, name, "training", "fit")
        z = std.transform(s.feats.X)
        k = int(np.nonzero(s.upload_mask)[0][0])
        t_ms = int(round(float(s.feats.t_start_s[k]) * 1000))
        sent = next(f for _, _, f in send.calls if f[0][0] == f"{name}_{t_ms}.json")
        np.testing.assert_allclose(json.loads(sent[0][1])["payload"]["values"][0], z[k], rtol=1e-6)
        # --reuse-norm なしで 1 日だけなら従来どおり内容の不一致で止まる（定数ファイルはそのまま）
        with self.assertRaises(SystemExit) as cm:
            run([str(ROOT), *args1, "--dry-run"], env={eu.API_KEY_ENV: KEY}, norm_path=self.norm())
        self.assertIn("内容が違います", str(cm.exception))
        self.assertEqual(self.norm().read_bytes(), before)

    def test_reuse_norm_stops_on_bad_file_or_args(self):
        args1 = ["--bucket", "training", "--days", "20260921", "--reuse-norm", "--dry-run"]
        # ファイルが無い
        with self.assertRaises(SystemExit) as cm:
            run([str(ROOT), *args1], env={eu.API_KEY_ENV: KEY}, norm_path=self.norm())
        self.assertIn("m2_norm.json", str(cm.exception))
        # stats_sessions が 14 本（validation を除いて作ったもの）では止まる
        run([str(ROOT), *ARGS_TRAIN], env={eu.API_KEY_ENV: KEY}, norm_path=self.norm())
        before = self.norm().read_bytes()
        self.assertEqual(len(json.loads(before)["stats_sessions"]), 14)
        with self.assertRaises(SystemExit) as cm:
            run([str(ROOT), *args1], env={eu.API_KEY_ENV: KEY}, norm_path=self.norm())
        self.assertIn("21 本", str(cm.exception))
        self.assertEqual(self.norm().read_bytes(), before)
        # feature_set が違えば止まる（read_norm）
        doc = json.loads(before)
        doc["stats_sessions"] = [session_name(d, k) for d in DAYS[:3] for k in range(7)]
        doc["feature_set"] = "m2-other"
        self.norm().write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            run([str(ROOT), *args1], env={eu.API_KEY_ENV: KEY}, norm_path=self.norm())
        self.assertIn("feature_set", str(cm.exception))
        # --bucket testing / --validation-day とは一緒に使えない
        doc["feature_set"] = eu.FEATURE_SET
        self.norm().write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaises(SystemExit):
            run([str(ROOT), *ARGS_TEST, "--reuse-norm", "--dry-run"], env={eu.API_KEY_ENV: KEY}, norm_path=self.norm())
        with self.assertRaises(SystemExit):
            run([str(ROOT), "--bucket", "training", "--days", "20260921,20260922", "--validation-day", "20260922",
                 "--reuse-norm", "--dry-run"], env={eu.API_KEY_ENV: KEY}, norm_path=self.norm())

    def test_retry_then_success(self):
        statuses = [(500, "server error"), (0, "connection failed")]   # 最初の項目が 2 回失敗、3 回目に成功
        sleeps = []
        res, out, send = run([str(ROOT), *ARGS_TRAIN], statuses=statuses, env={eu.API_KEY_ENV: KEY},
                             norm_path=self.norm(), sleep=sleeps.append)
        _, total = table_rows(out)
        self.assertEqual(len(send.calls), total["n_upload"] + 2)
        self.assertEqual((res.n_requests, res.n_items), (total["n_upload"] + 2, total["n_upload"]))
        self.assertEqual(sleeps, [eu.RETRY_WAIT_S[0], eu.RETRY_WAIT_S[1]])
        self.assertEqual(send.calls[0][2], send.calls[1][2])   # 同じ項目を送り直す
        self.assertEqual(send.calls[1][2], send.calls[2][2])

    def test_four_failures_stop(self):
        statuses = [(500, "a"), (502, "b"), (0, "c"), (503, f"body with {KEY} inside")]
        with self.assertRaises(SystemExit) as cm:
            run([str(ROOT), *ARGS_TRAIN], statuses=statuses, env={eu.API_KEY_ENV: KEY}, norm_path=self.norm())
        msg = str(cm.exception)
        self.assertNotIn(KEY, msg)
        self.assertIn(session_name("20260921", 0), msg)
        self.assertIn("送った項目 0", msg)

    def test_duplicate_is_skipped_without_retry(self):
        sleeps = []
        res, out, send = run([str(ROOT), *ARGS_TRAIN], statuses=[(400, "Sample already exists (duplicate)")],
                             env={eu.API_KEY_ENV: KEY}, norm_path=self.norm(), sleep=sleeps.append)
        _, total = table_rows(out)
        self.assertEqual(len(send.calls), total["n_upload"])
        self.assertEqual((res.n_items, res.n_duplicates), (total["n_upload"] - 1, 1))
        self.assertEqual(sleeps, [])

    def test_workers_send_every_item_once_and_match_sequential(self):
        # 逐次と並列（--workers 4）で、偽の送信が全項目に 1 回ずつ呼ばれ、受け付け数・重複数・一覧が同じになる
        dup = [(400, "Sample already exists (duplicate)")]
        res1, out1, send1 = run([str(ROOT), *ARGS_TRAIN], statuses=dup, env={eu.API_KEY_ENV: KEY}, norm_path=self.norm())
        res4, out4, send4 = run([str(ROOT), *ARGS_TRAIN, "--workers", "4"], statuses=dup, env={eu.API_KEY_ENV: KEY},
                                norm_path=self.norm())
        rows1, total1 = table_rows(out1)
        rows4, total4 = table_rows(out4)
        self.assertEqual((rows4, total4), (rows1, total1))
        self.assertEqual(len(send4.calls), total4["n_upload"])
        self.assertEqual((res4.n_requests, res4.n_items, res4.n_duplicates), (res1.n_requests, res1.n_items, res1.n_duplicates))
        self.assertEqual((res4.n_items, res4.n_duplicates), (total4["n_upload"] - 1, 1))
        names1 = sorted(f[0][0] for _, _, f in send1.calls)
        names4 = sorted(f[0][0] for _, _, f in send4.calls)
        self.assertEqual(names4, names1)                       # 同じ項目の集合を 1 回ずつ
        self.assertEqual(len(set(names4)), len(names4))
        self.assertEqual({h["x-disallow-duplicates"] for _, h, _ in send4.calls}, {"1"})
        self.assertNotIn(KEY, out4)
        # 進み具合はセッションごとに 1 行、セッション名順のまま
        prog1 = [l for l in out1.splitlines() if l.startswith("送信: ")]
        prog4 = [l for l in out4.splitlines() if l.startswith("送信: ")]
        self.assertEqual(prog4, prog1)
        self.assertEqual(len(prog4), 21)
        # 一覧の本文（コマンドラインの行と、1 回目だけが m2_norm.json を書く行を除く）は同じ
        strip = lambda t: [l for l in t.splitlines() if not l.startswith(("- コマンドライン", "- 書いた: "))]
        self.assertEqual(strip(out4), strip(out1))

    def test_workers_four_failures_stop(self):
        # 収集日1 の 2 本目のセッションの真ん中の項目が常に失敗する → そのセッションで止まり、後のセッションは送らない
        name = session_name("20260921", 1)
        exp = expected_uploads(ROOT, [name])
        t_ms = sorted(exp[name]["labels"])[len(exp[name]["labels"]) // 2]
        send = FakeSend(fail_names=[f"{name}_{t_ms}.json"])
        sleeps = []
        with self.assertRaises(SystemExit) as cm:
            run([str(ROOT), *ARGS_TRAIN, "--workers", "4"], send=send, env={eu.API_KEY_ENV: KEY}, norm_path=self.norm(),
                sleep=sleeps.append)
        msg = str(cm.exception)
        self.assertIn(name, msg)
        self.assertIn("HTTP 503", msg)
        self.assertNotIn(KEY, msg)
        target = [c for c in send.calls if c[2][0][0] == f"{name}_{t_ms}.json"]
        self.assertEqual(len(target), eu.MAX_ATTEMPTS)
        self.assertEqual(len(sleeps), eu.MAX_ATTEMPTS - 1)
        # 受け付けられた数は偽の送信が 200 を返した回数と一致する（進行中の送信を待ってから数える）
        accepted = len(send.calls) - eu.MAX_ATTEMPTS
        self.assertIn(f"全体で受け付けられた項目 {accepted}、", msg)
        self.assertIn(f"リクエスト {len(send.calls)}。", msg)
        sessions = {json.loads(h["x-metadata"])["session"] for _, h, _ in send.calls}
        self.assertEqual(sessions, {session_name("20260921", 0), name})
        n_first = sum(1 for _, h, _ in send.calls if json.loads(h["x-metadata"])["session"] == session_name("20260921", 0))
        self.assertIn(f"このセッションで送った項目 {accepted - n_first} / {exp[name]['n_upload']}", msg)

    def test_workers_must_be_positive(self):
        with self.assertRaises(SystemExit):
            run([str(ROOT), *ARGS_TRAIN, "--dry-run", "--workers", "0"], env={eu.API_KEY_ENV: KEY}, norm_path=self.norm())


class TestingUploadTest(TmpCase):
    def norm(self) -> Path:
        return self.root / "m2_norm.json"

    def test_requires_norm_file(self):
        with self.assertRaises(SystemExit) as cm:
            run([str(ROOT), *ARGS_TEST, "--dry-run"], env={eu.API_KEY_ENV: KEY}, norm_path=self.norm())
        self.assertIn("m2_norm.json", str(cm.exception))

    def test_rejects_norm_with_day4_session(self):
        run([str(ROOT), *ARGS_TRAIN], env={eu.API_KEY_ENV: KEY}, norm_path=self.norm())
        doc = json.loads(self.norm().read_text(encoding="utf-8"))
        doc["stats_sessions"].append(session_name("20260924", 1))
        self.norm().write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            run([str(ROOT), *ARGS_TEST, "--dry-run"], env={eu.API_KEY_ENV: KEY}, norm_path=self.norm())
        self.assertIn("収集日4", str(cm.exception))

    def test_uses_norm_file_and_does_not_fit(self):
        run([str(ROOT), *ARGS_TRAIN], env={eu.API_KEY_ENV: KEY}, norm_path=self.norm())
        before = self.norm().read_bytes()
        with mock.patch.object(features.Standardizer, "fit", side_effect=AssertionError("fit を呼んではいけない")):
            res, out, send = run([str(ROOT), *ARGS_TEST], env={eu.API_KEY_ENV: KEY}, norm_path=self.norm())
        self.assertEqual(self.norm().read_bytes(), before)
        rows, total = table_rows(out)
        self.assertEqual(len(rows), 7)
        self.assertEqual({r["day"] for r in rows}, {"20260924"})
        self.assertEqual({r["role"] for r in rows}, {"test"})
        self.assertEqual({r["bucket"] for r in rows}, {"testing"})
        self.assertEqual(len(send.calls), total["n_upload"])
        self.assertTrue(all(url == "https://ingestion.edgeimpulse.com/api/testing/data" for url, _, _ in send.calls))
        for _, headers, _ in send.calls:
            self.assertEqual(json.loads(headers["x-metadata"])["day"], "20260924")
        # 一覧の stats_sessions はファイルのもの（収集日1・2 の 14 本）
        stats = stats_sessions_in(out)
        self.assertEqual(len(stats), 14)
        self.assertTrue(all(n[:8] != "20260924" for n in stats))
        # 正規化はファイルの定数で行われている: 同じ窓を手で正規化した値と一致する
        doc = json.loads(before)
        std = eu.standardizer_from_norm(doc)
        name = session_name("20260924", 1)
        s = eu.load_session(ROOT, name, "testing", "test")
        z = std.transform(s.feats.X)
        k = int(np.nonzero(s.upload_mask)[0][0])
        t_ms = int(round(float(s.feats.t_start_s[k]) * 1000))
        sent = next(f for _, _, f in send.calls if f[0][0] == f"{name}_{t_ms}.json")
        np.testing.assert_allclose(json.loads(sent[0][1])["payload"]["values"][0], z[k], rtol=1e-6)


class ProbeTest(TmpCase):
    def test_probe_items(self):
        items = eu.probe_items()
        self.assertEqual(len(items), 9)
        groups = {}
        for it in items:
            groups.setdefault((it.metadata["session"], it.metadata["day"]), []).append(it)
        self.assertEqual(sorted(groups), [("probe-a", "probe-1"), ("probe-b", "probe-2"), ("probe-c", "probe-3")])
        self.assertEqual([len(groups[k]) for k in sorted(groups)], [2, 3, 4])
        self.assertEqual(sorted(it.label for it in items), ["cough"] * 3 + ["other"] * 3 + ["swallow"] * 3)
        for k, its in groups.items():
            self.assertEqual([it.metadata["t_ms"] for it in its], [str(1000 * i) for i in range(len(its))])
            for it in its:
                self.assertEqual(it.filename, f"{it.metadata['session']}_{it.metadata['t_ms']}.json")
                self.assertEqual(set(it.metadata), {"session", "day", "t_ms", "subject", "feature_set"})
                self.assertNotEqual(it.metadata["subject"], "self")   # 実データではない
                doc = json.loads(it.data)
                self.assertEqual(len(doc["payload"]["values"][0]), 29)
        self.assertEqual(len({it.filename for it in items}), 9)
        # seed 固定で決定的。iat は固定値 + t_ms // 1000（実行時刻ではない）
        self.assertEqual([i.data for i in eu.probe_items()], [i.data for i in items])
        for it in items:
            self.assertEqual(json.loads(it.data)["protected"]["iat"], eu.PROBE_IAT_BASE + int(it.metadata["t_ms"]) // 1000)

    def test_probe_dry_run_and_send(self):
        _, out, send = run(["--probe", "--dry-run"], env={eu.API_KEY_ENV: KEY}, norm_path=self.root / "n.json")
        self.assertEqual(send.calls, [])
        self.assertIn("probe-a", out)
        self.assertNotIn(KEY, out)
        res, out, send = run(["--probe"], env={eu.API_KEY_ENV: KEY}, norm_path=self.root / "n.json")
        self.assertEqual(len(send.calls), 9)
        self.assertEqual((res.n_requests, res.n_items), (9, 9))
        self.assertTrue(all(url == "https://ingestion.edgeimpulse.com/api/training/data" for url, _, _ in send.calls))
        days = [json.loads(h["x-metadata"])["day"] for _, h, _ in send.calls]
        self.assertEqual(sorted(days), ["probe-1"] * 2 + ["probe-2"] * 3 + ["probe-3"] * 4)
        self.assertEqual({h["x-disallow-duplicates"] for _, h, _ in send.calls}, {"1"})
        self.assertTrue(all(h["x-file-name"] == f[0][0] for _, h, f in send.calls))
        self.assertNotIn(KEY, out)
        with self.assertRaises(SystemExit):
            run([str(ROOT), "--probe"], env={eu.API_KEY_ENV: KEY}, norm_path=self.root / "n.json")


class MultipartTest(unittest.TestCase):
    def test_multipart_body(self):
        ctype, body = eu._multipart([("a_0.json", b'{"x":1}')])
        boundary = ctype.split("boundary=")[1]
        self.assertTrue(ctype.startswith("multipart/form-data; boundary="))
        self.assertIn(f'--{boundary}\r\nContent-Disposition: form-data; name="data"; filename="a_0.json"\r\n'.encode(), body)
        self.assertIn(b'Content-Type: application/json\r\n\r\n{"x":1}\r\n', body)
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode()))

    def test_item_json_rejects_wrong_shape(self):
        with self.assertRaises(ValueError):
            eu.item_json(np.zeros(28), 0)
        with self.assertRaises(ValueError):
            eu.item_json(np.zeros((2, 29)), 0)

    def test_make_item_is_deterministic(self):
        """同じ項目を 2 回作っても data が byte 単位で一致する（再送を EI の重複検査で弾かせるため。iat に実行時刻を使わない）。"""
        name, t_ms = "20260921-104059_self_quiet", 2500
        values = np.arange(29, dtype=np.float32) / 7
        a = eu.make_item(name, "20260921", t_ms, "swallow", values)
        with mock.patch("time.time", side_effect=AssertionError("iat に time.time() を使わない")):
            b = eu.make_item(name, "20260921", t_ms, "swallow", values.copy())
        self.assertEqual(a.data, b.data)
        # iat = セッション名の先頭 20260921-104059 を JST（+09:00）で epoch 秒にしたもの + t_ms // 1000
        expected = int(datetime(2026, 9, 21, 10, 40, 59, tzinfo=timezone(timedelta(hours=9))).timestamp()) + 2
        self.assertEqual(json.loads(a.data)["protected"]["iat"], expected)
        self.assertEqual(eu.session_iat(name, t_ms), expected)
        # 項目ごとに違う（同じセッションの別の窓、別のセッションの同じ窓）
        self.assertNotEqual(eu.session_iat(name, 3500), expected)
        self.assertNotEqual(eu.session_iat("20260921-104413_self_water", t_ms), expected)
        with self.assertRaises(ValueError):
            eu.session_iat("not-a-session", 0)

    def test_redact(self):
        self.assertEqual(eu._redact("key=abc rest", "abc"), f"key=<{eu.API_KEY_ENV}> rest")
        self.assertEqual(eu._redact("x", ""), "x")


if __name__ == "__main__":
    unittest.main()
