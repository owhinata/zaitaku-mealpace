// led_out.cpp — 基板の RGB LED（NINA-W102 経由）への書き込み（led_out.h）。
// Arduino / WiFiNINA の API を呼ぶ（WiFiNINA.h、NinaPin 版の pinMode / digitalWrite、LEDR / LEDG / LEDB、SPIWIFI_ACK）のは
// 検出器ではこのファイルだけ（docs/decisions/0001）。WiFi.begin() は呼ばない（NINA の無線は使わない）。
//
// 極性: WiFiNINA（utility/nano_rp2040_support.cpp）は digitalWrite(NinaPin, LOW) で NINA に 1 を、それ以外で 0 を書く
// （NINA 側が LOW で点く配線を隠している）ので、ここでは HIGH = 点灯とする。実機で逆なら LED_ON_LEVEL / LED_OFF_LEVEL だけを直す。
//
// 準備の確認: NINA に書く前に ACK ピン（SPIWIFI_ACK。SpiDrv::begin() が INPUT にする）を読み、LOW（Arduino_SpiNINA の
// waitSlaveReady() と同じ条件）でなければ書かずに戻る。書き込みを始めた後に NINA が応答を止めた場合は防げない
// （SpiDrv::waitForSlaveReady に上限が無い）。
#include "led_out.h"
#include "led_rule.h"
#include <Arduino.h>
#include <WiFiNINA.h>

#ifndef DETECTOR_PROFILE
#define DETECTOR_PROFILE 0
#endif

static const PinStatus LED_ON_LEVEL = HIGH;    // 点灯（上の極性の注記。実機で確かめる）
static const PinStatus LED_OFF_LEVEL = LOW;    // 消灯

// 状態 → 赤・緑のピン（1 箇所）。黄 = 赤＋緑（0018）
struct LedLevels { bool red; bool green; };
static const LedLevels LEVELS[3] = {
  { false, false },   // LED_OFF
  { true,  true  },   // LED_YELLOW
  { false, true  },   // LED_GREEN
};

static uint8_t s_shown = LED_OFF;
static bool s_begun = false;
static uint32_t s_writes = 0;
static uint32_t s_skipped = 0;
static uint32_t s_write_max_us = 0;

static void write_pin(NinaPin pin, bool on) {
#if DETECTOR_PROFILE
  uint32_t t0 = (uint32_t)micros();
#endif
  digitalWrite(pin, on ? LED_ON_LEVEL : LED_OFF_LEVEL);
#if DETECTOR_PROFILE
  uint32_t dt = (uint32_t)micros() - t0;
  if (dt > s_write_max_us) s_write_max_us = dt;
#endif
  s_writes++;
}

void led_out_begin() {
  pinMode(LEDR, OUTPUT);   // 最初の NINA への書き込みで SpiDrv::begin()（NINA のリセット + 約 760 ms）が走る
  pinMode(LEDG, OUTPUT);
  pinMode(LEDB, OUTPUT);
  write_pin(LEDR, false);
  write_pin(LEDG, false);
  write_pin(LEDB, false);
  s_shown = LED_OFF;
  s_begun = true;
}

void led_out_set(uint8_t state) {
  if (!s_begun || state > LED_GREEN || state == s_shown) return;   // 変化が無ければ NINA に触らない
  bool ready = digitalRead((pin_size_t)SPIWIFI_ACK) == LOW;
  if (led_shown_after(s_shown, state, ready) == s_shown) {         // 準備ができていない: 書かず、状態を進めない
    s_skipped++;
    return;
  }
  const LedLevels& from = LEVELS[s_shown];
  const LedLevels& to = LEVELS[state];
  if (from.red != to.red) write_pin(LEDR, to.red);
  if (from.green != to.green) write_pin(LEDG, to.green);
  s_shown = state;                                                 // 書き終えてから進める
}

uint8_t led_out_state() { return s_shown; }
uint32_t led_out_writes() { return s_writes; }
uint32_t led_out_skipped() { return s_skipped; }
uint32_t led_out_write_max_us() { return s_write_max_us; }
