// matrix_frame.cpp — フレームの組み立てと状態の範囲の検査（matrix_frame.h）。
// Arduino に依存しない。
#include "matrix_frame.h"
#include "shapes.h"

static const int FRAME_COUNT = (int)(sizeof(FRAMES) / sizeof(FRAMES[0]));
static const int SHAPE_ROWS = (int)(sizeof(FRAMES[0]) / sizeof(FRAMES[0][0]));
static const int SHAPE_COLS = (int)(sizeof(FRAMES[0][0]));

static_assert(SHAPE_ROWS == MATRIX_ROWS, "図形の行数がマトリクスと違う");
static_assert(SHAPE_COLS < MATRIX_COLS, "図形の列数がマトリクスに収まらない");

bool unoq_state_from_rpc(int v, uint8_t* out) {
  if (v >= 0 && v <= 2) {
    *out = (uint8_t)v;
    return true;
  }
  *out = 0;
  return false;
}

void matrix_frame_compose(int frame, uint8_t out[MATRIX_ROWS][MATRIX_COLS]) {
  bool valid = frame >= 0 && frame < FRAME_COUNT;
  for (int y = 0; y < MATRIX_ROWS; ++y)
    for (int x = 0; x < MATRIX_COLS; ++x)
      out[y][x] = (valid && x < SHAPE_COLS)
                      ? FRAMES[frame][MATRIX_ROWS - 1 - y][x]
                      : (uint8_t)0;
}

void matrix_view_init(MatrixView* v) { v->shown = MATRIX_VIEW_NONE; }

bool matrix_view_update(MatrixView* v, int frame) {
  if (frame == v->shown) return false;
  v->shown = frame;
  return true;
}
