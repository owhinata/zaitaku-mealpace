"""MCU の LED マトリクスの表示を Linux 側から切り替える確かめ（Issue #38）。

UNO Q の上で:
python3 -B ~/zaitaku-mealpace-device/led_check.py wave|smile|off|bench

- wave: 流れる波線（状態 1）にして終わる。smile: 微笑む顔（状態 2）にして
  終わる（どちらも人が目で見る）。off: 消して終わる。
- bench: 微笑む顔と消灯を交互に送り、1 回ごとの往復（状態を渡して応答が
  返るまで。描画は含まない）を測って、件数・中央値・95 パーセンタイル・
  最大（ms）を標準出力に出す。最後（例外のときも）に消灯を送る。

画の確かめだけで、表示の規則（docs/decisions/0018。M4）は持たない。
RPC は mcu_rpc.py の中だけ。ファイルは書かない。標準ライブラリだけ。
"""

import argparse
import statistics
import sys
import time

import mcu_rpc


def summarize(samples_ns):
    """往復の時間（ns）の列から件数・中央値・95 パーセンタイル・最大（ms）。

    95 パーセンタイルは numpy.percentile の既定（線形補間）と同じ値。
    """
    ms = [s / 1e6 for s in samples_ns]
    return {
        "n": len(ms),
        "median_ms": statistics.median(ms),
        "p95_ms": statistics.quantiles(ms, n=100, method="inclusive")[94],
        "max_ms": max(ms),
    }


def run_bench(link, n, warmup, clock=time.perf_counter_ns):
    """顔・消灯を交互に warmup 回（測らない）と n 回（測る）。最後に消灯。"""
    samples = []
    states = (mcu_rpc.STATE_SMILE, mcu_rpc.STATE_OFF)
    i = 0
    try:
        for _ in range(warmup):
            link.set_state(states[i % 2])
            i += 1
        for _ in range(n):
            t0 = clock()
            link.set_state(states[i % 2])
            samples.append(clock() - t0)
            i += 1
    finally:
        link.set_state(mcu_rpc.STATE_OFF)
    return summarize(samples)


_SHOW = {
    "wave": (
        mcu_rpc.STATE_WAVE,
        "表示を流れる波線にしました（確かめ。表示の規則はまだ無い）",
    ),
    "smile": (
        mcu_rpc.STATE_SMILE,
        "表示を微笑む顔にしました（確かめ。表示の規則はまだ無い）",
    ),
    "off": (mcu_rpc.STATE_OFF, "表示を消しました（確かめ）"),
}


def main(argv=None, open_link=mcu_rpc.McuLink.open_default):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("wave", help="流れる波線にして終わる")
    sub.add_parser("smile", help="微笑む顔にして終わる")
    sub.add_parser("off", help="消して終わる")
    b = sub.add_parser("bench", help="往復の遅れを測る（最後に消す）")
    b.add_argument("--n", type=int, default=100)
    b.add_argument("--warmup", type=int, default=5)
    args = ap.parse_args(argv)
    if args.cmd == "bench" and (args.n < 2 or args.warmup < 0):
        ap.error("--n は 2 以上、--warmup は 0 以上")

    with open_link() as link:
        if args.cmd in _SHOW:
            state, text = _SHOW[args.cmd]
            link.set_state(state)
            print(text)
        else:
            s = run_bench(link, args.n, args.warmup)
            print(
                "往復の遅れ（zm_set_state、状態を渡して応答が返るまで。"
                "描画は含まない）: "
                f"件数 {s['n']}、中央値 {s['median_ms']:.3f} ms、"
                f"95 パーセンタイル {s['p95_ms']:.3f} ms、"
                f"最大 {s['max_ms']:.3f} ms"
            )
            print(_SHOW["off"][1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
