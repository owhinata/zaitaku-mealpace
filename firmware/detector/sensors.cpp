// 検出器のセンサの読み出し（sensors.h）。Arduino / mbed の API はこのファイルと detector.ino だけで呼ぶ。
#include "sensors.h"
#include "audio_capture.h"

#include <Arduino.h>
#include <Arduino_LSM6DSOX.h>
#include <PDM.h>
#include <chrono>
#include "platform/mbed_critical.h"
#include "rtos/Thread.h"
#include "rtos/ThisThread.h"

#ifndef DETECTOR_PROFILE
#define DETECTOR_PROFILE 0
#endif

static const int AUDIO_HZ = 16000;
static const int AUDIO_CHUNK = 256;               // logger と同じ受け皿（512 B）。PDM.setBufferSize(512)
static int16_t pdm_buf[AUDIO_CHUNK];              // コールバックの中で読み切り、audio_capture の面に写してすぐ捨てる
static volatile uint32_t s_pdm_cb_max_us = 0;

static const uint32_t IMU_POLL_MS = 2;
static const uint32_t IMU_STACK_BYTES = 2048;
static unsigned char imu_stack[IMU_STACK_BYTES] __attribute__((aligned(8)));
static rtos::Thread imu_thread(osPriorityAboveNormal, IMU_STACK_BYTES, imu_stack, "imu");
static ImuCapture s_imu;
static ImuWindowSource s_imu_src;

#if DETECTOR_PROFILE
static const uint32_t STACK_PATTERN = 0x5A5A5A5Au;
#endif

// --- PDM（割り込みの中。logger.ino の on_pdm と同じ順） ---
static void on_pdm() {
#if DETECTOR_PROFILE
  uint32_t t_in = micros();
#endif
  int n = PDM.available();
  if (n > (int)sizeof(pdm_buf)) n = sizeof(pdm_buf);
  if (n > 0) PDM.read(pdm_buf, n);
  uint32_t t = millis();
  audio_capture_push_chunk(pdm_buf, (uint32_t)(n < 0 ? 0 : n), t);
#if DETECTOR_PROFILE
  uint32_t dt = micros() - t_in;
  if (dt > s_pdm_cb_max_us) s_pdm_cb_max_us = dt;
#endif
}

// --- IMU スレッド ---
static void imu_lock() { core_util_critical_section_enter(); }
static void imu_unlock() { core_util_critical_section_exit(); }

static void imu_thread_main() {
  float acc[3], gyro[3];
  while (true) {
    if (IMU.accelerationAvailable() && IMU.gyroscopeAvailable()) {
      IMU.readAcceleration(acc[0], acc[1], acc[2]);   // g
      IMU.readGyroscope(gyro[0], gyro[1], gyro[2]);   // deg/s
      uint32_t t = millis();
      imu_lock();
      imu_capture_push(&s_imu, t, acc, gyro);
      imu_unlock();
    }
    rtos::ThisThread::sleep_for(std::chrono::milliseconds(IMU_POLL_MS));
  }
}

bool sensors_begin() {
  audio_capture_init();
  imu_capture_init(&s_imu);
  s_imu_src.cap = &s_imu;
  s_imu_src.lock = imu_lock;
  s_imu_src.unlock = imu_unlock;
#if DETECTOR_PROFILE
  for (uint32_t i = 0; i + 4 <= IMU_STACK_BYTES; i += 4) *(uint32_t*)(imu_stack + i) = STACK_PATTERN;
#endif
  if (!IMU.begin()) return false;                 // LSM6DSOX: 104 Hz, ±4 g, ±2000 dps（ライブラリ既定、bypass モード）
  if (imu_thread.start(imu_thread_main) != osOK) return false;
  PDM.onReceive(on_pdm);
  PDM.setBufferSize(AUDIO_CHUNK * 2);
  if (!PDM.begin(1, AUDIO_HZ)) return false;
  return true;
}

bool sensors_take_slice(const int16_t** slice, uint32_t* t0_ms, uint32_t* seq, uint32_t* ready_t_ms) {
  return audio_capture_take(slice, t0_ms, seq, ready_t_ms);
}

void sensors_release_slice() {
  audio_capture_release();
}

const ImuWindowSource* sensors_imu_source() {
  return &s_imu_src;
}

void sensors_stats(SensorStats* out) {
  AudioCaptureStats a;
  audio_capture_stats(&a);
  out->dropped_chunks = a.dropped_chunks;
  out->pdm_odd_chunks = a.pdm_odd_chunks;
  out->pdm_gaps = a.pdm_gaps;
  out->pdm_chunks = a.chunks;
  out->pdm_cb_max_us = s_pdm_cb_max_us;
  imu_lock();
  out->imu_rows_total = imu_capture_total(&s_imu);
  imu_unlock();
  out->imu_stack_high_water = 0;
#if DETECTOR_PROFILE
  // 下（低いアドレス）から走査して、パターンが壊れた最初の位置から上端までを高水位とする（bench.ino と同じ）
  uint32_t i = 0;
  while (i + 4 <= IMU_STACK_BYTES && *(uint32_t*)(imu_stack + i) == STACK_PATTERN) i += 4;
  out->imu_stack_high_water = IMU_STACK_BYTES - i;
#endif
}
