# 現在地

更新: 2026-09-20

## 段階

M1 ロギング・分岐点（9/22〜9/27）。M0 準備の関門は 9/20 に通過（docs/decisions/0010、`gate-M0`）。

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
- #4: Edge Impulse のプロジェクト `zaitaku-mealpace` を作成（Private、One label per data item、
  Target は Raspberry Pi RP2040 / RAM 264 KB / ROM 16 MB、docs/log/2026-09-20.md）。データは未投入。
- #6: 関門のレビュー（Codex、base は初期コミット）を1回回した。公開情報の線引きへの指摘はなし。
  指摘は1件: `tools/record.py` の `--out` と `.gitignore` の `!data/sample/**` により、p1 の生データを
  コミットできる場所へ記録できる。対応として `--out` を廃止し、出力先を `data/raw/` に固定した
  （docs/decisions/0007）。リポジトリ外の cwd から3秒の記録で確認済み。`git add -f` と手での移動は塞いでいない。
  再レビュー1回目の指摘（`cond` に `../` を入れると `data/raw/` の外に記録できる）には、`cond` を英数字と
  ハイフンに限り、`Session` が出力先の直下にしか作らない形で対応した。
  再レビュー2回目（最後）の指摘は1件: `data/raw` をシンボリックリンクに差し替えると直下の検査を抜ける。
  今の `data/raw` は実ディレクトリで、違反は起きていない。人の判断で CONCERN とし、運用で注意する
  （docs/decisions/0007「塞がっていない経路」）。関門のレビューに BLOCKING は残っていない。
- #6: plan の再レビューは前回の Codex スレッドを resume する（docs/decisions/0008）。初回と関門は fresh。
  再レビューの回数の上限はなくした。止める条件は BLOCKING の有無と人の採否（docs/decisions/0009）。
- ロボセンサー技研への問い合わせ未送付（代替センサ、docs/decisions/0002、#5）。

## 次にやること

M1 ロギング・分岐点（期限 9/27）: #7〜#13 と #15（音声チャンクの `t_ms`）、判定は #14。
関門のレビューは `--base gate-M0` で回す。

M0 からの持ち越し

- #5 代替センサ（ロボセンサー技研）の入手性を問い合わせる（人、優先度最低）。

## 保留中の判断

- 一口ボタンの扱い（企画書 5章）。M2 検出器（10/11）までに決める。
- IMU と音の結合の方式（docs/plan.md、docs/log/2026-09-20.md）。M2 検出器（10/11）までに決める。
- Edge Impulse プロジェクトの公開範囲。今は Private。M2 検出器（10/11）までに決める。
- `p1` の記録先（リポジトリごと暗号化ディスクに置くか、`p1` だけリポジトリ外を必須にするか）。M3 本人試用（10/12）の前に決める。
