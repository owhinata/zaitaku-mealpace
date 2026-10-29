"""M4 の推論を装置（UNO Q の Linux 側）と PC で同じコードで行う（Issue #47）。

0038 の構成（M4-CONFIG）と重み（analysis/models/m4-0038.npz）を読み、1 窓ずつ
確率・後処理（postprocess.PostStepper）・LED の目安（led_rule.LedStepper）を
進める。装置の device/unoq/m4_infer.py はこのファイルへのリンク（0035）。

m4_compare.py から移したもの（m4_compare は同じ名前を再輸出する）:
  CONFIG_KEYS・CNN_SHAPES・N_MEL・N_LR・MODEL_FORMAT・CNN_SET・LR_SET・
  HN_WEIGHT・parse_config・config_mask・_expected_keys・_check_arrays・
  load_model・model_prob。
足したもの:
  sha256_file  ファイルの sha256（読むだけ）。
  code_sha256  配るファイル（.py・.npy・.npz）の sha256 をまとめた値（装置と
               PC で同じ計算。record.py --unoq-detector の照合）。
  Detector     1 窓ずつ進める（step）。値の経路は model_prob を窓 1 つで呼ぶ形。

依存は numpy・features_np・postprocess・led_rule と標準ライブラリだけ。ファイルを
書かない。標準出力に出さない。波形・特徴量を保存しない。
"""

from __future__ import annotations
import hashlib
from pathlib import Path
from typing import Iterable

import numpy as np

import features_np, led_rule, postprocess

HN_WEIGHT = 3.0  # hard negative の窓の損失の重み（探索しない）
CNN_SET = "mfe40"
LR_SET = "a52"
# 1 ホップ（窓の開始の間隔）。evaluate.HOP_S と同じ値
HOP_S = postprocess.HOP_S
# code_sha256 に入れる拡張子
CODE_SUFFIXES = (".py", ".npy", ".npz")

CONFIG_KEYS = (
    "set",
    "classifier",
    "seeds",
    "hard_negative",
    "post",
    "threshold",
    "enter",
    "exit",
    "cap_s",
    "refractory_s",
)
CNN_SHAPES = {
    "conv1.weight": (8, 1, 3, 3),
    "conv1.bias": (8,),
    "conv2.weight": (16, 8, 3, 3),
    "conv2.bias": (16,),
    "conv3.weight": (32, 16, 3, 3),
    "conv3.bias": (32,),
    "fc.weight": (1, 32),
    "fc.bias": (1,),
}
N_MEL = features_np.N_MEL_CNN
N_LR = 4 * features_np.N_MFCC  # 52
MODEL_FORMAT = 1


def parse_config(text: str) -> dict:
    """M4-CONFIG の値を読む。形が違えば ValueError。"""
    parts = text.split(" ")
    kv = [p.split("=", 1) for p in parts]
    if [k[0] for k in kv] != list(CONFIG_KEYS) or any(len(k) != 2 for k in kv):
        raise ValueError(f"M4-CONFIG の形が違います: {text!r}")
    c = dict(kv)
    lr = c["classifier"] == "lr"
    ok = (
        (c["set"], c["classifier"]) in ((LR_SET, "lr"), (CNN_SET, "cnn"))
        and c["seeds"] in (("-",) if lr else ("0", "0,1,2"))
        and c["hard_negative"]
        in (("none",) if lr else ("none", f"w{HN_WEIGHT:.1f}"))
        and c["post"] in ("none", "hyst")
        and c["cap_s"]
        in ("none", *(f"{v:.1f}" for v in postprocess.CAP_GRID_S))
        and c["refractory_s"] in ("none", f"{postprocess.REFRACTORY_S:.1f}")
    )
    if not ok:
        raise ValueError(f"M4-CONFIG の値が違います: {text!r}")
    out = {
        "set": c["set"],
        "kind": c["classifier"],
        "seeds": () if lr else tuple(int(s) for s in c["seeds"].split(",")),
        "hard_negative": c["hard_negative"] != "none",
        "post": c["post"],
        "threshold": None,
        "enter": None,
        "exit": None,
        "cap_s": None if c["cap_s"] == "none" else float(c["cap_s"]),
        "refractory_s": None
        if c["refractory_s"] == "none"
        else float(c["refractory_s"]),
    }
    try:
        if c["post"] == "none":
            if (
                c["enter"] != "-"
                or c["exit"] != "-"
                or out["cap_s"]
                or out["refractory_s"]
            ):
                raise ValueError
            out["threshold"] = float(c["threshold"])
        else:
            if c["threshold"] != "-":
                raise ValueError
            out["enter"], out["exit"] = float(c["enter"]), float(c["exit"])
    except ValueError:
        raise ValueError(
            f"M4-CONFIG の後処理の値が違います: {text!r}"
        ) from None
    return out


