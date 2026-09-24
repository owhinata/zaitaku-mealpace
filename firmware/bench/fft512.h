// 案2 用の 512 点実 FFT（radix-2、float）。Arduino 依存なし。
// 実入力 512 点を 256 点の複素 FFT に詰めて（偶数番を実部、奇数番を虚部）、
// 最後に分離する。ツイドルとビット反転の表は fft512_init() で double から作る。
// EI SDK の kissfft や CMSIS-DSP には依存しない。
#pragma once
#include <stdint.h>

static const uint32_t FFT512_N = 512;
static const uint32_t FFT512_BINS = 257;   // 0..256

void fft512_init();

// in: 実数 512 点。power[k] = |X_k|² ÷ 512（k = 0..256）。docs/decisions/0012 のパワースペクトルの定義。
void fft512_power(const float* in, float* power);
