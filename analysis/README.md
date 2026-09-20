# analysis/

PC 側の解析・評価。ファームウェアが何で書かれていても、`docs/data-schema.md` の形式が入力。

- `features.py`   窓ごとの特徴量（IMU＋音）
- `evaluate.py`   `docs/evaluation.md` の定義でイベント単位の検出率・誤検出率を出す
- `split.py`      セッション単位で学習／評価を分ける（窓単位の分割は実装しない）
- `train_eval.py` M1 の分岐点: ロジスティック回帰で学習し、上の3つを使って採点した報告を出す
- `to_edge_impulse.py`  events.csv のラベルで WAV/CSV を Edge Impulse に投入する形に整える

M1 では `features.py` ＋ ロジスティック回帰、M2 から Edge Impulse。

## 特徴量（`features.py`）

窓は 1.0 秒 / 0.25 秒（`evaluate.py` の `WINDOW_S` / `HOP_S` を import する）。1窓 29 次元。式の定義と理由は
`docs/decisions/0012-feature-definitions.md`。M1 の PC 側の評価用の定義で、#13 の結果を見てから動かさない。
依存は numpy と標準ライブラリだけ。

```
python analysis/features.py <セッションフォルダ>
```

表示するのはセッション名・窓の数・次元・`valid` の数・最初と最後の `t_start_s` だけ。特徴量の値は表示しない。
特徴量をファイルに書く機能は無い（使う側が `extract_session` を呼んで、その場で計算する）。

```python
f = features.extract_session(session_dir)   # WindowFeatures(session, t_start_s (n,), X (n, 29) float32, valid (n,) bool)
s = features.Standardizer.fit([学習側の WindowFeatures, ...])   # valid な窓だけ。s.sessions に使ったセッション名
Z = s.transform(f.X)
```

`extract_session` は常に正規化前の値を返す。正規化の統計量は `Standardizer.fit` に渡したセッションだけから作る
（評価側を渡さない）。`std` が 0 の次元は 1 に置き換える。有効な窓が無い `fit`、末尾が 29 次元でない `transform` は
`ValueError`。戻り値に生の波形と IMU の系列は含めない。

### 次元の一覧（`FEATURE_NAMES` と同じ順）

前処理: 加速度 3 軸とジャイロ 3 軸は、それぞれ窓内の平均を引く。フィルタは掛けない。分散・RMS は ÷ n。

| # | 名前 | 定義 | 次元 |
|---|---|---|---|
| 0–2 | `acc_ptp_x` `acc_ptp_y` `acc_ptp_z` | 振幅: 加速度の各軸の peak-to-peak（g） | 3 |
| 3 | `gyro_norm_ptp` | 振幅: ジャイロのノルムの peak-to-peak（deg/s） | 1 |
| 4–6 | `acc_rms_x` `acc_rms_y` `acc_rms_z` | RMS: 加速度の各軸 | 3 |
| 7–9 | `gyro_rms_x` `gyro_rms_y` `gyro_rms_z` | RMS: ジャイロの各軸 | 3 |
| 10 | `acc_peak_count` | ピーク数: 加速度のノルムの局所極大（`x[i] > x[i−1]` かつ `x[i] ≥ x[i+1]`、窓の両端は数えない）。高さがノルムの窓内 RMS × 1.0 以上、最小間隔 50 ms（`t_ms` の差。近いものは高いほうを残す） | 1 |
| 11–13 | `acc_axis_x` `acc_axis_y` `acc_axis_z` | 主軸方向: 加速度の 3×3 共分散行列の第1固有ベクトル（単位ベクトル）。符号は Y、X、Z の順で最初の `|成分|` ≥ 1e-6 を正に。λ1 < 1e-8 g²（静止）か `λ1 − λ2 < 0.01 × λ1` のときは `[0, 1, 0]` | 3 |
| 14–26 | `mfcc_0` 〜 `mfcc_12` | MFCC: フレーム 25 ms（400）/ 10 ms（160）、Hamming、プリエンファシス 0.97、`n_fft` 512、パワーは `|FFT|² ÷ 512`、メル 26 本（0〜8000 Hz、HTK 式、三角フィルタは面積の正規化なし）、`log(E + 1e-10)`、DCT-II（ortho）の先頭 13 係数（c0 を含む）。窓内の 98 フレームの平均 | 13 |
| 27 | `spectral_centroid_hz` | スペクトル重心: フレーム（MFCC と同じ切り方、Hamming、プリエンファシス前）ごとの `Σ f·P / Σ P`（Hz）の窓内平均。`Σ P < 1e-12` のフレームは 0 | 1 |
| 28 | `zero_crossing_rate` | ゼロ交差率: 窓内の平均を引いた波形の符号（`≥ 0` を正）の変化回数 ÷ (N − 1) | 1 |

