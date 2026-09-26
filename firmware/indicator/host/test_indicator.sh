#!/usr/bin/env bash
# firmware/indicator/host/test_indicator.sh — 表示器の受信の解釈（indicator_rx）の PC 上のテスト（Issue #29 plan 第 5.3 節）。
# 使い方（リポジトリの root から）: bash firmware/indicator/host/test_indicator.sh
# g++ 1 本。data/ を使わない。led_rule.h は firmware/indicator/ のリンク（→ firmware/detector/）を通して読む。
# led_out.cpp は足さない（WiFiNINA。Arduino 依存）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
IND="$ROOT/firmware/indicator"
OUT_DIR="$ROOT/build-host"
OUT="$OUT_DIR/test_indicator"
CXX="${CXX:-g++}"

mkdir -p "$OUT_DIR"
"$CXX" -std=gnu++14 -O1 -Wall -Wextra -I "$IND" \
    "$IND/host/test_indicator.cpp" "$IND/indicator_rx.cpp" \
    -o "$OUT"
"$OUT"
