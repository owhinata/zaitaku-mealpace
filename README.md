# zaitaku-mealpace

在宅介護の食卓で、毎食の嚥下のタイミングを咽喉マイクで捉え、介助者にその場で LED の「目安」として返す装置。

年 1 回程度の嚥下内視鏡検査（VE）と次の検査の間、嚥下の状態を知る手段は介助者の目視しかない。本プロジェクトはその空白を埋める。
喉元に当てた咽喉マイク（SH-12JK）の音を Arduino UNO Q の Linux 側でメル特徴量と小さな CNN にかけ、0.25 秒ごとに嚥下の有無を推定し、
MCU 側の LED マトリクスに「目安」を出す（流れる波線 = 動作中、微笑む顔 = 嚥下の目安）。生の音声は特徴量に変換した後に捨て、装置にも PC にも残さない。
評価は開発者自身の実際の食事 4 食（収集日 3 日）で 1 回だけ行い、検出率 0.957（199/208）、誤検出率 1.25 回/分（70 回 / 56 分）。
合格線（85% / 1 回/分以下）のうち誤検出率は満たさない（docs/decisions/0041・0042）。本人試用と姿勢の同時記録は未実施。
経緯: 基板の IMU＋PDM マイク（Nano RP2040 Connect、M1〜M2）→ 咽喉マイク（M3〜M4）。docs/decisions/0024・0025。

**本装置は医療機器ではなく、誤嚥の検出や診断を目的としない。** LED の表示は嚥下の「目安」で、判定や指示ではない。
食形態・姿勢の判断はしない。疾病の診断・治療・予防を目的とした嚥下機能評価については医療関係者に相談すること。

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

必要なもの: `arduino-cli`（1.5.1 で確認）、`cmake` 3.16 以上、`python3`。
`arduino-cli` は公式手順でユーザー領域（`~/.local/bin` など）に入れてよい。

```
cmake -S . -B build -DPORT=/dev/ttyACM0
cmake --build build --target deps     # 初回のみ。ボードコア約 100 MB を取得する
cmake --build build --target build
cmake --build build --target upload
cmake --build build --target record -- SUBJECT=self COND=water
```

Debian / Ubuntu のシステム Python は `pip install` を拒む（PEP 668,
`externally-managed-environment`）。その場合は venv を作って `-DPYTHON3=` で渡す。
`.venv/` は .gitignore 済み。

```
python3 -m venv .venv
cmake -S . -B build -DPORT=/dev/ttyACM0 -DPYTHON3=$PWD/.venv/bin/python3
```

## ライセンス

コード: MIT（[LICENSE](LICENSE)）。文書と図（`docs/`）: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.ja)。
