# 0004 Issue の着手は plan mode から始め、制約領域は plan レビューを通す

日付: 2026-09-19　状態: 採用

## 決定

- 実装を担当するエージェントは、Issue の着手を plan mode から始める。CLAUDE.md の
  「セッションの入口と出口」にこの一行を追記する。
- 制約領域に触れる plan は、確定前に Codex でレビューする。ゲートは BLOCKING だけとし、
  CONCERN の採否は人が決める。再レビューは2回まで。
- 承認は plan 本文のハッシュに結びつける。承認を書く経路は `.claude/hooks/plan-approve.sh` だけとし、
  Codex の出力に BLOCKING の verdict があれば機械的に拒否する。
- 制約領域（`analysis/`、`firmware/`、`tools/record.py`、`docs/evaluation.md`、
  `docs/data-schema.md`）は、承認済みの plan が無いと編集できない。
- 運用の詳細は `docs/workflow.md` の「レビュー（Codex）」。GitHub Actions は使わない。

## 理由

- 制約違反は、実装してから見つけるより plan の段階で止めるほうが安い。
- 「一定時間内にレビューした」だけを見るゲートは、別の plan や直した後の plan も通してしまう。
- CONCERN の全解消を条件にすると、レビューが終わらない。
- 自分の plan への批判を、plan を書いた本人（エージェント）が読んで合否を決める形は避ける。
- plan mode を通らずに編集を始めると、plan のゲートは存在しないのと同じになる。
