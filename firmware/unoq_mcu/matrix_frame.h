// matrix_frame.h — UNO Q の 8×13 LED マトリクスに書くフレームの組み立てと、
// RPC で受けた状態の範囲の検査（Issue #38、plan 38d 第 2 節）。
// Arduino に依存しない（PC のホストテスト host/test_unoq_mcu.sh でも
// ビルドする）。図形の表は shapes.h（firmware/indicator/ へのリンク。#32 の
// 生成物）。表示の規則（0018 の保持・消灯のタイムアウト）は持たない（M4）。
#pragma once
#include <stdint.h>

static const int MATRIX_ROWS = 8;
static const int MATRIX_COLS = 13;
// MatrixView.shown の初期値（まだ何も書いていない）
static const int MATRIX_VIEW_NONE = -2;

// RPC の引数（int のまま）を検査する。0〜2（0018 の LED_OFF / LED_YELLOW /
// LED_GREEN）なら *out にその値を入れて true、それ以外は *out = 0（消灯）で
// false。
bool unoq_state_from_rpc(int v, uint8_t* out);

// フレームの番号（shape_seq_frame の戻り値。-1 = 消灯）から 8×13 の点灯の
// 配列を作る。表の 12 列を左寄せにし、右端の 1 列は消灯。行は上下を反転
// する（UNO Q のマトリクスは R4 WiFi と行の向きが逆。人の目視 10/13）。
// 左右はそのまま。範囲外の番号は消灯。
void matrix_frame_compose(int frame, uint8_t out[MATRIX_ROWS][MATRIX_COLS]);

// 最後に書いたフレームの番号（同じフレームを書き直さないため）
struct MatrixView {
  int shown;
};

void matrix_view_init(MatrixView* v);
// frame が前に書いた番号と違えば shown を進めて true（書く）、同じなら
// false。初期（MATRIX_VIEW_NONE）からは消灯（-1）でも true。
bool matrix_view_update(MatrixView* v, int frame);
