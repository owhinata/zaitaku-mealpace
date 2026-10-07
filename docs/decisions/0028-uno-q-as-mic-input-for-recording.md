# 0028 咽喉マイクの学習用の記録は、UNO Q に挿した USB オーディオアダプタから生の音声を PC に流し、PC が書く（記録専用の経路。検出器とは別）

日付: 2026-10-07　状態: 採用

人が 10/7 に方針を示し（PC が USB オーディオアダプタ UGREEN CM721 を認識せず、UNO Q は認識する。録音の作業は UNO Q で行いたい）、同日にこの記録を採用した。
CLAUDE.md・AGENTS.md・codex-review skill の差し替えは人の依頼でメインのエージェントが同じコミットで行った（下の「CLAUDE.md・AGENTS.md との関係」）。
#36 は試用ブランチを切らず main で行う（#28 は 10/4 に判定済み。docs/status.md「試用ブランチの作業は main に入れてよい（#28 の後。0024）」）。
docs/decisions/0025「データの扱い（検出器）」は「UNO Q を通した生の音声の記録が要ることになったら、検出器とは別のプログラムとして、先に `docs/decisions/` に記録する」と
決めていた。この記録はその手続きに当たる。`docs/evaluation.md`・`docs/data-schema.md`・`tools/`・`analysis/` はこの記録では変えない（#36 の plan で直す）。

## 決定

### 記録の経路

```
 [咽喉マイク SH-12JK] ─3.5 mm─ [USB オーディオアダプタ UGREEN CM721] ─USB─ [UNO Q（Linux 側）]
                                                                                 │ arecord（Debian 標準のコマンド。ディスクに書かず標準出力へ）
                                                                                 │ ssh（有線または Wi-Fi の LAN）
                                                                           [PC] tools/record.py が受けて data/raw/<session>/throat.wav に書く
```

1. 咽喉マイクの学習用の `self` の生の音声（開発用の計測。0005 の記録ファームウェアに当たるもの）は、UNO Q に挿した USB オーディオアダプタから取り、PC が `data/raw/` に書く。
   0025 の「USB オーディオアダプタを PC に挿して PC で録る」のうち、アダプタの差し込み先を UNO Q に変える。**書く側は PC のまま。**
2. UNO Q 側で動くのは `arecord`（alsa-utils。Debian 13 標準）だけ。生の PCM を標準出力に出し、ssh 越しに PC の `tools/record.py` が受ける。
   UNO Q 側にこのプロジェクトのプログラムは置かない。UNO Q のディスクには音声を書かない（WAV・raw・一時ファイルとも）。
3. この経路は**記録専用**で、検出器（0025 の Linux 側のプログラムと MCU 側のスケッチ）とは別のものとして扱う。検出器のプログラムに録音の機能を持たせない、
   検出器の Linux 側が音声をディスク・ログ・標準出力・ネットワーク・RPC に出さない、という 0025 の決まりは変えない。
   同じ UNO Q で検出器と記録の両方を動かすことはしない（記録の間、検出器のプログラムは動かさない）。
4. 被験者は `self` のみ。`p1` にこの経路を使うかは本人試用の前に別に決める（0024）。
5. PC 側の生波形の扱いは今までどおり（CLAUDE.md「データの扱い」の被験者ごとの規則。`data/raw/` 固定、コミットしない。0007）。

### 実測（10/7。ssh `arduino@unoq.local`、鍵認証）

- UNO Q: Debian 13、Python 3.13（numpy・pip なし）、alsa-utils 1.2.14。NTP 同期あり。
- UGREEN CM721 は ALSA の card 0（`hw:CARD=Audio,DEV=0`）。ハードウェアの対応は **48000 Hz・2 ch・S16_LE 固定**（`--dump-hw-params`）。
  16 kHz・モノラルは `plughw` の変換（ALSA の plug）で取れる。Mic Capture Volume は最大 58（+4.00 dB）。
- `ssh arduino@unoq.local 'arecord -D plughw:CARD=Audio,DEV=0 -f S16_LE -r 16000 -c 1 -t raw -d 3 -q -'` で 3.0 秒ぶん（48000 サンプル）が PC に届いた。
  ssh の立ち上がりを含めた所要は約 3.6 秒。
- PC（HP のノート）の USB にアダプタは現れない（人の報告。`lsusb` にも無い）。原因は追っていない。

### `meta.json` の `sensors.iface`（案。#36 の plan で確定）

