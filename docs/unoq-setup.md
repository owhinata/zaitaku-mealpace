# UNO Q の設定と構成 B の接続

咽喉マイクの記録（`tools/record.py --throat`）で使う UNO Q（Linux 側）に、人とエージェントが残した設定と、構成 B（NZ-W210C の送信機 → UNO Q の A2DP シンク）の
接続の手順。UNO Q を作り直すとき、2GB 版で同じことをするときの再現のために置く（plan #38 第 6 節）。
決定は docs/decisions/0028（USB オーディオの経路）・0030（構成 B）。無線リンクと検出器の境界は #42 で決めるもので、この文書には書かない。
送信機の BD アドレスは書かない（`<addr>`）。

## 版

| 項目 | 値 |
|---|---|
| OS | Debian 13 (trixie) aarch64 |
| bluez | 5.82-1.1 |
| PipeWire | 1.4.2-1~qcom1（libspa-0.2-bluetooth 1.4.2） |
| WirePlumber | 0.5.8-2 |
| pulseaudio-utils | 無い（入れない。取り出しは PipeWire の `pw-record`） |
| コーデック | SBC（UNO Q 側の A2DP シンクは sbc / opus_05 / opus_g。送信機とは SBC で合意） |
| arduino-router | 0.11.0（Debian パッケージ。10/24 の apt の更新で 0.10.0 から上がった。`arduino-router.service`、ソケット `/var/run/arduino-router.sock`（0.11.0 では `srw-rw---- root arduino-router`。0.10.0 の 10/13 は `srw-rw-rw-`）、MCU とはシリアル `/dev/ttyHS1` 115200。#38・#44） |
| arduino-cli / arduino-app-cli | 1.5.1 / 0.14.0（基板の上。arduino-app-cli は 10/24 の apt の更新で 0.13.0 から上がった。App Lab の既定のアプリは無い） |
| Python | 3.13.5（msgpack・pip・scipy・librosa は無い） |
| numpy | 2.2.4（`python3-numpy 1:2.2.4+ds-1`。人が `sudo apt install python3-numpy` で入れた。10/24、#44。一緒に `libblas3 3.12.1-6`・`liblapack3 3.12.1-6`・`libgfortran5`・`python3-numpy-dev` が入った） |
| BLAS | netlib の参照実装（`libblas3`）だけ。OpenBLAS・BLIS は入れない（#44 D9）。確かめ方: `update-alternatives --display libblas.so.3-aarch64-linux-gnu`（候補は `/usr/lib/aarch64-linux-gnu/blas/libblas.so.3` の 1 つ） |
| rsync | 3.5.0（人が `sudo apt install rsync` で入れた。10/13、#38 の `deploy-unoq` のため） |

## 残した設定（音声ではない）

