// firmware/detector/host/test_detector.cpp — 検出器の Arduino に依存しないモジュールの PC 上のテスト（Issue #24 plan 第 15 節 1）。
// 対象: audio_capture、imu_capture、pipeline の帳簿（classify はスタブ）、detector_meta、detector_frames。
// data/ を使わない。Edge Impulse の SDK を要らない。実行: bash firmware/detector/host/test_detector.sh
//
// IMU の valid の突き合わせ: 合成の t_ms（gen_alt: 0 から +9, +10 を交互に 2100 未満まで）を analysis/features.py の
// _imu_period_ms と valid の規則に掛けた期待値（scratchpad の使い捨てスクリプトで 2026-09-25 に出した）を埋めてある。
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <math.h>
#include <vector>

#include "audio_capture.h"
#include "imu_capture.h"
#include "pipeline.h"
#include "classify.h"
#include "detector_meta.h"
#include "detector_frames.h"
#include "m2_norm.h"
#include "m2_threshold.h"

static int g_fail = 0;
static int g_pass = 0;
#define CHECK(cond) do { if (cond) { g_pass++; } else { g_fail++; printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } } while (0)
#define CHECK_EQ(a, b) do { long long _a = (long long)(a), _b = (long long)(b); if (_a == _b) { g_pass++; } else { g_fail++; printf("FAIL %s:%d: %s == %s (%lld != %lld)\n", __FILE__, __LINE__, #a, #b, _a, _b); } } while (0)

// ---------- classify のスタブ（SDK 無し。pipeline_hop の帳簿と positive の判定を試す） ----------
static float g_stub_swallow = 0.0f;
static bool g_stub_ok = true;
bool classify_init() { return true; }
bool classify_run(const float* z, float* probs) {
  (void)z;
  if (!g_stub_ok) return false;
  probs[0] = 0.0f; probs[1] = 1.0f - g_stub_swallow; probs[2] = g_stub_swallow;
  return true;
}
int classify_swallow_index() { return 2; }
int32_t classify_last_error() { return 0; }
uint32_t classify_project_id() { return 1117130; }
uint32_t classify_deploy_version() { return 2; }

// ---------- audio_capture ----------
static int16_t g_chunk[256];

// 64 サンプルのチャンクを値 v で埋めて積む（t は 4 ms 刻み）
static void push64(uint32_t t_ms, int16_t v) {
  for (int i = 0; i < 64; i++) g_chunk[i] = v;
  audio_capture_push_chunk(g_chunk, 128, t_ms);
}

static void test_audio_capture_split() {
  audio_capture_init();
  uint32_t t = 1000;
  // 62 チャンク = 3968 サンプル。まだ READY でない
  for (int c = 0; c < 62; c++) { push64(t, (int16_t)c); t += 4; }
  CHECK_EQ(audio_capture_face_state(0), AC_WRITING);
  CHECK_EQ(audio_capture_face_fill(0), 3968);
  CHECK_EQ(audio_capture_face_state(1), AC_FREE);
  // 63 チャンク目（t = 1000 + 62×4 = 1248）: 前半 32 が面 0 を満たし、後半 32 が面 1 の先頭に来る
  push64(t, 62);
  CHECK_EQ(audio_capture_face_state(0), AC_READY);
  CHECK_EQ(audio_capture_face_state(1), AC_WRITING);
  CHECK_EQ(audio_capture_face_fill(1), 32);
  const int16_t* s; uint32_t t0, seq, rd;
  CHECK(audio_capture_take(&s, &t0, &seq, &rd));
  CHECK_EQ(t0, 1000 - 4);          // 先頭のチャンクの t − 4 ms
  CHECK_EQ(seq, 0);
  CHECK_EQ(rd, 1248);
  CHECK_EQ(s[0], 0);
  CHECK_EQ(s[3967], 61);
  CHECK_EQ(s[3999], 62);
  CHECK_EQ(audio_capture_face_state(0), AC_IN_USE);
  audio_capture_release();
  CHECK_EQ(audio_capture_face_state(0), AC_FREE);
  // 面 1 を満たす: 残り 3968 = 62 チャンク
  t += 4;
  for (int c = 0; c < 62; c++) { push64(t, (int16_t)(100 + c)); t += 4; }
  CHECK_EQ(audio_capture_face_state(1), AC_READY);
  CHECK(audio_capture_take(&s, &t0, &seq, &rd));
  CHECK_EQ(seq, 1);                // 連番は 1 ずつ
  CHECK_EQ(t0, 1248 - 2);          // 後半 32 サンプルから始まった: t − 2 ms
  CHECK_EQ(s[0], 62);
  CHECK_EQ(s[32], 100);
  audio_capture_release();
  AudioCaptureStats st; audio_capture_stats(&st);
  CHECK_EQ(st.dropped_chunks, 0);
  CHECK_EQ(st.pdm_odd_chunks, 0);
  CHECK_EQ(st.pdm_gaps, 0);
  CHECK_EQ(st.slices_started, 2);  // 62 チャンクでちょうど満ちたので 3 つ目はまだ始まっていない
  CHECK_EQ(audio_capture_face_state(0), AC_FREE);
  push64(t, 7);                    // 次のチャンクで 3 つ目（面 0）が始まる
  CHECK_EQ(audio_capture_face_state(0), AC_WRITING);
  audio_capture_stats(&st);
  CHECK_EQ(st.slices_started, 3);
}

