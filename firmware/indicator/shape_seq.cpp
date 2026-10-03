// shape_seq.cpp — 表示器の状態 → マトリクスのフレームの番号（Issue #32）。
// Arduino 依存なし。説明は shape_seq.h。
#include "shape_seq.h"
#include "led_rule.h"
#include "shapes.h"

void shape_seq_init(ShapeSeq* s) {
  s->state = LED_OFF;
  s->t0_ms = 0;
}

int shape_seq_frame(ShapeSeq* s, uint8_t target, uint32_t now_ms) {
  // 0〜2 以外の目標は消灯として扱う（indicator_rx は渡さないが、守りとして）
  uint8_t st =
      (target == LED_YELLOW || target == LED_GREEN) ? target : (uint8_t)LED_OFF;
  if (st != s->state) {
    s->state = st;
    s->t0_ms = now_ms;
  }
  if (st == LED_GREEN) return SHAPE_SMILE;  // 微笑む顔（静止画）
  if (st != LED_YELLOW) return -1;          // 消灯

  // 流れる波線: 状態に入った時刻からの経過を周期で割った余りを FRAME_MS
  // で区切る
  uint32_t period = 0;
  for (int k = 0; k < SHAPE_WAVE_COUNT; ++k)
    period += FRAME_MS[SHAPE_WAVE_FIRST + k];
  uint32_t t = (uint32_t)(now_ms - s->t0_ms) % period;
  for (int k = 0; k < SHAPE_WAVE_COUNT; ++k) {
    uint32_t d = FRAME_MS[SHAPE_WAVE_FIRST + k];
    if (t < d) return SHAPE_WAVE_FIRST + k;
    t -= d;
  }
  return SHAPE_WAVE_FIRST + SHAPE_WAVE_COUNT - 1;  // 届かない（t < period）
}
