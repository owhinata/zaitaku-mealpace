// 検出器の推論（Issue #22 の書き出し firmware/detector/src/ の run_classifier）。Edge Impulse の SDK はこのモジュールの
// 中（classify.cpp）だけで使う。Arduino 依存なし。入口の型は C の型だけ。
// PC 側の同じ経路は firmware/detector/host/score_windows.cpp（check_raw_block と static_assert も同じ形）。
#pragma once
#include <stdint.h>
#include <stdbool.h>

static const uint32_t CLASSIFY_N_LABELS = 3;   // cough / other / swallow（model_variables.h の並び。添字はラベル名で引く）

// 書き出しの前提を確かめ、"swallow" の添字を引く。前提（Raw ブロック 1 つ、scale_axes 1.0、出力 29、ラベルに "swallow"）が
// 崩れていれば偽（setup() で止まる）。
bool classify_init();
// z: 正規化後の特徴量（M2_N_FEATURES 個、float32）。probs: ラベルごとの確率（CLASSIFY_N_LABELS 個）。
// run_classifier が EI_IMPULSE_OK 以外なら偽（classify_last_error にコードを残す）。
bool classify_run(const float* z, float* probs);
int classify_swallow_index();
int32_t classify_last_error();
uint32_t classify_project_id();       // EI_CLASSIFIER_PROJECT_ID（Public なので META に載せる。docs/decisions/0019）
uint32_t classify_deploy_version();   // EI_CLASSIFIER_PROJECT_DEPLOY_VERSION
