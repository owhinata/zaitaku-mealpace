// 案3 の移植（imu_features.h）。Arduino 依存なし。ヒープは使わない。
// 装置のビルドでは BENCH_CASE == 3 のときだけ組み込む。PC の答え合わせ（BENCH_CASE 未定義）では常に組み込む。
#if !defined(BENCH_CASE) || BENCH_CASE == 3

#include "imu_features.h"
#include <math.h>

static float s_acc[IMU_MAX_ROWS][3];    // 平均を引いた加速度
static float s_gyro[IMU_MAX_ROWS][3];   // 平均を引いたジャイロ
static float s_norm[IMU_MAX_ROWS];      // 加速度のノルム
static uint16_t s_cand[IMU_MAX_ROWS];   // ピークの候補（高い順）
static uint16_t s_kept[IMU_MAX_ROWS];

// features.py の _count_peaks。局所極大（x[i] > x[i−1] かつ x[i] ≥ x[i+1]、両端は数えない）、高さ ≥ RMS × 1.0、
// 高い順（同じ高さは添字順）に見て、既に残したピークと t_ms の差が 50 ms 未満なら捨てる。
static uint32_t count_peaks(const float* x, const uint32_t* t_ms, uint32_t n) {
  if (n < 3) return 0;
  float ss = 0.0f;
  for (uint32_t i = 0; i < n; i++) ss += x[i] * x[i];
  float rms = sqrtf(ss / (float)n);
  float thr = IMU_PEAK_HEIGHT_RMS_RATIO * rms;
  uint32_t nc = 0;
  for (uint32_t i = 1; i + 1 < n; i++) {
    if (x[i] > x[i - 1] && x[i] >= x[i + 1] && x[i] >= thr) s_cand[nc++] = (uint16_t)i;
  }
  // 高い順の安定な挿入ソート
  for (uint32_t a = 1; a < nc; a++) {
    uint16_t v = s_cand[a];
    uint32_t b = a;
    while (b > 0 && x[s_cand[b - 1]] < x[v]) { s_cand[b] = s_cand[b - 1]; b--; }
    s_cand[b] = v;
  }
  uint32_t nk = 0;
  for (uint32_t a = 0; a < nc; a++) {
    uint32_t i = s_cand[a];
    bool ok = true;
    for (uint32_t b = 0; b < nk; b++) {
      uint32_t j = s_kept[b];
      uint32_t d = t_ms[i] > t_ms[j] ? t_ms[i] - t_ms[j] : t_ms[j] - t_ms[i];
      if (d < IMU_PEAK_MIN_DISTANCE_MS) { ok = false; break; }
    }
    if (ok) s_kept[nk++] = (uint16_t)i;
  }
  return nk;
}

// 対称 3×3 の Jacobi 法。固有値 w[3] と固有ベクトル v[:, i]（列）
static void jacobi3(float a[3][3], float w[3], float v[3][3]) {
  for (int i = 0; i < 3; i++) for (int j = 0; j < 3; j++) v[i][j] = (i == j) ? 1.0f : 0.0f;
  for (int sweep = 0; sweep < 50; sweep++) {
    float off = a[0][1] * a[0][1] + a[0][2] * a[0][2] + a[1][2] * a[1][2];
    if (off < 1e-30f) break;
    for (int p = 0; p < 2; p++) {
      for (int q = p + 1; q < 3; q++) {
        if (fabsf(a[p][q]) < 1e-30f) continue;
        float theta = (a[q][q] - a[p][p]) / (2.0f * a[p][q]);
        float t = (theta >= 0.0f ? 1.0f : -1.0f) / (fabsf(theta) + sqrtf(theta * theta + 1.0f));
        float c = 1.0f / sqrtf(t * t + 1.0f);
        float s = t * c;
        for (int k = 0; k < 3; k++) {          // 列の回転
          float akp = a[k][p], akq = a[k][q];
          a[k][p] = c * akp - s * akq;
          a[k][q] = s * akp + c * akq;
        }
        for (int k = 0; k < 3; k++) {          // 行の回転
          float apk = a[p][k], aqk = a[q][k];
          a[p][k] = c * apk - s * aqk;
          a[q][k] = s * apk + c * aqk;
        }
        for (int k = 0; k < 3; k++) {
          float vkp = v[k][p], vkq = v[k][q];
          v[k][p] = c * vkp - s * vkq;
          v[k][q] = s * vkp + c * vkq;
        }
      }
    }
  }
  for (int i = 0; i < 3; i++) w[i] = a[i][i];
}

