"""M2 の特徴量ベクトル（1 窓 1 項目）を Edge Impulse に投入する（Issue #21、docs/decisions/0019・0020）。

使い方:
  python analysis/ei_upload.py data/raw --bucket training --days 20260921,20260922,20260923 --validation-day 20260923 [--dry-run]
  python analysis/ei_upload.py data/raw --bucket testing --days 20260924 [--dry-run]
  python analysis/ei_upload.py --probe [--dry-run]

- 投入するのは `features_m2.extract_session` の 29 次元を `features.Standardizer` で正規化した値だけ。生の音声・IMU の窓は上げない。
- 分割はセッション単位。`split.split_sessions(root, 7, "self")` の `train`（収集日1〜3、21 本）が EI の training set、
  `eval`（収集日4、7 本）が test set。収集日4 は `--bucket testing` 以外に入れない。subject は `self` 固定。
- ラベルは `swallow` / `cough` / `other` の 3 クラス（`window_labels3`。0019「ラベルの規則」「3 クラスの線引き」）。
  `valid = False` の窓と境目（UNUSED）の窓は投入しない。`other` の間引きはしない。
- 正規化の定数は fit セッション（`--days` から `--validation-day` を除いたもの）の `valid` な窓の全部から作り、
  `analysis/m2_norm.json` に書く（集計値のみ）。`--bucket testing` はそのファイルを読んで使い、収集日4 から統計量を作らない。
- 転送は標準ライブラリの `urllib` で、1 リクエスト 1 項目（`x-metadata` がリクエスト単位に掛かるため）。失敗は 3 回まで再試行。
  ヘッダは `x-api-key`、`x-label`、`x-metadata`、`x-file-name`（EI が要求する。無いと HTTP 422）、`x-disallow-duplicates`。
  API キーは環境変数 `EI_API_KEY` で渡し、標準出力・例外・ログに出さない。
- `--dry-run` は送信もファイルの書き込みもせず、一覧だけを出す。`--probe` は乱数の 9 項目で EI の受け付けを確かめる（実データを使わない）。
- 標準出力には集計値とセッション名だけを出す。特徴量の値・波形・個々の窓の一覧は出さない。
"""
from __future__ import annotations
import argparse, io, json, os, sys, time, uuid
import urllib.error, urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

import numpy as np

import evaluate, features, features_m2, split, train_eval

SUBJECT = "self"                                      # docs/evaluation.md「M1・M2 の数字は self のみ」。p1 は投入しない
FEATURE_SET = features_m2.FEATURE_SET                 # "m2-0020"
FEATURE_NAMES = features_m2.FEATURE_NAMES
N_FEATURES = features_m2.N_FEATURES                   # 29
EVAL_MIN = 7                                          # split_sessions の eval = 収集日4 の 7 本
TRAIN_DAYS = ("20260921", "20260922", "20260923")     # EI の training set（収集日1〜3）
TEST_DAYS = ("20260924",)                             # EI の test set（収集日4）
SESSIONS_PER_DAY = 7
BUCKETS = ("training", "testing")

# 3 クラスのラベル。swallow / cough の窓と境目の幅は train_eval（0014）と同じ
OTHER, SWALLOW, COUGH, UNUSED = 0, 1, 2, -1
CLASS_NAMES = {SWALLOW: "swallow", COUGH: "cough", OTHER: "other"}
LABEL_POS_S = train_eval.LABEL_POS_S                  # 1.0
LABEL_MARGIN_S = train_eval.LABEL_MARGIN_S            # 0.5

NORM_PATH = Path(__file__).resolve().parent / "m2_norm.json"
NORM_KEYS = ("feature_set", "feature_names", "mean", "std", "stats_sessions", "n_windows")   # commit は照合しない

INGESTION_URL = "https://ingestion.edgeimpulse.com/api/{bucket}/data"
API_KEY_ENV = "EI_API_KEY"
DEVICE_NAME = "pc"
DEVICE_TYPE = "analysis/ei_upload.py"
INTERVAL_MS = 1000                                    # 1 行の項目を impulse の窓 1000 ms と読ませるための名目の値
UNITS = "z"                                           # 正規化後の値
MAX_ATTEMPTS = 4                                      # 1 回 + 再試行 3 回
RETRY_WAIT_S = (1.0, 2.0, 4.0)                        # 再試行の前の待ち時間
TIMEOUT_S = 60.0
FORM_FIELD = "data"

