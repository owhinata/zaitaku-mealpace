# 0001 開発環境: CMake をタスクランナーにして arduino-cli を呼ぶ

日付: 2026-09-19　状態: 採用

## 決定

- CMake の `add_custom_target` で `build` / `upload` / `record` を定義し、中身は arduino-cli と Python を呼ぶ（A案）。
- pico-sdk によるネイティブ CMake ビルド（B案）は採用しない。

## 理由

- 9/27 の分岐点までに PDM マイクの PIO ドライバと LSM6DSOX の I2C ドライバを書く余裕がない。
- Arduino は応募要件のために使う。コンテスト後は B 案（Arduino を介さない構成、RP2040 以外の可能性も含む）へ移行する意思がある。

## 移行しやすくするための条件（実装で守る）

- センサ入力とシリアルのフレーム形式に Arduino の型を漏らさない。Arduino ライブラリの API はモジュールの内側だけで呼ぶ。
- 推論は Edge Impulse の C++ ライブラリ形式で書き出す。
- 解析と評価は PC 側の Python で完結させ、ファームウェアが何で書かれていても CSV/WAV の形式を変えない。

## 再検討の条件

- M2 で RP2040 の性能が足りない（FPU なし）と判明した場合は、まず Nano 33 BLE Sense Rev2 への移行を検討する。