static void test_audio_capture_states() {
  audio_capture_init();
  uint32_t t = 0;
  for (int c = 0; c < 63; c++) { push64(t, 1); t += 4; }   // 面 0 READY、面 1 に 32
  const int16_t* s; uint32_t t0, seq, rd;
  CHECK(audio_capture_take(&s, &t0, &seq, &rd));            // 面 0 IN_USE のまま放さない
  CHECK_EQ(seq, 0);
  for (int c = 0; c < 62; c++) { push64(t, 2); t += 4; }   // 面 1 が 4000 で READY
  CHECK_EQ(audio_capture_face_state(1), AC_READY);
  CHECK_EQ(audio_capture_face_state(0), AC_IN_USE);
  // FREE の面が無い状態で届いたチャンクは捨てられる
  push64(t, 99); t += 4;
  push64(t, 99); t += 4;
  AudioCaptureStats st; audio_capture_stats(&st);
  CHECK_EQ(st.dropped_chunks, 2);
  CHECK_EQ(audio_capture_face_state(0), AC_IN_USE);
  CHECK_EQ(audio_capture_face_state(1), AC_READY);
  CHECK_EQ(s[0], 1); CHECK_EQ(s[3999], 1);                 // IN_USE の中身は変わらない
  // 主スレッドが取っていない（READY）面の中身も変わらない: 放してから取って確かめる
  audio_capture_release();
  const int16_t* s1; uint32_t t01, seq1;
  CHECK(audio_capture_take(&s1, &t01, &seq1, &rd));
  CHECK_EQ(seq1, 1);
  CHECK_EQ(s1[0], 1); CHECK_EQ(s1[32], 2); CHECK_EQ(s1[3999], 2);
  audio_capture_release();
  // 面 0 は FREE。次のチャンクで始まるスライスの連番は 1 つ飛ぶ（2 ではなく 3）
  uint32_t t_start = t;
  for (int c = 0; c < 63; c++) { push64(t, 5); t += 4; }
  CHECK_EQ(audio_capture_face_state(0), AC_READY);
  CHECK(audio_capture_take(&s, &t0, &seq, &rd));
  CHECK_EQ(seq, 3);
  CHECK_EQ(t0, t_start - 4);
  audio_capture_release();
  audio_capture_stats(&st);
  CHECK_EQ(st.dropped_chunks, 2);
  CHECK_EQ(st.pdm_gaps, 0);
}