| 場所 | 中身 | 置いた人 | 理由 | 確かめ方（読み取り） |
|---|---|---|---|---|
| `/etc/bluetooth/main.conf` | `Class = 0x240414`（Audio/Video・Loudspeaker） | 人（sudo） | 送信機が Audio/Video の機器だけを探すため | `grep -n '^Class' /etc/bluetooth/main.conf`、`bluetoothctl show` の Class |
| `/etc/bluetooth/main.conf` | `JustWorksRepairing = always` | 人（sudo） | 送信機は電源を入れるたびに新規ペアリングで来る。既定（`never`）では鍵が残っていると拒む。**適用済み（人、10/9〜10）**。これと送信機の trust（1 回）で、承認のエージェントは要らない（E9、10/10） | `grep -n JustWorksRepairing /etc/bluetooth/main.conf` |
| `/var/lib/lightdm/.config/wireplumber/wireplumber.conf.d/90-disable-bluez.conf` | lightdm の WirePlumber で bluez の監視を止める | 人（sudo） | A2DP のエンドポイントは先に登録した WirePlumber が持ち、ログイン画面（lightdm）側に音が流れていた | `ls` |
| `~/.config/wireplumber/wireplumber.conf.d/90-bluez-no-seat.conf`（arduino） | コメント 1 行と、`wireplumber.profiles = { main = { monitor.bluez.seat-monitoring = disabled } }` の入れ子の 5 行（下） | メイン（ssh、sudo 不要） | ssh のセッションは seat0 の active ではなく、arduino の WirePlumber が bluez の監視を始めない | `cat` |
| arduino の linger（`loginctl enable-linger arduino`） | ssh のセッションが無くても arduino の user manager（PipeWire・WirePlumber）を動かしたままにする | メイン（10/10） | linger が無いと、最後の ssh が閉じて約 10 秒で PipeWire が止まり、送信機の A2DP が切れる（10/10 の実測） | `loginctl show-user arduino -p Linger` |
| `arduino` を `arduino-router` のグループに足した（`sudo usermod -aG arduino-router arduino`） | `arduino` が router のソケットを開けるようにする | 人（sudo、10/24、#44） | `arduino-router` 0.11.0 でソケットが `srw-rw---- root arduino-router` になり、`mcu_rpc.py` の接続が `PermissionError` になった。新しいログインのセッションから効く | `id`（`110(arduino-router)` があること）、`ls -l /var/run/arduino-router.sock` |
| rfkill の soft block（`sudo rfkill block bluetooth`） | Bluetooth の無線を止める（`/sys/class/rfkill/rfkill0/soft` = 1、`bluetoothctl show` の Powered: no） | 人（sudo、10/11） | 構成 B を記録に使わないと決めた（10/10）後、Discoverable が yes のまま残っていた。有線の記録と M4 の間は Bluetooth を使わないので止める。`main.conf` と WirePlumber の設定は残す。systemd-rfkill が状態を保存するので再起動後も無効のまま。`all` は付けない（rfkill1 は Wi-Fi で、ssh が落ちる） | `cat /sys/class/rfkill/rfkill0/soft`（1）、`bluetoothctl show | grep Powered`（no）。`rfkill` は `/usr/sbin` にあり一般ユーザーの PATH に無い |

`90-bluez-no-seat.conf` の中身（コメントを除く。10/10 の実物）:

```
wireplumber.profiles = {
  main = {
    monitor.bluez.seat-monitoring = disabled
  }
}
```

- ここに無い PipeWire・WirePlumber の設定を足さない（docs/decisions/0030「塞がっていない経路」）。`record.py` は構成 B の記録の前に、`~/.config/pipewire` と
  `~/.config/wireplumber/wireplumber.conf` が無いこと、`~/.config/wireplumber/wireplumber.conf.d/` が `90-bluez-no-seat.conf` だけで、中身がコメント行と空行を除き空白を正規化して上の 5 行と一致することを確かめ、
  違えば記録を始めない。`/etc/pipewire`・`/etc/wireplumber` は一覧を `meta.json` に残すだけ（中身は確かめない）。

## 構成 B の接続（記録の座の初めに 1 回）

送信機を切らなければ、5 条件の間は接続が保たれる見込み。送信機は接続が無いと約 3 分で自動 OFF。

### 主の手順（`JustWorksRepairing = always` を適用した後。10/9〜10 に適用済み）

**10/11 から Bluetooth は rfkill で無効（上の表）。構成 B を再び試すときは、先に人が `sudo rfkill unblock bluetooth` を行い、`bluetoothctl show` で Powered: yes を確かめてから下へ進む。** `record.py --throat sh12jk-nz210c-a2dp-unoq` は Bluetooth が無効だと起動前の検査で止まる（意図どおり）。

1. 人が `/etc/bluetooth/main.conf` の `[General]` に `JustWorksRepairing = always` を書き、bluetooth を再起動する（sudo。1 回だけ。済み）。
   適用の確かめ（読み取り）: `grep -n JustWorksRepairing /etc/bluetooth/main.conf`。
2. UNO Q の電源を入れ、ssh が通ることを確かめる。受信機 NZ-W210R は電源を切ったまま。
   **UNO Q を再起動した後は**（10/10 に分かった）:
   - Pairable は再起動で off に戻るので `bluetoothctl pairable on`。
   - 起動直後は A2DP Sink のエンドポイントが登録されていないことがあるので `systemctl --user restart wireplumber` を行い、
     `bluetoothctl show` の UUID に Audio Sink があることを確かめる。
   - linger（上の表）が有効なことを `loginctl show-user arduino -p Linger` で確かめる。
