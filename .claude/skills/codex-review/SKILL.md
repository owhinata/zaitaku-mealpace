---
name: codex-review
description: Codex による plan（実装計画）レビュー。ExitPlanMode ゲートの marker を更新する唯一の経路。zaitaku-mealpace の生データの扱い・記録形式 / 評価の分割と指標 / 推論パイプライン / LED の表示ロジックと文言 / センサ構成に関わる計画に使う。実装後の diff レビューは codex plugin の /codex:review・/codex:adversarial-review を使う（この skill ではない）。
argument-hint: <plan | 設計説明>
---

# Codex plan レビュー (zaitaku-mealpace)

## この skill の役割 / 役割でないもの

Codex 呼び出しは codex plugin（`codex@openai-codex`）のランタイムに一本化されている。
用途ごとの入口は `docs/workflow.md` の「レビュー（Codex）」の通り:

| レビュー対象 | 使うもの |
|---|---|
| **実装計画（scratchpad の素案、または plan mode の plan ファイル）** | **この skill**（`plan-approve.sh` で ExitPlanMode の marker を書く） |
| 関門のレビュー、制約に近い領域の差分 | `/codex:adversarial-review --base <ref> <focus>` |
| それ以外の差分 | `/codex:review --base <ref>` |
| 不具合の原因追跡 | `/codex:rescue` |

plan だけがこの skill に残っている理由: plugin の review コマンドは git 差分専用で、
まだコードになっていない会話中の plan をレビューできない。制約違反は実装してから
見つけるより plan の段階で止めるほうが安いので、ここだけ自前で持つ。

## 誰が実行するか（docs/decisions/0016）

- 手順 1〜3（プロンプトを書く、Codex に投げる、裏取りと報告）は、plan を書いた subagent が行う。
  レビューが済んだら報告して止まる。
- **手順 4（`plan-approve.sh`）はメインのエージェントだけが行う。`--skip` も同じ。subagent は実行しない。**
  `plan-approve.sh` は scratchpad の plan ファイルでも通り、`edit-gate.sh` は marker が空でないことしか見ないので、
  plan を書いた本人が承認できてしまう。それを運用で塞いでいる。
- CONCERN の採否と、plan の中の選択肢を決めるのは人。subagent は人に質問できないので、決めてもらう点を
  報告に並べてメインに渡す。
- plan レビューを担当する subagent は同時に1つまで。初回から再レビュー（resume）までを同じ subagent が続ける。

## 実行手順

### 1. plan を自己完結した 1 枚のプロンプトに落とす

plan は全文が1つのファイルに書いてあること。plan を作る段階では scratchpad の素案ファイル
（`plan-<Issue 番号>.md`）でよい。承認は**ファイルの内容の**ハッシュに結びつくので、承認のときに素案を
plan mode の plan ファイル（`~/.claude/plans/*.md`）へ一字一句そのままコピーすれば、素案に対する `PLAN-SHA` が
そのまま通る。レビューに出す plan とファイルの中身は、常に一致させる。

Codex はこの会話のコンテキストを見られない。「上記の plan」のような参照は書かない。
scratchpad にプロンプトファイルを書く:

- 1行目に `PLAN-SHA: <plan を書いたファイル（素案でよい）の sha256 先頭16桁>`
  （`sha256sum < <plan ファイル> | cut -c1-16`）。
  「出力の冒頭に、この行をそのまま書け」と指示する。`plan-approve.sh` は、出力の PLAN-SHA が
  承認時点の plan ファイルと一致しなければ拒否する。別の plan や直す前の plan へのレビューで
  承認されるのを防ぐためなので、plan を直したらハッシュを取り直して再レビューする
- 担当する Issue の番号と、その Issue が求めている範囲
- plan の全文（変更するファイル、追加する処理、記録・保存するもの、評価の手順）
- plan が属する Milestone と、その判定基準（`docs/plan.md`）
- プロジェクト不変条件（`AGENTS.md` にも置いてあるが、プロンプトにも明示する）
- 下の「3 面レビュー観点」を明示的に 3 つとも指示
- 「LGTM を出す場合は該当面すべてについて根拠（ファイル:行、`docs/` の節）を示すこと」
- 出力形式の指定: 面ごとに次の形の行を**必ず1行ずつ、行頭から**書かせ、その下に根拠と修正案を書かせる。
  `plan-approve.sh` がこの行を機械的に読む。3行そろっていない出力は承認されない

  ```
  PLAN-SHA: <プロンプトの1行目と同じ値>
  VERDICT: 制約: LGTM|CONCERN|BLOCKING
  VERDICT: 評価: LGTM|CONCERN|BLOCKING
  VERDICT: 設計: LGTM|CONCERN|BLOCKING
  ```
