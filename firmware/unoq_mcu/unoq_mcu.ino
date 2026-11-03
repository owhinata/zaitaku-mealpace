// unoq_mcu.ino — UNO Q の MCU 側のスケッチ（Issue #38、docs/decisions/0035）。
// Linux 側から RPC の zm_set_state(int) で受けた状態（0 消灯 / 1 流れる波線 /
// 2 微笑む顔）を 8×13 LED マトリクスにそのまま出す。
// 0018 の保持と、窓の結果が 1.0 秒出ないときの消灯は Linux 側が持つ。
// 消灯は loop() が受信に気づいてから約 2 秒（2000 ms）次の受信が無ければ
// MCU が自分で行う（docs/decisions/0049、rx_hold.h）。
// マトリクスと millis() は led_out.cpp、RPC は rpc_server.cpp の中だけで呼ぶ
// （docs/decisions/0001・0025）。音声・ADC・シリアルへの出力は無い。
#include "led_out.h"
#include "rpc_server.h"

void setup() {
  led_init();  // 起動時は消灯
  rpc_begin();
}

// RPC の受け付けはライブラリの別スレッドが行う。ここは描画だけ（フレームが
// 変わったときだけ書く。流れる波線は 200 ms ごと）。
void loop() { led_tick(); }
