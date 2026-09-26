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
// LED は docs/decisions/0018 の規則で緑・黄を点ける（嚥下の目安。規則は led_rule、書き込みは led_out。README.md）。
// 窓の結果が 1.0 秒出なければ loop() の millis() のタイムアウトで消灯する。DETECT の led は実際に表示している状態（led_out_state）。
// 閾値は m2_threshold.h の M2_THRESHOLD（float32 同士で比べる）。
//
// Arduino / mbed の API を呼ぶのはこのファイルと sensors.cpp、led_out.cpp（基板の RGB LED）だけ（docs/decisions/0001）。
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
// DETECTOR_PROFILE（既定 0）: 1 で本番と同じ経路を動かしながら各段の時間・ヒープ・スタックを測り、10 秒ごとに統計行を 1 行出す
//   （窓が 1 つも出ていなくても出す。行の形と読み方は下の prof_emit のコメント）。
// DETECTOR_PROF_STOP_AFTER_MS（DETECTOR_PROFILE=1 のときだけ。既定 0 = 無効）: 起動からその ms を過ぎたら loop() は消灯の
//   タイムアウトの確認と統計行だけを行い、スライスを取らない（センサは動いたまま）。止めたときの消灯を実機で確かめる（#25）。

#include "frame.h"
#include "sensors.h"
#include "pipeline.h"
#include "classify.h"
#include "detector_meta.h"
#include "detector_frames.h"
#include "led_rule.h"
#include "led_out.h"

#ifndef DETECTOR_SEND_FEAT
#define DETECTOR_SEND_FEAT 1
#endif
#ifndef DETECTOR_PROFILE
#define DETECTOR_PROFILE 0
#endif
#if DETECTOR_PROFILE
#ifndef DETECTOR_PROF_STOP_AFTER_MS
#define DETECTOR_PROF_STOP_AFTER_MS 0
#endif
#else
#if defined(DETECTOR_PROF_STOP_AFTER_MS)
#error "DETECTOR_PROF_STOP_AFTER_MS は DETECTOR_PROFILE=1 の計測ビルドだけで使う"
#endif
#define DETECTOR_PROF_STOP_AFTER_MS 0
#endif

#if DETECTOR_PROFILE
#include <malloc.h>
#include <stdio.h>
#include "platform/mbed_mem_trace.h"
#include "rtx_os.h"
#endif

static const uint32_t IMU_SETTLE_MS = 5;   // スライスが READY になってから IMU の最後の行を待つ余裕
static char meta_buf[DETECTOR_META_CAP];
static uint32_t s_last_result_ms = 0;      // 直近に窓の結果が出た millis()（1.0 秒の消灯のタイムアウト）

// ---------- 計測（DETECTOR_PROFILE = 1 のときだけ。出すのは数字と見出しだけ） ----------
#if DETECTOR_PROFILE
static const uint32_t PROF_N = 48;               // 10 秒に 40 ホップ。取ったスライスすべて（窓が出なかったホップも）を数える
static const uint32_t PROF_PERIOD_MS = 10000;    // 統計行の間隔（時間で出す。ホップが 0 でも出す）
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
  // 先頭の 1 語は RTX の Stack Magic Word（rtx_os.h の osRtxStackMagicWord）なので塗らない・読まない
  for (uint32_t* p = stack_base + 1; p < end; p++) *p = STACK_PATTERN;
  stack_ok = true;
}
static uint32_t stack_high_water() {
  if (!stack_ok) return 0;
  uint32_t* p = stack_base + 1;
  uint32_t* top = (uint32_t*)((uint32_t)stack_base + stack_size);
  while (p < top && *p == STACK_PATTERN) p++;
  return (uint32_t)((uint32_t)top - (uint32_t)p);
}

static uint32_t micros_u32() { return (uint32_t)micros(); }

static ProfSeries p_total, p_wall, p_audio, p_imu_copy, p_imu_feat, p_nn, p_send, p_led;
static uint32_t prof_n = 0;               // この期間に取ったスライス（ホップ）の数。PROF_N で飽和
static uint32_t prof_windows = 0;         // この期間に出した窓の数
static uint32_t prof_wait_max_ms = 0;     // この期間の w1 + 5 の待ちの最大 [ms]
static uint32_t prof_rows_min = 0xFFFFFFFFu, prof_rows_max = 0;   // この期間の IMU の窓の行数（無効だった窓も含む）
static uint32_t prof_last_emit_ms = 0;
static uint32_t prof_led_begin_ms = 0;    // setup() の led_out_begin の所要 [ms]
static char prof_line[1024];

