// unoq_mcu.ino — UNO Q の MCU 側のスケッチ（Issue #38、docs/decisions/0035）。
// Linux 側から RPC の zm_set_led(bool) で LED を点けて消すだけ。
// 表示の規則（0018 の緑・黄・消灯、1.0 秒で消灯）は持たない（M4）。
// Arduino の GPIO は led_out.cpp、RPC は rpc_server.cpp の中だけで呼ぶ
// （docs/decisions/0001・0025）。音声・ADC・シリアルへの出力は無い。
#include "led_out.h"
#include "rpc_server.h"

void setup() {
  led_init();  // 起動時は消灯
  rpc_begin();
}

// RPC の受け付けはライブラリの別スレッドが行う（Arduino_RouterBridge 0.4.3 の
// Bridge.provide。rpc_server.cpp）。ループで回すものは無い。
void loop() {}
