#!/usr/bin/env python3
"""装置（UNO Q の detector.py）の再生モードの出力と、PC の再計算の一致を確かめる
（Issue #47、plan #47 の 8 節）。

使い方（リポジトリの root から）:
  .venv/bin/python tools/unoq_replay_check.py data/raw/<学習用 meal> [--host H] [--feat]

対象は docs/decisions/0037 の M4-TRAIN にある学習用 meal（fw = pc-throat）だけ。
それ以外の名前は、フォルダの中を開く前に止まる（評価側を開かない）。
throat.wav のフレームを ssh の標準入力に S16_LE・2 ch のまま流し、
`detector.py --replay --no-rpc [--feat]` の標準出力の行をメモリに集める。
PC では同じ窓の格子（入力の先頭から 16 kHz の 4000 サンプルごと。装置が作れる窓
だけ）で、throat_check.to_analysis（decimate3）→ / 32768 → float32 →
features_v2.mfe_one → m4_infer.model_prob → postprocess.post_mask →
led_rule.led_states を計算し直す（FEAT は features_np.mfe_and_feat）。
比べるもの: 窓の数と window_t_ms の集合、prob の差の最大（1e-5 以下）、
positive・led の不一致の数（0）、FEAT の差（|Δ| ≤ 1e-6 + 1e-5 × |PC の値|）。

ファイルを書かない（PC にも UNO Q にも）。表示は集計だけ（窓の数、差の最大、
不一致の数、所要時間、装置の proc_ms の中央値・最大）。prob の値・陽性の数は
出さない。
"""

from __future__ import annotations
import argparse, json, statistics, subprocess, sys, threading, time, wave
from pathlib import Path

import numpy as np

import record

PROB_TOL = 1e-5
FEAT_ATOL, FEAT_RTOL = 1e-6, 1e-5
WINDOW, HOP16, DECIM, HALF = 16000, 4000, 3, 30
CHUNK_FRAMES = 6000


def modules():
    fixed_eval, m4_infer, features_np = record.analysis_modules()
    import features_v2, led_rule, postprocess  # noqa: E401
    import throat_check

    return {
        "fixed_eval": fixed_eval,
        "m4_infer": m4_infer,
        "features_np": features_np,
        "features_v2": features_v2,
        "led_rule": led_rule,
        "postprocess": postprocess,
        "throat_check": throat_check,
    }


def device_windows(n_in: int) -> int:
    """48 kHz の n_in フレームから装置が作れる窓の数。窓 k は 16 kHz の
    [4000 k, 4000 k + 16000) で、その最後のサンプルには入力 3 × (4000 k +
    15999) + 30 までが要る（features_np.Decimate3 の遅れ）。"""
    last = n_in - 1 - HALF
    if last < 0:
        return 0
    n16 = last // DECIM + 1  # 装置が出せる 16 kHz のサンプル数
    if n16 < WINDOW:
        return 0
    return (n16 - WINDOW) // HOP16 + 1


def recompute(wav: np.ndarray, model: dict, feat: bool = False) -> dict:
    """PC の再計算。wav は int16 の (N, 2)。窓の格子は装置と同じ（anchor 0）。"""
    m = modules()
    x16 = m["throat_check"].to_analysis({"fs": 48000, "wav": wav})
    x = (x16 / 32768.0).astype(np.float32)
    n = device_windows(len(wav))
    segs = [x[HOP16 * k : HOP16 * k + WINDOW] for k in range(n)]
    M = (
        np.stack([m["features_v2"].mfe_one(s) for s in segs])
        if n
        else np.zeros((0, 98, 40), np.float32)
    )
    prob = m["m4_infer"].model_prob(model, M) if n else np.zeros(0)
    cfg = model["cfg"]
    w = [250 * k for k in range(n)]
    if n:
        pos = m["postprocess"].post_mask(
            prob,
            np.ones(n, bool),
            0.25 * np.arange(n),
            cfg["enter"],
            cfg["exit"],
            cfg["cap_s"],
            cfg["refractory_s"],
        )
    else:
        pos = np.zeros(0, bool)
    led = m["led_rule"].led_states(w, pos.astype(int))
    out = {
        "window_t_ms": w,
        "prob": prob,
        "positive": pos.astype(int),
        "led": np.asarray(led, int),
    }
    if feat:
        out["feat"] = (
            np.stack([m["features_np"].mfe_and_feat(s)[1] for s in segs])
            if n
            else np.zeros((0, 82))
        )
    return out


