// led_out.cpp — MCU 側の 8×13 LED マトリクスへの点灯の確かめ（led_out.h）。
// led_set(true) で #32 の微笑む顔（shapes_unoq.h。12 列を左寄せ、右端の
// 1 列は消灯）、led_set(false) で全消灯（人の指示 10/13。LED3 は使わない。
// 流れる波線と表示の規則は M4）。
// Arduino_LED_Matrix（コア arduino:zephyr 1.0.0 に同梱、0.1.3）を include
// するのはこのスケッチではこのファイルだけ（docs/decisions/0001）。
// renderBitmap はマクロなので、この翻訳単位の外に見出しを出さない。
// loadPixels は const でない uint8_t* を取るので、作業用の s_frame に
// 写してから渡す。
//
// begin() の失敗時: 0.1.3 の begin() は matrixBegin() を呼んで常に 1 を
// 返す（失敗を返さない）ので、LED3 への切り替え（fallback）は持たない。
#include "led_out.h"
#include "shapes_unoq.h"
#include <Arduino_LED_Matrix.h>

static const int MATRIX_ROWS = 8;
static const int MATRIX_COLS = 13;

static ArduinoLEDMatrix s_matrix;
static uint8_t s_frame[MATRIX_ROWS][MATRIX_COLS];  // renderBitmap に渡す

void led_init() {
  s_matrix.begin();
  led_set(false);  // 起動時は消灯
}

void led_set(bool on) {
  for (int y = 0; y < MATRIX_ROWS; ++y)
    for (int x = 0; x < MATRIX_COLS; ++x)
      s_frame[y][x] = (on && x < SMILE_COLS) ? SMILE[y][x] : 0;
  s_matrix.renderBitmap(s_frame, MATRIX_ROWS, MATRIX_COLS);
}