PROBE_SEED = 21
PROBE_GROUPS = (("probe-a", "probe-1", 2), ("probe-b", "probe-2", 3), ("probe-c", "probe-3", 4))   # (session, day, 件数)
PROBE_SUBJECT = "probe"                               # 乱数の項目。self のデータではない
PROBE_FEATURE_SET = "probe"

SendFn = Callable[[str, Mapping[str, str], Sequence[tuple[str, bytes]]], tuple[int, str]]


# --- ラベル ---

def window_labels3(t_start_s: np.ndarray, swallow_t: Sequence[float], cough_t: Sequence[float]) -> np.ndarray:
    """3 クラスの学習のラベル (n,) int8。評価の数え方には使わない。

    1. 全窓を other。2. 各 c について中心が [t_c − 0.5, t_c + 1.5] の窓を UNUSED、次に [t_c, t_c + 1.0] を cough。
    3. 各 s について同じ形で UNUSED → swallow。嚥下の規則を後に掛けるので s の陽性と境目が c の規則より優先される。
    近接した 2 つの嚥下では陽性が境目より優先（train_eval.window_labels と同じ）。
    """
    c = np.asarray(t_start_s, dtype=np.float64) + evaluate.WINDOW_S / 2
    labels = np.full(len(c), OTHER, dtype=np.int8)
    for t in cough_t:
        labels[(c >= t - LABEL_MARGIN_S) & (c <= t + LABEL_POS_S + LABEL_MARGIN_S)] = UNUSED
    for t in cough_t:
        labels[(c >= t) & (c <= t + LABEL_POS_S)] = COUGH
    for t in swallow_t:
        labels[(c >= t - LABEL_MARGIN_S) & (c <= t + LABEL_POS_S + LABEL_MARGIN_S)] = UNUSED
    for t in swallow_t:
        labels[(c >= t) & (c <= t + LABEL_POS_S)] = SWALLOW
    return labels


# --- 入力と分割 ---

def _day(name: str) -> str:
    """フォルダ名の先頭 8 桁（収集日）。"""
    return name[:8]


def check_split(root: Path) -> tuple[list[str], list[str]]:
    """split_sessions の train が収集日1〜3 の各 7 本、eval が収集日4 の 7 本ちょうどであることを検査する。"""
    s = split.split_sessions(root, EVAL_MIN, SUBJECT)
    train, eval_ = s["train"], s["eval"]
    for label, names, days in (("train", train, TRAIN_DAYS), ("eval", eval_, TEST_DAYS)):
        count = Counter(_day(n) for n in names)
        if set(count) != set(days) or any(count[d] != SESSIONS_PER_DAY for d in days):
            raise SystemExit(f"split_sessions の {label} が期待と違います（{', '.join(days)} の各 {SESSIONS_PER_DAY} 本）: "
                             f"{dict(sorted(count.items()))}")
    return train, eval_


def parse_days(text: str) -> list[str]:
    days = [d.strip() for d in text.split(",") if d.strip()]
    if not days or any(not (len(d) == 8 and d.isdigit()) for d in days) or len(set(days)) != len(days):
        raise SystemExit(f"--days は 8 桁の収集日をコンマで並べます（重複なし）: {text!r}")
    return days