3. 送信機の電源を入れる（ペアリングモードで来る）。鍵の削除（`bluetoothctl remove`）は要らない。
   **初めての送信機（または鍵を消した後）は 1 回だけ** `bluetoothctl trust <addr>` を行う。trust が無いと、再ペアリングは通るが A2DP のサービスの認可が
   拒まれる（bluetoothd「Authentication attempt without agent」）。trust の後は、送信機の次の試行で A2DP がつながる。
4. `record.py` を起動する。子を起動する前に `pw-dump` で接続を確かめる。ノードが無ければ「送信機が接続されていません」と出て止まるので、3 からやり直す。

   ```
   .venv/bin/python tools/record.py --throat sh12jk-nz210c-a2dp-unoq --position lateral-below-cricoid --cond quiet --duration 100
   ```

- **エージェントは要らない**（E9、10/10: `JustWorksRepairing = always` ＋ trust 1 回）。下のエージェントは、適用の前の手順と、trust の前につながらないときにだけ使う
  （PC の端末で前面に置く。ペアリングが済んだら Ctrl-C で止めてよい）:

  ```
  ssh arduino@<UNO Q> 'while sleep 0.5; do echo yes; done | bluetoothctl --agent NoInputNoOutput'
  ```

- 手で接続を見るなら: `ssh arduino@<UNO Q> 'XDG_RUNTIME_DIR=/run/user/$(id -u) pw-dump' | grep -c bluez_input`。

### 適用の前の手順（10/8。適用までの間だけ使う）

1. UNO Q の電源を入れ、ssh が通ることを確かめる（上の 2）。
2. ペアリング済みなら、UNO Q で `bluetoothctl remove <addr>`（鍵が残っていると送信機の新規ペアリングを拒む）。
3. 上のエージェントを起動したままにする。
4. 送信機の電源を入れる（上の 3）→ `record.py` を起動する（上の 4）。

### `record.py` が確かめること

- 子を起動する前（1 回の ssh。読み取りのみ）: ユーザーの PipeWire・WirePlumber の設定（上）、目的のノード（`bluez_input.<addr>.<番号>`、`media.class` が `Stream/Output/Audio`（実物、10/10）か `Audio/Source`、`api.bluez5.profile` が `a2dp-source`）の有無、
  ミュート、他の取り込みのストリームがつながっていないこと、`libpipewire-module-pipe-tunnel` が無いこと。`--throat-device` を省くと、条件に合うノードがちょうど 1 つのときだけそれを使う。
- 起動後: `pw-record`（`node.name` = `zm-throat-record`）が目的のノードにだけつながっているか（`meta.json` の `throat.pipewire.link_check`）。受信が 2 秒止まれば記録を止める（`stall`）。
- 記録の直後に `tools/throat_check.py <セッション>` で 0 の区間を確かめる。

## UNO Q に音声のファイルが残っていないことの確かめ方（読み取り）

構成 B の記録の後に行う。`<時刻>` は記録を始める前の時刻。

```
ssh arduino@<UNO Q> 'find ~ /tmp -xdev -type f -newermt "<時刻>" 2>/dev/null'
ssh arduino@<UNO Q> 'find ~ /tmp -xdev -type f \( -iname "*.wav" -o -iname "*.raw" -o -iname "*.pcm" -o -iname "*.flac" -o -iname "*.sbc" -o -iname "*.mp3" -o -iname "*.ogg" \)'
```