static void sort_u32(uint32_t* a, uint32_t n) {
  for (uint32_t i = 1; i < n; i++) { uint32_t v = a[i]; uint32_t j = i; while (j > 0 && a[j - 1] > v) { a[j] = a[j - 1]; j--; } a[j] = v; }
}
// 中央値と最大を ms（小数 3 桁）で "name_med=… name_max=…" に足す（n == 0 なら 0）
static int put_ms(char* buf, size_t cap, const char* name, ProfSeries* s, uint32_t n) {
  uint32_t med = 0, mx = 0;
  if (n > 0) {
    sort_u32(s->v, n);
    med = (n % 2) ? s->v[n / 2] : (s->v[n / 2 - 1] + s->v[n / 2]) / 2;
    mx = s->v[n - 1];
  }
  return snprintf(buf, cap, " %s_med=%lu.%03lu %s_max=%lu.%03lu", name, (unsigned long)(med / 1000), (unsigned long)(med % 1000),
                  name, (unsigned long)(mx / 1000), (unsigned long)(mx % 1000));
}

// 統計行（10 秒ごと。数字と見出しだけ。サンプル・特徴量・確率は出さない）。
//   prof t=<millis> imu_thread=0/1 pdm=0/1 i2c_hz=<DETECTOR_I2C_HZ> poll_ms=<DETECTOR_IMU_POLL_MS>（ビルドの切り替え。sensors.h）
//   n=<期間のホップ数> windows=<期間に出した窓> total/wall/audio/imu_copy/imu_feat/nn/send の _med/_max [ms]（期間のホップ全部。
//   窓が出なかったホップの imu_feat/nn/send は 0）wait_max [ms] imu_rows_min/max（期間。IMU の窓を取り出したホップだけ）
//   累計: hops windows hop_false_not_filled hop_false_reset hop_false_imu_short hop_false_classify last_reason（0 OK/1 窓未満/2 連番の飛び/
//   3 IMU 不足/4 推論エラー）imu_rows_last classify_err_last slices_taken slices_ready_max dropped_chunks pdm_odd_chunks pdm_gaps
//   pdm_chunks_total pdm_bytes_last pdm_cb_max [µs] imu_rows_total imu_polls heap_peak stack_main stack_imu [B]
//   LED（#25）: led_med/led_max [ms]（期間。led_out_set の所要 = 状態変更 1 回分 = digitalWrite 1〜2 回 ＋ ACK の確認。書かなかったホップは 0）、
//   累計: led_write_max [µs]（digitalWrite 1 回の最大。0018 の「1 回の書き込みの時間」はこの値）led_writes led_skipped（NINA の準備が
//   できていなくて飛ばした回数）led_state（0 消灯 / 1 黄 / 2 緑）led_begin_ms（setup() の led_out_begin の所要）
// 読み方: pdm_chunks_total が 10 秒で約 2500 増え pdm_bytes_last が 128 なら PDM は届いている。slices_taken が 40 増えなければ面が満ちていない。
//   hops が増えて windows が増えなければ hop_false_* のどれが増えるかを見る（reset なら dropped_chunks / pdm_gaps、imu_short なら imu_rows_last と
//   imu_rows_total（10 秒で約 1040 増えるはず）・imu_polls）。total_max・wall_max が 250 ms を超えていれば追いついていない。
//   total・wall には LED の書き込みの時間が入る（send には入らない）。led_max は状態変更 1 回分、led_write_max は digitalWrite 1 回分。
//   led_skipped が増え続ければ NINA の準備ができていない（その窓の DETECT の led は前の状態のまま）。
static void prof_emit() {
  SensorStats ss; sensors_stats(&ss);
  PipelineStats ps; pipeline_stats(&ps);
  size_t len = 0;
  int k;
  k = snprintf(prof_line, sizeof prof_line, "prof t=%lu imu_thread=%u pdm=%u i2c_hz=%lu poll_ms=%lu n=%lu windows=%lu",
               (unsigned long)millis(), (unsigned)ss.imu_thread, (unsigned)ss.pdm_real, (unsigned long)ss.i2c_hz,
               (unsigned long)ss.imu_poll_ms, (unsigned long)prof_n, (unsigned long)prof_windows);
  if (k > 0) len += k;
  len += put_ms(prof_line + len, sizeof prof_line - len, "total", &p_total, prof_n);
  len += put_ms(prof_line + len, sizeof prof_line - len, "wall", &p_wall, prof_n);
  len += put_ms(prof_line + len, sizeof prof_line - len, "audio", &p_audio, prof_n);
  len += put_ms(prof_line + len, sizeof prof_line - len, "imu_copy", &p_imu_copy, prof_n);
  len += put_ms(prof_line + len, sizeof prof_line - len, "imu_feat", &p_imu_feat, prof_n);
  len += put_ms(prof_line + len, sizeof prof_line - len, "nn", &p_nn, prof_n);
  len += put_ms(prof_line + len, sizeof prof_line - len, "send", &p_send, prof_n);
  len += put_ms(prof_line + len, sizeof prof_line - len, "led", &p_led, prof_n);
  k = snprintf(prof_line + len, sizeof prof_line - len,
               " wait_max=%lu imu_rows_min=%lu imu_rows_max=%lu"
               " hops=%lu windows_total=%lu hop_false_not_filled=%lu hop_false_reset=%lu hop_false_imu_short=%lu hop_false_classify=%lu"
               " last_reason=%u imu_rows_last=%lu classify_err_last=%ld"
               " slices_taken=%lu slices_ready_max=%lu dropped_chunks=%lu pdm_odd_chunks=%lu pdm_gaps=%lu pdm_chunks_total=%lu pdm_bytes_last=%lu"
               " pdm_cb_max=%lu imu_rows_total=%lu imu_polls=%lu heap_peak=%lu stack_main=%lu stack_imu=%lu"
               " led_write_max=%lu led_writes=%lu led_skipped=%lu led_state=%u led_begin_ms=%lu\n",
               (unsigned long)prof_wait_max_ms,
               (unsigned long)(prof_rows_min == 0xFFFFFFFFu ? 0 : prof_rows_min), (unsigned long)prof_rows_max,
               (unsigned long)ps.hops, (unsigned long)ps.windows, (unsigned long)ps.not_filled, (unsigned long)ps.resets,
               (unsigned long)ps.imu_short, (unsigned long)ps.classify_errors,
               (unsigned)ps.last_reason, (unsigned long)ps.imu_rows_last, (long)classify_last_error(),
               (unsigned long)ss.slices_taken, (unsigned long)ss.slices_ready_max, (unsigned long)ss.dropped_chunks,
               (unsigned long)ss.pdm_odd_chunks, (unsigned long)ss.pdm_gaps, (unsigned long)ss.pdm_chunks, (unsigned long)ss.pdm_bytes_last,
               (unsigned long)ss.pdm_cb_max_us, (unsigned long)ss.imu_rows_total, (unsigned long)ss.imu_polls,
               (unsigned long)heap_peak, (unsigned long)stack_high_water(), (unsigned long)ss.imu_stack_high_water,
               (unsigned long)led_out_write_max_us(), (unsigned long)led_out_writes(), (unsigned long)led_out_skipped(),
               (unsigned)led_out_state(), (unsigned long)prof_led_begin_ms);
  if (k > 0) len += k;
  if (len >= sizeof prof_line) len = sizeof prof_line - 1;
  Serial.write((const uint8_t*)prof_line, len);   // 数字と見出しだけ（サンプル・特徴量・確率は出さない）
  prof_n = 0;
  prof_windows = 0;
  prof_wait_max_ms = 0;
  prof_rows_min = 0xFFFFFFFFu; prof_rows_max = 0;
}

