// matrix_out.cpp — UNO R4 WiFi の 12×8 LED マトリクスへの書き込み（Issue #32）。説明は matrix_out.h。
// Arduino_LED_Matrix.h の renderBitmap はマクロなので、この翻訳単位の外に見出しを出さない。
// loadPixels は const でない uint8_t* を取るので、表（const）から作業用の s_frame に写してから渡す。
// マトリクスの点灯は begin() が取ったタイマの割り込み（10 kHz）が行う。明るさは変えない（固定）。
#include <Arduino.h>
#include "Arduino_LED_Matrix.h"
#include "matrix_out.h"
#include "led_rule.h"
#include "shape_seq.h"
#include "shapes.h"

static const uint32_t BEGIN_FAIL_BLINK_MS = 200;   // 起動失敗の LED_BUILTIN の点滅（0018「起動失敗の表示」と同じ形）

static ArduinoLEDMatrix s_matrix;
static uint8_t s_frame[8][12];      // renderBitmap に渡す作業用
static int s_shown_frame = -2;      // 最後に書いたフレーム（-1 = 消灯、-2 = 未書き込み）
static ShapeSeq s_seq;
static bool s_ok = false;           // begin() が成功したか
static int s_blink_level = -1;      // 起動失敗のとき最後に LED_BUILTIN に書いた値（-1 = 未書き込み）

static void write_frame(int f) {
  for (int y = 0; y < 8; ++y)
    for (int x = 0; x < 12; ++x) s_frame[y][x] = (f >= 0) ? FRAMES[f][y][x] : 0;
  s_matrix.renderBitmap(s_frame, 8, 12);
  s_shown_frame = f;
}

void matrix_out_begin() {
  shape_seq_init(&s_seq);
  s_ok = s_matrix.begin();
  if (s_ok) {
    write_frame(-1);   // 消灯
  } else {
    pinMode(LED_BUILTIN, OUTPUT);
    digitalWrite(LED_BUILTIN, LOW);
    s_blink_level = LOW;
  }
}

void matrix_out_show(uint8_t target, uint32_t now_ms) {
  if (!s_ok) {
    int level = ((now_ms / BEGIN_FAIL_BLINK_MS) & 1u) ? HIGH : LOW;
    if (level != s_blink_level) {
      digitalWrite(LED_BUILTIN, level);
      s_blink_level = level;
    }
    return;
  }
  int f = shape_seq_frame(&s_seq, target, now_ms);
  if (f != s_shown_frame) write_frame(f);
}

uint8_t matrix_out_state() {
  return s_ok ? s_seq.state : (uint8_t)LED_OFF;
}
