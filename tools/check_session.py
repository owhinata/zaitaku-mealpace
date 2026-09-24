#!/usr/bin/env python3
"""記録済みセッションフォルダの取りこぼしを数える（docs/data-schema.md, Issue #7）。

記録ファームウェアのセッションでは imu.csv（t_ms, ax..gz）と audio_chunks.csv（t_ms, sample_index）を読む。
フレームに連番が無いので、取りこぼしは t_ms の飛び（1周期の1.5倍以上の差分）から
数える。IMU の1周期は、飛びでない t_ms の差分の平均（実測では約9.5ms。ファイルごとに
計算し直す。millis() は1ms刻みなので差分は9か10が混ざる）。音声はチャンク長から決まる。

検出器のセッション（docs/decisions/0021）では detect.csv・feat.csv の window_t_ms の飛び（公称 250 ms の
1.5 倍以上）から取りこぼしを推定し、t_ms の逆行・最大差分・送信時刻 − 窓の開始（t_ms − window_t_ms）・DETECT と
FEAT の窓の不一致も出す。positive・prob・led の集計は出さない（採否を決める前に陽性を数えない。
docs/recording-protocol.md）。実効レートと送信時刻 − 窓の開始は装置の送信時刻から出す目安で、PC への到着時刻は
record.py が記録しない。

判定の「取りこぼし率 1% 未満」は Issue #7 で置いた作業上の基準であり、
docs/evaluation.md の検出率・誤検出率の合格線とは別物。ここでは変えない。

XOR 不一致数（シリアルフレームの破損数）はこのファイルからは分からない。
tools/record.py の終了時の表示（「XOR 不一致数」）を見ること。

使い方: python tools/check_session.py <セッションフォルダ>
"""
from __future__ import annotations
import argparse
import csv
import sys
from collections import Counter
from pathlib import Path
from statistics import median

NOMINAL_IMU_HZ = 104.0
NOMINAL_AUDIO_HZ = 16000.0
NOMINAL_HOP_MS = 250.0      # 検出器の窓のホップ（docs/evaluation.md の 0.25 秒）
NOMINAL_WINDOW_MS = 1000.0  # 検出器の窓の長さ
NOMINAL_DETECT_HZ = 1000.0 / NOMINAL_HOP_MS
GAP_RATIO = 1.5  # 1周期のこの倍以上の差分を「飛び」とする
LOSS_RATE_LIMIT = 0.01  # Issue #7 の作業上の基準（docs/evaluation.md の定義ではない）
MAX_GAPS_SHOWN = 10


def load_rows(path: Path, min_cols: int) -> list[list[str]]:
    """CSV を読み、ヘッダーを除いたデータ行を返す。読めない/短すぎる場合は終了する。"""
    if not path.is_file():
        sys.exit(f"ファイルが見つかりません: {path}")
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        rows = [r for r in reader if r]
    if header is None:
        sys.exit(f"{path} が空です（ヘッダー行がありません）")
    for r in rows:
        if len(r) < min_cols:
            sys.exit(f"{path} に列が足りない行があります: {r}")
    if len(rows) < 2:
        sys.exit(f"{path} のデータ行が2行未満です（{len(rows)} 行）。差分を計算できません")
    return rows