- BLOCKING は不変条件（`AGENTS.md`）の違反と `docs/evaluation.md` からの逸脱だけ。それ以外は CONCERN

Codex は read-only サンドボックスでローカルシェルを使える。リポジトリの実物を読ませてよいので、
plan 中のファイルパスはそのまま書く。

**プロンプトに入れてはいけないもの**: `data/raw/` の中身、被験者 `p1` の生データ、
CLAUDE.md「公開してよい情報の線引き」を越える情報。プロンプトは外部サービスに送られる。
p1 のデータに触れる plan は、集計値と「試用N日目」の相対表記だけで説明する。

### 2. Codex に投げる

plugin のバージョン番号をハードコードしないこと（更新で壊れる）:

```bash
node "$(ls -d "$HOME"/.claude/plugins/cache/openai-codex/codex/*/scripts/codex-companion.mjs \
        | sort -V | tail -1)" task --effort medium --prompt-file /abs/path/to/plan-review.md
```

（コマンドは `node` で始めること。先頭に変数代入を置くと `Bash(node:*)` の
permission ルールに当たらず毎回プロンプトが出る。）

**再レビュー（plan を直した後）は、前回のスレッドを resume する**（docs/decisions/0008）:

```bash
node "$(ls -d "$HOME"/.claude/plugins/cache/openai-codex/codex/*/scripts/codex-companion.mjs \
        | sort -V | tail -1)" task --resume --effort medium --prompt-file /abs/path/to/plan-rereview.md
```

- 再レビューのプロンプトは短くてよい。1行目に新しい `PLAN-SHA`、直した plan の全文、前回の指摘の
  どれをどう直したか。見る範囲は「前回の BLOCKING / CONCERN が直ったか」と「直した箇所が新しい
  BLOCKING を生んでいないか」に絞ると明示する。出力形式（`PLAN-SHA` と3面の `VERDICT` 行）は初回と同じ。
- `--resume` は、この Claude セッションの最新の Codex task を拾う。初回のレビューから再レビューまでの
  間に、他の task（`/codex:rescue` など）を挟まない。挟んでしまったら `--fresh` で初回の形に戻す。
- 初回は resume しない（`--resume` を付けない）。

共通:

- `--write` は付けない。付けなければサンドボックスは `read-only`（レビューに書込は不要）。
- `--effort` は `medium`。時間が問題なら effort を上げ下げするより観点を絞る。
- `Bash(run_in_background: true)` で起動して待つ。plan review は 120s を超えるので
  フォアグラウンドだとツール側でタイムアウトする。出力ファイルを読んで結果を取る。

### 3. 結果をユーザーに報告

- Codex の生の出力は、全文を担当 Issue のコメントに貼る。要約だけを人に届けない。
- そのうえで面ごとに verdict と根拠を並べる。Codex の指摘は鵜呑みにしない。リポジトリの実物と
  `docs/` の定義で裏を取れる指摘かどうかを確認し、裏が取れない指摘はそう明記する。
- **ゲートは BLOCKING だけ。** CONCERN は列挙して人に渡し、採否は人が決める。
  決めた結果は Issue のコメントに一行ずつ残す。
- 再レビューに回数の上限は置かない（docs/decisions/0009）。BLOCKING が残っている間は直して再レビューする。
  CONCERN だけになったら自分の判断で回し続けず、直して再レビューするか、そのまま進むかを人に決めてもらう。
  同じ指摘が続く、または直すたびに隣の指摘が出て収束しないときは、その旨を添えて人に渡す。
- BLOCKING が誤りだと考える場合も、自分で verdict を読み替えない。根拠を付けて人に渡す。

### 4. 承認（ここがゲート。メインだけが行う）

marker を書くのは `plan-approve.sh` だけ。`touch` や手書きで marker を作らない。
**この手順を実行するのはメインのエージェントだけ**（docs/decisions/0016）。plan を書いた subagent は、
手順 3 の報告で止まる。

メインは plan mode に入り、確定した plan を scratchpad から plan ファイルへ一字一句そのままコピーしてから:

```bash
bash .claude/hooks/plan-approve.sh <plan ファイル> <Codex の出力ファイル>
```

