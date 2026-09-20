"""学習／評価をセッション単位で分ける。窓単位の分割は提供しない。

使い方: python split.py data/raw --eval-min 3
収集日の新しい順に評価セッションを取り、残りを学習にする。

フォルダ名の形式（docs/data-schema.md）と meta.json の subject を確かめ、合わないものがあれば止まる
（docs/decisions/0011）。
"""
from __future__ import annotations
import argparse, json, re
from pathlib import Path

SUBJECTS = ("self", "p1")
EVAL_MIN_FLOOR = 3   # docs/evaluation.md「評価には 3 セッション以上を使う」
# YYYYMMDD-HHMMSS_<subject>_<cond>。cond は docs/data-schema.md の列挙
NAME_RE = re.compile(r"^\d{8}-\d{6}_(self|p1)_(water|saliva|talk|cough|neck|quiet|meal)$")


def _meta_subject(session: Path) -> str:
    """meta.json の subject を返す。無い・読めない・self / p1 でないなら止まる。"""
    path = session / "meta.json"
    if not path.is_file():
        raise SystemExit(f"meta.json がありません（中断したセッションは消す）: {session.name}")
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise SystemExit(f"meta.json が JSON として読めません: {session.name}")
    if not isinstance(meta, dict):
        raise SystemExit(f"meta.json が JSON のオブジェクトではありません: {session.name}")
    if meta.get("subject") not in SUBJECTS:
        raise SystemExit(f"meta.json の subject が self / p1 ではありません: {session.name}")
    return meta["subject"]


def split_sessions(root: Path, eval_min: int = 3, subject: str = "self"):
    if eval_min < EVAL_MIN_FLOOR:
        raise SystemExit(f"--eval-min は {EVAL_MIN_FLOOR} 以上にしてください: {eval_min}")
    if subject not in SUBJECTS:
        raise SystemExit(f"subject は self / p1 のどちらかです: {subject!r}")
    sessions = []
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue   # 通常ファイルは無視する
        if p.is_symlink():
            # 同じセッションを別名のリンクで並べると、学習側と評価側に同じ実体が入る
            raise SystemExit(f"セッションのフォルダがシンボリックリンクです: {p.name}")
        m = NAME_RE.fullmatch(p.name)
        if m is None:
            raise SystemExit(f"フォルダ名が形式に合いません（消すか直す）: {p.name}")
        if _meta_subject(p) != m.group(1):
            raise SystemExit(f"フォルダ名と meta.json の subject が違います: {p.name}")
        if m.group(1) == subject:
            sessions.append(p)
    if len(sessions) < eval_min + 1:
        raise SystemExit(f"セッションが足りません: {len(sessions)} (評価 {eval_min} + 学習 1 以上が必要)")
    eval_s = sessions[-eval_min:]
    train_s = sessions[:-eval_min]
    return {"train": [s.name for s in train_s], "eval": [s.name for s in eval_s]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--eval-min", type=int, default=3)
    ap.add_argument("--subject", default="self")
    a = ap.parse_args()
    print(json.dumps(split_sessions(a.root, a.eval_min, a.subject), ensure_ascii=False, indent=2))
