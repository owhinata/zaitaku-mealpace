# 0030 咽喉マイクの記録の経路に構成 B（NZ-W210C の送信機 → UNO Q の A2DP シンク → `pw-record` の標準出力 → ssh → PC）を足す（記録専用。検出器とは別）

日付: 2026-10-08　状態: 採用

人が 10/8 に決めた（構成 B だけを使い、構成 A は使わない。docs/log/2026-10-08.md「人の決定」。この記録の採用も同日）。
CLAUDE.md・AGENTS.md・codex-review skill の差し替えは、人の決定によりメインのエージェントが同じコミットで行う（下の「CLAUDE.md・AGENTS.md との関係」）。
plan #38 の Codex のレビューは、このコミットの後に回す。

docs/decisions/0025「データの扱い（検出器）」は「UNO Q を通した生の音声の記録が要ることになったら、検出器とは別のプログラムとして、先に `docs/decisions/` に記録する」と
決めていた。0028 は USB オーディオアダプタの経路についてその手続きをとった。この記録は、Bluetooth の A2DP で受ける経路（構成 B）について同じ手続きをとる。
`tools/record.py`・`tools/throat_check.py`・`docs/data-schema.md` はこの記録では変えない（plan #38 で直す）。`docs/evaluation.md`・`analysis/`・`firmware/` は変えない。

## 決定

### 記録の経路

```
 [咽喉マイク SH-12JK] ─3.5 mm─ [NZ-210C の送信機 NZ-W210C]
                                         │ Bluetooth A2DP（SBC）。受信機 NZ-W210R は使わない
                                  [UNO Q（Linux 側）] bluez → PipeWire のノード bluez_input.<addr>.N（a2dp-source）
                                         │ pw-record（PipeWire 標準のコマンド。ディスクに書かず標準出力へ）
                                         │ ssh（有線または Wi-Fi の LAN）
                                   [PC] tools/record.py が受けて data/raw/<session>/throat.wav に書く
```

1. 咽喉マイクの学習用の `self` の生の音声（開発用の計測。0005 の記録ファームウェアに当たるもの）を、NZ-210C の送信機から UNO Q が A2DP で受け、PC が `data/raw/` に書く経路を足す。
   0028 の経路（USB オーディオアダプタ＋`arecord`）は残す。**書く側は PC のまま。**
2. UNO Q 側で動くのは `pw-record`（PipeWire 1.4 の標準のコマンド）と、読み取りのコマンド（`pw-dump`、`ls`、`test`）だけ。`pw-record` は生の PCM を標準出力に出し、
   ssh 越しに PC の `tools/record.py` が受ける。出力先は標準出力（`-`）だけに固定し、ファイル名・リダイレクトを含めない。
   UNO Q 側にこのプロジェクトのプログラムは置かない。UNO Q のディスクには音声を書かない（WAV・raw・一時ファイルとも）。
3. 取り出す相手は PipeWire の bluez のノード（`bluez_input.<アドレス>.<番号>`、プロファイル `a2dp-source`）だけ。`tools/record.py` の `--throat-device` は、
   構成 B の iface（`sh12jk-nz210c-a2dp-unoq`）ではこの形だけを受け、USB の iface では今までどおり hw / plughw だけを受ける（iface と形を結ぶ）。
   目的のノードが無いときに既定の入力へ落ちない、記録の途中で切れたときに別の入力へつながり直さない設定で起動する。
4. 子を起動する前に、PipeWire の状態を読み取りで確かめ（ノードの有無・プロファイル・ミュート・他の取り込みのストリーム・音をファイルに流すモジュール・
   ユーザーの PipeWire / WirePlumber の設定）、当たれば記録を始めない。起動後に、`pw-record` が目的のノードにだけつながっていることを確かめて記録に残す。
   受信が一定時間止まったら記録を止め、使わない。細部（閾値、キー名）は plan #38 と `docs/data-schema.md`。
5. 構成 B では、Bluetooth の途切れが「ちょうど 0 の区間」として現れ、フレーム数と推定差（0029）に出ない場合がある。`tools/throat_check.py` に 0 の区間の検査を足し、
   構成 B の記録の「使わない目安」に入れる。閾値と規則は、構成 B の最初の記録の前に実測を見て人が決め、`docs/log/` に固定する（結果を見てから緩めない）。
6. `throat.wav` の形式は 0029 のまま（48 kHz・2 ch・int16）。SBC の実際のレートが 48 kHz でなければ PipeWire が 48 kHz に変換したものになるので、実際のレートとコーデック・プロファイル・
   ノードの音量を `meta.json` の `throat.pipewire` に残す。`amixer` は読まない。
