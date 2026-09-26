// 検出器のセンサの読み出し（Issue #24 plan 第 5.1 節）。Arduino / mbed の API（PDM、IMU、rtos::Thread、millis、micros、
// critical section）を呼ぶのは sensors.cpp と detector.ino だけ（docs/decisions/0001）。公開する関数の引数・戻り値は C の型だけ。
//   - 設定は firmware/logger/logger.ino と同じ: IMU.begin()（104 Hz・±4 g・±2000 dps）、PDM.setBufferSize(512)、PDM.begin(1, 16000)。
//   - PDM のコールバック: PDM.available() → PDM.read() → millis() → audio_capture_push_chunk（logger の on_pdm と同じ順）。
//   - IMU スレッド（優先度 AboveNormal、スタック 2048 B、2 ms ごとに poll）: accelerationAvailable && gyroscopeAvailable →
//     readAcceleration → readGyroscope → millis() → imu_capture_push。主スレッドの特徴量の計算の間も 9.6 ms 周期を落とさない。
//
// ビルドフラグ（既定はここで決める。sensors.cpp の中だけで見る。docs/log 2026-09-26 の切り分け: I2C 100 kHz・poll 2 ms では IMU スレッドが
// 主スレッドの時間を 138 ms 食って 1 ホップ 293 ms、400 kHz・4 ms で 208 ms。人の決定で本番の既定を後者にした）:
//   DETECTOR_I2C_HZ（既定 400000。IMU.begin() の後に Wire.setClock。0 ならボードコアの既定（100 kHz と推定）のまま。plan #24 第 16 節 P の 1 段目）
//   DETECTOR_IMU_POLL_MS（既定 4。IMU スレッドの poll 間隔 [ms]。P の 2 段目）
//   DETECTOR_PROFILE=1 のときだけ（切り分け用。本番では #error）:
//     DETECTOR_PROF_NO_IMU=1  IMU スレッドを起動しない（窓は imu_short で出ない。音の時間だけ測る）
//     DETECTOR_PROF_NO_PDM=1  PDM を始めず、タイマ（4 ms ごと、割り込みの中）で固定の合成チャンク 64 サンプルを積む（PDM の変換の負担を外す）
#pragma once
#include <stdint.h>
#include <stdbool.h>
#include "imu_capture.h"

#ifndef DETECTOR_I2C_HZ
#define DETECTOR_I2C_HZ 400000
#endif
#ifndef DETECTOR_IMU_POLL_MS
#define DETECTOR_IMU_POLL_MS 4
#endif

struct SensorStats {
  uint32_t dropped_chunks, pdm_odd_chunks, pdm_gaps, pdm_chunks;
  uint32_t pdm_bytes_last;     // 直近の PDM のコールバックの n（バイト数。64 サンプルなら 128）
  uint32_t pdm_cb_max_us;      // PDM のコールバックの入口と出口の micros() の差の最大（DETECTOR_PROFILE のときだけ測る）
  uint32_t slices_taken;       // 主スレッドが取ったスライスの数
  uint32_t slices_ready_max;   // 取るときに READY だった面の数の最大（0〜2）
  uint32_t imu_rows_total;     // IMU スレッドが積んだ行の総数
  uint32_t imu_polls;          // IMU スレッドのループの回数（スレッドが動いているかの目安）
  uint32_t imu_stack_high_water;   // IMU スレッドのスタックの高水位 [B]（DETECTOR_PROFILE のときだけ。それ以外は 0）
  uint8_t imu_thread;          // IMU スレッドを起動したか（DETECTOR_PROF_NO_IMU なら 0）
  uint8_t pdm_real;            // 本物の PDM か（DETECTOR_PROF_NO_PDM なら 0 = 合成チャンク）
  uint32_t i2c_hz;             // DETECTOR_I2C_HZ（0 = 既定のまま）
  uint32_t imu_poll_ms;        // DETECTOR_IMU_POLL_MS
};

bool sensors_begin();
// READY のスライス（16 kHz、4000 サンプル）を IN_USE にして返す。無ければ偽。
bool sensors_take_slice(const int16_t** slice, uint32_t* t0_ms, uint32_t* seq, uint32_t* ready_t_ms);
void sensors_release_slice();
const ImuWindowSource* sensors_imu_source();
void sensors_stats(SensorStats* out);
