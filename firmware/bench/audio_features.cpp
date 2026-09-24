// 案2 の移植（audio_features.h）。Arduino 依存なし。ヒープは使わない。
// 装置のビルドでは BENCH_CASE == 2 のときだけ組み込む。PC の答え合わせ（BENCH_CASE 未定義）では常に組み込む。
#if !defined(BENCH_CASE) || BENCH_CASE == 2

#include "audio_features.h"
#include "fft512.h"
#include <math.h>
#include <string.h>

// --- 表（audio_features_init で作る） ---
static float s_hamming[AF_FRAME_LEN];
// メル三角フィルタ（疎）。フィルタ m は power[s_mel_start[m] + i] × s_mel_w[s_mel_off[m] + i]（i < s_mel_len[m]）
static uint16_t s_mel_start[AF_N_MEL];
static uint16_t s_mel_len[AF_N_MEL];
static uint16_t s_mel_off[AF_N_MEL];
static float s_mel_w[2 * AF_N_BINS];   // 各ビンは高々 2 本のフィルタに属する
static float s_dct[AF_N_MFCC][AF_N_MEL];

// --- 作業領域（固定配列） ---
static float s_frame[AF_N_FFT];      // Hamming 後、512 にゼロ詰め
static float s_power[AF_N_BINS];
static float s_pre[AF_FRAME_LEN];
static float s_raw[AF_FRAME_LEN];
static float s_logmel[AF_N_MEL];

static double hz_to_mel(double f) { return 2595.0 * log10(1.0 + f / 700.0); }
static double mel_to_hz(double m) { return 700.0 * (pow(10.0, m / 2595.0) - 1.0); }

void audio_features_init() {
  fft512_init();
  for (uint32_t n = 0; n < AF_FRAME_LEN; n++) {
    s_hamming[n] = (float)(0.54 - 0.46 * cos(2.0 * M_PI * (double)n / (double)(AF_FRAME_LEN - 1)));
  }
  // メル（HTK 式、FFT ビンの周波数で評価、面積の正規化なし）。features.py の _mel_filterbank と同じ
  double mel_lo = hz_to_mel(AF_MEL_FMIN_HZ), mel_hi = hz_to_mel(AF_MEL_FMAX_HZ);
  double hz[AF_N_MEL + 2];
  for (uint32_t i = 0; i < AF_N_MEL + 2; i++) {
    hz[i] = mel_to_hz(mel_lo + (mel_hi - mel_lo) * (double)i / (double)(AF_N_MEL + 1));
  }
  uint32_t off = 0;
  for (uint32_t m = 0; m < AF_N_MEL; m++) {
    double lo = hz[m], c = hz[m + 1], hi = hz[m + 2];
    uint32_t start = 0, len = 0;
    bool started = false;
    s_mel_off[m] = (uint16_t)off;
    for (uint32_t k = 0; k < AF_N_BINS; k++) {
      double f = (double)k * (double)AF_AUDIO_HZ / (double)AF_N_FFT;
      double up = (f - lo) / (c - lo), down = (hi - f) / (hi - c);
      double w = up < down ? up : down;
      if (w < 0.0) w = 0.0;
      if (w > 0.0) {
        if (!started) { started = true; start = k; }
        len = k - start + 1;
      }
    }
    s_mel_start[m] = (uint16_t)start;
    s_mel_len[m] = (uint16_t)len;
    for (uint32_t i = 0; i < len; i++) {
      double f = (double)(start + i) * (double)AF_AUDIO_HZ / (double)AF_N_FFT;
      double up = (f - lo) / (c - lo), down = (hi - f) / (hi - c);
      double w = up < down ? up : down;
      if (w < 0.0) w = 0.0;
      s_mel_w[off + i] = (float)w;
    }
    off += len;
  }
  // DCT-II（ortho）の先頭 13 行。features.py の _dct_matrix と同じ
  for (uint32_t k = 0; k < AF_N_MFCC; k++) {
    for (uint32_t m = 0; m < AF_N_MEL; m++) {
      double d = cos(M_PI * (double)k * (2.0 * (double)m + 1.0) / (2.0 * (double)AF_N_MEL)) * sqrt(2.0 / (double)AF_N_MEL);
      if (k == 0) d *= sqrt(0.5);
      s_dct[k][m] = (float)d;
    }
  }
}

