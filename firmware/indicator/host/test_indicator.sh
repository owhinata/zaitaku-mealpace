#!/usr/bin/env bash
# firmware/indicator/host/test_indicator.sh — 表示器の PC 上のテスト（受信の解釈 indicator_rx: Issue #29 plan 第 5.3 節、
# 状態 → フレーム shape_seq と図形の表 shapes.h: Issue #32 plan 第 6.6 節）。
# 使い方（リポジトリの root から）: bash firmware/indicator/host/test_indicator.sh
# g++ 1 本と、shapes.h が元（shapes/gen_shapes.py）と一致するかの確認（python3 の標準ライブラリだけ）。data/ を使わない。
# led_rule.h は firmware/indicator/ のリンク（→ firmware/detector/）を通して読む。
# matrix_out.cpp は足さない（Arduino_LED_Matrix。Arduino 依存）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
IND="$ROOT/firmware/indicator"
OUT_DIR="$ROOT/build-host"
OUT="$OUT_DIR/test_indicator"
CXX="${CXX:-g++}"

mkdir -p "$OUT_DIR"
"$CXX" -std=gnu++14 -O1 -Wall -Wextra -I "$IND" \
    "$IND/host/test_indicator.cpp" "$IND/indicator_rx.cpp" "$IND/shape_seq.cpp" \
    -o "$OUT"
"$OUT"
python3 "$IND/shapes/gen_shapes.py" --check
