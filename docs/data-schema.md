# 記録形式（固定）

1 セッション = 1 フォルダ。名前は `YYYYMMDD-HHMMSS_<subject>_<cond>`。

```
data/raw/20260922-193012_self_water/
  imu.csv           t_ms, ax, ay, az, gx, gy, gz
  audio.wav         16 kHz, mono, int16
  audio_chunks.csv  t_ms, sample_index
  analog.csv        t_ms, ch0, ch1, ...      （代替センサ使用時のみ）
  events.csv        t_ms, label, note
  meta.json
```

検出器ファームウェア（M2 以降、`meta.json` の `fw` が `detector`）で録ったセッションには、IMU と音声のファイルはできない。

```
data/raw/20261005-121500_self_meal/
  detect.csv        t_ms, window_t_ms, positive, prob, led
  feat.csv          t_ms, window_t_ms, f0, f1, ..., f(N-1)   （FEAT を送る設定のとき。self のみ）
  events.csv        t_ms, label, note
  meta.json
```

咽喉マイクだけのセッション（`meta.json` の `fw` が `pc-throat`。装置をつながない。`tools/record.py --throat`、docs/decisions/0029）の構成:

```
data/raw/20261008-101500_self_water/
  throat.wav          48 kHz（実際に録った値は meta.json の sample_rates.audio_hz）、2 ch、int16。受けたまま
  throat_chunks.csv   t_ms, sample_index
  events.csv          t_ms, label, note
  meta.json
```

M4 の装置のセッション（`meta.json` の `fw` が `unoq-detector`。UNO Q の Linux 側の `detector.py` が推論し、`tools/record.py --unoq-detector` が ssh で
行を受けて書く。音声のファイルはできない。#47、下の「装置（UNO Q）のセッション」）の構成:

```
data/raw/20261030-120000_self_water/
  detect.csv        t_ms, window_t_ms, positive, prob, led          （M2 と同じ列・同じ順。値の意味は下）
  feat.csv          t_ms, window_t_ms, f0, ..., f81                  （--feat のとき。self のみ）
  detect_rx.csv     window_t_ms, rx_ms, proc_us, lat_us, queue, backlog
  events.csv        t_ms, label, note
  meta.json
```

## 共通

- 時刻の基準は装置の `millis()`。すべてのストリームを同じクロックで刻印する。
- 咽喉マイクのセッションでは、PC の単調時計の、最初の音声のバイトが届いた時刻からの ms（`events.csv` と `throat_chunks.csv`）。
  装置の `millis()` とは別の時計で、同じセッションに両方は入らない（docs/decisions/0029）。
- ストリームのファイル（`imu.csv`、`audio.wav` と `audio_chunks.csv`、`analog.csv`、`detect.csv`、`feat.csv`）は、そのストリームの最初のフレームが
  届いたときに作る。`events.csv` と `meta.json` は常に作る。フレームが 1 つも届かなかったセッションには `events.csv` と `meta.json` しかできない。
- `subject`: `self` | `p1`
- `cond`: `water` | `saliva` | `talk` | `cough` | `neck` | `quiet` | `meal`

## imu.csv

- 104 Hz（LSM6DSOX のネイティブレート）
- `ax, ay, az`: g。`gx, gy, gz`: deg/s。float、小数4桁
- 軸: 基板を「Y 軸が首の上下方向、+Y が頭側」になる向きで装着する

## audio.wav

- 16 kHz, mono, int16。基板 PDM マイク
- 音声チャンクの `t_ms` は、装置が PDM の受信コールバックでそのチャンクを読み出した直後に取った `millis()`。
  論理上はチャンクの終端側の時刻として扱い、先頭サンプルの時刻の推定値を `t_ms − 1000 × チャンクのサンプル数 ÷ audio_hz` ms
  （64 サンプル・16 kHz なら 4 ms 前）とする（docs/decisions/0013）。WAV には連続波形として書き、
  各チャンクの `t_ms` は `audio_chunks.csv`（`t_ms, sample_index`）に別途残す

## throat.wav（咽喉マイクのセッションのみ）

- SH-12JK（docs/decisions/0025）の音声。`tools/record.py` が子プロセス（`arecord`。UNO Q では ssh 越し。docs/decisions/0028）から受けた S16_LE を
  変換せずに書く。サンプリング周波数とチャンネル数は `meta.json` の `sample_rates.audio_hz` と `throat.channels`（2026-10 の構成では 48000 Hz・2 ch）。
