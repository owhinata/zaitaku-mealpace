"""Edge Impulse の Model testing（test set = 収集日4）の窓ごとの結果を API で取り、PC 上の実行ファイルの確率と突き合わせる（Issue #22 第 9 節、決定 E1・F1）。

使い方:
  EI_API_KEY=... python analysis/ei_testing_result.py data/raw --project-id <ID>

- `GET https://studio.edgeimpulse.com/v1/api/<projectId>/classify/page?variant=int8&limit=..&offset=..`（ヘッダ `x-api-key`）を
  ページ分割で全件取り、`variant=float32` も参考に取る。**応答はメモリ上で突き合わせ、ファイルにも scratchpad にも書かない**
  （項目ごとの確率は収集日4 の窓ごとの情報。docs/decisions/0021 の FEAT と同じ扱い）。標準出力に出すのは集計値だけ。
- PC 側は `m2_threshold.py` と同じ経路（`features_m2.extract_session` → 実行ファイル）で収集日4 の 7 本を採点し、投入した窓（`valid` かつ
  境目でない窓）を `(session, t_ms)` で EI の項目と対応づける。EI の項目数が PC の投入数と一致しなければ止まる。
- 出す数字: 窓の数、int8 同士の `max |p_EI − p_PC|`（クラスごと）、`|Δp| > 0` と `|Δp| > 1/256` の窓の数、argmax が違う窓の数、凍結した閾値
  （`firmware/detector/m2_threshold.h`）で `swallow` の陽性・陰性が違う窓の数、3 × 3 の混同行列の一致、参考として float32（EI）と int8（PC）の `max |Δp|`。
  「一致」は argmax の不一致 0、閾値での不一致 0、かつ int8 同士の `max |Δp| = 0` のときだけ（決定 F1）。それ以外は「差あり」。
- API キーは環境変数 `EI_API_KEY`、プロジェクト ID は引数。どちらも標準出力・例外に出さない。`--project-id` は実行ファイルの見出しの
  `EI_CLASSIFIER_PROJECT_ID` と一致しなければ止まる（値は出さない）。
- 送受信の関数は `run(argv, fetch=...)` で差し替えられる（テストは偽の応答で動かす）。

応答の項目名について: 下の `parse_page` / `parse_item` が仮定している項目名（`result`、`sample.name` / `sample.metadata` / `sample.label`、
`classifications[].result[]`）は EI の API 文書の要約から置いたもので、実際の応答でまだ確かめていない。初回の実行で違っていれば
`parse_item` を実際の形に直し、項目名を log に書く（plan #22 第 4.4 節・第 16 節）。読めない項目があれば止まる（黙って飛ばさない）。
"""
from __future__ import annotations
import argparse, json, os, re, sys
import urllib.error, urllib.parse, urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

import numpy as np

import ei_upload, evaluate, features_m2, m2_scorer, m2_threshold, train_eval

API_BASE = "https://studio.edgeimpulse.com/v1/api"
API_KEY_ENV = ei_upload.API_KEY_ENV                    # EI_API_KEY
VARIANTS = ("int8", "float32")
PAGE_LIMIT = 500
TIMEOUT_S = 120.0
MAX_PAGES = 1000                                       # 無限ループの保険（4824 / 500 = 10 ページ）
QUANT_STEP = 1.0 / 256                                 # int8 の softmax の出力の刻み（plan 第 4.1 節）
CLASS_ORDER = m2_threshold.CLASS_ORDER
THRESHOLD_HEADER_PATH = m2_threshold.THRESHOLD_HEADER_PATH

FetchFn = Callable[[str, Mapping[str, str]], tuple[int, str]]   # GET url → (status, 本文)


@dataclass(frozen=True, eq=False)
class EIItem:
    session: str
    t_ms: int
    label: str | None                 # EI 上の正解のラベル（あれば）
    probs: dict                       # クラス名 → 確率


@dataclass(frozen=True, eq=False)
class PCItem:
    session: str
    t_ms: int
    label: str                        # window_labels3 のクラス名
    probs: dict                       # クラス名 → 確率（int8 の実行ファイル）


