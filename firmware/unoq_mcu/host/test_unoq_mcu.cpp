// firmware/unoq_mcu/host/test_unoq_mcu.cpp — UNO Q の MCU 側の PC 上のテスト
// （Issue #38、plan 38d 第 4 節）。状態の範囲の検査、フレームの組み立て
// （上下反転・13 列の左寄せ・右端の列は消灯）、同じフレームを書き直さない
// こと、led_tick() と同じ関数の組の流れ。
// Arduino に依存しない（led_out.cpp と rpc_server.cpp は足さない）。
// shapes.h・shape_seq.*・led_rule.h は firmware/unoq_mcu/ のリンクを通して
// 読む。data/ を使わない。実行: bash firmware/unoq_mcu/host/test_unoq_mcu.sh
#include <limits.h>
#include <stdint.h>
#include <stdio.h>

#include "led_rule.h"
#include "matrix_frame.h"
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

int main() {
  test_1_state_from_rpc();
  test_2_compose();
  test_3_view();
  test_4_boot_off_once();
  test_5_wave_every_200ms();
  test_6_smile_then_off();
  test_7_bad_rpc_is_off();
  printf("test_unoq_mcu: %d passed, %d failed\n", g_pass, g_fail);
  return g_fail == 0 ? 0 : 1;
}
