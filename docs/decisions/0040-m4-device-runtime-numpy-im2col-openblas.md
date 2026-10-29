# 0040 装置の推論は numpy の im2col ＋ OpenBLAS（1 スレッド）で行い、LiteRT は M5 に送る（#51 の採否）

日付: 2026-10-29　状態: 採用（人の決定 10/29。0039 決定 5 の「別の decision」）

## 前提

- 0038 で M4 の構成が CNN `mfe40` の種 0・1・2 の平均に決まった。装置（UNO Q の Linux 側）は 1 窓（ホップ 0.25 秒）に CNN を 3 回回す。
- #51（docs/log/2026-10-24.md「#51」、0039）で UNO Q の CNN 1 窓の時間を計った（乱数の重み、構成 J = 1 種）。中央値（ms）: numpy 27.7（参照 BLAS）、
  numpy im2col 23.2（参照）・8.9（OpenBLAS 1 スレッド）、LiteRT CPU 1.3、ncnn CPU 1.8、ONNX Runtime 1.4〜2.2、ncnn Vulkan 8.7〜9.7（CPU 時間 2.5、初期化 4.5 秒）。
  構成 J の 1 ホップ全体（RPC 込み）は numpy 45 ms、im2col＋OpenBLAS 25 ms、LiteRT 19 ms。CPU（プロセス、ホップ 0.25 秒）は numpy 22.6%、im2col＋OpenBLAS（1 スレッド）18.5%、
  LiteRT 13.5%。OpenBLAS を既定のスレッド数で動かすと 4 コアが空回りして CPU 149〜314% になり、1 窓の時間はほぼ変わらない。
- 3 種に当てはめた見込み（推測。中央値 × 3 ＋ 構成 J の CNN 以外の部分）: numpy 約 100 ms / ホップ、im2col＋OpenBLAS 約 45 ms、LiteRT 約 20 ms。どれも 250 ms に収まる。
- 0039 決定 5: 装置のランタイムを numpy のままにしない（OpenBLAS を残す、別のランタイムに変える）なら、D3（装置の推論は numpy だけ）・D9（UNO Q に入れるパッケージは
  `python3-numpy` だけ）・0025（装置と PC で同じ Python のコード）との関係を別の decision で決め、#47 の plan の前に置く。この記録がそれ。
- 10/24 の段階 5 で、計測のために入れたもの（`libopenblas0-pthread` を含む）は UNO Q から外し、BLAS の alternatives は auto（参照実装）に戻してある。

## 決定

1. **装置の推論経路は numpy のまま、畳み込みを im2col にする。** `features_np.cnn_forward` の畳み込み（現在は直接の積和）を、#51 の `bench_runtimes._conv_im2col`
   と同じ形（パディング → `sliding_window_view` → reshape → `W.reshape(Co, -1) @ cols`）に替える。`features_np.py` は PC と装置の共有コード（0025）なので、
   PC 側の採点（#49）も同じ実装で動く。旧実装との差は #51 で 1.4e-08（照合の差）。#47 で、乱数の重みと 0038 の最終モデル（`analysis/models/m4-0038.npz`）の両方で
   旧実装との差が 1e-6 以下であることをテストで確かめてから置き換える（旧実装はテストの中に参照として残す）。
2. **UNO Q に OpenBLAS を入れる（D9 の例外）。** apt の `libopenblas0-pthread`（10/24 に計った版 0.3.29+ds-3）を人が入れる。BLAS の alternatives は auto のまま
   （OpenBLAS は優先度 100、参照実装は 10 なので、入れれば自動で OpenBLAS が選ばれる。`update-alternatives --display libblas.so.3-aarch64-linux-gnu` で確かめる）。
   D9 は「`python3-numpy` と `libopenblas0-pthread`」に読み替える。ほかのパッケージ・venv・pip は入れない（D3 は変えない）。
3. **スレッドは 1 に固定する。** 検出器（#47 の `detector.py`）は numpy を import する前に `OPENBLAS_NUM_THREADS=1`・`OMP_NUM_THREADS=1`・`MKL_NUM_THREADS=1` を
   環境に置く（起動スクリプトにも書く）。既定のスレッド数では CPU 150〜314% になり速くならない（前提）。CNN 3 回を 1 コアで直列に回す。
4. **LiteRT CPU（と ncnn CPU）は M5 に送る。** 採らない理由は下の「理由」。M5 で検討する入口は、ホップを 0.1 秒にする（M4 起票の D5）、電池の持ちを延ばす、
   CPU を空けて別の処理（記録や表示）を同じコアに載せる、のどれかが要るとき。そのときの材料は #51 の数字と、#47 の実測（決定 6）。
5. **GPU（ncnn Vulkan、LiteRT GPU、Arm NN GpuAcc）は採らない。** CPU を空ける目的でも LiteRT CPU のほうが CPU 時間が少なく（1.3 ms 対 2.5 ms）、初期化 4.5 秒と
   fp16 の差 2e-4 が付く。LiteRT GPU と Arm NN GpuAcc は UNO Q で動かなかった（#51）。
6. **#47 で実測して、この記録の見直しの目安を当てる。** 0038 の構成（3 種、後処理、FEAT の要約）で 1 ホップ全体（特徴量 → CNN × 3 → 平均 → 後処理 → RPC）の
   中央値・95 パーセンタイル・最大と、プロセスの CPU、電池の減り（待機の 6.8%/時 との比較）を log に書く。**1 ホップ全体の中央値が 125 ms を超える、または最大が
   250 ms を超える**なら、評価用 `meal` の記録の前にこの記録を見直す（LiteRT CPU の前倒しを含む）。収まっていれば M4 の評価（11/1〜）はこの経路で行う。