音声は int16 を 32768 で割った float32。読み込みは標準ライブラリの `wave`。

### 定数（`features.py` の冒頭）

| 定数 | 値 | 意味 |
|---|---|---|
| `AUDIO_HZ` | 16000 | これ以外のサンプルレートは `ValueError` |
| `AUDIO_CHUNK_T_OFFSET_MS` | 0.0 | 先頭サンプルの時刻への換算のあとに足す補正（下の #15 の注記） |
| `GAP_RATIO` | 1.5 | 1周期のこの倍以上の差分を飛びとする（`tools/check_session.py` と同じ）。音声は空白が周期の `GAP_RATIO − 1` 倍以上 |
| `MIN_IMU_ROW_RATIO` | 0.9 | 窓の IMU 行数が期待（1.0 ÷ 周期）のこの割合未満なら `valid = False` |
| `PEAK_HEIGHT_RMS_RATIO` / `PEAK_MIN_DISTANCE_S` | 1.0 / 0.050 | ピーク数 |
| `AXIS_SIGN_EPS` / `AXIS_MIN_EIGENVALUE` / `AXIS_MIN_EIGEN_GAP_RATIO` / `AXIS_DEFAULT` | 1e-6 / 1e-8 / 0.01 / `[0, 1, 0]` | 主軸方向 |
| `FRAME_LEN` / `FRAME_HOP` / `PREEMPHASIS` / `N_FFT` / `N_MEL` / `MEL_FMIN_HZ`–`MEL_FMAX_HZ` / `LOG_FLOOR` / `N_MFCC` | 400 / 160 / 0.97 / 512 / 26 / 0–8000 / 1e-10 / 13 | MFCC |
| `CENTROID_MIN_POWER` | 1e-12 | スペクトル重心を 0 にするフレームの `Σ P` の境 |

### `t_ms` の扱いと窓

- 時間軸は装置の `t_ms` ÷ 1000（秒）。0 への付け替えはしない。`evaluate.load_events` と同じ軸なので、`t_start_s` は
  そのまま `event_metrics` の `positive_windows` に使える。
- チャンクの `t_ms` は、装置がそのチャンクを読み出した直後の時刻（論理上はチャンクの終端側。`docs/data-schema.md`、
  docs/decisions/0013）。チャンクごとの長さ `L[i] = sample_index[i+1] − sample_index[i]`（最終行は WAV のサンプル数 −
  `sample_index[last]`）を使い、先頭サンプルの時刻のアンカーを
  `anchor[i] = (t_ms[i] − 1000 × L[i] ÷ 16000 + AUDIO_CHUNK_T_OFFSET_MS) ÷ 1000` 秒とする（64 サンプルなら `t_ms` の 4 ms 前）。
- 音声サンプルの時刻は `(sample_index[i], anchor[i])` から出す。チャンクの間は線形補間、最後のアンカー以後は 16 kHz の
  傾きで WAV の最後まで補外する。最頻のチャンク長（`sample_index` の差分の最頻値）は、入力の検証と音声の1周期に使う。
- 各ストリームの範囲は半開区間。音声は `[先頭サンプルの時刻, 最終サンプルの直後の時刻)`、IMU は
  `[最初の行の t_ms, 最後の行の t_ms + 1周期)`。窓は両者の重なりに、遅いほうの開始から 0.25 秒刻みで置く。
  IMU の1周期は、飛びでない `t_ms` の差分（中央値の 1.5 倍未満）の平均。