- 構成 B（iface `sh12jk-nz210c-a2dp-unoq`。子は ssh 越しの `pw-record`、docs/decisions/0030）では、PipeWire のノードの形式のレートが 48000 でなければ
  PipeWire が 48000 に変換したもの（ノードの形式のレートは `throat.pipewire.node_rate_hz`、SBC の符号化のレートは裏が取れたときだけ `throat.pipewire.codec_rate_hz`）。
- **解析の基準は 16 kHz・1 ch**。解析は L（ch0）を 16 kHz に落として使う（落とし方は解析のコード。#36 では `scipy.signal.resample_poly(x, 1, 3)`）。
  この文書の他の節の「16 kHz」（`audio.wav`）は記録ファームウェアの基板マイクの値で、変わらない。
- 生の計測データ。`data/raw/` の外に出さず、コミットしない。`self` のみ（docs/decisions/0028）。

## throat_chunks.csv（咽喉マイクのセッションのみ）

- 1 行 = 子の標準出力の 1 回の読み取り（書いたフレームが 0 の読み取りは行にしない）。`t_ms` は読み取りが戻った時刻（上の PC の時計）で、
  `audio_chunks.csv` と同じく終端側の時刻として読む。`sample_index` はその読み取りで書いた最初のフレームの番号（0 から）。
- サンプルの時刻の出し方（アンカーの下側の包絡と直線）は docs/decisions/0029 と `tools/throat_check.py`。

## analog.csv（拡張点）

- 代替センサ（圧電コンタクトマイク、ピエゾ電線センサなど）を ADC で読む場合
- 1〜4 kHz。`ch0..chN` は ADC 生値（12 bit）
- `meta.json` の `sensors` に接続を記録する

## events.csv

- `label`: `s`（嚥下）`t`（発話）`c`（咳）`n`（首の動き）`q`（静止）`b`（一口）`o`（観察: note に内容）
- マーカーは**開始時刻**。区間の終端は後処理で決める（嚥下は開始から 1.5 秒、など）
- 咽喉マイクのセッションのマーカーの `t_ms` は、キーを受けた時刻（PC の時計。上の「共通」）。装置のセッションの「直前に届いたフレームの `t_ms`」とは違う
- 咽喉マイクのセッションの `o` の note: `tap` = 叩き（時刻の対応の確かめ。`tools/throat_check.py`）、`start` = 記録の範囲の始まり（解析はこの `t_ms` から最後のサンプルの時刻までを範囲にする。docs/decisions/0031）

## detect.csv（検出器のみ）

- 1 行 = 1 窓（1.0 秒、ホップ 0.25 秒。`docs/evaluation.md`）。装置が窓ごとに送る DETECT フレームをそのまま書く
- `t_ms`: フレームの送信時刻（ヘッダの `t_ms`。装置の `millis()`）。`window_t_ms`: 窓 `[window_t_ms, window_t_ms + 1000)` の開始
- `positive`: `1` = `prob` が凍結した閾値（`meta.json` の `threshold`）以上、`0` = 陰性。装置の 2 値の出力そのもの
- `prob`: `swallow` の確率（0〜1。float32 を 9 桁の有効数字で書く）。装置が閾値と比べた値そのもので、PC 側で `positive == (prob >= threshold)` を丸めなしに確かめられる。咳のクラスの確率は載せない（docs/decisions/0019）
- `led`: `0` 消灯 / `1` まだ確認していない目安 / `2` 確認した目安（docs/decisions/0018。検出器の基板の LED では `1` = 黄・`2` = 緑、表示器（`firmware/indicator/`）では `1` = 流れる波線・`2` = 微笑む顔）。この窓の結果を反映した後の状態。LED は嚥下の目安であり、評価には使わない
- 評価（`analysis/evaluate_detector.py`）は `positive` と `events.csv` の `s` だけを使う。記録の範囲（`detect.csv` と `feat.csv` の時刻から取る。送信の遅れの上限 1.0 秒）は docs/decisions/0021

## feat.csv（検出器のみ。`self` のセッションで有効にする。`p1` で使うかは別途決める）