- `sh12jk-wired-unoq-usbaudio`: SH-12JK を有線で、UNO Q の USB オーディオアダプタに挿す（この記録の経路）。
- `sh12jk-nz210c-rx-unoq-usbaudio`: NZ-210C の受信機の出力を、UNO Q の USB オーディオアダプタに入れる。
- `sh12jk-nz210c-a2dp-unoq`: NZ-210C の送信機を UNO Q が A2DP で直接受ける（#38）。
- #36 の Issue 本文の `sh12jk-wired-pc`・`sh12jk-nz210c-rx-usbaudio` は、PC がアダプタを認識しないので使わない（名前は予約のまま残してよい）。

### plan #36 の実測の追加（10/7、plan 担当の subagent。UNO Q では読み取りと標準出力への `arecord` だけ）

- hw の 48 kHz・2 ch で 8 秒: バイト数は 8.000 秒分ちょうど。L と R は全サンプルで同一。`arecord` の既定はバッファ 0.5 秒・ピリオド 125 ms。overrun 0。
- `-d` なしで PC 側から止める（SIGTERM・SIGINT・パイプを閉じる）: ssh は 255 で終わり、1 秒後の UNO Q に `arecord` は残らない。
- 60 秒・180 秒の連続受信: overrun 0、バイト数の不足なし。クロックの差 −8 ppm、到着の揺れは 95 パーセンタイル 9 ms・最大 35 ms。PC・UNO Q とも Wi-Fi。

## CLAUDE.md・AGENTS.md との関係（人の依頼でメインが差し替えた。10/7）

CLAUDE.md「データの扱い」の次の 1 文は、この記録と合わなかった。

> 咽喉マイクの学習用の記録は、USB オーディオアダプタを PC に挿して PC で録る（開発用の計測。docs/decisions/0025）。

差し替えの案:

> 咽喉マイクの学習用の記録は、UNO Q に挿した USB オーディオアダプタから `arecord` の生の PCM を ssh で PC に流し、PC で録る（開発用の計測。記録専用の経路で、
> 検出器のプログラムとは別。UNO Q のディスクには書かない。docs/decisions/0025・0028）。

同じコミットで `AGENTS.md` の不変条件 4 の同じ文と、`.claude/skills/codex-review/SKILL.md` 観点 1「生データ」に「記録専用の経路（0028）は検出器に当たらない」を足す。
`docs/workflow.md`「CLAUDE.md を変えたら `AGENTS.md` も同じコミットで直す」のとおり。順序は 0025 と同じ（#36 の plan の Codex レビューより前に差し替える。
差し替え前にレビューを回すと、観点 1 で BLOCKING になる）。

## 理由

- PC がアダプタを認識しない（10/7、人の報告）。UNO Q は認識し、`arecord` で取れることを実測した。
- 書く側を PC に残すと、`data/raw/` 固定（0007）、`tools/record.py` のマーカーとセッションの形式、`analysis/` の読み口をそのまま使える。
  UNO Q に書くと、`p1` の規則（暗号化ディスク）や `data/raw/` の外に生データが出ない決まりを UNO Q 側にも作り直す必要がある。
- UNO Q 側を標準のコマンドだけにすると、「検出器は音声を出さない」の検査の対象（このプロジェクトのプログラム）が増えない。

## 選ばなかった案

- **UNO Q の中で WAV に書き、後で PC に転送する。** 0025「UNO Q の Linux 側は音声をディスクに書かない」を記録の場面でも破ることになり、`data/raw/` の外に生データが残る。
- **PC で別の USB オーディオアダプタを探す。** 人の方針（UNO Q で録る）に合わない。認識しない原因も分かっていない。
- **UNO Q の Linux 側に記録用の Python を置く。** numpy・pip が無く、UNO Q 側に置くコードが増える。`arecord` で足りる。
- **UNO Q 側で 16 kHz に落として送る（`plughw`）。** ALSA の plug の変換の方式が記録に残らない。48 kHz のまま送って PC で落とすほうが後から追える
  （どちらにするかは #36 の plan で実測して決める）。

## 確かめていないこと

- マイクを挿した状態でアダプタがプラグインパワーを出し、SH-12JK の信号が取れるか（#37）。10/7 の実測はマイクの有無を確かめていない（RMS は 16 ビットで 6 程度）。
- 3 極プラグを 4 極（ヘッドセット）のジャックに挿したときの結線（マイクの線が接地に落ちないか）。#37 で信号が見えなければここを疑う。
- ssh 越しの生ストリームの取りこぼし（`arecord` の overrun）と、長時間（180 秒・1800 秒）での安定。LAN が Wi-Fi のときの揺れ。
- マーカー（PC の時計）と `throat.wav` のサンプル位置の合わせ方と誤差（#36 の plan で叩きを使って決める）。