// 境目で割れたチャンクの後半だけが捨てられる場合
static void test_audio_capture_split_drop() {
  audio_capture_init();
  uint32_t t = 0;
  for (int c = 0; c < 63; c++) { push64(t, 1); t += 4; }   // 面 0 READY、面 1 に 32（seq 1）
  const int16_t* s; uint32_t t0, seq, rd;
  CHECK(audio_capture_take(&s, &t0, &seq, &rd));            // 面 0 IN_USE
  audio_capture_release();                                  // 面 0 FREE
  // 面 1 を満たす途中で面 0 に新しいスライス（seq 2）が始まるように: 面 1 の残り 3968 = 62 チャンク。
  // 61 チャンク積んでから面 0 を「取れない」状態にはできないので、代わりに面 1 を READY にしてから取り、面 0 が満ちる境目で
  // 後半を捨てる形にする
  for (int c = 0; c < 62; c++) { push64(t, 2); t += 4; }   // 面 1 READY（seq 1）、面 0 は FREE のまま
  CHECK(audio_capture_take(&s, &t0, &seq, &rd));            // 面 1 IN_USE（放さない）
  CHECK_EQ(seq, 1);
  for (int c = 0; c < 62; c++) { push64(t, 3); t += 4; }   // 面 0 に 3968（seq 2）
  CHECK_EQ(audio_capture_face_fill(0), 3968);
  push64(t, 4); t += 4;                                     // 前半 32 で面 0 が READY、後半 32 は行き場が無い
  CHECK_EQ(audio_capture_face_state(0), AC_READY);
  AudioCaptureStats st; audio_capture_stats(&st);
  CHECK_EQ(st.dropped_chunks, 1);
  audio_capture_release();                                  // 面 1 FREE
  const int16_t* s0; uint32_t seq0;
  CHECK(audio_capture_take(&s0, &t0, &seq0, &rd));
  CHECK_EQ(seq0, 2);
  CHECK_EQ(s0[3968], 4); CHECK_EQ(s0[3999], 4);            // 前半は入っている
  audio_capture_release();
  push64(t, 6);                                             // 次のスライスは連番が飛ぶ（4）。空いている面 0 に始まる
  CHECK_EQ(audio_capture_face_state(0), AC_WRITING);
  for (int c = 0; c < 62; c++) { t += 4; push64(t, 6); }
  CHECK(audio_capture_take(&s, &t0, &seq, &rd));
  CHECK_EQ(seq, 4);
  audio_capture_release();
}

