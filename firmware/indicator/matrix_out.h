// matrix_out.h — UNO R4 WiFi の 12×8 LED マトリクスへの書き込み（Issue #32、docs/decisions/0018 の追記）。C の型だけ。
// Arduino_LED_Matrix の見出しと LED_BUILTIN の pinMode / digitalWrite は matrix_out.cpp の中だけ（docs/decisions/0001）。
#pragma once
#include <stdint.h>

// マトリクスのタイマを取り（ArduinoLEDMatrix::begin）、消灯のフレームを書く。失敗したら以後マトリクスに書かず、LED_BUILTIN を 200 ms で点滅する。
void matrix_out_begin();
// 目標の状態（LED_OFF / LED_YELLOW / LED_GREEN）と今の時刻 [ms]。shape_seq_frame でフレームを決め、前に書いたフレームと違うときだけ renderBitmap する。
void matrix_out_show(uint8_t target, uint32_t now_ms);
// 今表示している状態（最後に matrix_out_show で反映した状態。起動失敗なら LED_OFF）。INDICATOR_PROFILE の返送に使う。
uint8_t matrix_out_state();
