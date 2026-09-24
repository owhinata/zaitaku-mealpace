// zaitaku-mealpace 検出器ファームウェア（Nano RP2040 Connect。Issue #24、M2）
// IMU 104 Hz と PDM マイク 16 kHz を読み、250 ms ごとに 1.0 秒窓の特徴量 29 次元（docs/decisions/0020）を計算し、
// m2_norm.h で正規化して Edge Impulse の書き出し（firmware/detector/src/、int8）で推論し、DETECT（0x04）と FEAT（0x05）を
// USB シリアルに送る（形式は docs/decisions/0021、docs/data-schema.md）。
//
// 生の音声波形は保存も送信もしない（CLAUDE.md、docs/decisions/0005）: 16 kHz の波形は audio_capture の 2 面（最長 0.5 秒）にしか
// 無く、pipeline が audio_reuse_push に渡して間引いた後は次のスライスで上書きされる。音声のフレーム（frame.h の AUDIO の ID）を
// 送る経路は無い（送信関数の呼び出しは下の META・DETECT・FEAT の 3 箇所だけ）。
// 装置が出す文字列は META（1 回）、DETECTOR_PROFILE の統計行（数字と見出しだけ）、SDK のエラー文だけ。
// サンプル・特徴量・確率を文字列で出すコードは書かない。
//
// LED は点けない（DETECT の led は 0。#25）。閾値は m2_threshold.h の M2_THRESHOLD（float32 同士で比べる）。
//
// Arduino / mbed の API を呼ぶのはこのファイルと sensors.cpp だけ（docs/decisions/0001）。
// このファイルで使うのは Serial、millis、micros、LED_BUILTIN、delay と、DETECTOR_PROFILE のときの mbed のメモリトレースと
// RTX のスレッド構造体（bench.ino と同じ流儀）。
//
// ビルド（CMakeLists.txt。SKETCH_NAME=detector で基底フラグ -I firmware/detector/src -DEI_PORTING_MBED=0 -DAF_PROFILE=1
// -DEI_CLASSIFIER_TFLITE_ENABLE_CMSIS_NN=0 -DEI_LOG_LEVEL=1 が自動で付く。BUILD_FLAGS は追加分）:
//   cmake -S . -B build-detector -DSKETCH_NAME=detector -DPORT=/dev/ttyACM0 -DPYTHON3=$PWD/.venv/bin/python3
//   cmake --build build-detector --target build && cmake --build build-detector --target upload
//   計測: -DBUILD_FLAGS="-DDETECTOR_PROFILE=1"（40 ホップごとに統計行）、FEAT を送らない: -DBUILD_FLAGS="-DDETECTOR_SEND_FEAT=0"
//   logger に戻す: build（SKETCH_NAME=logger）で build → upload
// 記録: USB を挿し直してから .venv/bin/python tools/record.py --cond water --duration 60（docs/decisions/0006）。
//
// DETECTOR_SEND_FEAT（既定 1）: FEAT を送るか。self の記録は送る。p1 の扱いは別に決める（docs/decisions/0021）。
// DETECTOR_PROFILE（既定 0）: 1 で本番と同じ経路を動かしながら各段の時間・ヒープ・スタックを測り、40 ホップごとに 1 行出す。

#include "frame.h"
#include "sensors.h"
#include "pipeline.h"
#include "classify.h"
#include "detector_meta.h"
#include "detector_frames.h"

#ifndef DETECTOR_SEND_FEAT
#define DETECTOR_SEND_FEAT 1
#endif
#ifndef DETECTOR_PROFILE
#define DETECTOR_PROFILE 0
#endif

#if DETECTOR_PROFILE
#include <malloc.h>
#include <stdio.h>
#include "platform/mbed_mem_trace.h"
#include "rtx_os.h"
#endif

static const uint32_t IMU_SETTLE_MS = 5;   // スライスが READY になってから IMU の最後の行を待つ余裕
static char meta_buf[DETECTOR_META_CAP];

// ---------- 計測（DETECTOR_PROFILE = 1 のときだけ。出すのは数字と見出しだけ） ----------
#if DETECTOR_PROFILE
static const uint32_t PROF_N = 40;   // 10 秒ごと
struct ProfSeries { uint32_t v[PROF_N]; };   // 型は最初の関数より前に置く（.ino が関数のプロトタイプを先頭に挿入するため）