static void test_audio_capture_pdm_gaps() {
  // n が 128 B でないコールバック: pdm_odd_chunks が増え、実際の量だけ積まれる
  audio_capture_init();
  for (int i = 0; i < 256; i++) g_chunk[i] = 7;
  audio_capture_push_chunk(g_chunk, 64, 100);              // 32 サンプル
  AudioCaptureStats st; audio_capture_stats(&st);
  CHECK_EQ(st.pdm_odd_chunks, 1);
  CHECK_EQ(st.pdm_gaps, 0);
  CHECK_EQ(audio_capture_face_fill(0), 32);
  audio_capture_push_chunk(g_chunk, 256, 104);             // 128 サンプル
  audio_capture_stats(&st);
  CHECK_EQ(st.pdm_odd_chunks, 2);
  CHECK_EQ(audio_capture_face_fill(0), 160);
  // t0 の換算は実際の n で: 最初のチャンク 32 サンプル → 100 − 2
  // （面 0 を満たして取り出して確かめる）
  uint32_t t = 108;
  while (audio_capture_face_state(0) != AC_READY) { push64(t, 7); t += 4; }
  const int16_t* s; uint32_t t0, seq, rd;
  CHECK(audio_capture_take(&s, &t0, &seq, &rd));
  CHECK_EQ(t0, 98);
  CHECK_EQ(seq, 0);
  audio_capture_release();

  // 面を半分書いた時点で n == 0: pdm_gaps が増え、途中までのサンプルは捨てられ、次のチャンクから飛んだ連番で始まる
  audio_capture_init();
  t = 1000;
  for (int c = 0; c < 31; c++) { push64(t, 1); t += 4; }   // 1984 サンプル
  CHECK_EQ(audio_capture_face_fill(0), 1984);
  audio_capture_push_chunk(g_chunk, 0, t); t += 4;         // n == 0
  audio_capture_stats(&st);
  CHECK_EQ(st.pdm_gaps, 1);
  CHECK_EQ(audio_capture_face_fill(0), 0);
  CHECK_EQ(audio_capture_face_state(0), AC_WRITING);
  uint32_t t_restart = t;
  for (int c = 0; c < 63; c++) { push64(t, 2); t += 4; }
  CHECK(audio_capture_take(&s, &t0, &seq, &rd));
  CHECK_EQ(seq, 1);                                        // 0 を飛ばして 1
  CHECK_EQ(t0, t_restart - 4);                             // t0 は次のチャンクで取り直す
  CHECK_EQ(s[0], 2); CHECK_EQ(s[3999], 2);
  audio_capture_release();

  // 面の途中で 6 ms 以上の飛び: 同じ扱い。5 ms は飛びにならない
  audio_capture_init();
  t = 2000;
  for (int c = 0; c < 20; c++) { push64(t, 1); t += 4; }
  t += 1;                                                  // 5 ms 間隔
  push64(t, 1); t += 4;
  audio_capture_stats(&st);
  CHECK_EQ(st.pdm_gaps, 0);
  CHECK_EQ(audio_capture_face_fill(0), 21 * 64);
  t += 2;                                                  // 6 ms 間隔
  push64(t, 3);
  audio_capture_stats(&st);
  CHECK_EQ(st.pdm_gaps, 1);
  CHECK_EQ(audio_capture_face_fill(0), 64);                // 途中までは捨て、このチャンクから始まる
  t_restart = t;
  t += 4;
  for (int c = 0; c < 62; c++) { push64(t, 3); t += 4; }
  CHECK(audio_capture_take(&s, &t0, &seq, &rd));
  CHECK_EQ(seq, 1);
  CHECK_EQ(t0, t_restart - 4);
  CHECK_EQ(s[0], 3);
  audio_capture_release();
}

// ---------- imu_capture ----------
static ImuCapture g_cap;
static ImuWindowSource g_src = { &g_cap, nullptr, nullptr };

static void push_row(uint32_t t) {
  float a[3] = { 0.0f, 1.0f, 0.0f }, g[3] = { 0.0f, 0.0f, 0.0f };
  imu_capture_push(&g_cap, t, a, g);
}

// 0 から +9, +10 を交互に limit 未満まで（scratchpad の imu_valid_expect.py の gen_alt と同じ）
static std::vector<uint32_t> gen_alt(uint32_t limit = 2100) {
  std::vector<uint32_t> t; t.push_back(0);
  int i = 0;
  while (true) { uint32_t nxt = t.back() + ((i % 2 == 0) ? 9 : 10); i++; if (nxt >= limit) break; t.push_back(nxt); }
  return t;
}

static bool near(float a, double b, double tol) { return fabs((double)a - b) <= tol; }

