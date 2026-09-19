# 運用の型

## 役割

- 考える: チャット（Claude / ChatGPT）。リポジトリの URL と Issue 番号を渡して相談する。
  結論は Issue のコメントか `docs/decisions/` に書く。書かれない結論は存在しない。
- 作る: Claude Code。1 セッション 1 Issue。入口と出口は CLAUDE.md の通り。
- 見直す: Codex の adversarial review。関門（Milestone 完了時）だけ回す。
  対象はコードの正しさより「CLAUDE.md の制約に触れていないか」。

## Issue

- Issue は LLM が `docs/plan.md` の Milestone ごとに起票し、人が確認・修正する。
- 1 Issue は 1 セッションで終わる大きさ。終わらないなら分ける。
- Milestone 名は関門名（`M1 分岐点 9/27` など）。
- ラベルは `firmware` / `analysis` / `docs` の3つ。
- 各 Milestone の末尾に「判定」Issue を置き、閉じるのは人だけ。

## 言語

- コミットメッセージ（件名と本文）は英語。
- Issue、PR、Milestone、コメント、`docs/` は日本語。

## セッション

1. `docs/status.md` と `docs/plan.md` を読ませる。
2. 「Issue #N をやって」。
3. 終わったら `docs/status.md` を更新させ、差分を自分で見る。
4. 週1回、`docs/plan.md` の関門と現在地を自分で見直す。

## 相談するとき

- CLAUDE.md の URL を先に渡し、「この制約の中で」と言う。
- 被験者 `p1` の生データは貼らない。
- 複数のモデルで意見が割れたら、それは実測で確かめる箇所。決めるのは人。
