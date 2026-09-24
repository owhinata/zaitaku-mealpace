// zaitaku-mealpace 計測用スケッチ（#17）。Nano RP2040 Connect。
// 装置上で窓ごとの特徴量（1.0 秒窓・0.25 秒ホップ）の計算コストを 3 案で測る。
// 入力は装置上で作る合成信号（synth.h）だけ。マイクと IMU は使わず、生の音声を扱わない。
// PDM.h・Arduino_LSM6DSOX.h・frame.h は含めない。
//
// Serial・micros()・mbed のメモリトレース・SystemCoreClock・RTX のスレッド構造体はこのファイルだけで触る。
// シリアルに出すのは数字と見出しだけ（合成信号の値も特徴量の値も出さない）。
//
// ビルド（案ごとに別ディレクトリ。plan #17「ビルド・書き込み・計測の手順」）:
//   cmake -S . -B build-bench2 -DSKETCH_NAME=bench -DBUILD_FLAGS="-DBENCH_CASE=2"
//   cmake --build build-bench2 --target build
// BENCH_CASE: 0 ハーネスだけ / 1 EI の MFCC と MFE / 2 0012 の音 15 次元 / 3 0012 の IMU 14 次元。
// 案1 は BUILD_FLAGS に -I<リポジトリ>/firmware/bench/src -DEI_PORTING_MBED=0 も要る。
// -DBENCH_HEAP_TABLE=1 で、コールバック内の mallinfo() の代わりに自前の表でヒープを追う（mallinfo() が止まるときの代替）。

#include <malloc.h>
#include <stdarg.h>
#include "platform/mbed_mem_trace.h"
#include "rtx_os.h"
#include "synth.h"

#ifndef BENCH_CASE
#error "BENCH_CASE を -DBENCH_CASE=N で与えてください (0..3)"
#endif

#if BENCH_CASE == 1
#include "ei_dsp_case.h"
#elif BENCH_CASE == 2
#include "audio_features.h"
#elif BENCH_CASE == 3
#include "imu_features.h"
#elif BENCH_CASE != 0
#error "BENCH_CASE は 0..3"
#endif

static const uint32_t WINDOW_SAMPLES = 16000;   // 1.0 秒 @ 16 kHz
static const uint32_t HOP_SAMPLES = 4000;       // 0.25 秒
static const uint32_t N_REPEAT = 20;            // 中央値と最大を取る回数（別に 1 回のウォームアップ）

// ---------- ヒープ（mbed のメモリトレース） ----------

static volatile size_t heap_peak = 0;

#if defined(BENCH_HEAP_TABLE) && BENCH_HEAP_TABLE
// 代替: コールバックの引数の size と戻りのポインタを小さな表に記録して自前で合計を追う
static const int HEAP_TABLE_N = 256;
static void* heap_ptr[HEAP_TABLE_N];
static size_t heap_size[HEAP_TABLE_N];
static size_t heap_in_use = 0;
static uint32_t heap_table_overflow = 0;   // 表に入りきらなかった割り当ての数（0 でなければ表は信用できない）
static void heap_table_remove(void* p) {
  if (!p) return;
  for (int i = 0; i < HEAP_TABLE_N; i++) {
    if (heap_ptr[i] == p) { heap_in_use -= heap_size[i]; heap_ptr[i] = nullptr; heap_size[i] = 0; return; }
  }
}
static void heap_table_add(void* p, size_t n) {
  if (!p) return;
  for (int i = 0; i < HEAP_TABLE_N; i++) {
    if (!heap_ptr[i]) { heap_ptr[i] = p; heap_size[i] = n; heap_in_use += n; goto added; }
  }
  heap_table_overflow++;
added:
  if (heap_in_use > heap_peak) heap_peak = heap_in_use;
}
static void trace_cb(uint8_t op, void* res, void* caller, ...) {
  (void)caller;
  va_list va;
  va_start(va, caller);
  switch (op) {
    case MBED_MEM_TRACE_MALLOC: { size_t n = va_arg(va, size_t); heap_table_add(res, n); break; }
    case MBED_MEM_TRACE_REALLOC: { void* old = va_arg(va, void*); size_t n = va_arg(va, size_t); heap_table_remove(old); heap_table_add(res, n); break; }
    case MBED_MEM_TRACE_CALLOC: { size_t num = va_arg(va, size_t); size_t sz = va_arg(va, size_t); heap_table_add(res, num * sz); break; }
    case MBED_MEM_TRACE_FREE: { void* p = va_arg(va, void*); heap_table_remove(p); break; }
    default: break;
  }
  va_end(va);
}
static size_t heap_now() { return heap_in_use; }
#else
static void trace_cb(uint8_t op, void* res, void* caller, ...) {
  (void)op; (void)res; (void)caller;
  struct mallinfo mi = mallinfo();
  if ((size_t)mi.uordblks > heap_peak) heap_peak = mi.uordblks;
}
static size_t heap_now() { return (size_t)mallinfo().uordblks; }
#endif

