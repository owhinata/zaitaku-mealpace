// firmware/unoq_mcu/host/test_unoq_mcu.cpp — UNO Q の MCU 側の PC 上のテスト
// （Issue #38、plan 38d 第 4 節）。状態の範囲の検査、フレームの組み立て
// （上下反転・13 列の左寄せ・右端の列は消灯）、同じフレームを書き直さない
// こと、led_tick() と同じ関数の組の流れ、自律消灯（rx_hold、#52・0049）。
// Arduino に依存しない（led_out.cpp と rpc_server.cpp は足さない）。
// shapes.h・shape_seq.*・led_rule.h は firmware/unoq_mcu/ のリンクを通して
// 読む。data/ を使わない。実行: bash firmware/unoq_mcu/host/test_unoq_mcu.sh
#include <limits.h>
#include <stdint.h>
#include <stdio.h>

#include "led_rule.h"
#include "matrix_frame.h"
#include "rx_hold.h"
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

static const int N_FRAMES = (int)(sizeof(FRAMES) / sizeof(FRAMES[0]));

// 1. 範囲の検査: 0〜2 はそのまま true、それ以外は 0 で false
static void test_1_state_from_rpc() {
  for (int v = 0; v <= 2; ++v) {
    uint8_t out = 99;
    CHECK(unoq_state_from_rpc(v, &out));
    CHECK_EQ(out, v);
  }
  const int bad[] = {-1, 3, 255, 256, 257, INT_MIN, INT_MAX};
  for (int v : bad) {
    uint8_t out = 99;
    CHECK(!unoq_state_from_rpc(v, &out));
    CHECK_EQ(out, 0);
  }
  CHECK_EQ(LED_OFF, 0);
  CHECK_EQ(LED_YELLOW, 1);
  CHECK_EQ(LED_GREEN, 2);
}

static int count_on(uint8_t m[MATRIX_ROWS][MATRIX_COLS]) {
  int n = 0;
  for (int y = 0; y < MATRIX_ROWS; ++y)
    for (int x = 0; x < MATRIX_COLS; ++x) n += m[y][x];
  return n;
}

// 2. 組み立て: 消灯は全部 0。各フレームは上下反転・左寄せ・13 列目は 0
static void test_2_compose() {
  uint8_t m[MATRIX_ROWS][MATRIX_COLS];
  const int off_frames[] = {-1, -2, N_FRAMES, 1000};
  for (int f : off_frames) {
    matrix_frame_compose(f, m);
    CHECK_EQ(count_on(m), 0);
  }
  CHECK_EQ(N_FRAMES, 7);
  for (int f = 0; f < N_FRAMES; ++f) {
    matrix_frame_compose(f, m);
    int table_on = 0;
    for (int y = 0; y < MATRIX_ROWS; ++y) {
      for (int x = 0; x < 12; ++x) {
        CHECK_EQ(m[y][x], FRAMES[f][MATRIX_ROWS - 1 - y][x]);
        table_on += FRAMES[f][y][x];
      }
      CHECK_EQ(m[y][12], 0);
    }
    CHECK_EQ(count_on(m), table_on);
  }
  // 顔の目（表の 2・3 行目）は、反転して 4・5 行目に来る
  matrix_frame_compose(SHAPE_SMILE, m);
  CHECK_EQ(m[5][3], 1);
  CHECK_EQ(m[4][8], 1);
  CHECK_EQ(m[2][3], 0);
}

// 3. 同じフレームを書き直さない。初回は消灯でも書く
static void test_3_view() {
  MatrixView v;
  matrix_view_init(&v);
  CHECK_EQ(v.shown, MATRIX_VIEW_NONE);
  CHECK(matrix_view_update(&v, -1));
  CHECK(!matrix_view_update(&v, -1));
  CHECK(matrix_view_update(&v, 6));
  CHECK(!matrix_view_update(&v, 6));
  CHECK(matrix_view_update(&v, 0));
  CHECK(matrix_view_update(&v, -1));
}

