// A0 の 2 kHz の読み取り（analog_in.h）。Issue #35 の試用、docs/decisions/0022。
// T2: mbed::Ticker（500 µs）の割り込みは EventFlags を立てるだけ。analogRead は mbed::AnalogIn のミューテックスを取るので
// 割り込みから呼べない。osPriorityRealtime のスレッドがフラグを待ち、起きるたびに 1 回読む。
// 塊（ANALOG_N サンプル）が揃ったら millis() を取って確定し、リングに入れる（満杯なら捨てて数える。上書きしない）。
// リングはこのスレッドが書き、loop() が analog_in_pop で読む単一生産者・単一消費者。
#include "analog_in.h"

#if LOGGER_ANALOG

#include <Arduino.h>
#include <chrono>
#include "drivers/Ticker.h"
#include "platform/mbed_critical.h"
#include "rtos/EventFlags.h"
#include "rtos/Thread.h"

static const uint32_t FLAG_TICK = 0x1;
static const uint32_t THREAD_STACK_BYTES = 1024;

typedef struct {
  uint16_t v[ANALOG_N];
  uint32_t t_ms;
} analog_block_t;

static unsigned char s_stack[THREAD_STACK_BYTES] __attribute__((aligned(8)));
static rtos::Thread s_thread(osPriorityRealtime, THREAD_STACK_BYTES, s_stack, "analog");
static rtos::EventFlags s_flags;
static mbed::Ticker s_ticker;

static analog_block_t s_ring[ANALOG_RING];
static volatile uint32_t s_head = 0;   // スレッドだけが進める
static volatile uint32_t s_tail = 0;   // loop() だけが進める

static volatile uint32_t s_ticks = 0;      // 割り込みだけが書く
static volatile uint32_t s_reads = 0;      // スレッドだけが書く
static volatile uint32_t s_blocks = 0;     // スレッドだけが書く
static volatile uint32_t s_dropped = 0;    // スレッドだけが書く
static volatile uint32_t s_lag_max_ms = 0; // loop() だけが書く

static void on_tick() {
  s_ticks++;
  s_flags.set(FLAG_TICK);
}

static void thread_main() {
  uint16_t cur[ANALOG_N];
  int k = 0;
  while (true) {
    s_flags.wait_any(FLAG_TICK);
    cur[k++] = (uint16_t)analogRead(A0);
    s_reads++;
    if (k < ANALOG_N) continue;
    uint32_t t = millis();
    k = 0;
    s_blocks++;
    uint32_t head = s_head;
    if (head - s_tail >= (uint32_t)ANALOG_RING) {
      s_dropped++;
      continue;
    }
    analog_block_t* b = &s_ring[head % ANALOG_RING];
    memcpy(b->v, cur, sizeof(cur));
    b->t_ms = t;
    __asm__ volatile("" ::: "memory");
    s_head = head + 1;
  }
}

bool analog_in_begin(void) {
  analogReadResolution(12);
  (void)analogRead(A0);   // mbed::AnalogIn をここで作る（スレッドの中で new しない）
  if (s_thread.start(thread_main) != osOK) return false;
  s_ticker.attach(&on_tick, std::chrono::microseconds(1000000 / ANALOG_HZ));
  return true;
}

bool analog_in_pop(uint16_t* out_n_samples, uint32_t* out_t_ms) {
  uint32_t tail = s_tail;
  if (s_head == tail) return false;
  __asm__ volatile("" ::: "memory");
  const analog_block_t* b = &s_ring[tail % ANALOG_RING];
  memcpy(out_n_samples, b->v, sizeof(b->v));
  uint32_t t = b->t_ms;
  __asm__ volatile("" ::: "memory");
  s_tail = tail + 1;
  *out_t_ms = t;
  uint32_t lag = millis() - t;
  if (lag > s_lag_max_ms) s_lag_max_ms = lag;
  return true;
}

void analog_in_stats(analog_in_stats_t* out) {
  core_util_critical_section_enter();
  out->ticks = s_ticks;
  out->reads = s_reads;
  out->blocks = s_blocks;
  out->dropped = s_dropped;
  out->lag_max_ms = s_lag_max_ms;
  out->t_ms = millis();
  core_util_critical_section_exit();
}

#endif  // LOGGER_ANALOG