// ---------- スタック（RTX の主スレッドの構造体とパターン塗り） ----------

static const uint32_t STACK_PATTERN = 0x5A5A5A5Au;
static uint32_t* stack_base = nullptr;
static uint32_t stack_size = 0;
static bool stack_ok = false;

static uint32_t read_sp() {
  uint32_t sp;
  __asm volatile("mov %0, sp" : "=r"(sp));
  return sp;
}

// 現在の SP より下（少し余裕を残す）をパターンで塗る。動かない条件では stack_ok = false
static void stack_fill() {
  osRtxThread_t* th = (osRtxThread_t*)osThreadGetId();
  if (!th || !th->stack_mem || th->stack_size == 0) { stack_ok = false; return; }
  stack_base = (uint32_t*)th->stack_mem;
  stack_size = th->stack_size;
  uint32_t sp = read_sp();
  uint32_t lo = (uint32_t)stack_base, hi = lo + stack_size;
  if (sp <= lo || sp > hi) { stack_ok = false; return; }
  uint32_t* end = (uint32_t*)((sp - 256) & ~3u);
  for (uint32_t* p = stack_base; p < end; p++) *p = STACK_PATTERN;
  stack_ok = true;
}

// 下から走査して、パターンが壊れた最初の位置から上端までを高水位とする
static uint32_t stack_high_water() {
  if (!stack_ok) return 0;
  uint32_t* p = stack_base;
  uint32_t* top = (uint32_t*)((uint32_t)stack_base + stack_size);
  while (p < top && *p == STACK_PATTERN) p++;
  return (uint32_t)((uint32_t)top - (uint32_t)p);
}

// ---------- 時間 ----------

static uint32_t samples[N_REPEAT];

static void sort_u32(uint32_t* a, uint32_t n) {
  for (uint32_t i = 1; i < n; i++) {
    uint32_t v = a[i];
    uint32_t j = i;
    while (j > 0 && a[j - 1] > v) { a[j] = a[j - 1]; j--; }
    a[j] = v;
  }
}

// N 回の µs から中央値と最大を ms で出す（見出しは name と sub をつないだもの。String は使わない: ヒープを動かさない）
static void print_timing(const char* name, const char* sub, uint32_t* us, uint32_t n) {
  sort_u32(us, n);
  uint32_t med = (n % 2) ? us[n / 2] : (us[n / 2 - 1] + us[n / 2]) / 2;
  Serial.print(name);
  Serial.print(sub);
  Serial.print(" median_ms=");
  Serial.print(med / 1000.0f, 3);
  Serial.print(" max_ms=");
  Serial.println(us[n - 1] / 1000.0f, 3);
}

// before: 計測の直前の uordblks、peak: 計測直後に控えた heap_peak（表示の前に控える）
static void print_heap(const char* name, const char* sub, size_t before, size_t peak) {
  Serial.print(name);
  Serial.print(sub);
  Serial.print(" heap_before_B=");
  Serial.print((unsigned long)before);
  Serial.print(" heap_peak_abs_B=");
  Serial.print((unsigned long)peak);
  Serial.print(" heap_delta_B=");
  Serial.println((long)((long)peak - (long)before));
}

// 最適化で消えないための受け皿（値は出さない）
static volatile float sink = 0.0f;

// ---------- 合成入力 ----------

static int16_t window_buf[WINDOW_SAMPLES];
static int16_t slice_buf[HOP_SAMPLES];
static SynthRng audio_rng;
static uint32_t audio_index = 0;

static void next_slice() {
  synth_audio(&audio_rng, audio_index, HOP_SAMPLES, slice_buf);
  audio_index += HOP_SAMPLES;
}

// ---------- 案ごとの計測 ----------

