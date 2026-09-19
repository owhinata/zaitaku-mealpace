// zaitaku-mealpace 記録ファームウェア（Nano RP2040 Connect）
// IMU 104 Hz と PDM マイク 16 kHz を、同じ millis() で刻印して USB シリアルに送る。
// フレーム形式は docs/data-schema.md。Arduino のライブラリ API はこのファイルの
// sensors_* 関数の内側だけで呼ぶ（docs/decisions/0001）。
//
// 未検証: 実機でのビルドと帯域は M0 で確認する。

#include <Arduino_LSM6DSOX.h>
#include <PDM.h>
#include "frame.h"

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

void setup() {
  Serial.begin(2000000);
  while (!Serial) {}
  if (!sensors_begin()) {
    while (true) { digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN)); delay(200); }
  }
  const char* meta = "{\"fw\":\"logger\",\"imu_hz\":104,\"audio_hz\":16000}";
  frame_send(Serial, FRAME_META, millis(), (const uint8_t*)meta, strlen(meta));
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
}
