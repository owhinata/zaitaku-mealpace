# 0035 UNO Q の Linux 側のコードの置き場所と配置、MCU との RPC の方式（#38）

日付: 2026-10-13　状態: 採用（10/13。書き込み・配置・LED の点灯・往復の遅れ・PD 電源の実測を下の「確かめたこと」に入れた）

plan #38（続き。PLAN-SHA `fa908f0da6355c58`、人が承認、Codex の plan レビューを 3 回通した）と、人の決定 D1〜D4（10/13）による。
docs/decisions/0025「0001 との関係」の「Linux 側の Python の配置の方法は M3 の Issue で決める」に答える。0025 の本文は変えない。

## 決定

1. **置き場所と配り方（D1）。** UNO Q の Linux 側で動かすこのプロジェクトのコードは、リポジトリの `device/unoq/` に置く。
   UNO Q には `device/unoq/DEPLOY_FILES` に書いたファイルだけを、CMake のターゲット `deploy-unoq`（rsync）で `~/zaitaku-mealpace-device/` に配る。
   - 一覧に絶対パス・`..`・`data/` を含む行があれば、`deploy-unoq` は送らずに止まる。テスト（`device/unoq/test_mcu_rpc.py`）・`__pycache__` は配らない。
   - `--delete` は使わない。配った後に UNO Q の上のディレクトリが一覧と一致するかを見る。消すときは `rm -r ~/zaitaku-mealpace-device`。
   - 常駐させない（systemd のユニットは作らない）。UNO Q の上の実行は `python3 -B`（`__pycache__` を作らない）。
   - M4 では同じ一覧に、PC と同じ推論のファイル（`analysis/` の側）を足す（0025「装置の推論と PC 側の解析・評価は同じ Python のコード」）。
2. **0028 決定 2 の読み。** 0028 決定 2 の「UNO Q 側にこのプロジェクトのプログラムは置かない」は、記録の経路（`arecord` / `pw-record` の標準出力 → ssh → PC。
   0028・0030）の決まりとして読む。検出器の側のプログラムは 0025 で UNO Q の Linux 側に置く前提で、この記録の 1 で置く。
   - 記録の経路は配ったものを使わない（`tools/record.py` は変えない）。
   - 記録の間は配ったプログラムを動かさない（0028 決定 3）。
   - CLAUDE.md の「UNO Q 側はプロジェクトのプログラムを置かず」（記録の経路の文の中）の文言は、今は直さない（D1b）。M4 で人が直す。
3. **配ったプログラムの扱い。** 配ったプログラムはファイルを書かない。この記録の時点で音声を扱うコードは無い（ALSA・PipeWire を開くコード、波形を扱うコードが
   無いことをテストで確かめる）。M4 で音声を扱うときは CLAUDE.md・0025 の決まり（メモリだけ、ディスク・ログ・標準出力・ネットワーク・RPC に波形を出さない）を当てる。
4. **RPC の方式（D2）。** Linux 側は `arduino-router`（systemd のサービス）の UNIX ソケット `/var/run/arduino-router.sock` に、素の MessagePack-RPC
   （rpclib と同じ形。要求 `[0, msgid, method, params]`、応答 `[1, msgid, error, result]`）で話す。
   - Linux 側で router のソケットと MessagePack を扱うのは `device/unoq/mcu_rpc.py` だけ。MessagePack の符号化・復号は使う型だけをこのモジュールに書く
     （UNO Q に pip と python3-msgpack が無い。パッケージを入れない）。App Lab の `arduino.app_utils` は使わない。
   - MCU 側で `Arduino_RouterBridge` を include するのは `firmware/unoq_mcu/rpc_server.cpp` だけ。`Bridge.provide` で名前を登録する
     （ハンドラはライブラリの別スレッドで動く。GPIO を 1 本書くだけなので `provide_safe` は使わない）。
   - 公開する名前は `zm_set_led(bool)` だけ。M4 で表示の状態を渡す名前を足す。音声は渡さない。
5. **ビルドと版（D3・D4）。** MCU 側のスケッチ `firmware/unoq_mcu/` は 0001 の A 案（CMake が arduino-cli を呼ぶ）でビルドし、`SKETCH_NAME=unoq_mcu` のとき
   FQBN は `arduino:zephyr:unoq`。版は UNO Q の上の版に合わせて固定する: コア `arduino:zephyr@1.0.0`、`Arduino_RouterBridge@0.4.3`（依存 `Arduino_RPClite` 0.3.1）。
   router は `arduino-router` 0.10.0（UNO Q の Debian パッケージ。こちらでは入れ替えない）。
6. **LED（D6）。** この Issue の LED は MCU 側の RGB LED（LED3）の緑 1 色で、点けて消すことの確かめだけ。表示の規則（0018 の緑・黄の「目安」、
   1.0 秒で消灯）は持たない。M4 の表示の場所と規則は M4 で決める。
7. **LED の変更（D6 を改める。人の指示 10/13）。** 点灯の確かめは MCU 側の 8×13 LED マトリクス（コア同梱の `Arduino_LED_Matrix` 0.1.3）に出す。
   `zm_set_led(true)` で #32 の「微笑む顔」（`firmware/indicator/shapes.h` の静止画 12×8 を `firmware/unoq_mcu/shapes_unoq.h` に 1 枚だけ写し、
   13 列の左寄せ、右端の 1 列は消灯）、`false` で全消灯。LED3 は使わない。流れる波線（動画）と表示の規則は M4。呼び出し名と Linux 側は変えない。
   下の「確かめたこと」の LED3 の点灯と往復の遅れは、変える前（LED3 の緑）のスケッチでの値。マトリクスのスケッチは書き込み待ち。

## 理由

