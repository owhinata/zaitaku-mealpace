"""学習／評価をセッション単位で分ける。窓単位の分割は提供しない。

使い方: python split.py data/raw --eval-min 3
収集日の新しい順に評価セッションを取り、残りを学習にする。
"""
from __future__ import annotations
import argparse, json
from pathlib import Path


def split_sessions(root: Path, eval_min: int = 3, subject: str = "self"):
    sessions = sorted(p for p in root.iterdir() if p.is_dir() and f"_{subject}_" in p.name)
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
