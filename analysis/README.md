# analysis/

PC 側の解析・評価。ファームウェアが何で書かれていても、`docs/data-schema.md` の形式が入力。

- `features.py`   窓ごとの特徴量（IMU＋音。M1 の式、docs/decisions/0012）
- `features_m2.py`  窓ごとの特徴量（IMU＋音。M2 の式、docs/decisions/0020。IMU と窓は `features.py` を流用）
- `evaluate.py`   `docs/evaluation.md` の定義でイベント単位の検出率・誤検出率を出す
- `split.py`      セッション単位で学習／評価を分ける（窓単位の分割は実装しない）
- `train_eval.py` M1 の分岐点: ロジスティック回帰で学習し、上の3つを使って採点した報告を出す
- `evaluate_detector.py` M2 の検出器のセッション（`detect.csv`・`events.csv`）を `evaluate.py` の数え方で採点した報告を出す（#23、docs/decisions/0021）
- `ei_upload.py`  M2 の特徴量ベクトル（1 窓 1 項目）を Edge Impulse に投入する（#21。生の音声・IMU は上げない）

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

## M2 の特徴量（`features_m2.py`）

M2 の検出器（Edge Impulse の分類器）に渡す 29 次元。式の定義と理由は `docs/decisions/0020-m2-audio-features.md`。
0012 の式のうち音の 15 次元（MFCC 13、スペクトル重心、ゼロ交差率）だけを M2 用に置き換えたもので、IMU 14 次元・
読み込みと検証・チャンクの時刻の換算・飛び・窓の格子・`valid` の規則は `features.py` の関数を import して使う
（`features.py` は M1 の定義として残し、変えていない）。依存は numpy と標準ライブラリだけ。

| 項目 | M2（`features_m2.py`） | M1（`features.py`） |
|---|---|---|
| 音の周波数 | 8 kHz（16 kHz の隣り合う 2 サンプルの平均 `(a + b) >> 1`。int32 の整数演算、奇数長は最後を捨てる） | 16 kHz |
| 窓 | 8000 サンプル（開始に最も近い 8 kHz のサンプルから固定長） | 16000 |
| フレーム | 25 ms（200）/ stride 25 ms（200、重なりなし）。1 窓 40 フレーム、1 ホップ 10 フレーム | 400 / 160、98 フレーム |
| プリエンファシス | 0.97。フレームごとに独立（各フレームの `pre[0] = x[0]`） | 窓全体に掛けてから切る |
| FFT・メル | Hamming（200）、`n_fft` 256（129 ビン）、メル 26 本 0〜4000 Hz、DCT-II の先頭 13 | 512、0〜8000 Hz |
| スペクトル重心 | MFCC と同じ（プリエンファシス後の）パワースペクトルから | プリエンファシス前の別の FFT |
| ゼロ交差率 | 8000 サンプル ÷ 7999 | 16000 ÷ 15999 |

- 公開する API は `features.py` と同じ形。`extract_session(session_dir) → features.WindowFeatures`（`X` は (n, 29) float32、正規化前）。
  窓の数・`t_start_s`・IMU 14 次元・`valid` は同じセッションに対して `features.extract_session` と一致する。
- `FEATURE_NAMES` は `features.FEATURE_NAMES` の再公開（同じ順・同じ名前）。M1 の値と取り違えない目印は
  `FEATURE_SET = "m2-0020"`（`analysis/m2_norm.json` と検出器のセッションの `meta.json` の `feature_set` に書く）。
- 正規化は `features.Standardizer` をそのまま使う（`fit` / `transform` は次元数 29 だけを見る）。
- 表示（`python analysis/features_m2.py <セッションフォルダ>`）はセッション名・式の識別子・窓の数・次元・`valid` の数・最初と最後の
  `t_start_s` だけ。特徴量の値は表示せず、ファイルにも書かない。
- 定数（`FEATURE_SET`、`AUDIO_HZ_IN`、`AUDIO_HZ`、`DECIMATION`、`WINDOW_SAMPLES`、`HOP_SAMPLES`、`FRAME_LEN`、`FRAME_HOP`、`PREEMPHASIS`、
  `N_FFT`、`N_MEL`、`MEL_FMIN_HZ`–`MEL_FMAX_HZ`、`LOG_FLOOR`、`N_MFCC`、`CENTROID_MIN_POWER`）はモジュール冒頭。0020 で固定し、動かさない。
- 装置側の同じ式は `firmware/bench/`（`-DAF_PROFILE=1`）で、`firmware/bench/host/check_port.py --profile m2` が `features_m2` と比べる。

## Edge Impulse への投入（`ei_upload.py`）

`features_m2.extract_session` で出した 29 次元を `features.Standardizer` で正規化し、1 窓 = 1 項目（`swallow` / `cough` / `other`）として
Edge Impulse（EI）の ingestion API に送る。生の音声・IMU の窓は上げない。投入するデータ・ラベルの規則・分け方は docs/decisions/0019、
式と正規化の定数は 0020。依存は numpy と標準ライブラリ（`urllib`）だけで、EI の SDK は使わない。

