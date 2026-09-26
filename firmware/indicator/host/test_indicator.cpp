// firmware/indicator/host/test_indicator.cpp — 表示器の受信の解釈（indicator_rx）の PC 上のテスト（Issue #29 plan 第 5.3 節の表 1〜10）。
// Arduino に依存しない。led_rule.h は firmware/indicator/ のリンク（→ ../detector/）を通して読む。led_out.cpp は足さない（WiFiNINA）。
// data/ を使わない。実行: bash firmware/indicator/host/test_indicator.sh
#include <stdio.h>
#include <stdint.h>
#include <string.h>

#include "indicator_rx.h"
#include "led_rule.h"

static int g_fail = 0;
static int g_pass = 0;
#define CHECK(cond) do { if (cond) { g_pass++; } else { g_fail++; printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } } while (0)
#define CHECK_EQ(a, b) do { long long _a = (long long)(a), _b = (long long)(b); if (_a == _b) { g_pass++; } else { g_fail++; printf("FAIL %s:%d: %s == %s (%lld != %lld)\n", __FILE__, __LINE__, #a, #b, _a, _b); } } while (0)

static void feed1(IndicatorRx* s, uint8_t b, uint32_t now) { indicator_rx_feed(s, &b, 1, now); }

// 1. 初期化直後は、どの now でも消灯
static void test_1_init_is_off() {
  IndicatorRx s;
  indicator_rx_init(&s);
  const uint32_t nows[] = {0u, 1u, 999u, 1000u, 123456u, 0xFFFFFFFFu};
  for (uint32_t now : nows) CHECK_EQ(indicator_rx_target(&s, now), LED_OFF);
  CHECK_EQ(s.has_rx, 0);
  CHECK_EQ(s.ignored, 0);
}

// 2. 1 を受けたら 1.0 秒未満は黄、1000 ms ちょうどで消灯（led_off_due と同じ境界）
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

// 4. ゴミだけの塊は目標も消灯の時計も変えない（印字できる文字は '0' = 0x30 も捨てる）
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

// 6. 0 を受けたら消灯で、時計は進める（検出器が消灯から黄への書き込みを飛ばした窓）
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
  CHECK_EQ(indicator_rx_target(&s, 0x000002E7u), LED_GREEN);   // 差 999
  CHECK_EQ(indicator_rx_target(&s, 0x000002E8u), LED_OFF);     // 差 1000
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

// 10. 32 バイトを超える塊（indicator.ino と同じく 32 バイトずつ渡す）: 全体の最後の有効なバイトが目標
static void test_10_over_32_bytes() {
  IndicatorRx s;
  indicator_rx_init(&s);
  uint8_t first[32];
  memset(first, 1, 31);
  first[31] = 2;
  indicator_rx_feed(&s, first, sizeof first, 1000);
  CHECK_EQ(indicator_rx_target(&s, 1000), LED_GREEN);   // 1 回目だけの直後（indicator.ino はこの間に LED を書かない）
  const uint8_t second[] = {2, 2, 2, 1, 'x', 'x', 'x', 'x'};
  indicator_rx_feed(&s, second, sizeof second, 1000);
  CHECK_EQ(indicator_rx_target(&s, 1000), LED_YELLOW);
  CHECK_EQ(s.ignored, 4);
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
  printf("test_indicator: %d passed, %d failed\n", g_pass, g_fail);
  return g_fail == 0 ? 0 : 1;
}
