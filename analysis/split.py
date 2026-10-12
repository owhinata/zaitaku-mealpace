"""学習／評価をセッション単位で分ける。窓単位の分割は提供しない。

使い方: python split.py data/raw --eval-min 3 [--kind m1|throat]
収集日の新しい順に評価セッションを取り、残りを学習にする。

フォルダ名の形式（docs/data-schema.md）と meta.json の subject を確かめ、
合わないものがあれば止まる
（docs/decisions/0011）。
meta.json の fw が detector のセッション（検出器で録った M2 のセッション）は M1
の分割（train / eval）に入れず、
戻り値の detector に列挙する。fw が detector でないのに detect.csv か feat.csv
があるセッションは止まる
（docs/decisions/0021）。fw が無い記録ファームウェアのセッション（
docs/decisions/0006）は今までどおり分割に入れる。

咽喉マイクのセッション（fw が pc-throat。docs/decisions/0029・0031）:
  --kind m1（既定）  M1 の分割に入れず、戻り値の throat に列挙する。
  --kind throat      咽喉マイクのセッションだけを分割する。M1 と detector は
                     戻り値の excluded に列挙する。iface・ファイルの有無を確かめ、
                     合わなければ止まる（meta.json とファイルの有無だけを見て、
                     throat.wav・events.csv の中身は開かない）。
  fw が pc-throat でないのに throat.wav があるセッションは、どちらでも止まる。
data/raw/ の直下で名前がちょうど trial-throat のディレクトリ（試用の置き場。
シンボリックリンクでないもの）は読まずに飛ばし、戻り値の skipped に列挙する
（docs/decisions/0031）。
"""

from __future__ import annotations
import argparse, json, re
from pathlib import Path

SUBJECTS = ("self", "p1")
EVAL_MIN_FLOOR = 3  # docs/evaluation.md「評価には 3 セッション以上を使う」
# YYYYMMDD-HHMMSS_<subject>_<cond>。cond は docs/data-schema.md の列挙
NAME_RE = re.compile(
    r"^\d{8}-\d{6}_(self|p1)_(water|saliva|talk|cough|neck|quiet|meal)$"
)


DETECTOR_FW = "detector"
DETECTOR_FILES = ("detect.csv", "feat.csv")

# 咽喉マイク（docs/decisions/0029・0031）
KINDS = ("m1", "throat")
THROAT_FW = "pc-throat"
THROAT_IFACE = "sh12jk-wired-unoq-usbaudio"
THROAT_FILES = ("throat.wav", "throat_chunks.csv")
THROAT_FORBIDDEN_FILES = (
    "imu.csv",
    "audio.wav",
    "audio_chunks.csv",
    *DETECTOR_FILES,
)
SKIP_DIRS = ("trial-throat",)  # 試用の置き場（#37）。読まずに飛ばす


def _meta(session: Path) -> dict:
    """meta.json を返す。無い・読めない・オブジェクトでない・subject が self /
    p1 でないなら止まる。"""
    path = session / "meta.json"
    if not path.is_file():
        raise SystemExit(
            f"meta.json がありません（中断したセッションは消す）: {session.name}"
        )
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise SystemExit(f"meta.json が JSON として読めません: {session.name}")
    if not isinstance(meta, dict):
        raise SystemExit(
            f"meta.json が JSON のオブジェクトではありません: {session.name}"
        )
    if meta.get("subject") not in SUBJECTS:
        raise SystemExit(
            f"meta.json の subject が self / p1 ではありません: {session.name}"
        )
    return meta


def _check_throat(p: Path, meta: dict) -> None:
    """咽喉マイクのセッションの iface とファイルの有無。中身は開かない。"""
    sensors = meta.get("sensors")
    first = sensors[0] if isinstance(sensors, list) and sensors else None
    iface = first.get("iface") if isinstance(first, dict) else None
    if iface != THROAT_IFACE:
        raise SystemExit(
            f"咽喉マイクのセッションの sensors[0].iface が {THROAT_IFACE} ではありません"
            f"（{iface}）: {p.name}"
        )
    missing = [n for n in THROAT_FILES if not (p / n).is_file()]
    if missing:
        raise SystemExit(
            f"咽喉マイクのセッションに {' / '.join(missing)} がありません: {p.name}"
        )
    extra = [n for n in THROAT_FORBIDDEN_FILES if (p / n).exists()]
    if extra:
        raise SystemExit(
            f"咽喉マイクのセッションに他の形式のファイル（{' / '.join(extra)}）があります: {p.name}"
        )