// led_tick() と同じ組（shape_seq_frame → matrix_view_update）を合成の時刻で
// 回し、書いた回数と書いたフレームを数える
struct Sim {
  ShapeSeq seq;
  MatrixView view;
  int writes;
  int last;
};

static void sim_init(Sim* s) {
  shape_seq_init(&s->seq);
  matrix_view_init(&s->view);
  s->writes = 0;
  s->last = MATRIX_VIEW_NONE;
}

static void sim_tick(Sim* s, uint8_t state, uint32_t now) {
  int f = shape_seq_frame(&s->seq, state, now);
  if (!matrix_view_update(&s->view, f)) return;
  s->writes++;
  s->last = f;
}

// 4. 起動時は消灯を 1 回だけ書く
static void test_4_boot_off_once() {
  Sim s;
  sim_init(&s);
  for (uint32_t t = 0; t < 3000; t += 1) sim_tick(&s, LED_OFF, t);
  CHECK_EQ(s.writes, 1);
  CHECK_EQ(s.last, -1);
}

// 5. 状態 1 は 200 ms ごとにだけ書き、1.2 秒で一巡して最初のフレームに戻る
static void test_5_wave_every_200ms() {
  Sim s;
  sim_init(&s);
  sim_tick(&s, LED_OFF, 0);
  CHECK_EQ(s.writes, 1);
  const uint32_t t0 = 1000;
  int seen[6] = {0};
  for (uint32_t t = t0; t < t0 + 1200; t += 1) {
    sim_tick(&s, LED_YELLOW, t);
    if (s.last >= 0 && s.last < 6) seen[s.last] = 1;
  }
  CHECK_EQ(s.writes, 1 + 6);
  for (int k = 0; k < 6; ++k) CHECK_EQ(seen[k], 1);
  CHECK_EQ(s.last, SHAPE_WAVE_FIRST + SHAPE_WAVE_COUNT - 1);
  sim_tick(&s, LED_YELLOW, t0 + 1200);
  CHECK_EQ(s.last, SHAPE_WAVE_FIRST);
  CHECK_EQ(s.writes, 1 + 7);
  // 199 ms の間は書かない
  int w = s.writes;
  for (uint32_t t = t0 + 1201; t < t0 + 1400; ++t) sim_tick(&s, LED_YELLOW, t);
  CHECK_EQ(s.writes, w);
}

// 6. 状態 2 は 1 回だけ書いて書き直さない。0 に戻すと消灯を 1 回書く
static void test_6_smile_then_off() {
  Sim s;
  sim_init(&s);
  sim_tick(&s, LED_OFF, 0);
  for (uint32_t t = 10; t < 5000; t += 3) sim_tick(&s, LED_GREEN, t);
  CHECK_EQ(s.writes, 2);
  CHECK_EQ(s.last, SHAPE_SMILE);
  for (uint32_t t = 5000; t < 6000; t += 3) sim_tick(&s, LED_OFF, t);
  CHECK_EQ(s.writes, 3);
  CHECK_EQ(s.last, -1);
}

// 7. 範囲外の RPC は消灯として流れに入る
static void test_7_bad_rpc_is_off() {
  Sim s;
  sim_init(&s);
  sim_tick(&s, LED_GREEN, 0);
  uint8_t st = 99;
  CHECK(!unoq_state_from_rpc(7, &st));
  sim_tick(&s, st, 10);
  CHECK_EQ(s.last, -1);
}

// ---------- rx_hold（Issue #52、docs/decisions/0049） ----------

// 8. 起動から受信なし → いつでも 0
static void test_8_rx_no_rx_is_off() {
  RxHold h;
  rx_hold_init(&h);
  for (uint32_t t = 0; t < 10000; t += 7)
    CHECK_EQ(rx_hold_shown(&h, 0, LED_GREEN, t), LED_OFF);
}