// 取ったスライスごとに呼ぶ（窓が出なかったホップも）。ok が真なら送信まで済んだホップ
static void prof_add(uint32_t total, uint32_t wall, const HopResult* r, uint32_t send, uint32_t led, bool ok, uint32_t wait_ms) {
  if (prof_n < PROF_N) {
    p_total.v[prof_n] = total; p_wall.v[prof_n] = wall;
    p_audio.v[prof_n] = r->us_audio; p_imu_copy.v[prof_n] = r->us_imu_copy;
    p_imu_feat.v[prof_n] = r->us_imu_feat; p_nn.v[prof_n] = r->us_nn; p_send.v[prof_n] = send; p_led.v[prof_n] = led;
    prof_n++;
  }
  if (ok) prof_windows++;
  if (wait_ms > prof_wait_max_ms) prof_wait_max_ms = wait_ms;
  if (r->reason == HOP_OK || r->reason == HOP_IMU_SHORT || r->reason == HOP_CLASSIFY_ERROR) {
    if (r->imu_rows < prof_rows_min) prof_rows_min = r->imu_rows;
    if (r->imu_rows > prof_rows_max) prof_rows_max = r->imu_rows;
  }
}

// 10 秒ごとに統計行を出す（loop() から毎回呼ぶ。ホップが無くても出る）
static void prof_tick() {
  uint32_t now = millis();
  if ((uint32_t)(now - prof_last_emit_ms) >= PROF_PERIOD_MS) {
    prof_emit();
    prof_last_emit_ms = now;
  }
}
#endif  // DETECTOR_PROFILE