7. **0025 との関係。** 装置と PC は同じ Python のコード（`features_np.py`）で推論する。違うのは numpy が呼ぶ BLAS（UNO Q は OpenBLAS、PC は numpy の wheel の
   scipy-openblas）だけで、#51 の照合では差 1.4e-08。評価側（#49）の採点は PC 側の同じコードで行う。

## 理由

- 0038 で 3 種になり、numpy（参照 BLAS）では CNN だけで約 83 ms、1 ホップ約 100 ms と見込まれる。250 ms には収まるが、1 コアの 6 割を推論が使う。
  im2col＋OpenBLAS は約 45 ms・2 割で、コードの変更は畳み込み 1 関数、装置側の変更は apt のパッケージ 1 つで済む。
- LiteRT CPU はさらに速いが、npz → tflite の変換と照合の経路、UNO Q への venv の再導入（0039 で消したもの。130 MB）、0025「同じ Python のコード」からの
  逸脱の記録が要る。評価用 `meal` の記録が 11/1 からで、#47 に残る時間を推論経路の差し替えに使わない。数字（1 窓 1.3 ms、CPU 13.5%）は M5 の材料として残す。
- GPU は時間でも CPU 時間でも CPU のランタイムに劣る（決定 5）。

## 選ばなかった案

- **numpy の参照 BLAS のまま（変更なし）**: 動くが 1 ホップ約 100 ms・CPU 約 6 割（推測）。OpenBLAS を入れる手間（apt 1 つ）に対して差が大きいので採らない。
- **im2col だけ（参照 BLAS）**: 1 窓 23.2 ms → 3 種で約 70 ms。OpenBLAS 無しでは im2col の利点が小さい。
- **OpenBLAS を既定のスレッド数で**: CPU 149〜314% で 1 窓の時間は変わらない（#51）。
- **LiteRT CPU を今**: 上の「理由」。**ncnn CPU**: 同じ。ONNX Runtime: Debian 版は SIGILL、PyPI の wheel は venv が要る。
- **GPU**: 決定 5。

## 確かめていないこと

- 3 種を直列に回したときの実測（#51 は 1 種の数字 × 3 の見込み）。FEAT の要約（0037、82 次元）と後処理の時間は未計測。決定 6 で #47 が測る。
- `libopenblas0-pthread` を入れ直したときの版が 10/24 と同じか（apt の更新で変わりうる。#47 で版を log に書く）。
- 電池の減り（推論中）。#47 で測る。
- im2col に替えた後の最終モデルの読み戻し（0038 の確かめは旧実装で行った）。決定 1 のテストで 1e-6 以下を確かめる。

## 影響する場所

`analysis/features_np.py` と `device/unoq/features_np.py`（同じ内容。畳み込みを im2col に）、そのテスト、#47 の plan（`detector.py` の環境変数、起動スクリプト、実測の項目）、
`docs/unoq-setup.md`（OpenBLAS の導入と alternatives の状態）、`docs/status.md`、M4 起票の D9 の読み替え。M5 の Issue（LiteRT の検討）は M4 の関門の後に起票する。

## 変えていないこと

0025 の「装置と PC で同じ Python のコード」、D3「装置の推論は numpy だけ」、0038 の構成と重み、0039 の決定 1〜4・6（計測のために入れたものは外したまま）、
`docs/evaluation.md`。

## 追記（2026-10-29、Issue #47）: 「確かめていないこと」への答え

上の決定の本文は変えない。数字は docs/log/2026-10-29.md「#47」。

- **3 種を直列に回した実測（決定 6）**: 0038 の構成（3 種、後処理、FEAT の要約）で 1 ホップ全体（新しい 0.25 秒分の入力処理 ＋ 特徴量・FEAT・CNN × 3・平均・後処理・LED・
  行の投入 ＋ RPC）の `proc_ms` は、30 分の `water`（RPC あり、`--feat`）で中央値 79.8・95 パーセンタイル 81.4・最大 86.1 ms、180 秒で中央値 79.9・最大 106.2 ms。
  RPC なしは中央値 64.4 ms（ライブ、19 窓）、再生モード（標準入力、RPC なし）は中央値 35.8・最大 44.8 ms。**見直しの目安（中央値 125 ms 超・最大 250 ms 超）にはどの回も当たらない。**
  `lat_ms`（ブロックの到着から結果まで）は 30 分で中央値 85.7・最大 92.5 ms、キューの最大 0、入力の溜まりの最大 1990 標本（1 ホップの 4000 未満）。
- **OpenBLAS の版**: 10/29 に入れ直した `libopenblas0-pthread` 0.3.29（10/24 と同じ）。検出器が読み込んだのは `libopenblasp-r0.3.29.so`（`/proc/self/maps`）、スレッド 1。
- **電池**: 30 分の記録で PD のモバイルバッテリーの残量が 73% → 69%（約 8%/時。待機の 6.8%/時 に対して約 1.2%/時 の増え。表示の分解能は 1%）。
- **im2col に替えた後の照合**: 旧実装（einsum）との差は、乱数の重み（5 種、B = 1・7・256）で最大 1.48e-08、0038 の最終モデル（3 種、合成の入力）で 2.18e-10、
  `features_np_ref.npz` の参照とは 0（PC）。UNO Q の `detector.py --selftest` で参照との差 2.04e-08・新旧の差 7.39e-09。学習用 `meal` 1 本（窓 3979）の再生モードで
  装置と PC の `prob` の差の最大 1.61e-07、`positive`・`led` の不一致 0。`m4_compare --final-model` は再実行していない（モデルは変えていない）。