# --- 送受信 ---

def fetch_urllib(url: str, headers: Mapping[str, str]) -> tuple[int, str]:
    req = urllib.request.Request(url, method="GET", headers=dict(headers))
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return int(r.status), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return int(e.code), e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        return 0, str(getattr(e, "reason", e))


def _redact(text: str, api_key: str, project_id: str) -> str:
    for secret, name in ((api_key, f"<{API_KEY_ENV}>"), (project_id, "<project-id>")):
        if secret:
            text = text.replace(secret, name)
    return text


def page_url(project_id: str, variant: str, limit: int, offset: int, impulse_id: str | None = None) -> str:
    q = {"variant": variant, "limit": str(limit), "offset": str(offset)}
    if impulse_id:
        q["impulseId"] = impulse_id
    return f"{API_BASE}/{project_id}/classify/page?{urllib.parse.urlencode(q)}"


# --- 応答の読み方（項目名は仮置き。実際の応答で確かめる） ---

def _name_to_session_t_ms(name: str) -> tuple[str, int]:
    """ei_upload のファイル名 `<セッション名>_<t_ms>.json`（拡張子は無いこともある）。"""
    base = name[:-5] if name.endswith(".json") else name
    session, _, t = base.rpartition("_")
    if not session or not t.isdigit():
        raise ValueError(f"項目名からセッション名と t_ms が読めません: {name!r}")
    return session, int(t)


def parse_item(item: Mapping) -> EIItem:
    """1 項目。仮定: item["sample"] に name / metadata / label、item["classifications"][0]["result"][0] がクラス名 → 確率の辞書
    （または [{"label": ..., "value": ...}] の並び）。1 窓 1 項目なので classifications と result は 1 つずつのはず（複数なら止まる）。"""
    sample = item.get("sample", item)
    metadata = sample.get("metadata") or {}
    if "session" in metadata and "t_ms" in metadata:
        session, t_ms = str(metadata["session"]), int(metadata["t_ms"])
    else:
        session, t_ms = _name_to_session_t_ms(str(sample.get("name", "")))
    label = sample.get("label")
    cls = item.get("classifications")
    if not isinstance(cls, list) or len(cls) != 1:
        raise ValueError(f"classifications が 1 つではありません（{session}_{t_ms}）: {type(cls).__name__} "
                         f"{len(cls) if isinstance(cls, list) else ''}")
    res = cls[0].get("result")
    if not isinstance(res, list) or len(res) != 1:
        raise ValueError(f"classifications[0].result が 1 窓ではありません（{session}_{t_ms}）")
    r0 = res[0]
    if isinstance(r0, Mapping) and "label" in r0 and "value" in r0:
        probs = {str(r0["label"]): float(r0["value"])}
    elif isinstance(r0, Mapping):
        probs = {str(k): float(v) for k, v in r0.items() if isinstance(v, (int, float))}
    else:
        raise ValueError(f"result の形が読めません（{session}_{t_ms}）")
    if not probs:
        raise ValueError(f"確率が読めません（{session}_{t_ms}）")
    return EIItem(session=session, t_ms=t_ms, label=str(label) if label is not None else None, probs=probs)


def parse_page(body: str) -> list[EIItem]:
    doc = json.loads(body)
    if not isinstance(doc, dict) or not doc.get("success", True):
        raise ValueError(f"応答が success ではありません: {str(doc.get('error', ''))[:200] if isinstance(doc, dict) else '形が違う'}")
    items = doc.get("result")
    if not isinstance(items, list):
        raise ValueError("応答に result（項目の並び）がありません")
    return [parse_item(it) for it in items]


