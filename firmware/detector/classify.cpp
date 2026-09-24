// 検出器の推論（classify.h）。Edge Impulse の SDK を使うのはこのファイルだけ。
// ビルドフラグ（CMakeLists.txt の DETECTOR_BASE_FLAGS）: -I firmware/detector/src -DEI_PORTING_MBED=0
//   -DEI_CLASSIFIER_TFLITE_ENABLE_CMSIS_NN=0（PC の build.sh と同じカーネル）-DEI_LOG_LEVEL=1（SDK が出すのはエラー文だけ）。
// SDK の printf（Arduino porting が Serial に書く）はここから呼ばない（SDK の内部のエラー出力は src/ の中で、値ではなくメッセージ）。
#include "classify.h"
#include "m2_norm.h"

#include <string.h>
#include "edge-impulse-sdk/classifier/ei_run_classifier.h"
#include "model-parameters/model_metadata.h"
#include "model-parameters/model_variables.h"

// 書き出しの define が M2 の前提と食い違えばコンパイルで止まる（host/score_windows.cpp と同じ）
static_assert(EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE == M2_N_FEATURES, "EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE != M2_N_FEATURES (29)");
static_assert(EI_CLASSIFIER_LABEL_COUNT == 3, "EI_CLASSIFIER_LABEL_COUNT != 3 (swallow / cough / other)");
static_assert(EI_CLASSIFIER_LABEL_COUNT == CLASSIFY_N_LABELS, "CLASSIFY_N_LABELS が書き出しと違う");
static_assert(EI_CLASSIFIER_HAS_DATA_NORMALIZATION == 0, "EI 側の正規化が有効。正規化は PC / 装置の m2_norm.h で行う（docs/decisions/0019）");

static int s_swallow_ix = -1;
static int32_t s_last_error = 0;

// Raw data ブロックが 1 つで scale_axes == 1.0（正規化済みの値を素通しする）。score_windows.cpp の check_raw_block と同じ
static bool check_raw_block() {
  const ei_impulse_t* impulse = ei_default_impulse.impulse;
  if (impulse->dsp_blocks_size != 1) return false;
  const ei_model_dsp_t& block = impulse->dsp_blocks[0];
  if (block.extract_fn != &extract_raw_features) return false;
  const ei_dsp_config_raw_t* cfg = static_cast<const ei_dsp_config_raw_t*>(block.config);
  if (cfg->scale_axes != 1.0f) return false;
  if (block.n_output_features != M2_N_FEATURES) return false;
  return true;
}

bool classify_init() {
  s_swallow_ix = -1;
  if (!check_raw_block()) return false;
  for (size_t i = 0; i < EI_CLASSIFIER_LABEL_COUNT; i++) {
    if (strcmp(ei_classifier_inferencing_categories[i], "swallow") == 0) s_swallow_ix = (int)i;
  }
  return s_swallow_ix >= 0;
}

bool classify_run(const float* z, float* probs) {
  signal_t signal;
  int err = numpy::signal_from_buffer(z, EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE, &signal);
  if (err != 0) { s_last_error = err; return false; }
  ei_impulse_result_t result;
  memset(&result, 0, sizeof result);
  EI_IMPULSE_ERROR res = run_classifier(&signal, &result, false);
  if (res != EI_IMPULSE_OK) { s_last_error = (int32_t)res; return false; }
  for (size_t i = 0; i < EI_CLASSIFIER_LABEL_COUNT; i++) probs[i] = result.classification[i].value;
  return true;
}

int classify_swallow_index() { return s_swallow_ix; }
int32_t classify_last_error() { return s_last_error; }
uint32_t classify_project_id() { return (uint32_t)EI_CLASSIFIER_PROJECT_ID; }
uint32_t classify_deploy_version() { return (uint32_t)EI_CLASSIFIER_PROJECT_DEPLOY_VERSION; }
