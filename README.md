# zaitaku-mealpace

在宅介護の食卓で、毎食の嚥下を記録し、介助者にその場で返す装置。

年1回程度の嚥下内視鏡検査（VE）と次の検査の間には、嚥下の状態を知る手段が介助者の目視しかない。
本プロジェクトは、喉元の小型センサ（IMU＋マイク）で嚥下の発生・タイミング・回数を家庭で記録し、
食事中にLEDでペーシングの目安を返し、食事ごとの要約を蓄積して、次の検査の判断材料を作る。

**本装置は医療機器ではなく、誤嚥の検出や診断を目的としない。** 記録・可視化のための装置である。
疾病の診断・治療・予防を目的とした嚥下機能評価については医療関係者に相談すること。

- 企画書: [docs/proposal.md](docs/proposal.md)
- 計画と関門: [docs/plan.md](docs/plan.md)
- 現在地: [docs/status.md](docs/status.md)
- 判断の記録: [docs/decisions/](docs/decisions/)

## 生データはコミットしない

`data/raw/` は .gitignore 済み。被験者の生の計測データ（音声・IMU）は、いかなる理由でもリポジトリに入れない。
公開してよい情報の線引きは [CLAUDE.md](CLAUDE.md) を参照。

## 構成

```
CLAUDE.md            不変の制約（作らないもの、データの扱い、公開範囲）
AGENTS.md            Codex 向けの制約の要約（レビューの判定基準）
.claude/             plan レビューの skill と、plan・制約領域の編集を止める hook
docs/plan.md         段階と関門、判定基準
docs/status.md       現在地。セッション末に更新
docs/decisions/      1判断1ファイル
docs/log/            実験ノート（日付ファイル）
docs/data-schema.md  記録形式。先に固定
docs/evaluation.md   評価の定義。先に固定
docs/proposal.md     企画書（匿名化版）
docs/workflow.md     Issue運用・セッションの入口と出口
firmware/logger/     Nano RP2040 Connect 記録ファームウェア
tools/record.py      PC側の記録スクリプト
analysis/            解析・評価スクリプト
models/              Edge Impulse から書き出したモデル
data/                ローカルのみ（raw は除外）
CMakeLists.txt       build / upload / record のタスク定義
```

## 使い方（分岐点まで）

```
cmake -S . -B build -DPORT=/dev/ttyACM0
cmake --build build --target build
cmake --build build --target upload
cmake --build build --target record -- SUBJECT=self COND=water
```

## ライセンス

コード: MIT（[LICENSE](LICENSE)）。文書と図（`docs/`）: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.ja)。