def fetch_all(fetch: FetchFn, project_id: str, api_key: str, variant: str, limit: int = PAGE_LIMIT,
              impulse_id: str | None = None) -> list[EIItem]:
    """limit / offset で全ページを取る。1 ページの項目が limit 未満（または 0）になったら終わり。"""
    headers = {"x-api-key": api_key}
    out: list[EIItem] = []
    for page in range(MAX_PAGES):
        url = page_url(project_id, variant, limit, page * limit, impulse_id)
        status, body = fetch(url, headers)
        if not 200 <= status < 300:
            raise SystemExit(f"classify/page（{variant}、offset {page * limit}）が HTTP {status}: "
                             f"{_redact(body, api_key, project_id)[:200]!r}")
        try:
            items = parse_page(body)
        except ValueError as e:
            raise SystemExit(f"classify/page（{variant}、offset {page * limit}）の応答が読めません（parse_item の項目名を実際の応答に合わせる）: "
                             f"{_redact(str(e), api_key, project_id)}")
        out.extend(items)
        if len(items) < limit:
            return out
    raise SystemExit(f"ページが {MAX_PAGES} を超えました（最後のページを見つけられない）")


# --- PC 側 ---

def pc_items(root: Path, scorer: m2_scorer.Scorer, extract=None) -> list[PCItem]:
    """収集日4 の 7 本を採点し、投入した窓（valid かつ境目でない窓）の全クラスの確率を (session, t_ms) 付きで返す
    （m2_threshold.load_and_score と同じ経路: features_m2.extract_session → 実行ファイル。収集日4 のファイルを開く）。"""
    extract = extract or features_m2.extract_session
    _, eval_names = ei_upload.check_split(Path(root))
    out = []
    for name in eval_names:
        d = Path(root) / name
        f = extract(d)
        labels3 = ei_upload.window_labels3(f.t_start_s, evaluate.load_events(d, "s"), evaluate.load_events(d, "c"))
        probs = scorer.score_rows(f.X.tolist())
        for k in np.nonzero(f.valid & (labels3 != ei_upload.UNUSED))[0]:
            t_ms = int(round(float(f.t_start_s[k]) * 1000))
            out.append(PCItem(session=name, t_ms=t_ms, label=m2_threshold.LABEL3_NAME[int(labels3[k])],
                              probs={lab: float(probs[k, i]) for i, lab in enumerate(scorer.labels)}))
    return out


def read_threshold(header_path: Path = THRESHOLD_HEADER_PATH) -> float:
    """firmware/detector/m2_threshold.h の M2_THRESHOLD。"""
    header_path = Path(header_path)
    if not header_path.is_file():
        raise SystemExit(f"{header_path} がありません（先に m2_threshold.py --freeze で凍結する）")
    m = re.search(r"static const float M2_THRESHOLD = ([0-9.eE+-]+)f;", header_path.read_text(encoding="utf-8"))
    if m is None:
        raise SystemExit(f"{header_path} に M2_THRESHOLD がありません")
    return float(m.group(1))


# --- 突き合わせ ---

