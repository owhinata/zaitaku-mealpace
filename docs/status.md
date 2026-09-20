# 現在地

更新: 2026-09-19

## 段階

M0 準備（9/19〜9/21）

## 直近の状態

- リポジトリ初期化。文書・骨格・記録スクリプトを配置。
- GitHub に Milestone M0〜M6 とラベル3つを作成。
- M0 の Issue（#1〜#6）と M1 の Issue（#7〜#15）を起票。
- Codex のレビュールールと plan レビューのゲートを配置（docs/workflow.md、docs/decisions/0004）。
  承認は plan ファイルのハッシュと Codex 出力の PLAN-SHA 行に結びつく。hook は判定できないとき block する。
  hook は単体で動作確認済み（承認→通過、plan を1文字変えると block）。
- #1: 開発環境を導入し、`firmware/logger` のビルドが無修正で通った（arduino:mbed_nano 4.6.0、docs/log/2026-09-19.md）。
  Python 依存は `.venv` に入れ、`-DPYTHON3=` で渡す。
- #2: 実機への書き込みと10秒の記録が通った。META 1 / IMU 1045 / AUDIO 2475 フレーム、XOR 不一致 0、
  実効 105.5 Hz / 16006.5 Hz（docs/log/2026-09-19.md）。`record` ターゲットの引数の不具合を直し、
  `tools/record.py` に `--duration` とフレーム数の表示を足した。`docs/data-schema.md` の記載漏れ2点は
  docs/decisions/0006 に記録。音声チャンクの `t_ms` の食い違いは #15 に切り出した。
- Codex を実際に呼ぶ plan レビューを #2 で初めて通した（3回、計約41分、CONCERN 10件、BLOCKING 0）。
- #3: 生の音声波形の線引きを docs/decisions/0005 に記録し、AGENTS.md・docs/workflow.md・codex-review skill を直した。
  CLAUDE.md の該当行も差し替え済み。
- Edge Impulse プロジェクト未作成。
- ロボセンサー技研への問い合わせ未送付（代替センサ、docs/decisions/0002、#5）。

## 次にやること

M0 準備（期限 9/21）

- #4 Edge Impulse プロジェクトを作成し、設定を記録する（人）。
- #5 代替センサ（ロボセンサー技研）の入手性を問い合わせる（人、優先度最低）。
- #6 【判定】M0 準備（閉じるのは人）。

M1 ロギング・分岐点（期限 9/27）: #7〜#13 と #15（音声チャンクの `t_ms`）、判定は #14。

#6 の判定で見直すこと: plan レビューが CONCERN だけのときに何回まで回すか（#2 では3回とも CONCERN が出た）。

## 保留中の判断

- 一口ボタンの扱い（企画書 5章）。M2 検出器（10/11）までに決める。
