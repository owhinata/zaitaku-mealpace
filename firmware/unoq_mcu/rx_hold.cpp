// rx_hold.cpp — MCU の自律消灯（rx_hold.h）。Arduino に依存しない。
#include "rx_hold.h"

void rx_hold_init(RxHold* h) {
  h->seen_seq = 0;
  h->last_rx_ms = 0;
  h->has_rx = 0;
}

uint8_t rx_hold_shown(RxHold* h, uint32_t seq, uint8_t state, uint32_t now_ms) {
  if (seq != h->seen_seq) {
    h->seen_seq = seq;
    h->last_rx_ms = now_ms;
    h->has_rx = 1;
  }
  if (!h->has_rx) return 0;
  if ((uint32_t)(now_ms - h->last_rx_ms) >= UNOQ_RX_OFF_MS) {
    // 時間切れの後は次の受信まで消灯を保つ（約 49.7 日の一周で再点灯しない）
    h->has_rx = 0;
    return 0;
  }
  return state;
}
