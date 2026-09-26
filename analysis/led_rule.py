"""detect.csv の positive と window_t_ms から LED の状態を再計算する（docs/decisions/0018、#25）。

装置の firmware/detector/led_rule.h / led_rule.cpp と同じ式: 最後の陽性窓の開始 w_p を保持し、現在の窓の開始 w との差が
GREEN_HOLD_MS 以下なら緑（2）、それ以外は黄（1）。陽性のたびに w_p を取り直す。消灯（0）は窓の結果が 1.0 秒出ないときで、
その時点は detect.csv の行に残らないので、ここでは出さない。LED は嚥下の目安であり、評価には使わない。
純粋関数。analysis/ の他のモジュールを import しない。
"""
from __future__ import annotations
from typing import Sequence

LED_OFF, LED_YELLOW, LED_GREEN = 0, 1, 2
GREEN_HOLD_MS = 1750          # docs/decisions/0018。firmware/detector/led_rule.h の LED_GREEN_HOLD_MS と同じ値


def led_states(window_t_ms: Sequence[int], positive: Sequence[int]) -> list[int]:
    """detect.csv の行（窓の開始の昇順）から 0018 の規則で各行の led（1 / 2）を出す。0（消灯）は行に出ない。"""
    if len(window_t_ms) != len(positive):
        raise ValueError(f"window_t_ms と positive の長さが違います: {len(window_t_ms)} != {len(positive)}")
    out: list[int] = []
    last_w: int | None = None
    prev: int | None = None
    for w, p in zip(window_t_ms, positive):
        w = int(w)
        if prev is not None and w <= prev:
            raise ValueError(f"window_t_ms が昇順ではありません: {prev} → {w}")
        prev = w
        if int(p):
            last_w = w
        out.append(LED_GREEN if last_w is not None and w - last_w <= GREEN_HOLD_MS else LED_YELLOW)
    return out