// 9. 気づいた直後と 1999 ms は状態、2000 ms で 0
static void test_9_rx_off_at_2000() {
  CHECK_EQ(UNOQ_RX_OFF_MS, 2000);
  RxHold h;
  rx_hold_init(&h);
  CHECK_EQ(rx_hold_shown(&h, 1, LED_GREEN, 5000), LED_GREEN);
  CHECK_EQ(rx_hold_shown(&h, 1, LED_GREEN, 5000 + 1999), LED_GREEN);
  CHECK_EQ(rx_hold_shown(&h, 1, LED_GREEN, 5000 + 2000), LED_OFF);
  CHECK_EQ(rx_hold_shown(&h, 1, LED_GREEN, 5000 + 60000), LED_OFF);
}

// 10. 同じ seq が続いても時刻を取り直さない
static void test_10_rx_same_seq_no_refresh() {
  RxHold h;
  rx_hold_init(&h);
  for (uint32_t t = 100; t < 100 + 2000; t += 5)
    CHECK_EQ(rx_hold_shown(&h, 3, LED_YELLOW, t), LED_YELLOW);
  CHECK_EQ(rx_hold_shown(&h, 3, LED_YELLOW, 100 + 2000), LED_OFF);
}

// 11. seq が進めば同じ状態でも取り直す（0.25 秒ごとの受信なら 10 秒でも
// 消えない）
static void test_11_rx_refresh_every_250ms() {
  RxHold h;
  rx_hold_init(&h);
  uint32_t seq = 0;
  for (uint32_t t = 0; t <= 10000; t += 10) {
    if (t % 250 == 0) seq++;
    CHECK_EQ(rx_hold_shown(&h, seq, LED_YELLOW, t), LED_YELLOW);
  }
}

// 12. 状態 0 を受けても取り直す（0 のまま）。消灯の後に受ければ戻る
static void test_12_rx_off_state_and_recover() {
  RxHold h;
  rx_hold_init(&h);
  CHECK_EQ(rx_hold_shown(&h, 1, LED_OFF, 0), LED_OFF);
  CHECK_EQ(h.has_rx, 1);
  CHECK_EQ(h.last_rx_ms, 0);
  CHECK_EQ(rx_hold_shown(&h, 2, LED_OFF, 1500), LED_OFF);
  CHECK_EQ(h.last_rx_ms, 1500);
  CHECK_EQ(rx_hold_shown(&h, 2, LED_GREEN, 9000), LED_OFF);  // 時間切れ
  CHECK_EQ(rx_hold_shown(&h, 3, LED_GREEN, 9001), LED_GREEN);
  CHECK_EQ(rx_hold_shown(&h, 3, LED_GREEN, 9001 + 1999), LED_GREEN);
  CHECK_EQ(rx_hold_shown(&h, 3, LED_GREEN, 9001 + 2000), LED_OFF);
}

// 13. millis() の一周（0xFFFFFF00 付近から 0 をまたぐ）
static void test_13_rx_millis_wrap() {
  RxHold h;
  rx_hold_init(&h);
  const uint32_t t0 = 0xFFFFFF00u;
  CHECK_EQ(rx_hold_shown(&h, 1, LED_GREEN, t0), LED_GREEN);
  CHECK_EQ(rx_hold_shown(&h, 1, LED_GREEN, 0x00000010u), LED_GREEN);
  CHECK_EQ(rx_hold_shown(&h, 1, LED_GREEN, t0 + 1999), LED_GREEN);
  CHECK_EQ(rx_hold_shown(&h, 1, LED_GREEN, t0 + 2000), LED_OFF);
}

// 14. seq の一周（0xFFFFFFFF → 0）も受信として数える
static void test_14_rx_seq_wrap() {
  RxHold h;
  rx_hold_init(&h);
  CHECK_EQ(rx_hold_shown(&h, 0xFFFFFFFFu, LED_YELLOW, 0), LED_YELLOW);
  CHECK_EQ(rx_hold_shown(&h, 0, LED_YELLOW, 1900), LED_YELLOW);
  CHECK_EQ(rx_hold_shown(&h, 0, LED_YELLOW, 1900 + 1999), LED_YELLOW);
  CHECK_EQ(rx_hold_shown(&h, 0, LED_YELLOW, 1900 + 2000), LED_OFF);
}