- 1 行 = 1 窓。`t_ms`・`window_t_ms` は `detect.csv` と同じ意味で、同じ窓の行は同じ `window_t_ms` を持つ
- `f0 … f(N−1)`: 窓の特徴量ベクトル。装置が正規化する**前**の値（物理単位。float32 を 9 桁の有効数字で書く）。次元数 N と順序は音の式の記録に従い、
  名前は `meta.json` の `feature_names`（同じ順）にある
- 生の音声波形ではなく変換後の特徴量なので、検出器が PC に送ってよい（docs/decisions/0005 は波形の保存と送信を禁じ、特徴量は禁じていない）。
  ただし生の計測データとして扱い、`data/raw/` の外に出さず、コミットしない
- PC 側の再採点（#22 の正規化の定数とライブラリ）に使う。装置の `positive` との差を `evaluate_detector.py --scorer` で出す

## meta.json

```json
{
  "subject": "self",
  "cond": "water",
  "position": "midline-below-thyroid",
  "band": "elastic-25mm",
  "firmware_sha": "abc1234",
  "sample_rates": {"imu_hz": 104, "audio_hz": 16000, "analog_hz": 0},
  "sensors": [
    {"id": "imu", "part": "LSM6DSOX", "iface": "onboard"},
    {"id": "mic", "part": "MP34DT06JTR", "iface": "onboard-pdm"}
  ],
  "notes": "",
  "fw": "logger",
  "imu_hz": 104,
  "audio_hz": 16000
}
```

- `fw` / `imu_hz` / `audio_hz` は装置の META フレームの中身がそのまま入る（装置の申告値）。
  `sample_rates` は PC 側の既定値。どちらを正とするかと、META が届かなかった場合の扱いは
  `docs/decisions/0006`。

検出器ファームウェアの META フレームは次のキーを持つ（装置の申告値。`record.py` はそのままトップレベルに merge する）:

```json
{
  "fw": "detector",
  "imu_hz": 104, "audio_hz": 16000,
  "window_ms": 1000, "hop_ms": 250,
  "threshold": 0.90,
  "model": {"source": "edge-impulse", "project_id": 0, "deploy_version": 0},
  "feature_set": "m2-0020",
  "feature_names": ["acc_ptp_x", "..."]
}
```

- `threshold` は #22 で凍結した閾値（装置が持つ float32 の定数の値。有効数字 9 桁）。`positive` は装置が float32 で `prob >= threshold` を評価した結果で、PC 側の
  比較も両方を float32 にして行う。`model` は Edge Impulse の書き出しのプロジェクト ID と deploy version（Private にした場合は 0。docs/decisions/0019）。
  `feature_set` は音の式の記録の番号。`feature_names` は `feat.csv` の `f0 …` の順の名前
- `imu_hz` / `audio_hz` はセンサの取り込みレート。特徴量の計算で音を間引いても、マイクの取り込みは 16000
- `record.py` は記録を始める前に合図を送って META を待ち、届かなければ記録を始めない（docs/decisions/0006 の追記）。
  それより前に録ったセッションには、これらのキーが無いものがある。`fw` が `detector` でないセッションは M2 の評価に使わない
  （`docs/recording-protocol.md`）

咽喉マイクだけのセッション（docs/decisions/0029）の `meta.json` の例:

```json
{
  "subject": "self",
  "cond": "water",
  "position": "midline-below-thyroid",
  "band": "elastic-25mm",
  "firmware_sha": "abc1234",
  "sample_rates": {"imu_hz": 0, "audio_hz": 48000, "analog_hz": 0},
  "sensors": [{"id": "throat", "part": "SH-12JK", "iface": "sh12jk-wired-unoq-usbaudio"}],
  "notes": "",
  "fw": "pc-throat",
  "throat": {
    "host": "arduino@unoq.local",
    "device": "hw:CARD=Audio,DEV=0",
    "format": "S16_LE", "channels": 2, "rate_hz": 48000,
    "remote_command": "exec arecord -D hw:CARD=Audio,DEV=0 -f S16_LE -r 48000 -c 2 -t raw -B 2000000 -F 125000 -v -",
    "alsa_buffer_size": 96000, "alsa_period_size": 6000,
    "first_byte_wait_s": 3.91,
    "frames": 8640000, "elapsed_s": 180.01, "est_diff_frames": -432,
    "overrun_lines": 0,
    "stop": "duration", "child_returncode": 255,
    "mixer": "Simple mixer control 'Mic',0\n  ..."
  }
}
```

