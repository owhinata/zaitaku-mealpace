// シリアルフレーム（docs/data-schema.md）。Arduino 依存は Print& の引数のみ。
#pragma once
#include <stdint.h>
#include <string.h>

enum FrameId : uint8_t { FRAME_IMU = 0x01, FRAME_AUDIO = 0x02, FRAME_ANALOG = 0x03, FRAME_META = 0x7F };

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
