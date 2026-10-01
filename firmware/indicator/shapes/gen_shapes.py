#!/usr/bin/env python3
"""表示器の図形の元（ASCII）から firmware/indicator/shapes.h を作る（Issue #32、docs/decisions/0018 の追記）。

使い方（リポジトリの root から）:
  python3 firmware/indicator/shapes/gen_shapes.py           # shapes.h を作り直す
  python3 firmware/indicator/shapes/gen_shapes.py --check   # shapes.h が元と一致するかを見る（書かない。違えば差を出して終了コード 1）

図形は人が実機で見比べて決めた（10/1、候補 Q3）。変えるときはこの元を直して作り直し、0018 に追記してから。
  状態 1（嚥下をまだ確認していない目安）: 波線がゆっくり左へ流れる動画（行 3〜5、1 列ずつ 200 ms × 6 フレーム、周期 1.2 秒）。
  状態 2（嚥下を確認した目安）: 微笑む顔（静止画。目と口だけで輪郭は描かない）。
  状態 0: 消灯（表は持たない）。
元は 8 行 × 12 桁、'#' = 点灯、'.' = 消灯。標準ライブラリだけ。
"""
from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

ROWS, COLS = 8, 12
OUT = Path(__file__).resolve().parent.parent / "shapes.h"

# 状態 1: 周期 6 の波の行番号。列 x の点を行 WAVE_ROWS[(x + shift) % 6] に置く。shift を 1 ずつ増やすと左へ流れる
WAVE_ROWS = [4, 3, 3, 4, 5, 5]
WAVE_FRAME_MS = 200


def wave(shift: int) -> list[str]:
    f = [["."] * COLS for _ in range(ROWS)]
    for x in range(COLS):
        f[WAVE_ROWS[(x + shift) % len(WAVE_ROWS)]][x] = "#"
    return ["".join(r) for r in f]


# 状態 2: 微笑む顔（静止画）
SMILE = [
    "............",
    "............",
    "...##..##...",
    "...##..##...",
    "............",
    "..#......#..",
    "...######...",
    "............",
]

WAVE_FRAMES = [(wave(s), WAVE_FRAME_MS) for s in range(len(WAVE_ROWS))]
SMILE_FRAMES = [(SMILE, 0)]   # 0 = 静止画


def check_shapes() -> None:
    for rows, _ in WAVE_FRAMES + SMILE_FRAMES:
        assert len(rows) == ROWS and all(len(r) == COLS and set(r) <= set("#.") for r in rows), rows
    for rows, _ in WAVE_FRAMES:
        for x in range(COLS):   # 各列にちょうど 1 個（計 12 個。どのフレームも点く数が同じ）
            assert sum(rows[y][x] == "#" for y in range(ROWS)) == 1, (rows, x)
    assert sum(r.count("#") for r in SMILE) == 16


def render() -> str:
    check_shapes()
    frames = WAVE_FRAMES + SMILE_FRAMES
    out = [
        "// shapes.h — 表示器の図形の表（Issue #32、docs/decisions/0018 の追記）。",
        "// 生成物。直接書き換えない。python3 firmware/indicator/shapes/gen_shapes.py で作り直す（元は shapes/gen_shapes.py）。",
        "// 状態 1 = 流れる波線（動画。FRAME_MS ごとに次のフレームへ進めてループ）、状態 2 = 微笑む顔（静止画。FRAME_MS は 0）。",
        "// 状態 0 は消灯（表を持たない）。Arduino 依存なし。",
        "#pragma once",
        "#include <stdint.h>",
        "",
        "static const int SHAPE_WAVE_FIRST = 0;   // 状態 1 の最初のフレーム",
        f"static const int SHAPE_WAVE_COUNT = {len(WAVE_FRAMES)};   // 状態 1 のフレーム数",
        f"static const int SHAPE_SMILE = {len(WAVE_FRAMES)};        // 状態 2 のフレーム",
        "",
        f"static const uint8_t FRAMES[{len(frames)}][{ROWS}][{COLS}] = {{",
    ]
    for k, (rows, ms) in enumerate(frames):
        name = "流れる波線" if k < len(WAVE_FRAMES) else "微笑む顔"
        out.append(f"  // [{k}] {name}" + (f" {ms} ms" if ms else "（静止画）"))
        out += [f"  //   {r}" for r in rows]
        out.append("  {" + ",".join("{" + ",".join("1" if c == "#" else "0" for c in r) + "}" for r in rows) + "},")
    out.append("};")
    out.append(f"static const uint16_t FRAME_MS[{len(frames)}] = {{{','.join(str(ms) for _, ms in frames)}}};")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="shapes.h が元と一致するかを見る（書かない）")
    a = ap.parse_args(argv)
    text = render()
    if a.check:
        cur = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if cur == text:
            print(f"gen_shapes: {OUT.name} は元と一致")
            return 0
        sys.stdout.writelines(difflib.unified_diff(cur.splitlines(True), text.splitlines(True),
                                                   f"{OUT.name}（今）", f"{OUT.name}（元から作った内容）"))
        print(f"gen_shapes: {OUT.name} が元と違う。python3 firmware/indicator/shapes/gen_shapes.py で作り直す")
        return 1
    OUT.write_text(text, encoding="utf-8")
    print(f"gen_shapes: {OUT} を書いた")
    return 0


if __name__ == "__main__":
    sys.exit(main())
