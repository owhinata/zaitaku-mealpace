#!/usr/bin/env bash
# firmware/detector/host/test_detector.sh — 検出器の Arduino に依存しないモジュールの PC 上のテスト（Issue #24 plan 第 15 節 1）。
# 使い方（リポジトリの root から）: bash firmware/detector/host/test_detector.sh
# g++ 1 本。data/ を使わない。Edge Impulse の SDK（firmware/detector/src/）を要らない（classify はテストの中のスタブ）。
# audio_features / fft512 / imu_features は firmware/detector/ のリンク（→ firmware/bench/）を通して使う。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DET="$ROOT/firmware/detector"
OUT_DIR="$ROOT/build-host"
OUT="$OUT_DIR/test_detector"
CXX="${CXX:-g++}"

mkdir -p "$OUT_DIR"
"$CXX" -std=gnu++14 -O1 -Wall -Wextra -DAF_PROFILE=1 -I "$DET" \
    "$DET/host/test_detector.cpp" \
    "$DET/audio_capture.cpp" "$DET/imu_capture.cpp" "$DET/pipeline.cpp" "$DET/detector_meta.cpp" \
    "$DET/audio_features.cpp" "$DET/fft512.cpp" "$DET/imu_features.cpp" \
    -o "$OUT" -lm
"$OUT"