def collect(lines) -> dict:
    """detector.py の標準出力の行（文字列の並び）を種類ごとに集める。"""
    out = {"H": None, "E": None, "D": [], "T": [], "F": {}, "bad": 0}
    for ln in lines:
        p = record.parse_detector_line(ln)
        if p is None:
            out["bad"] += 1
        elif p[0] in ("H", "E"):
            out[p[0]] = p[1]
        elif p[0] == "D":
            out["D"].append(p[1:])
        elif p[0] == "T":
            out["T"].append(p[1:])
        else:
            out["F"][p[1]] = [float(v) for v in p[2]]
    return out


def compare(dev: dict, pc: dict, feat: bool = False) -> dict:
    """集計だけを返す（値の並びは返さない）。"""
    d_w = [r[0] for r in dev["D"]]
    res = {
        "windows_device": len(d_w),
        "windows_pc": len(pc["window_t_ms"]),
        "same_windows": d_w == list(pc["window_t_ms"]),
        "bad_lines": dev["bad"],
    }
    if not res["same_windows"]:
        res["ok"] = False
        return res
    p = np.array([float(r[2]) for r in dev["D"]])
    pos = np.array([r[3] for r in dev["D"]], int)
    led = np.array([r[4] for r in dev["D"]], int)
    res["prob_max_diff"] = (
        float(np.abs(p - pc["prob"]).max()) if len(p) else 0.0
    )
    res["positive_mismatch"] = int((pos != pc["positive"]).sum())
    res["led_mismatch"] = int((led != pc["led"]).sum())
    ok = (
        res["prob_max_diff"] <= PROB_TOL
        and res["positive_mismatch"] == 0
        and res["led_mismatch"] == 0
        and dev["bad"] == 0
    )
    if feat:
        f_w = sorted(dev["F"])
        res["feat_same_windows"] = f_w == d_w
        if res["feat_same_windows"] and d_w:
            F = np.array([dev["F"][w] for w in d_w])
            ref = pc["feat"]
            excess = np.abs(F - ref) - (FEAT_ATOL + FEAT_RTOL * np.abs(ref))
            res["feat_max_abs_diff"] = float(np.abs(F - ref).max())
            res["feat_over_tol"] = int((excess > 0).sum())
        else:
            res["feat_over_tol"] = -1
        ok = ok and res["feat_same_windows"] and res["feat_over_tol"] == 0
    proc = [r[1] / 1000.0 for r in dev["T"]]
    if proc:
        res["proc_ms_median"] = round(statistics.median(proc), 3)
        res["proc_ms_max"] = round(max(proc), 3)
        res["proc_ms"] = record._quantiles(proc)
        res["lat_ms"] = record._quantiles([r[2] / 1000.0 for r in dev["T"]])
    # 再生では入力が実時間より速く届くので、e2e_ms は実時間の遅れではない
    # （t_ms − (window_t_ms + 1000)。窓の時刻は標本の数だけで決まる）
    e2e = [r[1] - (r[0] + 1000) for r in dev["D"]]
    if e2e:
        res["e2e_ms"] = record._quantiles(e2e)
    res["ok"] = bool(ok)
    return res