7. この経路は**記録専用**で、検出器（0025 の Linux 側のプログラムと MCU 側のスケッチ）とは別のものとして扱う（0028 決定 3 と同じ）。同じ UNO Q で検出器と記録の両方を動かさない。
8. 被験者は `self` のみ。無線の試用は `self` の開発用の計測に限る（Issue #38）。`p1` にこの経路を使うかは本人試用の前に別に決める（0024）。
9. 構成 A（送信機 → 受信機 NZ-W210R → USB オーディオ → UNO Q）は使わない（人の決定、10/8）。iface の名前 `sh12jk-nz210c-rx-unoq-usbaudio` は予約のまま残す。

### 0028 との関係

- 0028 の決定 1〜5（書く側は PC、UNO Q にプログラムと音声のファイルを置かない、記録専用、`self` のみ、PC 側の生波形の規則）を構成 B にもそのまま当てる。
- 違いは、入力（USB オーディオアダプタ → Bluetooth の A2DP）と、UNO Q で動く子（`arecord` → `pw-record`）と、起動前の検査（ALSA の設定 → PipeWire の状態と設定）。
- 時計と `throat.wav` の形式、受信の時刻による対応、叩きによる確かめは 0029 のまま。

### 検出器と無線リンクの境界

- 生の音声を無線で送る構成を検出器（食卓で使う形態）で使うか、CLAUDE.md「検出器は生の音声波形を保存も送信もしない」との関係、0025 への追記は **#42 で決める**。
  この記録は記録専用の経路だけを決め、それを先取りしない。

## 塞がっていない経路（CONCERN。運用で注意）

0029「塞がっていない経路」と同じ扱い。どれも UNO Q に ssh できる人（開発者本人）の故意の操作。

- (a) PipeWire・WirePlumber の配布元の設定（`/usr/share/pipewire` など）の書き換え。`record.py` はユーザーの設定（`~/.config/pipewire`、`~/.config/wireplumber`）と
  実行中のモジュールを確かめるが、配布元の設定の書き換えは塞がない。
- (b) `record.py` の外の操作。UNO Q で `pw-record` をファイルに向けて直接起動する、別の取り込みのストリームを後から足す、など。
- 運用の注意: UNO Q に PipeWire・WirePlumber の設定を足さない（下の表のものを除く）。構成 B の記録の後に、UNO Q のホームと `/tmp` に音声のファイルが無いことを
  docs/log/2026-10-08.md の方法（`find` の 2 行。読み取り）で確かめる。

## CLAUDE.md・AGENTS.md との関係（人の決定により、メインが同じコミットで差し替える。10/8）

CLAUDE.md「データの扱い」の次の文は、構成 B を含んでいなかった。

> 咽喉マイクの学習用の記録は、UNO Q に挿した USB オーディオアダプタから `arecord` の生の PCM を ssh で PC に流し、PC で録る（開発用の計測。記録専用の経路で、
> 検出器のプログラムとは別。UNO Q 側はプロジェクトのプログラムを置かず、ディスクに音声を書かない。docs/decisions/0025・0028）。

差し替えの文:

> 咽喉マイクの学習用の記録は、UNO Q に挿した USB オーディオアダプタから `arecord` の生の PCM を、または NZ-210C の送信機から UNO Q が A2DP で受けた音を
> `pw-record` で、ssh で PC に流し、PC で録る（開発用の計測。記録専用の経路で、検出器のプログラムとは別。UNO Q 側はプロジェクトのプログラムを置かず、
> ディスクに音声を書かない。docs/decisions/0025・0028・0030）。

同じコミットで `AGENTS.md` の不変条件 4 の同じ文と、`.claude/skills/codex-review/SKILL.md` 観点 1「生データ」の例外（記録専用の経路）に構成 B（0030）を足す。
`docs/workflow.md`「CLAUDE.md を変えたら `AGENTS.md` も同じコミットで直す」のとおり。順序は 0028 と同じ（plan #38 の Codex レビューより前に差し替える。
差し替え前にレビューを回すと、観点 1 で BLOCKING になる見込み）。

## UNO Q に残した設定（音声ではない。10/8）