static void test_imu_window_valid_rules() {
  // 基準の周期 9.48 ms: 106 行は有効、95 行は有効、94 行は無効（0.9 × 1000 ÷ 9.48 = 94.94）
  ImuWindow w;
  memset(&w, 0, sizeof w);
  w.w0_ms = 1000; w.w1_ms = 2000;
  const float P = 9.48f;
  // n 行を窓いっぱいに等間隔（step10 = 10 × 刻み [ms]）で置く: t[i] = 1000 + floor(i × step10 / 10)
  auto fill_rows = [&](uint32_t n, uint32_t step10) { w.n = n; for (uint32_t i = 0; i < n; i++) w.t_ms[i] = 1000 + (i * step10) / 10; };
  fill_rows(106, 95); CHECK(imu_window_valid(&w, P));    // 9.5 ms 刻み、最後 1997
  fill_rows(95, 105); CHECK(imu_window_valid(&w, P));    // 10.5 ms 刻み、最後 1987（0.9 × 1000 ÷ 9.48 = 94.94 以上）
  fill_rows(94, 106); CHECK(!imu_window_valid(&w, P));   // 10.6 ms 刻み、最後 1985。行数だけで無効
  // 窓内に 20 ms の差分があれば無効
  fill_rows(106, 95); for (uint32_t i = 51; i < 106; i++) w.t_ms[i] += 10; w.t_ms[105] = 1997; CHECK(!imu_window_valid(&w, P));
  // 窓の直前からの飛びが窓と交われば無効（prev = 980、t[0] = 1005 → 25 ms、区間 (980, 1005) は窓と交わる）
  fill_rows(106, 95); for (uint32_t i = 0; i < 105; i++) w.t_ms[i] += 5;
  w.have_prev = true; w.prev_t_ms = 980; CHECK(!imu_window_valid(&w, P));
  // 飛びが窓の先頭ちょうどで終わる（t[0] == w0）なら交わらない → 有効
  fill_rows(106, 95); w.have_prev = true; w.prev_t_ms = 970; CHECK(imu_window_valid(&w, P));
  w.have_prev = false;
  // 2 行未満は無効
  fill_rows(1, 95); CHECK(!imu_window_valid(&w, P));
  fill_rows(0, 95); CHECK(!imu_window_valid(&w, P));
  // 局所の周期 12 ms の 83 行（12 × 82 = 984、最後 1984）: 基準 9.48 では無効（局所の周期 12 なら 75 行で足りてしまう。基準を使うことの確認）
  fill_rows(83, 120); CHECK(!imu_window_valid(&w, P)); CHECK(imu_window_valid(&w, 12.0f));
  // (iv) 窓の最後の行から終端まで 1.5 周期以上空くと無効（行数は足りている: 100 行、9.5 ms 刻みで最後 1940）
  fill_rows(100, 95); CHECK(!imu_window_valid(&w, P));    // 2000 − 1940 = 60 ≥ 14.2
  fill_rows(100, 100); CHECK(imu_window_valid(&w, P));    // 同じ 100 行でも 10 ms 刻み（最後 1990）なら有効
}

static void test_imu_baseline_and_python_agreement() {
  // 基準の移動平均が飛びの差分を含めないこと
  imu_capture_init(&g_cap);
  CHECK(near(imu_period_baseline_ms(&g_src), 1000.0 / 104.0, 1e-4));
  push_row(0); push_row(10); push_row(19); push_row(29);   // 差分 10, 9, 10
  CHECK(near(imu_period_baseline_ms(&g_src), 29.0 / 3.0, 1e-5));
  push_row(60);                                           // 31 ms（≥ 1.5 × 9.67）は入れない
  CHECK(near(imu_period_baseline_ms(&g_src), 29.0 / 3.0, 1e-5));
  push_row(70);
  CHECK(near(imu_period_baseline_ms(&g_src), 39.0 / 4.0, 1e-5));

  // analysis/features.py の同じ入力での valid と一致すること（期待値は scratchpad の imu_valid_expect.py の出力）
  struct Case { const char* name; uint32_t drop_lo, drop_hi; bool local12; uint32_t w0; double period_py; uint32_t rows_py; bool valid_py; };
  const Case cases[] = {
    { "alt_9_10",               0, 0,       false, 1000, 9.497737557, 105, true  },
    { "alt_gap20_in_window",    1500, 1520, false, 1000, 9.495412844, 103, false },
    { "alt_gap_straddle_start", 992, 1010,  false, 1000, 9.500000000, 104, false },
    { "alt_gap_straddle_end",   1990, 2010, false, 1000, 9.495412844, 104, false },
    { "local_12ms",             0, 0,       true,  1000, 10.537688442, 83, false },
    { "alt_window_1003",        0, 0,       false, 1003, 9.497737557, 105, true  },
  };
  for (const Case& c : cases) {
    std::vector<uint32_t> t;
    if (c.local12) {
      std::vector<uint32_t> base = gen_alt();
      for (uint32_t x : base) if (x < 1000) t.push_back(x);
      uint32_t x = t.back();
      while (x + 12 < 2000) { x += 12; t.push_back(x); }
      int i = 0;
      while (true) { x += (i % 2 == 0) ? 9 : 10; i++; if (x >= 2100) break; t.push_back(x); }
    } else {
      for (uint32_t x : gen_alt()) if (!(c.drop_lo <= x && x < c.drop_hi)) t.push_back(x);
    }
    imu_capture_init(&g_cap);
    for (uint32_t x : t) push_row(x);
    float period = imu_period_baseline_ms(&g_src);
    ImuWindow w;
    imu_window_copy(&g_src, c.w0, c.w0 + 1000, &w);
    bool v = imu_window_valid(&w, period);
    bool ok = near(period, c.period_py, 1e-6) && w.n == c.rows_py && v == c.valid_py;
    if (!ok) printf("  %s: device period=%.9f rows=%u valid=%d / python period=%.9f rows=%u valid=%d\n",
                    c.name, (double)period, (unsigned)w.n, (int)v, c.period_py, (unsigned)c.rows_py, (int)c.valid_py);
    CHECK(ok);
  }
}