def select_sessions(bucket: str, days: Sequence[str], validation_day: str | None,
                    train: Sequence[str], eval_: Sequence[str]) -> list[tuple[str, str]]:
    """投入するセッションの (名前, 役割) を名前順に返す。bucket と収集日の組み合わせを検査する。"""
    if bucket not in BUCKETS:
        raise SystemExit(f"--bucket は training / testing のどちらかです: {bucket!r}")
    known = {_day(n) for n in train} | {_day(n) for n in eval_}
    if not set(days) <= known:
        raise SystemExit(f"--days に train ∪ eval に無い収集日があります: {sorted(set(days) - known)}")
    if bucket == "training":
        if set(days) & set(TEST_DAYS):
            raise SystemExit(f"--bucket training の --days に収集日4（{', '.join(TEST_DAYS)}）は入れられません（test set 以外に入れない）")
        if validation_day is not None and validation_day not in days:
            raise SystemExit(f"--validation-day が --days に含まれていません: {validation_day}")
        names = [n for n in train if _day(n) in days]
    else:
        if set(days) != set(TEST_DAYS):
            raise SystemExit(f"--bucket testing の --days は {', '.join(TEST_DAYS)} だけを受け付けます: {list(days)}")
        if validation_day is not None:
            raise SystemExit("--bucket testing に --validation-day は使えません")
        names = [n for n in eval_ if _day(n) in days]
    out = []
    for n in sorted(names):
        role = "test" if bucket == "testing" else ("validation" if _day(n) == validation_day else "fit")
        out.append((n, role))
    return out


# --- セッションの読み込みと集計 ---

@dataclass(frozen=True, eq=False)
class Loaded:
    name: str
    day: str
    bucket: str
    role: str                      # fit / validation / test
    feats: features.WindowFeatures
    labels: np.ndarray             # (n,) int8。window_labels3

    @property
    def upload_mask(self) -> np.ndarray:
        return self.feats.valid & (self.labels != UNUSED)


@dataclass(frozen=True)
class SessionSummary:
    name: str
    day: str
    bucket: str
    role: str
    n_windows: int
    n_valid: int
    n_upload: int
    n_swallow: int
    n_cough: int
    n_other: int
    n_invalid: int                 # 除外（無効）
    n_margin: int                  # 除外（境目。valid だが UNUSED）


def load_session(root: Path, name: str, bucket: str, role: str) -> Loaded:
    d = root / name
    f = features_m2.extract_session(d)
    labels = window_labels3(f.t_start_s, evaluate.load_events(d, "s"), evaluate.load_events(d, "c"))
    return Loaded(name=name, day=_day(name), bucket=bucket, role=role, feats=f, labels=labels)


def summarize(s: Loaded) -> SessionSummary:
    up = s.upload_mask
    return SessionSummary(
        name=s.name, day=s.day, bucket=s.bucket, role=s.role,
        n_windows=len(s.labels), n_valid=int(s.feats.valid.sum()), n_upload=int(up.sum()),
        n_swallow=int((up & (s.labels == SWALLOW)).sum()), n_cough=int((up & (s.labels == COUGH)).sum()),
        n_other=int((up & (s.labels == OTHER)).sum()),
        n_invalid=int((~s.feats.valid).sum()), n_margin=int((s.feats.valid & (s.labels == UNUSED)).sum()))


# --- 正規化の定数 ---

def norm_document(std: features.Standardizer, n_windows: int, commit: str) -> dict:
    return {"feature_set": FEATURE_SET, "feature_names": list(FEATURE_NAMES),
            "mean": [float(v) for v in std.mean], "std": [float(v) for v in std.std],
            "stats_sessions": list(std.sessions), "n_windows": int(n_windows), "commit": commit}


def norm_matches(a: Mapping, b: Mapping) -> bool:
    """commit 以外の項目が同じか（JSON を往復した後の値で比べる）。"""
    return all(json.loads(json.dumps(a.get(k))) == json.loads(json.dumps(b.get(k))) for k in NORM_KEYS)