- `fw` = `pc-throat` は装置の申告値ではなく `record.py` が書く値（装置をつながないセッション）。トップレベルの `imu_hz`・`audio_hz` は無い（申告する装置が無い）。
  `sample_rates` は実際に録った値（`imu_hz` 0、`analog_hz` 0）。`firmware_sha` は今までどおり PC 側のリポジトリの短い sha。
- `sensors[].iface`:

| `iface` | 経路 | `--throat-host` | 状態 |
|---|---|---|---|
| `sh12jk-wired-unoq-usbaudio` | SH-12JK を有線で UNO Q の USB オーディオアダプタに挿す | `local` 以外 | #37 の有線 |
| `sh12jk-nz210c-rx-unoq-usbaudio` | NZ-210C の受信機の出力を UNO Q の USB オーディオアダプタへ | `local` 以外 | 予約（構成 A は使わない。人の決定 10/8） |
| `sh12jk-nz210c-a2dp-unoq` | NZ-210C の送信機 → UNO Q の A2DP シンク（PipeWire）→ `pw-record` の標準出力 → ssh → PC | `local` 以外 | #37 の構成 B（#38、docs/decisions/0030） |
| `sh12jk-wired-pc` | SH-12JK を PC のマイク端子に挿す（PC 内蔵の入力） | `local` | 予約 |
| `sh12jk-nz210c-rx-usbaudio` | NZ-210C の受信機 → PC に挿した USB オーディオアダプタ | `local` | 予約（PC がアダプタを認識しない） |

- `throat` の各キー: `host`（ssh の宛先か `local`）、`device`（`alsa` の iface（下の構成 B 以外）では ALSA の PCM 名。許す形は hw/plughw の CARD/DEV 指定だけ（0028 の決定 2 を守るため）。
  `a2dp` の iface（`sh12jk-nz210c-a2dp-unoq`）では PipeWire の `bluez_input.<アドレス>.<番号>` だけ（0030）。hw / plughw は `alsa` の iface だけ）、`format`・`channels`・`rate_hz`（受けた形式）、`remote_command`（UNO Q に渡した文字列。
  `local` では `null`）、`alsa_buffer_size`・`alsa_period_size`（`arecord -v` の値。拾えなければ `null`）、`first_byte_wait_s`（子の起動から最初のバイトまで）、
  `frames`（`throat.wav` のフレーム数）、`elapsed_s`（最初の到着から最後の到着までの PC の時間）、`est_diff_frames`（推定差。
  `(elapsed_s × 48000 + 最初の読み取りのフレーム数) − frames`。到着の揺れ・滞留・クロックの差を含む診断値で、取りこぼしの数そのものではない）、
  `overrun_lines`（`arecord` の標準エラーで `overrun` を含む行の数）、`stop`（`duration` / `interrupt` / `child-exit`）、`child_returncode`、
  `mixer`（`amixer -c <CARD> sget Mic` の標準出力そのまま。読み取りのみ。読めなければ `null` で、理由を `mixer_error` に書く）。
- 構成 B（`a2dp` の iface。docs/decisions/0030）で変わる・足すキー: `device_source`（`arg` / `env` / `auto`。`--throat-device` で指した、環境変数 `THROAT_DEVICE` で指した、
  `pw-dump` から自動で探した。`a2dp` だけ。`alsa` では書かない）、`overrun_lines` は `null`（`pw-record` は overrun の行を出さない。0 ではない）、
  `alsa_buffer_size`・`alsa_period_size` は `null`、`stop` に `stall`（受信が 2 秒止まって記録を止めた。使わない）、`mixer` は `null`（`mixer_error` は「A2DP の経路ではミキサーを読まない」）、
  `pipewire`（子を起動する前と後の PipeWire の確認。読み取りのみ）:
  `profile`・`codec`（`api.bluez5.profile`・`api.bluez5.codec`）、`node_rate_hz`・`node_channels`・`node_format`（ノードの形式 `params.Format` の値。SBC の符号化のレートと同じとは限らない。
  無ければ `null`）、`codec_rate_hz`（SBC の符号化のレート。裏が取れたときだけ。分からなければ `null`）と `codec_rate_note`（その理由）、`mute`・`channel_volumes`（ノードの `params.Props`）、
  `driver`（ノードのドライバのノード名。分からなければ `null`）、`links`（起動前に目的のノードの出力がつながっていた先のノード名。再生側への自動リンク）、
  `config`（`wireplumber_conf_d` = `~/.config/wireplumber/wireplumber.conf.d` の一覧、`etc` = `/etc/pipewire`・`/etc/wireplumber` の一覧。一覧を残すだけで中身の安全は保証しない）、
  `link_check`（`ok` / `ng` / `error`。起動後に `node.name` = `zm-throat-record` のストリームがちょうど 1 つあり、目的のノードにだけつながっていれば `ok`）と `link_check_note`（理由。`ok` なら空）。
  BD アドレスを別のキーには入れない（`device` の名前に含まれる分だけ）。

