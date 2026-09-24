// 検出器の DETECT / FEAT フレームのペイロード（docs/decisions/0021、docs/data-schema.md、frame.h）。Arduino 依存なし。
//   DETECT（0x04、10 B）: window_t_ms u32 LE、prob float32 LE（swallow の確率）、positive u8（0/1）、led u8（0/1/2）
//   FEAT  （0x05、4 + 4 × 29 B）: window_t_ms u32 LE、正規化前の特徴量 float32 LE × 29（M2_FEATURE_NAMES の順 =
//          40 フレームの平均の MFCC 13・重心・ゼロ交差率と IMU の 14 次元。波形の標本は含まない）
// tools/record.py は struct.unpack("<IfBB") と "<I29f" で読む。
#pragma once
#include <stdint.h>
#include <string.h>
#include "pipeline.h"

static const uint16_t DETECT_PAYLOAD_LEN = 10;
static const uint16_t FEAT_PAYLOAD_LEN = 4 + 4 * M2_N_FEATURES;   // 120

static inline void put_u32le(uint8_t* p, uint32_t v) {
  p[0] = (uint8_t)(v & 0xFF); p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}

static inline void put_f32le(uint8_t* p, float f) {
  uint32_t v;
  memcpy(&v, &f, sizeof(v));   // float32 のビット列をそのまま（Cortex-M と x86 はどちらも IEEE 754 single）
  put_u32le(p, v);
}

static inline void pack_detect(const HopResult* r, uint8_t* p) {
  put_u32le(p, r->window_t_ms);
  put_f32le(p + 4, r->prob);
  p[8] = r->positive;
  p[9] = r->led;
}

static inline void pack_feat(const HopResult* r, uint8_t* p) {
  put_u32le(p, r->window_t_ms);
  for (int i = 0; i < M2_N_FEATURES; i++) put_f32le(p + 4 + 4 * i, r->features[i]);
}
