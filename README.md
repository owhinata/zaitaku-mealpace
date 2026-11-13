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
docs/unoq-setup.md   UNO Q の設定、書き込み、配置、自動起動の手順
docs/recording-protocol.md  記録の手順
docs/protopedia/     ProtoPedia の作品ページのストーリー
docs/img/            作品ページの画像
device/unoq/         UNO Q の Linux 側の検出器（音の取り込み、特徴量、CNN、後処理）
firmware/unoq_mcu/   UNO Q の MCU 側のスケッチ（LED マトリクスの表示）
firmware/logger/     Nano RP2040 Connect 記録ファームウェア（M1）
firmware/detector/   Nano RP2040 Connect 検出器（M2。採らなかった構成）
firmware/indicator/  UNO R4 WiFi 表示器（M2。図形の元は UNO Q でも使う）
tools/record.py      PC側の記録スクリプト
analysis/            解析・評価スクリプト
models/              Edge Impulse から書き出したモデル（M2）
data/                ローカルのみ（raw は除外）
CMakeLists.txt       build / upload / deploy / record のタスク定義
```

## 使い方

必要なもの: `arduino-cli`（1.5.1 で確認）、`cmake` 3.16 以上、`python3`、Arduino UNO Q、咽喉マイク（SH-12JK）、USB オーディオアダプタ。
`arduino-cli` は公式手順でユーザー領域（`~/.local/bin` など）に入れてよい。

```
cmake -S . -B build-unoq -DSKETCH_NAME=unoq_mcu -DPORT=<ポート>
cmake --build build-unoq --target deps          # 初回のみ。ボードコアを取得する
cmake --build build-unoq --target build
cmake --build build-unoq --target upload        # MCU 側のスケッチを書き込む
cmake --build build-unoq --target deploy-unoq   # Linux 側の検出器を UNO Q に配る
```

UNO Q の設定と、電源投入で検出器を起動する手順は [docs/unoq-setup.md](docs/unoq-setup.md)。
PC 側の解析と評価のテストは `python3 -m unittest discover -s analysis`。

Debian / Ubuntu のシステム Python は `pip install` を拒む（PEP 668,
`externally-managed-environment`）。その場合は venv を作って `-DPYTHON3=` で渡す。
`.venv/` は .gitignore 済み。

```
python3 -m venv .venv
cmake -S . -B build-unoq -DSKETCH_NAME=unoq_mcu -DPORT=<ポート> -DPYTHON3=$PWD/.venv/bin/python3
```

## ライセンス

コード: MIT（[LICENSE](LICENSE)）。文書と図（`docs/`）: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.ja)。
