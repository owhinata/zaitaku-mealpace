"""M3 の評価側の固定の一覧と、それを学習側に入れない検査（#40、docs/decisions/0034）。

0034 の FINAL-TRAIN・FINAL-EVAL と同じ一覧（final_eval.py が実行時に 0034 と照合する）。
後日 data/raw/ にセッションを足しても、0034 の評価側の 7 本は学習側・交差検証に
入れない（評価側を見た後に構成・後処理・種を比べ直す経路を止める）。
train_eval.py（--throat）・compare.py・visualize.py から読むので、標準ライブラリの
ほかは読み込まない（M1 の経路に torch・librosa を入れない）。

M4（#46、docs/decisions/0037）の定数と検査も置く。M4 は日付の境
（M4_EVAL_FROM）と名前の固定の一覧で分け、M3 の評価側の 7 本は M4 では学習側
（M4_TRAIN_M3）。
M3 の一覧と検査（上の 3 つと assert_fixed_eval_not_in・marker_note）は変えない。
M4 の検査を呼ぶのは #45（assert_m4_train）と #49（assert_m4_eval・M4_MARKER）の
入口。ここでは名前だけを見て、データを開かない。
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


# --- M4（#46、docs/decisions/0037）。M4 のどの性能の数字も見る前に固定 ---
M4_DECISION = "docs/decisions/0037-m4-split-and-recording-plan.md"
# 日付はフォルダ名の先頭 8 桁で比べる（人の決定 10/24 の日程）
M4_TRAIN_CUTOFF = "20261028"  # 学習用の記録の締め（この日の終わりまで）
M4_EVAL_FROM = "20261101"  # 評価側の収集日の窓の初日
M4_EVAL_TO = "20261104"  # 評価側の打ち切り（この日の終わりまで）
# M3 の 21 本。M3 の評価側だった 7 本も M4 では学習側（0037 の 2）
M4_TRAIN_M3 = EXPECTED_TRAIN + EXPECTED_EVAL
# 学習用の記録の締めの後、#45 が 0038 を固定する前に名前を入れる
M4_EXPECTED_TRAIN: tuple | None = None
# 評価用の記録の打ち切りと採否の後、#49 が評価側を開く前に名前を入れる
M4_EXPECTED_EVAL: tuple | None = None
M4_MARKER = "docs/log/m4-final-49.txt"  # 固定のパス。作るのは #49 の入口
M4_MARKER_NOTE = (
    f"- M4 の評価側は採点済み（印 `{M4_MARKER}` あり。docs/decisions/0037）。"
)
# 0037 の M4- の行で、一覧がまだ無い（None）ときに書いてある文
M4_TRAIN_PENDING = "（学習用の記録の締めの後に追記）"
M4_EVAL_PENDING = "（評価用の記録の打ち切りと採否の後に追記）"


def _day(name: str) -> str:
    """フォルダ名の先頭 8 桁（収集日）。日付の形でなければ止まる。"""
    d = name[:8]
    if len(d) != 8 or not d.isdigit():
        raise SystemExit(f"{M4_DECISION}: 日付で始まらない名前です: {name}")
    return d


def _dash(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def assert_m4_train(train_names: Sequence[str]) -> None:
    """M4 の学習側の一覧の検査（0037 の 2・3・7）。名前だけを見る。

    次のどれかで止まる: (i) 日付が M4_TRAIN_CUTOFF より後の名前がある、
    (ii) M4_EXPECTED_EVAL の名前がある、(iii) M4_TRAIN_M3 の 21 本が全部
    そろっていない、(iv) M4_EXPECTED_TRAIN が入っていて、一覧と一致しない。
    """
    names = list(train_names)
    late = [n for n in names if _day(n) > M4_TRAIN_CUTOFF]
    if late:
        raise SystemExit(
            f"{M4_DECISION} の学習用の記録の締め（{_dash(M4_TRAIN_CUTOFF)}）"
            f"より後のセッションが学習側に入っています: {', '.join(late)}"
        )
    if M4_EXPECTED_EVAL is not None:
        hit = [n for n in names if n in set(M4_EXPECTED_EVAL)]
        if hit:
            raise SystemExit(
                f"{M4_DECISION} の評価側（M4-EVAL）のセッションが学習側に"
                f"入っています: {', '.join(hit)}"
            )
    missing = [n for n in M4_TRAIN_M3 if n not in set(names)]
    if missing:
        raise SystemExit(
            f"{M4_DECISION} の学習側に M3 の 21 本がそろっていません。"
            f"無いもの: {', '.join(missing)}"
        )
    if M4_EXPECTED_TRAIN is not None and sorted(names) != sorted(
        M4_EXPECTED_TRAIN
    ):
        raise SystemExit(
            f"学習側の一覧が {M4_DECISION} の M4-TRAIN と一致しません。\n"
            "一覧: " + " ".join(sorted(names)) + "\n"
            "M4-TRAIN: " + " ".join(sorted(M4_EXPECTED_TRAIN))
        )


def assert_m4_eval(eval_names: Sequence[str]) -> None:
    """M4 の評価側の一覧の検査（0037 の 1・5・7）。名前だけを見る。

    次のどれかで止まる: (i) M4_EXPECTED_EVAL が None（一覧の追記の前に評価側を
    開かせない）、(ii) 一覧と一致しない、(iii) 日付が窓
    （M4_EVAL_FROM〜M4_EVAL_TO）の外、(iv) 3 本未満。収集日の数（2 日以上）は
    止める条件にせず、報告の注記に回す（0037 の 1）。
    """
    names = list(eval_names)
    if M4_EXPECTED_EVAL is None:
        raise SystemExit(
            f"{M4_DECISION} の評価側の一覧（M4-EVAL）がまだありません。"
            "評価用の記録の打ち切りと採否の後に追記してから開く"
        )
    if sorted(names) != sorted(M4_EXPECTED_EVAL):
        raise SystemExit(
            f"評価側の一覧が {M4_DECISION} の M4-EVAL と一致しません。\n"
            "一覧: " + " ".join(sorted(names)) + "\n"
            "M4-EVAL: " + " ".join(sorted(M4_EXPECTED_EVAL))
        )
    out = [n for n in names if not M4_EVAL_FROM <= _day(n) <= M4_EVAL_TO]
    if out:
        raise SystemExit(
            f"{M4_DECISION} の評価側の窓（{_dash(M4_EVAL_FROM)}〜"
            f"{_dash(M4_EVAL_TO)}）の外のセッションがあります: {', '.join(out)}"
        )
    if len(names) < 3:
        raise SystemExit(
            f"{M4_DECISION} の評価側が {len(names)} 本です（3 本以上が要る。"
            "docs/evaluation.md）"
        )


def m4_expected_lines() -> list:
    """0037 にあるべき M4- の行（この順）。"""
    train = M4_TRAIN_PENDING
    if M4_EXPECTED_TRAIN is not None:
        train = " ".join(M4_EXPECTED_TRAIN)
    ev = M4_EVAL_PENDING
    if M4_EXPECTED_EVAL is not None:
        ev = " ".join(M4_EXPECTED_EVAL)
    return [
        f"M4-EVAL-WINDOW: from={_dash(M4_EVAL_FROM)} to={_dash(M4_EVAL_TO)}",
        f"M4-TRAIN-CUTOFF: {_dash(M4_TRAIN_CUTOFF)}",
        f"M4-TRAIN: {train}",
        f"M4-EVAL: {ev}",
    ]


def m4_lines_match(decision_text: str) -> bool:
    """0037 の本文の M4- で始まる行が、定数と一字一句一致するか。

    0034 の FINAL- の照合（final_eval.check_decision）と同じ読み方: 各行の
    前後の空白を除き、M4- で始まる行を順に並べて比べる。
    """
    got = [
        s.strip()
        for s in decision_text.splitlines()
        if s.strip().startswith("M4-")
    ]
    return got == m4_expected_lines()


def m4_marker_note() -> str | None:
    """M4 の印があれば、報告の冒頭に出す 1 行（出すのは #45 の入口）。"""
    p = REPO_ROOT / M4_MARKER
    return M4_MARKER_NOTE if p.exists() or p.is_symlink() else None
