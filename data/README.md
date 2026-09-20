# data/

- `raw/`: ローカルのみ。.gitignore 済み。**コミットしない。**
- `sample/`: 被験者 `self` の 10 秒以内の見本のみ。形式の確認用。
  `tools/record.py` の出力先は `raw/` 固定なので、見本は `raw/` から手でコピーして置く
  （docs/decisions/0007）。

被験者 `p1` の生データは暗号化ディスクに置き、モデル確定後に削除する（CLAUDE.md）。
