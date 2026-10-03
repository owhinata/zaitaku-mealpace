// firmware/indicator/host/test_indicator.cpp — 表示器の PC 上のテスト。
// 受信の解釈（indicator_rx。Issue #29 plan 第 5.3 節の表 1〜10）と、
// 状態 → フレーム（shape_seq）と図形の表（shapes.h）（Issue #32 plan 第 6.6
// 節の表 11〜18）。
// Arduino に依存しない。led_rule.h は firmware/indicator/ のリンク（→
// ../detector/）を通して読む。matrix_out.cpp は足さない（Arduino_LED_Matrix）。
// data/ を使わない。実行: bash firmware/indicator/host/test_indicator.sh
#include <stdio.h>
#include <stdint.h>
#include <string.h>

#include "indicator_rx.h"
#include "led_rule.h"
#include "shape_seq.h"
#include "shapes.h"

static int g_fail = 0;
static int g_pass = 0;
#define CHECK(cond)                                          \
  do {                                                       \
    if (cond) {                                              \
      g_pass++;                                              \
    } else {                                                 \
      g_fail++;                                              \
      printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
    }                                                        \
  } while (0)
#define CHECK_EQ(a, b)                                                        \
  do {                                                                        \
    long long _a = (long long)(a), _b = (long long)(b);                       \
    if (_a == _b) {                                                           \
      g_pass++;                                                               \
    } else {                                                                  \
      g_fail++;                                                               \
      printf("FAIL %s:%d: %s == %s (%lld != %lld)\n", __FILE__, __LINE__, #a, \
             #b, _a, _b);                                                     \
    }                                                                         \
  } while (0)

static void feed1(IndicatorRx* s, uint8_t b, uint32_t now) {
  indicator_rx_feed(s, &b, 1, now);
}

// 1. 初期化直後は、どの now でも消灯
static void test_1_init_is_off() {
  IndicatorRx s;
  indicator_rx_init(&s);
  const uint32_t nows[] = {0u, 1u, 999u, 1000u, 123456u, 0xFFFFFFFFu};
  for (uint32_t now : nows) CHECK_EQ(indicator_rx_target(&s, now), LED_OFF);
  CHECK_EQ(s.has_rx, 0);
  CHECK_EQ(s.ignored, 0);
}

// 2. 1 を受けたら 1.0 秒未満は状態 1（LED_YELLOW）、1000 ms
//   ちょうどで消灯（led_off_due と同じ境界）
static void test_2_timeout_boundary() {
  IndicatorRx s;
  indicator_rx_init(&s);
  feed1(&s, 1, 1000);
  CHECK_EQ(indicator_rx_target(&s, 1000), LED_YELLOW);
  CHECK_EQ(indicator_rx_target(&s, 1999), LED_YELLOW);
  CHECK_EQ(indicator_rx_target(&s, 2000), LED_OFF);
}

// 3. 塊の中の最後の有効なバイトが目標
static void test_3_last_valid_byte() {
  IndicatorRx s;
  indicator_rx_init(&s);
  const uint8_t a[] = {2, 1};
  indicator_rx_feed(&s, a, sizeof a, 1000);
  CHECK_EQ(indicator_rx_target(&s, 1000), LED_YELLOW);
  indicator_rx_init(&s);
  const uint8_t b[] = {1, 2};
  indicator_rx_feed(&s, b, sizeof b, 1000);
  CHECK_EQ(indicator_rx_target(&s, 1000), LED_GREEN);
}

// 4. ゴミだけの塊は目標も消灯の時計も変えない（印字できる文字は '0' = 0x30
//   も捨てる）
static void test_4_garbage_only() {
  IndicatorRx s;
  indicator_rx_init(&s);
  feed1(&s, 1, 1000);
  const uint8_t g[] = {'A', 'T', '\r', 0x03, 0xFF, '0'};
  indicator_rx_feed(&s, g, sizeof g, 1500);
  CHECK_EQ(indicator_rx_target(&s, 1500), LED_YELLOW);
  CHECK_EQ(s.last_rx_ms, 1000);
  CHECK_EQ(indicator_rx_target(&s, 1999), LED_YELLOW);
  CHECK_EQ(indicator_rx_target(&s, 2000), LED_OFF);
  CHECK_EQ(s.ignored, 6);
}

// 5. 有効なバイトとゴミの混在
static void test_5_mixed() {
  IndicatorRx s;
  indicator_rx_init(&s);
  const uint8_t a[] = {2, 'x'};
  indicator_rx_feed(&s, a, sizeof a, 1000);
  CHECK_EQ(indicator_rx_target(&s, 1000), LED_GREEN);
  CHECK_EQ(s.ignored, 1);
}

// 6. 0 を受けたら消灯で、時計は進める（検出器が消灯から状態 1（LED_YELLOW）
//   への書き込みを飛ばした窓）
static void test_6_zero_is_off_and_refreshes() {
  IndicatorRx s;
  indicator_rx_init(&s);
  feed1(&s, 2, 500);
  feed1(&s, 0, 1000);
  CHECK_EQ(indicator_rx_target(&s, 1000), LED_OFF);
  CHECK_EQ(s.last_rx_ms, 1000);
  CHECK_EQ(s.has_rx, 1);
  CHECK_EQ(s.target, LED_OFF);
}

// 7. millis() の一周をまたいでも 1000 ms で消灯
static void test_7_millis_wrap() {
  IndicatorRx s;
  indicator_rx_init(&s);
  feed1(&s, 2, 0xFFFFFF00u);
  CHECK_EQ(indicator_rx_target(&s, 0x000002E7u), LED_GREEN);  // 差 999
  CHECK_EQ(indicator_rx_target(&s, 0x000002E8u), LED_OFF);    // 差 1000
}

// 8. 消灯の後に再開
static void test_8_resume_after_off() {
  IndicatorRx s;
  indicator_rx_init(&s);
  feed1(&s, 1, 1000);
  CHECK_EQ(indicator_rx_target(&s, 3000), LED_OFF);
  feed1(&s, 2, 3000);
  CHECK_EQ(indicator_rx_target(&s, 3000), LED_GREEN);
}

// 9. n = 0 は何も変えない
static void test_9_empty_feed() {
  IndicatorRx s;
  indicator_rx_init(&s);
  feed1(&s, 1, 1000);
  const uint8_t dummy[] = {2};
  indicator_rx_feed(&s, dummy, 0, 1500);
  CHECK_EQ(s.target, LED_YELLOW);
  CHECK_EQ(s.last_rx_ms, 1000);
  CHECK_EQ(s.ignored, 0);
  CHECK_EQ(s.has_rx, 1);
  indicator_rx_init(&s);
  indicator_rx_feed(&s, dummy, 0, 1500);
  CHECK_EQ(s.has_rx, 0);
  CHECK_EQ(indicator_rx_target(&s, 1500), LED_OFF);
}

// 10. 32 バイトを超える塊（indicator.ino と同じく 32 バイトずつ渡す）:
//   全体の最後の有効なバイトが目標
static void test_10_over_32_bytes() {
  IndicatorRx s;
  indicator_rx_init(&s);
  uint8_t first[32];
  memset(first, 1, 31);
  first[31] = 2;
  indicator_rx_feed(&s, first, sizeof first, 1000);
  // 1 回目だけの直後（indicator.ino はこの間に表示を書かない）
  CHECK_EQ(indicator_rx_target(&s, 1000), LED_GREEN);
  const uint8_t second[] = {2, 2, 2, 1, 'x', 'x', 'x', 'x'};
  indicator_rx_feed(&s, second, sizeof second, 1000);
  CHECK_EQ(indicator_rx_target(&s, 1000), LED_YELLOW);
  CHECK_EQ(s.ignored, 4);
}

// 11. 初期化直後と目標 LED_OFF: どの時刻でも消灯（-1）
static void test_11_off() {
  ShapeSeq q;
  shape_seq_init(&q);
  CHECK_EQ(q.state, LED_OFF);
  const uint32_t nows[] = {0u, 1u, 1199u, 1200u, 123456u, 0xFFFFFFFFu};
  for (uint32_t now : nows) CHECK_EQ(shape_seq_frame(&q, LED_OFF, now), -1);
}

// 12. 目標 LED_GREEN（状態 2）: 時刻によらず微笑む顔
static void test_12_smile_static() {
  ShapeSeq q;
  shape_seq_init(&q);
  const uint32_t nows[] = {0u, 1u, 1199u, 5000u};
  for (uint32_t now : nows)
    CHECK_EQ(shape_seq_frame(&q, LED_GREEN, now), SHAPE_SMILE);
}

// 13. 目標 LED_YELLOW（状態 1）を時刻 1000 で始める: 200 ms
//   ごとに次のフレーム、周期 1200 ms でループ
static void test_13_wave_phase() {
  ShapeSeq q;
  shape_seq_init(&q);
  CHECK_EQ(shape_seq_frame(&q, LED_YELLOW, 1000), 0);
  CHECK_EQ(shape_seq_frame(&q, LED_YELLOW, 1199), 0);
  CHECK_EQ(shape_seq_frame(&q, LED_YELLOW, 1200), 1);
  CHECK_EQ(shape_seq_frame(&q, LED_YELLOW, 1999), 4);
  CHECK_EQ(shape_seq_frame(&q, LED_YELLOW, 2000), 5);
  CHECK_EQ(shape_seq_frame(&q, LED_YELLOW, 2199), 5);
  CHECK_EQ(shape_seq_frame(&q, LED_YELLOW, 2200), 0);
}

// 14. 同じ目標が続いても巻き戻さない。状態 2 を挟んで戻ると最初から
static void test_14_no_rewind_and_restart() {
  ShapeSeq q;
  shape_seq_init(&q);
  CHECK_EQ(shape_seq_frame(&q, LED_YELLOW, 1000), 0);
  CHECK_EQ(shape_seq_frame(&q, LED_YELLOW, 1400), 2);
  CHECK_EQ(q.t0_ms, 1000);
  CHECK_EQ(shape_seq_frame(&q, LED_GREEN, 1500), SHAPE_SMILE);
  CHECK_EQ(shape_seq_frame(&q, LED_YELLOW, 2000), 0);
  CHECK_EQ(q.t0_ms, 2000);
  CHECK_EQ(shape_seq_frame(&q, LED_YELLOW, 2200), 1);
}

// 15. 消灯から戻ると最初から
static void test_15_restart_after_off() {
  ShapeSeq q;
  shape_seq_init(&q);
  CHECK_EQ(shape_seq_frame(&q, LED_YELLOW, 1000), 0);
  CHECK_EQ(shape_seq_frame(&q, LED_YELLOW, 1700), 3);
  CHECK_EQ(shape_seq_frame(&q, LED_OFF, 1800), -1);
  CHECK_EQ(shape_seq_frame(&q, LED_YELLOW, 1900), 0);
  CHECK_EQ(shape_seq_frame(&q, LED_YELLOW, 2100), 1);
}

// 16. millis() の一周をまたいでも位相が続く（差 504 ms でフレーム 2）
static void test_16_millis_wrap() {
  ShapeSeq q;
  shape_seq_init(&q);
  CHECK_EQ(shape_seq_frame(&q, LED_YELLOW, 0xFFFFFF00u), 0);
  CHECK_EQ(shape_seq_frame(&q, LED_YELLOW, 0x000000F8u), 2);
}

// 17. 0〜2 以外の目標は消灯
static void test_17_invalid_target() {
  ShapeSeq q;
  shape_seq_init(&q);
  CHECK_EQ(shape_seq_frame(&q, 3, 1000), -1);
  CHECK_EQ(shape_seq_frame(&q, 0xFF, 1000), -1);
  CHECK_EQ(shape_seq_frame(&q, LED_YELLOW, 1000), 0);
  CHECK_EQ(shape_seq_frame(&q, 3, 1100), -1);
}

// 18. 表の形: 流れる波線の 6 フレームは各列にちょうど 1 個（計 12 個）で 200
//   ms ずつ（合計 1200 ms）。微笑む顔は 1 枚で 16 個。値は 0 か 1
static void test_18_table_shape() {
  CHECK_EQ(SHAPE_WAVE_FIRST, 0);
  CHECK_EQ(SHAPE_WAVE_COUNT, 6);
  CHECK_EQ(SHAPE_SMILE, SHAPE_WAVE_FIRST + SHAPE_WAVE_COUNT);
  CHECK_EQ(sizeof FRAMES / sizeof FRAMES[0], 7);
  CHECK_EQ(sizeof FRAME_MS / sizeof FRAME_MS[0], 7);
  uint32_t period = 0;
  for (int k = 0; k < SHAPE_WAVE_COUNT; ++k) {
    int f = SHAPE_WAVE_FIRST + k;
    CHECK_EQ(FRAME_MS[f], 200);
    period += FRAME_MS[f];
    int lit = 0;
    for (int x = 0; x < 12; ++x) {
      int col = 0;
      for (int y = 0; y < 8; ++y) col += FRAMES[f][y][x];
      CHECK_EQ(col, 1);
      lit += col;
    }
    CHECK_EQ(lit, 12);
  }
  CHECK_EQ(period, 1200);
  CHECK_EQ(FRAME_MS[SHAPE_SMILE], 0);
  int smile = 0;
  for (int y = 0; y < 8; ++y)
    for (int x = 0; x < 12; ++x) smile += FRAMES[SHAPE_SMILE][y][x];
  CHECK_EQ(smile, 16);
  for (int f = 0; f < 7; ++f)
    for (int y = 0; y < 8; ++y)
      for (int x = 0; x < 12; ++x)
        CHECK(FRAMES[f][y][x] == 0 || FRAMES[f][y][x] == 1);
}

int main() {
  test_1_init_is_off();
  test_2_timeout_boundary();
  test_3_last_valid_byte();
  test_4_garbage_only();
  test_5_mixed();
  test_6_zero_is_off_and_refreshes();
  test_7_millis_wrap();
  test_8_resume_after_off();
  test_9_empty_feed();
  test_10_over_32_bytes();
  test_11_off();
  test_12_smile_static();
  test_13_wave_phase();
  test_14_no_rewind_and_restart();
  test_15_restart_after_off();
  test_16_millis_wrap();
  test_17_invalid_target();
  test_18_table_shape();
  printf("test_indicator: %d passed, %d failed\n", g_pass, g_fail);
  return g_fail == 0 ? 0 : 1;
}
