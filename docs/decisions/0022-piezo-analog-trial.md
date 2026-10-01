# 0022 圧電ディスク DFR0052 の試用で、記録ファームウェアの `analog.csv` の読み方を固定する

日付: 2026-10-01　状態: 採用（試用。ブランチ `trial/piezo-dfr0052`。main に入れるかは試用の結果を見て人が決める。
main に入れるときにこの番号が埋まっていれば振り直す）

Issue #35。記録ファームウェア（`firmware/logger/`）に A0 の 2 kHz の読み取りと `0x03` ANALOG の送信を足し、`docs/data-schema.md` の
`analog.csv`（拡張点）の節と、フレーム形式の節に注記を足した。ファイル名・列の作り方（`t_ms, ch0, ch1, …`）・単位・フレーム形式（ヘッダ、
`0x03` = ANALOG、uint16 LE）・`tools/record.py` は変えていない。`docs/evaluation.md` の定義と合格線、検出器（`firmware/detector/`）、`analysis/` も
変えていない。この試用は M2 の判定基準の数字を出さず、#28（代替センサの判断）の材料を作るだけ。細部は人が決めた（10/1）。採否の経緯は Issue #35 にある。

## 決定

### ビルドフラグ

- 全体をビルドフラグ `LOGGER_ANALOG`（既定 0）で囲む。0 のとき `logger.ino` は今までと同じ動作で、META の文字列も今までと同じ
  （`analog_in.cpp` は空になり、スケッチ・グローバル変数のバイト数も変わらない）。試用は `-DBUILD_FLAGS="-DLOGGER_ANALOG=1"` の別のビルドディレクトリで作る。
- 理由: main に入れた場合に、圧電をつないでいない logger の記録へ浮いた A0 の値と `piezo` の `sensors` が入らないようにする。

### 読み取り（T2）

- `mbed::Ticker`（500 µs）の割り込みは `rtos::EventFlags` を立てるだけ。`osPriorityRealtime`・スタック 1024 B の専用スレッドがフラグを待ち、
  起きるたびに `analogRead(A0)` を 1 回読む（`analogReadResolution(12)`。12 bit の生値 0〜4095）。`analogRead` は `mbed::AnalogIn` のミューテックスを
  取るので、割り込みからは呼べない。Arduino / mbed の API は `firmware/logger/analog_in.cpp` の中だけで呼び、`analog_in.h` は C の型だけを出す（0001）。
- `ANALOG_N` = 20 サンプルで 1 つの塊を確定し、16 段のリングに入れる。`loop()` が取り出して 1 塊 = 1 フレームで送る。リングが満杯なら新しい塊を
  捨てて数える（上書きしない）。

### `analog.csv` の読み方（1 行 = 1 塊）

- `record.py` は 1 フレーム = 1 行で書くので、`analog.csv` は `t_ms, ch0, …, ch19` になる。**`chK` は塊の中の K 番目のサンプル（古い順）で、
  チャネルではない。** チャネルは A0 の 1 つだけ。`data-schema.md` の「`ch0..chN` は ADC 生値」の `ch` をチャネルと読む書き方とずれるので、
  `piezo` を持つセッションについてこの読み方を固定し、装置が META に `analog_n`（20）と `analog_channels`（1）を載せる。
- `t_ms` は、装置がその塊の最後のサンプルを読んだ直後に取った `millis()`（音声チャンクと同じく終端側の時刻。0013 と同じ流儀）。送る時刻ではない。
  K 番目のサンプルの時刻の推定値は `t_ms − 1000 × (analog_n − 1 − K) ÷ analog_hz` ms。`millis()` の 1 ms の刻みとスレッドの起床の遅れを含むので、
  それ以上の精度は保証しない。

### META

- `LOGGER_ANALOG=1` のときだけ、装置の META に次を載せる（`record.py` の浅い merge でトップレベルに入る）:
  `fw`・`imu_hz`・`audio_hz`（今までと同じ）、`analog_hz`（2000）、`analog_n`（20）、`analog_channels`（1）、
  `sample_rates`（`imu_hz` 104・`audio_hz` 16000・`analog_hz` 2000）、`sensors`（`imu`・`mic` を `record.py` の既定と同じ値で、と
  `{"id":"piezo","part":"DFR0052","iface":"A0-direct"}`）、欠けの計数 6 つ（下）。
- merge は浅いので、`sample_rates` と `sensors` は PC の既定を丸ごと置き換える。そのため既定の項目も同じ値ですべて書く。
  トップレベルの `analog_hz` は 0006 の「装置の申告値を正とする」に揃えたもの。`sample_rates.analog_hz` も同じ 2000 で、食い違わない。
- 配線は直列抵抗なしで A0 に直結し、`iface` は `A0-direct`。直列 10 kΩ を入れて録る場合は `A0-direct-10k` として区別する（この試用では使わない）。
- JSON は `logger.ino` の中で `snprintf` で組み立てる（512 B のバッファ。越えるなら送らない。計数がすべて uint32 の最大でも 498 B）。
- 送る時機: 起動時に 1 回 ＋ `loop()` で 1000 ms ごとに送り直す（`LOGGER_ANALOG=1` のときだけ）。`meta.json` には最後に受けた META の値が残る。
  logger に `M` への応答は足さない。記録の前に USB を挿し直す手順（0006 の追記）は変えない。

