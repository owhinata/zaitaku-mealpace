// indicator.ino — 表示器（Nano RP2040 Connect。Issue #29、docs/decisions/0018 の追記）
// PC（tools/record.py --indicator）から USB シリアルで LED の状態（0 消灯 / 1 黄 / 2 緑）を 1 バイトずつ受け、基板の RGB LED を点ける。
// 状態は検出器の DETECT の led をそのまま中継したもの。規則（緑・黄の条件）は検出器が持ち、表示器は計算しない。LED は嚥下の目安で、判定ではない。
// 受けるのは状態の 1 バイトだけ（音声・特徴量・確率は受けない。docs/decisions/0005）。0〜2 以外のバイトは捨てる。
// 1.0 秒有効なバイトが届かなければ消灯（0018 の消灯の規則。led_rule.h の led_off_due）。起動直後は消灯。
// Arduino の API を呼ぶのはこのファイル（Serial、millis）と led_out.cpp（NINA のライブラリ。../detector/ へのリンク）だけ（docs/decisions/0001）。
// led_out.cpp・led_rule.h のコメントは検出器を主語にしているが、表示器では「DETECT の led」を「表示器が表示している状態」と読む。
#include "led_rule.h"
#include "led_out.h"
#include "indicator_rx.h"

#ifndef INDICATOR_PROFILE
#define INDICATOR_PROFILE 0   // 1: 受けた塊ごとに、書き込みの後で表示している状態を 1 バイト返す（遅れの計測だけ。plan #29 第 11 節）
#endif

static IndicatorRx s_rx;

void setup() {
  Serial.begin(115200);   // USB CDC なので値は使われない。1200 は使わない（ブートローダに入る）
  // while (!Serial) で待たない: PC がポートを開くまでバイトは届かず、消灯のまま（受信はホストが DTR を立ててから。USBCDC.cpp）
  led_out_begin();        // NINA のライブラリの初期化（約 760 ms）。3 ピン消灯
  indicator_rx_init(&s_rx);
}

void loop() {
  // 受信が空になるまで読む（32 バイトずつ indicator_rx_feed に渡す）。LED はその後で 1 回だけ書く（途中の古い状態を表示しない）
  uint8_t buf[32];
  uint32_t total = 0;
  uint32_t now = millis();
  while (Serial.available() > 0) {
    uint32_t n = 0;
    while (n < sizeof buf && Serial.available() > 0) {
      int c = Serial.read();
      if (c < 0) break;
      buf[n++] = (uint8_t)c;
    }
    if (n == 0) break;
    now = millis();
    indicator_rx_feed(&s_rx, buf, n, now);
    total += n;
  }
  now = millis();
  led_out_set(indicator_rx_target(&s_rx, now));   // 同じ状態なら NINA に触らない。書き込みを飛ばしたら次の loop で書き直す
#if INDICATOR_PROFILE
  if (total > 0) { uint8_t e = led_out_state(); Serial.write(&e, 1); }
#endif
}
