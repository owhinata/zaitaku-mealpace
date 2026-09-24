// 検出器の音声の取り込み（Issue #24、plan 第 6 節 (a)・(c)）。Arduino 依存なし。
//
// PDM のコールバック（割り込み）が 64 サンプルずつ届ける 16 kHz のチャンクを、4000 サンプル（0.25 秒）のスライスの
// 2 面バッファに積む。面の状態は FREE / WRITING / READY / IN_USE で、
//   コールバックが動かすのは FREE → WRITING（スライスの開始）と WRITING → READY（4000 サンプルで満ちた）だけ、
//   主スレッドが動かすのは READY → IN_USE（audio_capture_take）と IN_USE → FREE（audio_capture_release）だけ。
// 同じ面を両側が同時に動かすことは無い。
//
// 16 kHz の波形はこの 2 面（最長 0.5 秒ぶん）にしか無く、次のスライスで上書きされる。面はフレームの送信関数に渡らない
// （pipeline.cpp が audio_reuse_push に渡し、間引いて 8 kHz のリングへ入れるだけ。docs/decisions/0005・0020）。
//
// 欠落の扱い:
//   - FREE の面が無い（一方が IN_USE、もう一方が READY。主スレッドが 1 ホップ分以上遅れた）ときに届いたチャンクは捨てて
//     dropped_chunks を数え、不連続とする。IN_USE の面も READY の面も上書きしない。
//   - n_bytes が 128（64 サンプル）でないコールバックは pdm_odd_chunks に数える（積むのは実際の量だけ）。
//   - n_bytes == 0、または前のコールバックとの millis() の差が AC_PDM_GAP_MS（6 ms = 1.5 × 4 ms。tools/check_session.py と
//     docs/decisions/0013 の音声の飛びの基準と同じ）以上なら pdm_gaps を数えて不連続とする。そのとき WRITING の面に途中まで
//     入っているサンプルは捨て（fill = 0。面は WRITING のまま）、t0_ms を次に届いたチャンクで取り直す。捨てたスライスの
//     連番は届かないので、それが連番の飛びになる（次のスライスは 1 つ飛ばした連番で始まる）。
//   - まだ番号を付けていない状態で不連続になったとき（チャンクを捨てた、面が空のときの飛び）は、次に始めるスライスの連番を
//     1 つ余分に進める（連番の飛び）。pipeline.cpp が連番の飛びを見て audio_reuse_init に戻す（連続でない音声で窓を作らない。
//     docs/decisions/0012 の「飛びと交わる窓は無効」と同じ向き）。
//
// スライスの先頭サンプルの時刻 t0_ms（plan 第 6 節 (d)、docs/decisions/0013 の換算）:
//   t0_ms = chunk_t_ms − round(k × 1000 ÷ 16000)。k はそのチャンクのうち面の先頭サンプル以降のサンプル数
//   （64 サンプルのチャンクの先頭から始まれば −4 ms、後半 32 サンプルから始まれば −2 ms）。
#pragma once
#include <stdint.h>
#include <stdbool.h>

static const uint32_t AC_IN_HZ = 16000;
static const uint32_t AC_SLICE_SAMPLES = 4000;          // 0.25 秒（audio_features.h の AF_IN_HOP_SAMPLES と同じ）
static const uint32_t AC_SLICE_MS = 250;
static const uint32_t AC_N_FACES = 2;
static const uint32_t AC_CHUNK_NOMINAL_BYTES = 128;     // 64 サンプル × 2 B（PDM.cpp の 1 回の DMA 割り込みぶん）
static const uint32_t AC_PDM_GAP_MS = 6;                // 1.5 × 4 ms

enum AudioFaceState : uint8_t { AC_FREE = 0, AC_WRITING = 1, AC_READY = 2, AC_IN_USE = 3 };

struct AudioCaptureStats {
  uint32_t dropped_chunks;   // FREE の面が無くて捨てたチャンク（後半だけ捨てた場合も 1）
  uint32_t pdm_odd_chunks;   // n_bytes が 128 でなかったコールバック
  uint32_t pdm_gaps;         // n_bytes == 0、または millis() の差が 6 ms 以上
  uint32_t slices_started;   // 始めたスライスの数
  uint32_t chunks;           // コールバックの数
};

void audio_capture_init();

// PDM のコールバックから呼ぶ（割り込みの中）。chunk は n_bytes ÷ 2 サンプルの int16、t_ms はコールバックで取った millis()
// （docs/decisions/0013 のとおりチャンクの終端側の時刻）。
void audio_capture_push_chunk(const int16_t* chunk, uint32_t n_bytes, uint32_t t_ms);

// 主スレッド。READY の面があれば連番の若い順に 1 つ IN_USE にして真を返す。無ければ偽。
// ready_t_ms は面が READY になったコールバックの t_ms（計測の wall 用）。
bool audio_capture_take(const int16_t** slice, uint32_t* t0_ms, uint32_t* seq, uint32_t* ready_t_ms);
// 主スレッド。IN_USE の面を FREE に戻す（次の書き込みで上書きされる）。
void audio_capture_release();

void audio_capture_stats(AudioCaptureStats* out);
// テスト用: 面の状態と連番
AudioFaceState audio_capture_face_state(uint32_t face);
uint32_t audio_capture_face_fill(uint32_t face);