```
python analysis/ei_upload.py data/raw --bucket training --days 20260921,20260922,20260923 --validation-day 20260923 [--dry-run]
python analysis/ei_upload.py data/raw --bucket testing --days 20260924 [--dry-run]
python analysis/ei_upload.py --probe [--dry-run]
```

- **分割**: `split.split_sessions(root, 7, "self")` の `train` が収集日 `20260921`・`20260922`・`20260923` の各 7 本、`eval` が `20260924` の
  7 本ちょうどであることを検査する（外れたら止まる）。`--days` はその収集日の部分集合。`--bucket training` に収集日4 は入れられず、
  `--bucket testing` は収集日4 だけを受け付ける。subject は `self` 固定（`p1` のセッションは投入しない）。
- **ラベル**（`window_labels3`）: 全窓を `other` にし、各 `c` について窓の中心が `[t_c − 0.5, t_c + 1.5]` の窓を境目、次に `[t_c, t_c + 1.0]` を
  `cough` にし、その後で各 `s` について同じ形で境目 → `swallow` を掛ける（`s` の陽性と境目が `c` の規則より優先。近接した 2 つの嚥下では
  陽性が境目より優先）。`swallow` の窓は `train_eval.window_labels` の陽性と同じ集合。`t`・`n`・`q` は `other`。
- **投入しない窓**: `valid = False` の窓と境目の窓。除外した数（無効 / 境目）をセッションごとに出す。`other` の間引きはしない。
- **正規化**: `--bucket training` は fit セッション（`--days` から `--validation-day` を除いたもの）の `valid` な窓の全部で
  `Standardizer.fit` し、定数を `analysis/m2_norm.json`（`feature_set`、`feature_names`、`mean`、`std`、`stats_sessions`、`n_windows`、
  `commit`。集計値のみ）に書く。ファイルが既にあって `commit` 以外の内容が違えば止まる（黙って上書きしない。作り直すときは人が消す）。
  `--bucket testing` はそのファイルを読んで使い（無ければ止まる。`stats_sessions` に収集日4 があれば止まる）、収集日4 から統計量を作らない。
  投入する値は `transform` の後の float32。
- **項目の形**: EI のデータ取得の JSON（`protected.alg = "none"`、`interval_ms` 1000、`sensors` は `FEATURE_NAMES` の 29 個、`values` は
  1 行 29 個）。ファイル名 `<セッション名>_<t_ms>.json`（`t_ms` は窓の開始 `round(t_start_s × 1000)`）。ヘッダは `x-label`、
  `x-metadata`（`session`・`day`・`t_ms`・`subject`・`feature_set`。値はすべて文字列）、`x-disallow-duplicates: 1`、`x-api-key`。
- **転送**: `https://ingestion.edgeimpulse.com/api/<training|testing>/data` に multipart/form-data を POST。**1 リクエスト 1 項目**
  （`x-metadata` がリクエスト単位に掛かるため）。失敗（HTTP 4xx/5xx、接続の失敗）は 3 回まで再試行し（待ち 1・2・4 秒）、それでも
  失敗したら、止まった場所（セッション名、そのセッションで送った項目数、全体で受け付けられた項目数）を出して止まる。
  `x-disallow-duplicates` で弾かれた項目（HTTP 400 で本文に duplicate / already exists）は再試行せず数だけ数える（再実行のとき）。
  進み具合はセッションごとに 1 行。送信の関数は `run(argv, send=..., sleep=...)` で差し替えられる（`send(url, headers, files) → (status, body)`）。
- **API キー**: 環境変数 `EI_API_KEY`。無ければ `--dry-run` 以外は止まる。値を標準出力・例外・ログに出さない（応答の本文に含まれていても伏せる）。
- **`--dry-run`**: 送信も `m2_norm.json` の書き込みもせず、一覧だけを出す。投入の前に人が一覧を見る。
- **`--probe`**: 乱数（seed 固定）の 29 次元を 9 項目（`swallow` / `cough` / `other` × 3。群 `probe-a` / `probe-b` / `probe-c`、
  `day` = `probe-1` / `probe-2` / `probe-3`、件数 2 / 3 / 4、`t_ms` は群の中で 0, 1000, …。`subject` と `feature_set` は `probe`）を
  training に送り、EI の受け付け（1 行の項目、`x-metadata`、メタデータの鍵による validation の分割）を確かめる。実データは使わない。
  送る先は `EI_API_KEY` の指すプロジェクト（#17 のダミー）。
- **標準出力**（`docs/log/` に貼る）: 実行の条件（コマンドライン、コミット、`feature_set`、numpy の版、dry-run か）、セッションごとの表
  （収集日、bucket、役割 fit / validation / test、全窓、`valid`、投入、クラスごと、除外）、合計（bucket・役割・収集日・クラスごと）、
  `stats_sessions` と窓の数、収集日ごとの投入数、送信したリクエスト数と受け付けられた項目数（Studio の項目数と照合する）。
  `--bucket training` では収集日ごとの投入数が互いに異なることを検査する（同数の日があれば止まる。EI の validation の件数から
  選ばれた日を読み取るため）。特徴量の値・波形・個々の窓の一覧は出さない。

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