def load_header(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8") as f:
        return next(csv.reader(f), [])


def analyze(t_ms: list[int], nominal_period_ms: float | None) -> dict:
    """t_ms の列から飛び・逆行を数える共通処理。

    nominal_period_ms が None なら「1周期」を差分の中央値から求める（IMU 用）。
    指定があれば、それをそのまま1周期として使う（音声のチャンク長から決まるため）。
    """
    n = len(t_ms)
    diffs = [t_ms[i] - t_ms[i - 1] for i in range(1, n)]
    if nominal_period_ms is not None:
        period = nominal_period_ms
    else:
        # 中央値は整数（9 か 10）に寄るので、飛びでない差分の平均を1周期にする。
        med = median(diffs)
        normal = [d for d in diffs if 0 <= d < GAP_RATIO * med]
        period = sum(normal) / len(normal) if normal else med
    if period <= 0:
        sys.exit("1周期の推定値が0以下になりました。t_ms の並びを確認してください")

    gaps: list[tuple[int, int]] = []
    lost = 0
    backwards = 0
    for i, d in enumerate(diffs, start=1):
        if d < 0:
            backwards += 1
        elif d >= GAP_RATIO * period:
            lost += round(d / period) - 1
            gaps.append((t_ms[i], d))

    loss_rate = lost / (n + lost) if (n + lost) > 0 else 0.0
    return {
        "n": n,
        "period": period,
        "diffs": diffs,
        "gaps": gaps,
        "lost": lost,
        "loss_rate": loss_rate,
        "max_diff": max(diffs),
        "backwards": backwards,
    }


def analyze_imu(rows: list[list[str]]) -> dict:
    t = [int(r[0]) for r in rows]
    result = analyze(t, nominal_period_ms=None)
    duration_s = (t[-1] - t[0]) / 1000.0
    effective_hz = (result["n"] - 1) / duration_s if duration_s > 0 else float("nan")
    result["duration_s"] = duration_s
    result["effective_hz"] = effective_hz
    result["hz_ratio"] = effective_hz / NOMINAL_IMU_HZ if duration_s > 0 else float("nan")
    return result


def analyze_audio(rows: list[list[str]]) -> dict:
    t = [int(r[0]) for r in rows]
    si = [int(r[1]) for r in rows]
    si_diffs = [si[i] - si[i - 1] for i in range(1, len(si))]
    chunk_len, _ = Counter(si_diffs).most_common(1)[0]
    if chunk_len <= 0:
        sys.exit("audio_chunks.csv の sample_index 差分の最頻値が0以下です")
    nominal_chunk_ms = chunk_len / NOMINAL_AUDIO_HZ * 1000.0

    result = analyze(t, nominal_period_ms=nominal_chunk_ms)
    result["chunk_len"] = chunk_len
    result["nominal_chunk_ms"] = nominal_chunk_ms
    result["total_samples"] = si[-1] + chunk_len
    result["mismatched_si"] = sum(1 for d in si_diffs if d != chunk_len)

    duration_s = (t[-1] - t[0] + nominal_chunk_ms) / 1000.0
    effective_hz = result["total_samples"] / duration_s if duration_s > 0 else float("nan")
    result["duration_s"] = duration_s
    result["effective_hz"] = effective_hz
    result["hz_ratio"] = effective_hz / NOMINAL_AUDIO_HZ if duration_s > 0 else float("nan")
    return result


def analyze_windows(rows: list[list[str]]) -> dict:
    """detect.csv / feat.csv の共通処理。列は t_ms, window_t_ms, ...。窓の飛びは window_t_ms で数える。

    positive・prob・led の列は読まない。
    """
    t = [int(r[0]) for r in rows]
    w = [int(r[1]) for r in rows]
    result = analyze(w, nominal_period_ms=NOMINAL_HOP_MS)
    result["windows"] = set(w)
    result["t_backwards"] = sum(1 for i in range(1, len(t)) if t[i] < t[i - 1])
    lag = sorted(ti - wi for ti, wi in zip(t, w))
    result["lag_min"], result["lag_median"], result["lag_max"] = lag[0], median(lag), lag[-1]
    # 記録の長さは detect.csv / feat.csv だけの値（評価の範囲は両方の送信時刻を含む。docs/decisions/0021）。目安
    end_ms = max(max(t), max(w) + int(NOMINAL_WINDOW_MS))
    result["duration_s"] = (end_ms - w[0]) / 1000.0
    span_s = (w[-1] - w[0]) / 1000.0   # 実効レートは窓の開始の間隔から（公称 4 Hz と比べるため）
    effective_hz = (result["n"] - 1) / span_s if span_s > 0 else float("nan")
    result["effective_hz"] = effective_hz
    result["hz_ratio"] = effective_hz / NOMINAL_DETECT_HZ if span_s > 0 else float("nan")
    return result


def analyze_session(session_dir: Path) -> dict:
    """セッションフォルダにあるファイルを全部見る。無いストリームは None。"""
    has = {name: (session_dir / name).is_file()
           for name in ("imu.csv", "audio_chunks.csv", "detect.csv", "feat.csv")}
    if not any(has.values()):
        sys.exit(f"imu.csv か detect.csv が要る: {session_dir}")
    result = {"session_dir": session_dir, "imu": None, "audio": None, "detect": None, "feat": None, "notes": []}
    if has["imu.csv"] and has["detect.csv"]:
        result["notes"].append("注意: imu.csv と detect.csv が同じセッションにある（想定外の組み合わせ。両方見る）")
    if has["imu.csv"] or has["audio_chunks.csv"]:
        # 記録ファームウェアのセッション。今までどおり両方を要求する
        imu_rows = load_rows(session_dir / "imu.csv", min_cols=1)
        audio_rows = load_rows(session_dir / "audio_chunks.csv", min_cols=2)
        result["imu"] = analyze_imu(imu_rows)
        result["audio"] = analyze_audio(audio_rows)
    if has["detect.csv"]:
        result["detect"] = analyze_windows(load_rows(session_dir / "detect.csv", min_cols=5))
    if has["feat.csv"]:
        header = load_header(session_dir / "feat.csv")
        feat = analyze_windows(load_rows(session_dir / "feat.csv", min_cols=2))
        feat["n_dims"] = sum(1 for h in header[2:] if h.startswith("f"))
        if result["detect"] is not None:
            feat["only_in_detect"] = len(result["detect"]["windows"] - feat["windows"])
            feat["only_in_feat"] = len(feat["windows"] - result["detect"]["windows"])
        result["feat"] = feat
    return result


def gap_lines(label: str, gaps: list[tuple[int, int]]) -> list[str]:
    if not gaps:
        return []
    shown = gaps[:MAX_GAPS_SHOWN]
    out = [f"  {label}の飛び（先頭 {len(shown)}/{len(gaps)} 箇所。t_ms と差分[ms]）:"]
    out += [f"    t_ms={t_ms} diff={d}" for t_ms, d in shown]
    return out


def _window_lines(label: str, r: dict) -> list[str]:
    out = [f"  窓の数: {r['n']}"]
    if "n_dims" in r:
        out.append(f"  次元数 N: {r['n_dims']}")
    out += [
        f"  記録の長さ（{label} だけの値。目安）: {r['duration_s']:.3f} 秒",
        f"  実効レート（装置の送信時刻から。窓の開始の間隔）: {r['effective_hz']:.2f} 窓/秒"
        f"（公称 4 Hz 比 {r['hz_ratio'] * 100:.1f}%）",
        f"  飛び: {len(r['gaps'])} 箇所 / 失われた窓数(推定): {r['lost']} / 取りこぼし率: {r['loss_rate'] * 100:.3f}%",
        f"  window_t_ms の最大差分: {r['max_diff']} ms / window_t_ms の逆行: {r['backwards']} 箇所 "
        f"/ t_ms の逆行: {r['t_backwards']} 箇所",
        f"  送信時刻 − 窓の開始（t_ms − window_t_ms）: 最小 {r['lag_min']} / 中央値 {r['lag_median']:g} "
        f"/ 最大 {r['lag_max']} ms",
    ]
    if "only_in_detect" in r:
        out.append(f"  DETECT との window_t_ms の不一致: {r['only_in_detect'] + r['only_in_feat']} 窓"
                   f"（DETECT にだけある {r['only_in_detect']} / FEAT にだけある {r['only_in_feat']}）")
    out += gap_lines(label, [(t, d) for t, d in r["gaps"]])
    return out


def format_report(result: dict) -> list[str]:
    """表示する行。最後の要素は判定の節。"""
    out = [f"=== {result['session_dir']} ==="]
    out += result["notes"]
    imu, audio, detect, feat = result["imu"], result["audio"], result["detect"], result["feat"]
    if imu is not None:
        out += ["[IMU]",
                f"  行数: {imu['n']}",
                f"  記録の長さ: {imu['duration_s']:.3f} 秒",
                f"  実効レート: {imu['effective_hz']:.2f} Hz（公称 104 Hz 比 {imu['hz_ratio'] * 100:.1f}%）",
                f"  1周期（飛びでない t_ms 差分の平均）: {imu['period']:.2f} ms",
                f"  飛び: {len(imu['gaps'])} 箇所 / 失われた行数(推定): {imu['lost']} "
                f"/ 取りこぼし率: {imu['loss_rate'] * 100:.3f}%",
                f"  最大差分: {imu['max_diff']} ms / t_ms の逆行: {imu['backwards']} 箇所"]
        out += gap_lines("IMU", imu["gaps"])
    if audio is not None:
        out += ["[音声]",
                f"  チャンク数: {audio['n']}",
                f"  総サンプル数(推定): {audio['total_samples']}"
                f"（1チャンク {audio['chunk_len']} サンプル、公称 {audio['nominal_chunk_ms']:.3f} ms）",
                f"  飛び: {len(audio['gaps'])} 箇所 / 失われたチャンク数(推定): {audio['lost']} "
                f"/ 取りこぼし率: {audio['loss_rate'] * 100:.3f}%",
                f"  最大差分: {audio['max_diff']} ms / t_ms の逆行: {audio['backwards']} 箇所",
                f"  sample_index の差分が最頻値と違う箇所: {audio['mismatched_si']} 箇所",
                f"  実効サンプルレート: {audio['effective_hz']:.1f} Hz（公称 16000 Hz 比 {audio['hz_ratio'] * 100:.2f}%）"]
        out += gap_lines("音声", audio["gaps"])
    if detect is not None:
        out += ["[DETECT]"] + _window_lines("DETECT", detect)
    if feat is not None:
        out += ["[FEAT]"] + _window_lines("FEAT", feat)

    out += ["", "[判定] 取りこぼし率 1%未満を「基準内」とする（Issue #7 の作業上の基準。docs/evaluation.md の合格線ではない）"]
    for label, r in (("IMU:   ", imu), ("音声:  ", audio), ("DETECT:", detect), ("FEAT:  ", feat)):
        if r is not None:
            ok = r["loss_rate"] < LOSS_RATE_LIMIT
            out.append(f"  {label}{'基準内' if ok else '基準外'}（{r['loss_rate'] * 100:.3f}%）")
    out.append("  ※ XOR 不一致数はここでは分からない。tools/record.py の終了時の表示を見ること。")
    return out


def all_within_limit(result: dict) -> bool:
    streams = [r for r in (result["imu"], result["audio"], result["detect"], result["feat"]) if r is not None]
    return all(r["loss_rate"] < LOSS_RATE_LIMIT for r in streams)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session_dir", type=Path,
                    help="セッションフォルダ（imu.csv と audio_chunks.csv、または detect.csv・feat.csv を含む）")
    a = ap.parse_args(argv)

    if not a.session_dir.is_dir():
        sys.exit(f"セッションフォルダが見つかりません: {a.session_dir}")

    result = analyze_session(a.session_dir)
    for line in format_report(result):
        print(line)
    sys.exit(0 if all_within_limit(result) else 1)


if __name__ == "__main__":
    main()
