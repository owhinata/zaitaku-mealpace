// 検出器の META フレームの JSON（Issue #24 plan 第 8 節、docs/decisions/0021、docs/data-schema.md）。Arduino 依存なし。
//   {"fw":"detector","imu_hz":104,"audio_hz":16000,"window_ms":1000,"hop_ms":250,"threshold":0.949999988,
//    "model":{"source":"edge-impulse","project_id":<ID>,"deploy_version":<V>},"feature_set":"m2-0020",
//    "feature_names":["acc_ptp_x",…,"zero_crossing_rate"]}
// threshold は M2_THRESHOLD（float32）を %.9g で。feature_set は M2_FEATURE_SET、feature_names は M2_FEATURE_NAMES の 29 個
// （analysis/m2_scorer.check_meta が完全一致を要求する）。imu_hz / audio_hz はセンサの取り込みのレート。
#pragma once
#include <stdint.h>
#include <stddef.h>

static const uint32_t DETECTOR_IMU_HZ = 104;
static const uint32_t DETECTOR_AUDIO_HZ = 16000;
static const uint32_t DETECTOR_WINDOW_MS = 1000;
static const uint32_t DETECTOR_HOP_MS = 250;
static const size_t DETECTOR_META_CAP = 768;   // 約 620 B が収まる固定バッファ

// buf（cap バイト）に JSON を作り、長さを返す（終端の NUL は含めない）。収まらなければ −1（setup() で止まる）。
int detector_meta_build(char* buf, size_t cap, uint32_t project_id, uint32_t deploy_version);

// PC → 装置の合図（Issue #33、docs/decisions/0006 の追記）。tools/record.py が記録の開始時に 1 バイト 'M' を送り、
// 検出器は受けたら setup() で作った同じ META を送り直す。フレームではない（SYNC も長さも無い）。他のバイトは捨てる。
static const uint8_t DETECTOR_META_REQUEST = 0x4D;   // 'M'
// c（Serial.read() の戻り値。受けていなければ −1）が合図なら真
bool detector_meta_is_request(int c);