static void test_imu_window_copy() {
  // 半開区間 [w0, w1)、直前の行、リングの一周、128 行の上限
  imu_capture_init(&g_cap);
  for (uint32_t i = 0; i < 300; i++) push_row(i * 10);     // 0..2990。リングは 192 行（1080..2990 が残る）
  ImuWindow w;
  imu_window_copy(&g_src, 2000, 3000, &w);
  CHECK_EQ(w.n, 100);
  CHECK_EQ(w.t_ms[0], 2000);                                // w0 は含む
  CHECK_EQ(w.t_ms[99], 2990);
  CHECK(w.have_prev); CHECK_EQ(w.prev_t_ms, 1990);
  CHECK(!w.truncated);
  imu_window_copy(&g_src, 1500, 2500, &w);
  CHECK_EQ(w.n, 100);
  CHECK_EQ(w.t_ms[99], 2490);                               // w1 は含まない
  CHECK_EQ(w.acc[0][1], 1);
  // リングより古い窓: 行は残っていない
  imu_window_copy(&g_src, 0, 1000, &w);
  CHECK_EQ(w.n, 0);
  CHECK(!w.have_prev);
  // 128 行の上限（5 ms 刻みで 200 行）
  imu_capture_init(&g_cap);
  for (uint32_t i = 0; i < 190; i++) push_row(5000 + i * 5);
  imu_window_copy(&g_src, 5000, 6000, &w);
  CHECK_EQ(w.n, IMU_MAX_ROWS);
  CHECK(w.truncated);
}

// ---------- pipeline の帳簿（classify はスタブ） ----------
static int16_t g_slice[AC_SLICE_SAMPLES];

// IMU の行を実時間の到着のように積む（リングは 192 行しか持たないので、窓の終端の少し先まで進めてから prepare を呼ぶ）
static uint32_t g_imu_t = 0;
static int g_imu_i = 0;
static void imu_reset() { imu_capture_init(&g_cap); g_imu_t = 0; g_imu_i = 0; }
static void imu_advance_to(uint32_t t_end) {
  while (g_imu_t < t_end) { push_row(g_imu_t); g_imu_t += (g_imu_i % 2 == 0) ? 9 : 10; g_imu_i++; }
}
// スライス t0 の 250 ms 後 + 20 ms まで IMU を進めてから prepare（detector.ino が w1 + 5 まで待つのと同じ向き）
static bool prep(uint32_t t0, uint32_t seq, HopResult* r) {
  imu_advance_to(t0 + 250 + 20);
  return pipeline_prepare(g_slice, t0, seq, &g_src, r);
}
static bool hop(uint32_t t0, uint32_t seq, HopResult* r) {
  imu_advance_to(t0 + 250 + 20);
  return pipeline_hop(g_slice, t0, seq, &g_src, r);
}