## 検出器の評価（`evaluate_detector.py`）

M2 の数字（#27）を出す。検出器ファームウェア（#24）で録ったセッションには `imu.csv`・`audio_chunks.csv` が無く、
`detect.csv`（`t_ms, window_t_ms, positive, prob, led`）・`feat.csv`・`events.csv`・`meta.json` がある（`docs/data-schema.md`）。
数え方は `evaluate.event_metrics` / `evaluate.aggregate` をそのまま使う。記録の範囲だけを検出器のセッション向けに読み替え、
docs/decisions/0021 に記録した。`positive` と `events.csv` の `s` だけを使い、`led` と `prob` は評価に使わない。
依存は標準ライブラリと、`train_eval` 経由の numpy / scikit-learn。

```
python analysis/evaluate_detector.py data/raw                        # 既定: self の meal で fw が detector のセッション全部
python analysis/evaluate_detector.py data/raw --sessions A B C        # 明示したセッションだけ（除外を報告に出す）
python analysis/evaluate_detector.py data/raw --scorer path/to/m2_scorer.py   # feat.csv の再採点と装置の出力の差（#22）
```

- **対象**: 既定は subject `self`・cond `meal`・`meta.json` の `fw` が `detector` のセッション全部。`meal` なのに `fw` が
  `detector` でないセッションがあれば止まる（消してから走らせる）。`--sessions` で明示したときはそれだけ（cond は問わず、
  `meal` 以外は「M2 の数字に使わない」と印を付ける）。root の検出器のセッション全部と採否を報告に出す。評価に使うセッションの
  `threshold`・`model`・`feature_names` が一致しなければ止まる（同じファームウェアで録ったものだけを合算する）。
- **記録の範囲**（docs/decisions/0021）: 始まりは `detect.csv` の最初の窓の開始。終わりは `detect.csv` の送信時刻、最後の窓の終端、
  `feat.csv`（あれば）の送信時刻の最大。両ファイルで `window_t_ms` は狭義に単調増加、`t_ms` は単調非減少、隣との差 1000 ms 以下、
  送信の遅れ `t_ms − (window_t_ms + 1000)` は 1000 ms 以下（`MAX_SEND_LAG_MS`）。`feat.csv` の窓は `detect.csv` の窓の部分集合。
  外れたセッションは止まる。範囲より前のマーカーは `event_metrics` がエラーにする。
- **合算**: 既定の実行は `evaluate.aggregate`（3 セッション未満は止まる）。`--sessions` で 3 セッション未満のときだけ、
  セッションごとの数字を出し、合算は「出さない」と明記する（#24 の 60 秒の確認用。M2 の数字ではない）。
- **参考値**: 常時陽性の場合の数字、陽性窓の割合、`positive != (prob >= threshold)` の行数（比較は float32）、`c` に紐づく
  誤検出の塊（塊の最初の窓の中心 − `t_c` が `[−1.0, +5.0]` 秒）、収集日ごと・会話の有無ごと（`o` の note が `conv`）の合算。
- **`--scorer PATH`**: `score(features: list[list[float]], meta: dict) -> list[float]` を持つ Python ファイルで `feat.csv` を
  再採点し、装置の `positive` との差（装置 1 / PC 0、装置 0 / PC 1、`|prob − prob_pc|` の最大）と PC の陽性で数えた数字を出す。
  閾値は `meta.json` の `threshold`（上書きの引数は無い）。scorer の中身は #22。
- 報告は標準出力に Markdown。ファイルは書かない。個々の窓の確率と特徴量の値は出さない。未コミットの変更があってもコミットに
  `-dirty` を付けて止めない（#27 の M2 の数字はコミット済みの状態で走らせる）。

`split.py` は `fw` が `detector` のセッションを M1 の分割（`train` / `eval`）に入れず、戻り値の `detector` に列挙する。
`fw` が `detector` でないのに `detect.csv` か `feat.csv` があるセッションは止まる。

## テスト

`evaluate.py` と `split.py` の数え方、`features.py` の窓と特徴量、`train_eval.py` の手順、`evaluate_detector.py` の記録の範囲と
対象の選び方は、合成データのテスト
（`test_evaluate.py`・`test_split.py`・`test_features.py`・`test_train_eval.py`・`test_evaluate_detector.py`）で固定している。`test_features.py` は
numpy、`test_train_eval.py` は numpy と scikit-learn を使う。ほかは標準ライブラリの `unittest` だけで動く。
合成データは一時フォルダに作り、`data/` は使わない。`test_train_eval.py` は 60 秒の合成セッションを 9 つ作って
学習を繰り返すので、20 秒ほど掛かる。

```
python -m unittest discover -s analysis -v
```

`docs/evaluation.md` が決めていない点（窓が区間に「ある」の判定、連続する陽性の数え方、記録の範囲、
複数セッションの集計、混同行列）の解釈は `docs/decisions/0011-evaluation-interpretation.md` にある。
