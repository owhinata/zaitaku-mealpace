// A0 の 2 kHz の読み取り（Issue #35 の試用。docs/decisions/0022）。C の型だけを出す。
// Arduino / mbed の API（analogRead、mbed::Ticker、rtos::Thread、rtos::EventFlags、millis）は analog_in.cpp の中だけで呼ぶ。
// ビルドフラグ LOGGER_ANALOG（既定 0）が 0 のとき analog_in.cpp は空になり、logger.ino はこのヘッダを読まない。
#pragma once
#include <stdint.h>
#include <stdbool.h>

#ifndef LOGGER_ANALOG
#define LOGGER_ANALOG 0
#endif

#ifdef __cplusplus
extern "C" {
#endif

enum {
  ANALOG_HZ = 2000,   // 500 µs ごと（ハードウェアのタイマ）
  ANALOG_N = 20,      // 1 フレームのサンプル数（10 ms 分）
  ANALOG_RING = 16    // 確定した塊のリングの段数（160 ms 分）
};

// 起動からの累計（docs/decisions/0022）。META に載せて欠けを照合する。
typedef struct {
  uint32_t ticks;      // タイマ割り込みの回数
  uint32_t reads;      // analogRead を呼んだ回数（= 読んだサンプル数）
  uint32_t blocks;     // 確定した塊の数（リングに入れた数 ＋ 捨てた数）
  uint32_t dropped;    // リングが満杯で捨てた塊の数
  uint32_t lag_max_ms; // 取り出したときの millis() − 塊の t_ms の最大
  uint32_t t_ms;       // 上の値を写したときの millis()
} analog_in_stats_t;

// 読み取りを始める。失敗なら false。
bool analog_in_begin(void);
// 確定した塊が 1 つあれば、ANALOG_N 個の 12 bit 生値（古い順）と t_ms（最後のサンプルを読んだ直後の millis()）を写して true。
bool analog_in_pop(uint16_t* out_n_samples, uint32_t* out_t_ms);
// 計数を写す。
void analog_in_stats(analog_in_stats_t* out);

#ifdef __cplusplus
}
#endif
