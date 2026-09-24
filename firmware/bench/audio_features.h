// 案2: docs/decisions/0012 の音の特徴量 15 次元（MFCC 13・スペクトル重心・ゼロ交差率）。
// analysis/features.py の _audio_features を同じ計算順で C++ に移植したもの。Arduino 依存なし。
// 定数は features.py と同じ値。式・定数は変えない（0012）。ヒープは使わない（固定配列）。
//
// 全窓版（audio_features_full）: 16000 サンプルの int16 から 15 次元。
// 再利用版（audio_reuse_*）: 4000 サンプルのスライスを受け取り、新しい 25 フレームだけ計算する。
//   既知の差: 全窓版はプリエンファシスの最初のサンプルが pre[0] = x[0] だが、再利用版では窓の先頭フレームが
//   以前に「前のサンプルを持つ位置」で計算されている（400 サンプル中 1 つ）。この差は許容差（相対 1e-3）を
//   超えた（firmware/bench/host/check_port.py で最大 1.5e-3）ので、ホップごとに先頭フレームの DCT だけ
//   pre[0] = x[0] で計算し直す（FFT が 1 ホップに 1 回増える）。
// 段階ごとの関数（audio_stage_*）は計測（bench.ino）のために公開する。
#pragma once
#include <stdint.h>
#include <stdbool.h>

static const uint32_t AF_AUDIO_HZ = 16000;
static const uint32_t AF_WINDOW_SAMPLES = 16000;     // 1.0 秒
static const uint32_t AF_HOP_SAMPLES = 4000;         // 0.25 秒
static const uint32_t AF_FRAME_LEN = 400;            // 25 ms
static const uint32_t AF_FRAME_HOP = 160;            // 10 ms
static const float AF_PREEMPHASIS = 0.97f;
static const uint32_t AF_N_FFT = 512;
static const uint32_t AF_N_BINS = AF_N_FFT / 2 + 1;  // 257
static const uint32_t AF_N_MEL = 26;
static const float AF_MEL_FMIN_HZ = 0.0f;
static const float AF_MEL_FMAX_HZ = 8000.0f;
static const float AF_LOG_FLOOR = 1e-10f;
static const uint32_t AF_N_MFCC = 13;
static const float AF_CENTROID_MIN_POWER = 1e-12f;
static const float AF_AUDIO_SCALE = 32768.0f;
static const uint32_t AF_N_FRAMES = (AF_WINDOW_SAMPLES - AF_FRAME_LEN) / AF_FRAME_HOP + 1;   // 98
static const uint32_t AF_HOP_FRAMES = AF_HOP_SAMPLES / AF_FRAME_HOP;                         // 25
static const uint32_t AF_N_FEATURES = AF_N_MFCC + 2;                                         // 15

// 表（Hamming、メル、DCT、FFT）を作る。double で作って float で持つ。最初に 1 回呼ぶ。
void audio_features_init();

// 全窓版。x は 16000 サンプル。out[0..12] MFCC、out[13] 重心 [Hz]、out[14] ゼロ交差率。
void audio_features_full(const int16_t* x, float* out);

// 再利用版
struct AudioReuseState {
  int16_t ring[AF_WINDOW_SAMPLES];        // 直近 1.0 秒
  float dct[AF_N_FRAMES][AF_N_MFCC];      // フレームごとの DCT 13 係数
  float centroid[AF_N_FRAMES];            // フレームごとの重心
  uint32_t filled;                        // リングに入ったサンプル数（16000 で飽和）
  bool primed;                            // 全 98 フレームを一度計算したか
};
void audio_reuse_init(AudioReuseState* st);
// slice は 4000 サンプル。窓が満ちるまでは false を返し out は触らない。
bool audio_reuse_push(AudioReuseState* st, const int16_t* slice, float* out);

// 段階（計測用）
// int16 → float（÷ 32768）とプリエンファシス。has_prev が真なら x[-1] を使い、偽なら pre[0] = x[0]。
void audio_stage_preemphasis(const int16_t* x, uint32_t n, bool has_prev, float* pre);
// int16 → float（÷ 32768）だけ（重心用）
void audio_stage_convert(const int16_t* x, uint32_t n, float* raw);
// 1 フレーム（400）: Hamming → 512 点 FFT → |X|² ÷ 512（257 ビン）
void audio_stage_power(const float* frame, float* power);
// 1 フレーム: メル 26 → log(E + 1e-10) → DCT-II（ortho）の先頭 13
void audio_stage_mel_dct(const float* power, float* dct);
// 1 フレーム: Σ f·P ÷ Σ P（Σ P < 1e-12 なら 0）
float audio_stage_centroid(const float* power);
// 窓全体: 平均を引いた符号（≥ 0 を正）の変化回数 ÷ (n − 1)
float audio_stage_zcr(const int16_t* x, uint32_t n);