static volatile size_t heap_peak = 0;
static void trace_cb(uint8_t op, void* res, void* caller, ...) {
  (void)op; (void)res; (void)caller;
  struct mallinfo mi = mallinfo();
  if ((size_t)mi.uordblks > heap_peak) heap_peak = mi.uordblks;
}

// 主スレッドのスタック（bench.ino と同じパターン塗り）
static const uint32_t STACK_PATTERN = 0x5A5A5A5Au;
static uint32_t* stack_base = nullptr;
static uint32_t stack_size = 0;
static bool stack_ok = false;
static uint32_t read_sp() { uint32_t sp; __asm volatile("mov %0, sp" : "=r"(sp)); return sp; }
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
static uint32_t stack_high_water() {
  if (!stack_ok) return 0;
  uint32_t* p = stack_base;
  uint32_t* top = (uint32_t*)((uint32_t)stack_base + stack_size);
  while (p < top && *p == STACK_PATTERN) p++;
  return (uint32_t)((uint32_t)top - (uint32_t)p);
}

static uint32_t micros_u32() { return (uint32_t)micros(); }

static ProfSeries p_total, p_wall, p_audio, p_imu_copy, p_imu_feat, p_nn, p_send;
static uint32_t prof_n = 0;
static uint32_t prof_rows_min = 0xFFFFFFFFu, prof_rows_max = 0;
static char prof_line[640];

static void sort_u32(uint32_t* a, uint32_t n) {
  for (uint32_t i = 1; i < n; i++) { uint32_t v = a[i]; uint32_t j = i; while (j > 0 && a[j - 1] > v) { a[j] = a[j - 1]; j--; } a[j] = v; }
}
// 中央値と最大を ms（小数 3 桁）で "name_med=… name_max=…" に足す
static int put_ms(char* buf, size_t cap, const char* name, ProfSeries* s, uint32_t n) {
  sort_u32(s->v, n);
  uint32_t med = (n % 2) ? s->v[n / 2] : (s->v[n / 2 - 1] + s->v[n / 2]) / 2;
  uint32_t mx = s->v[n - 1];
  return snprintf(buf, cap, " %s_med=%lu.%03lu %s_max=%lu.%03lu", name, (unsigned long)(med / 1000), (unsigned long)(med % 1000),
                  name, (unsigned long)(mx / 1000), (unsigned long)(mx % 1000));
}

static void prof_emit() {
  SensorStats ss; sensors_stats(&ss);
  PipelineStats ps; pipeline_stats(&ps);
  size_t len = 0;
  int k;
  k = snprintf(prof_line, sizeof prof_line, "prof n=%lu", (unsigned long)prof_n); if (k > 0) len += k;
  len += put_ms(prof_line + len, sizeof prof_line - len, "total", &p_total, prof_n);
  len += put_ms(prof_line + len, sizeof prof_line - len, "wall", &p_wall, prof_n);
  len += put_ms(prof_line + len, sizeof prof_line - len, "audio", &p_audio, prof_n);
  len += put_ms(prof_line + len, sizeof prof_line - len, "imu_copy", &p_imu_copy, prof_n);
  len += put_ms(prof_line + len, sizeof prof_line - len, "imu_feat", &p_imu_feat, prof_n);
  len += put_ms(prof_line + len, sizeof prof_line - len, "nn", &p_nn, prof_n);
  len += put_ms(prof_line + len, sizeof prof_line - len, "send", &p_send, prof_n);
  k = snprintf(prof_line + len, sizeof prof_line - len,
               " imu_rows_min=%lu imu_rows_max=%lu imu_short=%lu dropped_chunks=%lu pdm_odd_chunks=%lu pdm_gaps=%lu resets=%lu"
               " classify_errors=%lu pdm_cb_max=%lu heap_peak=%lu stack_main=%lu stack_imu=%lu imu_rows_total=%lu\n",
               (unsigned long)prof_rows_min, (unsigned long)prof_rows_max, (unsigned long)ps.imu_short,
               (unsigned long)ss.dropped_chunks, (unsigned long)ss.pdm_odd_chunks, (unsigned long)ss.pdm_gaps,
               (unsigned long)ps.resets, (unsigned long)ps.classify_errors, (unsigned long)ss.pdm_cb_max_us,
               (unsigned long)heap_peak, (unsigned long)stack_high_water(), (unsigned long)ss.imu_stack_high_water,
               (unsigned long)ss.imu_rows_total);
  if (k > 0) len += k;
  if (len >= sizeof prof_line) len = sizeof prof_line - 1;
  Serial.write((const uint8_t*)prof_line, len);   // 数字と見出しだけ（サンプル・特徴量・確率は出さない）
  prof_n = 0;
  prof_rows_min = 0xFFFFFFFFu; prof_rows_max = 0;
}

