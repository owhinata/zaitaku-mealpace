cmake_minimum_required(VERSION 3.16)
project(zaitaku_mealpace NONE)

# CMake はタスクランナー。中身は arduino-cli と Python（docs/decisions/0001）。
# 使い方:
#   cmake -S . -B build -DPORT=/dev/ttyACM0
#   cmake --build build --target deps     # 初回のみ
#   cmake --build build --target build
#   cmake --build build --target upload
#   SUBJECT=self COND=water DURATION=10 cmake --build build --target record
#
# record に渡す SUBJECT / COND / POSITION / BAND / DURATION は環境変数として
# tools/record.py に届く（record.py が既定値として読む）。CMake の $ENV{} は
# configure 時に評価されてしまうので、ここでは引数に展開しない。
#
# PYTHON3 には deps の pip と record が使う python を渡せる。Debian / Ubuntu の
# システム Python は pip install を拒む（PEP 668, externally-managed-environment）ので、
# その場合は venv を作って渡す:
#   python3 -m venv .venv
#   cmake -S . -B build -DPORT=/dev/ttyACM0 -DPYTHON3=$PWD/.venv/bin/python3
#
# SKETCH_NAME でビルドするスケッチ（firmware/<SKETCH_NAME>/）を選ぶ。既定は logger。
# BUILD_FLAGS は arduino-cli の build.extra_flags に渡る（C・C++・リンクの全部に付く）。既定は空。
# 計測用スケッチ（firmware/bench/、#17）は案ごとに別のビルドディレクトリで:
#   cmake -S . -B build-bench2 -DSKETCH_NAME=bench -DBUILD_FLAGS="-DBENCH_CASE=2"
#   cmake --build build-bench2 --target build
#   案1 は -DBUILD_FLAGS="-DBENCH_CASE=1 -I$PWD/firmware/bench/src -DEI_PORTING_MBED=0"
#   案1 の書き出し（ダミー）は firmware/bench/src/ に展開したときだけ置く。他の案をビルドするときは消す（arduino-cli が src/ を全部コンパイルするため。#22）
# 検出器（firmware/detector/、#24）は logger と別のビルドディレクトリで。SKETCH_NAME=detector のときは基底フラグ
# （DETECTOR_BASE_FLAGS: 書き出しの include、Arduino porting だけにする、0020 の式、PC と同じカーネル、SDK の出力はエラーだけ）
# が自動で付き、BUILD_FLAGS はその後ろに足される（追加分だけ渡す）:
#   cmake -S . -B build-detector -DSKETCH_NAME=detector -DPORT=/dev/ttyACM0 -DPYTHON3=$PWD/.venv/bin/python3
#   cmake --build build-detector --target build && cmake --build build-detector --target upload
#   計測: -DBUILD_FLAGS="-DDETECTOR_PROFILE=1"、FEAT を送らない: -DBUILD_FLAGS="-DDETECTOR_SEND_FEAT=0"
#   logger に戻す: build（SKETCH_NAME=logger）で build → upload

set(FQBN "arduino:mbed_nano:nanorp2040connect" CACHE STRING "Arduino FQBN")
set(PORT "/dev/ttyACM0" CACHE STRING "Serial port")
set(SKETCH_NAME "logger" CACHE STRING "firmware/ の下のスケッチ名")
set(BUILD_FLAGS "" CACHE STRING "arduino-cli の build.extra_flags に渡す追加フラグ")
set(SKETCH "${CMAKE_SOURCE_DIR}/firmware/${SKETCH_NAME}")
set(BUILD_OUT "${CMAKE_BINARY_DIR}/firmware/${SKETCH_NAME}")

# 検出器の基底フラグ（plan #24 第 11 節）。他のスケッチは BUILD_FLAGS だけ（今までと同じ）
if(SKETCH_NAME STREQUAL "detector")
  set(DETECTOR_BASE_FLAGS "-I${CMAKE_SOURCE_DIR}/firmware/detector/src -DEI_PORTING_MBED=0 -DAF_PROFILE=1 -DEI_CLASSIFIER_TFLITE_ENABLE_CMSIS_NN=0 -DEI_LOG_LEVEL=1")
  set(EXTRA_FLAGS "${DETECTOR_BASE_FLAGS} ${BUILD_FLAGS}")
else()
  set(EXTRA_FLAGS "${BUILD_FLAGS}")
endif()

find_program(ARDUINO_CLI arduino-cli REQUIRED)
find_program(PYTHON3 python3 REQUIRED)

add_custom_target(deps
  COMMAND ${ARDUINO_CLI} core update-index
  COMMAND ${ARDUINO_CLI} core install arduino:mbed_nano
  COMMAND ${ARDUINO_CLI} lib install Arduino_LSM6DSOX
  COMMAND ${PYTHON3} -m pip install -r ${CMAKE_SOURCE_DIR}/tools/requirements.txt
  COMMENT "ボードコアとライブラリ、Python 依存の導入")

add_custom_target(build
  COMMAND ${ARDUINO_CLI} compile --fqbn ${FQBN}
          --build-property "build.extra_flags=${EXTRA_FLAGS}"
          --output-dir ${BUILD_OUT} ${SKETCH}
  COMMENT "ファームウェアのビルド")

add_custom_target(upload
  COMMAND ${ARDUINO_CLI} upload --fqbn ${FQBN} --port ${PORT} --input-dir ${BUILD_OUT} ${SKETCH}
  COMMENT "書き込み")

# baudrate は logger.ino の Serial.begin(2000000) と record.py の既定に揃えてある。
# USB CDC では値が無視される（#2 で 115200 と 2000000 を実測し、受信バイト数も
# フレーム数も同じだった）。揃えてあるのは読む人が迷わないようにするため。
add_custom_target(monitor
  COMMAND ${ARDUINO_CLI} monitor --port ${PORT} --config baudrate=2000000
  COMMENT "シリアルモニタ（デバッグ用。記録は record を使う）")

# record は環境変数 SUBJECT / COND / POSITION / BAND / DURATION を受け取る。
# 値は record.py が環境変数から読むので、ここでは引数に展開しない。
add_custom_target(record
  COMMAND ${CMAKE_COMMAND} -E env PORT=${PORT}
          ${PYTHON3} ${CMAKE_SOURCE_DIR}/tools/record.py
          --port ${PORT}
  COMMENT "記録（PC 側）。SUBJECT=self COND=water POSITION=midline-below-thyroid DURATION=10")
