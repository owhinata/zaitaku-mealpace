#!/usr/bin/env python3
"""LED マトリクスの表示を模した小さな絵（PNG）を docs/img/ に作る（Issue #56）。

使い方（リポジトリの root から）:
  .venv/bin/python tools/gen_led_images.py
      docs/img/led_wave.png・led_smile.png・led_off.png を作り直す

図形は firmware/indicator/shapes/gen_shapes.py の元（波線の最初のフレームと
微笑む顔）をそのまま使う。UNO Q のマトリクスは 8 行 × 13 列で、表の 12 列を
左寄せにし右端の 1 列は消灯（docs/decisions/0035）。人の目に見える向きは
元の ASCII のとおり。色は firmware/unoq_mcu/README.md のカードの「赤い色」に
合わせた。依存は Pillow。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "img"
SHAPES = ROOT / "firmware" / "indicator" / "shapes" / "gen_shapes.py"

ROWS, COLS = 8, 13
PITCH = 14  # 点の間隔（px）
RADIUS = 5  # 点の半径（px）
MARGIN = 10  # 基板の縁（px）
BOARD = (28, 30, 34)
OFF = (52, 55, 60)
ON = (236, 64, 48)
GLOW = (120, 36, 30)


def load_shapes():
    spec = importlib.util.spec_from_file_location("gen_shapes", SHAPES)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def draw(rows: list[str], path: Path) -> None:
    w = MARGIN * 2 + PITCH * COLS
    h = MARGIN * 2 + PITCH * ROWS
    img = Image.new("RGB", (w, h), BOARD)
    d = ImageDraw.Draw(img)
    for y in range(ROWS):
        for x in range(COLS):
            lit = x < len(rows[y]) and rows[y][x] == "#"
            cx = MARGIN + PITCH * x + PITCH // 2
            cy = MARGIN + PITCH * y + PITCH // 2
            if lit:
                d.ellipse(
                    (
                        cx - RADIUS - 2,
                        cy - RADIUS - 2,
                        cx + RADIUS + 2,
                        cy + RADIUS + 2,
                    ),
                    fill=GLOW,
                )
            d.ellipse(
                (cx - RADIUS, cy - RADIUS, cx + RADIUS, cy + RADIUS),
                fill=ON if lit else OFF,
            )
    img.save(path, optimize=True)


def main() -> None:
    sh = load_shapes()
    sh.check_shapes()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    wave_rows, _ = sh.WAVE_FRAMES[0]
    draw(wave_rows, OUT_DIR / "led_wave.png")
    draw(sh.SMILE, OUT_DIR / "led_smile.png")
    draw(["." * sh.COLS] * sh.ROWS, OUT_DIR / "led_off.png")


if __name__ == "__main__":
    main()