static void prof_add(uint32_t total, uint32_t wall, const HopResult* r, uint32_t send) {
  p_total.v[prof_n] = total; p_wall.v[prof_n] = wall;
  p_audio.v[prof_n] = r->us_audio; p_imu_copy.v[prof_n] = r->us_imu_copy;
  p_imu_feat.v[prof_n] = r->us_imu_feat; p_nn.v[prof_n] = r->us_nn; p_send.v[prof_n] = send;
  if (r->imu_rows < prof_rows_min) prof_rows_min = r->imu_rows;
  if (r->imu_rows > prof_rows_max) prof_rows_max = r->imu_rows;
  prof_n++;
  if (prof_n >= PROF_N) prof_emit();
}
#endif  // DETECTOR_PROFILE

// ---------- 入口 ----------

static void fail_blink() {
  // 起動失敗: LED_BUILTIN の 200 ms 点滅で止まる（logger と同じ。docs/decisions/0018）
  pinMode(LED_BUILTIN, OUTPUT);
  while (true) { digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN)); delay(200); }
}

void setup() {
  Serial.begin(2000000);
  while (!Serial) {}
#if DETECTOR_PROFILE
  mbed_mem_trace_set_callback(trace_cb);
  pipeline_set_clock(micros_u32);
#endif
  // 表と推論の前提を先に整え（数十 ms）、それからセンサを始める（起動直後のスライスを捨てないため）
  if (!pipeline_init()) fail_blink();
  int n = detector_meta_build(meta_buf, sizeof meta_buf, classify_project_id(), classify_deploy_version());
  if (n < 0) fail_blink();
  if (!sensors_begin()) fail_blink();
  frame_send(Serial, FRAME_META, millis(), (const uint8_t*)meta_buf, (uint16_t)n);
#if DETECTOR_PROFILE
  stack_fill();
#endif
}

void loop() {
  const int16_t* slice; uint32_t t0, seq, ready_ms;
  if (!sensors_take_slice(&slice, &t0, &seq, &ready_ms)) return;       // READY の面が無ければ次の loop へ
  uint32_t w1 = pipeline_window_end_ms(t0);
  while ((int32_t)(millis() - (w1 + IMU_SETTLE_MS)) < 0) {}            // IMU の最後の行を待つ（通常 1〜5 ms）
#if DETECTOR_PROFILE
  uint32_t t_take = micros_u32();
#endif
  HopResult r;
  bool ok = pipeline_hop(slice, t0, seq, sensors_imu_source(), &r);
  sensors_release_slice();                                             // 16 kHz の面を空ける（次の書き込みで上書きされる）
  if (!ok) return;
  uint8_t p[DETECT_PAYLOAD_LEN]; pack_detect(&r, p);
#if DETECTOR_PROFILE
  uint32_t t_send = micros_u32();
#endif
  frame_send(Serial, FRAME_DETECT, millis(), p, sizeof p);
#if DETECTOR_SEND_FEAT
  uint8_t f[FEAT_PAYLOAD_LEN]; pack_feat(&r, f);
  frame_send(Serial, FRAME_FEAT, millis(), f, sizeof f);
#endif
#if DETECTOR_PROFILE
  uint32_t t_end = micros_u32();
  uint32_t wall_ms = millis() - ready_ms;
  prof_add(t_end - t_take, wall_ms * 1000u, &r, t_end - t_send);
#endif
}
