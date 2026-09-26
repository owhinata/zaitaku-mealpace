// led_rule.h — docs/decisions/0018 の LED の規則（Issue #25）。Arduino 依存なし（PC のテストでもビルドする）。
// 値は DETECT の led と同じ（docs/data-schema.md: 0 消灯 / 1 黄 / 2 緑）。LED は嚥下の目安であり、評価には使わない。
// PC 側の再計算 analysis/led_rule.py と同じ式（firmware/detector/README.md）。
//
// 緑: 現在の窓（開始 w）を含めて、開始が [w − 1.75, w] にある陽性窓が 1 つ以上ある。黄: 窓の結果が出ていて緑の条件を満たさない。
// 装置は最後の陽性窓の開始 w_p を保持して、現在の窓の開始と比べるだけ（窓の履歴を持たない）。最初の陽性窓で点ける（案A）。
// 消灯は窓の結果が LED_OFF_TIMEOUT_MS 出ないとき（detector.ino の loop() が millis() で測る。窓の結果とは独立）。
#pragma once
#include <stdint.h>

enum LedState : uint8_t { LED_OFF = 0, LED_YELLOW = 1, LED_GREEN = 2 };

static const uint32_t LED_GREEN_HOLD_MS = 1750;   // 開始が [w − 1.75, w] にある陽性窓があれば緑（0018。ホップ 0.25 秒なら結果 8 回分 = 2.0 秒）
static const uint32_t LED_OFF_TIMEOUT_MS = 1000;  // 窓の結果がこの時間出なければ消灯（0018。millis() で測る。detector.ino）

struct LedRule {
  uint32_t last_positive_w_ms;   // 最後の陽性窓の開始 w_p [ms]（装置の時計）
  uint8_t has_positive;          // 陽性窓を 1 つ以上見たか
};

void led_rule_init(LedRule* s);

// 窓の結果が出るたびに呼ぶ。この窓の結果を反映した後の状態（LED_YELLOW か LED_GREEN）を返す。
// LED_OFF は返さない（消灯は detector.ino のタイムアウトだけ）。消灯の間も w_p は消さない（PC 側が行だけから同じ値を出せる）。
uint8_t led_rule_update(LedRule* s, uint32_t window_t_ms, uint8_t positive);

// 消灯の式: 最後の窓の結果から LED_OFF_TIMEOUT_MS 以上経ったか（uint32 の差で millis() の一周に強い）
static inline bool led_off_due(uint32_t now_ms, uint32_t last_result_ms) {
  return (uint32_t)(now_ms - last_result_ms) >= LED_OFF_TIMEOUT_MS;
}

// 表示の状態の進め方（led_out.cpp が使う。PC のテストで確かめる）: 目標が今と同じか、NINA の準備ができていれば目標へ進む。
// 準備ができていなければ今のまま（書き込みを飛ばした窓は、DETECT の led に前の状態が載る）。
static inline uint8_t led_shown_after(uint8_t shown, uint8_t target, bool ready) {
  return (shown == target || ready) ? target : shown;
}
