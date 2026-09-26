// 検出器の音声の取り込み（audio_capture.h）。Arduino 依存なし。ヒープは使わない。
#include "audio_capture.h"
#include <string.h>

struct AudioFace {
  int16_t buf[AC_SLICE_SAMPLES];
  volatile uint8_t state;      // AudioFaceState。割り込みと主スレッドの両方が読む（1 バイトの読み書き）
  uint32_t fill;               // WRITING の間だけ意味がある（割り込みだけが触る）
  uint32_t t0_ms;              // 先頭サンプルの時刻の推定値
  uint32_t seq;                // 連番
  uint32_t ready_t_ms;         // READY になったコールバックの t_ms
};

static AudioFace s_face[AC_N_FACES];
static int s_writing = -1;             // WRITING の面の添字（無ければ −1）。割り込みだけが触る
static int s_in_use = -1;              // IN_USE の面の添字。主スレッドだけが触る
static uint32_t s_next_seq = 0;        // 次に始めるスライスの連番
static bool s_discontinuity = false;   // 次に始めるスライスの連番を 1 つ余分に進める
static bool s_have_last_t = false;
static uint32_t s_last_t_ms = 0;
static AudioCaptureStats s_stats;

void audio_capture_init() {
  for (uint32_t i = 0; i < AC_N_FACES; i++) {
    s_face[i].state = AC_FREE;
    s_face[i].fill = 0;
    s_face[i].t0_ms = 0;
    s_face[i].seq = 0;
    s_face[i].ready_t_ms = 0;
  }
  s_writing = -1;
  s_in_use = -1;
  s_next_seq = 0;
  s_discontinuity = false;
  s_have_last_t = false;
  s_last_t_ms = 0;
  memset(&s_stats, 0, sizeof(s_stats));
}

// k サンプルぶんの長さ [ms]（四捨五入）
static uint32_t samples_to_ms(uint32_t k) {
  return (k * 1000u + AC_IN_HZ / 2) / AC_IN_HZ;
}

// WRITING の面を返す。無ければ FREE の面を WRITING にして返す。FREE も無ければ −1。
static int writing_face() {
  if (s_writing >= 0) return s_writing;
  for (uint32_t i = 0; i < AC_N_FACES; i++) {
    if (s_face[i].state == AC_FREE) {
      s_face[i].fill = 0;
      s_face[i].state = AC_WRITING;
      s_writing = (int)i;
      return s_writing;
    }
  }
  return -1;
}

void audio_capture_push_chunk(const int16_t* chunk, uint32_t n_bytes, uint32_t t_ms) {
  s_stats.chunks++;
  s_stats.bytes_last = n_bytes;
  bool gap = false;
  if (n_bytes != AC_CHUNK_NOMINAL_BYTES) s_stats.pdm_odd_chunks++;
  if (n_bytes == 0) gap = true;
  if (s_have_last_t && (uint32_t)(t_ms - s_last_t_ms) >= AC_PDM_GAP_MS) gap = true;
  s_have_last_t = true;
  s_last_t_ms = t_ms;
  if (gap) {
    s_stats.pdm_gaps++;
    if (s_writing >= 0 && s_face[s_writing].fill > 0) {
      // WRITING の面に途中まで入っているサンプルは捨てる（面は WRITING のまま。t0_ms と連番は次のチャンクで取り直す）。
      // 捨てたスライスの連番は届かないので、それが連番の飛びになる（次は 1 つ飛ばした連番で始まる）
      s_face[s_writing].fill = 0;
    } else {
      s_discontinuity = true;   // まだ番号を付けていないので、次に始めるスライスで 1 つ飛ばす
    }
  }

  uint32_t remaining = n_bytes / 2;
  uint32_t pos = 0;
  bool dropped = false;
  while (remaining > 0) {
    int f = writing_face();
    if (f < 0) {
      // FREE の面が無い（一方が IN_USE、もう一方が READY）。届いた分（境目で割れた後半だけのこともある）を捨てる
      dropped = true;
      s_discontinuity = true;
      break;
    }
    AudioFace& face = s_face[f];
    if (face.fill == 0) {
      // スライスの開始。先頭サンプルの時刻はチャンクの終端側の時刻から残りのサンプル数ぶん戻す
      face.t0_ms = t_ms - samples_to_ms(remaining);
      if (s_discontinuity) { s_next_seq++; s_discontinuity = false; }
      face.seq = s_next_seq++;
      s_stats.slices_started++;
    }
    uint32_t k = AC_SLICE_SAMPLES - face.fill;
    if (k > remaining) k = remaining;
    memcpy(face.buf + face.fill, chunk + pos, k * sizeof(int16_t));
    face.fill += k;
    pos += k;
    remaining -= k;
    if (face.fill == AC_SLICE_SAMPLES) {
      face.ready_t_ms = t_ms;
      face.state = AC_READY;
      s_writing = -1;
    }
  }
  if (dropped) s_stats.dropped_chunks++;
}

bool audio_capture_take(const int16_t** slice, uint32_t* t0_ms, uint32_t* seq, uint32_t* ready_t_ms) {
  if (s_in_use >= 0) return false;   // 放していない面がある
  int best = -1;
  uint32_t n_ready = 0;
  for (uint32_t i = 0; i < AC_N_FACES; i++) {
    if (s_face[i].state != AC_READY) continue;
    n_ready++;
    if (best < 0 || (int32_t)(s_face[i].seq - s_face[best].seq) < 0) best = (int)i;
  }
  if (best < 0) return false;
  if (n_ready > s_stats.ready_max) s_stats.ready_max = n_ready;
  s_stats.slices_taken++;
  AudioFace& face = s_face[best];
  face.state = AC_IN_USE;
  s_in_use = best;
  *slice = face.buf;
  *t0_ms = face.t0_ms;
  *seq = face.seq;
  if (ready_t_ms) *ready_t_ms = face.ready_t_ms;
  return true;
}

void audio_capture_release() {
  if (s_in_use < 0) return;
  s_face[s_in_use].fill = 0;
  s_face[s_in_use].state = AC_FREE;
  s_in_use = -1;
}

void audio_capture_stats(AudioCaptureStats* out) {
  *out = s_stats;
}

AudioFaceState audio_capture_face_state(uint32_t face) {
  return (AudioFaceState)s_face[face].state;
}

uint32_t audio_capture_face_fill(uint32_t face) {
  return s_face[face].fill;
}
