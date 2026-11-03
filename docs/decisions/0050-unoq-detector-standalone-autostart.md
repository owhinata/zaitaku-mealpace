# 0050 UNO Q の検出器の単独モードと自動起動（systemd のユーザーユニット、記録との排他）（#53）

日付: 2026-11-03　状態: 採用（人の決定 11/3。実機の確かめ (0)〜(8) は 11/3 に通過。下の「確かめたこと」）

plan #53（PLAN-SHA `8d08124527bcf3e6`、Codex の plan レビュー 4 回・最後は BLOCKING なし、人が 11/3 に承認。指摘と裏取りは Issue #53 のコメント）による。
番号: Issue #53 の本文は「0043 か別番号」と書くが、0047・0048 は #54・#55、0049 は #52 が使うので 0050 とする。
依存: docs/decisions/0049（MCU の自律消灯）。

## 決定

1. **単独モードの定義。** UNO Q の検出器の「単独モード」（`detector.py --live --standalone`）は、電源投入から PC なしで、マトリクスの目安だけを出す動かし方。
   1. PC に何も送らない（ssh を使わない。標準出力に行を出さない。H・F・D・T・E のどれも出さない）。
   2. UNO Q にファイルを残さない（検出器はファイルを書かない。音声をディスクに書かない。今のまま）。
   3. journald に窓の結果の行を残さない。標準エラー（journald に入る）に出すのは今の文（照合・守り・止まり方）と、始めの `code_sha256` の先頭 12 桁、
      終わりの窓の数・`rpc_errors`・`overrun_lines` の 1 行ずつだけ。窓ごとの値・特徴量・波形は出さない。
   4. 出すのはマトリクスの目安だけ（`zm_set_state`。`mcu_rpc.py` の内側だけ、0035）。`--feat`・`--no-rpc` とは併用しない（`--live` のときだけ）。
   5. 単独モードの窓は記録ではなく、M4・M5 のどの数字にも使わない（数字が出ないので使えない）。
   6. 標準入力を読まない（ユニットの標準入力は `/dev/null`）。`--expect-config`・`--expect-sha256`（重みの sha256）の照合は今のまま行い、違えば消灯のまま 2 で終わる。
2. **止まったときの消灯。** 単独モードにはハートビートが無い（受け手の PC が無い）。検出器が止まった・つながりが切れたときの消灯は 0049 の MCU の約 2 秒が受け持つ。
   0049 のスケッチを書き込む前の UNO Q では単独モードのユニットを有効にしない。検出器の内側の消灯（窓が 1.0 秒出なければ 1 回、終わるときの消灯）は今のまま持つ。
3. **常駐。** 0035 決定 1 の「常駐させない（systemd のユニットは作らない）」を、単独モードのユーザーユニット 1 つ（`device/unoq/zaitaku-detector.service`）に限って改める。
   - ユニットは DEPLOY_FILES で `~/zaitaku-mealpace-device/` に配り、人が 1 回だけ `systemctl --user link` と `enable` を行う。`.service` は `code_sha256` に入らない
     （`analysis/m4_infer.py` の `CODE_SUFFIXES` は `.py`・`.npy`・`.npz` だけ）。
   - ユニットは単独モードだけを起動する。`--feat`・`--no-rpc`・`--replay` をユニットから起動しない。
   - `Restart=on-failure`、`RestartSec=2`、`StartLimitIntervalSec=120`・`StartLimitBurst=10`（暴走しない）、`StandardOutput=null`、`StandardError=journal`、`LimitCORE=0`、
     BLAS のスレッドを 1 つにする 3 つの変数（0040 決定 3）。`After=` は `sound.target` だけ（`arduino-router.service` はシステムのユニットでユーザーの manager から見えないので書かない。
     起動の順は `Restart` が受け持つ）。