def read_norm(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SystemExit(f"{path} が読めません: {e}")
    if not isinstance(doc, dict) or any(k not in doc for k in NORM_KEYS):
        raise SystemExit(f"{path} の形が違います（{', '.join(NORM_KEYS)} が要る）")
    if doc["feature_set"] != FEATURE_SET or list(doc["feature_names"]) != list(FEATURE_NAMES):
        raise SystemExit(f"{path} の feature_set / feature_names が {FEATURE_SET} と違います")
    if len(doc["mean"]) != N_FEATURES or len(doc["std"]) != N_FEATURES:
        raise SystemExit(f"{path} の mean / std が {N_FEATURES} 個ではありません")
    return doc


def standardizer_from_norm(doc: Mapping) -> features.Standardizer:
    return features.Standardizer(mean=np.asarray(doc["mean"], dtype=np.float64),
                                 std=np.asarray(doc["std"], dtype=np.float64),
                                 sessions=tuple(doc["stats_sessions"]))


# --- 項目の形 ---

@dataclass(frozen=True)
class Item:
    session: str
    filename: str                  # <セッション名>_<t_ms>.json
    label: str                     # swallow / cough / other
    metadata: dict                 # x-metadata（値はすべて文字列）
    data: bytes                    # データ取得の JSON


def item_json(values: np.ndarray, iat: int) -> bytes:
    values = np.asarray(values, dtype=np.float32)
    if values.shape != (N_FEATURES,):
        raise ValueError(f"値は {N_FEATURES} 個の 1 行です: {values.shape}")
    payload = {"device_name": DEVICE_NAME, "device_type": DEVICE_TYPE, "interval_ms": INTERVAL_MS,
               "sensors": [{"name": n, "units": UNITS} for n in FEATURE_NAMES],
               "values": [[float(v) for v in values]]}
    doc = {"protected": {"ver": "v1", "alg": "none", "iat": int(iat)}, "signature": "0" * 64, "payload": payload}
    return json.dumps(doc, separators=(",", ":")).encode("utf-8")


def make_item(session: str, day: str, t_ms: int, label: str, values: np.ndarray, iat: int,
              subject: str = SUBJECT, feature_set: str = FEATURE_SET) -> Item:
    metadata = {"session": session, "day": day, "t_ms": str(int(t_ms)), "subject": subject, "feature_set": feature_set}
    return Item(session=session, filename=f"{session}_{int(t_ms)}.json", label=label, metadata=metadata,
                data=item_json(values, iat))


def build_items(s: Loaded, std: features.Standardizer, iat: int) -> list[Item]:
    """投入する窓（valid かつ UNUSED でない）を正規化して項目にする。"""
    z = std.transform(s.feats.X)
    out = []
    for k in np.nonzero(s.upload_mask)[0]:
        t_ms = int(round(float(s.feats.t_start_s[k]) * 1000))
        out.append(make_item(s.name, s.day, t_ms, CLASS_NAMES[int(s.labels[k])], z[k], iat))
    return out


def probe_items(iat: int) -> list[Item]:
    """乱数（seed 固定）の 29 次元を 9 項目。swallow / cough / other × 3。群ごとの件数は 2 / 3 / 4。実データは使わない。"""
    rng = np.random.default_rng(PROBE_SEED)
    labels = [CLASS_NAMES[c] for c in (SWALLOW, COUGH, OTHER)]
    out = []
    for session, day, count in PROBE_GROUPS:
        for k in range(count):
            values = rng.standard_normal(N_FEATURES).astype(np.float32)
            out.append(make_item(session, day, 1000 * k, labels[len(out) % len(labels)], values, iat,
                                 subject=PROBE_SUBJECT, feature_set=PROBE_FEATURE_SET))
    assert len(out) == 9 and Counter(i.label for i in out) == {l: 3 for l in labels}
    return out


# --- 転送 ---

def _multipart(files: Sequence[tuple[str, bytes]]) -> tuple[str, bytes]:
    boundary = "----zaitaku-mealpace-" + uuid.uuid4().hex
    buf = io.BytesIO()
    for name, data in files:
        buf.write((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{FORM_FIELD}\"; filename=\"{name}\"\r\n"
                   f"Content-Type: application/json\r\n\r\n").encode("utf-8"))
        buf.write(data)
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode("utf-8"))
    return f"multipart/form-data; boundary={boundary}", buf.getvalue()


