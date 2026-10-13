// led_out.cpp — MCU 側の 8×13 LED マトリクスの表示（led_out.h）。
// Arduino_LED_Matrix（コア arduino:zephyr 1.0.0 に同梱、0.1.3）と millis()
// を呼ぶのはこのスケッチではこのファイルだけ（docs/decisions/0001）。
// renderBitmap はマクロなので、この翻訳単位の外に見出しを出さない。
//
// スレッド: led_set_state は RPC のハンドラ（RouterBridge の別スレッド
// "bridge"）から呼ばれ、led_tick は loop()（main スレッド）から呼ばれる。
// 2 つが共有するのは状態の 1 つだけで、std::atomic<uint8_t>（relaxed）にする。
// ShapeSeq・MatrixView・描画の配列とマトリクスは main スレッドだけが触る。
//
// begin() の失敗時: 0.1.3 の begin() は常に 1 を返すので、切り替えは持たない。
#include "led_out.h"
#include <Arduino_LED_Matrix.h>
#include <atomic>
#include "matrix_frame.h"
#include "shape_seq.h"

static_assert(ATOMIC_CHAR_LOCK_FREE == 2,
              "uint8_t の atomic が lock-free でない");

static std::atomic<uint8_t> s_state{0};  // 起動時は消灯

static ArduinoLEDMatrix s_matrix;
static ShapeSeq s_seq;
static MatrixView s_view;
static uint8_t s_frame[MATRIX_ROWS][MATRIX_COLS];  // renderBitmap に渡す

void led_init() {
  s_matrix.begin();
  shape_seq_init(&s_seq);
  matrix_view_init(&s_view);
  led_tick();  // 全消灯を書く
}

void led_set_state(uint8_t state) {
  s_state.store(state, std::memory_order_relaxed);
}

void led_tick() {
  uint8_t st = s_state.load(std::memory_order_relaxed);
  int f = shape_seq_frame(&s_seq, st, millis());
  if (!matrix_view_update(&s_view, f)) return;
  matrix_frame_compose(f, s_frame);
  s_matrix.renderBitmap(s_frame, MATRIX_ROWS, MATRIX_COLS);
}
