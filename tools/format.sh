#!/usr/bin/env bash
# tools/format.sh — リポジトリのコードにフォーマッタをかける。
# 使い方（リポジトリの root から）: bash tools/format.sh
# Python は ruff（pyproject.toml）、C++ と .ino は clang-format（.clang-format）。どちらも行幅 80。
# firmware/detector/src/（Edge Impulse の書き出し）は触らない。生成物も触らない: m2_norm.h（m2_norm_header.py）、
# m2_threshold.h（m2_threshold.py --freeze）、shapes.h（gen_shapes.py が // clang-format off を出す）。
# コメントは折り返さない設定（ReflowComments: false）なので、コメントの 80 桁は書くときに守る。
# 行末コメントで 80 を超えるときは前の行に移す（clang-format と ruff は宣言のほうを割ってしまう）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

.venv/bin/ruff format .

# シンボリックリンク（bench や logger の実体を指す）は飛ばす。clang-format -i はリンクを実ファイルに置き換えてしまう
git ls-files -z -- 'firmware/**/*.cpp' 'firmware/**/*.h' 'firmware/**/*.ino' \
  | grep -zv '^firmware/detector/src/' \
  | grep -zv '^firmware/detector/m2_\(norm\|threshold\)\.h$' \
  | while IFS= read -r -d '' f; do [ -L "$f" ] || printf '%s\0' "$f"; done \
  | xargs -0 clang-format -i