def _argmax(probs: Mapping[str, float]) -> str:
    return max(probs.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _confusion(pairs) -> list:
    m = np.zeros((len(CLASS_ORDER), len(CLASS_ORDER)), dtype=int)
    for true, pred in pairs:
        m[CLASS_ORDER.index(true), CLASS_ORDER.index(pred)] += 1
    return m.tolist()


def compare(ei: Sequence[EIItem], pc: Sequence[PCItem], threshold: float, swallow: str = "swallow") -> dict:
    """int8 同士の突き合わせ。閾値の比較は float32（docs/decisions/0021）。"""
    f32 = m2_threshold.f32
    ei_by = {(e.session, e.t_ms): e for e in ei}
    pc_by = {(p.session, p.t_ms): p for p in pc}
    if len(ei_by) != len(ei) or len(pc_by) != len(pc):
        raise SystemExit(f"(session, t_ms) が重複しています: EI {len(ei) - len(ei_by)}、PC {len(pc) - len(pc_by)}")
    common = sorted(set(ei_by) & set(pc_by))
    max_abs = {c: 0.0 for c in CLASS_ORDER}
    n_gt0 = n_gt_step = n_argmax = n_thr = 0
    label_mismatch = 0
    pairs_ei, pairs_pc = [], []
    thr = f32(threshold)
    for key in common:
        e, p = ei_by[key], pc_by[key]
        if set(e.probs) != set(CLASS_ORDER) or set(p.probs) != set(CLASS_ORDER):
            raise SystemExit(f"クラス名が {CLASS_ORDER} と違います: EI {sorted(e.probs)}、PC {sorted(p.probs)}")
        d = {c: abs(e.probs[c] - p.probs[c]) for c in CLASS_ORDER}
        for c in CLASS_ORDER:
            max_abs[c] = max(max_abs[c], d[c])
        if max(d.values()) > 0:
            n_gt0 += 1
        if max(d.values()) > QUANT_STEP:
            n_gt_step += 1
        if _argmax(e.probs) != _argmax(p.probs):
            n_argmax += 1
        if (f32(e.probs[swallow]) >= thr) != (f32(p.probs[swallow]) >= thr):
            n_thr += 1
        true = p.label
        if e.label is not None and e.label != p.label:
            label_mismatch += 1
        pairs_ei.append((true, _argmax(e.probs)))
        pairs_pc.append((true, _argmax(p.probs)))
    conf_ei, conf_pc = _confusion(pairs_ei), _confusion(pairs_pc)
    return {"n_ei": len(ei_by), "n_pc": len(pc_by), "n_common": len(common),
            "missing_in_ei": len(set(pc_by) - set(ei_by)), "missing_in_pc": len(set(ei_by) - set(pc_by)),
            "max_abs": max_abs, "n_gt0": n_gt0, "n_gt_step": n_gt_step, "n_argmax": n_argmax, "n_threshold": n_thr,
            "label_mismatch": label_mismatch, "confusion_ei": conf_ei, "confusion_pc": conf_pc,
            "confusion_equal": conf_ei == conf_pc, "threshold": threshold,
            "match": n_argmax == 0 and n_thr == 0 and all(v == 0.0 for v in max_abs.values())
                     and len(common) == len(ei_by) == len(pc_by)}


def compare_float32(ei_f32: Sequence[EIItem], pc: Sequence[PCItem]) -> dict:
    """参考: float32（EI）と int8（PC）の max |Δp|（量子化の影響の大きさ）。"""
    pc_by = {(p.session, p.t_ms): p for p in pc}
    max_abs = {c: 0.0 for c in CLASS_ORDER}
    n = 0
    for e in ei_f32:
        p = pc_by.get((e.session, e.t_ms))
        if p is None:
            continue
        n += 1
        for c in CLASS_ORDER:
            max_abs[c] = max(max_abs[c], abs(e.probs[c] - p.probs[c]))
    return {"n_common": n, "max_abs": max_abs}


# --- 報告 ---

def _matrix_lines(title: str, m: list) -> list[str]:
    out = [f"{title}", "| 正解 \\ argmax | " + " | ".join(CLASS_ORDER) + " |", "|---|" + "---|" * len(CLASS_ORDER)]
    for i, name in enumerate(CLASS_ORDER):
        out.append(f"| {name} | " + " | ".join(str(v) for v in m[i]) + " |")
    return out


def format_report(argv: Sequence[str], commit: str, deploy_version: int, c: dict, cf: dict | None) -> str:
    """argv はプロジェクト ID を伏せたもの（run が置き換える）。"""
    out = ["# EI の Model testing（int8）と PC 上の実行ファイルの突き合わせ（収集日4、投入した窓）", "",
           "## 実行の条件",
           f"- コマンドライン: `ei_testing_result.py {' '.join(argv)}`（プロジェクト ID は書かない）",
           f"- コミット: {commit}",
           f"- 実行ファイルの model: deploy version {deploy_version}（`model_metadata.h` の値のとおり）",
           f"- 閾値: {c['threshold']:.2f}（float32 {m2_threshold.f32(c['threshold']):.9g}。`m2_threshold.h`）",
           "- 応答はメモリ上で突き合わせ、ファイルに書かない。項目ごとの確率は出さない",
           "",
           "## 窓の数",
           f"- EI（variant=int8）: {c['n_ei']}、PC: {c['n_pc']}、対応づいた窓: {c['n_common']}"
           f"（PC にあって EI に無い {c['missing_in_ei']}、EI にあって PC に無い {c['missing_in_pc']}）",
           f"- EI のラベルと PC のラベル（window_labels3）が違う窓: {c['label_mismatch']}",
           "",
           "## int8 同士の差",
           "- max |p_EI − p_PC|: " + "、".join(f"{k} {v:.9g}" for k, v in c["max_abs"].items()),
           f"- |Δp| > 0 の窓: {c['n_gt0']}、|Δp| > 1/256 の窓: {c['n_gt_step']}",
           f"- argmax が違う窓: {c['n_argmax']}",
           f"- 閾値 {c['threshold']:.2f} で swallow の陽性・陰性が違う窓: {c['n_threshold']}",
           f"- 3 × 3 の混同行列（argmax、uncertain 無し）の一致: {'一致' if c['confusion_equal'] else '不一致'}",
           ""]
    out += _matrix_lines("EI（int8）:", c["confusion_ei"]) + [""]
    out += _matrix_lines("PC（int8）:", c["confusion_pc"]) + [""]
    if cf is not None:
        out += ["## 参考: float32（EI）と int8（PC）",
                f"- 対応づいた窓 {cf['n_common']}、max |Δp|: " + "、".join(f"{k} {v:.9g}" for k, v in cf["max_abs"].items()), ""]
    out += ["## 判断（決定 F1: argmax の不一致 0、閾値での不一致 0、int8 同士の max |Δp| = 0 のときだけ「一致」）",
            f"- {'一致' if c['match'] else '差あり（窓の数と max |Δp| を log に書き、原因の切り分けを人に渡す。閾値は動かさない）'}",
            "- 注: Studio の Model testing の表示は信頼度 0.6 未満を uncertain に分けるので、上の行列は表示と同じ形ではない", ""]
    return "\n".join(out)


# --- 本体 ---

def run(argv: Sequence[str], *, fetch: FetchFn = fetch_urllib, environ: Mapping[str, str] | None = None,
        scorer_bin: Path | str | None = None, norm_path: Path = m2_scorer.NORM_PATH,
        threshold_header_path: Path = THRESHOLD_HEADER_PATH, extract=None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", type=Path, help="data/raw")
    ap.add_argument("--project-id", required=True, help="EI のプロジェクト ID（出力に出さない）")
    ap.add_argument("--impulse-id", default=None, help="impulse が複数あるときだけ")
    ap.add_argument("--limit", type=int, default=PAGE_LIMIT, help="1 ページの項目数")
    a = ap.parse_args(argv)
    environ = os.environ if environ is None else environ
    api_key = environ.get(API_KEY_ENV, "")
    if not api_key:
        raise SystemExit(f"環境変数 {API_KEY_ENV} がありません")
    if not a.project_id.isdigit():
        raise SystemExit("--project-id は数字です")
    if not a.root.is_dir():
        raise SystemExit(f"フォルダが見つかりません: {a.root}")

    scorer = m2_scorer.Scorer(scorer_bin, norm_path=Path(norm_path))
    if str(scorer.model[0]) != a.project_id:
        raise SystemExit("--project-id が実行ファイルの見出し（EI_CLASSIFIER_PROJECT_ID）と一致しません（値は出さない）")
    threshold = read_threshold(threshold_header_path)

    pc = pc_items(a.root, scorer, extract)
    ei_int8 = fetch_all(fetch, a.project_id, api_key, "int8", a.limit, a.impulse_id)
    if len(ei_int8) != len(pc):
        raise SystemExit(f"EI の項目数 {len(ei_int8)} が test set の投入数（PC の投入した窓 {len(pc)}）と一致しません")
    c = compare(ei_int8, pc, threshold)
    cf = None
    try:
        ei_f32 = fetch_all(fetch, a.project_id, api_key, "float32", a.limit, a.impulse_id)
        cf = compare_float32(ei_f32, pc)
    except SystemExit as e:
        cf = None
        print(f"（参考の float32 は取れなかった: {_redact(str(e), api_key, a.project_id)[:200]}）", file=sys.stderr)
    shown = ["<project-id>" if x == a.project_id else x for x in argv]
    print(format_report(shown, train_eval.git_commit(), scorer.deploy_version, c, cf))
    return c


def main(argv: Sequence[str] | None = None) -> None:
    run(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    main()
