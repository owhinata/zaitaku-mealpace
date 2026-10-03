// indicator.ino — 表示器（Arduino UNO R4 WiFi の 12×8 LED マトリクス。Issue
// #29・#32、docs/decisions/0018 の追記）
// PC（tools/record.py --indicator）から USB シリアルで状態（0 消灯 / 1
// まだ確認していない目安 / 2 確認した目安）を 1 バイトずつ受け、形で示す
// （1 = 流れる波線の動画、2 = 微笑む顔、0 = 消灯）。状態は検出器の DETECT の
// led をそのまま中継したもの。規則は検出器が持ち、表示器は計算しない。
// 表示は嚥下の目安で、判定ではない。受けるのは状態の 1
// バイトだけ（音声・特徴量・確率は受けない。docs/decisions/0005）。0〜2
// 以外のバイトは捨てる。
// 1.0 秒有効なバイトが届かなければ消灯（led_rule.h の led_off_due）。
// 起動直後は消灯。
// Arduino の API を呼ぶのはこのファイル（Serial、millis）と
// matrix_out.cpp（Arduino_LED_Matrix、LED_BUILTIN）だけ（
// docs/decisions/0001）。
#include "led_rule.h"
#include "indicator_rx.h"
#include "matrix_out.h"

#ifndef INDICATOR_PROFILE
// 1: 受けた塊ごとに、書き込みの後で表示している状態を 1
// バイト返す（遅れの計測だけ。plan #32 第 10.3 節）
#define INDICATOR_PROFILE 0
#endif

static IndicatorRx s_rx;

void setup() {
  // UNO R4 WiFi の Serial は ESP32-S3 の USB ブリッジにつながる UART で、PC
  // 側のボーレートが効く（record.py の INDICATOR_BAUD と揃える）。
  // 1200 で開くとブートローダに入る（書き込みの 1200 bps のタッチ）ので使わない
  Serial.begin(115200);
  // while (!Serial) は書かない: UART の operator bool は常に true（待たない）。
  // PC がポートを開いて送るまでは消灯のまま
  // マトリクスのタイマを取り、消灯。取れなければ LED_BUILTIN の 200 ms
  // 点滅（matrix_out.cpp）
  matrix_out_begin();
  indicator_rx_init(&s_rx);
}

void loop() {
  // 受信が空になるまで読む（32 バイトずつ indicator_rx_feed に渡す。#29
  // と同じ）。表示はその後で 1 回だけ決める
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
  // フレームが変わったときだけマトリクスに書く。動画は毎 loop で時刻から進める
  matrix_out_show(indicator_rx_target(&s_rx, now), now);
#if INDICATOR_PROFILE
  if (total > 0) {
    uint8_t e = matrix_out_state();
    Serial.write(&e, 1);
  }
#endif
}
