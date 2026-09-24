// 検出器のセンサの読み出し（Issue #24 plan 第 5.1 節）。Arduino / mbed の API（PDM、IMU、rtos::Thread、millis、micros、
// critical section）を呼ぶのは sensors.cpp と detector.ino だけ（docs/decisions/0001）。公開する関数の引数・戻り値は C の型だけ。
//   - 設定は firmware/logger/logger.ino と同じ: IMU.begin()（104 Hz・±4 g・±2000 dps）、PDM.setBufferSize(512)、PDM.begin(1, 16000)。
//   - PDM のコールバック: PDM.available() → PDM.read() → millis() → audio_capture_push_chunk（logger の on_pdm と同じ順）。
//   - IMU スレッド（優先度 AboveNormal、スタック 2048 B、2 ms ごとに poll）: accelerationAvailable && gyroscopeAvailable →
//     readAcceleration → readGyroscope → millis() → imu_capture_push。主スレッドの特徴量の計算の間も 9.6 ms 周期を落とさない。
#pragma once
#include <stdint.h>
#include <stdbool.h>
#include "imu_capture.h"

struct SensorStats {
  uint32_t dropped_chunks, pdm_odd_chunks, pdm_gaps, pdm_chunks;
  uint32_t pdm_cb_max_us;      // PDM のコールバックの入口と出口の micros() の差の最大（DETECTOR_PROFILE のときだけ測る）
  uint32_t imu_rows_total;     // IMU スレッドが積んだ行の総数
  uint32_t imu_stack_high_water;   // IMU スレッドのスタックの高水位 [B]（DETECTOR_PROFILE のときだけ。それ以外は 0）
};

bool sensors_begin();
// READY のスライス（16 kHz、4000 サンプル）を IN_USE にして返す。無ければ偽。
bool sensors_take_slice(const int16_t** slice, uint32_t* t0_ms, uint32_t* seq, uint32_t* ready_t_ms);
void sensors_release_slice();
const ImuWindowSource* sensors_imu_source();
void sensors_stats(SensorStats* out);