def format_result(res: dict, elapsed_s: float | None = None) -> list[str]:
    out = [
        f"窓の数: 装置 {res['windows_device']}、PC {res['windows_pc']}"
        f"（window_t_ms の集合 {'同じ' if res['same_windows'] else '違う'}）",
        f"読めない行: {res['bad_lines']}",
    ]
    if "prob_max_diff" in res:
        out.append(
            f"prob の差の最大: {res['prob_max_diff']:.2e}（許容 {PROB_TOL:g}）"
            f" / positive の不一致 {res['positive_mismatch']}"
            f" / led の不一致 {res['led_mismatch']}"
        )
    if "feat_over_tol" in res:
        out.append(
            f"FEAT: 窓の集合 {'同じ' if res.get('feat_same_windows') else '違う'}"
            f" / 差の最大 {res.get('feat_max_abs_diff', float('nan')):.2e}"
            f" / 許容を超えた値 {res['feat_over_tol']}"
        )

    def q(d):
        return (
            f"中央値 {d['median']:.1f}・95 パーセンタイル {d['p95']:.1f}・"
            f"最大 {d['max']:.1f} ms"
        )

    if "proc_ms" in res:
        out.append(f"装置の proc_ms（RPC なし）: {q(res['proc_ms'])}")
        out.append(f"装置の lat_ms: {q(res['lat_ms'])}")
    if "e2e_ms" in res:
        out.append(
            f"e2e_ms（再生。入力が実時間より速いので参考）: {q(res['e2e_ms'])}"
        )
    if elapsed_s is not None:
        out.append(f"所要時間: {elapsed_s:.1f} 秒")
    out.append(f"一致: {'OK' if res['ok'] else 'NG'}")
    return out


def check_target(path: Path, m: dict) -> None:
    """名前が M4-TRAIN の学習用 meal でなければ、中を開く前に止まる。"""
    fe = m["fixed_eval"]
    name = path.name
    if fe.M4_EXPECTED_TRAIN is None or name not in fe.M4_EXPECTED_TRAIN:
        raise SystemExit(f"0037 の M4-TRAIN に無い名前です（開かない）: {name}")
    if not name.endswith("_meal"):
        raise SystemExit(f"学習用 meal ではありません（開かない）: {name}")
    meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    if meta.get("fw") != "pc-throat":
        raise SystemExit(f"fw が pc-throat ではありません: {name}")


def stream_wav(proc, path: Path, err: list) -> None:
    """throat.wav のフレームをそのまま標準入力に流す（別のスレッド）。"""
    try:
        with wave.open(str(path), "rb") as w:
            if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (
                2,
                2,
                48000,
            ):
                raise ValueError("throat.wav が 48 kHz・2 ch・16 bit ではない")
            while True:
                b = w.readframes(CHUNK_FRAMES)
                if not b:
                    break
                proc.stdin.write(b)
    except (OSError, ValueError) as e:
        err.append(f"{type(e).__name__}: {e}")
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("session", type=Path)
    ap.add_argument("--host", default=record.THROAT_DEFAULT_HOST)
    ap.add_argument("--feat", action="store_true")
    a = ap.parse_args(argv)
    m = modules()
    check_target(a.session, m)
    exp = record.expected_unoq()
    args = record.detector_args(None, a.feat, True, exp, mode="--replay")
    argv_ssh = record.unoq_detector_argv(a.host, args)
    t0 = time.monotonic()
    proc = subprocess.Popen(
        argv_ssh,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    err: list = []
    errlines: list = []

    def read_stderr():
        for raw in proc.stderr:
            errlines.append(raw.decode("utf-8", "replace").rstrip())
            del errlines[:-8]

    th = threading.Thread(
        target=stream_wav,
        args=(proc, a.session / "throat.wav", err),
        daemon=True,
    )
    th_err = threading.Thread(target=read_stderr, daemon=True)
    th.start()
    th_err.start()
    out = proc.stdout.read()  # 行はメモリに集めるだけ（書かない）
    proc.wait()
    th.join(timeout=5)
    th_err.join(timeout=5)
    elapsed = time.monotonic() - t0
    if err or proc.returncode != 0:
        tail = "\n".join(errlines)
        print(
            f"検出器が正常に終わりませんでした（終了コード {proc.returncode}）{err}"
        )
        print(tail)
        return 1
    dev = collect(out.decode("utf-8", "replace").splitlines())
    del out
    h = dev["H"] or {}
    if record.header_error(h, exp, a.feat, True, input_kind="replay"):
        print("H 行が期待と違います（deploy-unoq してから）")
        return 1
    wav = m["throat_check"].load_session(a.session)["wav"]
    model = m["m4_infer"].load_model(record.REPO_DIR / exp["model"]["path"])
    pc = recompute(wav, model, a.feat)
    del wav
    res = compare(dev, pc, a.feat)
    for ln in format_result(res, elapsed):
        print(ln)
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
