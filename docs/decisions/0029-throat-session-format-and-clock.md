# 0029 咽喉マイクだけのセッションの記録形式と時計（`throat.wav`・`throat_chunks.csv`・PC の単調時計・`fw` = `pc-throat`）

日付: 2026-10-07　状態: 採用（#36）

plan #36（PLAN-SHA `d590741fcf4c6a26`。Codex の 3 面レビューを通した）による。docs/decisions/0025「後の plan で決める」の 1（時刻の合わせ方）と
2（音声の形式と `meta.json`）に答える。3（UNO Q の検出器が送るもの）と 4（既存の評価器との接続）は #39 以降で決める。
記録の経路（UNO Q の `arecord` → ssh → PC）は docs/decisions/0028。

## 決定

### セッションの形

- `tools/record.py --throat <iface>` で、装置（シリアル）をつながない「咽喉マイクだけのセッション」を録る。既定（`--throat` なし）の動作は変えない。
- ファイル: `throat.wav`、`throat_chunks.csv`（`t_ms, sample_index`）、`events.csv`、`meta.json`。`imu.csv`・`audio.wav`・`audio_chunks.csv` はできない。
- 出力先は今までどおり `data/raw/` の直下だけ（0007）。被験者は `self` のみ（`--subject p1` を受けない。0028 決定 4）。`--cond meal` と `--indicator` も受けない。

### `throat.wav` の形式（48 kHz・2 ch・int16、受けたまま）

- 子が出した S16_LE・48000 Hz・2 ch のバイト列を、変換せずに WAV に書く。`meta.json` の `sample_rates.audio_hz` は実際に録った値（48000）。
- **解析の基準は 16 kHz・1 ch**。解析は L（ch0）を `scipy.signal.resample_poly(x, 1, 3)` で 16 kHz に落とす。`docs/data-schema.md` の `audio.wav` の 16 kHz は
  記録ファームウェアの基板マイクの値で、変わらない。
- 理由:
  1. アダプタ（UGREEN CM721）のハードウェアは 48 kHz・2 ch 固定（実測）。`plughw` で 16 kHz・1 ch にすると、ALSA の plug の変換が記録に残らない。
     PC で落とせば、方法がコードに残り、やり直せる。
  2. CM721 では L と R が同一だった（実測）が、NZ-210C の受信機の出力は L 側だけに入りうる。記録の時点ではどちらも捨てない。
  3. 192 kB/秒（30 分で約 346 MB）。Wi-Fi で 180 秒の取りこぼし 0（実測）。

### 時計

- **咽喉マイクのセッションの時刻は、PC の単調時計（`time.monotonic()`）の、最初の PCM のバイトが届いた時刻 `t0` からの ms**（整数）。
  `events.csv` の `t_ms`（キーを受けた時刻）と `throat_chunks.csv` の `t_ms`（読み取りが戻った時刻）を同じ時計で刻む。
- 装置の `millis()` とは別の時計で、同じセッションに両方は入らない。装置と咽喉マイクを同じセッションで録る形は作らない（要れば #39 で決める）。

### サンプル位置とマーカーの対応（受信の時刻。主）

- 読み取り `k` の最後のフレーム `e_k` について、アンカー `a_k = t_ms_k − 1000 × (e_k + 1) ÷ 48000`。`a_k` は取り込みの時刻 + 遅れ（≥ 0）なので、下側の包絡を取る。
- 記録を 10 秒ごとの区間に分け、区間ごとの `a_k` の最小値を区間の中央の時刻に対して直線で当てはめる（最小二乗）。
  `τ(i) = 1000 × i ÷ 48000 + c0 + s × (1000 × i ÷ 48000)`。区間が 2 つ未満（20 秒未満の記録）は全体の最小値を `c0`、`s = 0`。
- 隣り合う区間の最小の差が 50 ms を超えたら「段差」（overrun などでフレームが抜けた疑い）として報告し、そのセッションは使わない目安に当たる。
- 実装は `tools/throat_check.py`（`sample_time_map`・`marker_to_sample`）。

### 叩きによる確かめ（端から端までの検証）

- 記録の冒頭で、マイクを指で持ち、`o` を押して note に `tap` と打ち、Enter をマイクのケースで押す（約 3 秒おきに 5 回）。
  `throat_check.py` が L を 16 kHz に落とした包絡（20 ms の移動 RMS）で、`o tap` の `[t − 300 ms, t + 300 ms]` から立ち上がりを探し、`Δ = 立ち上がり − t_ms` を出す。
  叩きが 3 回以上見つかり `|Δ の中央値|` ≤ 50 ms なら、受信の時刻による対応をそのまま使う。外れたら印を付け、使うかは人（`Δ` で対応を直さない）。
- Enter の衝撃が入らなければ、代替の手順（指で叩いてから `o`、`--tap-mode finger`）に切り替え、見つかった件数を参考として記録するだけにする（docs/log/2026-10-07.md）。

### 取りこぼしの数え方（終了時に表示し、`meta.json` にも書く）

