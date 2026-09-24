// 案3: docs/decisions/0012 の IMU 14 次元（振幅 4・RMS 6・ピーク数 1・主軸方向 3）。
// analysis/features.py の _imu_features を同じ計算順で C++ に移植したもの。Arduino 依存なし。
// 再利用しない実装だけ（和・共分散はホップをまたいで更新できるが、peak-to-peak とピーク数は窓全体の行が要る）。
// ヒープは使わない（最大 128 行の固定配列）。
#pragma once
#include <stdint.h>

static const uint32_t IMU_MAX_ROWS = 128;            // 1.0 秒 ÷ 9.48 ms = 105〜106 行
static const uint32_t IMU_N_FEATURES = 14;
static const float IMU_PEAK_HEIGHT_RMS_RATIO = 1.0f;
static const uint32_t IMU_PEAK_MIN_DISTANCE_MS = 50;  // t_ms の整数差で比べる
static const float IMU_AXIS_SIGN_EPS = 1e-6f;
static const float IMU_AXIS_MIN_EIGENVALUE = 1e-8f;  // g²
static const float IMU_AXIS_MIN_EIGEN_GAP_RATIO = 0.01f;

// t_ms: 行の時刻 [ms]、acc: 加速度 [g]、gyro: ジャイロ [deg/s]、n: 行数（≤ IMU_MAX_ROWS。超えた分は使わない）。
// out[0..2] acc ptp、out[3] gyro ノルム ptp、out[4..6] acc RMS、out[7..9] gyro RMS、out[10] ピーク数、out[11..13] 主軸。
// 2 行未満なら全部 0。
void imu_features(const uint32_t* t_ms, const float (*acc)[3], const float (*gyro)[3], uint32_t n, float* out);
