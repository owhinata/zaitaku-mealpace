// 検出器の IMU の取り込み（Issue #24、plan 第 6 節 (b)）。Arduino 依存なし。ヒープは使わない。
//
// IMU スレッド（sensors.cpp）が 104 Hz の行（millis()、acc[3] [g]、gyro[3] [deg/s]）をリング（IC_RING_ROWS 行）に積む。
// 主スレッド（pipeline.cpp）が窓 [w0, w1) の行を取り出す（半開区間。analysis/features.py の
// searchsorted(imu_t, [s, s + 1.0], side="left") と同じ）。直前の 1 行（t < w0 の最後の行）も添える。
//
// 排他: リングは IMU スレッドと主スレッドの両方が触るので、ImuWindowSource の lock / unlock（sensors.cpp が mbed の
// critical section を渡す。無ければ呼ばない）で囲む。push は呼ぶ側が lock を持って呼ぶ。
//
// 基準の周期（docs/decisions/0012 の valid 規則の装置での近似。plan 第 6 節 (b)）: 直近 IC_BASELINE_ROWS 行の差分の移動平均
// （= 経過時間 ÷ 行数）を公称の 1/1.25〜1.25 倍に収めたもの。起動直後は公称 104 Hz の周期で、行が積まれるにつれて実測に寄る。
// analysis/features.py の _imu_period_ms がセッション全体で出す周期の、装置での近似。
// plan は「飛びでない差分（1.5 × 現在の基準 未満）だけの移動平均」だったが、装置の t_ms は IMU スレッドの poll（4 ms）で量子化されて
// 差分が 4〜16 ms に散るため、大きい側だけを除くと平均が下に偏り、基準が下がる → 閾値が下がってさらに除く、の自己固定に入りうる
// （#24 の 4 回目の実機で起動後 約 11 秒すべての窓が無効だった原因の見立て）。飛びを含めた平均は量子化に偏らず、飛び G ms の影響は
// G ÷ 511 ms に留まる（飛びそのものは (ii)〜(iv) で窓ごとに弾く）。
//
// imu_window_valid（0012 の規則、analysis/features.py の valid と同じ形）:
//   (0) 行が 2 未満なら無効。
//   (i) 行数が MIN_IMU_ROW_RATIO(0.9) × 1000 ÷ 周期 未満なら無効。
//   (ii) 窓内の隣り合う行の差分が GAP_RATIO(1.5) × 周期 以上なら無効（飛び）。
//   (iii) 窓の先頭の行の直前の行との差分が 1.5 × 周期 以上で、その飛びの区間 (prev, t[0]) が窓と交わる（t[0] > w0）なら無効。
//   (iv) 窓の最後の行から窓の終端までが 1.5 × 周期 以上なら無効（次の行はまだ届いていなくても、次の行との差分は必ず飛びになり、
//        その区間は窓と交わる。features.py では飛びの区間 (t[n−1], next) が窓と交わる形で無効になる）。
#pragma once
#include <stdint.h>
#include <stdbool.h>
#include "imu_features.h"   // IMU_MAX_ROWS（128）

static const uint32_t IC_RING_ROWS = 192;                // 104 Hz で約 1.85 秒
static const uint32_t IC_BASELINE_ROWS = 512;            // 基準の周期の移動平均の長さ
static const float IC_NOMINAL_PERIOD_MS = 1000.0f / 104.0f;
static const float IC_BASELINE_CLAMP = 1.25f;                // 基準の周期を公称の 1/1.25〜1.25 倍に収める
static const float IC_GAP_RATIO = 1.5f;                  // analysis/features.py の GAP_RATIO
static const float IC_MIN_ROW_RATIO = 0.9f;              // analysis/features.py の MIN_IMU_ROW_RATIO
static const uint32_t IC_WINDOW_MS = 1000;

struct ImuRow {
  uint32_t t_ms;
  float acc[3];
  float gyro[3];
};

struct ImuCapture {
  ImuRow ring[IC_RING_ROWS];
  uint32_t head;            // 次に書く位置
  uint32_t count;           // 入っている行数（IC_RING_ROWS で飽和）
  uint32_t total;           // 積んだ行の総数
  uint16_t diffs[IC_BASELINE_ROWS];   // 直近の差分 [ms]（飛びも含む。0xFFFF で飽和）
  uint32_t diff_head;
  uint32_t diff_count;
  uint32_t diff_sum;
  bool have_last;
  uint32_t last_t_ms;
};

struct ImuWindowSource {
  ImuCapture* cap;
  void (*lock)(void);       // 無ければ nullptr
  void (*unlock)(void);
};

struct ImuWindow {
  uint32_t t_ms[IMU_MAX_ROWS];
  float acc[IMU_MAX_ROWS][3];
  float gyro[IMU_MAX_ROWS][3];
  uint32_t n;               // 取り出した行数（≤ IMU_MAX_ROWS）
  uint32_t w0_ms, w1_ms;    // 窓 [w0, w1)
  bool have_prev;           // t < w0 の行があった
  uint32_t prev_t_ms;       // その最後の行の時刻
  bool truncated;           // 窓内の行が IMU_MAX_ROWS を超えた（超えた分は捨てた）
};

void imu_capture_init(ImuCapture* cap);
// 1 行を積む。呼ぶ側が lock を持つ。t_ms は millis()（音声と同じ時計）。
void imu_capture_push(ImuCapture* cap, uint32_t t_ms, const float acc[3], const float gyro[3]);
// 基準の周期 [ms]（直近 IC_BASELINE_ROWS 行の差分の平均を公称の 1/1.25〜1.25 倍に収めたもの。差分が無ければ公称）。lock を取って読む。
float imu_period_baseline_ms(const ImuWindowSource* src);
// 窓 [w0, w1) の行を out に写す（時刻順。IMU_MAX_ROWS を超える分は捨てる）。lock を取って写す。
void imu_window_copy(const ImuWindowSource* src, uint32_t w0_ms, uint32_t w1_ms, ImuWindow* out);
// 0012 の valid 規則（上のコメント）。period_ms は imu_period_baseline_ms の値。
bool imu_window_valid(const ImuWindow* w, float period_ms);
// テスト用: 積んだ行の総数
uint32_t imu_capture_total(const ImuCapture* cap);