// --- 段階 ---

void audio_stage_preemphasis(const int16_t* x, uint32_t n, bool has_prev, float* pre) {
  const float inv = 1.0f / AF_AUDIO_SCALE;
  float prev = has_prev ? (float)x[-1] * inv : 0.0f;
  for (uint32_t i = 0; i < n; i++) {
    float cur = (float)x[i] * inv;
    pre[i] = (i == 0 && !has_prev) ? cur : cur - AF_PREEMPHASIS * prev;
    prev = cur;
  }
}

void audio_stage_convert(const int16_t* x, uint32_t n, float* raw) {
  const float inv = 1.0f / AF_AUDIO_SCALE;
  for (uint32_t i = 0; i < n; i++) raw[i] = (float)x[i] * inv;
}

void audio_stage_power(const float* frame, float* power) {
  for (uint32_t n = 0; n < AF_FRAME_LEN; n++) s_frame[n] = frame[n] * s_hamming[n];
  for (uint32_t n = AF_FRAME_LEN; n < AF_N_FFT; n++) s_frame[n] = 0.0f;
  fft512_power(s_frame, power);
}

void audio_stage_mel_dct(const float* power, float* dct) {
  for (uint32_t m = 0; m < AF_N_MEL; m++) {
    const float* p = power + s_mel_start[m];
    const float* w = s_mel_w + s_mel_off[m];
    float e = 0.0f;
    for (uint32_t i = 0; i < s_mel_len[m]; i++) e += p[i] * w[i];
    s_logmel[m] = logf(e + AF_LOG_FLOOR);
  }
  for (uint32_t k = 0; k < AF_N_MFCC; k++) {
    float acc = 0.0f;
    for (uint32_t m = 0; m < AF_N_MEL; m++) acc += s_dct[k][m] * s_logmel[m];
    dct[k] = acc;
  }
}

float audio_stage_centroid(const float* power) {
  const float bin_hz = (float)AF_AUDIO_HZ / (float)AF_N_FFT;   // 31.25
  float total = 0.0f, weighted = 0.0f;
  for (uint32_t k = 0; k < AF_N_BINS; k++) {
    total += power[k];
    weighted += (float)k * bin_hz * power[k];
  }
  if (total < AF_CENTROID_MIN_POWER) return 0.0f;
  return weighted / total;
}

float audio_stage_zcr(const int16_t* x, uint32_t n) {
  // 符号は x − 平均 ≥ 0 を正。平均は Σx ÷ n なので、x[i] ≥ 平均 は n·x[i] ≥ Σx と同じ（整数で厳密）
  int64_t sum = 0;
  for (uint32_t i = 0; i < n; i++) sum += x[i];
  uint32_t changes = 0;
  bool prev_pos = ((int64_t)x[0] * (int64_t)n) >= sum;
  for (uint32_t i = 1; i < n; i++) {
    bool pos = ((int64_t)x[i] * (int64_t)n) >= sum;
    if (pos != prev_pos) changes++;
    prev_pos = pos;
  }
  return (float)changes / (float)(n - 1);
}

// 1 フレーム: DCT 13 係数と重心
static void frame_features(const int16_t* x, bool has_prev, float* dct, float* centroid) {
  audio_stage_preemphasis(x, AF_FRAME_LEN, has_prev, s_pre);
  audio_stage_power(s_pre, s_power);
  audio_stage_mel_dct(s_power, dct);
  audio_stage_convert(x, AF_FRAME_LEN, s_raw);        // 重心はプリエンファシス前（2 回目の FFT）
  audio_stage_power(s_raw, s_power);
  *centroid = audio_stage_centroid(s_power);
}

// --- 全窓版 ---

