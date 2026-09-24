// 案1: Edge Impulse の音声用 DSP ブロック（MFCC と MFE）のアダプタ。
// BENCH_CASE == 1 のときだけコンパイルされる。EI SDK のヘッダは含むが Arduino のヘッダは含まない。
// EI の書き出し（C++ library）を firmware/bench/src/ に展開し、BUILD_FLAGS に
// -I<リポジトリ>/firmware/bench/src -DEI_PORTING_MBED=0 を入れてビルドする（plan #17「案1」）。
//
// 注: この実装は書き出しが届く前に、手元の別プロジェクトの EI SDK（Studio 1.54）の API を見て書いた。
// 書き出された版でスライス版の API（引数、スライスの集約）が違えば、ei_dsp_case.cpp をその版に合わせて直す。
#pragma once
#if BENCH_CASE == 1

#include <stdint.h>
#include <stddef.h>

enum EiDspBlock { EI_DSP_BLOCK_MFCC = 0, EI_DSP_BLOCK_MFE = 1 };

// model_variables.h の ei_dsp_blocks から MFCC と MFE のブロックを探す。見つからなければ false。
bool ei_dsp_case_init();
// ブロックが書き出しに含まれているか
bool ei_dsp_case_has(EiDspBlock b);
// ブロックの出力特徴量の数（ei_dsp_blocks[i].n_output_features）
uint32_t ei_dsp_case_n_features(EiDspBlock b);
// 全窓（EI_CLASSIFIER_RAW_SAMPLE_COUNT サンプル）: extract_mfcc_features / extract_mfe_features。戻りは EIDSP_OK == 0
int ei_dsp_case_full(EiDspBlock b, const int16_t* window);
// スライス（EI_CLASSIFIER_SLICE_SIZE サンプル）: extract_mfcc_per_slice_features / extract_mfe_per_slice_features。
// run_classifier_continuous と同じ呼び方（出力行列は窓全体の特徴量を持ち、SDK 側がずらす）
int ei_dsp_case_slice(EiDspBlock b, const int16_t* slice);
// 窓とスライスのサンプル数（model_metadata.h の値）
uint32_t ei_dsp_case_window_samples();
uint32_t ei_dsp_case_slice_samples();
// 裏取り用: SDK 自身の追跡（-DEIDSP_TRACK_ALLOCATIONS=1 のビルドだけ有効。それ以外は 0）。
// reset は ei_memory_peak_use を現在の使用量に戻す。peak はその後の最大 [B]
void ei_dsp_case_mem_reset();
uint32_t ei_dsp_case_mem_peak();

#endif  // BENCH_CASE == 1
