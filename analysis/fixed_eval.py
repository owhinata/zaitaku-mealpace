"""M3 の評価側の固定の一覧と、それを学習側に入れない検査（#40、docs/decisions/0034）。

0034 の FINAL-TRAIN・FINAL-EVAL と同じ一覧（final_eval.py が実行時に 0034 と照合する）。
後日 data/raw/ にセッションを足しても、0034 の評価側の 7 本は学習側・交差検証に
入れない（評価側を見た後に構成・後処理・種を比べ直す経路を止める）。
train_eval.py（--throat）・compare.py・visualize.py から読むので、標準ライブラリの
ほかは読み込まない（M1 の経路に torch・librosa を入れない）。
"""

from __future__ import annotations
from pathlib import Path
from typing import Sequence

DECISION = "docs/decisions/0034-throat-mic-final-eval-cnn-judgment.md"
EXPECTED_TRAIN = (
    "20261011-100847_self_quiet",
    "20261011-101644_self_water",
    "20261011-102354_self_talk",
    "20261011-102854_self_saliva",
    "20261011-103352_self_neck",
    "20261011-103912_self_water",
    "20261011-104408_self_cough",
    "20261011-202238_self_quiet",
    "20261011-202730_self_water",
    "20261011-203152_self_talk",
    "20261011-203631_self_saliva",
    "20261011-204057_self_neck",
    "20261011-204526_self_water",
    "20261011-204956_self_cough",
)
EXPECTED_EVAL = (
    "20261012-191826_self_quiet",
    "20261012-192531_self_water",
    "20261012-193001_self_talk",
    "20261012-193459_self_saliva",
    "20261012-193929_self_neck",
    "20261012-194417_self_water",
    "20261012-194854_self_cough",
)
MARKER = "docs/log/m3-final-40.txt"  # 固定のパス（実行日によらない）
# 文書・印のパスはここを基準にする
REPO_ROOT = Path(__file__).resolve().parent.parent
MARKER_NOTE = (
    f"- 評価側は採点済み（印 `{MARKER}` あり。docs/decisions/0034）。"
    "この実行は学習側だけ。"
)


def assert_fixed_eval_not_in(train_names: Sequence[str]) -> None:
    """0034 の評価側の 7 本（EXPECTED_EVAL）のどれかが学習側にあれば止まる。
    分割の直後、データを読む前に呼ぶ（咽喉マイクの経路だけ。M1 には当てない）。"""
    hit = [n for n in train_names if n in set(EXPECTED_EVAL)]
    if hit:
        raise SystemExit(
            f"{DECISION} の評価側（FINAL-EVAL）のセッションが学習側に入っています: "
            f"{', '.join(hit)}。M3 の評価側は学習・交差検証に使わない"
        )


def marker_note() -> str | None:
    """印があれば、報告の冒頭に出す 1 行。"""
    p = REPO_ROOT / MARKER
    return MARKER_NOTE if p.exists() or p.is_symlink() else None
