// indicator_rx.h — 表示器の受信の解釈（Issue #29）。Arduino 依存なし（PC のテストでもビルドする）。
// 受けるのは LED の状態の生の 1 バイト（0x00 消灯 / 0x01 黄 / 0x02 緑。led_rule.h の LedState）。同期バイト・区切り・検査は無い。
// 0〜2 以外のバイトは捨てる（端末の文字列などが届いても表示を変えず、消灯の時計も進めない）。
#pragma once
#include <stdint.h>

struct IndicatorRx {
  uint8_t target;          // 最後に受けた有効な状態（LED_OFF / LED_YELLOW / LED_GREEN）
  uint8_t has_rx;          // 有効なバイトを 1 つ以上受けたか
  uint32_t last_rx_ms;     // 最後に有効なバイトを受けた時刻 [ms]（indicator.ino が渡す now_ms）
  uint32_t ignored;        // 捨てたバイトの累計（0〜2 以外）
};

void indicator_rx_init(IndicatorRx* s);
// 受けたバイト列を反映する。0〜2 のバイトだけを有効とし、最後の有効なバイトを target にして last_rx_ms = now_ms とする。
// 0〜2 以外は捨てて ignored に数え、last_rx_ms を進めない。
void indicator_rx_feed(IndicatorRx* s, const uint8_t* buf, uint32_t n, uint32_t now_ms);
// 今点ける状態: 有効なバイトを受けていない、または led_off_due(now_ms, last_rx_ms) なら LED_OFF、そうでなければ target。
uint8_t indicator_rx_target(const IndicatorRx* s, uint32_t now_ms);
