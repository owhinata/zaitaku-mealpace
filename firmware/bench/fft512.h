// 案2 用の実 FFT（radix-2、float）。Arduino 依存なし。
// 点数はマクロ AF_N_FFT で決める（AF_PROFILE 0 = M1（docs/decisions/0012）: 512、AF_PROFILE 1 = M2（0020）: 256）。
// ファイル名と関数名は 512 点の版から変えていない。
// 実入力 N 点を N/2 点の複素 FFT に詰めて（偶数番を実部、奇数番を虚部）、最後に分離する。
// ツイドルとビット反転の表は fft512_init() で double から作る（ビット反転のビット数は log2(N/2)）。
// EI SDK の kissfft や CMSIS-DSP には依存しない。
#pragma once
#include <stdint.h>

#ifndef AF_PROFILE
#define AF_PROFILE 0
#endif
#if AF_PROFILE == 1
#define AF_N_FFT 256
#elif AF_PROFILE == 0
#define AF_N_FFT 512
#else
#error "AF_PROFILE は 0（M1）か 1（M2）"
#endif
static_assert((AF_N_FFT & (AF_N_FFT - 1)) == 0 && AF_N_FFT >= 4, "AF_N_FFT は 2 の冪");

static const uint32_t FFT512_N = AF_N_FFT;
static const uint32_t FFT512_BINS = AF_N_FFT / 2 + 1;   // 0..N/2

void fft512_init();

// in: 実数 N 点。power[k] = |X_k|² ÷ N（k = 0..N/2）。docs/decisions/0012 のパワースペクトルの定義（0020 も同じ形）。
void fft512_power(const float* in, float* power);