static void test_pipeline_bookkeeping() {
  pipeline_init_stages();
  for (uint32_t i = 0; i < AC_SLICE_SAMPLES; i++) g_slice[i] = (int16_t)((i * 37) % 2000 - 1000);
  imu_reset();
  HopResult r;
  // 4 スライス目で窓が出る。window_t_ms は最も古いスライスの t0
  CHECK(!prep(0, 0, &r));   CHECK_EQ(r.reason, HOP_NOT_FILLED);
  CHECK(!prep(250, 1, &r)); CHECK_EQ(r.reason, HOP_NOT_FILLED);
  CHECK(!prep(500, 2, &r));
  CHECK_EQ(pipeline_window_end_ms(750), 1000);
  CHECK(prep(750, 3, &r));
  CHECK_EQ(r.reason, HOP_OK);
  CHECK_EQ(r.window_t_ms, 0);
  CHECK(r.imu_rows >= 100 && r.imu_rows <= 108);
  CHECK(prep(1000, 4, &r));
  CHECK_EQ(r.window_t_ms, 250);
  CHECK_EQ(pipeline_window_end_ms(1250), 1500);
  // 連番の飛び: audio_reuse_init に戻り（resets++）、4 スライス後に窓が出る
  CHECK(!prep(1500, 6, &r)); CHECK_EQ(r.reason, HOP_RESET);
  PipelineStats st; pipeline_stats(&st);
  CHECK_EQ(st.resets, 1);
  CHECK(!prep(1750, 7, &r)); CHECK_EQ(r.reason, HOP_NOT_FILLED);
  CHECK(!prep(2000, 8, &r));
  CHECK(prep(2250, 9, &r));
  CHECK_EQ(r.window_t_ms, 1500);
  // 無効な IMU の窓: 窓を出さず imu_short++（IMU スレッドが止まった形: 行を進めずに次のスライス。窓 [1750, 2750) の行は 2520 まで）
  CHECK(!pipeline_prepare(g_slice, 2500, 10, &g_src, &r));
  CHECK_EQ(r.reason, HOP_IMU_SHORT);
  CHECK(r.imu_rows < 94);
  pipeline_stats(&st);
  CHECK_EQ(st.imu_short, 1);
  CHECK_EQ(st.windows, 0);   // prepare だけでは windows は増えない
  // 行が届けば次の窓は出る（音声は連続なので窓を作り直さない）
  CHECK(prep(2750, 11, &r));
  CHECK_EQ(r.window_t_ms, 2000);

  // pipeline_hop（スタブの classify）: positive は float32 同士の比較
  CHECK(pipeline_init());
  imu_reset();
  g_stub_swallow = 0.95f;
  for (uint32_t k = 0; k < 3; k++) CHECK(!hop(k * 250, k, &r));
  CHECK(hop(750, 3, &r));
  CHECK_EQ(r.positive, 1);            // 0.95f >= M2_THRESHOLD(0.95f)
  CHECK_EQ(r.led, 0);
  CHECK(r.prob == M2_THRESHOLD);
  g_stub_swallow = 0.9499f;
  CHECK(hop(1000, 4, &r));
  CHECK_EQ(r.positive, 0);
  CHECK_EQ(r.window_t_ms, 250);
  // 特徴量の並び: 0..13 が IMU（静止の合成: acc_axis_y が 1 に近い）、14..28 が音
  CHECK(near(r.features[12], 1.0, 1e-3));
  g_stub_ok = false;
  CHECK(!hop(1250, 5, &r));
  CHECK_EQ(r.reason, HOP_CLASSIFY_ERROR);
  g_stub_ok = true;
  pipeline_stats(&st);
  CHECK_EQ(st.windows, 2);
  CHECK_EQ(st.classify_errors, 1);
}

