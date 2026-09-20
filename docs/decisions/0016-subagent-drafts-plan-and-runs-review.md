# 0016 plan の素案づくりと Codex の plan レビューの実施も subagent が行う

日付: 2026-09-20　状態: 採用

docs/decisions/0015 の「Codex の plan レビューはメインが行う」を改める。ゲートの仕組み（`.claude/hooks/`）と
「Issue の着手は plan mode から始める」（CLAUDE.md、docs/decisions/0004）は変えない。

## 決定

- plan の素案づくりと、Codex の plan レビューの実施（初回・再レビュー・指摘の裏取り・Issue へのコメント）は
  subagent が行う。model は opus（または既定の上位の model）。
- plan mode は残す。メインが plan mode にいるのは、確定した plan を承認する間だけにする。

### 流れ

1. メインが Issue と範囲を決め、plan 担当の subagent を起動する。
2. plan 担当の subagent: 下調べ → plan の素案を scratchpad に書く（`plan-<N>.md`）→ 制約領域に触れる plan なら
   `codex-review` skill の手順で Codex の plan レビューを回す（`PLAN-SHA` は scratchpad の plan ファイルのハッシュ）→
   指摘をリポジトリの実物で裏取りする → Codex の出力の全文と裏取りを Issue のコメントに貼る → 報告して止まる。
   subagent は人に質問できないので、決めてもらう点（plan の中の選択肢、CONCERN の採否）は報告に並べる。
3. メインが人に聞く。決定を同じ subagent に戻し、subagent が plan を直して再レビュー（resume、docs/decisions/0008）を
   回す。BLOCKING が無くなり、人が進めると決めるまで繰り返す（回数の上限なし、docs/decisions/0009）。
4. メインが plan mode に入り、確定した plan を scratchpad から plan ファイルへそのままコピーして
   `bash .claude/hooks/plan-approve.sh <plan ファイル> <Codex の出力>` を実行し、`ExitPlanMode`（人が plan の全文を見て
   承認する）。ハッシュは内容で決まるので、コピーが一字一句同じなら、scratchpad の plan に対する `PLAN-SHA` がそのまま通る。
5. 実装は別の subagent（または同じ subagent の続き）。確認とコミットはメイン。

### メインに残すもの

- 人とのやり取りと、CONCERN の採否を人に決めてもらうこと。subagent は人に質問できない。
- `plan-approve.sh` の実行（`--skip` を含む）。**subagent は `plan-approve.sh` を実行しない。**
- plan mode の出入り（`EnterPlanMode` / `ExitPlanMode`）、コミット、push（push は人の指示で）。
  subagent は `ExitPlanMode` を実行できない。
- subagent の報告の確認。レビューの verdict、`PLAN-SHA`、Codex の出力の全文が Issue のコメントに貼られているか。

## 理由

- メインのコンテキストを、進め方の判断・人とのやり取り・確認に使う（docs/decisions/0015）。#16 ではメインで plan を書き
  レビューを回したので、Codex の出力、裏取りの再現、plan の2版がメインのコンテキストに入った。
- メインが plan mode にいる間は、実行中の subagent が止まる（docs/decisions/0015）。plan mode にいる時間を承認の間だけに
  すれば、止める幅が小さくなる。
- `plan-approve.sh` をメインに残すのは、plan を書いた本人が承認しない形を保つため（docs/decisions/0004「自分の plan への
  批判を、plan を書いた本人が読んで合否を決める形は避ける」）。`plan-approve.sh` は scratchpad の plan ファイルでも通り、
  `edit-gate.sh` は marker が空でないことしか見ないので、subagent が自分の plan を自分で承認して制約領域を編集することが
  技術的にはできてしまう。hook は変えず、運用で塞ぐ。subagent に渡す指示にも毎回書く。

## 注意

- `--resume` は「この Claude セッションの最新の Codex task」を拾う（docs/decisions/0008）。subagent から起動した task で
  resume が正しいスレッドを拾うかは未確認。plan レビューを担当する subagent は同時に1つまでにし、初回から再レビューまでを
  同じ subagent に任せ、その間に他の Codex task を挟まない。最初の1回で resume が前回のスレッドを拾ったことを出力
  （Thread の ID）で確かめ、だめなら `--fresh` で初回の形に戻す。
- Codex に送るプロンプトの制約（`data/raw/` の中身、被験者 `p1` の生データ、公開の線引きを越える情報を入れない）は
  subagent にもそのまま適用する。subagent への指示に書く。
- Codex が使う model は `~/.codex/config.toml` の既定で決まる（companion に `--model` を渡していない）。Issue のコメントに
  model 名を書くなら、Codex のセッションログ（`~/.codex/sessions/…jsonl` の `"model"`）で確かめてから書く。
