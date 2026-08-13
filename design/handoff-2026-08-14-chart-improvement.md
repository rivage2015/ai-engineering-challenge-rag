# 引き継ぎ：グラフ・画像認識の改善

更新時点: 2026-08-14 02:45 JST

## 最初に読む結論

明日は、復元済みの `ChartTable` を既存の `Evidence -> SearchUnit -> 検索`
に接続するところから再開する。

今日の代表グラフ `figure_06.png` では、画像からの概算よりも、
生成元ノートブックとCSVからの復元が大きく効いた。
そのため、グラフは次の優先順位で処理する。

1. 生成元コード・CSV・Excel・埋め込みデータから厳密値を回収する。
2. 生成元を回収できない場合に、色系列の分離画像とGemma 4を使う。
3. 最後に軸較正、複数観測の合意、再描画照合を追加する。

特定質問、質問ID、正解による分岐は今後も禁止する。

## 作業対象とGit状態

- 作業ルート:
  `/Users/takashifukutomi/Documents/ChatGPT/AIエンジニアリングチャレンジ`
- ブランチ: `main`
- 今日の作業開始時HEAD: `19e53fe Harden resumable local submissions`
- この引き継ぎ文を含む今日の変更は、ユーザーの追加指示によりコミットする。
- pushは行わない。
- 既存変更を `reset` や `checkout` で消さない。

今日のコミット対象:

- `README.md`
- `rag/index.py`
- `design/sequential-multimodal-orchestration.md`
- `schemas/visual-analysis.schema.json`
- `schemas/chart-table.schema.json`
- `scripts/build_answer_diagnostic_report.py`
- `scripts/run_visual_analysis.py`
- `scripts/validate_visual_analysis.py`
- `scripts/build_chart_source_candidates.py`
- `scripts/recover_groupby_chart_table.py`
- `scripts/validate_chart_table.py`
- `scripts/build_chart_views.py`

`rag/index.py` と回答診断スクリプトは、今回のグラフ改善より前に行った
診断レポート対応の変更である。

## これまでに完了したこと

### 1. 回答診断

正解付30問の診断は次の分類になった。

- 成功: 9問
- 検索不足: 9問
- 前処理不足: 7問
- 回答生成不足: 5問

前処理不足7問の内訳は、表計算関連5問、画像・表示構造2問。

ローカル診断レポート:

`rag/logs/diagnostic_valid_20260813_233511.md`

### 2. 逐次マルチモーダル解析

Gemma 4は並列実行せず、次の順で逐次処理する。

1. Agent 1: 文字・数値の忠実な転記
2. Agent 2: 色・配置・系列・軸の独立観測
3. Agent 3: Agent 1と2の統合
4. 決定的Verifier

Agent 2にAgent 1の結果は渡さない。Agent 3のみが両方を受け取る。

代表グラフの初回実行時間:

- Agent 1: 約109秒
- Agent 2: 約220秒
- Agent 3: 約195秒
- 合計: 約8分44秒
- 同条件再実行: キャッシュ利用で約3.4秒

生成済みローカル結果:

`rag/visual-analysis/figure_06/analysis.json`

### 3. 生成元優先のChartTable復元

代表画像:

`share/共有ドライブ/プロジェクト/京橋信用ソリューションズ株式会社/04.分析/analysis_project/reports/figures/figure_06.png`

生成元:

- Notebook: `analysis_project/notebooks/01_eda.ipynb` の `c18`
- CSV: `analysis_project/data/train.csv`
- x軸: `day`
- 左y軸: `件数`
- 右y軸: `目的変数平均`
- 集計: `groupby(day)` の `size` と `mean(y)`

復元結果:

- 画像観測では30点と見ていたが、元データは1日〜31日の31点。
- 2系列を厳密に回収。
- `目的変数平均` の最小値は20日の `0.05831265508684864`。
- `件数` の最大値は20日の `1612`。
- 質問文と正解は探索・復元処理に未使用。

生成済みローカル結果:

- `rag/chart-tables/figure_06/source-candidates.json`
- `rag/chart-tables/figure_06/chart-table.json`

これらは `.gitignore` 対象で、Gitには含まれない。

### 4. 色系列ごとの分離画像

PillowとNumPyで色相クラスタを取得し、軸・他系列を薄く残しながら
一系列だけを強調する画像を作るようにした。

`figure_06.png` では正しく2系列を抽出した。

- オレンジ候補: `#f08329`
- 青候補: `#3081ba`
- 出力: `rag/chart-views/figure_06/`

この出力も `.gitignore` 対象。

## 明日の再開手順

### Step 0. 変更と生成物を再確認する

```bash
cd '/Users/takashifukutomi/Documents/ChatGPT/AIエンジニアリングチャレンジ'
git status --short
git diff --check
rag/.venv/bin/python scripts/validate_chart_table.py \
  rag/chart-tables/figure_06/chart-table.json
rag/.venv/bin/python scripts/validate_visual_analysis.py \
  rag/visual-analysis/figure_06/analysis.json
```

期待結果は両バリデータの `validation passed`。

### Step 1. ChartTableをEvidenceに変換する

最初の実装対象はここ。

1. 既存の `evidence.schema.json`、中間シャード、`build_search_units.py`の
   参照整合性を確認する。
2. ChartTableから次のEvidenceを質問非依存で生成する。
   - `chart`: タイトル、グラフ種別、軸、系列数、x範囲
   - `chart_series`: 系列名、対応軸、全ポイント、最小・最大