- 出力に、現在の plan ファイルのハッシュと一致する PLAN-SHA 行が無ければ、スクリプトが拒否する。
- Codex の出力に BLOCKING の VERDICT 行が1つでもあれば、スクリプトが拒否する。
  3面の VERDICT 行がそろっていない場合も拒否する。
- 通れば、plan ファイルの sha256（先頭16桁）が marker
  `~/.claude/.zaitaku-mealpace-plan-codex-reviewed` に書かれる。
- `ExitPlanMode` の gate（`.claude/hooks/plan-gate.sh`）は、plan 本文のハッシュが marker と
  一致するときだけ通す。**承認後に plan を一文字でも変えたら再レビューが要る。**
  CONCERN を受けて plan を直した場合も同じ。
- 制約領域のパスやキーワードを含まない plan は、gate がレビューなしで通す。
  その場合 marker は無いので、制約領域のファイルは `edit-gate.sh` が編集を止める。
- trivial な plan で skip する場合は、人の承認を得てから
  `bash .claude/hooks/plan-approve.sh --skip <plan ファイル>`。
- marker はセッション開始時（SessionStart hook）に消える。

## 3 面レビュー観点

それぞれ独立したチェックとして実施する。1 面が LGTM でも、他面が未確認なら全体 LGTM にしない。

### 観点 1: 制約レビュー（CLAUDE.md / AGENTS.md）

- **範囲**: plan は担当 Issue の範囲に収まっているか。検出・LED・記録の3つ以外
  （ダッシュボード、設定画面、グラフ UI）を足していないか。関門を通過する前に
  次の Milestone のものを作ろうとしていないか
- **作らないもの**: 誤嚥の検出・診断、食形態・姿勢の医学的判断、ベッド・車椅子の自動制御に
  当たる機能や表現が入っていないか。ラベル名、指標名、変数名、UI 文言も対象
- **生データ**: 検出器ファームウェアにおいて、生の音声波形がファイル、ログ、シリアル出力、
  デバッグ用ダンプのどこかに残る経路を作っていないか。特徴量に変換した後に破棄しているか。
  生の計測データが `data/raw/` の外やコミット対象に出る経路はないか
- **公開範囲**: docs/、log/、Issue、コミットメッセージに、続柄・年齢・病名・服薬名・
  絶対日付が出る手順になっていないか。被験者 ID は `self` と `p1` のみか
- **表現**: LED の表示が「目安」ではなく「判定」や指示として読めないか。「VE の代替」と書いていないか

### 観点 2: 評価の妥当性レビュー（docs/evaluation.md と照合）

- 学習と評価はセッション単位で分かれているか。同じセッションの窓が両側に入る経路はないか
  （窓の重なり、シャッフル、前処理の統計量を全データで計算、を含む）
- `docs/evaluation.md` の定義（検出の数え方、誤検出の単位、合格線）を、`docs/decisions/` の
  記録なしに変えていないか。合格線に合わせて定義のほうを動かしていないか
- 閾値やハイパーパラメータを評価用セッションで選んでいないか
- 結果の報告に混同行列と評価に使ったセッション一覧が付く手順になっているか
- plan が判定基準の数字を出すと言うとき、その数字は `docs/plan.md` の関門の条件
  （記録回数、検出率、誤検出率）と同じ定義で測られるか

### 観点 3: 設計・実機レビュー（docs/decisions/ と docs/data-schema.md と照合）

- センサ入力とフレーム形式に Arduino の型が漏れていないか。Arduino ライブラリの API を
  モジュールの外で呼んでいないか（`firmware/logger/frame.h` の境界）
- 記録形式は `docs/data-schema.md` と一致しているか。形式を変える plan は、ファームウェア、
  `tools/record.py`、`analysis/` の3者を同時に直しているか
- 推論は Edge Impulse の C++ ライブラリ形式で書き出しているか（Arduino ライブラリ形式ではない）
- Nano RP2040 Connect の制約と整合しているか: FPU なし（int8 量子化が前提）、RAM 264KB、
  PDM マイクと IMU（LSM6DSOX）の取り込みタイミング、シリアル転送の帯域。
  能力の主張はデータシートや実測で裏を取る。裏が取れない推測は推測として明示させる
- 解析と評価が PC 側の Python で完結しているか。装置側に解析ロジックを持ち込んでいないか
- ビルドは CMake が arduino-cli を呼ぶ構成（A案）のままか