def send_urllib(url: str, headers: Mapping[str, str], files: Sequence[tuple[str, bytes]]) -> tuple[int, str]:
    """multipart/form-data を POST し (HTTP の status, 本文) を返す。接続の失敗は status 0。例外を投げない。"""
    ctype, body = _multipart(files)
    req = urllib.request.Request(url, data=body, method="POST", headers={**headers, "Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return int(r.status), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return int(e.code), e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        return 0, str(getattr(e, "reason", e))


def _redact(text: str, api_key: str) -> str:
    return text.replace(api_key, f"<{API_KEY_ENV}>") if api_key else text


def _is_duplicate(status: int, body: str) -> bool:
    """x-disallow-duplicates で弾かれた項目（再実行のとき）。EI の本文の文言は probe で確かめる。"""
    b = body.lower()
    return status == 400 and ("duplicate" in b or "already exists" in b)


@dataclass
class UploadResult:
    n_requests: int = 0            # 再試行を含む
    n_items: int = 0               # 受け付けられた項目
    n_duplicates: int = 0          # 既にある項目として弾かれたもの（再実行のとき）


def upload(items_by_session: Sequence[tuple[str, Sequence[Item]]], bucket: str, api_key: str,
           send: SendFn = send_urllib, sleep: Callable[[float], None] = time.sleep) -> UploadResult:
    """1 リクエスト 1 項目で送る。失敗は MAX_ATTEMPTS − 1 回まで再試行し、それでも失敗したら止まる。"""
    url = INGESTION_URL.format(bucket=bucket)
    res = UploadResult()
    for session, items in items_by_session:
        sent = 0
        for it in items:
            # x-file-name は EI の ingestion API が要求する（無いと HTTP 422。probe で判明。plan 7.4 のヘッダ一覧には無い）。
            # multipart の filename と同じ値
            headers = {"x-api-key": api_key, "x-label": it.label, "x-metadata": json.dumps(it.metadata),
                       "x-file-name": it.filename, "x-disallow-duplicates": "1"}
            status, body = 0, ""
            for attempt in range(MAX_ATTEMPTS):
                if attempt:
                    sleep(RETRY_WAIT_S[min(attempt - 1, len(RETRY_WAIT_S) - 1)])
                res.n_requests += 1
                status, body = send(url, headers, [(it.filename, it.data)])
                if 200 <= status < 300:
                    res.n_items += 1
                    sent += 1
                    break
                if _is_duplicate(status, body):
                    res.n_duplicates += 1
                    sent += 1
                    break
            else:
                raise SystemExit(f"送信に失敗しました（{MAX_ATTEMPTS} 回）。止まった場所: セッション {session}、"
                                 f"このセッションで送った項目 {sent} / {len(items)}、全体で受け付けられた項目 {res.n_items}、"
                                 f"リクエスト {res.n_requests}。最後の応答: HTTP {status} {_redact(body, api_key)[:200]!r}")
        print(f"送信: {session}: {sent} / {len(items)} 項目")
    return res


# --- 一覧 ---

def _print_header(argv: Sequence[str], dry_run: bool) -> None:
    print("## 実行の条件")
    print(f"- コマンドライン: `ei_upload.py {' '.join(argv)}`")
    print(f"- コミット: {train_eval.git_commit()}")
    print(f"- feature_set: {FEATURE_SET}")
    print(f"- numpy: {np.__version__}")
    print(f"- dry-run: {'yes（送信もファイルの書き込みもしない）' if dry_run else 'no'}")


def print_listing(summaries: Sequence[SessionSummary], stats_sessions: Sequence[str], n_stats_windows: int) -> dict[str, int]:
    """セッションごとの表と合計。収集日ごとの投入数を返す。"""
    print()
    print("## セッションごと")
    print("| セッション | 収集日 | bucket | 役割 | 全窓 | valid | 投入 | swallow | cough | other | 除外（無効） | 除外（境目） |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for s in summaries:
        print(f"| {s.name} | {s.day} | {s.bucket} | {s.role} | {s.n_windows} | {s.n_valid} | {s.n_upload} | "
              f"{s.n_swallow} | {s.n_cough} | {s.n_other} | {s.n_invalid} | {s.n_margin} |")
    total = SessionSummary(
        name="合計", day="", bucket="", role="",
        n_windows=sum(s.n_windows for s in summaries), n_valid=sum(s.n_valid for s in summaries),
        n_upload=sum(s.n_upload for s in summaries), n_swallow=sum(s.n_swallow for s in summaries),
        n_cough=sum(s.n_cough for s in summaries), n_other=sum(s.n_other for s in summaries),
        n_invalid=sum(s.n_invalid for s in summaries), n_margin=sum(s.n_margin for s in summaries))
    print(f"| {total.name} | | | | {total.n_windows} | {total.n_valid} | {total.n_upload} | {total.n_swallow} | "
          f"{total.n_cough} | {total.n_other} | {total.n_invalid} | {total.n_margin} |")

    def group(key):
        out: dict[str, list[int]] = {}
        for s in summaries:
            g = out.setdefault(key(s), [0, 0, 0, 0, 0])
            g[0] += 1; g[1] += s.n_upload; g[2] += s.n_swallow; g[3] += s.n_cough; g[4] += s.n_other
        return out

    print()
    print("## 合計")
    print(f"- セッション {len(summaries)} 本、全窓 {total.n_windows}、valid {total.n_valid}、投入 {total.n_upload}"
          f"（swallow {total.n_swallow}、cough {total.n_cough}、other {total.n_other}）、"
          f"除外 無効 {total.n_invalid} / 境目 {total.n_margin}")
    for title, key in (("bucket", lambda s: s.bucket), ("役割", lambda s: s.role), ("収集日", lambda s: s.day)):
        print(f"- {title}ごと:")
        for k, (n, up, sw, co, ot) in sorted(group(key).items()):
            print(f"  - {k}: セッション {n} 本、投入 {up}（swallow {sw}、cough {co}、other {ot}）")
    print(f"- クラスごと: swallow {total.n_swallow}、cough {total.n_cough}、other {total.n_other}")
    print()
    print("## 正規化の定数（stats_sessions）")
    print(f"- セッション {len(stats_sessions)} 本、valid な窓 {n_stats_windows}")
    for n in stats_sessions:
        print(f"  - {n}")
    return {k: v[1] for k, v in group(lambda s: s.day).items()}


def check_day_counts_distinct(per_day: Mapping[str, int]) -> None:
    """収集日ごとの投入数が互いに異なる（EI の validation の件数から選ばれた日を一意に読み取るため。plan 第 8.2 節）。"""
    dup = [d for d, n in per_day.items() if list(per_day.values()).count(n) > 1]
    if dup:
        raise SystemExit(f"収集日ごとの投入数が同数の日があります（validation に選ばれた日を件数で読み取れない）: "
                         f"{dict(sorted(per_day.items()))}")


# --- 本体 ---

def _parse(argv: Sequence[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", type=Path, nargs="?", help="data/raw（--probe では不要）")
    ap.add_argument("--bucket", choices=BUCKETS, help="EI の training / testing")
    ap.add_argument("--days", help="投入する収集日（8 桁、コンマ区切り）")
    ap.add_argument("--validation-day", help="EI の学習中に validation に回す収集日（統計量から除く）")
    ap.add_argument("--dry-run", action="store_true", help="送信もファイルの書き込みもせず、一覧だけ出す")
    ap.add_argument("--probe", action="store_true", help="乱数の 9 項目で EI の受け付けを確かめる")
    return ap.parse_args(argv)


def _api_key(environ: Mapping[str, str], dry_run: bool) -> str:
    key = environ.get(API_KEY_ENV, "")
    if not key and not dry_run:
        raise SystemExit(f"環境変数 {API_KEY_ENV} がありません（--dry-run 以外は必要）")
    return key


def run(argv: Sequence[str], *, send: SendFn = send_urllib, sleep: Callable[[float], None] = time.sleep,
        environ: Mapping[str, str] | None = None, norm_path: Path = NORM_PATH) -> UploadResult:
    a = _parse(argv)
    environ = os.environ if environ is None else environ
    iat = int(time.time())
    _print_header(argv, a.dry_run)

    if a.probe:
        if a.root is not None or a.bucket or a.days or a.validation_day:
            raise SystemExit("--probe は root / --bucket / --days / --validation-day と一緒に使えません")
        api_key = _api_key(environ, a.dry_run)
        items = probe_items(iat)
        print()
        print("## probe の項目（training に送る。実データではない）")
        by_session = [(s, [i for i in items if i.session == s]) for s, _, _ in PROBE_GROUPS]
        for (s, day, count), (_, its) in zip(PROBE_GROUPS, by_session):
            print(f"- {s}（day = {day}）: {len(its)} 項目（{', '.join(i.label for i in its)}）")
            assert len(its) == count
        print(f"- 合計 {len(items)} 項目（swallow 3、cough 3、other 3）")
        if a.dry_run:
            print("- dry-run: 送信しない")
            return UploadResult()
        res = upload(by_session, "training", api_key, send, sleep)
        print(f"- 送信したリクエスト {res.n_requests}、受け付けられた項目 {res.n_items}、重複として弾かれた項目 {res.n_duplicates}")
        return res

    if a.root is None or not a.bucket or not a.days:
        raise SystemExit("root、--bucket、--days は必須です（--probe 以外）")
    if not a.root.is_dir():
        raise SystemExit(f"フォルダが見つかりません: {a.root}")
    days = parse_days(a.days)
    train, eval_ = check_split(a.root)
    selected = select_sessions(a.bucket, days, a.validation_day, train, eval_)
    if not selected:
        raise SystemExit("投入するセッションがありません")

    existing = None
    if a.bucket == "testing":
        # 収集日4 から統計量を作らない。m2_norm.json が要る
        if not norm_path.is_file():
            raise SystemExit(f"{norm_path} がありません。先に --bucket training を実行してください")
        existing = read_norm(norm_path)
        bad = [n for n in existing["stats_sessions"] if _day(n) in TEST_DAYS]
        if bad:
            raise SystemExit(f"{norm_path} の stats_sessions に収集日4 のセッションがあります: {bad}")

    api_key = _api_key(environ, a.dry_run)
    loaded = [load_session(a.root, name, a.bucket, role) for name, role in selected]

    doc = None
    if a.bucket == "training":
        fit = [s.feats for s in loaded if s.role == "fit"]
        if not fit:
            raise SystemExit("fit セッションがありません（--days の全部が --validation-day）")
        std = features.Standardizer.fit(fit)
        n_stats_windows = int(sum(f.valid.sum() for f in fit))
        doc = norm_document(std, n_stats_windows, train_eval.git_commit())
        if norm_path.is_file():
            # 黙って上書きしない。作り直すときは人が消す
            existing = read_norm(norm_path)
            if not norm_matches(existing, doc):
                raise SystemExit(f"{norm_path} が既にあり、内容が違います（黙って上書きしない。作り直すときは人が消す）")
    else:
        assert existing is not None
        std = standardizer_from_norm(existing)
        n_stats_windows = int(existing["n_windows"])

    summaries = [summarize(s) for s in loaded]
    per_day = print_listing(summaries, std.sessions, n_stats_windows)
    print(f"- 収集日ごとの投入数: " + ", ".join(f"{d} = {n}" for d, n in sorted(per_day.items())))
    if a.bucket == "training":
        check_day_counts_distinct(per_day)
        print("  （互いに異なる）")

    n_upload = sum(s.n_upload for s in summaries)
    print()
    print("## 送信")
    if a.dry_run:
        print(f"- dry-run: 送信しない（投入する予定の項目 {n_upload}、リクエスト {n_upload}）。{norm_path.name} は書かない")
        return UploadResult()
    if doc is not None and not norm_path.is_file():
        norm_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"- 書いた: {norm_path}")
    by_session = [(s.name, build_items(s, std, iat)) for s in loaded]
    assert sum(len(i) for _, i in by_session) == n_upload
    res = upload(by_session, a.bucket, api_key, send, sleep)
    print(f"- 送信したリクエスト {res.n_requests}、受け付けられた項目 {res.n_items}、重複として弾かれた項目 {res.n_duplicates}"
          f"（EI の Studio の項目数と照合する）")
    return res


def main(argv: Sequence[str] | None = None) -> None:
    run(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    main()
