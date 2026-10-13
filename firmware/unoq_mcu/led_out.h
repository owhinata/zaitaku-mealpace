// led_out.h — MCU 側の 8×13 LED マトリクスの点灯の確かめ（Issue #38）。
// 点灯は #32 の微笑む顔（静止画）、消灯は全消灯。
// Arduino_LED_Matrix は led_out.cpp の中だけ（docs/decisions/0001）。
// この見出しは C の型だけ。
#pragma once

// マトリクスを始めて全消灯する。setup() の最初に呼ぶ。
void led_init();
// on = true で微笑む顔、false で全消灯。
void led_set(bool on);