構成 B の `meta.json` の例（`<addr>` は実物では BD アドレス。`meta.json` は `data/raw/` にだけあり、コミットしない）:

```json
{
  "subject": "self",
  "cond": "water",
  "position": "lateral-below-cricoid",
  "band": "elastic-25mm",
  "firmware_sha": "abc1234",
  "sample_rates": {"imu_hz": 0, "audio_hz": 48000, "analog_hz": 0},
  "sensors": [{"id": "throat", "part": "SH-12JK", "iface": "sh12jk-nz210c-a2dp-unoq"}],
  "notes": "",
  "fw": "pc-throat",
  "throat": {
    "host": "arduino@unoq.local",
    "device": "bluez_input.<addr>.2",
    "device_source": "auto",
    "format": "S16_LE", "channels": 2, "rate_hz": 48000,
    "remote_command": "exec env XDG_RUNTIME_DIR=/run/user/$(id -u) pw-record --target bluez_input.<addr>.2 --rate 48000 --channels 2 --format s16 -P '{ node.dont-reconnect = true node.dont-fallback = true node.name = zm-throat-record }' --raw -",
    "alsa_buffer_size": null, "alsa_period_size": null,
    "first_byte_wait_s": 1.2,
    "frames": 4800000, "elapsed_s": 100.0, "est_diff_frames": -300,
    "overrun_lines": null,
    "stop": "duration", "child_returncode": 255,
    "mixer": null, "mixer_error": "A2DP の経路ではミキサーを読まない",
    "pipewire": {
      "profile": "a2dp-source", "codec": "sbc",
      "node_rate_hz": 48000, "node_channels": 2, "node_format": "S16LE",
      "codec_rate_hz": null, "codec_rate_note": "SBC のレートは確かめていない",
      "mute": false, "channel_volumes": [1.0, 1.0],
      "driver": "<ドライバのノード名>",
      "links": ["<再生側のノード名>"],
      "config": {"wireplumber_conf_d": ["90-bluez-no-seat.conf"], "etc": ["<一覧>"]},
      "link_check": "ok", "link_check_note": ""
    }
  }
}
```

- 構成 B の追記（`device` の形、`device_source`、`stall`、`pipewire`）も既存のファイル・列・単位・フレーム形式を変えていないので、記録済みのセッションの変換は要らない（docs/decisions/0030）。

## 装置（UNO Q）のセッション（`fw` = `unoq-detector`。#47）

`tools/record.py --unoq-detector` が UNO Q の `~/zaitaku-mealpace-device/detector.py --live` を ssh で起動し、標準出力の行を受けて書く。
検出器は音声をディスクに書かず、標準出力・標準エラー・RPC に波形を出さない（CLAUDE.md、docs/decisions/0025・0035）。最初の D 行が届いたときにフォルダを作る。

### 時計

- **装置の時計**: 検出器の起動からの ms（UNO Q の `CLOCK_MONOTONIC`）。`detect.csv`・`feat.csv` の `t_ms`・`window_t_ms` と `events.csv` の `t_ms` はこの時計。
- `window_t_ms`（窓 k）= `round(anchor + 250 k)`。`anchor` は、最初の窓を出すまで（入力の最初の 1.0 秒）に届いた入力のブロック i ごとの
  `c_i = 到着時刻 − 1000 × (そのブロックの最後の標本までの累計の標本数) ÷ 48000` の最小（下側の包絡）で、以後は動かさない。窓は入力の先頭から
  16 kHz の 4000 標本ごと（標本の数から決まるので 0.25 秒の等間隔）。`o start` は使わない（PC の評価の格子とは別）。
