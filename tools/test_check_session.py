"""check_session.py を合成データで検証する（#23、docs/decisions/0021）。

実行: python -m unittest discover -s tools -v
合成セッションは tempfile に作る。data/raw/ は使わない。C6 だけは data/sample/（self の 10 秒の見本）を読む。
"""
from __future__ import annotations
import contextlib, io, tempfile, unittest
from pathlib import Path

import check_session

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "sample" / "20260920-000000_self_quiet"
N_WINDOWS = 240
LAG_MS = 1030          # 送信時刻 − 窓の開始（合成の値）

# data/sample/20260920-000000_self_quiet に対する変更前（HEAD c9ae24a）の出力。1 行目のフォルダ名は除く
SAMPLE_EXPECTED = """[IMU]
  行数: 1055
  記録の長さ: 9.989 秒
  実効レート: 105.52 Hz（公称 104 Hz 比 101.5%）
  1周期（飛びでない t_ms 差分の平均）: 9.48 ms
  飛び: 0 箇所 / 失われた行数(推定): 0 / 取りこぼし率: 0.000%
  最大差分: 11 ms / t_ms の逆行: 0 箇所
[音声]
  チャンク数: 2500
  総サンプル数(推定): 160000（1チャンク 64 サンプル、公称 4.000 ms）
  飛び: 0 箇所 / 失われたチャンク数(推定): 0 / 取りこぼし率: 0.000%
  最大差分: 4 ms / t_ms の逆行: 0 箇所
  sample_index の差分が最頻値と違う箇所: 0 箇所
  実効サンプルレート: 16000.0 Hz（公称 16000 Hz 比 100.00%）

[判定] 取りこぼし率 1%未満を「基準内」とする（Issue #7 の作業上の基準。docs/evaluation.md の合格線ではない）
  IMU:   基準内（0.000%）
  音声:  基準内（0.000%）
  ※ XOR 不一致数はここでは分からない。tools/record.py の終了時の表示を見ること。"""


def windows(n: int = N_WINDOWS, start_ms: int = 1000, hop_ms: int = 250) -> list[int]:
    return [start_ms + hop_ms * i for i in range(n)]


def write_detect(d: Path, w: list[int], lag_ms: int = LAG_MS) -> None:
    lines = ["t_ms,window_t_ms,positive,prob,led"] + [f"{x + lag_ms},{x},1,0.95,2" for x in w]
    (d / "detect.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_feat(d: Path, w: list[int], lag_ms: int = LAG_MS + 2, n_dims: int = 5) -> None:
    header = ["t_ms", "window_t_ms"] + [f"f{i}" for i in range(n_dims)]
    lines = [",".join(header)] + [",".join([str(x + lag_ms), str(x)] + ["0.5"] * n_dims) for x in w]
    (d / "feat.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_main(d: Path) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            check_session.main([str(d)])
        except SystemExit as e:
            return (e.code if isinstance(e.code, int) else 1), out.getvalue()
    raise AssertionError("main() が sys.exit で終わらなかった")


class CheckSessionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)

    def test_c1_detect_and_feat_without_gaps(self):
        w = windows()
        write_detect(self.d, w)
        write_feat(self.d, w)
        r = check_session.analyze_session(self.d)
        self.assertIsNone(r["imu"]); self.assertIsNone(r["audio"])
        self.assertEqual(r["detect"]["n"], N_WINDOWS)
        self.assertEqual((len(r["detect"]["gaps"]), r["detect"]["lost"], r["detect"]["loss_rate"]), (0, 0, 0.0))
        self.assertEqual(r["feat"]["n_dims"], 5)
        self.assertEqual((r["feat"]["only_in_detect"], r["feat"]["only_in_feat"]), (0, 0))
        code, text = run_main(self.d)
        self.assertEqual(code, 0)
        self.assertIn("[DETECT]", text); self.assertIn("[FEAT]", text)
        self.assertIn("DETECT:基準内（0.000%）", text)
        self.assertIn("FEAT:  基準内（0.000%）", text)
        self.assertIn("実効レート（装置の送信時刻から。窓の開始の間隔）: 4.00 窓/秒", text)
        for word in ("positive", "陽性", "prob", "led"):
            self.assertNotIn(word, text)

    def test_c2_two_missing_windows(self):
        w = windows()
        del w[100:102]                       # 差分 750 ms、行数 238
        write_detect(self.d, w)
        r = check_session.analyze_session(self.d)["detect"]
        self.assertEqual(r["n"], 238)
        self.assertEqual(len(r["gaps"]), 1)
        self.assertEqual(r["lost"], 2)
        self.assertAlmostEqual(r["loss_rate"], 2 / N_WINDOWS, places=9)
        self.assertEqual(r["max_diff"], 750)

    def test_c3_backwards_window_and_lag_stats(self):
        w = windows()
        w[50], w[51] = w[51], w[50]          # window_t_ms が 1 箇所逆行
        (self.d / "detect.csv").write_text(
            "t_ms,window_t_ms,positive,prob,led\n"
            + "".join(f"{x + LAG_MS + (i % 3) * 10},{x},0,0.1,1\n" for i, x in enumerate(w)), encoding="utf-8")
        r = check_session.analyze_session(self.d)["detect"]
        self.assertEqual(r["backwards"], 1)
        self.assertEqual(r["t_backwards"], 1)
        self.assertEqual((r["lag_min"], r["lag_median"], r["lag_max"]), (LAG_MS, LAG_MS + 10, LAG_MS + 20))
        _, text = run_main(self.d)
        self.assertIn(f"送信時刻 − 窓の開始（t_ms − window_t_ms）: 最小 {LAG_MS} / 中央値 {LAG_MS + 10} / 最大 {LAG_MS + 20} ms", text)
        self.assertIn("window_t_ms の逆行: 1 箇所 / t_ms の逆行: 1 箇所", text)

    def test_c4_feat_missing_window_and_no_feat(self):
        w = windows()
        write_detect(self.d, w)
        write_feat(self.d, w[:120] + w[121:])
        r = check_session.analyze_session(self.d)["feat"]
        self.assertEqual((r["only_in_detect"], r["only_in_feat"]), (1, 0))
        _, text = run_main(self.d)
        self.assertIn("DETECT との window_t_ms の不一致: 1 窓", text)
        (self.d / "feat.csv").unlink()
        del w[100:110]                       # DETECT の取りこぼし 10/240 = 4.2% → 基準外
        write_detect(self.d, w)
        code, text = run_main(self.d)
        self.assertNotIn("[FEAT]", text)
        self.assertEqual(code, 1)

    def test_c5_missing_files(self):
        with self.assertRaises(SystemExit) as cm:
            check_session.analyze_session(self.d)
        self.assertNotEqual(cm.exception.code, 0)
        (self.d / "imu.csv").write_text("t_ms,ax,ay,az,gx,gy,gz\n1000,0,0,1,0,0,0\n1010,0,0,1,0,0,0\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            check_session.analyze_session(self.d)     # audio_chunks.csv が無い → 今までどおり終了

    @unittest.skipUnless(SAMPLE.is_dir(), "data/sample の見本が無い")
    def test_c6_sample_output_unchanged(self):
        lines = check_session.format_report(check_session.analyze_session(SAMPLE))
        self.assertEqual(lines[0], f"=== {SAMPLE} ===")
        self.assertEqual("\n".join(lines[1:]), SAMPLE_EXPECTED)
        self.assertTrue(check_session.all_within_limit(check_session.analyze_session(SAMPLE)))


if __name__ == "__main__":
    unittest.main()
