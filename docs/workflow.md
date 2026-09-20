# 運用の型

## 役割

- 考える: チャット（Claude / ChatGPT）。リポジトリの URL と Issue 番号を渡して相談する。
  結論は Issue のコメントか `docs/decisions/` に書く。書かれない結論は存在しない。
- 作る: Claude Code。1 セッション 1 Issue。入口と出口は CLAUDE.md の通り。
- 見直す: Codex。plan 確定前に plan レビュー、関門（Milestone 完了時）に adversarial review。
  対象はコードの正しさより「CLAUDE.md の制約に触れていないか」。詳細は「レビュー（Codex）」。

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
- 対象は制約領域に触れる plan。次に関わるもの。
  - 生データの扱いと記録形式（`docs/data-schema.md`、`firmware/logger/frame.h`、`tools/record.py`）
  - 評価の分割と指標（`analysis/`、`docs/evaluation.md`）
  - 推論パイプライン（Edge Impulse の書き出し、量子化、装置上の推論）
  - LED の表示ロジックと文言
  - センサ構成の変更（`docs/decisions/0002`）
- **ゲートは BLOCKING だけ。** BLOCKING は CLAUDE.md の制約違反と評価定義からの逸脱。
  CONCERN は列挙して人が採否を決め、結果を Issue のコメントに一行ずつ残す。
- 再レビューは2回まで。2回で残った指摘は、3回目を回さずに人に持っていく。
- 再レビューは新しいセッションを開かず、前回のスレッドを resume する（docs/decisions/0008）。
  見るのは、前回の指摘が直ったかと、直した箇所が新しい BLOCKING を生んでいないか。
  初回は fresh。初回から再レビューまでの間に、他の Codex task（`/codex:rescue` など）を挟まない。
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
| plan（plan mode の plan ファイル） | `codex-review` skill + `plan-approve.sh` |
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
- 再レビューは2回まで。関門の再レビューは fresh で回す（review コマンドに resume が無い）。
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
