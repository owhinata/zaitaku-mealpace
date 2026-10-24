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
     （ハンドラはライブラリの別スレッドで動く。ハンドラは状態を渡すだけなので `provide_safe` は使わない）。
   - 公開する名前は `zm_set_state(int)` だけ（0 消灯 / 1 流れる波線 / 2 微笑む顔。0018 の `led` の値）。範囲外は MCU が消灯にして false を返す。音声は渡さない。
     最初は `zm_set_led(bool)` だったが、10/13 夜に `zm_set_state(int)` に替え、`zm_set_led` は残さない（人の決定。plan 38d、M4 で使うのは状態だけ）。
5. **ビルドと版（D3・D4）。** MCU 側のスケッチ `firmware/unoq_mcu/` は 0001 の A 案（CMake が arduino-cli を呼ぶ）でビルドし、`SKETCH_NAME=unoq_mcu` のとき
   FQBN は `arduino:zephyr:unoq`。版は UNO Q の上の版に合わせて固定する: コア `arduino:zephyr@1.0.0`、`Arduino_RouterBridge@0.4.3`（依存 `Arduino_RPClite` 0.3.1）。
   router は `arduino-router` 0.10.0（UNO Q の Debian パッケージ。こちらでは入れ替えない）。
6. **LED（D6）。** この Issue の LED は MCU 側の RGB LED（LED3）の緑 1 色で、点けて消すことの確かめだけ。表示の規則（0018 の緑・黄の「目安」、
   1.0 秒で消灯）は持たない。M4 の表示の場所と規則は M4 で決める。
7. **LED の変更（D6 を改める。人の指示・決定 10/13）。** 表示は MCU 側の 8×13 LED マトリクス（コア同梱の `Arduino_LED_Matrix` 0.1.3）に出し、LED3 は使わない。
   状態 0 / 1 / 2 と 2 つの画（0018 の追記（2026-10-01）の表のまま）: `0` 全消灯、`1` 流れる波線（200 ms × 6 フレーム、周期 1.2 秒）、`2` 微笑む顔（静止画）。
   図形と順序は #32 の `firmware/indicator/shapes.h`（生成物）と `shape_seq.h/.cpp` を `firmware/unoq_mcu/` からシンボリックリンクで使う（元は 1 つ。plan 38d の B1）。
   受け取った状態をそのまま画にするだけで、表示の規則（0018 の保持、更新が 1.0 秒無ければ消灯）は持たない（M4）。
   - 書くとき: 表の 12 列を 13 列の左寄せ（右端の 1 列は消灯）、行は上下を反転する（UNO Q のマトリクスは R4 WiFi と行の向きが逆に見えた。人の目視 10/13）。左右はそのまま。表は変えない。
   - 上下反転の結果、UNO Q では波線が**左から右へ流れて見える**（0018 の追記は「左へ流れる」）。人が見て、このままでよいとした（10/13 夜）。
   - スレッド: RPC のハンドラ（別スレッド）は状態を `std::atomic<uint8_t>` に書くだけで、描画は `loop()`（main スレッド）がフレームの変わったときだけ行う。
     描画の組み立てと範囲の検査は Arduino に依存しない `matrix_frame.*` に分け、PC のホストテスト（`firmware/unoq_mcu/host/test_unoq_mcu.sh`）で確かめる。
   - 経緯: 10/13 の午後に、LED3 の緑の版、次に `zm_set_led(bool)` で顔だけを出す版（反転版、22:3x JST）を書き込んだ。波線を入れた版（`zm_set_state`）は 22:5x JST に書き込んだ。

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
- マトリクス（決定 7）: 10/13 22:3x JST に USB（adb）で書き込んだ（スケッチ 88,208 B、所要 約 10 秒）。最初の版は顔が上下反転して見え（人の目視）、
  行を上下反転して書く版に直して書き込み直した。`on` で微笑む顔、`off` で全消灯（人が目で確かめた）。
