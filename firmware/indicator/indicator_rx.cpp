// indicator_rx.cpp — 表示器の受信の解釈（Issue #29）。Arduino 依存なし。説明は indicator_rx.h。
#include "indicator_rx.h"
#include "led_rule.h"

void indicator_rx_init(IndicatorRx* s) {
  s->target = LED_OFF;
  s->has_rx = 0;
  s->last_rx_ms = 0;
  s->ignored = 0;
}

void indicator_rx_feed(IndicatorRx* s, const uint8_t* buf, uint32_t n, uint32_t now_ms) {
  for (uint32_t i = 0; i < n; ++i) {
    uint8_t b = buf[i];
    if (b == LED_OFF || b == LED_YELLOW || b == LED_GREEN) {
      s->target = b;
      s->has_rx = 1;
      s->last_rx_ms = now_ms;
    } else {
      s->ignored++;
    }
  }
}

uint8_t indicator_rx_target(const IndicatorRx* s, uint32_t now_ms) {
  if (!s->has_rx || led_off_due(now_ms, s->last_rx_ms)) return LED_OFF;
  return s->target;
}
