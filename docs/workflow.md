# 運用の型

## 役割

- 考える: チャット（Claude / ChatGPT）。リポジトリの URL と Issue 番号を渡して相談する。
  結論は Issue のコメントか `docs/decisions/` に書く。書かれない結論は存在しない。
- 作る: Claude Code。入口と出口は CLAUDE.md の通り。メインのエージェントは管理だけを行い、作業は subagent が行う
  （下の「メインと subagent」、docs/decisions/0015・0016）。
- 見直す: Codex。plan 確定前に plan レビュー、関門（Milestone 完了時）に adversarial review。
  対象はコードの正しさより「CLAUDE.md の制約に触れていないか」。詳細は「レビュー（Codex）」。

## メインと subagent

メインのエージェント（人と話しているセッション）は管理だけを行う。Issue の作業は subagent に任せる（docs/decisions/0015）。
plan の素案づくりと Codex の plan レビューの実施も subagent が行う（docs/decisions/0016）。

**1つの Issue の流れ**

1. メインが Issue と範囲を決め、plan 担当の subagent（model は `opus` か `sonnet`。下の「subagent の model」）を起動する。
2. plan 担当の subagent: 下調べ → plan の素案を scratchpad に書く（`plan-<N>.md`）→ 制約領域に触れる plan なら
   `codex-review` skill の手順で Codex の plan レビューを回す（`PLAN-SHA` は scratchpad の plan ファイルのハッシュ）→
   指摘をリポジトリの実物で裏取りする → Codex の出力の全文と裏取りを Issue のコメントに貼る → 報告して止まる。
   subagent は人に質問できないので、決めてもらう点（plan の中の選択肢、CONCERN の採否）は報告に並べる。
3. メインが人に聞き、決定を同じ subagent に戻す。subagent が plan を直し、再レビュー（resume、docs/decisions/0008）を
   回す。BLOCKING が無くなり、人が進めると決めるまで繰り返す（回数の上限なし、docs/decisions/0009）。
4. メインが plan mode に入り、確定した plan を scratchpad から plan ファイルへそのままコピーして
   `bash .claude/hooks/plan-approve.sh <plan ファイル> <Codex の出力>` を実行し、`ExitPlanMode`（人が plan の全文を見て
   承認する）。ハッシュは内容で決まるので、コピーが一字一句同じなら、scratchpad の plan に対する `PLAN-SHA` がそのまま通る。
5. 実装は別の subagent（または同じ subagent の続き）。確認とコミットはメイン。

**subagent の model**

- subagent は `Agent` の `model` に **`opus` か `sonnet` を必ず指定して**起動する。指定しないと親（メイン）の model を
  継承し、メインが Fable のときは subagent も Fable で動いて利用上限を使い切る（2026-09-26 に #25・#27 の subagent が
  429 で止まった）。plan の下書き・レビューの裏取り・実装は opus、下調べや機械的な作業は sonnet。Fable はメインの管理と
  判断にだけ使う。
- `fork` は親の model で動くので使わない。

**メインがやること**（メインに残す理由も添える）

- Issue・`docs/status.md`・`docs/plan.md` を読み、進め方を決める。人とのやり取りは全部メインが持つ。CONCERN の採否と
  plan の中の選択肢は人に決めてもらう（subagent は人に質問できない）。
- `plan-approve.sh` の実行（`--skip` を含む）。**subagent は `plan-approve.sh` を実行しない。** `plan-approve.sh` は
  scratchpad の plan ファイルでも通り、`edit-gate.sh` は marker が空でないことしか見ないので、subagent が自分の plan を
  自分で承認して制約領域を編集することが技術的にはできてしまう。plan を書いた本人が承認しない、という運用で塞ぐ
  （hook は変えない）。subagent に渡す指示にも毎回書く。
- plan mode への出入り（`EnterPlanMode` / `ExitPlanMode`）。**subagent は `ExitPlanMode` を実行できない。**
- subagent の報告の確認。plan なら、レビューの verdict、`PLAN-SHA`、Codex の出力の全文が Issue のコメントに貼られているか。
  実装なら、テストを自分で走らせ、差分を読み、plan から外れた点と subagent が自分で決めた点を Issue のコメントに残す。
- `docs/status.md` と `docs/log/` の更新、コミット。push は人に確認してから。
- 実機や人の手が要る作業（装着しての記録、音声を聞いて確かめる、など）を人に頼み、結果を受け取る。