- 測れる範囲（`meta.json` の `unoq_detector.end`）: `anchor_spread_ms`（最初の 1.0 秒の `c_i` の最大 − 最小）、`clock_drift_min_ms`・`clock_drift_max_ms`
  （記録の全体の `c_i` の最小・最大 − `anchor`。到着の揺れと、単調時計と ALSA の標本の時計のずれの合計で、2 つは区別できない）。
- 測れない部分: ALSA・USB オーディオ・arecord の中で標本が取られてからパイプに出るまでの**固定の遅れ**（全ブロックに共通なので包絡にも drift にも
  現れない）。見込みは arecord の 1 ピリオド（125 ms）程度まで（**推測**。#47 では測らない）。この遅れの分、窓の時刻は真の時刻より遅い側にずれる。
- **PC の時刻**: すべて `pc_ms` = PC の単調時計の、最初の D 行を受けた時刻からの ms（`detect_rx.csv` の `rx_ms`）。2 つの時計は合わせていない。
  `offset` = それまでに受けた全 D 行の `(rx_ms − t_ms)` の最小（下側の包絡。真の時計の差 ＋ 片方向の遅れの最小 `d_min` に近い）。
- `events.csv` の `t_ms` = キーを受けた `pc_ms` − そのときの `offset`（装置の時計に直した値）。最初の D 行より前のキーは記録しない。
  最後の `offset` は `meta.json` の `clock_offset_ms`、キーを押した時点の `offset` と最後の値の差の最大は `marker_offset_shift_max_ms`。

### ファイル

- `detect.csv`: 1 行 = 1 窓（1.0 秒、ホップ 0.25 秒）。列と順は M2 と同じ。値は D 行のまま。
  - `t_ms`: 装置がその窓の行を出力のキューに入れる直前の時刻（装置の時計）。
  - `positive`: **0038 の後処理（ヒステリシス enter 0.85・exit 0.65 ＋ 不応期 0.5 秒）の出力**。M2 の「`prob >= threshold`」とは意味が違う（`meta.json` に `threshold` は無い）。
  - `prob`: 3 つの種の CNN の確率の算術平均（0〜1、9 桁の有効数字）。`led`: 0018 の規則で装置が計算した `1` / `2`（`--no-rpc` でも計算して書く。消灯 `0` は行に出ない）。
    LED の表示は嚥下の「目安」で、評価には使わない。
  - `e2e_ms` = `t_ms − (window_t_ms + 1000)`: 装置の同じ時計での、窓の終端から行をキューに入れるまでの遅れ（パイプや ALSA に溜まっていた時間を含む。
    上の固定の遅れの分だけ小さく出る）。`meta.json` の `e2e_ms`（中央値・最大）。
- `feat.csv`（`--feat` のとき）: `t_ms`（同じ窓の `detect.csv` の `t_ms`）、`window_t_ms`、`f0 … f81`。FEAT の 82 次元（`feature_set` = `m4-0037-feat82`、
  docs/decisions/0037 の 4）。窓（16 kHz・16000 標本）にプリエンファシス → 25 ms の Hamming・10 ms のホップ・FFT 512 の `|FFT|² ÷ 512` を `P`（98 × 257）として:
  - `mel_mean_00`〜`mel_mean_39`: `L = log(P @ MEL40ᵀ + 1e-10)` を float32 にしたもの（= `mfe40`）の時間方向の平均。
  - `mel_std_00`〜`mel_std_39`: 同じ `L` の時間方向の標準偏差（÷ n）。
  - `log_energy_mean`・`log_energy_max`: フレームごとの `log(P の行の和 + 1e-10)`（自然対数）の平均・最大。
  - 正規化の前の値。窓より細かい時間の値・CNN の重み・中間層の出力を含めない。9 桁の有効数字。名前は `meta.json` の `feature_names`（この順）。
    生の計測データとして扱い、`data/raw/` の外に出さない・コミットしない。