def config_mask(cfg: dict, prob, valid, t_start_s) -> np.ndarray:
    """0038 の後処理（P0 は threshold、ほかは post_mask）。"""
    if cfg["post"] == "none":
        return postprocess.threshold(prob, valid, cfg["threshold"])
    return postprocess.post_mask(
        prob,
        valid,
        t_start_s,
        cfg["enter"],
        cfg["exit"],
        cfg["cap_s"],
        cfg["refractory_s"],
    )


def _expected_keys(cfg: dict) -> dict:
    """鍵 → (形, 型)。"""
    keys = {"config": ((), "U"), "format": ((), "i")}
    if cfg["kind"] == "lr":
        keys.update(
            {
                "std_mean": ((N_LR,), "f8"),
                "std_std": ((N_LR,), "f8"),
                "coef": ((N_LR,), "f8"),
                "intercept": ((), "f8"),
            }
        )
        return keys
    for s in cfg["seeds"]:
        for k, shp in CNN_SHAPES.items():
            keys[f"s{s}/{k}"] = (shp, "f4")
        keys[f"s{s}/mean"] = ((N_MEL,), "f8")
        keys[f"s{s}/std"] = ((N_MEL,), "f8")
    return keys


def _check_arrays(a: dict) -> dict:
    cfg = parse_config(str(a["config"])) if "config" in a else None
    if cfg is None:
        raise SystemExit("重みのファイルに config がありません")
    want = _expected_keys(cfg)
    if set(a) != set(want):
        raise SystemExit(
            f"重みのファイルの鍵が違います（足りない: {sorted(set(want) - set(a))}、"
            f"余分: {sorted(set(a) - set(want))}）"
        )
    for k, (shp, kind) in want.items():
        v = a[k]
        if v.shape != shp:
            raise SystemExit(
                f"重みのファイルの {k} の形が違います: {v.shape}（{shp}）"
            )
        if kind == "U":
            ok = v.dtype.kind == "U"
        elif kind == "i":
            ok = v.dtype.kind == "i" and int(v) == MODEL_FORMAT
        else:
            ok = v.dtype == np.dtype(kind)
        if not ok:
            raise SystemExit(f"重みのファイルの {k} の型が違います: {v.dtype}")
    return cfg


def load_model(path: Path) -> dict:
    """重みのファイルを読む（allow_pickle を使わない）。鍵の過不足・形・型が違えば
    止まる。戻り値 {"config", "cfg", "arrays"}。numpy だけで読む。"""
    with np.load(path, allow_pickle=False) as z:
        a = {k: z[k] for k in z.files}
    cfg = _check_arrays(a)
    return {"config": str(a["config"]), "cfg": cfg, "arrays": a}


