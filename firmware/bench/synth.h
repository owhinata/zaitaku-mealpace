// 計測用の決定的な合成信号（#17）。Arduino 依存なし。
// 実機のマイクと IMU は使わない。生の音声は扱わない。
// 同じ式を firmware/bench/host/check_port.py が numpy で再現するので、
// ここを変えたら check_port.py も同じに変える。
#pragma once
#include <stdint.h>

// xorshift32（seed 固定）
struct SynthRng {
  uint32_t s;
};
void synth_rng_seed(SynthRng* r, uint32_t seed);
uint32_t synth_rng_next(SynthRng* r);

static const uint32_t SYNTH_SEED_AUDIO = 0x12345678u;
static const uint32_t SYNTH_SEED_IMU = 0x9ABCDEF1u;

// 音: 16 kHz、1 kHz 正弦波（振幅 8000。周期 16 サンプルの整数表）＋一様雑音 [-1000, 1000] の int16。
// start_index はストリーム上の先頭サンプルの番号（スライスごとに続きを作るため）。
// 雑音は 1 サンプルにつき rng を 1 回引く。
static const uint32_t SYNTH_AUDIO_HZ = 16000;
void synth_audio(SynthRng* rng, uint32_t start_index, uint32_t n, int16_t* out);

// IMU: 周期 9.48 ms（t_ms = round(row × 9.48)、整数）。
//   加速度 [g]: X = 0.01 nx、Y = 1 + 0.1 sin(2π 5 t) + 0.01 ny、Z = 0.01 nz
//   ジャイロ [deg/s]: X = 1.5 + 2 gx、Y = -0.7 + 2 gy、Z = 0.3 + 2 gz
//   n*, g* は一様雑音 [-1, 1]（rng を行ごとに nx, ny, nz, gx, gy, gz の順に 6 回引く）。
//   sin は double で計算して float に落とす。
static const uint32_t SYNTH_IMU_PERIOD_100US = 948;   // 9.48 ms
void synth_imu(SynthRng* rng, uint32_t start_row, uint32_t n_rows, uint32_t* t_ms, float (*acc)[3], float (*gyro)[3]);
