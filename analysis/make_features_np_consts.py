"""features_np.py が読む定数を PC で作る（Issue #44。librosa・scipy を使う。
装置には配らない）。

作るもの（どれも音声ではない）:
  analysis/mel40_htk_0_4000.npy  メル 40 本（0〜4000 Hz、HTK、面積の正規化なし）
                                 (40, 257) float64。librosa.filters.mel の値。
  analysis/mel26_htk_0_4000.npy  メル 26 本（同じ。a52 用）(26, 257) float64。
  analysis/features_np_ref.npz   照合用の参照: 間引きのタップ（scipy の firwin、
                                 61）、決定的な合成入力（features_np.reference_*。
                                 入力の信号そのものは入れない）に対する librosa 版
                                 の mfe40 (98, 40)・a52 (52) と、決定的な重みでの
                                 cnn_forward の出力 (1,)。

使い方: python analysis/make_features_np_consts.py [--write]
既定は作り直した値と既にあるファイルを比べるだけ（違えば 1 で止まる）。--write の
ときだけ上の 3 つのパスに書く。features_v2 は import の時点でメル行列の .npy を
読むので、.npy が消えたときは git から戻す（生成物はコミット済み）。
"""

from __future__ import annotations
import argparse, sys

import numpy as np

import features_v2  # librosa のキャッシュの確かめを通してから librosa を読む
import features_np as fnp

features_v2.check_librosa_cache()
import librosa  # noqa: E402
import scipy.fft  # noqa: E402
import scipy.signal  # noqa: E402

MEL_SPECS = {fnp.MEL40_PATH: fnp.N_MEL_CNN, fnp.MEL26_PATH: fnp.N_MEL_A}
REF_KEYS = ("taps", "mfe40", "a52", "cnn")


def librosa_mel(n_mels: int) -> np.ndarray:
    return librosa.filters.mel(
        sr=fnp.SR,
        n_fft=fnp.N_FFT,
        n_mels=n_mels,
        fmin=0.0,
        fmax=fnp.A_FMAX_HZ,
        htk=True,
        norm=None,
        dtype=np.float64,
    )


# --- 参照の式（変更前の features_v2 の mfe_one・a52_segment。librosa・scipy） ---


def _ref_logmel(x: np.ndarray, mel_fb: np.ndarray) -> np.ndarray:
    xp = np.append(x[0], x[1:] - fnp.PREEMPHASIS * x[:-1])
    fr = librosa.util.frame(
        xp, frame_length=fnp.FRAME_LEN, hop_length=fnp.FRAME_HOP, axis=0
    )
    spec = np.fft.rfft(fr * np.hamming(fnp.FRAME_LEN), n=fnp.N_FFT, axis=1)
    p = (spec.real**2 + spec.imag**2) / fnp.N_FFT
    return np.log(p @ mel_fb.T + fnp.LOG_FLOOR)


def ref_mfe40(x: np.ndarray, mel40: np.ndarray) -> np.ndarray:
    x = np.asarray(x[: fnp.WINDOW_SAMPLES], dtype=np.float64)
    return _ref_logmel(x, mel40).astype(np.float32)


def ref_a52(x: np.ndarray, mel26: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    C = scipy.fft.dct(_ref_logmel(x, mel26), type=2, norm="ortho", axis=1)
    C = C[:, : fnp.N_MFCC]
    features_v2.check_librosa_cache()
    d = librosa.feature.delta(
        C.T, width=fnp.DELTA_WIDTH, order=1, mode="interp", axis=-1
    )
    return np.concatenate(
        [C.mean(axis=0), C.max(axis=0), C.std(axis=0), np.abs(d).mean(axis=1)]
    )


def build() -> tuple[dict, dict]:
    """(パス → メル行列, 参照の dict)。ファイルは書かない。"""
    mels = {p: librosa_mel(n) for p, n in MEL_SPECS.items()}
    mel40, mel26 = mels[fnp.MEL40_PATH], mels[fnp.MEL26_PATH]
    x = fnp.reference_signal()
    params, M = fnp.reference_cnn()
    ref = {
        "taps": scipy.signal.firwin(
            fnp.DECIMATE_TAPS,
            1.0 / fnp.DECIMATE,
            window=("kaiser", fnp.DECIMATE_BETA),
        ),
        "mfe40": ref_mfe40(x, mel40),
        "a52": ref_a52(x, mel26),
        "cnn": np.asarray(fnp.cnn_forward(params, M), dtype=np.float64),
    }
    return mels, ref


def differences(mels: dict, ref: dict) -> list[str]:
    """既にあるファイルと作り直した値の違い（無ければ空）。"""
    out = []
    for p, m in mels.items():
        if not p.exists():
            out.append(f"{p.name} が無い")
        elif not np.array_equal(np.load(p, allow_pickle=False), m):
            out.append(f"{p.name} が作り直した値と違う")
    if not fnp.REF_PATH.exists():
        return out + [f"{fnp.REF_PATH.name} が無い"]
    with np.load(fnp.REF_PATH, allow_pickle=False) as z:
        if sorted(z.files) != sorted(REF_KEYS):
            return out + [f"{fnp.REF_PATH.name} の項目が違う: {z.files}"]
        for k in REF_KEYS:
            if z[k].shape != ref[k].shape or not np.allclose(
                z[k], ref[k], rtol=1e-12, atol=1e-12
            ):
                out.append(f"{fnp.REF_PATH.name} の {k} が作り直した値と違う")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--write", action="store_true", help="3 つのパスに書く")
    a = ap.parse_args(argv)
    mels, ref = build()
    if a.write:
        for p, m in mels.items():
            np.save(p, m, allow_pickle=False)
        np.savez(fnp.REF_PATH, **ref)
        print("書いた: " + "、".join(p.name for p in [*mels, fnp.REF_PATH]))
        return 0
    diff = differences(mels, ref)
    if diff:
        print("違う（--write で作り直す）: " + "、".join(diff))
        return 1
    print("一致: " + "、".join(p.name for p in [*mels, fnp.REF_PATH]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