def model_prob(model: dict, X: np.ndarray) -> np.ndarray:
    """窓ごとの確率。CNN は X (n, 98, 40) の対数メル、LR は X (n, 52) の a52。
    種が複数なら確率の算術平均（5.2）。numpy と features_np だけで計算する。"""
    cfg, a = model["cfg"], model["arrays"]
    X = np.asarray(X)
    if cfg["kind"] == "lr":
        # 正規化の後に float32 に丸めるのは features.Standardizer.transform と同じ
        Z = ((X.astype(np.float64) - a["std_mean"]) / a["std_std"]).astype(
            np.float32
        )
        z = Z.astype(np.float64) @ a["coef"] + float(a["intercept"])
        return 1.0 / (1.0 + np.exp(-z))
    probs = []
    for s in cfg["seeds"]:
        params = {k: a[f"s{s}/{k}"] for k in CNN_SHAPES}
        mean, std = a[f"s{s}/mean"], a[f"s{s}/std"]
        p = np.zeros(len(X))
        B = 256  # cnn_score と同じ 256 窓ずつのまとめ（結果は変わらない）
        for k in range(0, len(X), B):
            Z = ((X[k : k + B] - mean) / std).astype(np.float32)
            p[k : k + B] = features_np.cnn_forward(params, Z)
        probs.append(p)
    return probs[0] if len(probs) == 1 else postprocess.ensemble_mean(probs)


def sha256_file(path) -> str:
    """ファイルの sha256（16 進）。読むだけ。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def code_sha256(paths: Iterable) -> str:
    """配るファイルのうち拡張子が .py・.npy・.npz のものを名前（パスの最後の
    要素）の順に並べ、各行「<名前> <sha256>\n」をつないだ文字列の sha256。
    装置では配った先のディレクトリの中のファイル、PC では DEPLOY_FILES の行
    （リンクは指す先の中身）を渡す。名前が重なれば ValueError。"""
    rows = {}
    for p in paths:
        p = Path(p)
        if p.suffix not in CODE_SUFFIXES:
            continue
        if p.name in rows:
            raise ValueError(f"名前が重なっています: {p.name}")
        rows[p.name] = sha256_file(p)
    text = "".join(f"{n} {rows[n]}\n" for n in sorted(rows))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Detector:
    """1 窓ずつ進める M4 の推論（plan #47 の 4 節）。

    step(seg16k, window_t_ms) -> (prob, positive, led, feat | None)
      seg16k: 16 kHz の 1 窓（16000 サンプル、/ 32768 の後の float32）。
      mfe40（feat=True のときは features_np.mfe_and_feat で FEAT の 82 次元も）
      → model_prob（B = 1。種ごとの正規化・cnn_forward・ensemble_mean）
      → PostStepper（0038 の enter・exit・cap_s・refractory_s。t = k × 0.25）
      → LedStepper（0018）。窓 k は step を呼んだ順の番号。
    """

    def __init__(
        self, model: dict, cfg: dict | None = None, feat: bool = False
    ):
        self.model = model
        self.cfg = model["cfg"] if cfg is None else cfg
        if self.cfg["kind"] != "cnn" or self.cfg["set"] != CNN_SET:
            raise ValueError("Detector は mfe40 の CNN の構成だけを受けます")
        self.feat = bool(feat)
        if self.cfg["post"] == "none":
            self.post = None
        else:
            self.post = postprocess.PostStepper(
                self.cfg["enter"],
                self.cfg["exit"],
                self.cfg["cap_s"],
                self.cfg["refractory_s"],
            )
        self.led = led_rule.LedStepper()
        self.k = 0

    def features(self, seg16k):
        """(mfe40, FEAT | None)。状態を進めない。"""
        if self.feat:
            return features_np.mfe_and_feat(seg16k)
        return features_np.mfe40(seg16k), None

    def prob(self, M) -> float:
        """1 窓の mfe40 から確率（状態を進めない。試しの推論にも使う）。"""
        return float(model_prob(self.model, np.asarray(M)[None])[0])

    def step(self, seg16k, window_t_ms: int):
        M, f = self.features(seg16k)
        p = self.prob(M)
        if self.post is None:
            pos = bool(p >= self.cfg["threshold"])
        else:
            pos = self.post.step(p, True, self.k * HOP_S)
        led = self.led.step(window_t_ms, pos)
        self.k += 1
        return p, int(pos), led, f