3. `document_id` と `evidence_id` は元画像とChartTableのハッシュから安定生成する。
4. 既存Evidenceと二重化させず、元画像まで追跡できる構造にする。
5. 中間層の検証を通す。

注意: ChartTableをSearchUnitに直接ねじ込まない。
既存の検証は `source_evidence_ids` が実在するEvidenceを参照することを要求している。

### Step 2. Chart EvidenceをSearchUnitに変換する

`search-unit.schema.json`、`build_search_units.py`、`validate_search_units.py`を
同時に整合させる。

追加候補の `unit_type`:

- `chart_summary`
- `chart_series`

検索文本は決定的に生成する。例:

```text
グラフ: day による件数推移
x軸: day
左y軸: 件数
右y軸: 目的変数平均
系列: 目的変数平均
値: 1日=0.265957..., 2日=..., 20日=0.058312...
最小: 20日 0.058312...
最大: 1日 0.265957...
```

全点を入れる単位と、要約・極値の単位を分け、検索結果に
原値と概要の両方が現れるようにする。

### Step 3. 代表グラフの検索動作を確認する

この段階で初めてテスト用の問い合わせを使う。
質問は前処理の分岐には使わず、生成後の評価にだけ使う。

確認項目:

- タイトル、軸名、系列名で検索できる。
- `20日`、`0.058312...`、`最小`の対応を検索本文から追跡できる。
- 検索結果からChartTable、CSV、Notebook、画像へ戻れる。
- 元の30問診断で、対象問の分類が「前処理不足」から改善する。

### Step 4. 生成元がないグラフの逐次観測

Step 1〜3の効果を確認した後に行う。

1. `build_chart_views.py` の `manifest.json` を `run_visual_analysis.py` が受け取れるようにする。
2. 全体図を1回観測する。
3. 各色系列の分離図を1枚ずつ逐次観測する。
4. 全体図の系列数と分離図の数を照合する。
5. 系列別の値を合意層で統合する。
6. 消えた系列、軸の取り違え、マーカー数不一致は `unresolved` にする。

Gemma 4呼び出しはすべて直列とする。

### Step 5. 軸較正と再描画照合

生成元のないグラフに限定する。

1. OCRまたはAgent 1から軸ティック値と座標を取得する。
2. Pillow・NumPyで軸と色マーカーの座標を検出する。
3. 座標からデータ値へ線形較正する。
4. 復元値からグラフを再描画し、元画像とのズレを測る。
5. 数値は複数観測を行整列し、中央値を採用候補にする。

`cv2` は現在の `rag/.venv` に入っていない。
まずPillowとNumPyで進め、OpenCVの追加が必要にった時点で依存追加を判断する。

### Step 6. 代表対象を増やす

次の順で、1件ずつ正確性を確認する。

1. 別の折れ線グラフ
2. 棒グラフ
3. 複数y軸グラフ
4. 凡例のないグラフ
5. PivotTable
6. フィルター・マーカーの表示状態が意味を持つXLSX

形式ごとに効果が確認できた処理だけを全件展開する。

### Step 7. 30問で再診断し、100問へ広げる

1. 30問の診断レポートを再生成する。
2. 前処理不足7問を最優先で比較する。
3. 検索不足、回答生成不足への副作用も確認する。
4. 効果がある場合のみ、100問を再実行する。
5. 最後に `submission.zip` の検証を行う。

## 12時間の実行時間配分案

- 生成元探索・構造復元: 最大1時間
- 逐次画像解析: 最大8時間
- Evidence・SearchUnit・索引構築: 約1時間
- 100問の回答: 約1時間
- 検証・再試行予備: 約1時間

逐次処理を優先し、速度のための並列化は行わない。

## 成功判定

次を満たしてから、次の形式または全件へ広げる。

- 質問非依存である。
- 元ファイルまでprovenanceを追跡できる。
- 系列数、軸数、各系列の点数が一致する。
- 厳密値と概算値が区別される。
- 不明な値を推測で埋めず `unresolved` にできる。
- 検索結果から抽出根拠を目視できる。
- 30問の診断で具体回答または正解率が改善する。

## 中断条件

次の場合は自動で先に広げず、診断レポートに残す。

- 生成元コードが複数候補あり、一意に特定できない。
- ノートブックの任意コード実行が必要になる。
- 系列数が全体図と分離図で一致しない。
- 軸と系列の対応を確定できない。
- 同じ画像の再観測で数値が大きく変わる。
- 検索・回答結果が改善しない、または別問の性能を落とす。

## 参照資料

- DePlot: chart image -> linearized table -> LLM
  - https://aclanthology.org/2023.findings-acl.660/
- MatCha: chart derendering + numerical reasoning
  - https://aclanthology.org/2023.acl-long.714/
- UniChart: chart element/data extraction + reasoning
  - https://aclanthology.org/2023.emnlp-main.906/
- ChartOCR: axis/plot/keypoint detection
  - https://openaccess.thecvf.com/content/WACV2021/papers/Luo_ChartOCR_Data_Extraction_From_Charts_Images_via_a_Deep_Hybrid_WACV_2021_paper.pdf
- Self-Ensembling VLM Chart Extraction: repeated extraction, alignment, median, convergence
  - https://arxiv.org/abs/2605.27298

## 明日最初の一文

`引き継ぎ文を読み、Step 0の検証後、ChartTableからEvidenceを生成する実装から再開してください。特定質問へのハードコードは禁止です。`
