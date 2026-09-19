# 記録形式（固定）

1 セッション = 1 フォルダ。名前は `YYYYMMDD-HHMMSS_<subject>_<cond>`。

```
data/raw/20260922-193012_self_water/
  imu.csv      t_ms, ax, ay, az, gx, gy, gz
  audio.wav    16 kHz, mono, int16
  analog.csv   t_ms, ch0, ch1, ...      （代替センサ使用時のみ）
  events.csv   t_ms, label, note
  meta.json
```

## 共通

- 時刻の基準は装置の `millis()`。すべてのストリームを同じクロックで刻印する。
- `subject`: `self` | `p1`
- `cond`: `water` | `saliva` | `talk` | `cough` | `neck` | `quiet` | `meal`

## imu.csv

- 104 Hz（LSM6DSOX のネイティブレート）
- `ax, ay, az`: g。`gx, gy, gz`: deg/s。float、小数4桁
- 軸: 基板を「Y 軸が首の上下方向、+Y が頭側」になる向きで装着する

## audio.wav

- 16 kHz, mono, int16。基板 PDM マイク
- 装置から届いた音声チャンクは先頭サンプルの `t_ms` を持つ。WAV には連続波形として書き、
  各チャンクの `t_ms` は `audio_chunks.csv`（`t_ms, sample_index`）に別途残す

## analog.csv（拡張点）

- 代替センサ（圧電コンタクトマイク、ピエゾ電線センサなど）を ADC で読む場合
- 1〜4 kHz。`ch0..chN` は ADC 生値（12 bit）
- `meta.json` の `sensors` に接続を記録する

## events.csv

- `label`: `s`（嚥下）`t`（発話）`c`（咳）`n`（首の動き）`q`（静止）`b`（一口）`o`（観察: note に内容）
- マーカーは**開始時刻**。区間の終端は後処理で決める（嚥下は開始から 1.5 秒、など）

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
  "notes": ""
}
```

## シリアルのフレーム形式（装置 → PC）

```
[0xA5 0x5A] [stream_id u8] [len u16 LE] [t_ms u32 LE] [payload len bytes] [xor u8]
```

- `stream_id`: `0x01` IMU（float32 × 6）, `0x02` AUDIO（int16 × N）, `0x03` ANALOG（uint16 × N）, `0x7F` META（UTF-8 JSON）
- `xor`: stream_id〜payload の XOR
- ストリームを足すときは `stream_id` を増やす。PC 側は未知の ID を読み飛ばす

## 変更ルール

この文書を変えるときは `docs/decisions/` に記録を残す。記録済みのセッションは変換スクリプトで新形式に揃える。
