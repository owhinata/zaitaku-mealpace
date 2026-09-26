# firmware/detector/ — 検出器ファームウェアと LED（目安）

## 検出器ファームウェアと LED（目安）

Nano RP2040 Connect で動く M2 の検出器（Issue #24・#25）。IMU（104 Hz）と PDM マイク（16 kHz）を読み、0.25 秒ごとに
1.0 秒窓の特徴量 29 次元（docs/decisions/0020）を計算し、Edge Impulse の書き出し（`src/`、int8）で推論して、陽性・陰性の 2 値を出す。
結果は DETECT と FEAT のフレームで USB シリアルに送る（形式は docs/decisions/0021、`docs/data-schema.md`）。
生の音声波形は保存も送信もしない（docs/decisions/0005・0019・0020・0021。16 kHz の波形は取り込みの 2 面にしか無く、特徴量に変えた後に上書きされる）。

基板の RGB LED を緑・黄で点け、介助者に嚥下の「目安」を示す（docs/decisions/0018）。LED は表示だけで、ベッド・車椅子・ブザーなどの制御は付けない。
LED の状態は評価に使わない。

## 表示の意味

窓の結果が出るたび（0.25 秒ごと）に更新する。「陽性」は装置の 2 値の出力。表と条件は docs/decisions/0018 のとおり。

| 表示 | 意味（介助者向け） | 条件 |
|---|---|---|
| 緑 | 嚥下を確認した目安 | 現在の窓（開始 `w`）を含めて、開始が `[w − 1.75, w]` にある陽性の窓が1つ以上ある（窓の開始時刻の差で数える） |
| 黄 | 嚥下をまだ確認していない目安 | 装置が動いていて、緑の条件を満たさない |
| 消灯 | 動作していない | 起動から最初の窓の結果が出るまで。センサの初期化に失敗したとき。新しい窓の結果が 1.0 秒（4 ホップ分）以上出ないとき（窓の結果とは別に `millis()` で測る） |

- 緑は最初の陽性窓が出た直後に点ける。最後の陽性窓の開始から 2.0 秒（結果 8 回分）緑を保つ。陽性窓が続けば延びる
  （k 窓の連続なら `(k − 1) × 0.25 + 2.0` 秒）。
- 黄は時間が経っても色を変えない。赤・点滅・明るさの段階・音は無い。緑と黄は常時点灯。
- 起動失敗は `LED_BUILTIN`（D13、RGB とは別の LED）の 200 ms 点滅。RGB は消灯のまま。

## 装置に貼る表示

ケースの介助者側。本人の正面から見えにくい向きに貼る。

```
嚥下の目安（判定ではありません）
緑: 嚥下を確認した目安（約 2 秒）
黄: まだ確認していない目安
```

## 介助者に渡すカード

```
この光は嚥下の目安です。判定や指示ではありません。
緑: 喉の動きと音から嚥下らしいと見た目安。約 2 秒で黄に戻ります。
黄: まだ確認していない目安。時間が経っても色は変わりません。
消灯: 動いていません。
次の一口をいつにするかは、介助者が決めます。
咳や首の動きの直後に緑が点くことがあります。
誤嚥（気管に入ること）が起きたかどうかは、この装置では分かりません。
```

## 文言の決まり

表示・カード・文書・コードの名前の書き方（使う語と使わない語）は docs/decisions/0018「文言」の「書き方の決まり」に従う。
README は表示とカードの文面をそのまま載せる場所で、決まりの写しは置かない。

## LED の実装

| ファイル | 役割 | Arduino / ライブラリの API |
|---|---|---|
| `led_rule.h/.cpp` | 0018 の規則。`window_t_ms` と `positive` から 1（黄）/ 2（緑）を出す。状態は最後の陽性窓の開始 `w_p` とその有無だけ | 無し（PC のテストでビルドする） |
| `led_out.h/.cpp` | 基板の RGB LED（NINA-W102 経由）への書き込み。黄 = 赤＋緑。変化したピンだけ書く | `WiFiNINA.h`、`NinaPin` 版の `pinMode` / `digitalWrite`。このファイルだけ |
| `pipeline.cpp` | 窓の結果が出たら `led_rule_update` を呼び `r.led` に入れる | 無し |
| `detector.ino` | `setup()` の `led_out_begin()`（センサの前）、窓の結果ごとの `led_out_set`、1.0 秒の消灯のタイムアウト（`millis()`） | `millis`、`micros` |

