// led_out.cpp — MCU 側の RGB LED（LED3）の緑 1 色（led_out.h）。
// Arduino の API（pinMode / digitalWrite、ピン名 LED3_R / LED3_G / LED3_B）を
// 呼ぶのはこのスケッチではこのファイルだけ（docs/decisions/0001）。
//
// ピンと極性（arduino:zephyr 1.0.0 の variant arduino_uno_q_stm32u585xx）:
// LED3_G = PH11。Zephyr のボード定義（led3_green）は GPIO_ACTIVE_LOW だが、
// Arduino のピン表（digital-pin-gpios）は極性の旗を付けていない（0）。
// digitalWrite は gpio_pin_set_dt でピン表の旗に従うので、LOW で点く見込み。
// 実機で逆なら LED_ON_LEVEL / LED_OFF_LEVEL だけを直す。
#include "led_out.h"
#include <Arduino.h>

// 点灯（上の注記。実機で確かめる）
static const PinStatus LED_ON_LEVEL = LOW;
static const PinStatus LED_OFF_LEVEL = HIGH;  // 消灯

void led_init() {
  // 赤・青は使わない。消しておく
  pinMode(LED3_R, OUTPUT);
  pinMode(LED3_G, OUTPUT);
  pinMode(LED3_B, OUTPUT);
  digitalWrite(LED3_R, LED_OFF_LEVEL);
  digitalWrite(LED3_G, LED_OFF_LEVEL);
  digitalWrite(LED3_B, LED_OFF_LEVEL);
}

void led_set(bool on) {
  digitalWrite(LED3_G, on ? LED_ON_LEVEL : LED_OFF_LEVEL);
}