void audio_features_full(const int16_t* x, float* out) {
  float acc[AF_N_MFCC];
  float dct[AF_N_MFCC];
  float csum = 0.0f;
  for (uint32_t k = 0; k < AF_N_MFCC; k++) acc[k] = 0.0f;
  for (uint32_t f = 0; f < AF_N_FRAMES; f++) {
    uint32_t s = f * AF_FRAME_HOP;
    float c;
    frame_features(x + s, s > 0, dct, &c);
    for (uint32_t k = 0; k < AF_N_MFCC; k++) acc[k] += dct[k];
    csum += c;
  }
  for (uint32_t k = 0; k < AF_N_MFCC; k++) out[k] = acc[k] / (float)AF_N_FRAMES;
  out[AF_N_MFCC] = csum / (float)AF_N_FRAMES;
  out[AF_N_MFCC + 1] = audio_stage_zcr(x, AF_WINDOW_SAMPLES);
}

// --- 再利用版 ---

void audio_reuse_init(AudioReuseState* st) {
  memset(st, 0, sizeof(*st));
}

bool audio_reuse_push(AudioReuseState* st, const int16_t* slice, float* out) {
  // リングをずらして新しいスライスを末尾に置く（この時間もホップに含める）
  memmove(st->ring, st->ring + AF_HOP_SAMPLES, (AF_WINDOW_SAMPLES - AF_HOP_SAMPLES) * sizeof(int16_t));
  memcpy(st->ring + (AF_WINDOW_SAMPLES - AF_HOP_SAMPLES), slice, AF_HOP_SAMPLES * sizeof(int16_t));
  if (st->filled < AF_WINDOW_SAMPLES) st->filled += AF_HOP_SAMPLES;
  if (st->filled < AF_WINDOW_SAMPLES) return false;

  uint32_t first;
  if (!st->primed) {
    first = 0;                       // 最初の窓は全 98 フレーム
    st->primed = true;
  } else {
    first = AF_N_FRAMES - AF_HOP_FRAMES;   // 73
    memmove(st->dct[0], st->dct[AF_HOP_FRAMES], first * AF_N_MFCC * sizeof(float));
    memmove(st->centroid, st->centroid + AF_HOP_FRAMES, first * sizeof(float));
  }
  for (uint32_t f = first; f < AF_N_FRAMES; f++) {
    uint32_t s = f * AF_FRAME_HOP;
    frame_features(st->ring + s, s > 0, st->dct[f], &st->centroid[f]);
  }
  if (first > 0) {
    // 窓の先頭フレームは以前に「前のサンプルを持つ位置」で計算されているので、全窓版と同じ pre[0] = x[0] で
    // DCT だけ計算し直す（1 ホップに FFT が 1 回増える。重心はプリエンファシスに依存しないのでそのまま）。
    // 答え合わせで、この差が許容差（相対 1e-3）を超えたため（最大 1.5e-3）。0012 の式は変えていない。
    audio_stage_preemphasis(st->ring, AF_FRAME_LEN, false, s_pre);
    audio_stage_power(s_pre, s_power);
    audio_stage_mel_dct(s_power, st->dct[0]);
  }
  float acc[AF_N_MFCC];
  float csum = 0.0f;
  for (uint32_t k = 0; k < AF_N_MFCC; k++) acc[k] = 0.0f;
  for (uint32_t f = 0; f < AF_N_FRAMES; f++) {
    for (uint32_t k = 0; k < AF_N_MFCC; k++) acc[k] += st->dct[f][k];
    csum += st->centroid[f];
  }
  for (uint32_t k = 0; k < AF_N_MFCC; k++) out[k] = acc[k] / (float)AF_N_FRAMES;
  out[AF_N_MFCC] = csum / (float)AF_N_FRAMES;
  out[AF_N_MFCC + 1] = audio_stage_zcr(st->ring, AF_WINDOW_SAMPLES);   // 窓の平均に依存するので毎回
  return true;
}

#endif  // BENCH_CASE
