// rx_hold.h — MCU の自律消灯（Issue #52、docs/decisions/0049）。
// loop() が zm_set_state の受信に気づいてから約 2 秒（2000 ms）、次の
// 受信に気づかなければ消灯（0）にする。起点は RPC のハンドラが受けた
// 時刻ではなく、loop() が受信の数（seq）の変化を見た時刻。
// 0018 の保持（陽性窓から 2.0 秒）と、窓の結果が 1.0 秒出ないときの消灯は
// Linux 側（detector.py）が持つ。MCU は保持を計算しない。
// Arduino に依存しない（PC のホストテスト host/test_unoq_mcu.sh でも
// ビルドする）。共有の led_rule.h の LED_OFF_TIMEOUT_MS（1000）は使わない。
#pragma once
#include <stdint.h>

// loop() が最後の受信に気づいてから消灯までの時間 [ms]（0049）
static const uint32_t UNOQ_RX_OFF_MS = 2000;

struct RxHold {
  uint32_t seen_seq;    // 最後に見た受信の数
  uint32_t last_rx_ms;  // 受信の数の変化に気づいた時刻 [ms]
  uint8_t has_rx;  // 受信に気づいていて、まだ時間切れでないか
};

void rx_hold_init(RxHold* h);

// loop() から呼ぶ。seq: 受信の数（ハンドラが受けるたびに 1 つ増やす）、
// state: 最後に受けた状態、now_ms: millis()。seq が前と違えば
// last_rx_ms = now_ms にする（同じ状態でも、状態 0 でも取り直す）。
// 受信に気づいていて、気づいてから UNOQ_RX_OFF_MS 未満なら state を、
// そうでなければ 0（消灯）を返す。経過は uint32 の差（millis() の一周に
// 強い）。時間切れの後は次の受信まで消灯を保つ（一周で再点灯しない）。
uint8_t rx_hold_shown(RxHold* h, uint32_t seq, uint8_t state, uint32_t now_ms);
