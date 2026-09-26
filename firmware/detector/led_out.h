// led_out.h — 基板の RGB LED（NINA-W102 経由）への書き込み（Issue #25、docs/decisions/0018）。
// 状態は led_rule.h の LED_OFF / LED_YELLOW / LED_GREEN。黄 = 赤＋緑。青は使わない（begin で消しておく）。
// NINA のライブラリの見出しと、NINA のピン版の pinMode / digitalWrite は led_out.cpp の中だけ（docs/decisions/0001）。この見出しは C の型だけ。
#pragma once
#include <stdint.h>

// NINA のライブラリの SPI の初期化（NINA のリセット + 約 760 ms 待ち）を起こし、3 ピンを OUTPUT にして消灯する。
// setup() でセンサを始める前に呼ぶ（760 ms の待ちを PDM の取り込みが始まる前に済ませる）。
void led_out_begin();
// LED_OFF / LED_YELLOW / LED_GREEN。今の状態と同じなら何も書かない（変化したピンだけ書く）。
// NINA の準備ができていなければ（ACK ピンが LOW でない）書かずに戻り、状態を進めない（led_out_skipped が増える）。
void led_out_set(uint8_t state);
// 実際に表示している状態（最後に書き終えた状態。飛ばしたときは進めない）。DETECT の led はこの値。
uint8_t led_out_state();
// NINA への digitalWrite の累計（begin の分を含む。DETECTOR_PROFILE の統計行）
uint32_t led_out_writes();
// 準備ができていなくて飛ばした led_out_set の累計（同上）
uint32_t led_out_skipped();
// digitalWrite 1 回の所要の最大 [µs]（DETECTOR_PROFILE のときだけ測る。それ以外は 0）
uint32_t led_out_write_max_us();