#if BENCH_CASE == 1
static void bench_ei_block(EiDspBlock b, const char* name) {
  if (!ei_dsp_case_has(b)) {
    Serial.print(name);
    Serial.println(" absent");
    return;
  }
  Serial.print(name);
  Serial.print(" n_features=");
  Serial.println((unsigned long)ei_dsp_case_n_features(b));
  // 全窓
  size_t before = heap_now();
  heap_peak = before;
  ei_dsp_case_mem_reset();
  int rc = ei_dsp_case_full(b, window_buf);   // ウォームアップ
  for (uint32_t i = 0; i < N_REPEAT; i++) {
    uint32_t t0 = micros();
    rc |= ei_dsp_case_full(b, window_buf);
    samples[i] = micros() - t0;
  }
  size_t peak = heap_peak;
  uint32_t ei_peak = ei_dsp_case_mem_peak();
  Serial.print(name);
  Serial.print(" full rc=");
  Serial.println(rc);
  print_timing(name, " full", samples, N_REPEAT);
  print_heap(name, " full", before, peak);
  Serial.print(name);
  Serial.print(" full ei_track_peak_B=");
  Serial.println((unsigned long)ei_peak);
  // スライス（4 スライスで窓を満たしてから 20 ホップ）
  before = heap_now();
  heap_peak = before;
  ei_dsp_case_mem_reset();
  rc = 0;
  for (uint32_t k = 0; k < WINDOW_SAMPLES / HOP_SAMPLES; k++) {
    next_slice();
    rc |= ei_dsp_case_slice(b, slice_buf);
  }
  for (uint32_t i = 0; i < N_REPEAT; i++) {
    next_slice();
    uint32_t t0 = micros();
    rc |= ei_dsp_case_slice(b, slice_buf);
    samples[i] = micros() - t0;
  }
  peak = heap_peak;
  ei_peak = ei_dsp_case_mem_peak();
  Serial.print(name);
  Serial.print(" slice rc=");
  Serial.println(rc);
  print_timing(name, " slice", samples, N_REPEAT);
  print_heap(name, " slice", before, peak);
  Serial.print(name);
  Serial.print(" slice ei_track_peak_B=");
  Serial.println((unsigned long)ei_peak);
}

static void run_case() {
  Serial.print("ei window_samples=");
  Serial.print((unsigned long)ei_dsp_case_window_samples());
  Serial.print(" slice_samples=");
  Serial.println((unsigned long)ei_dsp_case_slice_samples());
  if (!ei_dsp_case_init()) {
    Serial.println("ei init failed");
    return;
  }
  bench_ei_block(EI_DSP_BLOCK_MFCC, "case1_mfcc");
  bench_ei_block(EI_DSP_BLOCK_MFE, "case1_mfe");
}

#elif BENCH_CASE == 2
static AudioReuseState reuse_state;
static float feat[AF_N_FEATURES];
static float stage_pre[AF_FRAME_LEN];
static float stage_power[AF_N_BINS];
static float stage_dct[AF_N_MFCC];

static void run_case() {
  audio_features_init();
  // 全窓（再利用なし）
  size_t before = heap_now();
  heap_peak = before;
  audio_features_full(window_buf, feat);   // ウォームアップ
  for (uint32_t i = 0; i < N_REPEAT; i++) {
    uint32_t t0 = micros();
    audio_features_full(window_buf, feat);
    samples[i] = micros() - t0;
    sink += feat[0];
  }
  size_t peak = heap_peak;
  print_timing("case2", " full", samples, N_REPEAT);
  print_heap("case2", " full", before, peak);
  // 再利用あり（4 スライスで窓を満たしてから 20 ホップ連続）
  before = heap_now();
  heap_peak = before;
  audio_reuse_init(&reuse_state);
  for (uint32_t k = 0; k < WINDOW_SAMPLES / HOP_SAMPLES; k++) {
    next_slice();
    audio_reuse_push(&reuse_state, slice_buf, feat);
  }
  for (uint32_t i = 0; i < N_REPEAT; i++) {
    next_slice();
    uint32_t t0 = micros();
    audio_reuse_push(&reuse_state, slice_buf, feat);
    samples[i] = micros() - t0;
    sink += feat[0];
  }
  peak = heap_peak;
  print_timing("case2", " reuse", samples, N_REPEAT);
  print_heap("case2", " reuse", before, peak);
  // 段階ごと（見立て用。1 フレーム = 400 サンプル、ゼロ交差率は窓全体）
  for (uint32_t i = 0; i < N_REPEAT + 1; i++) {
    uint32_t t0 = micros();
    audio_stage_preemphasis(window_buf + AF_FRAME_HOP, AF_FRAME_LEN, true, stage_pre);
    uint32_t dt = micros() - t0;
    if (i) samples[i - 1] = dt;
  }
  print_timing("stage", " preemphasis_400", samples, N_REPEAT);
  for (uint32_t i = 0; i < N_REPEAT + 1; i++) {
    uint32_t t0 = micros();
    audio_stage_power(stage_pre, stage_power);
    uint32_t dt = micros() - t0;
    if (i) samples[i - 1] = dt;
  }
  print_timing("stage", " hamming_fft_power_400", samples, N_REPEAT);
  for (uint32_t i = 0; i < N_REPEAT + 1; i++) {
    uint32_t t0 = micros();
    audio_stage_mel_dct(stage_power, stage_dct);
    uint32_t dt = micros() - t0;
    if (i) samples[i - 1] = dt;
    sink += stage_dct[0];
  }
  print_timing("stage", " mel_log_dct", samples, N_REPEAT);
  for (uint32_t i = 0; i < N_REPEAT + 1; i++) {
    uint32_t t0 = micros();
    float c = audio_stage_centroid(stage_power);
    uint32_t dt = micros() - t0;
    if (i) samples[i - 1] = dt;
    sink += c;
  }
  print_timing("stage", " centroid", samples, N_REPEAT);
  for (uint32_t i = 0; i < N_REPEAT + 1; i++) {
    uint32_t t0 = micros();
    float z = audio_stage_zcr(window_buf, WINDOW_SAMPLES);
    uint32_t dt = micros() - t0;
    if (i) samples[i - 1] = dt;
    sink += z;
  }
  print_timing("stage", " zcr_16000", samples, N_REPEAT);
}