### 欠けの計数

- `analog_in.cpp` が起動からの累計を数え、META で申告する: `analog_ticks`（タイマ割り込みの回数）、`analog_reads`（`analogRead` の回数）、
  `analog_blocks`（確定した塊の数。リングに入れた数 ＋ 捨てた数）、`analog_dropped`（リングが満杯で捨てた塊）、`analog_lag_max_ms`
  （`loop()` が塊を取り出したときの `millis() − 塊の t_ms` の最大）、`analog_stats_t_ms`（上の値を写したときの `millis()`）。
- `rtos::EventFlags` は回数を持たない（2 回の set が 1 回にまとまる）ので、フラグの取りこぼしは `analog.csv` の `t_ms` の飛びにならない。
  記録の最後で捨てた塊も飛びに出ない。**`analog.csv` の飛びと実効レートだけでは欠けを必ずは見つけられないので、欠けはこの計数で数える。**
- 照合: `analog_ticks − analog_reads` が 0〜1、`analog_dropped` = 0、`analog_reads − analog_blocks × analog_n` が 0〜19、
  `analog.csv` の `t_ms ≤ analog_stats_t_ms` の行数 − (`analog_blocks − analog_dropped`) が 0 または 1。最後の計数より後（約 1 秒）は
  「欠けなし」と言い切らない。

### データの扱い

- 試用のセッションは M1・M2 の学習・評価に使わない。
- **試用のセッションを main の checkout の `data/raw/`（M1 の `analysis/split.py` と M2 の `analysis/evaluate_detector.py` の入力）へ移さない・
  コピーしない。** `split.py` は `fw` が `detector` でないセッションを M1 の分割に入れるので、移すと混ざる。M1 の分割を回すときは、`split.py` が出す
  セッション名の一覧の各セッションについて `meta.json` の `sensors` を見て、`piezo` を持つセッションが無いことを確かめる（`split.py` の出力は
  セッション名だけで、`sensors` は載らない）。
- 被験者は `self` のみ。

### 変換

- `analog.csv` を持つ記録済みのセッションは無い（logger はこれまで `0x03` を送っていない）ので、変換は要らない。既存のファイル・列・単位・
  フレーム形式は変えていない。

## 理由

- T2 は周期をハードウェアのタイマが決め、`loop()` の I2C の待ち（1 回 1 ms 前後と推定）に引きずられない。`loop()` の中で `micros()` を見て読む案（T1）は、
  遅れた分をまとめて読むとサンプルの時刻が格子から数 ms ずれて固まり、`analog.csv` からそのずれが見えない。
- N = 20 は `t_ms` の飛びを 15 ms（1 周期の 1.5 倍）の細かさで見つけられ、1 回の送信が短い（50 B/フレーム、約 5.0 KB/s）。
- `record.py` を変えずに 1 チャネル・2 kHz を書くには、1 行 = 1 塊にするのが帯域と `t_ms` の扱いの点で無理が少ない（下の N = 1 の案を見る）。

## 選ばなかった案

- T1（`loop()` ＋ `micros()`）: 上の理由。
- T3（Ticker の割り込みで pico-sdk の `adc_read()` を直接呼ぶ）: Arduino の `analogRead` を使わない。T2 の段階 2 の確認で計数が基準を外れたときの次の手として残す。
- T4（ADC の自走 ＋ FIFO / DMA）: PDM（PIO ＋ DMA）との取り合いを確かめる手間が試用に見合わない。
- N = 50: 飛びを見つける細かさが粗い。
- N = 1（1 行 = 1 サンプル）: 2000 フレーム/s でヘッダだけで約 20 KB/s 増え、`t_ms` が 2 行ずつ同じ値になる。
- `record.py` で塊を行に展開する／`record.py` に引数を足す: Issue が `record.py` を変えないとしている。
- META をトップレベルの `analog_hz` と `sensors` だけにする: `meta.json` に `sample_rates.analog_hz` 0 と 2000 が並ぶ。

## 0002 との関係

- 0002 の代替候補 1 は「圧電素子＋高インピーダンスバッファ→ADC」。DFR0052 は基板のまま A0 に直結する試用で、バッファは入れていない。0002 は変えない。

## 確かめていないこと

- logger の 1 ループの時間、Ticker とスレッドの起床の遅れ・ばらつき（取りこぼしは計数で数えるが、取りこぼさなかったサンプルの時刻のずれは見えない）。
- `millis()` と Ticker が同じクロックから出ているか。RTOS の刻み、`analogRead` の実際の所要時間。
- 計数の起点（`setup()` の `analog_in_begin()`）と記録の開始のずれ。1000 ms ごとの META（1 回 約 0.5 KB）が IMU・音声の送信に与える影響。
- DFR0052 の出力回路（中点バイアスの有無、負の振れ、出力インピーダンス）と、直結した A0 に入力範囲を超える振れが来るかどうか（テスタ・オシロでは確かめない。人の決定）。
- 2 kHz の割り込みとスレッドが PDM のコールバックと `pdm_buf` の競合（0013「塞がっていない点」）を増やすか。
