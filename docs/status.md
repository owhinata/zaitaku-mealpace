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
- #7: 喉元に装着して60秒記録した（`self` / `quiet`）。IMU 6321 / AUDIO 14976 フレーム、XOR 不一致 0、
  `t_ms` の飛びは IMU・音声とも 0 箇所（取りこぼしの推定 0.000%）、実効 105.51 Hz / 15999.7 Hz
  （docs/log/2026-09-20.md）。ファームウェアは直していない。数えるために `tools/check_session.py` を足した
  （`imu.csv` と `audio_chunks.csv` の `t_ms` の飛びから取りこぼしを推定する。以降の記録でも使う）。
- #8: `self` の記録手順を `docs/recording-protocol.md` に固定した。1セッション 180 秒、1収集日 = 7セッション
  （`water` ×2、`saliva`、`talk`、`cough`、`neck`、`quiet`）。評価に回すのは最後の2収集日のセッション全部で、
  日数は先に固定しない。非嚥下の「50回」は `t`+`c`+`n` の合計50以上・各10以上・`quiet` 合計5分以上。
  手順を決める前のセッション（#2・#6・#7 の確認用）は M1 の学習・評価に使わない。
  plan レビューは1回（BLOCKING 0、CONCERN 4、採否は #8 のコメント）。
- #10: `analysis/evaluate.py` と `analysis/split.py` に、`docs/evaluation.md` との食い違いが9件あった
  （同じ陽性窓を検出と誤検出の両方に数える、重なった嚥下の区間を二重に引く、記録の端を切り詰めない、
  区間をまたぐ連続を2回と数える、`--eval-min 0` で全セッションが評価側になる、など）。実装のほうを直し、
  合成データのテスト 42 件で数え方を固定した（`python -m unittest discover -s analysis -v`）。
  定義が決めていない点の解釈（窓の中心で判定、連続は塊で1回、記録の範囲、回数の合算、イベント単位の混同行列）は
  docs/decisions/0011。`docs/evaluation.md` は変えていない。plan レビューは2回（初回 BLOCKING 2、再レビューで3面 LGTM）。
  コミット前の adversarial-review の指摘3件のうち、外れた `t_ms` による記録の長さの水増しと、シンボリックリンクの
  セッションは直した。`aggregate` が呼び出し側を信用する点は CONCERN とし、#13 で経路を1本にする。
  既知の弱点: 常に陽性を出す分類器でも M1 の合格線を通りうる。定義は動かさず、#13 の報告に常時陽性の場合の数字と
  陽性窓の割合を並べ、#14 で人が見る。
- #11: `analysis/features.py` を足した。窓（1.0 秒 / 0.25 秒）ごとに 29 次元（IMU 14: 振幅 4・RMS 6・ピーク数 1・
  主軸方向 3、音 15: MFCC 13・スペクトル重心 1・ゼロ交差率 1）。`docs/evaluation.md` は特徴量の名前だけを決めていて
  式が無いので、実データを見る前に式を docs/decisions/0012 に固定した。#13 の結果を見てから式を動かさない。
  IMU のキャリブレーションは前提にせず、加速度とジャイロの両方で窓内の平均を引く。依存は numpy だけ。特徴量は
  ファイルに保存しない。正規化（`Standardizer`）は渡されたセッションだけから統計量を作る。取りこぼしに掛かる窓は
  落とさず `valid = False` で返す（評価での扱いは #13 で決める）。合成セッションのテスト 34 件（全体で 76 件）が通る。
  60 秒のセッションで約 0.16 秒。plan レビューは3回（BLOCKING 0、CONCERN 7 → 3 → 0）。
  この定義は M1 の PC 側の評価用で、M2 の装置上の実装は保証しない。
- #9: `data/sample/20260920-000000_self_quiet/` に `self` / `quiet` の見本を置いた。音声 10.000 秒（2500 チャンク）と同じ時刻の
  範囲の IMU 1055 行。`sample_index` は 0 始まりに振り直し、`t_ms` は元のまま。`tools/check_session.py` は飛び 0、
  `analysis/features.py` は 36 窓 × 29 次元（#11 の見本での確認）。最初の見本はテレビの音が入っていたので捨て、
  録り直してから人が音声を聞いて確かめた。コミット前の adversarial-review は2回（1回目の指摘2件は CONCERN として
  `data/README.md` に明記、2回目は approve）。見本の `meta.json` には装置の申告値（`fw` など）が無い。基板をリセットせずに
  記録を始めると META フレームが届かないためで、#12 の記録でも起こりうる（扱いは docs/decisions/0006）。
- #15: 音声チャンクの `t_ms` の意味を、文書を実装に合わせる形でそろえた（docs/decisions/0013）。`t_ms` は装置が
  チャンクを読み出した直後の `millis()` で、論理上は終端側の時刻。先頭サンプルの時刻は `t_ms − 1000 × サンプル数 ÷ audio_hz` ms。
  ファームウェアと記録済みのデータは変えていない（読み方だけが変わる）。`analysis/features.py` の換算と音声の飛びの判定を直し、
  テストは 81 件。叩きの実測（50 対）は、どちらの読み方でも ±10 ms 程度（中央値 −3.9 / −7.9 ms）で、4 ms を切り分ける
  分解能は無い。送信中のバッファの上書きの競合は塞がっていない点として 0013 に記録した。plan レビューは3回
  （BLOCKING 0、CONCERN 5 → 1 → 0）。
- #13（スクリプトまで）: 実データを見る前に、学習と評価のやり方を `analysis/train_eval.py` とテストで固定した
  （docs/decisions/0014）。学習のラベルは窓の中心が `[t, t + 1.0]` なら陽性・境目は使わない、モデルは
  `LogisticRegression(C=1.0, class_weight="balanced")` で探索なし、閾値は学習側の交差検証（収集日ごと）で誤検出 3.0 回/分以下の
  うち検出率が最大、正規化は学習側の `valid` な窓の全部。既定の実行は学習側だけで、`--final` のときだけ評価側を採点する。
  報告には、学習と評価のセッションの一覧、混同行列、常時陽性の場合の数字、陽性窓の割合を出す。`scikit-learn` を足した。
  テストは全体で 120 件。plan レビューは2回（BLOCKING 0、CONCERN 4 → 0）。コミット前の adversarial-review の指摘2件のうち、
  未コミットの変更の検出（`-dirty` の表示、`--final` は止まる）は直し、セッションの中のファイルのリンクやコピーは CONCERN として
  0014 に記録した。**実データでの実行はまだ**（#12 のデータがそろってから、0014 の手順で行う）。
- ロボセンサー技研への問い合わせ未送付（代替センサ、docs/decisions/0002、#5）。

## 次にやること

M1 ロギング・分岐点（期限 9/27）: #12（`self` の記録。`docs/recording-protocol.md` の手順で、人が行う）、#13 の実データでの実行と報告（docs/decisions/0014 の手順）、判定は #14。
関門のレビューは `--base gate-M0` で回す。

M0 からの持ち越し

- #5 代替センサ（ロボセンサー技研）の入手性を問い合わせる（人、優先度最低）。

## 保留中の判断

- 一口ボタンの扱い（企画書 5章）。M2 検出器（10/11）までに決める。
- IMU と音の結合の方式（docs/plan.md、docs/log/2026-09-20.md）。M2 検出器（10/11）までに決める。
- Edge Impulse プロジェクトの公開範囲。今は Private。M2 検出器（10/11）までに決める。
- `p1` の記録先（リポジトリごと暗号化ディスクに置くか、`p1` だけリポジトリ外を必須にするか）。M3 本人試用（10/12）の前に決める。