// ---------- 入口 ----------

static void fail_blink() {
  // 起動失敗: LED_BUILTIN の 200 ms 点滅で止まる（logger と同じ。docs/decisions/0018）。RGB は消灯のまま
  led_out_set(LED_OFF);   // led_out_begin の前なら何もしない（RGB はまだ点いていない）
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
  // RGB LED（NINA）の初期化は約 760 ms 止まるので、PDM の取り込みが始まる前に済ませる。最初の窓の結果までは消灯のまま
#if DETECTOR_PROFILE
  uint32_t t_led0 = millis();
#endif
  led_out_begin();
#if DETECTOR_PROFILE
  prof_led_begin_ms = millis() - t_led0;
#endif
  if (!sensors_begin()) fail_blink();
  frame_send(Serial, FRAME_META, millis(), (const uint8_t*)meta_buf, (uint16_t)n);
#if DETECTOR_PROFILE
  stack_fill();
  prof_last_emit_ms = millis();
#endif
}

void loop() {
  // 窓の結果が 1.0 秒出なければ消灯（窓の結果とは独立した millis() のタイムアウト。ホップが無いときも毎回通る）
  if (led_out_state() != LED_OFF && led_off_due(millis(), s_last_result_ms)) led_out_set(LED_OFF);
#if DETECTOR_PROFILE
  prof_tick();
#endif
#if DETECTOR_PROF_STOP_AFTER_MS > 0
  if ((uint32_t)millis() >= (uint32_t)DETECTOR_PROF_STOP_AFTER_MS) return;   // 計測ビルドだけ: スライスを取らない（消灯の確認）
#endif
  const int16_t* slice; uint32_t t0, seq, ready_ms;
  if (!sensors_take_slice(&slice, &t0, &seq, &ready_ms)) return;       // READY の面が無ければ次の loop へ
  uint32_t w1 = pipeline_window_end_ms(t0);
#if DETECTOR_PROFILE
  uint32_t t_wait0 = millis();
#endif
  while ((int32_t)(millis() - (w1 + IMU_SETTLE_MS)) < 0) {}            // IMU の最後の行を待つ（通常 1〜5 ms）
#if DETECTOR_PROFILE
  uint32_t wait_ms = millis() - t_wait0;
  uint32_t t_take = micros_u32();
#endif
  HopResult r;
  bool ok = pipeline_hop(slice, t0, seq, sensors_imu_source(), &r);
  sensors_release_slice();                                             // 16 kHz の面を空ける（次の書き込みで上書きされる）
#if DETECTOR_PROFILE
  uint32_t t_led = micros_u32();
  uint32_t led_writes0 = led_out_writes();
#endif
  if (ok) {
    // LED を先に書き、DETECT の led には実際に表示している状態を載せる（書き込みを飛ばした窓は前の状態のまま）
    s_last_result_ms = millis();
    led_out_set(r.led);
    r.led = led_out_state();
  }
#if DETECTOR_PROFILE
  uint32_t t_send = micros_u32();
  uint32_t led_us = (led_out_writes() != led_writes0) ? t_send - t_led : 0;   // 書かなかったホップは 0
#endif
  if (ok) {
    uint8_t p[DETECT_PAYLOAD_LEN]; pack_detect(&r, p);
    frame_send(Serial, FRAME_DETECT, millis(), p, sizeof p);
#if DETECTOR_SEND_FEAT
    uint8_t f[FEAT_PAYLOAD_LEN]; pack_feat(&r, f);
    frame_send(Serial, FRAME_FEAT, millis(), f, sizeof f);
#endif
  }
#if DETECTOR_PROFILE
  uint32_t t_end = micros_u32();
  uint32_t wall_ms = millis() - ready_ms;
  prof_add(t_end - t_take, wall_ms * 1000u, &r, ok ? t_end - t_send : 0, led_us, ok, wait_ms);
#endif
}
