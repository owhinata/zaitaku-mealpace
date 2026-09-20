# analysis/

PC 側の解析・評価。ファームウェアが何で書かれていても、`docs/data-schema.md` の形式が入力。

- `features.py`   窓ごとの特徴量（IMU＋音）
- `evaluate.py`   `docs/evaluation.md` の定義でイベント単位の検出率・誤検出率を出す
- `split.py`      セッション単位で学習／評価を分ける（窓単位の分割は実装しない）
- `to_edge_impulse.py`  events.csv のラベルで WAV/CSV を Edge Impulse に投入する形に整える

M1 では `features.py` ＋ ロジスティック回帰、M2 から Edge Impulse。

## テスト

`evaluate.py` と `split.py` の数え方は、合成データのテスト（`test_evaluate.py`・`test_split.py`）で
固定している。標準ライブラリの `unittest` だけで動く。合成データは一時フォルダに作り、`data/` は使わない。

```
python -m unittest discover -s analysis -v
```

`docs/evaluation.md` が決めていない点（窓が区間に「ある」の判定、連続する陽性の数え方、記録の範囲、
複数セッションの集計、混同行列）の解釈は `docs/decisions/0011-evaluation-interpretation.md` にある。