def split_sessions(
    root: Path, eval_min: int = 3, subject: str = "self", kind: str = "m1"
):
    if eval_min < EVAL_MIN_FLOOR:
        raise SystemExit(
            f"--eval-min は {EVAL_MIN_FLOOR} 以上にしてください: {eval_min}"
        )
    if subject not in SUBJECTS:
        raise SystemExit(f"subject は self / p1 のどちらかです: {subject!r}")
    if kind not in KINDS:
        raise SystemExit(f"kind は m1 / throat のどちらかです: {kind!r}")
    sessions, detector, throat, excluded, skipped = [], [], [], [], []
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue  # 通常ファイルは無視する
        if p.is_symlink():
            # 同じセッションを別名のリンクで並べると、学習側と評価側に同じ実体が
            # 入る
            raise SystemExit(
                f"セッションのフォルダがシンボリックリンクです: {p.name}"
            )
        if p.name in SKIP_DIRS:
            # 試用の置き場は読まない（docs/decisions/0031）
            skipped.append(p.name)
            continue
        m = NAME_RE.fullmatch(p.name)
        if m is None:
            raise SystemExit(
                f"フォルダ名が形式に合いません（消すか直す）: {p.name}"
            )
        meta = _meta(p)
        if meta["subject"] != m.group(1):
            raise SystemExit(
                f"フォルダ名と meta.json の subject が違います: {p.name}"
            )
        mine = m.group(1) == subject
        if meta.get("fw") != THROAT_FW and (p / "throat.wav").exists():
            raise SystemExit(
                f"throat.wav があるのに meta.json の fw が {THROAT_FW} ではありません: {p.name}"
            )
        if meta.get("fw") == DETECTOR_FW:
            # 検出器のセッションは M1 の分割に入れない（docs/decisions/0021）
            if mine:
                (detector if kind == "m1" else excluded).append(p)
            continue
        if meta.get("fw") == THROAT_FW:
            # 咽喉マイクのセッションは M1 の分割に入れない（docs/decisions/0031）
            if kind == "m1":
                if mine:
                    throat.append(p)
                continue
            _check_throat(p, meta)
            if mine:
                sessions.append(p)
            continue
        if any((p / name).is_file() for name in DETECTOR_FILES):
            # ファイルの有無を見るだけで、中身は開かない
            raise SystemExit(
                f"検出器のファイル（detect.csv / feat.csv）があるのに meta.json の fw が {DETECTOR_FW} では"
                f"ありません（META が届かなかったセッションは消す）: {p.name}"
            )
        if mine:
            (sessions if kind == "m1" else excluded).append(p)
    if len(sessions) < eval_min + 1:
        raise SystemExit(
            f"セッションが足りません: {len(sessions)} (評価 {eval_min} + 学習 1 以上が必要)"
        )
    eval_s = sessions[-eval_min:]
    train_s = sessions[:-eval_min]
    out = {
        "train": [s.name for s in train_s],
        "eval": [s.name for s in eval_s],
    }
    if kind == "m1":
        out["detector"] = [s.name for s in detector]
        out["throat"] = [s.name for s in throat]
    else:
        out["excluded"] = [s.name for s in excluded]
    out["skipped"] = skipped
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--eval-min", type=int, default=3)
    ap.add_argument("--subject", default="self")
    ap.add_argument("--kind", choices=KINDS, default="m1")
    a = ap.parse_args()
    print(
        json.dumps(
            split_sessions(a.root, a.eval_min, a.subject, a.kind),
            ensure_ascii=False,
            indent=2,
        )
    )