// 16. 時間切れの後は、最後の受信から 2^32 ms を超えて一周しても 0 のまま
// （次の受信までラッチ。adversarial-review の指摘、#52）
static void test_16_rx_off_latched_over_wrap() {
  RxHold h;
  rx_hold_init(&h);
  const uint32_t t0 = 1000;
  CHECK_EQ(rx_hold_shown(&h, 1, LED_GREEN, t0), LED_GREEN);
  CHECK_EQ(rx_hold_shown(&h, 1, LED_GREEN, t0 + 2000), LED_OFF);
  CHECK_EQ(h.has_rx, 0);
  for (uint32_t t = t0 + 2001; t < t0 + 10000; t += 97)
    CHECK_EQ(rx_hold_shown(&h, 1, LED_GREEN, t), LED_OFF);
  // last_rx_ms + 0x100000000 + 100（uint32 で）。ラッチが無いと再点灯する
  const uint32_t wrapped = (uint32_t)((uint64_t)t0 + 0x100000000ull + 100);
  CHECK_EQ(rx_hold_shown(&h, 1, LED_GREEN, wrapped), LED_OFF);
  CHECK_EQ(rx_hold_shown(&h, 1, LED_GREEN, wrapped + 1000), LED_OFF);
  // 新しい受信で戻る
  CHECK_EQ(rx_hold_shown(&h, 2, LED_GREEN, wrapped + 1001), LED_GREEN);
}

// 15. led_tick() と同じ流れ（rx_hold_shown → shape_seq_frame →
// matrix_view_update）で、時間切れのときに全消灯のフレームを 1 回だけ書く
static void test_15_rx_flow_writes_off_once() {
  Sim s;
  sim_init(&s);
  RxHold h;
  rx_hold_init(&h);
  sim_tick(&s, rx_hold_shown(&h, 0, LED_OFF, 0), 0);  // 起動の消灯
  CHECK_EQ(s.writes, 1);
  sim_tick(&s, rx_hold_shown(&h, 1, LED_GREEN, 100), 100);
  CHECK_EQ(s.writes, 2);
  CHECK_EQ(s.last, SHAPE_SMILE);
  for (uint32_t t = 101; t < 100 + 2000; ++t)
    sim_tick(&s, rx_hold_shown(&h, 1, LED_GREEN, t), t);
  CHECK_EQ(s.writes, 2);
  for (uint32_t t = 100 + 2000; t < 100 + 6000; ++t)
    sim_tick(&s, rx_hold_shown(&h, 1, LED_GREEN, t), t);
  CHECK_EQ(s.writes, 3);
  CHECK_EQ(s.last, -1);
  uint8_t m[MATRIX_ROWS][MATRIX_COLS];
  matrix_frame_compose(s.last, m);
  CHECK_EQ(count_on(m), 0);
}

int main() {
  test_1_state_from_rpc();
  test_2_compose();
  test_3_view();
  test_4_boot_off_once();
  test_5_wave_every_200ms();
  test_6_smile_then_off();
  test_7_bad_rpc_is_off();
  test_8_rx_no_rx_is_off();
  test_9_rx_off_at_2000();
  test_10_rx_same_seq_no_refresh();
  test_11_rx_refresh_every_250ms();
  test_12_rx_off_state_and_recover();
  test_13_rx_millis_wrap();
  test_14_rx_seq_wrap();
  test_15_rx_flow_writes_off_once();
  test_16_rx_off_latched_over_wrap();
  printf("test_unoq_mcu: %d passed, %d failed\n", g_pass, g_fail);
  return g_fail == 0 ? 0 : 1;
}