4. **記録との排他。** 記録（`record.py --unoq-detector` と、UNO Q を通す `record.py --throat`）とユニットは同時に動かさない（0028 決定 3・0035 決定 2 をユニットにも当てる）。
   - 排他の本体は UNO Q の上の 1 つのロック。記録の子（`arecord`・`pw-record`）と検出器（ユニットからも `record.py --unoq-detector` からも）は、どれも
     `flock -n -F -E 75 /home/arduino`（util-linux の標準のコマンド。固定の絶対パスのディレクトリに排他のロックを掛け、fork せずにそのコマンドに置き換わる）の下で起動する。
     先に取った側が終わるまで、もう一方は終了コード 75 で始まらない。ロックはプロセスが終われば（`kill -9` でも）kernel が外す。ファイルを作らない。
   - ロックの fd は検出器が起こす `arecord` にも渡し（`audio_in.lock_fds()` が受け継いだディレクトリの fd を集め、`pass_fds` で渡す。fstat だけで読み書きしない）、
     `arecord` が終わるまでロックが残る（検出器が SIGKILL で死んでも、`arecord` が次の書き込みで死ぬまで。コミット前の adversarial-review の指摘、人の決定 11/3）。
   - `/proc/self/fd` を読めないときは入力を開かず 1 で終わる（fail-closed。2 回目の adversarial-review の指摘、人の決定 11/3）。ロックの fd が無い（flock の下で起動していない）ときは
     `arecord` を起こすが、記録と同時に動かさない保証は flock の下で起動したときだけ。
   - ロックのパスは ssh のユーザーで変わらない固定の文字（`$HOME`・`%h` は使わない）。ユニットと `tools/record.py` の `UNOQ_LOCK_DIR` に同じ文字を書き、テストで照らす。
   - ユニットはロックが取れなければ 75 で終わり、再起動しない（`RestartPreventExitStatus=75`）。記録の後に人が `start` する。
   - `record.py` は起動前に `systemctl --user is-active zaitaku-detector.service` の 1 行目を読み、`inactive`・`failed` 以外（`activating` = 再起動の待ちを含む）なら始めない
     （分かりやすい文を先に出すため。保証はロック）。子が最初のバイト・H 行の前に 75 で終われば、ロックの文で止めてフォルダを作らない。
   - 手順: 記録の前に `systemctl --user stop zaitaku-detector`、後に `start`。
5. **配布の照合。** `--expect-sha256` は重みの sha256 なので、ユニットを直すのは重み・構成を替えたときだけ（コードの配り直しでは直さない）。単独モードには `code_sha256` を照らす相手が
   無いので、どの版が動いたかは始めの文の先頭 12 桁を `journalctl` で読んで log に書く。

## 理由

- 人の意向（11/1）: UNO Q でアプリを自動起動したい（動画が撮りやすい）。今の `detector.py` は PC の `record.py` が起動し、標準入力のハートビートと標準出力の受け手を前提にする
  （0037 の追記 10/29）。systemd が起動すると標準入力は `/dev/null` で、今の `--live` は起動直後に EOF で止まる。
- journald が標準出力を取り込むと結果の行（音声ではない）がディスクに残るので、単独モードでは行を出さない（メインの D3）。
- 排他を「確かめてから起動する」だけにすると、確かめと起動の間に相手が起動しうる（Codex の plan レビューの 1 回目の BLOCKING）。A2DP の `pw-record` は ALSA の取り合いでも止まらない。
  ロックを取ることを起動の一部にすれば、隙間が無い。ロックの対象を `$HOME` にすると ssh のユーザーごとに変わる（2 回目の BLOCKING）ので、固定の絶対パスにした。

## 選ばなかった案

- ハートビートを MCU から返す・UNO Q の中で別のプロセスが打つ案（Issue にない。止まったときの消灯は MCU の約 2 秒で足りる）。
- 標準出力をそのまま journald に流す案（結果の行がディスクに残る）。窓の結果をファイルに書く案（CLAUDE.md「ファイルを書かない」）。
- システムのユニット（`/etc/systemd/system/`、`User=arduino`）。`After=arduino-router.service` は効くが、置く・直すのに sudo が要る。人の決定 11/3 でユーザーユニットを採った。
- ユニットの `ExecStartPre` で `pgrep` を見る案（人が 11/3 に一度採ったが、確かめと起動の間に隙間が残るので取り下げ、ロックに替えた）。
- 再起動の上限を置かずに間を空けていく案（`RestartSteps`・`RestartMaxDelaySec`）。人の決定 11/3 で上限（120 秒に 10 回）を採った。
- `deploy-unoq` がユニットを `~/.config/systemd/user/` に置く・生成する案（CMake を直す。配って `link` するので足りる）。

## 変えないこと