// features.py の _principal_axis。共分散（÷ n）の第1固有ベクトル。決まらないときは [0, 1, 0]
static void principal_axis(uint32_t n, float* axis) {
  float cov[3][3];
  for (int i = 0; i < 3; i++) for (int j = 0; j < 3; j++) cov[i][j] = 0.0f;
  for (uint32_t r = 0; r < n; r++) {
    for (int i = 0; i < 3; i++) for (int j = i; j < 3; j++) cov[i][j] += s_acc[r][i] * s_acc[r][j];
  }
  for (int i = 0; i < 3; i++) for (int j = i; j < 3; j++) { cov[i][j] /= (float)n; cov[j][i] = cov[i][j]; }
  float w[3], v[3][3];
  jacobi3(cov, w, v);
  int i1 = 0;
  for (int i = 1; i < 3; i++) if (w[i] > w[i1]) i1 = i;
  int i2 = -1;
  for (int i = 0; i < 3; i++) if (i != i1 && (i2 < 0 || w[i] > w[i2])) i2 = i;
  float l1 = w[i1], l2 = w[i2];
  axis[0] = 0.0f; axis[1] = 1.0f; axis[2] = 0.0f;
  if (l1 < IMU_AXIS_MIN_EIGENVALUE || l1 - l2 < IMU_AXIS_MIN_EIGEN_GAP_RATIO * l1) return;
  float e[3] = { v[0][i1], v[1][i1], v[2][i1] };
  static const int order[3] = { 1, 0, 2 };   // Y、X、Z の順で最初の 0 でない成分を正にそろえる
  for (int k = 0; k < 3; k++) {
    float c = e[order[k]];
    if (fabsf(c) >= IMU_AXIS_SIGN_EPS) {
      float sgn = c > 0.0f ? 1.0f : -1.0f;
      axis[0] = sgn * e[0]; axis[1] = sgn * e[1]; axis[2] = sgn * e[2];
      return;
    }
  }
}

void imu_features(const uint32_t* t_ms, const float (*acc)[3], const float (*gyro)[3], uint32_t n, float* out) {
  for (uint32_t k = 0; k < IMU_N_FEATURES; k++) out[k] = 0.0f;
  if (n < 2) return;
  if (n > IMU_MAX_ROWS) n = IMU_MAX_ROWS;
  // 窓内の平均を引く
  float ma[3] = { 0, 0, 0 }, mg[3] = { 0, 0, 0 };
  for (uint32_t r = 0; r < n; r++) for (int i = 0; i < 3; i++) { ma[i] += acc[r][i]; mg[i] += gyro[r][i]; }
  for (int i = 0; i < 3; i++) { ma[i] /= (float)n; mg[i] /= (float)n; }
  for (uint32_t r = 0; r < n; r++) for (int i = 0; i < 3; i++) { s_acc[r][i] = acc[r][i] - ma[i]; s_gyro[r][i] = gyro[r][i] - mg[i]; }
  // peak-to-peak（加速度 3 軸、ジャイロのノルム）
  float amin[3], amax[3];
  float gnmin = 0, gnmax = 0;
  for (int i = 0; i < 3; i++) { amin[i] = s_acc[0][i]; amax[i] = s_acc[0][i]; }
  for (uint32_t r = 0; r < n; r++) {
    for (int i = 0; i < 3; i++) {
      if (s_acc[r][i] < amin[i]) amin[i] = s_acc[r][i];
      if (s_acc[r][i] > amax[i]) amax[i] = s_acc[r][i];
    }
    float gn = sqrtf(s_gyro[r][0] * s_gyro[r][0] + s_gyro[r][1] * s_gyro[r][1] + s_gyro[r][2] * s_gyro[r][2]);
    if (r == 0 || gn < gnmin) gnmin = gn;
    if (r == 0 || gn > gnmax) gnmax = gn;
    s_norm[r] = sqrtf(s_acc[r][0] * s_acc[r][0] + s_acc[r][1] * s_acc[r][1] + s_acc[r][2] * s_acc[r][2]);
  }
  for (int i = 0; i < 3; i++) out[i] = amax[i] - amin[i];
  out[3] = gnmax - gnmin;
  // RMS（÷ n）
  float sa[3] = { 0, 0, 0 }, sg[3] = { 0, 0, 0 };
  for (uint32_t r = 0; r < n; r++) for (int i = 0; i < 3; i++) { sa[i] += s_acc[r][i] * s_acc[r][i]; sg[i] += s_gyro[r][i] * s_gyro[r][i]; }
  for (int i = 0; i < 3; i++) { out[4 + i] = sqrtf(sa[i] / (float)n); out[7 + i] = sqrtf(sg[i] / (float)n); }
  // ピーク数
  out[10] = (float)count_peaks(s_norm, t_ms, n);
  // 主軸
  principal_axis(n, out + 11);
}

#endif  // BENCH_CASE