新しいファイルに音声の形式のものが無く、音声の拡張子のファイルが 0 件であること（docs/log/2026-10-08.md と同じ方法）。
`~/.local/state/wireplumber/stream-properties` は確認から除く。`pw-record` のストリームが来ると WirePlumber が音量・ミュートの復元の状態を書くテキストで、音声ではない（10/10 の実測）。
ほかに音声ではない既知のもの（10/13）: `~/.bash_history`（人の対話のシェルの履歴）、`/tmp/.X0-lock`（起動のたびに X が作る）、`~/zaitaku-mealpace-device/` の配ったファイル（下）。
#51 の計測用の venv がある間（10/24〜）: `~/zaitaku-mealpace-bench-venv/` の下のファイル（pip が入れたもの）、`~/.cache/pip/`（pip のキャッシュ）、
`~/.cache/Microsoft/DeveloperTools/.onnxruntime/` の `deviceid`・`onnxruntime.db`（ONNX Runtime 1.31 が import のたびに作る・書き直す。0039 決定 6）。
`~/.cache/mesa_shader_cache/`（Mesa の GPU のシェーダのキャッシュ。10/24 14:44 にできた。計測は `MESA_SHADER_CACHE_DISABLE=true` で増やさない）。
UNO Q の時計は起動の後に進むことがある（10/13、ファイルの日時と `date -u` がずれた。NTP によると見ているが未確認）。`-newermt` の基準時刻が曖昧なときも、音声の拡張子の検索が 0 件であることで補う。

## MCU のスケッチと Linux 側の配置（#38、docs/decisions/0035）

- **MCU 側のスケッチ**: `firmware/unoq_mcu/`（RPC で `zm_set_state(int)` を公開し、8×13 の LED マトリクスに状態 0 = 全消灯、1 = 流れる波線、2 = 微笑む顔を出すだけ。
  図形と順序は `firmware/indicator/` へのシンボリックリンク。行は上下を反転して書き、UNO Q では波線が左から右へ流れて見える（人が可とした）。LED3 は使わない。表示の規則は M4。0035 決定 7）。
  今入っているのは 10/13 22:5x JST に書き込んだ波線と顔の版（その前に LED3 の緑の版、顔だけの版を書いた）。
  コア `arduino:zephyr@1.0.0`、`Arduino_RouterBridge@0.4.3`（依存 `Arduino_RPClite` 0.3.1）、FQBN `arduino:zephyr:unoq`。版は基板の上の `~/.arduino15` と同じ。
  書き込みは UNO Q の USB-C を PC に直につなぎ、`arduino-cli board list` に出るポート（`/dev/ttyACM0`、`configuration: ADB Configuration`）で:

  ```
  cmake -S . -B build-unoq -DSKETCH_NAME=unoq_mcu -DPORT=/dev/ttyACM0 -DPYTHON3=$PWD/.venv/bin/python3
  cmake --build build-unoq --target build && cmake --build build-unoq --target upload
  ```

  中身は remoteocd（USB の adb 越し）。書く物を UNO Q の `/tmp/remoteocd/`（`flash_sketch.cfg`、`unoq_mcu.ino.elf-zsk.bin`、`zephyr-arduino_uno_q_stm32u585xx.elf`）に置き、
  基板の上の openocd が SWD で書く。`/tmp` は tmpfs で、再起動で消える。音声ではない。10/13 に書き込んだ（10/13 のコミットのスケッチ）。
  書き込みの直後に router の journal に `decoding packet: invalid packet, expected array` が数行出るが、その後の RPC は通る。
- **Linux 側の配置**: `cmake --build build-unoq --target deploy-unoq`（rsync `--copy-links`。宛先は `-DUNOQ_HOST`、既定 `arduino@unoq.local`）で、
  `device/unoq/DEPLOY_FILES` の一覧だけを `~/zaitaku-mealpace-device/` に配る（#51 の後は 14 個。下の「推論ランタイムの比較の計測」）。#44 の 9 つ（10/24）: `mcu_rpc.py`、`led_check.py`、`bench_infer.py`、`features_np.py`、
  `mel40_htk_0_4000.npy`、`mel26_htk_0_4000.npy`、`features_np_ref.npz`、`postprocess.py`、`evaluate.py`。うち後ろの 6 つは `analysis/` へのリンクで、
  送る前に先が `analysis/` の直下の普通のファイルであることを確かめる（0035 の追記）。UNO Q の上はどれも普通のファイル。音声のファイルは無い。
  常駐させない（systemd のユニットは無い）。ファイルを書かない。実行は `python3 -B`（`__pycache__` を作らない）:

  ```
  ssh arduino@unoq.local 'python3 -B ~/zaitaku-mealpace-device/led_check.py wave'    # smile / off / bench --n 100 --warmup 5
  ```

  `wave`・`smile` は表示したまま終わる。`bench` は顔と消灯を交互に送って往復を測る（状態を渡して応答が返るまで。描画は含まない）。最後に消灯を送る。

  1 窓あたりの処理時間の計測（#44。合成入力だけ、ファイルを書かない、標準出力は集計の行だけ。最初に `features_np_ref.npz` との照合をし、NG なら測らない。
  RPC は消灯を送るだけで、最後にも消灯を送る）。1 スレッド指定と既定（ssh のセッションの値を引き継がない）の 2 回:

  ```
  ssh arduino@unoq.local 'cd ~/zaitaku-mealpace-device && env OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 python3 -B bench_infer.py'
  ssh arduino@unoq.local 'cd ~/zaitaku-mealpace-device && env -u OPENBLAS_NUM_THREADS -u OMP_NUM_THREADS -u MKL_NUM_THREADS python3 -B bench_infer.py'
  ```

  `--no-rpc` で RPC を省く（PC のテスト用）。10/24 の結果は docs/log/2026-10-24.md。

  記録（`record.py`）の間は動かさない（0028 決定 3）。消すときは `ssh arduino@unoq.local 'rm -r ~/zaitaku-mealpace-device'`。
