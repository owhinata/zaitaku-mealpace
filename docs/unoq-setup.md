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

## 残した設定（音声ではない）

| 場所 | 中身 | 置いた人 | 理由 | 確かめ方（読み取り） |
|---|---|---|---|---|
| `/etc/bluetooth/main.conf` | `Class = 0x240414`（Audio/Video・Loudspeaker） | 人（sudo） | 送信機が Audio/Video の機器だけを探すため | `grep -n '^Class' /etc/bluetooth/main.conf`、`bluetoothctl show` の Class |
| `/etc/bluetooth/main.conf` | `JustWorksRepairing = always` | 人（sudo） | 送信機は電源を入れるたびに新規ペアリングで来る。既定（`never`）では鍵が残っていると拒む。**人が適用すると決めた（10/8）。適用の確認（E9）とエージェントの要否は未確認** | `grep -n JustWorksRepairing /etc/bluetooth/main.conf` |
| `/var/lib/lightdm/.config/wireplumber/wireplumber.conf.d/90-disable-bluez.conf` | lightdm の WirePlumber で bluez の監視を止める | 人（sudo） | A2DP のエンドポイントは先に登録した WirePlumber が持ち、ログイン画面（lightdm）側に音が流れていた | `ls` |
| `~/.config/wireplumber/wireplumber.conf.d/90-bluez-no-seat.conf`（arduino） | `wireplumber.profiles.main.monitor.bluez.seat-monitoring = disabled` | メイン（ssh、sudo 不要） | ssh のセッションは seat0 の active ではなく、arduino の WirePlumber が bluez の監視を始めない | `cat` |

- ここに無い PipeWire・WirePlumber の設定を足さない（docs/decisions/0030「塞がっていない経路」）。`record.py` は構成 B の記録の前に、`~/.config/pipewire` と
  `~/.config/wireplumber/wireplumber.conf` が無いこと、`~/.config/wireplumber/wireplumber.conf.d/` が `90-bluez-no-seat.conf` だけで中身が上の 1 行（とコメント・空行）だけであることを確かめ、
  違えば記録を始めない。`/etc/pipewire`・`/etc/wireplumber` は一覧を `meta.json` に残すだけ（中身は確かめない）。

## 構成 B の接続（記録の座の初めに 1 回）

送信機を切らなければ、5 条件の間は接続が保たれる見込み。送信機は接続が無いと約 3 分で自動 OFF。

### 主の手順（`JustWorksRepairing = always` を適用した後）

1. 人が `/etc/bluetooth/main.conf` の `[General]` に `JustWorksRepairing = always` を書き、bluetooth を再起動する（sudo。1 回だけ）。
   適用の確かめ（読み取り）: `grep -n JustWorksRepairing /etc/bluetooth/main.conf`。
2. UNO Q の電源を入れ、ssh が通ることを確かめる。受信機 NZ-W210R は電源を切ったまま。
3. 送信機の電源を入れる（ペアリングモードで来る）。鍵の削除（`bluetoothctl remove`）は要らない見込み。
4. `record.py` を起動する。子を起動する前に `pw-dump` で接続を確かめる。ノードが無ければ「送信機が接続されていません」と出て止まるので、下のエージェントを起動して 3 からやり直す。

   ```
   .venv/bin/python tools/record.py --throat sh12jk-nz210c-a2dp-unoq --position lateral-below-cricoid --cond quiet --duration 100
   ```

- **エージェントの要否は分からない**（bluez が Just Works の確認をエージェントなしで受けるか。適用の後に確かめて、ここと log に書く）。要るなら 3 の前に次を起動したままにする
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

- 子を起動する前（1 回の ssh。読み取りのみ）: ユーザーの PipeWire・WirePlumber の設定（上）、目的のノード（`bluez_input.<addr>.<番号>`、`Audio/Source`、`a2dp-source`）の有無、
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

## 構成 A は使わない

送信機 → 受信機 NZ-W210R → USB オーディオ → UNO Q（構成 A）は使わない（人の決定、10/8）。iface `sh12jk-nz210c-rx-unoq-usbaudio` は予約のまま。
