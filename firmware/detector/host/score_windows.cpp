// firmware/detector/host/score_windows.cpp — PC 上の実行ファイル（Issue #22 第 6.3 節）。
//
// 標準入力の特徴量（1 行 1 窓、正規化前の 29 個、M2_FEATURE_NAMES の順、空白区切り）を m2_normalize（m2_norm.h。double で
// 計算して float32 に落とす）で正規化し、書き出した C++ ライブラリ（firmware/detector/src/）の run_classifier に掛けて、
// クラスごとの確率を標準出力に出す。「特徴量の後の装置の経路」の PC 版で、#24 の装置も同じ m2_norm.h と run_classifier を使う。
//
// 出力: 最初に見出し行（model / feature_set / n_features / labels / input_datatype / quantized / selfcheck）、
// その後 1 行 1 窓で LABEL_COUNT 個の確率（%.9g、labels と同じ順）。行数は入力と同じ。空行と # で始まる行は無視する。
// run_classifier が EI_IMPULSE_OK 以外を返したら、その行で止まりエラーコードを標準エラーに出して非 0 で終わる。
//
// ビルド: bash firmware/detector/host/build.sh → build-host/score_windows（書き出しが firmware/detector/src/ に要る）。
// 読み方は analysis/m2_scorer.py（Scorer）。
//
// 注（書き出しの SDK の版で直すところ）: run_classifier と signal_t の形は EI の SDK の版で少し違う。この実装は #17 のダミーの書き出しと
// 同じ世代（ei_impulse_handle_t の ei_default_impulse、numpy::signal_from_buffer）を前提にしている。ei_default_impulse が
// ei_impulse_t そのもの（古い版）なら、下の `ei_default_impulse.impulse->` を `ei_default_impulse.` に読み替える。

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "m2_norm.h"
#include "edge-impulse-sdk/classifier/ei_run_classifier.h"
#include "model-parameters/model_metadata.h"
#include "model-parameters/model_variables.h"

// plan 第 6.3 節: 書き出しの define が M2 の前提と食い違えばコンパイルで止まる
static_assert(EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE == M2_N_FEATURES, "EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE != M2_N_FEATURES (29)");
static_assert(EI_CLASSIFIER_LABEL_COUNT == 3, "EI_CLASSIFIER_LABEL_COUNT != 3 (swallow / cough / other)");
static_assert(EI_CLASSIFIER_HAS_DATA_NORMALIZATION == 0, "EI 側の正規化が有効。正規化は PC / 装置の m2_norm.h で行う（docs/decisions/0019）");

// 自己検査の既知の入力。analysis/m2_scorer.py の SELFCHECK_INPUT と同じ 29 個（float32 で正確に表せる値）
static const float kSelfcheckInput[M2_N_FEATURES] = {
    -1.75f, 0.375f, 12.5f, -0.0625f, 3.0f, -0.5f, 0.25f, 7.125f, -2.5f, 0.125f, 1.5f, -0.875f, 40.0f, 0.03125f, -6.25f,
    0.75f, -0.25f, 2.25f, -0.125f, 9.5f, 0.5f, -3.75f, 0.0f, 1.0f, -1.0f, 0.1875f, 5.5f, -0.4375f, 100.0f,
};

static int check_raw_block() {
    // Raw data ブロックが 1 つで scale_axes == 1.0（正規化済みの値を素通しする。plan 第 6.1 節 3）
    const ei_impulse_t* impulse = ei_default_impulse.impulse;
    if (impulse->dsp_blocks_size != 1) {
        std::fprintf(stderr, "dsp_blocks_size が 1 ではありません: %u\n", (unsigned)impulse->dsp_blocks_size);
        return 1;
    }
    const ei_model_dsp_t& block = impulse->dsp_blocks[0];
    if (block.extract_fn != &extract_raw_features) {
        std::fprintf(stderr, "処理ブロックが Raw data (extract_raw_features) ではありません\n");
        return 1;
    }
    const ei_dsp_config_raw_t* cfg = static_cast<const ei_dsp_config_raw_t*>(block.config);
    if (cfg->scale_axes != 1.0f) {
        std::fprintf(stderr, "Raw data の scale_axes が 1 ではありません: %.9g\n", (double)cfg->scale_axes);
        return 1;
    }
    if (block.n_output_features != M2_N_FEATURES) {
        std::fprintf(stderr, "Raw data の出力が %d 個ではありません: %u\n", M2_N_FEATURES, (unsigned)block.n_output_features);
        return 1;
    }
    return 0;
}

