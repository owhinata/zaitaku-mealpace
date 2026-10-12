// rpc_server.cpp — RPC の受け口（rpc_server.h）。
// Arduino_RouterBridge（0.4.3。依存 Arduino_RPClite 0.3.1）を呼ぶのはこの
// スケッチではこのファイルだけ。公開する名前は zm_set_led だけ
// （docs/decisions/0035。M4 で表示の状態を渡す名前を足す。音声は渡さない）。
//
// provide と provide_safe: provide のハンドラはライブラリの別スレッド
// （"bridge"）で動き、loop() に依らない。ハンドラは GPIO を 1 本書くだけ
// なので provide を使う。
#include "rpc_server.h"
#include "led_out.h"
#include <Arduino_RouterBridge.h>

// Linux 側の呼び出し名（device/unoq/mcu_rpc.py の定数と同じ）
static const char RPC_SET_LED[] = "zm_set_led";

static bool on_set_led(bool on) {
  led_set(on);
  return true;
}

void rpc_begin() {
  Bridge.begin();
  Bridge.provide(RPC_SET_LED, on_set_led);
}
