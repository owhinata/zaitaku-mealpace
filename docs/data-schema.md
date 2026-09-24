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

## 共通

- 時刻の基準は装置の `millis()`。すべてのストリームを同じクロックで刻印する。
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

## analog.csv（拡張点）

- 代替センサ（圧電コンタクトマイク、ピエゾ電線センサなど）を ADC で読む場合
- 1〜4 kHz。`ch0..chN` は ADC 生値（12 bit）
- `meta.json` の `sensors` に接続を記録する

## events.csv

- `label`: `s`（嚥下）`t`（発話）`c`（咳）`n`（首の動き）`q`（静止）`b`（一口）`o`（観察: note に内容）
- マーカーは**開始時刻**。区間の終端は後処理で決める（嚥下は開始から 1.5 秒、など）

## detect.csv（検出器のみ）

- 1 行 = 1 窓（1.0 秒、ホップ 0.25 秒。`docs/evaluation.md`）。装置が窓ごとに送る DETECT フレームをそのまま書く
- `t_ms`: フレームの送信時刻（ヘッダの `t_ms`。装置の `millis()`）。`window_t_ms`: 窓 `[window_t_ms, window_t_ms + 1000)` の開始
- `positive`: `1` = `prob` が凍結した閾値（`meta.json` の `threshold`）以上、`0` = 陰性。装置の 2 値の出力そのもの
- `prob`: `swallow` の確率（0〜1。float32 を 9 桁の有効数字で書く）。装置が閾値と比べた値そのもので、PC 側で `positive == (prob >= threshold)` を丸めなしに確かめられる。咳のクラスの確率は載せない（docs/decisions/0019）
- `led`: `0` 消灯 / `1` 黄 / `2` 緑（docs/decisions/0018）。この窓の結果を反映した後の状態。LED は嚥下の目安であり、評価には使わない
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
  "feature_set": "0020",
  "feature_names": ["acc_ptp_x", "..."]
}
```

- `threshold` は #22 で凍結した閾値（装置が持つ float32 の定数の値。有効数字 9 桁）。`positive` は装置が float32 で `prob >= threshold` を評価した結果で、PC 側の
  比較も両方を float32 にして行う。`model` は Edge Impulse の書き出しのプロジェクト ID と deploy version（Private にした場合は 0。docs/decisions/0019）。
  `feature_set` は音の式の記録の番号。`feature_names` は `feat.csv` の `f0 …` の順の名前
- `imu_hz` / `audio_hz` はセンサの取り込みレート。特徴量の計算で音を間引いても、マイクの取り込みは 16000
- 基板をリセットせずに記録を始めると META が届かず、これらのキーが入らない（docs/decisions/0006）。`fw` が `detector` でないセッションは M2 の評価に使わない
  （`docs/recording-protocol.md`）

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

## 変更ルール

この文書を変えるときは `docs/decisions/` に記録を残す。記録済みのセッションは変換スクリプトで新形式に揃える。
DETECT / FEAT と `detect.csv`・`feat.csv`・検出器の `meta.json` の追記は既存のファイル・列・単位・フレーム形式を変えていないので、
記録済みのセッションの変換は要らない（docs/decisions/0021）。