**subagent がやること**

- plan の素案づくり、Codex の plan レビューの実施、指摘の裏取り、Issue へのコメント。
- コード・テスト・文書の作成と変更、使い捨ての解析スクリプト、切り出しなどの作業。1つの subagent には1つの Issue の、範囲を
  区切った作業だけを渡す。レビューの指摘への数行の修正も、原則として subagent に渡す。
- 渡すもの: CLAUDE.md を最初に読むこと、plan のパス（素案づくりなら書き出す先、実装なら承認済みの plan のコピー）、
  変えてよいファイルと変えてはいけないファイル、確認の方法、報告の形式。plan と実物が食い違って進められないときは、
  読み替えずに止まって報告させる。
- 守らせること: 編集は Edit / Write で行う（制約領域は hook が見ている。Bash で書き換えない）。git の add / commit / push を
  しない。`plan-approve.sh` を実行しない。指示が無いかぎり実機と `data/raw/` に触らない。生の波形や特徴量の値を報告に出さない。
  Codex に送るプロンプトに、`data/raw/` の中身・p1 の生データ・公開の線引きを越える情報を入れない。

**model の選び方**

- sonnet: 文書の清書、合成データで確かめる小さなスクリプト、切り出しのような機械的な作業、読むだけの下調べ。
- opus（または既定の上位の model）: 制約領域（`analysis/`、`firmware/`、`tools/record.py`、`docs/evaluation.md`、
  `docs/data-schema.md`）の実装、評価の数え方・時刻の換算・分割が絡む作業、plan の素案づくりと plan レビューの実施。
  迷ったら上位を使う。

**plan mode と並行作業（M1 で分かったこと）**

- メインが plan mode に入ると、実行中の subagent も編集と実行ができなくなる。**subagent が実装している間は plan mode に入らない。**
  plan mode に入るのは、他の subagent が動いていないときの、承認のための短い時間だけにする。止めてしまった subagent は、
  plan mode を出てから再開させる。
- plan ファイル（`~/.claude/plans/*.md`）は1つしかない。subagent には plan ファイルそのものではなく、scratchpad に置いた
  plan（素案、または承認済みの plan のコピー）のパスを渡す（後で plan ファイルを次の Issue に替えても、実装中の subagent が
  別の plan を拾わない）。
- `edit-gate.sh` が見るのは marker が空でないことだけ。`plan-gate.sh` と `plan-approve.sh` は plan ファイルのハッシュを見る。
  承認の取り直しは、実装中の subagent の編集を止めない。
- 実装を待つ間に、次の Issue の plan を別の subagent に作らせてよい。ただし Codex の plan レビューを担当する subagent は
  同時に1つまでにする。`--resume` は「この Claude セッションの最新の Codex task」を拾うので、初回のレビューから再レビューまでの
  間に、別の Codex task（`/codex:rescue`、別の plan のレビュー）を挟まない。subagent から起動した task で resume が正しい
  スレッドを拾うかは未確認。最初の1回は出力の Thread の ID で確かめ、拾えていなければ `--fresh` で初回の形に戻す。
- Codex が使う model は `~/.codex/config.toml` の既定で決まる（companion に `--model` を渡していない）。Issue のコメントに
  model 名を書くなら、Codex のセッションログ（`~/.codex/sessions/…jsonl` の `"model"`）で確かめてから書く。
- `codex-companion.mjs` に `--help` を渡すと、focus の文言として扱われてレビューが実際に走る。オプションの確認に使わない。

## Issue

- Issue は LLM が `docs/plan.md` の Milestone ごとに起票し、人が確認・修正する。
- 1 Issue は 1 セッションで終わる大きさ。終わらないなら分ける。
- Milestone 名は関門名（`M1 分岐点 9/27` など）。
- ラベルは `firmware` / `analysis` / `docs` の3つ。
- 各 Milestone の末尾に「判定」Issue を置き、閉じるのは人だけ。

## レビュー（Codex）

Codex の呼び出しは codex plugin（`/codex:*`）に一本化する。

**plan 確定前（実装着手前）**

- Issue の着手は plan mode から始める（CLAUDE.md）。plan は `ExitPlanMode` の前に
  `codex-review` skill（`.claude/skills/codex-review/`）で3面（制約 / 評価 / 設計）をレビューする。