static void print_header() {
    std::printf("model %d %d\n", (int)EI_CLASSIFIER_PROJECT_ID, (int)EI_CLASSIFIER_PROJECT_DEPLOY_VERSION);
    std::printf("feature_set %s\n", M2_FEATURE_SET);
    std::printf("n_features %d\n", (int)M2_N_FEATURES);
    std::printf("labels");
    for (size_t i = 0; i < EI_CLASSIFIER_LABEL_COUNT; ++i) {
        std::printf(" %s", ei_classifier_inferencing_categories[i]);
    }
    std::printf("\n");
    std::printf("input_datatype %s\n", EI_CLASSIFIER_TFLITE_INPUT_DATATYPE == EI_CLASSIFIER_DATATYPE_INT8 ? "int8" : "float32");
    std::printf("quantized %d\n", (int)EI_CLASSIFIER_QUANTIZATION_ENABLED);
    float z[M2_N_FEATURES];
    m2_normalize(kSelfcheckInput, z);
    std::printf("selfcheck");
    for (int i = 0; i < M2_N_FEATURES; ++i) {
        std::printf(" %.9g", (double)z[i]);
    }
    std::printf("\n");
    std::fflush(stdout);
}

// 1 行を float32 の 29 個に読む。0 = 読めた、1 = 空行かコメント（飛ばす）、-1 = 形が違う
static int parse_line(const std::string& line, float* x) {
    size_t pos = line.find_first_not_of(" \t\r\n");
    if (pos == std::string::npos || line[pos] == '#') {
        return 1;
    }
    const char* p = line.c_str();
    char* end = nullptr;
    int n = 0;
    while (true) {
        while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') ++p;
        if (*p == '\0') break;
        if (n >= M2_N_FEATURES) return -1;
        float v = std::strtof(p, &end);
        if (end == p) return -1;
        x[n++] = v;
        p = end;
    }
    return n == M2_N_FEATURES ? 0 : -1;
}

int main() {
    if (check_raw_block() != 0) {
        return 2;
    }
    print_header();

    std::string line;
    float x[M2_N_FEATURES];
    float z[M2_N_FEATURES];
    long lineno = 0;
    long rows = 0;
    char buf[4096];
    while (std::fgets(buf, sizeof buf, stdin) != nullptr) {
        ++lineno;
        line.assign(buf);
        // 長い行の続き
        while (!line.empty() && line.back() != '\n' && std::fgets(buf, sizeof buf, stdin) != nullptr) {
            line.append(buf);
        }
        int rc = parse_line(line, x);
        if (rc == 1) continue;
        if (rc != 0) {
            std::fprintf(stderr, "%ld 行目: 特徴量が %d 個の数ではありません\n", lineno, M2_N_FEATURES);
            return 3;
        }
        m2_normalize(x, z);

        signal_t signal;
        int err = numpy::signal_from_buffer(z, EI_CLASSIFIER_DSP_INPUT_FRAME_SIZE, &signal);
        if (err != 0) {
            std::fprintf(stderr, "%ld 行目: signal_from_buffer が %d を返しました\n", lineno, err);
            return 4;
        }
        ei_impulse_result_t result;
        std::memset(&result, 0, sizeof result);
        EI_IMPULSE_ERROR res = run_classifier(&signal, &result, false);
        if (res != EI_IMPULSE_OK) {
            std::fprintf(stderr, "%ld 行目: run_classifier が %d を返しました\n", lineno, (int)res);
            return 5;
        }
        for (size_t i = 0; i < EI_CLASSIFIER_LABEL_COUNT; ++i) {
            std::printf(i ? " %.9g" : "%.9g", (double)result.classification[i].value);
        }
        std::printf("\n");
        ++rows;
    }
    std::fflush(stdout);
    return 0;
}