- リポジトリに置き、一覧で配ると、UNO Q の上に載るものが一覧で決まる（`data/raw/` や余計な文書・コードが載らない）。PC と同じコードを M4 で配れる。
- 素の MessagePack-RPC は、UNO Q の上に依存を足さずに済み、App Lab のコンテナと版に縛られない。PC のテストで `msgpack` と突き合わせられる。

## 選ばなかった案

- (b) UNO Q に git clone（リポジトリ全体が載る。`data/raw/` は ignore で載らないが、余計な文書・コードが載る）。
- (c) App Lab のアプリとして置く（App Lab のコンテナと版に縛られ、CMake / リポジトリの外になる）。
- (d) 毎回 ssh で標準入力からスクリプトを流す（ファイルは残らないが、M4 の常駐の検出器には使えない）。

## 確かめたこと（10/13）

- UNO Q の上（読み取りだけ。R0）: Debian 13、Python 3.13.5、numpy・msgpack は無い。`arduino-router.service` が動き、ソケット
  `/var/run/arduino-router.sock` は `srw-rw-rw-`（`arduino` で開ける）。router の起動の行はシリアル `/dev/ttyHS1`、115200。
  基板の上の arduino-cli 1.5.1、arduino-app-cli 0.13.0、`~/.arduino15` のコア `arduino:zephyr` 1.0.0、ライブラリ `Arduino_RouterBridge` 0.4.3・`Arduino_RPClite` 0.3.1。
  App Lab の既定のアプリは無い（`No default app set`）。
- PC: コアと RouterBridge を上の版で入れ、`board listall` で FQBN `arduino:zephyr:unoq` を確かめた。空のスケッチと `firmware/unoq_mcu/` のビルドが通った。
  書き込みは `remoteocd`（`upload --adb-path ... -s <シリアル番号>`。USB の adb 越し）が既定で、network の `remoteocd_network` もある（コアの `platform.txt`）。
- 書き込み（A1・A3）: UNO Q の USB-C を PC に直につなぐと、`arduino-cli board list` に `/dev/ttyACM0`（serial、`configuration: ADB Configuration`）が出た。
  `cmake --build build-unoq --target upload`（remoteocd。UNO Q の `/tmp/remoteocd/` に書く物を置き、基板の上の openocd が SWD で書く）で書けた。
  スケッチ 89,280 B、所要 約 10 秒。消したのはスケッチの領域だけ（ローダは照合して同じなら書かない。ブートローダの書き込みはしていない）。
  `/tmp/remoteocd/` は tmpfs で、再起動で消える。
- 配置: `deploy-unoq`（rsync）で `~/zaitaku-mealpace-device/` に `led_check.py`・`mcu_rpc.py` の 2 つだけが載った。UNO Q に rsync は初め無く、
  人が `sudo apt install rsync`（3.5.0）で入れた（人の決定 10/13。D1 のとおり rsync で配る）。`python3 -B` の実行の後も `__pycache__` は無い。
- LED: MCU 側の LED3 の緑（PH11）が `led_check.py on` で点き、`off` で消えた（人が目で確かめた）。Arduino のピン表は旗 0、Zephyr のボード定義は
  ACTIVE_LOW で、`digitalWrite` の LOW で点く（`led_out.cpp` の見込みどおり）。
- `zm_set_led` の往復の遅れ（`led_check.py bench --n 100 --warmup 5`。送ってから応答まで。合否は付けない）:

  | 電源 | 件数 | 中央値 | 95 パーセンタイル | 最大 |
  |---|---|---|---|---|
  | USB-C を PC に直挿し | 100 | 5.103 ms | 5.239 ms | 5.377 ms |
  | PD（下の構成） | 100 | 5.090 ms | 5.216 ms | 5.341 ms |

- PD 電源: PD 対応モバイルバッテリー（5000 mAh）→ USB-C→3.5 mm 変換アダプタ（UGREEN CM721。DAC 搭載、PD 60 W パススルー）の PD の入口 → UNO Q。
  plan のハブの代わりに、このアダプタのパススルーを使う構成。起動し、ALSA の `hw:CARD=Audio` が見え（P1）、カーネルログに電圧・過電流・USB のリセットの行は無く
  （起動の途中の数え直しの 1 行だけ。`/sys/class/power_supply` は無く電圧は読めない。P3）、LED の RPC も動いた（P2b）。60 秒の記録（P2）は `throat_check.py` の
  使わない目安に当たらなかった（overrun 0、推定差 +4 ms、段差 0、0 の区間 0）。持ち時間（P4）は人が決めるまで保留。
- 書き込みの直後に router の journal に `decoding packet: invalid packet, expected array` が出る。RouterBridge が起動時にシリアルに書く文字列
  （前の回の壊れた RPC を流すためのもの）を router が読んだものと見ている（見立て）。その後の RPC は通った。

## 確かめていないこと

- PD のバッテリーの持ち時間（P4）。電源由来の雑音の比較（任意）。
- 往復の遅れの、M4 の検出器を動かしている間の値（この記録の数字は検出器を動かしていないときのもの）。

## 0025「確かめていないこと」との関係

0025 の「確かめていないこと」のうち、次をこの記録で答えた（0025 の本文は変えない）。

- arduino-cli で UNO Q の MCU 側のビルドと書き込みができるか → できた（上の「書き込み」）。
- PD パススルー経由で UNO Q とアダプタが給電されたまま動くか → 動いた（上の「PD 電源」。ハブではなくアダプタのパススルー）。持ち時間は未確認。
- RPC の遅れ → 上の表（中央値 約 5.1 ms）。Linux 側の 1 ホップの処理時間は M4 に回す（人の決定 D7）。
- LED をどこに出すか → この Issue は MCU 側の LED3 で点灯を確かめただけ。M4 の表示の場所は M4 で決める（D6）。
