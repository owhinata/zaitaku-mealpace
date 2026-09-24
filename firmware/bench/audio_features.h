// 案2: 音の特徴量 15 次元（MFCC 13・スペクトル重心・ゼロ交差率）。Arduino 依存なし。ヒープは使わない（固定配列）。
// AF_PROFILE で式を切り替える（-DAF_PROFILE=1）。マクロ無し（AF_PROFILE 0）は #17 と同じ計算・同じ値。
//   AF_PROFILE 0: docs/decisions/0012（M1）。analysis/features.py の _audio_features の移植。16 kHz、400 / 160、512 点。
//   AF_PROFILE 1: docs/decisions/0020（M2）。analysis/features_m2.py の _audio_features の移植。入力は 16 kHz のまま
//                 受け取り、2:1 に間引いて 8 kHz で計算する。200 / AF_M2_FRAME_HOP（既定 200。-DAF_M2_FRAME_HOP=400 で f′）、
//                 256 点、メル 0〜4000 Hz、フレームごとのプリエンファシス、重心は同じ FFT から。
//
// 全窓版（audio_features_full）: AF_WINDOW_SAMPLES サンプル（計算用の周波数、M2 なら間引き後の 8000）の int16 から 15 次元。
// 再利用版（audio_reuse_*）: 入力の周波数（16 kHz）のスライス AF_IN_HOP_SAMPLES（4000）を受け取り、間引いてリングに入れ、
//   新しい AF_HOP_FRAMES フレームだけ計算する。
//   M1 の既知の差: 全窓版はプリエンファシスの最初のサンプルが pre[0] = x[0] だが、再利用版では窓の先頭フレームが
//   以前に「前のサンプルを持つ位置」で計算されている（400 サンプル中 1 つ）。この差は許容差（相対 1e-3）を
//   超えた（firmware/bench/host/check_port.py で最大 1.5e-3）ので、ホップごとに先頭フレームの DCT だけ
//   pre[0] = x[0] で計算し直す（FFT が 1 ホップに 1 回増える）。M2 はフレームごとのプリエンファシスなので、この計算し直しは無い。
// 段階ごとの関数（audio_stage_*）は計測（bench.ino）のために公開する。
#pragma once
#include <stdint.h>
#include <stdbool.h>
#include "fft512.h"   // AF_PROFILE の既定と AF_N_FFT（256 / 512）はここで決まる

static const uint32_t AF_IN_HZ = 16000;              // 入力（マイクの取り込み・記録形式）の周波数
static const uint32_t AF_IN_HOP_SAMPLES = 4000;      // 0.25 秒（入力の周波数）
static const uint32_t AF_N_MEL = 26;
static const float AF_MEL_FMIN_HZ = 0.0f;
static const float AF_LOG_FLOOR = 1e-10f;
static const uint32_t AF_N_MFCC = 13;
static const float AF_CENTROID_MIN_POWER = 1e-12f;
static const float AF_AUDIO_SCALE = 32768.0f;
static const float AF_PREEMPHASIS = 0.97f;

#if AF_PROFILE == 1
// M2（docs/decisions/0020）
#ifndef AF_M2_FRAME_HOP
#define AF_M2_FRAME_HOP 200
#endif
#define AF_DECIMATION 2                               // 16 kHz → 8 kHz。隣り合う 2 サンプルの平均 (a + b) >> 1（bench.ino が #if で見るのでマクロ）
static const uint32_t AF_AUDIO_HZ = 8000;            // 特徴量の計算に使う周波数
static const uint32_t AF_WINDOW_SAMPLES = 8000;      // 1.0 秒
static const uint32_t AF_HOP_SAMPLES = 2000;         // 0.25 秒
static const uint32_t AF_FRAME_LEN = 200;            // 25 ms
static const uint32_t AF_FRAME_HOP = AF_M2_FRAME_HOP;   // 25 ms（d′）/ 50 ms（f′）
static const float AF_MEL_FMAX_HZ = 4000.0f;
#define AF_PREEMPH_PER_FRAME 1                        // フレームごとに独立（各フレームの pre[0] = x[0]）
#define AF_CENTROID_SAME_FFT 1                        // 重心は MFCC と同じ（プリエンファシス後の）パワースペクトルから
#else
// M1（docs/decisions/0012）。#17 と同じ値
#define AF_DECIMATION 1                               // 間引きなし（audio_stage_decimate は複写）
static const uint32_t AF_AUDIO_HZ = 16000;
static const uint32_t AF_WINDOW_SAMPLES = 16000;     // 1.0 秒
static const uint32_t AF_HOP_SAMPLES = 4000;         // 0.25 秒
static const uint32_t AF_FRAME_LEN = 400;            // 25 ms
static const uint32_t AF_FRAME_HOP = 160;            // 10 ms
static const float AF_MEL_FMAX_HZ = 8000.0f;
#define AF_PREEMPH_PER_FRAME 0                        // 窓全体に掛けてからフレームに切る
#define AF_CENTROID_SAME_FFT 0                        // 重心はプリエンファシス前の別の FFT から
#endif

