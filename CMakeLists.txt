cmake_minimum_required(VERSION 3.16)
project(zaitaku_mealpace NONE)

# CMake はタスクランナー。中身は arduino-cli と Python（docs/decisions/0001）。
# 使い方:
#   cmake -S . -B build -DPORT=/dev/ttyACM0
#   cmake --build build --target build
#   cmake --build build --target upload
#   cmake --build build --target record -- SUBJECT=self COND=water

set(FQBN "arduino:mbed_nano:nanorp2040connect" CACHE STRING "Arduino FQBN")
set(PORT "/dev/ttyACM0" CACHE STRING "Serial port")
set(SKETCH "${CMAKE_SOURCE_DIR}/firmware/logger")
set(BUILD_OUT "${CMAKE_BINARY_DIR}/firmware/logger")

find_program(ARDUINO_CLI arduino-cli REQUIRED)
find_program(PYTHON3 python3 REQUIRED)

add_custom_target(deps
  COMMAND ${ARDUINO_CLI} core update-index
  COMMAND ${ARDUINO_CLI} core install arduino:mbed_nano
  COMMAND ${ARDUINO_CLI} lib install Arduino_LSM6DSOX
  COMMAND ${PYTHON3} -m pip install -r ${CMAKE_SOURCE_DIR}/tools/requirements.txt
  COMMENT "ボードコアとライブラリ、Python 依存の導入")

add_custom_target(build
  COMMAND ${ARDUINO_CLI} compile --fqbn ${FQBN} --output-dir ${BUILD_OUT} ${SKETCH}
  COMMENT "ファームウェアのビルド")

add_custom_target(upload
  COMMAND ${ARDUINO_CLI} upload --fqbn ${FQBN} --port ${PORT} --input-dir ${BUILD_OUT} ${SKETCH}
  COMMENT "書き込み")

add_custom_target(monitor
  COMMAND ${ARDUINO_CLI} monitor --port ${PORT} --config baudrate=115200
  COMMENT "シリアルモニタ（デバッグ用。記録は record を使う）")

# record は環境変数 SUBJECT / COND / POSITION を受け取る
add_custom_target(record
  COMMAND ${CMAKE_COMMAND} -E env PORT=${PORT}
          ${PYTHON3} ${CMAKE_SOURCE_DIR}/tools/record.py
          --port ${PORT} --out ${CMAKE_SOURCE_DIR}/data/raw
          --subject $ENV{SUBJECT} --cond $ENV{COND} --position $ENV{POSITION}
  COMMENT "記録（PC 側）。SUBJECT=self COND=water POSITION=midline-below-thyroid")
