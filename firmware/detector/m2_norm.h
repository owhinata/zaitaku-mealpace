// firmware/detector/m2_norm.h — M2 の正規化の定数。analysis/m2_norm_header.py が analysis/m2_norm.json から生成する。手で編集しない。
// feature_set m2-0020、stats_sessions 21 本、valid な窓 15030、m2_norm.json の commit c2aba0d（docs/decisions/0019・0020）。
// 値は %.17g（JSON の float64 がそのまま往復する桁数）。装置・PC・EI に投入した値が同じ float32 になるように、
// m2_normalize は double で (x − mean) / std を計算してから float32 に落とす（features.Standardizer.transform と同じ順序）。
// Arduino の型・ヘッダは含めない（docs/decisions/0001）。
#ifndef M2_NORM_H
#define M2_NORM_H

#include <stdint.h>

#define M2_FEATURE_SET "m2-0020"
#define M2_N_FEATURES 29

static const char* const M2_FEATURE_NAMES[M2_N_FEATURES] = {
    "acc_ptp_x",
    "acc_ptp_y",
    "acc_ptp_z",
    "gyro_norm_ptp",
    "acc_rms_x",
    "acc_rms_y",
    "acc_rms_z",
    "gyro_rms_x",
    "gyro_rms_y",
    "gyro_rms_z",
    "acc_peak_count",
    "acc_axis_x",
    "acc_axis_y",
    "acc_axis_z",
    "mfcc_0",
    "mfcc_1",
    "mfcc_2",
    "mfcc_3",
    "mfcc_4",
    "mfcc_5",
    "mfcc_6",
    "mfcc_7",
    "mfcc_8",
    "mfcc_9",
    "mfcc_10",
    "mfcc_11",
    "mfcc_12",
    "spectral_centroid_hz",
    "zero_crossing_rate",
};

static const double M2_NORM_MEAN[M2_N_FEATURES] = {
    0.061965921504642377,  // acc_ptp_x
    0.050539707278192084,  // acc_ptp_y
    0.052569367972847648,  // acc_ptp_z
    9.7891973931473721,  // gyro_norm_ptp
    0.01228164990740213,  // acc_rms_x
    0.0098392448373091201,  // acc_rms_y
    0.011864959495289268,  // acc_rms_z
    2.7334189963148421,  // gyro_rms_x
    2.5974154240981546,  // gyro_rms_y
    1.9946945549118384,  // gyro_rms_z
    6.2252162341982702,  // acc_peak_count
    0.47058603946760347,  // acc_axis_x
    0.42448771987803846,  // acc_axis_y
    -0.21760728180590935,  // acc_axis_z
    -81.039834671375829,  // mfcc_0
    -2.6122612274689674,  // mfcc_1
    -1.2532468701863175,  // mfcc_2
    -0.060331483535373528,  // mfcc_3
    -0.090519535434617646,  // mfcc_4
    0.59616789324726494,  // mfcc_5
    0.60367100351423764,  // mfcc_6
    0.28422919937670049,  // mfcc_7
    0.42108882812976511,  // mfcc_8
    0.060034398206151356,  // mfcc_9
    0.39613099549583636,  // mfcc_10
    0.41679598907455412,  // mfcc_11
    0.17318352365135189,  // mfcc_12
    1560.9032129405739,  // spectral_centroid_hz
    0.056335210567092397,  // zero_crossing_rate
};

static const double M2_NORM_STD[M2_N_FEATURES] = {
    0.061253342812678982,  // acc_ptp_x
    0.052804184437495794,  // acc_ptp_y
    0.065906949519302607,  // acc_ptp_z
    18.946369136462689,  // gyro_norm_ptp
    0.015137697634094789,  // acc_rms_x
    0.011648099941512058,  // acc_rms_y
    0.016826749024013984,  // acc_rms_z
    5.1061141272726793,  // gyro_rms_x
    5.8376458533011943,  // gyro_rms_y
    3.4509693212266712,  // gyro_rms_z
    1.8232373627298577,  // acc_peak_count
    0.53295540525865226,  // acc_axis_x
    0.20637508939117774,  // acc_axis_y
    0.47368120551083531,  // acc_axis_z
    6.8763657539106227,  // mfcc_0
    1.6125386989009627,  // mfcc_1
    0.91807118284238487,  // mfcc_2
    0.56134025327761472,  // mfcc_3
    0.58516215244148606,  // mfcc_4
    0.52440440889426243,  // mfcc_5
    0.44584853851807016,  // mfcc_6
    0.28977098942780666,  // mfcc_7
    0.30978365327437291,  // mfcc_8
    0.26264233993568997,  // mfcc_9
    0.26931026078140308,  // mfcc_10
    0.23869445801016601,  // mfcc_11
    0.2368143808371303,  // mfcc_12
    225.22633684976952,  // spectral_centroid_hz
    0.02514633357357187,  // zero_crossing_rate
};

// x: 正規化前の特徴量（float32、M2_FEATURE_NAMES の順）。z: 正規化後（float32）。x と z は同じ配列でもよい。
static inline void m2_normalize(const float* x, float* z) {
    for (int32_t i = 0; i < M2_N_FEATURES; ++i) {
        z[i] = (float)(((double)x[i] - M2_NORM_MEAN[i]) / M2_NORM_STD[i]);
    }
}

#endif  // M2_NORM_H