- `detector.py` の `record.py` から起動したときの振る舞い（ハートビート、行、終了コード）、`Pipeline`・`Rpc`・照合・`--selftest`・ダンプとスワップの守り。
- `mcu_rpc.py`・`audio_in.py`・`m4_infer.py`、`analysis/`、`firmware/`、`docs/evaluation.md`、`docs/data-schema.md`、0018 の規則と文言、0049。
- M4 の数字（評価用 `meal` 4 本と採点は 11/1 に終わっている。0041・0042）。

## 確かめたこと（2026-11-03、人が実機で行った。詳細と数字は docs/log/2026-11-03.md「#53」の「結果」）

`code_sha256` `1fd5b820…` の配布で、(0)〜(8) はすべて通過。

- (0) `flock` は util-linux 2.41.5 で `-F`（`--no-fork`）がある。`/home/arduino` のロックは 0 と 75 を返した。user manager のグループに `arduino-router`・`audio` があり、`sound.target` もある。
- (1) 電源投入から PC なしで、波線まで約 40 秒。起動直後は MCU の RPC がまだ揃わず、ユニットが 2 回「RPC を開けない」で 1 で終わり、2 秒後の再起動で 3 回目に動いた（`Restart` が起動の順を受け持つ設計どおり）。
- (2) 飲み込むと微笑む顔が出た（人の目視。数字に使わない）。
- (3a) ユニットを止めて flock の下で直接起動した検出器を `kill -9` → 約 2 秒で消え、消えたまま（0049）。ロックも外れた。(3b) ユニットの本体を `kill -9` → 消えて 2〜3 秒で波線が戻った。
- (4) `stop` で消えて再起動しない、`start` で戻る。(5) 電源断で消える。
- (6) アダプタ未接続: マトリクスは消えたまま。終了コードの実測値は 1（1 回目は RPC 未提供、2〜10 回目は入力を開けない）。約 2 分で failed（NRestarts 10、「Start request repeated too quickly」）。暴走しない。
- (7) 排他の 5 つの形: ユニット active の間の `--unoq-detector` と `--throat` は始めない、`stop` の後の `--unoq-detector` は始まる、手で持ったロックの間の `--throat` は始めない、
  `--throat` の記録中の `start` は 75 で failed（再起動しない）、`activating`（再起動の待ち）の間の `--unoq-detector` は始めない。
- (8) 新しい普通のファイルは検出器のものではない 2 件（WirePlumber の状態、X のロック）だけ、音声の拡張子 0 件。新しいリンクはユニットの 2 件だけ。journal は D/T/F/H/E の形の行 0、
  `detector: ` の行 44・systemd の行 40・どちらでもない行 0。窓ごとの値は無い。
- 確かめ用のセッション 2 本は数字に使わず、人の決定で消した。

## 塞がらない経路（運用で注意）

- 検出器が SIGKILL で死んだ後、`arecord` が死ぬまでの一瞬: `arecord` はロックの fd を持ったまま、次の書き込み（パイプの読み手が無い）で SIGPIPE で終わる。
  その一瞬は、ロックは残るが検出器はもういない（`arecord` は ALSA から読んでパイプに書くだけで、ファイルに書かない）。ロックが外れるのは `arecord` が終わった後。
- ロックを使わない経路（`flock` を通さずに人が UNO Q で `arecord` などを直接打つ）は塞がない。手順（記録の前に `stop`）で避ける。
- `/proc/self/fd` の列挙と、受け継いだ fd の `fstat` の失敗（閉じた fd の `EBADF` を除く）は fail-closed（入力を開かず 1 で終わる。3 回目の adversarial-review の指摘、人の決定 11/3）。ロックの fd が無い（flock の下で起動していない）ときは `arecord` を起こすが、記録と同時に動かさない保証は
  flock の下で起動したときだけ（`detector.py --live` を手で直接起動するときも `exec flock -n -F -E 75 /home/arduino …` を前に付ける）。

## 確かめていないこと

- 長い時間（数時間）の単独モードでのメモリの伸び、電源の瞬断からの戻り。
- (5)・(6) は PC の USB-C からの給電で行った（UGREEN のアダプタが PD の給電を兼ねるので、アダプタを外すと基板ごと止まるため）。モバイルバッテリーからの給電での (6) は行っていない。
- アダプタを戻した後に `reset-failed`・`start` で手で戻す経路（実機では電源投入でユニットが自動で active になり波線が戻ったので、手で戻す経路は試していない。docs/unoq-setup.md の手順のまま）。