- **推論ランタイムの比較の計測（#51、docs/decisions/0039）**: 配る一覧は 14 個（上の 9 つに `bench_runtimes.py` と乱数の重みのモデル 4 つ `cnn_rand0.onnx`・
  `cnn_rand0.tflite`・`cnn_rand0.ncnn.param`・`cnn_rand0.ncnn.weights` を足した。モデルは `analysis/` へのリンク）。計測のために入れたものは一時的なもので、
  装置の推論は numpy のまま（0039 決定 1）。計測が終わって要らなければ人が外す（下）。

  計測用の venv（配る先の外。numpy は system の 2.2.4 を共有する）の作り方（人、10/24）:

  ```
  ssh arduino@unoq.local 'python3 -m venv --system-site-packages ~/zaitaku-mealpace-bench-venv'
  ssh arduino@unoq.local '~/zaitaku-mealpace-bench-venv/bin/pip install ai-edge-litert==2.3.0 ncnn==1.0.20261020'
  ssh arduino@unoq.local '~/zaitaku-mealpace-bench-venv/bin/pip install --no-cache-dir --no-deps onnxruntime==1.31.0'
  ```

  10/24 に入れたもの（人、`sudo`）:

  | 入れ方 | 名前と版 | 用途 |
  |---|---|---|
  | apt | `python3-onnxruntime` 1.21.0+dfsg-1 | ONNX Runtime（import で SIGILL になり使えない。PyPI の wheel で測った） |
  | apt | `python3-pyarmnn` 23.08-5、`armnn-latest-cpu-gpu` 23.08-5（`libarm-compute43` 24.11） | Arm NN の CpuAcc・GpuAcc |
  | apt | `python3-venv` 3.13.5 | 計測用の venv |
  | apt | `libopenblas0-pthread` 0.3.29+ds-3 | OpenBLAS の計測（C） |
  | apt | `ocl-icd-opencl-dev` 2.3.3-1 | `libOpenCL.so` のリンク（Arm NN GpuAcc の再試行） |
  | venv（pip） | `ai-edge-litert` 2.3.0、`ncnn` 1.0.20261020、`onnxruntime` 1.31.0 | LiteRT・ncnn・ONNX Runtime |

  apt は自動で入ったものを含めて 47 個（一覧は `/var/log/apt/history.log` の 10/24 の行）。ディスクの空きは 1.8 GB → 1.6 GB、venv は 130 MB。

  走らせ方（`~/zaitaku-mealpace-device/` で、venv の python。バックエンドごとに別の ssh。合成入力だけ、ファイルを書かない、標準出力は集計の行だけ。最初に #44 の照合、
  次にバックエンドごとに `cnn_forward` との照合をし、NG なら測らない。RPC は消灯を送るだけ）:

  ```
  ssh arduino@unoq.local 'cd ~/zaitaku-mealpace-device && ~/zaitaku-mealpace-bench-venv/bin/python -B bench_runtimes.py --list'
  ssh arduino@unoq.local 'cd ~/zaitaku-mealpace-device && env OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 RUSTICL_ENABLE=freedreno MESA_SHADER_CACHE_DISABLE=true ~/zaitaku-mealpace-bench-venv/bin/python -B bench_runtimes.py --only ncnn-vulkan --threads 1 --overlap; echo "rc=$?"'
  ssh arduino@unoq.local 'cd ~/zaitaku-mealpace-device && ~/zaitaku-mealpace-bench-venv/bin/python -B bench_runtimes.py --only idle'
  ```

  既定のスレッドは `env -u OPENBLAS_NUM_THREADS -u OMP_NUM_THREADS -u MKL_NUM_THREADS …` と `--threads 0`。`--sustain-s`（既定 60）は持続的な負荷の秒数、0 で省く。
  GPU の環境変数（`RUSTICL_ENABLE=freedreno`・`MESA_SHADER_CACHE_DISABLE=true`）は計測のコマンドで渡し、ファイルに固定しない（0039 決定 3）。
  import でプロセスごと落ちる経路（SIGILL の 132 など）は出力が出ないので、終了コードを必ず控える。10/24 の結果は docs/log/2026-10-24.md「#51」。

  BLAS の切り替え（人、`sudo`）。OpenBLAS を入れると alternatives が OpenBLAS に替わる（auto、優先度 100）。参照実装に戻すには:

  ```
  sudo update-alternatives --set libblas.so.3-aarch64-linux-gnu /usr/lib/aarch64-linux-gnu/blas/libblas.so.3
  sudo update-alternatives --set liblapack.so.3-aarch64-linux-gnu /usr/lib/aarch64-linux-gnu/lapack/liblapack.so.3
  ```

  `--set` の後は **manual mode**（10/24 の今の状態）。OpenBLAS を外すか `sudo update-alternatives --auto libblas.so.3-aarch64-linux-gnu`（liblapack も）に戻すまでこのまま。
  実際に読み込んだ BLAS は `bench_runtimes.py` の環境の行（`/proc/self/maps`）で分かる（`np.show_config` はビルド時の値のままで替わらない）。

  消し方（人。計測の後、要らなければ。外す前に `apt-get -s remove …` で一覧を見て、もとから入っていたものを外さないことを確かめる）:

  ```
  ssh arduino@unoq.local 'rm -r ~/zaitaku-mealpace-bench-venv ~/.cache/Microsoft'
  sudo apt remove python3-onnxruntime python3-pyarmnn armnn-latest-cpu-gpu python3-venv libopenblas0-pthread ocl-icd-opencl-dev && sudo apt autoremove
  ```

  `libopenblas0-pthread` を外すと alternatives の候補は参照実装だけになる。外したもの・残したものは docs/log と 0039 に書く。
