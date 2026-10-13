#!/usr/bin/env bash
# firmware/unoq_mcu/host/test_unoq_mcu.sh — UNO Q の MCU 側の PC 上のテスト（Issue #38、plan 38d 第 4 節）。
# 使い方（リポジトリの root から）: bash firmware/unoq_mcu/host/test_unoq_mcu.sh
# g++ 1 本。matrix_frame.cpp と、リンク（→ firmware/indicator/）を通した shape_seq.cpp をビルドして動かす。
# led_out.cpp・rpc_server.cpp は足さない（Arduino_LED_Matrix・Arduino_RouterBridge。Arduino 依存）。data/ を使わない。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SK="$ROOT/firmware/unoq_mcu"
OUT_DIR="$ROOT/build-host"
OUT="$OUT_DIR/test_unoq_mcu"
CXX="${CXX:-g++}"

mkdir -p "$OUT_DIR"
"$CXX" -std=gnu++14 -O1 -Wall -Wextra -I "$SK" \
    "$SK/host/test_unoq_mcu.cpp" "$SK/matrix_frame.cpp" "$SK/shape_seq.cpp" \
    -o "$OUT"
"$OUT"