- `detect_rx.csv`: 1 行 = 1 窓。`window_t_ms`、`rx_ms`（その窓の D 行を PC が受けた `pc_ms`）、`proc_us`・`lat_us`・`queue`・`backlog`（同じ窓の T 行）。
  `prob`・`positive`・`led` を含めない。
  - `proc_us`: 窓 k で新しく入った 4000 標本（16 kHz の番号 `[4000 k + 12000, 4000 k + 16000)`）の入力処理（`/ 32768`・間引き・リングへの書き込み。
    ブロックが 2 つのホップにまたがるときは標本の数の割合で分ける）＋ 窓の処理（特徴量・FEAT・CNN × 3・平均・後処理・LED・行の投入）＋ RPC の往復
    （`--no-rpc` なら 0）。0040 決定 6 の「1 ホップ全体」。
  - `lat_us`: 窓 k の最後の標本を運んだブロックの到着から T 行を入れるまで（前の窓の処理を待った分を含む）。
  - `queue`: T 行を入れる時点の出力のキューの行数。`backlog`: その時点で、読んだブロックの中でまだ処理していない 16 kHz の標本の数。
- 受信の遅れ（docs/decisions/0037 の 5 の条件 4 の材料。当て方は評価用 `meal` の前に人が 0037 に追記する）:
  `delay_meas = (rx_ms − clock_offset_ms) − (window_t_ms + 1000)`（下側の値）、
  `delay_upper = delay_meas + rtt_max_ms`（`rtt_max_ms` = 記録の前後に同じ宛先へ ssh の `cat` で測った往復時間の最大。片方向の遅れは往復時間を超えない
  ので推定の上限とする。記録の間の遅れを必ず上回る保証は無い）。`meta.json` の `rx_delay_ms`（`meas`・`upper` の中央値・最大）。

### meta.json（`fw` = `unoq-detector`）

共通のキー（`subject`・`cond`・`position`・`band`・`firmware_sha`・`notes`）に加えて:
`fw` = `unoq-detector`、`sample_rates` = `{"imu_hz": 0, "audio_hz": 48000, "analog_hz": 0}`（取り込み）、
`sensors` = `[{"id": "throat", "part": "SH-12JK", "iface": "sh12jk-wired-unoq-usbaudio"}]`、`window_ms` 1000、`hop_ms` 250、`m4_config`（0038 の M4-CONFIG）、
`model`（`{"path", "sha256"}`）、`feature_set`・`feature_names`（`--feat` のとき）、`unoq_detector`（`header` = H 行、`end` = E 行（来なければ null））、
`host`・`device`・`remote_command`、`mixer`（`amixer sget` の出力）、`first_line_wait_s`、`bad_lines`（読めない・形や順の違う行の数。書かない）、
`stop`（`interrupt` / `duration` / `child-exit` / `error`）、`child_returncode`、`rpc`（`--no-rpc` なら false）、
`ssh_rtt_ms`（`start`・`end` の `n`・`median_ms`・`min_ms`・`max_ms` と、大きいほうの `max_ms`）、`rx_delay_ms`、`e2e_ms`、
`proc_ms`・`lat_ms`（`n`・`median`・`p95`・`max`）、`queue_max`・`backlog_max`、`clock_offset_ms`、`marker_offset_shift_max_ms`、
`line_gaps`（D 行が 1.0 秒以上届かなかった回数 `n` と最長 `max_s`）、`windows`・`windows_lost`、
`heartbeat`（`interval_s` 0.5・`timeout_s` 2.0・書いた数 `sent`・書けなかった数 `write_errors`）。`threshold` は書かない。
- ハートビート: `record.py` は検出器の標準入力に 0.5 秒ごとに改行 1 バイトを書く（音声ではない）。検出器（ライブ）は 2.0 秒以上何も届かなければ消灯して
  `heartbeat-timeout` で止まる（PC の Wi-Fi が切れたとき。人の決定 10/29）。

### 検出器の標準出力の行（`detector.py`。1 行 = 空白区切り）

- `H <JSON>`（最初に 1 回）: `fw`、`m4_config`、`model_sha256`、`code_sha256`（配った `.py`・`.npy`・`.npz` を名前の順に「`<名前> <sha256>`」の行に並べた文字列の
  sha256）、`window_ms`、`hop_ms`、`input`（`live` / `replay`）、`device`、`rpc`、`python`・`numpy`・`blas`・`threads`、`heartbeat_s`・`heartbeat_timeout_s`（ライブのとき。0.5・2.0 秒）、
  `feature_set`・`feature_names`（`--feat` のとき）。
