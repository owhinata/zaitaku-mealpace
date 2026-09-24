"""`analysis/m2_norm.json` → `firmware/detector/m2_norm.h`（Issue #22、docs/decisions/0019・0020）。

使い方:
  python analysis/m2_norm_header.py           # 生成する。出力が既にあって内容が違えば止まる（黙って上書きしない。作り直すときは人が消す）
  python analysis/m2_norm_header.py --check   # 差分の有無だけを返す（一致なら 0、違えば非 0 で理由を出す）

- 定数は `%.17g`（`m2_norm.json` の float64 がそのまま往復する桁数）。書いた後に読み戻し、`M2_NORM_MEAN` / `M2_NORM_STD` が
  JSON の値とビット一致することを確かめる。`std` に 0 か有限でない値があれば止まる。
- `m2_normalize` は double で `(x − mean) / std` を計算してから float32 に落とす（`features.Standardizer.transform` と同じ順序。
  plan #22 の決定 H1）。Arduino の型・ヘッダは含めない（`<stdint.h>` だけ。docs/decisions/0001）。
- コメントに書くのは `stats_sessions` の本数・`n_windows`・`commit` だけ（セッション名は書かない）。`m2_norm.json` は `self` の
  集計値なので、ヘッダも公開してよい。
"""
from __future__ import annotations
import argparse, math, re, struct, sys
from pathlib import Path
from typing import Mapping, Sequence

import ei_upload
from features_m2 import FEATURE_NAMES, FEATURE_SET, N_FEATURES

REPO = Path(__file__).resolve().parent.parent
NORM_PATH = ei_upload.NORM_PATH                                   # analysis/m2_norm.json
HEADER_PATH = REPO / "firmware" / "detector" / "m2_norm.h"
GUARD = "M2_NORM_H"


def _double_bits(v: float) -> bytes:
    return struct.pack("<d", float(v))


def fmt_double(v: float) -> str:
    """`%.17g`。整数に見える値には `.0` を付ける（C の double の初期化子として読みやすくするため。値は変わらない）。"""
    s = f"{float(v):.17g}"
    if re.fullmatch(r"-?\d+", s):
        s += ".0"
    return s


def check_doc(doc: Mapping) -> None:
    """ヘッダにできる形か。read_norm が見る項目に加えて、mean / std が有限で std に 0 が無いこと。"""
    for key in ("mean", "std"):
        vals = [float(v) for v in doc[key]]
        if len(vals) != N_FEATURES:
            raise SystemExit(f"{key} が {N_FEATURES} 個ではありません: {len(vals)}")
        bad = [i for i, v in enumerate(vals) if not math.isfinite(v)]
        if bad:
            raise SystemExit(f"{key} に有限でない値があります（添字 {bad}）")
    zero = [i for i, v in enumerate(doc["std"]) if float(v) == 0.0]
    if zero:
        raise SystemExit(f"std に 0 があります（添字 {zero}）。ei_upload.py は 0 を 1 にするので、この JSON は投入に使ったものと違います")


def render(doc: Mapping) -> str:
    check_doc(doc)
    names = list(doc["feature_names"])
    lines = [
        f"// firmware/detector/m2_norm.h — M2 の正規化の定数。analysis/m2_norm_header.py が analysis/m2_norm.json から生成する。手で編集しない。",
        f"// feature_set {doc['feature_set']}、stats_sessions {len(doc['stats_sessions'])} 本、valid な窓 {int(doc['n_windows'])}、"
        f"m2_norm.json の commit {doc.get('commit', 'unknown')}（docs/decisions/0019・0020）。",
        "// 値は %.17g（JSON の float64 がそのまま往復する桁数）。装置・PC・EI に投入した値が同じ float32 になるように、",
        "// m2_normalize は double で (x − mean) / std を計算してから float32 に落とす（features.Standardizer.transform と同じ順序）。",
        "// Arduino の型・ヘッダは含めない（docs/decisions/0001）。",
        f"#ifndef {GUARD}",
        f"#define {GUARD}",
        "",
        "#include <stdint.h>",
        "",
        f'#define M2_FEATURE_SET "{doc["feature_set"]}"',
        f"#define M2_N_FEATURES {N_FEATURES}",
        "",
        "static const char* const M2_FEATURE_NAMES[M2_N_FEATURES] = {",
        *(f'    "{n}",' for n in names),
        "};",
        "",
        "static const double M2_NORM_MEAN[M2_N_FEATURES] = {",
        *(f"    {fmt_double(v)},  // {n}" for v, n in zip(doc["mean"], names)),
        "};",
        "",
        "static const double M2_NORM_STD[M2_N_FEATURES] = {",
        *(f"    {fmt_double(v)},  // {n}" for v, n in zip(doc["std"], names)),
        "};",
        "",
        "// x: 正規化前の特徴量（float32、M2_FEATURE_NAMES の順）。z: 正規化後（float32）。x と z は同じ配列でもよい。",
        "static inline void m2_normalize(const float* x, float* z) {",
        "    for (int32_t i = 0; i < M2_N_FEATURES; ++i) {",
        "        z[i] = (float)(((double)x[i] - M2_NORM_MEAN[i]) / M2_NORM_STD[i]);",
        "    }",
        "}",
        "",
        f"#endif  // {GUARD}",
        "",
    ]
    return "\n".join(lines)