static const uint32_t AF_N_BINS = AF_N_FFT / 2 + 1;                                          // 257 / 129
static const uint32_t AF_N_FRAMES = (AF_WINDOW_SAMPLES - AF_FRAME_LEN) / AF_FRAME_HOP + 1;   // 98 / 40（f′ 20）
static const uint32_t AF_HOP_FRAMES = AF_HOP_SAMPLES / AF_FRAME_HOP;                         // 25 / 10（f′ 5）
static const uint32_t AF_N_FEATURES = AF_N_MFCC + 2;                                         // 15

// 再利用版は「ホップが stride の整数倍」が前提（割り切れないと窓ごとにフレームの位置がずれ、PC と同じ格子にならない）
static_assert(AF_HOP_SAMPLES % AF_FRAME_HOP == 0, "AF_HOP_SAMPLES は AF_FRAME_HOP の整数倍");
static_assert(AF_IN_HOP_SAMPLES == AF_HOP_SAMPLES * AF_DECIMATION, "入力のスライスは間引き後にちょうど 1 ホップ");
static_assert(AF_AUDIO_HZ * AF_DECIMATION == AF_IN_HZ, "間引きの比と周波数が合わない");
static_assert(AF_WINDOW_SAMPLES % AF_HOP_SAMPLES == 0, "窓はホップの整数倍");
static_assert(AF_N_FRAMES > AF_HOP_FRAMES, "窓のフレーム数はホップのフレーム数より多い");
static_assert(AF_FRAME_LEN <= AF_N_FFT, "フレームは FFT の点数以下");

// 表（Hamming、メル、DCT、FFT）を作る。double で作って float で持つ。最初に 1 回呼ぶ。
void audio_features_init();

// 全窓版。x は AF_WINDOW_SAMPLES サンプル（計算用の周波数）。out[0..12] MFCC、out[13] 重心 [Hz]、out[14] ゼロ交差率。
void audio_features_full(const int16_t* x, float* out);

// 再利用版
struct AudioReuseState {
  int16_t ring[AF_WINDOW_SAMPLES];        // 直近 1.0 秒（計算用の周波数）
  float dct[AF_N_FRAMES][AF_N_MFCC];      // フレームごとの DCT 13 係数
  float centroid[AF_N_FRAMES];            // フレームごとの重心
  uint32_t filled;                        // リングに入ったサンプル数（AF_WINDOW_SAMPLES で飽和）
  bool primed;                            // 全フレームを一度計算したか
};
void audio_reuse_init(AudioReuseState* st);
// slice は入力の周波数で AF_IN_HOP_SAMPLES サンプル（16 kHz・4000）。窓が満ちるまでは false を返し out は触らない。
bool audio_reuse_push(AudioReuseState* st, const int16_t* slice, float* out);

// 段階（計測用）
// 間引き: in は n サンプル（n は AF_DECIMATION の倍数）、out は n / AF_DECIMATION サンプル。(in[2i] + in[2i+1]) >> 1 を int32 で。
// AF_DECIMATION == 1 のときは複写。
void audio_stage_decimate(const int16_t* in, uint32_t n, int16_t* out);
// int16 → float（÷ 32768）とプリエンファシス。has_prev が真なら x[-1] を使い、偽なら pre[0] = x[0]。
void audio_stage_preemphasis(const int16_t* x, uint32_t n, bool has_prev, float* pre);
// int16 → float（÷ 32768）だけ（M1 の重心用）
void audio_stage_convert(const int16_t* x, uint32_t n, float* raw);
// 1 フレーム（AF_FRAME_LEN）: Hamming → AF_N_FFT 点 FFT → |X|² ÷ AF_N_FFT（AF_N_BINS ビン）
void audio_stage_power(const float* frame, float* power);
// 1 フレーム: メル 26 → log(E + 1e-10) → DCT-II（ortho）の先頭 13
void audio_stage_mel_dct(const float* power, float* dct);
// 1 フレーム: Σ f·P ÷ Σ P（Σ P < 1e-12 なら 0）
float audio_stage_centroid(const float* power);
// 窓全体: 平均を引いた符号（≥ 0 を正）の変化回数 ÷ (n − 1)
float audio_stage_zcr(const int16_t* x, uint32_t n);
