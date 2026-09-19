# analysis/

PC 側の解析・評価。ファームウェアが何で書かれていても、`docs/data-schema.md` の形式が入力。

- `features.py`   窓ごとの特徴量（IMU＋音）
- `evaluate.py`   `docs/evaluation.md` の定義でイベント単位の検出率・誤検出率を出す
- `split.py`      セッション単位で学習／評価を分ける（窓単位の分割は実装しない）
- `to_edge_impulse.py`  events.csv のラベルで WAV/CSV を Edge Impulse に投入する形に整える

M1 では `features.py` ＋ ロジスティック回帰、M2 から Edge Impulse。
