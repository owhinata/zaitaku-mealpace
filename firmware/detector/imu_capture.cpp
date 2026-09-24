// 検出器の IMU の取り込み（imu_capture.h）。Arduino 依存なし。ヒープは使わない。
#include "imu_capture.h"
#include <string.h>

void imu_capture_init(ImuCapture* cap) {
  memset(cap, 0, sizeof(*cap));
}

static float baseline_ms(const ImuCapture* cap) {
  if (cap->diff_count == 0) return IC_NOMINAL_PERIOD_MS;
  return (float)cap->diff_sum / (float)cap->diff_count;
}

void imu_capture_push(ImuCapture* cap, uint32_t t_ms, const float acc[3], const float gyro[3]) {
  ImuRow& r = cap->ring[cap->head];
  r.t_ms = t_ms;
  for (int i = 0; i < 3; i++) { r.acc[i] = acc[i]; r.gyro[i] = gyro[i]; }
  cap->head = (cap->head + 1) % IC_RING_ROWS;
  if (cap->count < IC_RING_ROWS) cap->count++;
  cap->total++;

  if (cap->have_last) {
    uint32_t d = t_ms - cap->last_t_ms;
    // 飛びでない差分（1.5 × 現在の基準 未満）だけを移動平均に入れる
    if ((float)d < IC_GAP_RATIO * baseline_ms(cap) && d <= 0xFFFFu) {
      if (cap->diff_count == IC_BASELINE_ROWS) {
        cap->diff_sum -= cap->diffs[cap->diff_head];
      } else {
        cap->diff_count++;
      }
      cap->diffs[cap->diff_head] = (uint16_t)d;
      cap->diff_sum += d;
      cap->diff_head = (cap->diff_head + 1) % IC_BASELINE_ROWS;
    }
  }
  cap->have_last = true;
  cap->last_t_ms = t_ms;
}

static void lock(const ImuWindowSource* src) { if (src->lock) src->lock(); }
static void unlock(const ImuWindowSource* src) { if (src->unlock) src->unlock(); }

float imu_period_baseline_ms(const ImuWindowSource* src) {
  lock(src);
  float p = baseline_ms(src->cap);
  unlock(src);
  return p;
}

void imu_window_copy(const ImuWindowSource* src, uint32_t w0_ms, uint32_t w1_ms, ImuWindow* out) {
  out->n = 0;
  out->w0_ms = w0_ms;
  out->w1_ms = w1_ms;
  out->have_prev = false;
  out->prev_t_ms = 0;
  out->truncated = false;
  lock(src);
  const ImuCapture* cap = src->cap;
  uint32_t idx = (cap->head + IC_RING_ROWS - cap->count) % IC_RING_ROWS;   // 最も古い行
  for (uint32_t i = 0; i < cap->count; i++) {
    const ImuRow& r = cap->ring[idx];
    idx = (idx + 1) % IC_RING_ROWS;
    // millis() の巻き戻りに備えて差で比べる
    int32_t rel = (int32_t)(r.t_ms - w0_ms);
    if (rel < 0) {
      out->have_prev = true;
      out->prev_t_ms = r.t_ms;
      continue;
    }
    if ((int32_t)(r.t_ms - w1_ms) >= 0) break;
    if (out->n >= IMU_MAX_ROWS) { out->truncated = true; break; }
    out->t_ms[out->n] = r.t_ms;
    for (int k = 0; k < 3; k++) { out->acc[out->n][k] = r.acc[k]; out->gyro[out->n][k] = r.gyro[k]; }
    out->n++;
  }
  unlock(src);
}

bool imu_window_valid(const ImuWindow* w, float period_ms) {
  if (w->n < 2) return false;
  float gap_ms = IC_GAP_RATIO * period_ms;
  // (i) 行数
  float min_rows = IC_MIN_ROW_RATIO * (float)IC_WINDOW_MS / period_ms;
  if ((float)w->n < min_rows) return false;
  // (ii) 窓内の飛び
  for (uint32_t i = 1; i < w->n; i++) {
    if ((float)(w->t_ms[i] - w->t_ms[i - 1]) >= gap_ms) return false;
  }
  // (iii) 窓の直前からの飛びが窓と交わる（t[0] > w0）
  if (w->have_prev && (float)(w->t_ms[0] - w->prev_t_ms) >= gap_ms && w->t_ms[0] != w->w0_ms) return false;
  // (iv) 窓の最後の行から終端までの空白（次の行との差分は必ず飛びになり、区間は窓と交わる）
  if ((float)(w->w1_ms - w->t_ms[w->n - 1]) >= gap_ms) return false;
  return true;
}

uint32_t imu_capture_total(const ImuCapture* cap) {
  return cap->total;
}
