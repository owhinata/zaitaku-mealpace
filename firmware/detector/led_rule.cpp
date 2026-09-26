// led_rule.cpp — docs/decisions/0018 の LED の規則（led_rule.h）。Arduino 依存なし。
#include "led_rule.h"

void led_rule_init(LedRule* s) {
  s->last_positive_w_ms = 0;
  s->has_positive = 0;
}

uint8_t led_rule_update(LedRule* s, uint32_t window_t_ms, uint8_t positive) {
  if (positive) {                       // 陽性のたびに w_p を取り直す（陽性窓が続けば緑が延びる）
    s->last_positive_w_ms = window_t_ms;
    s->has_positive = 1;
  }
  // window_t_ms は狭義に単調増加（docs/decisions/0021）なので差は非負。境界 1750 は緑（<=）
  if (s->has_positive && (uint32_t)(window_t_ms - s->last_positive_w_ms) <= LED_GREEN_HOLD_MS) return LED_GREEN;
  return LED_YELLOW;
}