- 波線を入れた版（決定 7、`zm_set_state`）: 10/13 22:5x JST に USB（adb）で書き込んだ（スケッチ 89,972 B、所要 約 10 秒）。
  リンクの `shape_seq.cpp` がビルドに入ったこと（`arduino-cli compile -v` の出力）と、`static_assert(ATOMIC_CHAR_LOCK_FREE == 2)` が通ったことを確かめた。
  `led_check.py wave` で波線が点滅せずに流れ（左から右へ流れて見える。上の決定 7）、`smile` で微笑む顔、`off` で全消灯（人が目で確かめた）。
- 往復の遅れ（`led_check.py bench --n 100 --warmup 5`。合否は付けない）:

  | スケッチ（呼び出し） | 測る範囲 | 電源 | 件数 | 中央値 | 95 パーセンタイル | 最大 |
  |---|---|---|---|---|---|---|
  | LED3 の緑（`zm_set_led`） | 送ってから応答まで（ハンドラの中で GPIO を書く） | USB-C を PC に直挿し | 100 | 5.103 ms | 5.239 ms | 5.377 ms |
  | LED3 の緑（`zm_set_led`） | 同上 | PD（下の構成） | 100 | 5.090 ms | 5.216 ms | 5.341 ms |
  | 顔だけ（`zm_set_led`、反転版） | 送ってから応答まで（ハンドラの中で描画する） | USB-C を PC に直挿し | 100 | 5.293 ms | 5.412 ms | 5.471 ms |
  | 波線と顔（`zm_set_state`） | 状態を渡して応答が返るまで。**描画は含まない**（描画は `loop()` が後で行う） | USB-C を PC に直挿し | 100 | 5.301 ms | 5.426 ms | 5.574 ms |

  最後の行は、前の 3 行（ハンドラの中で書き終えてから返す）と測る範囲が違う。

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
- RPC の遅れ → 上の表（中央値 約 5.1〜5.3 ms）。Linux 側の 1 ホップの処理時間は M4 に回す（人の決定 D7）。
- LED をどこに出すか → この Issue は MCU 側の 8×13 マトリクスに状態 0 / 1 / 2 の画を出すところまで（決定 7）。表示の規則は M4 で入れる。

## 追記（2026-10-24、Issue #44）: 推論のファイルの配り方と、確かめたこと

上の決定の本文は変えない。決定 1 の「M4 は同じ一覧に PC と同じ推論のファイルを足す」を、次の形で行った（plan #44、Codex 3 面 LGTM、人の承認 10/24）。

- **配り方（決定 1 への追加）**: `device/unoq/` に `analysis/` のファイルへの相対のシンボリックリンクを置き、一覧に足した。`deploy-unoq` の rsync に
  `--copy-links` を足し、リンクの先の中身を送る（UNO Q の上は今までと同じ平らな並びの普通のファイル）。送る前に、一覧の各行が `device/unoq/` の
  普通のファイルか、`analysis/` の直下の普通のファイルを指すリンクかを `realpath` で確かめ、`data/` を含む先・`analysis/` の外を指すリンク・
  先が無いリンクは送らずに止まる（CMakeLists.txt。止まることは一時的なリンクで確かめた）。リンクのやり方は `firmware/unoq_mcu/` と同じ。
- **配る 9 つ**: `mcu_rpc.py`、`led_check.py`、`bench_infer.py`（計測。合成入力だけ、ファイルを書かない）、リンクの `features_np.py`（numpy だけの間引き・
  特徴量・CNN の順伝播。PC の `features_v2.py`・`tools/throat_check.py` も同じものを呼ぶ）、`mel40_htk_0_4000.npy`・`mel26_htk_0_4000.npy`
  （メル行列。librosa から PC で作った定数）、`features_np_ref.npz`（照合用の参照。合成入力の特徴量）、`postprocess.py`・`evaluate.py`（#44 では import
  だけ。窓ごとの後処理は #47）。音声のファイルは無い。静的テストは配る `.py` の音声のモジュール・scipy・librosa・torch などの import と書き込みを禁じ、
  numpy は `features_np.py`・`bench_infer.py`・`postprocess.py` にだけ許す。
- **UNO Q に入れたもの**: 人が `sudo apt install python3-numpy`（10/24）。`python3-numpy 1:2.2.4+ds-1`（numpy 2.2.4）、一緒に `libblas3 3.12.1-6`・
  `liblapack3 3.12.1-6`・`libgfortran5 14.2.0-19`・`python3-numpy-dev`。BLAS は netlib の参照実装だけ（`update-alternatives` の候補は 1 つ）。
  librosa・scipy は入れていない。
