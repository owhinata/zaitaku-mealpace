# 0015 メインのエージェントは管理だけを行い、作業は subagent が行う

日付: 2026-09-20　状態: 採用（一部を docs/decisions/0016 で改めた）

## 決定

- メインのエージェント（人と話しているセッション）は管理だけを行う。Issue の作業（コード・テスト・文書の作成と変更、
  使い捨ての解析、切り出し）は subagent が行う。
- subagent の model は内容で選ぶ。軽い作業（文書の清書、小さなスクリプト、機械的な切り出し、読むだけの下調べ）は sonnet。
  制約領域の実装、評価の数え方・時刻の換算・分割が絡む作業、plan の素案づくりは opus（または既定の上位の model）。
- plan mode への出入り（`EnterPlanMode` / `ExitPlanMode`）、Codex の plan レビュー、`plan-approve.sh`、コミットはメインが行う。
  subagent は `ExitPlanMode` を実行できない。
  （2026-09-20、docs/decisions/0016 で Codex の plan レビューの実施を subagent に移した。`plan-approve.sh` と
  plan mode の出入り、コミットはメインのまま。）
- 手順の詳細は `docs/workflow.md`「メインと subagent」。「1 セッション 1 Issue」は「1つの subagent に1つの Issue の、範囲を
  区切った作業」に置き換える。メインのセッションは、複数の Issue を続けて管理してよい。
- ゲートは変えない。Issue の着手は plan mode から、制約領域に触れる plan は Codex のレビュー、決めるのは人、
  Issue を閉じるのは人（CLAUDE.md、docs/decisions/0004）。

## 理由

- メインのコンテキストを、進め方の判断・人とのやり取り・レビューの裏取りに使う。実装の詳細で埋めない。
- M1 の Issue（#7〜#11、#13、#15）をこの形で進めて、1日で #12 の記録の前提がそろった。subagent の報告に「plan から外れた点・
  自分で決めた点」を必ず書かせ、メインがテストと差分で確かめる形が機能した。
- plan が Codex のレビューを通っていれば、subagent への指示は「plan のとおりに。食い違ったら止まって報告」で足りる。

## M1 で分かったこと（`docs/workflow.md` に反映）

- メインが plan mode に入ると、実行中の subagent も編集と実行ができなくなる。#11 の実装中に #9 のために plan mode に入り、
  subagent を止めてしまった。失われた作業は無く、plan mode を出てから再開できた。以後、subagent が実装している間は
  plan mode に入らない。
- plan ファイルは1つなので、subagent には scratchpad に置いた plan のコピーを渡す。次の Issue の plan は scratchpad で下書きし、
  Codex の plan レビューも下書きのハッシュで先に回せる（承認のときに、下書きを plan ファイルへそのままコピーする）。
- Codex の plan レビューを待つ間に、次の Issue の下調べ（読むだけの subagent）を並行で進められる。ただし、初回のレビューから
  再レビュー（resume、docs/decisions/0008）までの間に、別の Codex task を挟まない。
- `codex-companion.mjs` に `--help` を渡すと、focus の文言として扱われ、レビューが実際に走る（#10 で1回、意図しないレビューが走った）。
- 実機と人の手が要る作業（装着しての記録、叩きの実測、見本の音声を聞く確認）は、メインが人に頼んで結果を受け取る。
  見本の音声は、人が聞いて初めてテレビの音に気づいた（#9）。音量の数字では分からない。