- レビューを回すのは plan を書いた subagent。`plan-approve.sh` と plan mode の出入りはメイン
  （上の「メインと subagent」、docs/decisions/0016）。plan は scratchpad の素案のままレビューしてよく、
  承認のときに plan ファイルへそのままコピーする。
- 対象は制約領域に触れる plan。次に関わるもの。
  - 生データの扱いと記録形式（`docs/data-schema.md`、`firmware/logger/frame.h`、`tools/record.py`）
  - 評価の分割と指標（`analysis/`、`docs/evaluation.md`）
  - 推論パイプライン（Edge Impulse の書き出し、量子化、装置上の推論）
  - LED の表示ロジックと文言
  - センサ構成の変更（`docs/decisions/0002`）
- **ゲートは BLOCKING だけ。** BLOCKING は CLAUDE.md の制約違反と評価定義からの逸脱。
  CONCERN は列挙して人が採否を決め、結果を Issue のコメントに一行ずつ残す。
- 再レビューに回数の上限は置かない（docs/decisions/0009）。BLOCKING が残っている間は直して再レビューする。
  CONCERN だけになったら、直して再レビューするか、そのまま進むかは人が決める。同じ指摘が続く、
  または直すたびに隣の指摘が出て収束しないときは、その旨を添えて人に渡す。
- 再レビューは新しいセッションを開かず、前回のスレッドを resume する（docs/decisions/0008）。
  見るのは、前回の指摘が直ったかと、直した箇所が新しい BLOCKING を生んでいないか。
  初回は fresh。初回から再レビューまでの間に、他の Codex task（`/codex:rescue` など）を挟まない。
  初回から再レビューまでを同じ subagent が続ける（上の「plan mode と並行作業」）。
- Codex の生の出力は全文を Issue のコメントに貼る。要約だけを人に届けない。
- plan のプロンプトは外部サービスに送られる。`data/raw/` の中身と p1 の生データを入れない。

**ゲートの強制**（`.claude/settings.json` と `.claude/hooks/`）

- `plan-approve.sh`: marker `~/.claude/.zaitaku-mealpace-plan-codex-reviewed` を書く唯一の経路。
  Codex の出力に `VERDICT: <面>: BLOCKING` の行が1つでもあれば拒否する。3面の VERDICT 行が
  そろっていない出力も拒否する。通れば plan ファイルのハッシュを marker に書く。
- レビューのプロンプトには `PLAN-SHA: <plan ファイルのハッシュ>` を入れ、Codex に出力の冒頭へそのまま
  書かせる。`plan-approve.sh` は、出力の PLAN-SHA が現在の plan と一致しなければ拒否する。
  別の plan や、直す前の plan へのレビューでは承認できない。
- `plan-gate.sh`（`ExitPlanMode`）: plan 本文のハッシュが marker と一致するときだけ通す。
  承認後に plan を一文字でも変えたら再レビューが要る。制約領域のパスやキーワードを含まない
  plan はレビューなしで通す。
- hook は、判定できないとき（jq が無い、入力から plan やパスが取れない）は block する。
- `edit-gate.sh`（`Edit` / `Write`）: `analysis/`、`firmware/`、`tools/record.py`、
  `docs/evaluation.md`、`docs/data-schema.md` は、承認済みの plan（marker）が無いと編集できない。
  plan mode を通らない編集と、キーワードを書かずにゲートを抜けた plan をここで止める。
- marker はセッション開始時に消える。前のセッションの承認は持ち越さない。
- trivial な plan で skip する場合も、人の承認を得てから `plan-approve.sh --skip <plan ファイル>`。
  marker を `touch` や手書きで作らない。
- 穴: Bash 経由の書き込み（`sed -i` など）は `edit-gate.sh` に掛からない。制約領域を Bash で編集しない。
- 壊れたとき: Claude Code や codex plugin の構成が変わると、`ExitPlanMode` が block され続ける。
  block の理由に原因が出るので、`.claude/hooks/plan-gate.sh` の入力の読み方
  （`tool_input.plan` / `planFilePath`）と、skill 中の `codex-companion.mjs` のパスを確認する。

**実装後: いつ回すか**

- 関門（Milestone 完了時）。「判定」Issue を人が閉じる前に1回回す。
- 対象は前の関門からの差分。関門を通過したら人が `gate-M<N>` タグを打ち、
  次のレビューは `--base gate-M<N>` で回す。M0 は初期コミットを base にする。
