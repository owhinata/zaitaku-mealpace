// 検出器の 1 ホップの処理（pipeline.h）。Arduino 依存なし。
#include "pipeline.h"
#include "classify.h"
#include "m2_threshold.h"
#include <string.h>

static AudioReuseState s_st;                     // 8 kHz リング、DCT、重心（bench の型）。ホップごとに上書きされる
static uint32_t s_slice_t0[PIPELINE_N_SLICES];   // 直近 4 スライスの先頭の時刻（ms）。添字 0 が最も古い
static uint32_t s_slices_in_run;                 // リセット後に受け取ったスライスの数（4 で飽和）
static uint32_t s_last_seq;
static bool s_have_seq;
static int s_swallow_ix = -1;
static PipelineStats s_stats;
static ImuWindow s_rows;                         // IMU の窓（固定配列。ヒープを使わない）

#if DETECTOR_PROFILE
static uint32_t (*s_micros)(void) = nullptr;
static uint32_t now_us() { return s_micros ? s_micros() : 0; }
void pipeline_set_clock(uint32_t (*micros_fn)(void)) { s_micros = micros_fn; }
#endif

void pipeline_init_stages() {
  audio_features_init();
  audio_reuse_init(&s_st);
  s_slices_in_run = 0;
  s_have_seq = false;
  s_last_seq = 0;
  memset(s_slice_t0, 0, sizeof(s_slice_t0));
  memset(&s_stats, 0, sizeof(s_stats));
}

bool pipeline_init() {
  pipeline_init_stages();
  if (!classify_init()) return false;
  s_swallow_ix = classify_swallow_index();
  return s_swallow_ix >= 0 && s_swallow_ix < (int)CLASSIFY_N_LABELS;
}

uint32_t pipeline_window_end_ms(uint32_t t0_ms) {
  // 次のスライスを足したとき最も古くなるのは s_slice_t0[1]（3 スライス受け取り済みなら）。それ以外は公称で t0 + 250
  if (s_slices_in_run >= PIPELINE_N_SLICES - 1) return s_slice_t0[1] + PIPELINE_WINDOW_MS;
  return t0_ms + PIPELINE_HOP_MS;
}

bool pipeline_prepare(const int16_t* slice16k, uint32_t t0_ms, uint32_t seq, const ImuWindowSource* imu, HopResult* r) {
  s_stats.hops++;
  r->reason = HOP_OK;
  r->led = 0;
  r->imu_rows = 0;
#if DETECTOR_PROFILE
  r->us_imu_copy = r->us_audio = r->us_imu_feat = r->us_nn = 0;
#endif
  bool reset = false;
  if (s_have_seq && seq != s_last_seq + 1) {
    // 連番の飛び（捨てたチャンクか PDM の欠落の後）。連続でない音声で窓を作らない
    audio_reuse_init(&s_st);
    s_slices_in_run = 0;
    s_stats.resets++;
    reset = true;
  }
  s_last_seq = seq;
  s_have_seq = true;
  for (uint32_t i = 1; i < PIPELINE_N_SLICES; i++) s_slice_t0[i - 1] = s_slice_t0[i];
  s_slice_t0[PIPELINE_N_SLICES - 1] = t0_ms;
  if (s_slices_in_run < PIPELINE_N_SLICES) s_slices_in_run++;

#if DETECTOR_PROFILE
  uint32_t ta = now_us();
#endif
  bool filled = audio_reuse_push(&s_st, slice16k, r->features + IMU_N_FEATURES);   // 16 kHz → 間引いて 8 kHz リングへ
#if DETECTOR_PROFILE
  r->us_audio = now_us() - ta;
#endif
  if (!filled) {
    r->reason = reset ? HOP_RESET : HOP_NOT_FILLED;
    if (!reset) s_stats.not_filled++;
    s_stats.last_reason = r->reason;
    return false;
  }
  uint32_t w0 = s_slice_t0[0], w1 = w0 + PIPELINE_WINDOW_MS;
#if DETECTOR_PROFILE
  uint32_t tc = now_us();
#endif
  imu_window_copy(imu, w0, w1, &s_rows);   // w0 <= t < w1。呼ぶ前に detector.ino が millis() >= w1 + 5 を待つ
  float period = imu_period_baseline_ms(imu);
#if DETECTOR_PROFILE
  r->us_imu_copy = now_us() - tc;
#endif
  r->imu_rows = s_rows.n;
  s_stats.imu_rows_last = s_rows.n;
  if (!imu_window_valid(&s_rows, period)) {
    r->reason = HOP_IMU_SHORT;
    s_stats.imu_short++;
    s_stats.last_reason = r->reason;
    return false;
  }
#if DETECTOR_PROFILE
  uint32_t ti = now_us();
#endif
  imu_features(s_rows.t_ms, s_rows.acc, s_rows.gyro, s_rows.n, r->features);
#if DETECTOR_PROFILE
  r->us_imu_feat = now_us() - ti;
#endif
  r->window_t_ms = w0;
  return true;
}

bool pipeline_hop(const int16_t* slice16k, uint32_t t0_ms, uint32_t seq, const ImuWindowSource* imu, HopResult* r) {
  if (!pipeline_prepare(slice16k, t0_ms, seq, imu, r)) return false;
#if DETECTOR_PROFILE
  uint32_t tn = now_us();
#endif
  float z[M2_N_FEATURES];
  m2_normalize(r->features, z);              // double で引いて割って float32（PC と同じ）
  float probs[CLASSIFY_N_LABELS];
  bool ok = (s_swallow_ix >= 0) && classify_run(z, probs);   // pipeline_init を通っていなければ窓を出さない
#if DETECTOR_PROFILE
  r->us_nn = now_us() - tn;
#endif
  if (!ok) {
    r->reason = HOP_CLASSIFY_ERROR;
    s_stats.classify_errors++;
    s_stats.last_reason = r->reason;
    return false;
  }
  r->prob = probs[s_swallow_ix];                          // float32
  r->positive = (r->prob >= M2_THRESHOLD) ? 1 : 0;        // float32 同士（m2_threshold.h、docs/decisions/0021）
  r->led = 0;                                             // #25 まで消灯
  s_stats.windows++;
  s_stats.last_reason = HOP_OK;
  return true;
}

void pipeline_stats(PipelineStats* out) {
  *out = s_stats;
}