1. `arecord` の標準エラーで `overrun` を含む行の数（`-q` を付けない）。
2. 推定差（診断値）= `(T × 48000 + 最初の読み取りのフレーム数) − N`（`T` は最初の到着から最後の到着までの PC の時間、`N` は受けたフレーム数）。
   到着の揺れ・滞留・クロックの差を含み、取りこぼしの数そのものではない。1 ピリオド（125 ms）未満は取りこぼしと言わない目安。

### `meta.json`

- `fw` = `pc-throat`（装置の申告値ではなく `record.py` が書く）。トップレベルの `imu_hz`・`audio_hz` は無い。`sample_rates` は `imu_hz` 0、`audio_hz` 48000、`analog_hz` 0。
- `sensors` = `[{"id": "throat", "part": "SH-12JK", "iface": <iface>}]`。`iface` は `sh12jk-wired-unoq-usbaudio`・`sh12jk-nz210c-rx-unoq-usbaudio`（#37）、
  `sh12jk-nz210c-a2dp-unoq`（予約。#38）、`sh12jk-wired-pc`・`sh12jk-nz210c-rx-usbaudio`（予約。PC 上の `arecord`、`--throat-host local`）。
  名前に `unoq` を含む `iface` は `local` 以外、他は `local` だけを受ける。
- `throat` に、宛先・PCM 名・形式・UNO Q に渡したコマンド・`buffer_size`/`period_size`・最初のバイトまでの秒数・フレーム数・経過・推定差・overrun の行・止まり方・
  子の終了コード・ミキサーの読み取り（`amixer -c <CARD> sget Mic` の標準出力。読み取りのみ）を書く。各キーは `docs/data-schema.md`。

## 塞がっていない経路（人の決定、10/7。CONCERN）

コミット前の adversarial-review（Codex）の 2 回目の指摘。人が「軽い検査＋運用で注意」と決めた。

- (a) **ALSA の設定による hw / plughw の再定義。** `arecord` は通常の ALSA 設定を読むので、ssh 先（`local` なら PC）の設定で `pcm.!hw` などを file プラグインに
  再定義されると、`--throat-device` の検査（hw / plughw の CARD・DEV 指定だけ）を通ったまま、波形の複製がファイルに残りうる。
  `record.py` は子を起動する前に、同じ経路で `~/.asoundrc` と `/etc/asound.conf` が無いことを確かめ、どちらかがあれば（または確かめられなければ）記録を始めない。
  `ALSA_CONFIG_PATH` や `/usr/share/alsa/alsa.conf` の書き換えは塞がない。
- (b) **`record.py` の外の操作。** ssh 先で `arecord` を直接ファイルに向けるなど。
- どちらも UNO Q に ssh できる人（開発者本人）の故意の操作で、0007「塞がっていない経路」（シンボリックリンク）と同じく運用で注意する。
- 運用の注意: UNO Q と PC に `~/.asoundrc`・`/etc/asound.conf` を置かない。10/8 に読み取り（`ssh arduino@unoq.local 'ls -la ~/.asoundrc /etc/asound.conf'`）で、
  UNO Q にはどちらも無いことを確かめた（PC にも無い）。

## 選ばなかった案

- **録音の開始からのサンプル数を時計にする。** 到着が 125 ms ごとの塊なのでマーカーが最大 125 ms 丸まり、到着の揺れがマーカーの値に溶けて後から外せない。
  PC の時計で刻み、到着の生の時刻を `throat_chunks.csv` に残せば、対応の出し方を後から変えられる。
- **叩きの `Δ` の中央値で対応を決める（叩きを主にする）。** 叩きが見つからない記録で対応が出せない。受信の時刻は毎回人の操作なしに残る。
- **UNO Q 側で `plughw` の 16 kHz・1 ch にする。** 変換が記録に残らない（上の理由 1）。
- **PC で受けながら 16 kHz・1 ch に落として書く。** 落とし方を記録の時点で固定し、元に戻せない。`record.py` に scipy の依存と信号処理が入る。
- **L だけを書く。** 経路によって片側だけになりうる（上の理由 2）。
- **`sounddevice` で PC の入力を録る（Issue #36 の本文）。** PC がアダプタを認識しない（0028）。PC の内蔵の入力も `arecord` を PC 上の子プロセスにして同じ仕組みで録る。

## 確かめていないこと

- 長い記録（1800 秒）での安定、Wi-Fi が混んだときの overrun（180 秒まで overrun 0）。
- マイクを挿した状態の信号、3 極のプラグを 4 極のジャックに挿したときの結線（#37）。
- Enter をマイクのケースで押した衝撃が、包絡のしきいを超える大きさで入るか（#37 の最初の記録で分かる）。
- 受信の時刻の下側の包絡が、実物で取り込みの時刻から何 ms 遅れるか（叩きの `Δ` が目安）。鍵盤から `record.py` がキーを受けるまでの遅れ。
- Wi-Fi の切断で UNO Q の `arecord` が残らないこと（SIGTERM・SIGINT・パイプを閉じる、の 3 通りでは残らなかった）。
- 合成データでの誤差の上限（20 ms）は合成の条件での要求で、実機の誤差を示すものではない。