- 関門の途中でも、次のどれかに触れる差分はコミット前に回す。
  - 生データの扱い（保存、破棄、`.gitignore`、`data/` の配置）
  - 評価の分割と指標（`analysis/split.py`、`analysis/evaluate.py`、`docs/evaluation.md`）
  - LED の表示ロジックと、装置・文書の文言

**どれを使うか**

| 対象 | 使うもの |
|---|---|
| plan（scratchpad の素案。承認のときに plan ファイルへコピー） | `codex-review` skill + `plan-approve.sh` |
| 関門のレビュー、上の3領域に触れる差分 | `/codex:adversarial-review --base <ref> <focus>` |
| それ以外で見てほしい差分 | `/codex:review --base <ref>` |
| 不具合の原因追跡 | `/codex:rescue` |

- 1つの差分に掛けるレビューは1本だけ。`/codex:review` と `/codex:adversarial-review` を
  同じ差分に両方掛けない。後者は前者を含む。
- focus は1〜2問に絞り、疑っている面を名指しする。「この検査を通過したまま X できるか」の形で書く。
  - 「窓単位の分割が混ざったまま、`evaluate.py` が合格と報告できるか」
  - 「検出器ファームウェアにおいて、生の音声波形が、特徴量に変換された後もどこかに残る経路はあるか」
  - 「p1 の日付・続柄・病名が、docs/ や log/ に出る経路はあるか」
  - 「LED の表示や文言が、『目安』ではなく『判定』や指示として読める箇所はあるか」
- レビューは時間がかかるので `--background` で起動して待つ（進捗は `/codex:status`）。

**何を基準にするか**

- 見るのはコードの正しさより「CLAUDE.md の制約に触れていないか」。
- `/codex:review` の内蔵レビュアーは focus を受け取れない。制約を Codex に伝える経路は
  `AGENTS.md` だけなので、CLAUDE.md を変えたら `AGENTS.md` も同じコミットで直す。
- Codex に渡るのはリポジトリの中身だけ。`data/raw/` と p1 の生データを、
  レビューのために貼ったり移したりしない。

**結果の扱い**

- Codex の指摘は鵜呑みにしない。リポジトリの実物と `docs/` の定義で裏を取ってから報告する。
  裏が取れない指摘はそう明記する。
- 指摘は BLOCKING（CLAUDE.md の制約違反、評価定義からの逸脱）と CONCERN に分ける。
- 結果は「判定」Issue のコメントに残す。Codex の生の出力も全文を貼る。
- ゲートは BLOCKING だけ。BLOCKING が残ったまま関門を通さない。CONCERN は人が採否を決める。
- 再レビューに回数の上限は置かない。止める条件は「plan 確定前」と同じ（docs/decisions/0009）。
  関門の再レビューは fresh で回す（review コマンドに resume が無い）。
- 通すかどうかを決めるのは人。エージェントは「判定」Issue を閉じない。

## 言語

- コミットメッセージ（件名と本文）は英語。
  - 件名は英語の命令形で、50字程度まで。種別の接頭辞（`feat:` など）は付けない。
  - Issue に対応するコミットは、件名の先頭に `#N` を付ける（`#1 Document dev environment setup`）。
    `#` で始まる行はエディタ経由だとコメントとして消えるので、`git commit -m` か `-F` で渡す。
  - `Closes` / `Fixes` は使わない。Issue を閉じるのは人。
- Issue、PR、Milestone、コメント、`docs/` は日本語。

## セッション

1. `docs/status.md` と `docs/plan.md` を読ませる。
2. 「Issue #N をやって」。
3. 終わったら `docs/status.md` を更新させ、差分を自分で見る。
   - `docs/status.md` の更新は、その Issue のコミットに含める。status だけのコミットを作らない。
   - `docs/status.md` に Issue の open / close は書かない。正は GitHub。書くのは、作業の結果と、
     次にやること（まだ作業が残っている Issue）だけ。「確認して閉じる」のような待ちの状態は書かない。
4. 週1回、`docs/plan.md` の関門と現在地を自分で見直す。

## 相談するとき

- CLAUDE.md の URL を先に渡し、「この制約の中で」と言う。
- 被験者 `p1` の生データは貼らない。
- 複数のモデルで意見が割れたら、それは実測で確かめる箇所。決めるのは人。
