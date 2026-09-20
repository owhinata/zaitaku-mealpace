# 0008 plan の再レビューは、前回の Codex スレッドを resume する

日付: 2026-09-20　状態: 採用

## 決定

- plan の再レビュー（docs/decisions/0004）は、新しい Codex セッションを開かず、
  `codex-companion.mjs task --resume` で前回の plan レビューのスレッドを続ける。
- 初回の plan レビューは fresh。関門のレビュー（`/codex:adversarial-review`）も、再レビューを含めて
  fresh（review コマンドに resume の機能が無い）。
- 再レビューのプロンプトは範囲を絞る。見るのは、前回の BLOCKING / CONCERN が直ったかと、
  直した箇所が新しい BLOCKING を生んでいないか。`PLAN-SHA` と3面の `VERDICT` 行の形式は
  初回と同じで、`plan-approve.sh` の検査も変えない。
- 初回のレビューから再レビューまでの間に、他の Codex task（`/codex:rescue` など）を挟まない。
  `--resume` は、その Claude セッションの最新の task を拾う。
- 再レビューは2回まで、は変えない。

## 理由

- #2 では fresh のレビューを3回回し、毎回新しい CONCERN が出た（計10件、約41分、BLOCKING 0）。
  新しいセッションは毎回リポジトリを一から読み直し、前回と別の場所を掘る。
- 再レビューの目的は「直ったことの確認」。前回の指摘と文脈を持ったスレッドのほうが、その目的に合う。
- resume したスレッドは自分の前回の指摘に引きずられ、独立性が落ちる。初回と関門を fresh に
  残すことで、独立した目は保つ。