- IMU は `t_ms` が `[開始, 開始 + 1.0)` に入る行（約 105 行）。音声は窓の開始時刻に最も近いサンプルから 16000 サンプル固定。
- 取りこぼし: IMU は `t_ms` の差分が 1.5 周期以上の箇所を飛びとし、飛びの区間は `(前の時刻, 次の時刻)`。音声は換算後の
  時刻から出す。前のチャンクの推定の終端 `end[i] = anchor[i] + L[i] ÷ 16000` と次のチャンクの推定の先頭 `anchor[i+1]` の
  間の空白が、周期（最頻のチャンク長 ÷ 16000）の `GAP_RATIO − 1` = 0.5 倍（64 サンプルなら 2 ms）以上なら飛びとし、
  飛びの区間は `(end[i], anchor[i+1])`。チャンク長が一定なら「`t_ms` の差分が 1.5 周期以上」（`tools/check_session.py`）と
  同じ判定で、長さの違う正常なチャンク（32 / 96 サンプル）は飛びにならない。
- 飛びの区間と交わる窓、IMU の行数が期待の 90% 未満の窓、音声が 16000 サンプルに満たない窓は `valid = False`。
  窓は落とさず、グリッドは等間隔のまま返す。
  無効な窓でも `X` は有限値（窓に入ったサンプルで計算。IMU が 2 行未満なら IMU の 14 次元、音声が足りなければ音の
  15 次元を 0 にする）。無効な窓を学習と正規化から外すこと、評価での扱いは #13 で決める。
- 入力の検証（合わなければ `ValueError`）: `audio.wav` が mono・int16・16000 Hz（`meta.json` のトップレベル `audio_hz`
  を優先し、無ければ `sample_rates.audio_hz`。docs/decisions/0006。WAV のヘッダーとも一致）、`audio_chunks.csv` が
  2 行以上・最初の `sample_index` が 0・`sample_index` が狭義の単調増加・`t_ms` が単調非減少、最終チャンクの長さ
  （WAV のサンプル数 − 最後の `sample_index`）が 1 以上チャンク長以下、`imu.csv` が 2 行以上で `t_ms` が単調非減少。

### #15 の注記（`AUDIO_CHUNK_T_OFFSET_MS`）

#15 の結論（docs/decisions/0013）: 文書を実装に合わせた。チャンクの `t_ms` は「読み出した直後の時刻」で、先頭サンプルの
時刻は上の換算（`t_ms` − チャンクの長さぶん）で出す。この換算はコードから定めた論理上の時刻で、約 4 ms を実測で証明した
ものではない。記録済みのセッションの変換は要らない（CSV と WAV の値は変わらず、読み方だけが変わる）。
`AUDIO_CHUNK_T_OFFSET_MS`（既定 0.0）は、換算のあとに足す補正（ms）。系統的なずれが分かったとき用で、0.0 のまま残してある。

## 学習と評価（`train_eval.py`）

M1 の分岐点の数字（#13）を出す。手順の定義と理由は `docs/decisions/0014-m1-training-procedure.md`。
実データを見る前に固定した。結果を見てから、ラベルの規則・閾値の決め方・モデルの設定・分割を動かさない。
依存は numpy と scikit-learn（`tools/requirements.txt`）。

```
python analysis/train_eval.py data/raw --eval-min <N>           # 既定。学習側だけ
python analysis/train_eval.py data/raw --eval-min <N> --final   # 評価側を採点する
```

- `--eval-min` は必須。最後の2収集日のセッション数（`docs/recording-protocol.md`）。subject は `self` 固定。
- **既定の実行**は学習側のセッションだけを使い、交差検証の数字と閾値を出す。何度実行してもよい。評価側は
  `meta.json`（`split_sessions` が subject の検査に読む）のほかは開かず、報告にはセッション名だけを出す。
