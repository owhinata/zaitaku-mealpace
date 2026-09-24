#!/usr/bin/env bash
# firmware/detector/host/build.sh — PC 上の実行ファイル build-host/score_windows をビルドする（Issue #22 第 6.3 節、決定 C1）。
#
# 使い方（リポジトリの root から）: bash firmware/detector/host/build.sh
# 前提: 書き出した C++ ライブラリ（zip の中身）が firmware/detector/src/ に展開されていること（edge-impulse-sdk/ model-parameters/ tflite-model/）。
# 形は EI の example-standalone-inferencing の Makefile に合わせる（CMSIS は x86 では使わないので入れない。リンクで CMSIS の記号が
# 未定義になったら、EI の Makefile と同じ CMSIS-DSP の .c の部分集合を足し、log に書く）。
# CMake のターゲットは足さない（root の CMakeLists.txt は arduino-cli のタスクランナー。docs/decisions/0001）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SRC="$ROOT/firmware/detector/src"
OUT_DIR="$ROOT/build-host"
OBJ_DIR="$OUT_DIR/obj-score_windows"
OUT="$OUT_DIR/score_windows"

if [ ! -d "$SRC/edge-impulse-sdk" ]; then
    echo "書き出しがありません: $SRC/edge-impulse-sdk/ が無い。EI Studio の Deployment（C++ library、Quantized int8、EON Compiler）の zip を" >&2
    echo "firmware/detector/src/ に展開してから実行する（plan #22 第 6.1 節）。" >&2
    exit 1
fi

CXX="${CXX:-g++}"
CC="${CC:-gcc}"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 2)}"
CFLAGS=(-Os -DNDEBUG -DTF_LITE_DISABLE_X86_NEON=1 -DEI_PORTING_POSIX=1 -DEIDSP_USE_CMSIS_DSP=0
        -DEI_CLASSIFIER_TFLITE_ENABLE_CMSIS_NN=0 -Wno-strict-aliasing
        -I "$ROOT/firmware/detector" -I "$SRC")
CXXFLAGS=(-std=gnu++14 "${CFLAGS[@]}")

shopt -s nullglob
SOURCES=(
    "$ROOT/firmware/detector/host/score_windows.cpp"
    "$SRC"/tflite-model/*.cpp
    "$SRC"/edge-impulse-sdk/dsp/kissfft/*.cpp
    "$SRC"/edge-impulse-sdk/dsp/dct/*.cpp
    "$SRC"/edge-impulse-sdk/dsp/memory.cpp
    "$SRC"/edge-impulse-sdk/porting/posix/*.cpp
    "$SRC"/edge-impulse-sdk/tensorflow/lite/kernels/*.cc
    "$SRC"/edge-impulse-sdk/tensorflow/lite/kernels/internal/*.cc
    "$SRC"/edge-impulse-sdk/tensorflow/lite/micro/*.cc
    "$SRC"/edge-impulse-sdk/tensorflow/lite/micro/kernels/*.cc
    "$SRC"/edge-impulse-sdk/tensorflow/lite/micro/memory_planner/*.cc
    "$SRC"/edge-impulse-sdk/tensorflow/lite/core/api/*.cc
    "$SRC"/edge-impulse-sdk/tensorflow/lite/c/common.c
)
shopt -u nullglob

mkdir -p "$OBJ_DIR"
echo "ソース ${#SOURCES[@]} 本を $JOBS 並列でコンパイルする → $OUT"

# ソースの相対パスをオブジェクト名にする（同名のファイルが別のディレクトリにあっても衝突しない）。
# 配列は export できないので、%q で文字列にして子プロセス側で組み立て直す
export ROOT OBJ_DIR CC CXX
export CFLAGS_ARR="$(printf '%q ' "${CFLAGS[@]}")"
export CXXFLAGS_ARR="$(printf '%q ' "${CXXFLAGS[@]}")"

printf '%s\0' "${SOURCES[@]}" | xargs -0 -n 1 -P "$JOBS" bash -c '
    set -euo pipefail
    eval "CFLAGS=($CFLAGS_ARR)"; eval "CXXFLAGS=($CXXFLAGS_ARR)"
    src="$1"; rel="${src#"$ROOT"/}"; obj="$OBJ_DIR/${rel//\//__}.o"
    case "$src" in
        *.c) "$CC" "${CFLAGS[@]}" -c "$src" -o "$obj" ;;
        *)   "$CXX" "${CXXFLAGS[@]}" -c "$src" -o "$obj" ;;
    esac
' _

"$CXX" -o "$OUT" "$OBJ_DIR"/*.o -lm
echo "ビルドした: $OUT"
echo "確認: printf '%s\n' \"\$(yes 0 | head -29 | tr '\\n' ' ')\" | $OUT   （見出しに n_features 29、labels 3 クラス、input_datatype int8。3 つの確率の和が 1 ± 3/256）"
