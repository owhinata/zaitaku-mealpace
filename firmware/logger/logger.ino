// zaitaku-mealpace 記録ファームウェア（Nano RP2040 Connect）
// IMU 104 Hz と PDM マイク 16 kHz を、同じ millis() で刻印して USB シリアルに送る。
// フレーム形式は docs/data-schema.md。Arduino のライブラリ API はこのファイルの
// sensors_* 関数の内側だけで呼ぶ（docs/decisions/0001）。
//
// 未検証: 実機でのビルドと帯域は M0 で確認する。

#include <Arduino_LSM6DSOX.h>
#include <PDM.h>
#include "frame.h"

#ifndef LOGGER_ANALOG
#define LOGGER_ANALOG 0                           // 1 で A0 の 2 kHz（圧電の試用、#35・docs/decisions/0022）を足す
#endif
#if LOGGER_ANALOG
#include "analog_in.h"
#endif

static const int AUDIO_HZ = 16000;
static const int AUDIO_CHUNK = 256;               // サンプル/チャンク（512 バイト）
static int16_t pdm_buf[AUDIO_CHUNK * 4];          // PDM コールバック用リング
static volatile int pdm_available = 0;
static uint32_t chunk_t_ms = 0;

static void on_pdm() {
  int n = PDM.available();
  if (n > (int)sizeof(pdm_buf)) n = sizeof(pdm_buf);
  PDM.read(pdm_buf, n);
  pdm_available = n / 2;
  chunk_t_ms = millis();
}

static bool sensors_begin() {
  if (!IMU.begin()) return false;                 // LSM6DSOX: 104 Hz, ±4 g, ±2000 dps（ライブラリ既定）
  PDM.onReceive(on_pdm);
  PDM.setBufferSize(AUDIO_CHUNK * 2);
  if (!PDM.begin(1, AUDIO_HZ)) return false;
  return true;
}

static bool sensors_read_imu(float v[6]) {
  if (!IMU.accelerationAvailable() || !IMU.gyroscopeAvailable()) return false;
  IMU.readAcceleration(v[0], v[1], v[2]);         // g
  IMU.readGyroscope(v[3], v[4], v[5]);            // deg/s
  return true;
}

#if LOGGER_ANALOG
// META（docs/decisions/0022）。固定部分は定数、計数だけ数字。sample_rates・sensors は record.py の既定を丸ごと置き換えるので全項目を書く。
static const char META_FMT[] =
  "{\"fw\":\"logger\",\"imu_hz\":104,\"audio_hz\":16000,\"analog_hz\":%u,\"analog_n\":%u,\"analog_channels\":1,"
  "\"sample_rates\":{\"imu_hz\":104,\"audio_hz\":16000,\"analog_hz\":%u},"
  "\"sensors\":[{\"id\":\"imu\",\"part\":\"LSM6DSOX\",\"iface\":\"onboard\"},"
  "{\"id\":\"mic\",\"part\":\"MP34DT06JTR\",\"iface\":\"onboard-pdm\"},"
  "{\"id\":\"piezo\",\"part\":\"DFR0052\",\"iface\":\"A0-direct\"}],"
  "\"analog_ticks\":%lu,\"analog_reads\":%lu,\"analog_blocks\":%lu,\"analog_dropped\":%lu,"
  "\"analog_lag_max_ms\":%lu,\"analog_stats_t_ms\":%lu}";
static const uint32_t META_RESEND_MS = 1000;
static char meta_buf[512];
static uint32_t meta_last_ms = 0;

static void send_meta_analog() {
  analog_in_stats_t st;
  analog_in_stats(&st);
  int n = snprintf(meta_buf, sizeof(meta_buf), META_FMT,
                   (unsigned)ANALOG_HZ, (unsigned)ANALOG_N, (unsigned)ANALOG_HZ,
                   (unsigned long)st.ticks, (unsigned long)st.reads, (unsigned long)st.blocks,
                   (unsigned long)st.dropped, (unsigned long)st.lag_max_ms, (unsigned long)st.t_ms);
  if (n <= 0 || n >= (int)sizeof(meta_buf)) return;   // 収まらなければ送らない
  frame_send(Serial, FRAME_META, millis(), (const uint8_t*)meta_buf, (uint16_t)n);
}
#endif

void setup() {
  Serial.begin(2000000);
  while (!Serial) {}
  if (!sensors_begin()) {
    while (true) { digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN)); delay(200); }
  }
#if LOGGER_ANALOG
  if (!analog_in_begin()) {
    while (true) { digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN)); delay(200); }
  }
  send_meta_analog();
  meta_last_ms = millis();
#else
  const char* meta = "{\"fw\":\"logger\",\"imu_hz\":104,\"audio_hz\":16000}";
  frame_send(Serial, FRAME_META, millis(), (const uint8_t*)meta, strlen(meta));
#endif
}

void loop() {
  float imu[6];
  if (sensors_read_imu(imu)) {
    frame_send(Serial, FRAME_IMU, millis(), (const uint8_t*)imu, sizeof(imu));
  }
  if (pdm_available) {
    int n = pdm_available; pdm_available = 0;
    frame_send(Serial, FRAME_AUDIO, chunk_t_ms, (const uint8_t*)pdm_buf, n * 2);
  }
#if LOGGER_ANALOG
  uint16_t abuf[ANALOG_N];
  uint32_t at;
  while (analog_in_pop(abuf, &at)) {
    frame_send(Serial, FRAME_ANALOG, at, (const uint8_t*)abuf, (uint16_t)(ANALOG_N * 2));
  }
  if (millis() - meta_last_ms >= META_RESEND_MS) {
    meta_last_ms = millis();
    send_meta_analog();
  }
#endif
}
