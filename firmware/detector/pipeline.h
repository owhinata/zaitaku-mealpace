// 検出器の 1 ホップの処理（Issue #24、plan 第 7 節）。Arduino 依存なし。
//   16 kHz スライス → audio_reuse_push（間引いて 8 kHz リング。音 15 次元）→ IMU の窓 → imu_features（14 次元）
//   → 29 次元を M2_FEATURE_NAMES の順に並べる → m2_normalize → classify_run → positive。
// 16 kHz のスライスは audio_reuse_push にしか渡らず、8 kHz のリングは AudioReuseState の中（audio_features.cpp）にあって
// ホップごとに上書きされる。どちらもフレームの送信関数に渡らない（docs/decisions/0005・0020）。
//
// 連番の帳簿: スライスの連番が前回 + 1 でなければ audio_reuse_init に戻す（連続でない音声で窓を作らない）。
// window_t_ms は直近 4 スライスのうち最も古いスライスの t0_ms（plan 第 6 節 (d)）。
// IMU の窓が 0012 の規則で無効なら窓を出さず imu_short を数える（plan 第 6 節 (b)）。
//
// 前段（pipeline_prepare: 帳簿・特徴量まで）と後段（正規化・推論）を分けてあり、PC のテストは前段だけを classify 無しで試す。
// 窓の結果が出たら led_rule_update（docs/decisions/0018。Arduino 依存なし）で LED の状態を r->led に入れる（#25）。
#pragma once
#include <stdint.h>
#include <stdbool.h>
#include "audio_features.h"
#include "imu_features.h"
#include "imu_capture.h"
#include "m2_norm.h"
#include "led_rule.h"

#ifndef DETECTOR_PROFILE
#define DETECTOR_PROFILE 0   // 1 で各段の時間を測る（detector.ino の統計行。-DBUILD_FLAGS="-DDETECTOR_PROFILE=1"）
#endif

static_assert(AF_PROFILE == 1, "検出器は 0020（M2）の式でビルドする（-DAF_PROFILE=1）");
static_assert(AF_AUDIO_HZ == 8000, "M2 の音の特徴量は 8 kHz で計算する");
static_assert(AF_N_FEATURES + IMU_N_FEATURES == M2_N_FEATURES, "特徴量の次元が 29 でない");
static_assert(AF_IN_HOP_SAMPLES == 4000, "16 kHz のスライスは 4000 サンプル");

static const uint32_t PIPELINE_WINDOW_MS = 1000;
static const uint32_t PIPELINE_HOP_MS = 250;
static const uint32_t PIPELINE_N_SLICES = PIPELINE_WINDOW_MS / PIPELINE_HOP_MS;   // 4

enum HopReason : uint8_t {
  HOP_OK = 0,
  HOP_NOT_FILLED = 1,     // 窓がまだ満ちていない（起動直後、または連番の飛びの後）
  HOP_RESET = 2,          // 連番の飛びで戻した（この呼び出しでは窓を出さない）
  HOP_IMU_SHORT = 3,      // IMU の窓が無効（行数不足か飛び）
  HOP_CLASSIFY_ERROR = 4  // run_classifier が EI_IMPULSE_OK 以外
};

struct HopResult {
  uint32_t window_t_ms;
  float prob;                       // swallow の確率（float32）
  uint8_t positive;                 // prob >= M2_THRESHOLD（float32 同士）
  uint8_t led;                      // docs/decisions/0018 の規則（led_rule.h）: 1 黄 / 2 緑。detector.ino が実際の表示（led_out_state）で上書きしてから送る
  uint8_t reason;                   // HopReason
  float features[M2_N_FEATURES];    // 正規化前（FEAT に載せる）
  uint32_t imu_rows;
#if DETECTOR_PROFILE
  uint32_t us_imu_copy, us_audio, us_imu_feat, us_nn;   // 各段の時間 [µs]
#endif
};

struct PipelineStats {
  uint32_t hops;              // pipeline_prepare の呼び出し数（窓が出なかったものを含む）
  uint32_t windows;           // 出した窓の数
  uint32_t not_filled;        // 窓が満ちていなくて出さなかった回数（連番の飛びで戻した直後の分は含まない）
  uint32_t resets;            // 連番の飛びで audio_reuse_init に戻した回数（その呼び出しも窓を出さない）
  uint32_t imu_short;         // IMU の窓が無効で出さなかった窓
  uint32_t classify_errors;
  uint32_t imu_rows_last;     // 直近に IMU の窓を取り出したときの行数（無効だった窓も含む）
  uint8_t last_reason;        // 直近の pipeline_hop の HopReason
};

// 表を作り、帳簿を初期化し、classify_init を呼ぶ。偽なら書き出しの前提が崩れている（setup() で止まる）。
bool pipeline_init();
// 前段だけの初期化（テスト用。classify_init を呼ばない）。LED の規則の状態（led_rule_init）もここで戻す。
void pipeline_init_stages();
// 直前のスライスの t0 から、窓の終端（w0 + 1000）の見込み [ms]。detector.ino が IMU の最後の行を待つのに使う。
uint32_t pipeline_window_end_ms(uint32_t t0_ms);
// 前段: 帳簿、audio_reuse_push、IMU の窓、imu_features。真なら r->features に 29 次元（正規化前）と window_t_ms・imu_rows が入る。
bool pipeline_prepare(const int16_t* slice16k, uint32_t t0_ms, uint32_t seq, const ImuWindowSource* imu, HopResult* r);
// 1 ホップ全部（前段 → m2_normalize → classify_run → positive → led_rule_update）。偽なら窓を出さない（理由は r->reason。LED の規則の状態は進めない）。
bool pipeline_hop(const int16_t* slice16k, uint32_t t0_ms, uint32_t seq, const ImuWindowSource* imu, HopResult* r);
void pipeline_stats(PipelineStats* out);
#if DETECTOR_PROFILE
// 段ごとの時間の計測に使う µs の時計（detector.ino が micros を渡す）。無ければ 0。
void pipeline_set_clock(uint32_t (*micros_fn)(void));
#endif
