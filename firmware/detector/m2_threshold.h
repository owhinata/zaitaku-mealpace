// firmware/detector/m2_threshold.h — M2 の凍結した閾値。analysis/m2_threshold.py --freeze が生成する。手で編集しない。
// 決めた日 2026-09-24、コミット db4aa7f、EI の deploy version 2。
// 収集日4（検証側）で決めた。#27 の記録が終わるまで、この閾値もモデルも変えない（Issue #22、docs/decisions/0019）。
// 決め方: 候補 0.05〜0.95（0.05 刻み）。誤検出率が 1.0 回/分以下の候補のうち、検出率が最大（同率なら高いほう）。
// float32 では 0.949999988（%.9g）。装置は float32 の確率と float32 のこの値を比べる。META の threshold にはこの 9 桁の値を書く（docs/decisions/0021）。
#ifndef M2_THRESHOLD_H
#define M2_THRESHOLD_H

static const float M2_THRESHOLD = 0.95f;
#define M2_THRESHOLD_FP_PER_MIN_LIMIT 1.0

#endif  // M2_THRESHOLD_H
