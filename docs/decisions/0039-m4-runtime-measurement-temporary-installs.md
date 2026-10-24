# 0039 UNO Q の推論ランタイムの比較のために一時的に入れるものと、装置で使うものを分ける（#51）

日付: 2026-10-24　状態: 採用（人の承認 10/24。plan #51、Codex 2 回・3 面 LGTM）

Issue #51 で、UNO Q の CNN の段の推論を numpy の書き方の違い（B）、OpenBLAS（C）、別の推論ランタイム（D。CPU と GPU）で比べた（人の決定 10/24: 数字を待たず
最初から比べる、GPU は外さない、計測に要るものは入れて要らなければ外す）。この記録は、そのために UNO Q に入れたものの扱いを決める。
装置の推論は numpy のまま（M4 起票の D3「装置の推論は numpy だけ」・D9「UNO Q に入れるパッケージは `python3-numpy` だけ」、0025 の「装置と PC で同じ Python のコード」）。
この記録では採否を決めない。

## 決定

1. **区別。** 「計測のために一時的に入れるもの」（apt のパッケージ、`~/zaitaku-mealpace-bench-venv` の venv と pip の wheel、`DEPLOY_FILES` に足した計測のスクリプト
   `bench_runtimes.py` と乱数の重みのモデル 4 つ）と、「装置で使うもの」（D3・D9・0025 のまま。numpy だけ）を分ける。前者は D3・D9 の例外として、#51 の計測の間だけ置く。
   装置の検出器（#47）は前者を import しない。
2. **外し方。** 計測が終わったら、人が要らないものを外す（`apt remove`、`update-alternatives` を戻す、venv の削除）。外したもの・残したもの・ディスクの増減を log に書き、
   残すものはこの記録に追記する。
3. **GPU の環境変数**（`RUSTICL_ENABLE=freedreno`、Mesa のシェーダのキャッシュを止める `MESA_SHADER_CACHE_DISABLE=true`）は計測のコマンドで渡し、ファイル
   （スクリプト・systemd・`~/.profile`）に固定しない。スレッドの環境変数（`OPENBLAS_NUM_THREADS` など）も同じ。`bench_runtimes.py` は環境変数を書き換えない（静的テスト）。
4. **QNN・SNPE を外した理由。** QRB2210 に NPU・HTP・cDSP が無く（cDSP の remoteproc なし、ADSP の FastRPC は root 専用）、Qualcomm の apt にも QNN・SNPE が無い（10/24 の調査）。
5. **採否の進め方。** 数字を見た後に装置のランタイムを numpy 以外に変える（あるいは OpenBLAS を残す）なら、別の decision で D3・D9・0025（装置と PC で同じ Python のコード）
   との関係を決め、#47 の plan の前に置く。この記録では採否を決めない。
6. **ランタイムが自分で書くファイル。** `bench_runtimes.py` はファイルを書かないが、ランタイムがホームの下に書くものがある（下の「確かめたこと」の ONNX Runtime）。
   音声ではないので計測は止めない。名前と大きさを log に書き、中身は読まない。決定 2 で venv と一緒に消す候補に入れる。

## 確かめたこと（2026-10-24。詳しくは docs/log/2026-10-24.md「#51」）

- **入れたもの（UNO Q。人、`sudo`）**
  - apt（15:55 ごろ）: `python3-onnxruntime` 1.21.0+dfsg-1、`python3-pyarmnn` 23.08-5（バインディング 33.0.0）、`armnn-latest-cpu-gpu` 23.08-5（`libarm-compute43` 24.11 など）、
    `python3-venv` 3.13.5。自動で入ったものを含めて 34 個（`python3-sympy`・`python3-pil` など）。
  - venv `~/zaitaku-mealpace-bench-venv`（`--system-site-packages`、配る先の外。numpy は system の 2.2.4 を読む）: pip で `ai-edge-litert` 2.3.0、`ncnn` 1.0.20261020、
    `onnxruntime` 1.31.0（PyPI の wheel、`--no-deps`）と、それらの依存（`ml_dtypes`・`portalocker`・`tqdm`・`typing_extensions`・`backports.strenum`）。130 MB。
  - apt（17:44 ごろ）: `libopenblas0-pthread` 0.3.29+ds-3、`ocl-icd-opencl-dev` 2.3.3-1（plan に無い。Arm NN GpuAcc の再試行のため。自動で 11 個）。
  - ディスクの空き 1.8 GB（81%）→ 1.6 GB（84%）。
- **BLAS の alternatives**: OpenBLAS の計測の後、人が `update-alternatives --set` で libblas・liblapack を参照実装に戻した。**manual mode** のまま
  （OpenBLAS を外すか `--auto` に戻すまで）。参照実装に戻した後の numpy と `bench_infer.py` の数字は #44 と同じ範囲。
- **計測の要約**（合成入力、乱数の重み、RPC 込み）: 測れた全部の行で、構成 J の 1 窓の全体は中央値・95 パーセンタイル・最大ともホップ 0.25 秒の 250 ms、0.1 秒の 100 ms 未満。
  CNN 1 窓の中央値は numpy 27.7 ms（参照 BLAS）・14.6〜14.9 ms（OpenBLAS）、CPU のランタイム（LiteRT・ONNX Runtime・ncnn・Arm NN CpuAcc）1.3〜2.2 ms、
  GPU（ncnn の Vulkan、Adreno 702）8.7〜9.7 ms（CPU 時間 約 2.5 ms）。動作しなかったもの: ONNX Runtime の XNNPACK の EP（wheel に無い）、
  Arm NN GpuAcc（ACL が rusticl の FD702 で `Non uniform workgroup size is not supported`）、LiteRT の GPU（wheel が LSE 命令付きで A53 で SIGILL）、
  Debian の `python3-onnxruntime` 1.21（import で SIGILL。PyPI の wheel で測った）。
- **ランタイムが書いたファイル**: ONNX Runtime 1.31（PyPI の wheel）が import で `~/.cache/Microsoft/DeveloperTools/.onnxruntime/` に `deviceid`（36 B）と
  `onnxruntime.db`（24,576 B）を作り、使うたびに `onnxruntime.db` を書き直した。音声ではない。ほかに新しいファイルは無い（Mesa のシェーダのキャッシュは増えていない）。
- UNO Q に音声の拡張子のファイルは 0 件。`~/zaitaku-mealpace-device/` は一覧の 14 個だけ、`__pycache__` 0 件。
- **外したもの**: まだ無い（決定 2。人の作業）。外した後にこの記録と log に追記する。