#elif BENCH_CASE == 3
static uint32_t imu_t_ms[IMU_MAX_ROWS];
static float imu_acc[IMU_MAX_ROWS][3];
static float imu_gyro[IMU_MAX_ROWS][3];
static float feat[IMU_N_FEATURES];

static void run_case() {
  // 1.0 秒 ÷ 9.48 ms → t_ms < 1000 の行だけ（105〜106 行）
  SynthRng rng;
  synth_rng_seed(&rng, SYNTH_SEED_IMU);
  synth_imu(&rng, 0, IMU_MAX_ROWS, imu_t_ms, imu_acc, imu_gyro);
  uint32_t n = 0;
  while (n < IMU_MAX_ROWS && imu_t_ms[n] < 1000) n++;
  Serial.print("case3 rows=");
  Serial.println((unsigned long)n);
  size_t before = heap_now();
  heap_peak = before;
  imu_features(imu_t_ms, imu_acc, imu_gyro, n, feat);   // ウォームアップ
  for (uint32_t i = 0; i < N_REPEAT; i++) {
    uint32_t t0 = micros();
    imu_features(imu_t_ms, imu_acc, imu_gyro, n, feat);
    samples[i] = micros() - t0;
    sink += feat[0];
  }
  size_t peak = heap_peak;
  print_timing("case3", " full", samples, N_REPEAT);
  print_heap("case3", " full", before, peak);
}

#else
static void run_case() {
  // ハーネスだけ。合成入力の生成時間を参考に出す（案の時間には含めない）
  size_t before = heap_now();
  heap_peak = before;
  for (uint32_t i = 0; i < N_REPEAT; i++) {
    uint32_t t0 = micros();
    next_slice();
    samples[i] = micros() - t0;
    sink += slice_buf[0];
  }
  size_t peak = heap_peak;
  print_timing("case0", " synth_slice_4000", samples, N_REPEAT);
  print_heap("case0", "", before, peak);
}
#endif

// ---------- 入口 ----------

void setup() {
  Serial.begin(115200);
  while (!Serial) {}
  delay(1000);   // ホストがポートを開いた直後の出力は読み手側で捨てられることがあるので少し待つ
  Serial.print("bench BENCH_CASE=");
  Serial.print(BENCH_CASE);
  Serial.print(" SystemCoreClock=");
  Serial.print((unsigned long)SystemCoreClock);
  Serial.print(" N=");
  Serial.println((unsigned long)N_REPEAT);

  mbed_mem_trace_set_callback(trace_cb);
  synth_rng_seed(&audio_rng, SYNTH_SEED_AUDIO);
  synth_audio(&audio_rng, 0, WINDOW_SAMPLES, window_buf);
  audio_index = WINDOW_SAMPLES;

  Serial.print("heap_start_B=");
  Serial.println((unsigned long)heap_now());
  Serial.print("stack_size_B=");
  {
    osRtxThread_t* th = (osRtxThread_t*)osThreadGetId();
    Serial.println((unsigned long)(th ? th->stack_size : 0));
  }
  stack_fill();
  run_case();
  uint32_t hwm = stack_high_water();
  if (stack_ok) {
    Serial.print("stack_high_water_B=");
    Serial.println((unsigned long)hwm);
  } else {
    Serial.println("stack_high_water_B=unmeasured");
  }
  Serial.print("heap_end_B=");
  Serial.println((unsigned long)heap_now());
#if defined(BENCH_HEAP_TABLE) && BENCH_HEAP_TABLE
  Serial.print("heap_table_overflow=");
  Serial.println((unsigned long)heap_table_overflow);
#endif
  Serial.println("done");
}

void loop() {}