- **`--final`** を付けたときだけ、評価側を採点する。実行した後は評価側を動かさない。
- 報告は標準出力に Markdown で出す。ファイルは書かない（モデル・特徴量・確率を保存しない）。
  波形、特徴量の値、個々の窓の確率は出さない。

### 手順の要約

1. 分割: `split_sessions(root, eval_min, subject="self")`。フォルダ名の日付で、評価側がちょうど 2 収集日、学習側が
   1 収集日以上、学習側のどの日も評価側のどの日より前、を検査する。外れたら止まる。
2. 特徴量: `features.extract_session`。`valid = False` の窓は学習と正規化に使わず、採点では陰性として扱う
   （陽性にしない）。数を報告に出す。
3. 学習のラベル: 嚥下のマーカー `t` に対して、窓の中心 `w + 0.5` が `[t, t + 1.0]` に入る窓が陽性。中心が
   `[t − 0.5, t)` か `(t + 1.0, t + 1.5]` の窓は学習に使わない。どの嚥下の `[t − 0.5, t + 1.5]` にも中心が入らない窓が
   陰性。学習のラベルだけの規則で、評価の数え方（`docs/evaluation.md`、docs/decisions/0011）は変えない。
4. モデル: `features.Standardizer`（統計量は、学習に使うセッションの `valid` な窓の全部。「使わない」窓も入れる）＋
   `LogisticRegression(C=1.0, class_weight="balanced", max_iter=1000)`（`lbfgs`。乱数を使わない）。探索しない。
5. 交差検証（学習側だけ）: 学習側の収集日が 2 日以上なら収集日ごと、1 日なら1セッションごと。fold ごとに
   `Standardizer` とモデルを作り直す。グループが 2 つ未満、fold の学習側に陽性と陰性がそろわない、合算で嚥下が 0 件か
   非嚥下の時間が 0、のときは fold とセッション名を示して止まる。
6. 閾値: 候補 0.05, 0.10, …, 0.95 を、交差検証の確率に対して評価と同じ数え方（`event_metrics` → 回数の合算）で採点する。
   誤検出率が 3.0 回/分以下の候補のうち検出率が最大（同率なら高いほう）。無ければ誤検出率が最小（同率なら高いほう）。
7. 最終モデルは学習側の全セッションで作り直す。`--final` のときだけ、評価側のセッションごとに `event_metrics` を計算し、
   `split_sessions` が返した `eval` の一覧をそのままキーにして `evaluate.aggregate` に渡す。
8. 参考値として、常時陽性の場合（全窓を陽性にした場合）の数字と、陽性窓の割合（陽性窓 ÷ 全窓）を並べる
   （docs/decisions/0011「既知の弱点」）。

### 再現の条件

同じ入力で、同じコミット・同じ依存の版（scikit-learn、numpy）・同じ実行環境なら、報告は一字一句同じになる
（`lbfgs` は乱数を使わない）。版や BLAS、実行環境をまたいだ一致は保証しない。報告の「実行の条件」に出る
コミットのハッシュと scikit-learn・numpy の版が、再現の条件。
追跡しているファイルに未コミットの変更があると、報告のコミットに `-dirty` が付く。`--final` は、その状態では
採点せずに止まる（先にコミットする）。既定の実行は止まらない。

## テスト

`evaluate.py` と `split.py` の数え方、`features.py` の窓と特徴量、`train_eval.py` の手順は、合成データのテスト
（`test_evaluate.py`・`test_split.py`・`test_features.py`・`test_train_eval.py`）で固定している。`test_features.py` は
numpy、`test_train_eval.py` は numpy と scikit-learn を使う。ほかは標準ライブラリの `unittest` だけで動く。
合成データは一時フォルダに作り、`data/` は使わない。`test_train_eval.py` は 60 秒の合成セッションを 9 つ作って
学習を繰り返すので、20 秒ほど掛かる。

```
python -m unittest discover -s analysis -v
```

`docs/evaluation.md` が決めていない点（窓が区間に「ある」の判定、連続する陽性の数え方、記録の範囲、
複数セッションの集計、混同行列）の解釈は `docs/decisions/0011-evaluation-interpretation.md` にある。
