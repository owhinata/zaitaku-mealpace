// 案1 のアダプタ（ei_dsp_case.h）。BENCH_CASE == 1 のときだけコンパイルされる。
// EI SDK のヘッダだけを含む。Arduino のヘッダは含まない。
//
// 未検証（書き出しが届いてから確かめる。plan #17「案1」）:
// - ei_run_classifier.h を含むのは、model_variables.h（ei_dsp_blocks の定義）が学習ブロックの関数
//   （run_nn_inference など）を参照し、単体では通らないため。分類器は呼ばない。参照されない tflite の
//   ランタイムとダミーのモデルはリンカ（-Wl,--gc-sections）が落とす想定で、.map で確かめる。
// - スライス版の API は、手元の別プロジェクトの SDK（Studio 1.54）の
//   int extract_*_per_slice_features(signal_t*, matrix_t*, void* config, float frequency, matrix_size_t*)
//   に合わせた。書き出された版で違えばここを直す。
#if BENCH_CASE == 1

#include "ei_dsp_case.h"
#include "edge-impulse-sdk/classifier/ei_run_classifier.h"   // ei_run_dsp.h と model-parameters/model_variables.h を含む

// int16 のバッファから EI の signal_t を作る（int16 → float の変換は計測に含まれる。EI の Arduino 例と同じ形）
static const int16_t* s_src = nullptr;

static int get_data(size_t offset, size_t length, float* out) {
  return ei::numpy::int16_to_float(s_src + offset, out, length);
}

static void make_signal(const int16_t* buf, size_t len, ei::signal_t* sig) {
  s_src = buf;
  sig->total_length = len;
  sig->get_data = &get_data;
}

// ブロックごとの出力行列。run_classifier_continuous の static_features_matrix と同じく、窓全体の特徴量を持つ
static const ei_model_dsp_t* s_block[2] = { nullptr, nullptr };
static float s_out_buf[EI_CLASSIFIER_NN_INPUT_FRAME_SIZE];
static size_t s_out_off[2] = { 0, 0 };

bool ei_dsp_case_init() {
  size_t off = 0;
  for (size_t i = 0; i < ei_dsp_blocks_size; i++) {
    const ei_model_dsp_t* blk = &ei_dsp_blocks[i];
    if (blk->extract_fn == &extract_mfcc_features) {
      s_block[EI_DSP_BLOCK_MFCC] = blk;
      s_out_off[EI_DSP_BLOCK_MFCC] = off;
    } else if (blk->extract_fn == &extract_mfe_features) {
      s_block[EI_DSP_BLOCK_MFE] = blk;
      s_out_off[EI_DSP_BLOCK_MFE] = off;
    }
    off += blk->n_output_features;
  }
  if (off > EI_CLASSIFIER_NN_INPUT_FRAME_SIZE) return false;
  return s_block[EI_DSP_BLOCK_MFCC] != nullptr || s_block[EI_DSP_BLOCK_MFE] != nullptr;
}

bool ei_dsp_case_has(EiDspBlock b) { return s_block[b] != nullptr; }

uint32_t ei_dsp_case_n_features(EiDspBlock b) { return s_block[b] ? s_block[b]->n_output_features : 0; }

uint32_t ei_dsp_case_window_samples() { return EI_CLASSIFIER_RAW_SAMPLE_COUNT; }

uint32_t ei_dsp_case_slice_samples() { return EI_CLASSIFIER_SLICE_SIZE; }

#if defined(EIDSP_TRACK_ALLOCATIONS) && EIDSP_TRACK_ALLOCATIONS
void ei_dsp_case_mem_reset() { ei_memory_peak_use = ei_memory_in_use; }
uint32_t ei_dsp_case_mem_peak() { return (uint32_t)ei_memory_peak_use; }
#else
void ei_dsp_case_mem_reset() {}
uint32_t ei_dsp_case_mem_peak() { return 0; }
#endif

int ei_dsp_case_full(EiDspBlock b, const int16_t* window) {
  const ei_model_dsp_t* blk = s_block[b];
  if (!blk) return -1;
  ei::signal_t sig;
  make_signal(window, EI_CLASSIFIER_RAW_SAMPLE_COUNT, &sig);
  ei::matrix_t fm(1, blk->n_output_features, s_out_buf + s_out_off[b]);
  return blk->extract_fn(&sig, &fm, blk->config, EI_CLASSIFIER_FREQUENCY);
}

int ei_dsp_case_slice(EiDspBlock b, const int16_t* slice) {
  const ei_model_dsp_t* blk = s_block[b];
  if (!blk) return -1;
  ei::signal_t sig;
  make_signal(slice, EI_CLASSIFIER_SLICE_SIZE, &sig);
  ei::matrix_t fm(1, blk->n_output_features, s_out_buf + s_out_off[b]);
  matrix_size_t written;
  if (b == EI_DSP_BLOCK_MFCC) {
    return extract_mfcc_per_slice_features(&sig, &fm, blk->config, EI_CLASSIFIER_FREQUENCY, &written);
  }
  return extract_mfe_per_slice_features(&sig, &fm, blk->config, EI_CLASSIFIER_FREQUENCY, &written);
}

#endif  // BENCH_CASE == 1
