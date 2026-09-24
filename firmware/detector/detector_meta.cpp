// 検出器の META フレームの JSON（detector_meta.h）。Arduino 依存なし。
#include "detector_meta.h"
#include "m2_norm.h"
#include "m2_threshold.h"
#include <stdarg.h>
#include <stdio.h>

// snprintf の結果を足す。収まらなければ偽
static bool put(char* buf, size_t cap, size_t* len, const char* fmt, ...) __attribute__((format(printf, 4, 5)));
static bool put(char* buf, size_t cap, size_t* len, const char* fmt, ...) {
  if (*len >= cap) return false;
  va_list ap;
  va_start(ap, fmt);
  int n = vsnprintf(buf + *len, cap - *len, fmt, ap);
  va_end(ap);
  if (n < 0 || (size_t)n >= cap - *len) return false;
  *len += (size_t)n;
  return true;
}

int detector_meta_build(char* buf, size_t cap, uint32_t project_id, uint32_t deploy_version) {
  size_t len = 0;
  if (!put(buf, cap, &len,
           "{\"fw\":\"detector\",\"imu_hz\":%lu,\"audio_hz\":%lu,\"window_ms\":%lu,\"hop_ms\":%lu,",
           (unsigned long)DETECTOR_IMU_HZ, (unsigned long)DETECTOR_AUDIO_HZ,
           (unsigned long)DETECTOR_WINDOW_MS, (unsigned long)DETECTOR_HOP_MS)) return -1;
  if (!put(buf, cap, &len, "\"threshold\":%.9g,", (double)M2_THRESHOLD)) return -1;
  if (!put(buf, cap, &len, "\"model\":{\"source\":\"edge-impulse\",\"project_id\":%lu,\"deploy_version\":%lu},",
           (unsigned long)project_id, (unsigned long)deploy_version)) return -1;
  if (!put(buf, cap, &len, "\"feature_set\":\"%s\",\"feature_names\":[", M2_FEATURE_SET)) return -1;
  for (int i = 0; i < M2_N_FEATURES; i++) {
    if (!put(buf, cap, &len, i ? ",\"%s\"" : "\"%s\"", M2_FEATURE_NAMES[i])) return -1;
  }
  if (!put(buf, cap, &len, "]}")) return -1;
  return (int)len;
}
