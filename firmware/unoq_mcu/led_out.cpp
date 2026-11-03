// led_out.cpp — MCU 側の 8×13 LED マトリクスの表示（led_out.h）。
// Arduino_LED_Matrix（コア arduino:zephyr 1.0.0 に同梱、0.1.3）と millis()
// を呼ぶのはこのスケッチではこのファイルだけ（docs/decisions/0001）。
// renderBitmap はマクロなので、この翻訳単位の外に見出しを出さない。
//
// スレッド: led_set_state は RPC のハンドラ（RouterBridge の別スレッド
// "bridge"）から呼ばれ、led_tick は loop()（main スレッド）から呼ばれる。
// 2 つが共有するのは状態と受信の数の 2 つだけで、どちらも std::atomic。
// ハンドラは millis() を呼ばない（時刻は main スレッドだけが取る）。
// ShapeSeq・MatrixView・RxHold・描画の配列とマトリクスは main スレッド
// だけが触る。
//
// 消灯（docs/decisions/0049）: loop() が受信に気づいてから約 2 秒
// （2000 ms）次の受信に気づかなければ消灯（rx_hold.h）。保持は持たない。
//
// begin() の失敗時: 0.1.3 の begin() は常に 1 を返すので、切り替えは持たない。
#include "led_out.h"
#include <Arduino_LED_Matrix.h>
#include <atomic>
#include "matrix_frame.h"
#include "rx_hold.h"
#include "shape_seq.h"

static_assert(ATOMIC_CHAR_LOCK_FREE == 2,
              "uint8_t の atomic が lock-free でない");
static_assert(std::atomic<uint32_t>::is_always_lock_free,
              "uint32_t の atomic が lock-free でない");

static std::atomic<uint8_t> s_state{0};  // 起動時は消灯
// 受信の数（ハンドラが状態を書いた後に 1 つ増やす）
static std::atomic<uint32_t> s_rx_seq{0};

static ArduinoLEDMatrix s_matrix;
static ShapeSeq s_seq;
static MatrixView s_view;
static RxHold s_hold;
static uint8_t s_frame[MATRIX_ROWS][MATRIX_COLS];  // renderBitmap に渡す

void led_init() {
  s_matrix.begin();
  shape_seq_init(&s_seq);
  matrix_view_init(&s_view);
  rx_hold_init(&s_hold);
  led_tick();  // 全消灯を書く
}

void led_set_state(uint8_t state) {
  s_state.store(state, std::memory_order_relaxed);
  s_rx_seq.fetch_add(1, std::memory_order_release);
}

void led_tick() {
  // seq を先に読む（seq が新しければ状態も同じかより新しい）
  uint32_t seq = s_rx_seq.load(std::memory_order_acquire);
  uint8_t st = s_state.load(std::memory_order_relaxed);
  uint32_t now = millis();
  uint8_t shown = rx_hold_shown(&s_hold, seq, st, now);
  int f = shape_seq_frame(&s_seq, shown, now);
  if (!matrix_view_update(&s_view, f)) return;
  matrix_frame_compose(f, s_frame);
  s_matrix.renderBitmap(s_frame, MATRIX_ROWS, MATRIX_COLS);
}