| 場所 | 中身 | 置いた人 | 理由 |
|---|---|---|---|
| `/etc/bluetooth/main.conf` | `Class = 0x240414`（Audio/Video・Loudspeaker） | 人（sudo） | 送信機が探す相手を Class of Device で絞っていて、既定（Misc）の UNO Q を候補にしない |
| `/etc/bluetooth/main.conf` | `JustWorksRepairing = always`（人が適用すると決めた、10/8。適用は読み取りで確かめて log に書く） | 人（sudo） | 送信機は電源を入れるたびに新規ペアリングで来る。既定（`never`）では鍵が残っていると拒むので、毎回 `bluetoothctl remove` が要る |
| `/var/lib/lightdm/.config/wireplumber/wireplumber.conf.d/90-disable-bluez.conf` | lightdm の WirePlumber で bluez の監視を止める | 人（sudo） | A2DP のエンドポイントは先に登録した WirePlumber が持ち、ログイン画面（lightdm）側に音が流れていた |
| `~/.config/wireplumber/wireplumber.conf.d/90-bluez-no-seat.conf`（arduino） | bluez の監視を logind の seat に結ばない | メイン（ssh、sudo 不要） | ssh のセッションは seat0 の active ではなく、arduino の WirePlumber が bluez の監視を始めない |

- 版: Debian 13 (trixie) aarch64、bluez 5.82-1.1、PipeWire 1.4.2-1~qcom1（libspa-0.2-bluetooth 1.4.2）、WirePlumber 0.5.8-2。コーデックは SBC。
- 再現の手順と接続の手順は `docs/unoq-setup.md`（plan #38 で書く）。送信機の BD アドレスは文書に書かない（`<addr>`）。

## 理由

- 構成 B は 10/8 に実機で動いた（SBC、`pw-record` の標準出力に 48 kHz・2 ch、マイクの音が乗る。docs/log/2026-10-08.md の #38 の節）。
- 構成 A は、受信機の 6.3 mm 出力を 3.5 mm の入力へつなぐ変換ケーブルが手元に無く、受信機と USB オーディオを挟む分だけ経路が長い（人の決定、10/8）。
- 書く側を PC に残すと、`data/raw/` 固定（0007）、`tools/record.py` のマーカーとセッションの形式、`tools/throat_check.py` と集計の読み口をそのまま使える（0028 と同じ理由）。
- UNO Q 側を標準のコマンドだけにすると、「検出器は音声を出さない」の検査の対象（このプロジェクトのプログラム）が増えない。

## 選ばなかった案

- **構成 A（送信機 → 受信機 NZ-W210R → USB オーディオ → UNO Q）。** 人の決定（10/8）。上の「理由」。
- **UNO Q の中で WAV に書き、後で PC に転送する。** 0025・0028「UNO Q の Linux 側は音声をディスクに書かない」を破り、`data/raw/` の外に生データが残る。
- **PulseAudio の互換の道具（`parec`・`pactl`）で取る。** UNO Q に pulseaudio-utils が無く、入れるとパッケージを足すことになる。`pw-record` は PipeWire に含まれる。
- **ALSA の PCM として出して `arecord` で取る（PipeWire の ALSA の口を経由）。** bluez のノードは PipeWire にあり、ALSA の名前で指すには ALSA の設定（`~/.asoundrc` など）を
  足す必要がある。0029 で「ALSA のユーザー設定が無いこと」を記録の条件にしたのと合わない。
- **PC で直接 A2DP を受ける。** PC の Bluetooth と PipeWire の設定を変えることになり、10/8 に動いた UNO Q の構成から離れる。試していない。

## 確かめていないこと

- 10/8 の 12 秒の確認で出た 3 秒ちょうど 0 の区間の原因（PipeWire の無音の補填か、送信機が入力の小さい間に無音を送るのか、Bluetooth の途切れか）。
  送信機の挙動なら、構成 B の `quiet` の記録を基準に使えるかを人が決める。
- SBC の実際のレート（44.1 kHz か 48 kHz か）と、PipeWire の変換の方式。
- 目的のノードが無いときに既定の入力へ落ちない設定、途中で切れたときにつながり直さない設定が、PipeWire 1.4.2 / WirePlumber 0.5.8 で効くか。
- 長い記録での安定。送信機と UNO Q の距離・向き・体の陰による途切れ。
- 構成 B の時刻の対応の遅れ（送信機の符号化・Bluetooth・PipeWire の遅れ。叩きの `Δ` に出る）。
- 構成 B で L と R が同じ値にならない理由（集計は L を使う。0029）。
- `JustWorksRepairing = always` の下で、承認のエージェントが要るか。
- 接続ごとにノード名の番号が変わるか。
- WirePlumber の既定の自動リンク（bluez のノード → USB オーディオアダプタの再生側）が、グラフの時計と記録に影響するか（設定は変えない）。
