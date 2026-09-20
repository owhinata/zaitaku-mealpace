#!/usr/bin/env python3
"""記録済みセッションフォルダの取りこぼしを数える（docs/data-schema.md, Issue #7）。

読むのは imu.csv（t_ms, ax..gz）と audio_chunks.csv（t_ms, sample_index）だけ。
フレームに連番が無いので、取りこぼしは t_ms の飛び（1周期の1.5倍以上の差分）から
数える。IMU の1周期は、飛びでない t_ms の差分の平均（実測では約9.5ms。ファイルごとに
計算し直す。millis() は1ms刻みなので差分は9か10が混ざる）。音声はチャンク長から決まる。

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


def print_gaps(label: str, gaps: list[tuple[int, int]]) -> None:
    if not gaps:
        return
    shown = gaps[:MAX_GAPS_SHOWN]
    print(f"  {label}の飛び（先頭 {len(shown)}/{len(gaps)} 箇所。t_ms と差分[ms]）:")
    for t_ms, d in shown:
        print(f"    t_ms={t_ms} diff={d}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session_dir", type=Path, help="セッションフォルダ（imu.csv, audio_chunks.csv を含む）")
    a = ap.parse_args()

    if not a.session_dir.is_dir():
        sys.exit(f"セッションフォルダが見つかりません: {a.session_dir}")

    imu_rows = load_rows(a.session_dir / "imu.csv", min_cols=1)
    audio_rows = load_rows(a.session_dir / "audio_chunks.csv", min_cols=2)

    imu = analyze_imu(imu_rows)
    audio = analyze_audio(audio_rows)

    print(f"=== {a.session_dir} ===")

    print("[IMU]")
    print(f"  行数: {imu['n']}")
    print(f"  記録の長さ: {imu['duration_s']:.3f} 秒")
    print(f"  実効レート: {imu['effective_hz']:.2f} Hz（公称 104 Hz 比 {imu['hz_ratio'] * 100:.1f}%）")
    print(f"  1周期（飛びでない t_ms 差分の平均）: {imu['period']:.2f} ms")
    print(
        f"  飛び: {len(imu['gaps'])} 箇所 / 失われた行数(推定): {imu['lost']} "
        f"/ 取りこぼし率: {imu['loss_rate'] * 100:.3f}%"
    )
    print(f"  最大差分: {imu['max_diff']} ms / t_ms の逆行: {imu['backwards']} 箇所")
    print_gaps("IMU", imu["gaps"])

    print("[音声]")
    print(f"  チャンク数: {audio['n']}")
    print(
        f"  総サンプル数(推定): {audio['total_samples']}"
        f"（1チャンク {audio['chunk_len']} サンプル、公称 {audio['nominal_chunk_ms']:.3f} ms）"
    )
    print(
        f"  飛び: {len(audio['gaps'])} 箇所 / 失われたチャンク数(推定): {audio['lost']} "
        f"/ 取りこぼし率: {audio['loss_rate'] * 100:.3f}%"
    )
    print(f"  最大差分: {audio['max_diff']} ms / t_ms の逆行: {audio['backwards']} 箇所")
    print(f"  sample_index の差分が最頻値と違う箇所: {audio['mismatched_si']} 箇所")
    print(f"  実効サンプルレート: {audio['effective_hz']:.1f} Hz（公称 16000 Hz 比 {audio['hz_ratio'] * 100:.2f}%）")
    print_gaps("音声", audio["gaps"])

    imu_ok = imu["loss_rate"] < LOSS_RATE_LIMIT
    audio_ok = audio["loss_rate"] < LOSS_RATE_LIMIT
    print()
    print("[判定] 取りこぼし率 1%未満を「基準内」とする（Issue #7 の作業上の基準。docs/evaluation.md の合格線ではない）")
    print(f"  IMU:   {'基準内' if imu_ok else '基準外'}（{imu['loss_rate'] * 100:.3f}%）")
    print(f"  音声:  {'基準内' if audio_ok else '基準外'}（{audio['loss_rate'] * 100:.3f}%）")
    print("  ※ XOR 不一致数はここでは分からない。tools/record.py の終了時の表示を見ること。")

    sys.exit(0 if (imu_ok and audio_ok) else 1)


if __name__ == "__main__":
    main()
