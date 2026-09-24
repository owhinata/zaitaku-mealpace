// シリアルフレーム（docs/data-schema.md）。Arduino 依存は Print& の引数のみ。
#pragma once
#include <stdint.h>
#include <string.h>

// ペイロードの並び（すべて LE。ヘッダの t_ms はどのストリームでも送るときの millis()）:
//   FRAME_IMU    float32 x6（ax ay az gx gy gz）
//   FRAME_AUDIO  int16 x N
//   FRAME_ANALOG uint16 x N
//   FRAME_DETECT 検出器のみ。10 B: window_t_ms u32, prob float32（swallow の確率）, positive u8（0/1）, led u8（0/1/2）
//   FRAME_FEAT   検出器のみ。4 + 4N B: window_t_ms u32, f0..f(N-1) float32（正規化前の特徴量。順序は META の feature_names）
//   FRAME_META   UTF-8 JSON
// 検出器ファームウェアは FRAME_AUDIO を送らない（docs/decisions/0005）。形式の決定は docs/decisions/0021。
enum FrameId : uint8_t {
  FRAME_IMU = 0x01, FRAME_AUDIO = 0x02, FRAME_ANALOG = 0x03, FRAME_DETECT = 0x04, FRAME_FEAT = 0x05, FRAME_META = 0x7F
};

template <typename Out>
static void frame_send(Out& out, uint8_t id, uint32_t t_ms, const uint8_t* payload, uint16_t len) {
  uint8_t hdr[9] = {0xA5, 0x5A, id, (uint8_t)(len & 0xFF), (uint8_t)(len >> 8),
                    (uint8_t)(t_ms & 0xFF), (uint8_t)(t_ms >> 8), (uint8_t)(t_ms >> 16), (uint8_t)(t_ms >> 24)};
  uint8_t x = 0;
  for (int i = 2; i < 9; i++) x ^= hdr[i];
  for (uint16_t i = 0; i < len; i++) x ^= payload[i];
  out.write(hdr, 9);
  out.write(payload, len);
  out.write(&x, 1);
}
