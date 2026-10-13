// rpc_server.cpp — RPC の受け口（rpc_server.h）。
// Arduino_RouterBridge（0.4.3。依存 Arduino_RPClite 0.3.1）を呼ぶのはこの
// スケッチではこのファイルだけ。公開する名前は zm_set_state(int) だけ
// （0 消灯 / 1 流れる波線 / 2 微笑む顔。docs/decisions/0035。音声は渡さない）。
//
// provide のハンドラはライブラリの別スレッド（"bridge"）で動く。ハンドラは
// 範囲を検査して状態を渡すだけで、描画しない（描画は loop() の led_tick）。
// 応答は状態を渡した時点で返る（描画の完了は含まない）。
#include "rpc_server.h"
#include "led_out.h"
#include "matrix_frame.h"
#include <Arduino_RouterBridge.h>

// Linux 側の呼び出し名（device/unoq/mcu_rpc.py の定数と同じ）
static const char RPC_SET_STATE[] = "zm_set_state";

// 範囲外（0〜2 以外）は消灯にして false を返す（Linux 側は McuRpcError）
static bool on_set_state(int v) {
  uint8_t state;
  bool ok = unoq_state_from_rpc(v, &state);
  led_set_state(state);
  return ok;
}

void rpc_begin() {
  Bridge.begin();
  Bridge.provide(RPC_SET_STATE, on_set_state);
}