- **PD 給電の構成（10/13）**: PD 対応モバイルバッテリー → USB-C→3.5 mm 変換アダプタ（UGREEN CM721。DAC 搭載、PD 60 W パススルー）の PD の入口 → UNO Q。
  SH-12JK はこのアダプタの 3.5 mm に挿す。ALSA では `hw:CARD=Audio`。PC とは Wi-Fi の ssh だけ。この構成で `record.py --throat sh12jk-wired-unoq-usbaudio` の
  60 秒が使わない目安に当たらなかった（docs/log/2026-10-13.md）。

## 構成 A は使わない。構成 B も記録には使わない

- 送信機 → 受信機 NZ-W210R → USB オーディオ → UNO Q（構成 A）は使わない（人の決定、10/8）。iface `sh12jk-nz210c-rx-unoq-usbaudio` は予約のまま。
- 構成 B（送信機 → UNO Q の A2DP）は動くが、送信機 NZ-W210C は入力がごく小さいとき無音（ちょうど 0）を送るノイズゲートを持ち（取説に切替なし。docs/log/2026-10-10.md）、
  装着して静止した咽喉マイクの `quiet` が成り立たない。人の決定（10/10）で学習用の記録には使わず、記録と検出器は有線（USB オーディオアダプタ）で進める。
  上の設定と `record.py` の構成 B の実装は残す（再び使うときの手順はこの文書のとおり）。
