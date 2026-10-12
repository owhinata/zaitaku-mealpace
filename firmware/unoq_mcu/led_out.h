// led_out.h — MCU 側の RGB LED（LED3）の緑 1 色の点灯と消灯（Issue #38）。
// pinMode / digitalWrite は led_out.cpp の中だけ（docs/decisions/0001）。
// この見出しは C の型だけ。
#pragma once

// ピンを出力にして消灯する。setup() の最初に呼ぶ。
void led_init();
// on = true で点灯、false で消灯。
void led_set(bool on);
