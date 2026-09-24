// 移植の答え合わせ（PC 側、#17）。synth / fft512 / audio_features / imu_features をリンクし、
// 合成信号（seed 固定）で計算して標準出力に出す。check_port.py が analysis/features.py と比べる。
// これは解析でも評価でもなく、移植の確認。特徴量の値は合成信号のものなので制限は無い。
//
// 実行:
//   g++ -O2 -std=gnu++14 -I firmware/bench firmware/bench/host/check_port.cpp firmware/bench/*.cpp -o build-host/check_port
//   .venv/bin/python firmware/bench/host/check_port.py build-host/check_port
//
// 出力（1 行 1 レコード、値は %.9g）:
//   full_audio <15 値>            全窓版（3.0 秒の合成音声の先頭 16000 サンプル）
//   full_imu <14 値>              IMU 14 次元（t_ms < 1000 の行）
//   reuse <k> <15 値>             再利用版。k = 0 は最初に窓が満ちたとき、k = 1..8 はその後の 8 ホップ
#include <stdio.h>
#include <stdint.h>
#include "synth.h"
#include "audio_features.h"
#include "imu_features.h"

static const uint32_t AUDIO_SECONDS = 3;
static const uint32_t AUDIO_TOTAL = AUDIO_SECONDS * AF_AUDIO_HZ;   // 48000

static int16_t audio[AUDIO_TOTAL];
static AudioReuseState reuse_state;
static uint32_t imu_t_ms[IMU_MAX_ROWS];
static float imu_acc[IMU_MAX_ROWS][3];
static float imu_gyro[IMU_MAX_ROWS][3];

static void print_row(const char* name, int index, const float* v, uint32_t n) {
  if (index >= 0) printf("%s %d", name, index); else printf("%s", name);
  for (uint32_t i = 0; i < n; i++) printf(" %.9g", (double)v[i]);
  printf("\n");
}

int main() {
  audio_features_init();

  SynthRng rng;
  synth_rng_seed(&rng, SYNTH_SEED_AUDIO);
  synth_audio(&rng, 0, AUDIO_TOTAL, audio);

  float out[AF_N_FEATURES];
  audio_features_full(audio, out);
  print_row("full_audio", -1, out, AF_N_FEATURES);

  synth_rng_seed(&rng, SYNTH_SEED_IMU);
  synth_imu(&rng, 0, IMU_MAX_ROWS, imu_t_ms, imu_acc, imu_gyro);
  uint32_t n = 0;
  while (n < IMU_MAX_ROWS && imu_t_ms[n] < 1000) n++;
  float imu_out[IMU_N_FEATURES];
  imu_features(imu_t_ms, imu_acc, imu_gyro, n, imu_out);
  print_row("full_imu", -1, imu_out, IMU_N_FEATURES);

  audio_reuse_init(&reuse_state);
  int k = 0;
  for (uint32_t s = 0; s + AF_HOP_SAMPLES <= AUDIO_TOTAL; s += AF_HOP_SAMPLES) {
    if (audio_reuse_push(&reuse_state, audio + s, out)) {
      print_row("reuse", k, out, AF_N_FEATURES);
      k++;
    }
  }
  return 0;
}