// ---------- detector_meta ----------
static void test_meta() {
  static char buf[DETECTOR_META_CAP];
  int n = detector_meta_build(buf, sizeof buf, 1117130, 2);
  CHECK(n > 0);
  const char* expected =
    "{\"fw\":\"detector\",\"imu_hz\":104,\"audio_hz\":16000,\"window_ms\":1000,\"hop_ms\":250,"
    "\"threshold\":0.949999988,"
    "\"model\":{\"source\":\"edge-impulse\",\"project_id\":1117130,\"deploy_version\":2},"
    "\"feature_set\":\"m2-0020\","
    "\"feature_names\":[\"acc_ptp_x\",\"acc_ptp_y\",\"acc_ptp_z\",\"gyro_norm_ptp\",\"acc_rms_x\",\"acc_rms_y\",\"acc_rms_z\","
    "\"gyro_rms_x\",\"gyro_rms_y\",\"gyro_rms_z\",\"acc_peak_count\",\"acc_axis_x\",\"acc_axis_y\",\"acc_axis_z\","
    "\"mfcc_0\",\"mfcc_1\",\"mfcc_2\",\"mfcc_3\",\"mfcc_4\",\"mfcc_5\",\"mfcc_6\",\"mfcc_7\",\"mfcc_8\",\"mfcc_9\",\"mfcc_10\","
    "\"mfcc_11\",\"mfcc_12\",\"spectral_centroid_hz\",\"zero_crossing_rate\"]}";
  if (n > 0 && strcmp(buf, expected) != 0) printf("  meta: %s\n", buf);
  CHECK(n > 0 && strcmp(buf, expected) == 0);
  CHECK_EQ(n, (int)strlen(expected));
  CHECK(n < (int)DETECTOR_META_CAP);
  printf("  meta length %d B\n", n);
  // 収まらないときは −1
  char small[64];
  CHECK_EQ(detector_meta_build(small, sizeof small, 1, 2), -1);
}

// ---------- detector_frames ----------
static void test_frames() {
  HopResult r;
  memset(&r, 0, sizeof r);
  r.window_t_ms = 0x04030A01u;
  r.prob = 0.75f;     // 0x3F400000
  r.positive = 1; r.led = 0;
  for (int i = 0; i < M2_N_FEATURES; i++) r.features[i] = (float)i * 0.5f - 3.0f;
  uint8_t p[DETECT_PAYLOAD_LEN];
  pack_detect(&r, p);
  CHECK_EQ(DETECT_PAYLOAD_LEN, 10);
  CHECK_EQ(p[0], 0x01); CHECK_EQ(p[1], 0x0A); CHECK_EQ(p[2], 0x03); CHECK_EQ(p[3], 0x04);
  CHECK_EQ(p[4], 0x00); CHECK_EQ(p[5], 0x00); CHECK_EQ(p[6], 0x40); CHECK_EQ(p[7], 0x3F);
  CHECK_EQ(p[8], 1); CHECK_EQ(p[9], 0);
  // struct.unpack("<IfBB") と同じ読み方で戻る
  uint32_t wt; float prob;
  memcpy(&wt, p, 4); memcpy(&prob, p + 4, 4);
  CHECK_EQ(wt, 0x04030A01u); CHECK(prob == 0.75f);
  uint8_t f[FEAT_PAYLOAD_LEN];
  pack_feat(&r, f);
  CHECK_EQ(FEAT_PAYLOAD_LEN, 120);
  CHECK_EQ((FEAT_PAYLOAD_LEN - 4) % 4, 0);
  memcpy(&wt, f, 4); CHECK_EQ(wt, 0x04030A01u);
  bool all = true;
  for (int i = 0; i < M2_N_FEATURES; i++) { float v; memcpy(&v, f + 4 + 4 * i, 4); if (v != r.features[i]) all = false; }
  CHECK(all);
}

int main() {
  test_audio_capture_split();
  test_audio_capture_states();
  test_audio_capture_split_drop();
  test_audio_capture_pdm_gaps();
  test_imu_window_valid_rules();
  test_imu_baseline_and_python_agreement();
  test_imu_window_copy();
  test_pipeline_bookkeeping();
  test_meta();
  test_frames();
  printf("test_detector: %d passed, %d failed\n", g_pass, g_fail);
  return g_fail == 0 ? 0 : 1;
}