- `F <window_t_ms> <f0> … <f81>`（`--feat` のとき、D 行の直前）。数の配列を出すのはこの行だけ（長さ 82）。
- `D <window_t_ms> <t_ms> <prob> <positive> <led>`（窓ごと）。
- `T <window_t_ms> <proc_us> <lat_us> <queue> <backlog>`（窓ごと、同じ窓の D 行と RPC の後。整数）。
- `E <JSON>`（終わりに 1 回）: `windows`、`stop`（`eof` / `signal` / `overrun` / `stdout-stall` / `stdout-closed` / `heartbeat-timeout` / `error`）、`rpc`、`overrun_lines`、
  `rpc_errors`、`anchor_spread_ms`、`clock_drift_min_ms`、`clock_drift_max_ms`、`queue_max`、`cpu_s`、`arecord_cpu_s`、`wall_s`。
- H・E の値は数・文字列・真偽・文字列の配列だけ。再生モード（`--replay`。標準入力の音声。一致の確かめだけ）では `anchor` = 0 で、時計の診断値に意味は無い。

## シリアルのフレーム形式（装置 → PC）

```
[0xA5 0x5A] [stream_id u8] [len u16 LE] [t_ms u32 LE] [payload len bytes] [xor u8]
```

- `stream_id`: `0x01` IMU（float32 × 6）, `0x02` AUDIO（int16 × N）, `0x03` ANALOG（uint16 × N）, `0x04` DETECT, `0x05` FEAT, `0x7F` META（UTF-8 JSON）
- ヘッダの `t_ms` は、どのストリームでも装置がそのフレームを送るときの `millis()`。`tools/record.py` のマーカーは直前に届いたフレームのこの値を使う
- `0x04` DETECT（検出器のみ。窓ごとに 1 フレーム、ペイロード 10 B）:
  `window_t_ms` u32 LE（窓の開始）、`prob` float32 LE（`swallow` の確率）、`positive` u8（0 / 1）、`led` u8（0 / 1 / 2）
- `0x05` FEAT（検出器のみ。窓ごとに 1 フレーム、ペイロード 4 + 4N B）:
  `window_t_ms` u32 LE、`f0 … f(N−1)` float32 LE × N（正規化前の特徴量。順序は `meta.json` の `feature_names`）。N はペイロードの長さから決まる
- 検出器ファームウェアは `0x02` AUDIO を送らない（生の音声波形を保存も送信もしない。docs/decisions/0005）。送らないことは装置側のコードで示す（#24）。
  PC 側は `0x02` が来れば今までどおり書く（記録ファームウェアと同じ受信経路）
- 帯域の目安: 記録ファームウェアは約 36 KB/s、検出器は N = 29 で 1 ホップ 150 B（DETECT 20 B ＋ FEAT 130 B）、4 Hz で 600 B/s
- `xor`: stream_id〜payload の XOR
- ストリームを足すときは `stream_id` を増やす。PC 側は未知の ID を読み飛ばす

PC → 装置: 検出器は 1 バイト `M`（0x4D）を受けると META を送り直す。フレームではない。他のバイトは捨てる。記録ファームウェアは受けない。

## 変更ルール

この文書を変えるときは `docs/decisions/` に記録を残す。記録済みのセッションは変換スクリプトで新形式に揃える。
DETECT / FEAT と `detect.csv`・`feat.csv`・検出器の `meta.json` の追記は既存のファイル・列・単位・フレーム形式を変えていないので、
記録済みのセッションの変換は要らない（docs/decisions/0021）。
開始時の合図（PC → 装置の `M`、docs/decisions/0006 の追記）もファイル・列・単位・フレーム形式を変えていないので、変換は要らない。
咽喉マイクのセッション（`throat.wav`・`throat_chunks.csv`・`fw` = `pc-throat`）の追記も既存のファイル・列・単位・フレーム形式を変えていないので、
変換は要らない（docs/decisions/0029）。
装置のセッション（`fw` = `unoq-detector`、`detect_rx.csv`。#47）の追記も既存のファイル・列・単位・フレーム形式を変えていないので、変換は要らない。