- **`arduino-router` 0.11.0 とソケットの権限**: 10/24 の apt の更新で `arduino-router` が 0.10.0 → 0.11.0 になり、ソケット `/var/run/arduino-router.sock` が
  `srw-rw---- root arduino-router` になった（10/13 は `srw-rw-rw-`）。`arduino` から開けなくなったので、人が `sudo usermod -aG arduino-router arduino`
  を行った（10/24。新しいログインのセッションから効く）。上の「確かめたこと（10/13）」のソケットの権限の記述は 10/13 の時点の事実として残す。
- **確かめたこと（合成入力、docs/log/2026-10-24.md）**: UNO Q の numpy 版の特徴量と順伝播が PC の librosa 版の参照と最大の差 6.1e-08 で一致した（許容 1e-5）。
  1 窓の全体（1 ホップの間引き → 特徴量 → 正規化 → 推論 → 後処理 → RPC を直列に 1 回）の中央値は、構成 J で 41.6〜45.0 ms、構成 J＋R で
  48.2〜53.8 ms（ホップ 0.1 秒・0.25 秒、1 スレッド指定・既定）。最大は 58.1 ms 以下で、ホップ 0.25 秒の 250 ms・ホップ 0.1 秒の 100 ms に収まる。
  CNN の順伝播だけで 1 窓 約 27.8 ms、RPC の往復 約 5.3〜5.4 ms（10/13 の表と同じ桁）。計測中のスレッド数は 1（既定でも 1）。
  配置と計測の後、UNO Q に音声の拡張子のファイルは 0 件、`~/zaitaku-mealpace-device/` は一覧の 9 つだけで `__pycache__` は無い。
  ホップの変更はここでは決めない（#44 D5。別の decision）。

## 追記（2026-10-24、Issue #51）: 推論ランタイムの比較の計測のファイルと、一時的に入れたもの

上の決定の本文は変えない。計測のために一時的に入れたものと装置で使うものの区別は docs/decisions/0039（採用）。

- **配る一覧は 14 個**: #44 の 9 つに、`bench_runtimes.py`（計測。合成入力だけ、ファイルを書かない、環境変数を書き換えない、`subprocess` を使わない）と、
  `analysis/` の直下へのリンクの乱数の重みのモデル 4 つ（`cnn_rand0.onnx`・`cnn_rand0.tflite`・`cnn_rand0.ncnn.param`・`cnn_rand0.ncnn.weights`。
  `make_runtime_models.py` で作った。音声・学習の成果・特徴量ではない）を足した。静的テストは、ランタイムのモジュール（`onnxruntime`・`ai_edge_litert`・`pyarmnn`・
  `ncnn`）の import を `bench_runtimes.py` にだけ許し、配る `.py` の環境変数の書き込みを禁じる。`deploy-unoq` の検査は今のまま（足したのは普通のファイルと
  `analysis/` の直下へのリンク）。計測のファイルを一覧から外すかは、ランタイムの採否の decision（0039 決定 5）の後、#47 の plan で決める。
- **venv は配る先の外**: `~/zaitaku-mealpace-bench-venv`（`--system-site-packages`。numpy は system の 2.2.4）。`deploy-unoq` は venv に触らない。
- **入れたもの（人、`sudo`。名前と版は 0039 と docs/log/2026-10-24.md「#51」）**: apt で `python3-onnxruntime`・`python3-pyarmnn`・`armnn-latest-cpu-gpu`・`python3-venv`
  （自動を含め 34 個）、`libopenblas0-pthread`・`ocl-icd-opencl-dev`（自動を含め 13 個）。venv に pip で `ai-edge-litert`・`ncnn`・`onnxruntime`（PyPI の wheel）。まだ外していない。
- **BLAS の alternatives**: libblas・liblapack とも参照実装（`blas/libblas.so.3`・`lapack/liblapack.so.3`）を指すが、`update-alternatives --set` で戻したので
  **manual mode**。OpenBLAS（優先度 100）が入っている間に `--auto` に戻すと OpenBLAS に替わる。numpy の数字（#44）は参照実装のときのもの。
