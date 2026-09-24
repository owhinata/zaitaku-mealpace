// 512 点実 FFT（fft512.h）。Arduino 依存なし。ヒープは使わない。
// 装置のビルドでは案2（BENCH_CASE == 2）のときだけ組み込む。PC の答え合わせ（BENCH_CASE 未定義）では常に組み込む。
#if !defined(BENCH_CASE) || BENCH_CASE == 2

#include "fft512.h"
#include <math.h>

static const uint32_t HALF = FFT512_N / 2;   // 256 点の複素 FFT

static float s_cos[HALF];        // cos(2π k / 512)
static float s_sin[HALF];        // sin(2π k / 512)
static uint16_t s_bitrev[HALF];  // 8 ビットの反転
static float s_re[HALF];
static float s_im[HALF];

void fft512_init() {
  for (uint32_t k = 0; k < HALF; k++) {
    double a = 2.0 * M_PI * (double)k / (double)FFT512_N;
    s_cos[k] = (float)cos(a);
    s_sin[k] = (float)sin(a);
    uint32_t r = 0, v = k;
    for (int b = 0; b < 8; b++) { r = (r << 1) | (v & 1u); v >>= 1; }
    s_bitrev[k] = (uint16_t)r;
  }
}

void fft512_power(const float* in, float* power) {
  // 詰めてビット反転順に置く
  for (uint32_t k = 0; k < HALF; k++) {
    uint32_t j = s_bitrev[k];
    s_re[j] = in[2 * k];
    s_im[j] = in[2 * k + 1];
  }
  // 256 点の複素 FFT（DIT）。W_len^j = W_512^(j × 512 / len)
  for (uint32_t len = 2; len <= HALF; len <<= 1) {
    uint32_t half = len >> 1;
    uint32_t step = FFT512_N / len;
    for (uint32_t i = 0; i < HALF; i += len) {
      for (uint32_t j = 0; j < half; j++) {
        uint32_t idx = j * step;
        float wr = s_cos[idx], wi = -s_sin[idx];
        uint32_t a = i + j, b = a + half;
        float tr = s_re[b] * wr - s_im[b] * wi;
        float ti = s_re[b] * wi + s_im[b] * wr;
        s_re[b] = s_re[a] - tr;
        s_im[b] = s_im[a] - ti;
        s_re[a] += tr;
        s_im[a] += ti;
      }
    }
  }
  // 分離: X_k = Fe + W_512^k Fo、Fe = (Z_k + conj Z_{256-k}) / 2、Fo = -i (Z_k − conj Z_{256-k}) / 2
  const float inv_n = 1.0f / (float)FFT512_N;
  {
    float x0 = s_re[0] + s_im[0];
    float xn = s_re[0] - s_im[0];
    power[0] = x0 * x0 * inv_n;
    power[HALF] = xn * xn * inv_n;
  }
  for (uint32_t k = 1; k < HALF; k++) {
    float ar = s_re[k], ai = s_im[k];
    float br = s_re[HALF - k], bi = -s_im[HALF - k];
    float fer = 0.5f * (ar + br), fei = 0.5f * (ai + bi);
    float dr = ar - br, di = ai - bi;
    float foR = 0.5f * di, foI = -0.5f * dr;
    float c = s_cos[k], s = s_sin[k];
    float xr = fer + (c * foR + s * foI);
    float xi = fei + (c * foI - s * foR);
    power[k] = (xr * xr + xi * xi) * inv_n;
  }
}

#endif  // BENCH_CASE
