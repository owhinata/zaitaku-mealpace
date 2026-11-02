# 0046 進捗・関門・リスク・判断待ち・日程を GitHub Projects で見る

日付: 2026-11-02　状態: 採用

## 決定

- リポジトリに GitHub Projects（Projects v2）`zaitaku-mealpace - Development Management`
  （`https://github.com/users/owhinata/projects/6`、Private、リポジトリに link）を 1 つ置く。
- フィールドは Status（Todo / In Progress / Blocked / In Review / Done）、Priority（Critical / High / Medium / Low）、
  Item Type（Task / Risk / Decision / Gate）、Gate Result（Pending / Passed / Failed / Conditional）、
  Impact（High / Medium / Low）、Decision Status（Pending / Decided / Deferred）、Start date、Due date。
  Milestone・Assignees・Labels は Issue 側のものを使い、Project で重ねて持たない。
- View は 5 つ: Project Overview（Table）、Execution Board（Board）、Milestone / Gate（Table）、
  Risks & Decisions（Table）、Delivery Roadmap（Roadmap）。
- 正の所在と更新の規則は docs/workflow.md「GitHub Projects」に置く。要点: Issue の open / close の正は GitHub の Issue、
  Status の正は Project、Gate Result と Decision Status は `docs/decisions/` と Issue の記録の写し、日付は
  `docs/plan.md`・Milestone・Issue 本文に書かれたものだけ。更新はメインがセッションの出口で行う。
- 既存の Issue 59 件（#1〜#59）を登録し、初期値を付けた。Gate Result は判定記録のとおり
  M0・M1・M3 = Passed（0010・0017・0036）、M2・M4 = Failed（0027・0042）、M5 = Pending。
  #54・#55 は人の決定（11/1、Issue 本文）があるので Decided。#52 は自動起動時の唯一の安全側なので Risk / Impact High。
  Priority は Open の Issue だけに付け、#56 だけ Critical（11/10 の公開が固定）。Closed の Issue には Priority と日付を付けない。
  M5 の Issue の Start / Due は `docs/status.md`「次にやること」の目安の期間（#56 の Due だけ本文の公開日 11/10）。
- ドラフト項目（Issue でない項目）は入れない。必要になったら足す。

## 理由

- 今の運用は、進捗は `docs/status.md`、関門の判定は `docs/decisions/`、open / close は GitHub と分かれていて、
  関門の見通し・リスク・人の判断待ち・期限を 1 か所で見る場所が無い。M5 は公開日（11/10）と応募締切（11/24）が固定で、
  期限の遅れを早く見つける必要がある。
- Issue の Closed と関門の通過は別（M2・M4 は Closed だが通過しない）。Gate Result を Issue の状態と独立に持つ。
- CLAUDE.md「作らないもの」のダッシュボードは装置の機能の話で、管理用の Project は含まない。

## API で出来たこと・出来なかったこと

- `gh project` で Project・フィールド・Issue の登録・値の設定ができる。Status の選択肢の追加は `updateProjectV2Field`。
- View の作成（`createProjectV2View`: 名前・形式・表示列）とフィルタ（`updateProjectV2View`）は GraphQL でできた。
  Roadmap は表示列を受け付けない。
- グループ化・並び順・Board の列の軸・スライス・Roadmap の日付欄とマーカーは API の入力に無く、Web UI で設定する
  （設定した内容は docs/workflow.md「GitHub Projects」の View の説明のとおり）。

## 注意

- Project の Status は人が見る目安で、`docs/status.md` の「次にやること」を置き換えない。status.md は作業の結果と
  次にやることを書き、Project は状態を持つ。
- 0043〜0045 は #52・#54・#55 の本文が仮に予約しているので、この記録は 0046 にした。
