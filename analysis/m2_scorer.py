"""M2 の scorer: PC 上の実行ファイル `build-host/score_windows`（書き出した C++ ライブラリ ＋ `m2_norm.h`）で窓を採点する（Issue #22）。

使い方（`evaluate_detector.py --scorer analysis/m2_scorer.py`。docs/decisions/0021 の `score(features, meta)` の形）:
  python analysis/evaluate_detector.py data/raw --scorer analysis/m2_scorer.py

- `Scorer(bin_path)` は実行ファイルを起動して見出し行（`model`、`feature_set`、`n_features`、`labels`、`input_datatype`、`quantized`、
  `selfcheck`）を読み、`score_rows(rows)` で正規化前の特徴量（1 行 29 個）を標準入力に流して確率 (n, LABEL_COUNT) を受け取る。
  正規化は実行ファイルの中（`m2_norm.h` の `m2_normalize`。double で計算して float32 に落とす）で行い、この scorer は正規化しない。
- 自己検査: 見出しの `selfcheck`（実行ファイルが `SELFCHECK_INPUT` を `m2_normalize` した 29 値、`%.9g`）が、`analysis/m2_norm.json` から作った
  `features.Standardizer.transform` の同じ入力の結果とビット一致しなければ止まる（ヘッダと JSON の食い違い、演算の順序の違いをここで捕まえる）。
- `score(features, meta)` は `meta["feature_set"]`（`m2-0020`）、`meta["feature_names"]`、次元、`meta["model"]`（あれば `project_id` と
  `deploy_version` が実行ファイルの見出しと一致すること）を検査してから `swallow` の確率の列を返す。閾値との比較は呼び出し側が行う。
- 実行ファイルの場所は既定 `<repo>/build-host/score_windows`。環境変数 `M2_SCORE_BIN` で差し替えられる（テスト用）。無ければ
  `build.sh` のコマンドを示して止まる。
- 特徴量・確率をファイルに書かない。標準出力に値を出さない。
"""
from __future__ import annotations
import os, subprocess
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

import ei_upload, features_m2

REPO = Path(__file__).resolve().parent.parent
SCORE_BIN_ENV = "M2_SCORE_BIN"
DEFAULT_SCORE_BIN = REPO / "build-host" / "score_windows"
BUILD_CMD = "bash firmware/detector/host/build.sh"
NORM_PATH = ei_upload.NORM_PATH                       # 自己検査にだけ使う
FEATURE_SET = features_m2.FEATURE_SET
FEATURE_NAMES = tuple(features_m2.FEATURE_NAMES)
N_FEATURES = features_m2.N_FEATURES
SWALLOW = "swallow"
HEADER_KEYS = ("model", "feature_set", "n_features", "labels", "input_datatype", "quantized", "selfcheck")
VALUE_FMT = "%.9g"                                    # float32 が往復する桁数（実行ファイルの出力と同じ）

# 自己検査の既知の入力。firmware/detector/host/score_windows.cpp の kSelfcheckInput と同じ 29 個。
# どれも float32 で正確に表せる値（2 の冪の分数）なので、Python と C++ で同じビットになる
SELFCHECK_INPUT = np.array(
    (-1.75, 0.375, 12.5, -0.0625, 3.0, -0.5, 0.25, 7.125, -2.5, 0.125, 1.5, -0.875, 40.0, 0.03125, -6.25,
     0.75, -0.25, 2.25, -0.125, 9.5, 0.5, -3.75, 0.0, 1.0, -1.0, 0.1875, 5.5, -0.4375, 100.0), dtype=np.float32)
assert SELFCHECK_INPUT.shape == (N_FEATURES,)