# --- 読み戻し ---

_ARRAY_RE = r"static const (?:double|char\* const) {name}\[M2_N_FEATURES\] = \{{(.*?)\}};"


def _array_body(text: str, name: str) -> str:
    m = re.search(_ARRAY_RE.format(name=name), text, re.S)
    if m is None:
        raise ValueError(f"{name} の配列が見つかりません")
    return m.group(1)


def parse_header(text: str) -> dict:
    """ヘッダから feature_set / n_features / feature_names / mean / std を読み戻す（値は float64）。"""
    m = re.search(r'#define M2_FEATURE_SET "([^"]*)"', text)
    n = re.search(r"#define M2_N_FEATURES (\d+)", text)
    if m is None or n is None:
        raise ValueError("M2_FEATURE_SET / M2_N_FEATURES が見つかりません")
    names = re.findall(r'"([^"]*)"', _array_body(text, "M2_FEATURE_NAMES"))

    def numbers(name: str) -> list[float]:
        body = re.sub(r"//[^\n]*", "", _array_body(text, name))          # 行末のコメント（次元の名前）を除く
        return [float(tok) for tok in body.replace("\n", " ").split(",") if tok.strip()]

    return {"feature_set": m.group(1), "n_features": int(n.group(1)), "feature_names": names,
            "mean": numbers("M2_NORM_MEAN"), "std": numbers("M2_NORM_STD")}


def differences(doc: Mapping, text: str) -> list[str]:
    """ヘッダの中身が JSON と食い違う点。空なら一致（値はビット一致、名前・識別子も同じ、本文は生成結果と同じ）。"""
    try:
        h = parse_header(text)
    except ValueError as e:
        return [f"ヘッダが読めません: {e}"]
    out = []
    if h["feature_set"] != doc["feature_set"]:
        out.append(f"feature_set が違います: ヘッダ {h['feature_set']!r}、JSON {doc['feature_set']!r}")
    if h["n_features"] != N_FEATURES or len(h["mean"]) != N_FEATURES or len(h["std"]) != N_FEATURES:
        out.append(f"次元数が {N_FEATURES} ではありません: n_features {h['n_features']}、mean {len(h['mean'])}、std {len(h['std'])}")
    if h["feature_names"] != list(doc["feature_names"]):
        out.append("feature_names が違います")
    for key in ("mean", "std"):
        bad = [i for i, (a, b) in enumerate(zip(h[key], doc[key])) if _double_bits(a) != _double_bits(b)]
        if bad:
            out.append(f"{key} がビット一致しません（添字 {bad}）")
    if not out and text != render(doc):
        out.append("値は一致しますが、本文が生成結果と違います（コメントか書式が編集されている）")
    return out


# --- 本体 ---

def run(norm_path: Path = NORM_PATH, header_path: Path = HEADER_PATH, check: bool = False) -> str:
    """生成（check=False）または照合（check=True）。戻り値は生成した本文。止まるときは SystemExit。"""
    doc = ei_upload.read_norm(Path(norm_path))
    text = render(doc)
    header_path = Path(header_path)
    if check:
        if not header_path.is_file():
            raise SystemExit(f"{header_path} がありません")
        diff = differences(doc, header_path.read_text(encoding="utf-8"))
        if diff:
            raise SystemExit(f"{header_path} が {norm_path} と一致しません:\n" + "\n".join(f"- {d}" for d in diff))
        return text
    if header_path.is_file():
        if header_path.read_text(encoding="utf-8") != text:
            raise SystemExit(f"{header_path} が既にあり、内容が違います（黙って上書きしない。作り直すときは人が消す）")
        return text
    header_path.parent.mkdir(parents=True, exist_ok=True)
    header_path.write_text(text, encoding="utf-8")
    # 読み戻してビット一致を確かめる
    diff = differences(doc, header_path.read_text(encoding="utf-8"))
    if diff:
        raise SystemExit(f"書いたヘッダが {norm_path} と一致しません（生成の不具合）:\n" + "\n".join(f"- {d}" for d in diff))
    return text


def main(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="生成せず、firmware/detector/m2_norm.h が m2_norm.json と一致するかだけを返す")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    run(check=a.check)
    doc = ei_upload.read_norm(NORM_PATH)
    print(f"{'一致' if a.check else '生成'}: {HEADER_PATH}（feature_set {doc['feature_set']}、{FEATURE_SET} の {len(FEATURE_NAMES)} 次元、"
          f"stats_sessions {len(doc['stats_sessions'])} 本、valid な窓 {int(doc['n_windows'])}、commit {doc.get('commit', 'unknown')}）")


if __name__ == "__main__":
    main()
