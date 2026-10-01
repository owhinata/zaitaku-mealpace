// shape_seq.h — 表示器の状態 → マトリクスのフレームの番号（Issue #32）。Arduino 依存なし（PC のテストでもビルドする）。
// 図形の表は shapes.h（shapes/gen_shapes.py の生成物）。状態 1 は動画（FRAME_MS に沿ってループ）、状態 2 は静止画、0 と 0〜2 以外は消灯（-1）。
#pragma once
#include <stdint.h>

struct ShapeSeq {
  uint8_t state;      // 今出している状態（LED_OFF / LED_YELLOW / LED_GREEN。初期は LED_OFF）
  uint32_t t0_ms;     // その状態に入った時刻 [ms]（動画の位相の起点）
};

void shape_seq_init(ShapeSeq* s);
// 目標の状態と今の時刻から出すフレームの番号（-1 = 消灯）を返す。目標が今の状態と違えば t0_ms = now_ms にして最初のフレームから。
// 同じなら t0_ms を変えない（同じ状態のバイトが続いても動画を巻き戻さない）。経過は uint32 の差（millis() の一周に強い）。
int shape_seq_frame(ShapeSeq* s, uint8_t target, uint32_t now_ms);
