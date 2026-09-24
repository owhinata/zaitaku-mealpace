// 決定的な合成信号（synth.h）。Arduino 依存なし。
#include "synth.h"
#include <math.h>

void synth_rng_seed(SynthRng* r, uint32_t seed) {
  r->s = seed ? seed : 1u;
}

uint32_t synth_rng_next(SynthRng* r) {
  uint32_t x = r->s;
  x ^= x << 13;
  x ^= x >> 17;
  x ^= x << 5;
  r->s = x;
  return x;
}

// 8000 × sin(2π k / 16) を整数に丸めた表（k = 0..15）
static const int16_t SINE16[16] = {
  0, 3061, 5657, 7391, 8000, 7391, 5657, 3061,
  0, -3061, -5657, -7391, -8000, -7391, -5657, -3061,
};

void synth_audio(SynthRng* rng, uint32_t start_index, uint32_t n, int16_t* out) {
  for (uint32_t k = 0; k < n; k++) {
    int32_t noise = (int32_t)(synth_rng_next(rng) % 2001u) - 1000;
    out[k] = (int16_t)(SINE16[(start_index + k) & 15u] + noise);
  }
}

static double uniform_pm1(SynthRng* rng) {
  return ((int32_t)(synth_rng_next(rng) % 20001u) - 10000) / 10000.0;
}

void synth_imu(SynthRng* rng, uint32_t start_row, uint32_t n_rows, uint32_t* t_ms, float (*acc)[3], float (*gyro)[3]) {
  for (uint32_t k = 0; k < n_rows; k++) {
    uint32_t row = start_row + k;
    uint32_t t = (row * SYNTH_IMU_PERIOD_100US + 50u) / 100u;
    t_ms[k] = t;
    double ts = t / 1000.0;
    double nx = uniform_pm1(rng), ny = uniform_pm1(rng), nz = uniform_pm1(rng);
    double gx = uniform_pm1(rng), gy = uniform_pm1(rng), gz = uniform_pm1(rng);
    acc[k][0] = (float)(0.01 * nx);
    acc[k][1] = (float)(1.0 + 0.1 * sin(2.0 * M_PI * 5.0 * ts) + 0.01 * ny);
    acc[k][2] = (float)(0.01 * nz);
    gyro[k][0] = (float)(1.5 + 2.0 * gx);
    gyro[k][1] = (float)(-0.7 + 2.0 * gy);
    gyro[k][2] = (float)(0.3 + 2.0 * gz);
  }
}
