# firmware/indicator/ — 表示器（LED の目安）

もう 1 枚の Nano RP2040 Connect の基板の RGB LED で、嚥下の「目安」を介助者に示す（Issue #29、docs/decisions/0018 の追記）。
検出器（`firmware/detector/`）の基板の LED は喉に当てる面にあり、装着すると見えないため（0018 の切り替え条件 (a)、#25）、表示を別の基板に移した。
表示器は PC から USB シリアルで状態を受ける。PC（`tools/record.py --indicator`）は、検出器から受けた DETECT の `led` を 1 バイトずつそのまま送る。
規則（緑・黄の条件）は検出器が持ち、PC と表示器は計算しない。表示器が受けるのは状態の 1 バイトだけで、音声・特徴量・確率は受けない。
LED は表示だけで、ベッド・車椅子・ブザーなどの制御は付けない。LED の状態は評価に使わない。

## 表示の意味

表と条件は docs/decisions/0018（`firmware/detector/README.md`「表示の意味」と同じ）。表示器は検出器の表示を、PC を通る分だけ遅れて表示する。

| 表示 | 意味（介助者向け） |
|---|---|
| 緑 | 嚥下を確認した目安 |
| 黄 | 嚥下をまだ確認していない目安 |
| 消灯 | 動作していない |

- 消灯になるのは、起動直後（PC から最初の状態が届くまで）と、状態が 1.0 秒以上届かないとき（検出器が止まった、`record.py` が止まった、どちらかの USB が抜けた）。
  表示器の `millis()` で測る。
- 緑と黄は常時点灯。赤・点滅・明るさの段階・音は無い。

## 置き方

- 表示器は食卓の介助者側に置き、LED を介助者に向ける。本人の正面から見えにくい向きにする（企画書 10章「表示は介助者側に向ける」、0018「文言」）。
- 下の「装置に貼る表示」は表示器（のケースか台）の介助者側に貼る。カードは介助者に渡す。検出器の基板には貼らない（喉元で見えないため）。
- 表示器の USB ケーブルは PC につなぐ。検出器とは別のポートになる（下の「ポートの見分け方」）。

## 装置に貼る表示

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

表示・カード・文書・コードの名前の書き方は docs/decisions/0018「文言」の「書き方の決まり」に従う。

## 記録に残るもの・残らないもの

- `detect.csv` の `led` は検出器が自分の基板の LED に書いた後の状態（`docs/data-schema.md`）で、表示器が実際に何を表示したかではない。
  表示器が何を表示したか（届いたか、遅れ、書き込みを飛ばしたか）は検出器からも PC の記録からも分からない。
- `record.py` は転送した時刻を記録しない。送信は別のスレッドで行い、記録の読み取りとマーカーは表示器への書き込みを待たない。`--indicator` の有無で記録のファイルの形式は変わらない。
  終了時に「表示器への転送: 送った / 失敗 / 送らなかった / 置き換えた」を表示する。
- 記録を始める前に表示器のポートが開けなければ、`record.py` は記録を始めない。記録を始めた後に送れなくなっても（USB が抜けたなど）記録は止めない。

## ファイルと Arduino の閉じ込め（docs/decisions/0001）

| ファイル | 役割 | Arduino / ライブラリの API |
|---|---|---|
| `indicator.ino` | `setup()`（`led_out_begin()`）、`loop()`（受けたバイトを読み、目標を決めて `led_out_set`） | `Serial`、`millis` |
| `indicator_rx.h/.cpp` | 受けたバイト列から表示の目標を決める（0〜2 以外を捨てる、最後の有効なバイト、1.0 秒で消灯） | 無し（PC のテストでビルドする） |
| `led_out.h/.cpp` | リンク → `../detector/led_out.*`。基板の RGB LED（NINA 経由）への書き込み。黄 = 赤＋緑。変化したときだけ書く | `WiFiNINA.h`、`NinaPin` 版の `pinMode` / `digitalWrite`。このファイルだけ |
| `led_rule.h` | リンク → `../detector/led_rule.h`。状態の値（`LED_OFF` / `LED_YELLOW` / `LED_GREEN`）と `led_off_due`（1.0 秒） | 無し |
| `host/test_indicator.cpp`・`test_indicator.sh` | `indicator_rx` の PC 上のテスト | 無し（PC の g++） |

`led_out.cpp` と `led_rule.h` のコメントは検出器を主語にしている（検出器のファームウェアを変えないため直していない）。表示器では「DETECT の led」を「表示器が表示している状態」と読む。

## ビルドと書き込み

`CMakeLists.txt` の表示器の節のとおり。書き込むときは表示器の基板だけを挿すか、`/dev/serial/by-id/` のパスを `-DPORT` に渡す（検出器に書き込まないため）。

```
cmake -S . -B build-indicator -DSKETCH_NAME=indicator -DPORT=/dev/serial/by-id/<表示器> -DPYTHON3=$PWD/.venv/bin/python3
cmake --build build-indicator --target build && cmake --build build-indicator --target upload
```

- 遅れの計測のビルド: `-DBUILD_FLAGS="-DINDICATOR_PROFILE=1"`（受けた塊ごとに表示している状態を 1 バイト返す）。記録には本番のビルド（`BUILD_FLAGS` 空）を使う。
- PC 上のテスト: `bash firmware/indicator/host/test_indicator.sh`。

## ポートの見分け方

- 2 枚とも同じ製品なので、`/dev/ttyACM0` と `1` は挿した順で決まり、検出器を挿し直す（META のため）と番号が入れ替わることがある。
  `ls -l /dev/serial/by-id/` のパス（基板ごとに違う番号を含む）で指定する。どちらがどちらかは、1 枚ずつ挿して `ls -l /dev/serial/by-id/` を見て控える。
- 記録: 検出器の USB を挿し直してから `.venv/bin/python tools/record.py --port /dev/serial/by-id/<検出器> --indicator /dev/serial/by-id/<表示器> --cond water --duration 90`。
  表示器は挿し直さなくてよい（META を送らない）。