def score_bin_path(bin_path: Path | str | None = None, environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    return Path(bin_path or environ.get(SCORE_BIN_ENV) or DEFAULT_SCORE_BIN)


def format_rows(rows: Sequence[Sequence[float]]) -> str:
    """1 行 1 窓、29 個を空白区切り `%.9g`（float32 が往復する桁数）。"""
    out = []
    for i, r in enumerate(rows):
        if len(r) != N_FEATURES:
            raise SystemExit(f"特徴量の次元数が {N_FEATURES} ではありません（{i} 行目: {len(r)}）")
        out.append(" ".join(VALUE_FMT % float(v) for v in r))
    return "\n".join(out) + ("\n" if out else "")


def expected_selfcheck(norm_path: Path = NORM_PATH) -> np.ndarray:
    """m2_norm.json の定数で SELFCHECK_INPUT を Standardizer.transform した float32 (29,)。"""
    doc = ei_upload.read_norm(Path(norm_path))
    return ei_upload.standardizer_from_norm(doc).transform(SELFCHECK_INPUT)


def _bits(x: np.ndarray) -> list[int]:
    return [int(v) for v in np.asarray(x, dtype=np.float32).view(np.uint32)]


class Scorer:
    def __init__(self, bin_path: Path | str | None = None, norm_path: Path = NORM_PATH,
                 environ: Mapping[str, str] | None = None):
        self.bin = score_bin_path(bin_path, environ)
        if not self.bin.is_file():
            raise SystemExit(f"実行ファイルがありません: {self.bin}。先に `{BUILD_CMD}` でビルドする"
                             f"（別の場所なら環境変数 {SCORE_BIN_ENV}）")
        header, rest = self._run("")
        if rest:
            raise SystemExit(f"{self.bin}: 入力が無いのに見出し以外の行があります（{len(rest)} 行）")
        self.model: tuple[int, int] = header["model"]
        self.feature_set: str = header["feature_set"]
        self.n_features: int = header["n_features"]
        self.labels: tuple[str, ...] = header["labels"]
        self.input_datatype: str = header["input_datatype"]
        self.quantized: int = header["quantized"]
        if self.feature_set != FEATURE_SET:
            raise SystemExit(f"{self.bin}: feature_set が {FEATURE_SET} ではありません: {self.feature_set!r}")
        if self.n_features != N_FEATURES:
            raise SystemExit(f"{self.bin}: n_features が {N_FEATURES} ではありません: {self.n_features}")
        if SWALLOW not in self.labels or len(set(self.labels)) != len(self.labels):
            raise SystemExit(f"{self.bin}: labels に {SWALLOW} が無いか重複しています: {self.labels}")
        self.swallow_index: int = self.labels.index(SWALLOW)
        expected = expected_selfcheck(norm_path)
        got = np.array(header["selfcheck"], dtype=np.float32)
        if len(got) != N_FEATURES or _bits(got) != _bits(expected):
            bad = [i for i in range(min(len(got), N_FEATURES)) if _bits(got[i:i + 1]) != _bits(expected[i:i + 1])]
            raise SystemExit(f"{self.bin}: selfcheck が Standardizer.transform とビット一致しません（{norm_path} と m2_norm.h の"
                             f"食い違いか、正規化の演算の順序の違い。添字 {bad if len(got) == N_FEATURES else '長さが違う'}）")

    @property
    def deploy_version(self) -> int:
        return self.model[1]

    def _run(self, stdin_text: str) -> tuple[dict, list[str]]:
        """実行ファイルを 1 回起動し、(見出しの辞書, 見出し以外の行) を返す。"""
        try:
            r = subprocess.run([str(self.bin)], input=stdin_text, capture_output=True, text=True, check=False)
        except OSError as e:
            raise SystemExit(f"{self.bin} を起動できません: {e}")
        if r.returncode != 0:
            raise SystemExit(f"{self.bin} が {r.returncode} で終わりました: {r.stderr.strip()[:500]}")
        header: dict = {}
        rest: list[str] = []
        for line in r.stdout.splitlines():
            if not line.strip():
                continue
            key, _, value = line.partition(" ")
            if key in HEADER_KEYS and key not in header and not rest:
                header[key] = value.strip()
            else:
                rest.append(line)
        missing = [k for k in HEADER_KEYS if k not in header]
        if missing:
            raise SystemExit(f"{self.bin}: 見出し行が足りません: {missing}")
        return self._parse_header(header), rest

    def _parse_header(self, h: Mapping[str, str]) -> dict:
        try:
            pid, ver = h["model"].split()
            model = (int(pid), int(ver))
            n_features = int(h["n_features"])
            quantized = int(h["quantized"])
            selfcheck = [float(v) for v in h["selfcheck"].split()]
        except ValueError as e:
            raise SystemExit(f"{self.bin}: 見出し行が読めません: {e}")
        return {"model": model, "feature_set": h["feature_set"], "n_features": n_features,
                "labels": tuple(h["labels"].split()), "input_datatype": h["input_datatype"], "quantized": quantized,
                "selfcheck": selfcheck}

    def score_rows(self, rows: Sequence[Sequence[float]]) -> np.ndarray:
        """正規化前の特徴量 (n, 29) → 確率 (n, LABEL_COUNT) float64（labels の順）。1 回の起動で全行を流す。"""
        text = format_rows(rows)
        if not rows:
            return np.zeros((0, len(self.labels)))
        header, lines = self._run(text)
        if header["model"] != self.model or tuple(header["labels"]) != self.labels:
            raise SystemExit(f"{self.bin}: 見出しが起動のたびに変わります")
        if len(lines) != len(rows):
            raise SystemExit(f"{self.bin}: 出力の行数 {len(lines)} が入力の行数 {len(rows)} と違います")
        out = np.zeros((len(rows), len(self.labels)))
        for i, line in enumerate(lines):
            vals = line.split()
            if len(vals) != len(self.labels):
                raise SystemExit(f"{self.bin}: {i} 行目の確率が {len(self.labels)} 個ではありません: {len(vals)}")
            try:
                out[i] = [float(v) for v in vals]
            except ValueError as e:
                raise SystemExit(f"{self.bin}: {i} 行目の確率が読めません: {e}")
        return out


# --- evaluate_detector.py --scorer の入口 ---

_SCORER: Scorer | None = None


def get_scorer() -> Scorer:
    global _SCORER
    if _SCORER is None:
        _SCORER = Scorer(norm_path=NORM_PATH)
    return _SCORER


def check_meta(meta: Mapping, scorer: Scorer) -> None:
    if meta.get("feature_set") != FEATURE_SET:
        raise SystemExit(f"meta.json の feature_set が {FEATURE_SET} ではありません: {meta.get('feature_set')!r}")
    names = meta.get("feature_names")
    if not isinstance(names, list) or tuple(names) != FEATURE_NAMES:
        raise SystemExit("meta.json の feature_names が features_m2.FEATURE_NAMES と違います")
    model = meta.get("model")
    if model is not None:
        if not isinstance(model, Mapping):
            raise SystemExit(f"meta.json の model が辞書ではありません: {model!r}")
        got = (model.get("project_id"), model.get("deploy_version"))
        if got != scorer.model:
            # 装置と違うライブラリで再採点しない。値は出さない（ID は log に書かない）
            raise SystemExit("meta.json の model（project_id / deploy_version）が実行ファイルの見出しと一致しません")


def score(features: Sequence[Sequence[float]], meta: Mapping) -> list[float]:
    """evaluate_detector.py --scorer の形（docs/decisions/0021）。features は正規化前の行（window_t_ms の昇順）。swallow の確率を返す。"""
    scorer = get_scorer()
    check_meta(meta, scorer)
    probs = scorer.score_rows(features)
    return [float(p) for p in probs[:, scorer.swallow_index]]


if __name__ == "__main__":
    print(__doc__)
