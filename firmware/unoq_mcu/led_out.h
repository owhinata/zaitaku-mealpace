// led_out.h — MCU 側の 8×13 LED マトリクスの表示（Issue #38）。
// 状態 0 = 消灯、1 = 流れる波線、2 = 微笑む顔（0018 の追記の表。受け取った
// 状態をそのまま画にする。保持は持たない。消灯は loop() が受信に気づいて
// から約 2 秒（2000 ms）次の受信が無いとき。docs/decisions/0049）。
// Arduino_LED_Matrix と millis() は led_out.cpp の中だけ（docs/decisions/0001）。
// この見出しは C の型だけ。
#pragma once
#include <stdint.h>

// マトリクスを始めて全消灯を書く。setup() の最初に呼ぶ（main スレッド）。
void led_init();
// 表示する状態（0〜2。範囲の検査は呼ぶ側。rpc_server.cpp）を渡す。
// RPC のハンドラ（別スレッド）から呼んでよい。描画はしない。受信の数を
// 1 つ増やす（範囲外で消灯にした値も受信として数える）。
void led_set_state(uint8_t state);
// loop() から呼ぶ（main スレッド）。受信の数の変化に気づいた時刻から
// 約 2 秒で消灯にする（rx_hold.h）。フレームが変わったときだけ書く。
void led_tick();
