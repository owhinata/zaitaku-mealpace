// led_out.h — MCU 側の 8×13 LED マトリクスの表示（Issue #38）。
// 状態 0 = 消灯、1 = 流れる波線、2 = 微笑む顔（0018 の追記の表。受け取った
// 状態をそのまま画にする。表示の規則は M4）。
// Arduino_LED_Matrix と millis() は led_out.cpp の中だけ（docs/decisions/0001）。
// この見出しは C の型だけ。
#pragma once
#include <stdint.h>

// マトリクスを始めて全消灯を書く。setup() の最初に呼ぶ（main スレッド）。
void led_init();
// 表示する状態（0〜2。範囲の検査は呼ぶ側。rpc_server.cpp）を渡す。
// RPC のハンドラ（別スレッド）から呼んでよい。描画はしない。
void led_set_state(uint8_t state);
// loop() から呼ぶ（main スレッド）。フレームが変わったときだけ書く。
void led_tick();