- 規則は PC 側の `analysis/led_rule.py` と同じ式。両方のテスト（`host/test_detector.cpp` の `test_led_rule`、`analysis/test_led_rule.py`）は同じ入力の表を使う。
- 1.0 秒の消灯は `detector.ino` の `loop()` の先頭で、窓の結果とは独立に `millis()` で測る。
- `led_out_begin()` は最初の NINA への書き込みで WiFiNINA の初期化（NINA のリセットと約 760 ms の待ち）を起こす。PDM の取り込みが
  始まる前に済ませるため、`setup()` でセンサを始める前に呼ぶ。`WiFi.begin()` は呼ばない。
- `led_out_set` は書く前に NINA の ACK ピンを見て、準備ができていなければ書かずに戻る（状態を進めない。次の結果か次の `loop()` で書き直す）。
  書き込みを始めた後に NINA が応答を止めた場合は防げない。
- 極性は `led_out.cpp` の `LED_ON_LEVEL` / `LED_OFF_LEVEL` の 1 箇所（WiFiNINA が NINA 側の値を反転しているので `HIGH` = 点灯と読んでいる。実機で確かめる）。
- DETECT の `led` は 0 消灯 / 1 黄 / 2 緑（`docs/data-schema.md`）で、実際に表示している状態（`led_out_state()`）。行に出るのは通常 1 / 2
  （消灯の時点は行に残らない。書き込みを飛ばした窓は前の状態のままで、0 のこともある）。PC 側の再計算との不一致は
  `analysis/evaluate_detector.py` の参考値に行数として出る。LED は評価に使わない。
- 外付けに切り替える条件（docs/decisions/0018。どれか 1 つで、RP2040 の GPIO に直結した緑・黄の 2 灯へ。規則と文言は同じ）:
  (a) バンドやケースで隠れて、介助者側から見えない。
  (b) NINA への書き込みで `t_ms` の飛びが作業上の基準（1%）を超える、または 1 ホップの処理が 250 ms に収まらなくなる。
  (c) WiFiNINA を入れたビルドが通らない、または NINA のファームウェアの版で動かない。

## ビルドと書き込み

`CMakeLists.txt` の検出器の節のとおり。WiFiNINA（と依存の Arduino_SpiNINA）は `deps` で入る。

```
cmake --build build --target deps          # 初回、または WiFiNINA が無いとき
cmake -S . -B build-detector -DSKETCH_NAME=detector -DPORT=/dev/ttyACM0 -DPYTHON3=$PWD/.venv/bin/python3
cmake --build build-detector --target build && cmake --build build-detector --target upload
```

- 計測: `-DBUILD_FLAGS="-DDETECTOR_PROFILE=1"`（10 秒ごとに統計行。LED の段は `led_med` / `led_max`、`led_write_max`、`led_writes`、
  `led_skipped`、`led_state`、`led_begin_ms`。読み方は `detector.ino` の `prof_emit` のコメント）。
- 止めたときの消灯の確認: `-DBUILD_FLAGS="-DDETECTOR_PROFILE=1 -DDETECTOR_PROF_STOP_AFTER_MS=20000"`（20 秒後にスライスを取らなくなり、
  約 1 秒で消灯する。本番のビルドで定義すると `#error`）。
- FEAT を送らない: `-DBUILD_FLAGS="-DDETECTOR_SEND_FEAT=0"`。
- logger に戻す: `build`（`SKETCH_NAME=logger`）で build → upload。
- detector のビルドは同時に走らせない。

## 記録と確認

- 記録: USB を挿し直してから `.venv/bin/python tools/record.py --cond water --duration 90`（docs/decisions/0006）。
- 取りこぼしと送信の遅れ: `.venv/bin/python tools/check_session.py data/raw/<セッション>`。
- `positive` の再採点と `led` の不一致: `.venv/bin/python analysis/evaluate_detector.py data/raw --sessions <セッション> --scorer analysis/m2_scorer.py`
  （参考値の「`led` と docs/decisions/0018 の規則からの再計算の不一致の行数」）。
- PC 上のテスト: `bash firmware/detector/host/test_detector.sh`、`.venv/bin/python -m unittest discover -s analysis`。

## 確かめたこと・確かめていないこと

`docs/log/` の #24 の節（2026-09-26）と #25 の節（実機の確認の後に書く）を参照。#25 で実機で確かめるのは、極性、黄の見え方、
1 回の書き込みの時間、`t_ms` の飛びへの影響、止めたときの消灯、嚥下に対する緑の遅れ、咳・首の動きでの緑、バンドとケースで隠れるか。
