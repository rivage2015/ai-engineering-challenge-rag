# AI Engineering Challenge - Intermediate RAG Pipeline

SIGNATE「AI ENGINEERING CHALLENGE」向けに検討している、質問非依存の
中間データ構造と抽出処理の実装リポジトリです。

GitHub上の収録物、各ディレクトリの役割、現行系統と履歴系統の違いは
[`REPOSITORY_CATALOG.md`](REPOSITORY_CATALOG.md)を参照してください。

大会から提供されたデータ、評価データ、提出物、生成済みインデックスは含みません。
原本ファイルを`Document`、抽出根拠を`Evidence`、構造的な関係を`Relation`として
保持し、後段の検索用派生データと回答生成から分離します。

## 構成

```text
design/   設計方針と実ファイルでの検証記録
schemas/  中間レコード、検索単位、検索評価ケースのJSON Schema
scripts/  抽出、検索単位生成、診断、整合性検証CLI
```

## 主なスクリプト

- `scripts/build_intermediate_records.py`
  - DOCX、XLSX、PPTX、PDF、CSV、JSON、JSONL、Markdown、Notebook、Python、テキストを再帰的に発見し、中間レコードを生成します。
- `scripts/probe_intermediate_records.py`
  - 少量の代表データでSchemaと抽出結果を確認する診断用プローブです。
- `scripts/validate_intermediate_records.py`
  - ID、ハッシュ、親子関係、原本参照、Relation端点に加え、質問依存データがEvidence層へ混入していないことを検証します。
- `scripts/validate_query_graph_records.py`
  - `QuestionIntentContract`、検索前の`QuestionUnderstandingRun`、完了済み`QueryRun`を、JSON Schemaと決定的semantic検査の二層でfail-closedに検証します。
- `scripts/build_question_understanding.py`
  - 生の質問だけを閉じた`IntentDraft`へ分解し、決定的compilerでQuestionIntentContract、文脈グラフ、論理並列の候補枝、IntentGateへ変換します。質問中の根拠表現と結びつかない解釈は検索へ渡しません。全文一致で一意に読める標準list、有限接尾辞list、2条件からの平均・最近傍IDはモデルを呼ばず決定的に処理します。一意な質問ではモデル候補と決定的正本の意味構造が完全一致する場合だけ検索可能とし、証明できなければ`clarification_required`にします。明示的な曖昧性は全候補を論理枝として保持し、提案入力の由来とSHA-256も監査記録に残します。
- `scripts/build_search_units.py`
  - 完了済み中間データから、質問非依存の検索単位を逐次生成します。
- `scripts/validate_search_units.py`
  - 検索単位のID、本文ハッシュ、元Evidenceへの参照を検証します。
- `scripts/build_lexical_index.py`
  - SearchUnitからAPI不要のSQLite BM25索引を構築します。
- `scripts/search_lexical_index.py`
  - 日本語n-gramのBM25に、表の列値一致と親子関係を使う汎用再ランキングを重ね、元Evidence参照を返します。
- `scripts/validate_lexical_index.py`
  - SQLite内部整合性、件数、入力SearchUnitのSHA-256を検証します。
- `scripts/build_semantic_index.py` / `scripts/validate_semantic_index.py`
  - ローカルOllamaでディスク常駐・再開可能な意味索引を生成し、モデルdigest、行列、元SearchUnitをストリーミング検証します。
- `scripts/search_semantic_index.py` / `scripts/search_hybrid.py`
  - ローカルコサイン検索と、BM25との適応型RRF統合を実行します。
- `rag/main.py` / `rag/answer.py` / `rag/layer1_index.py`
  - 既存の全資料キャッシュを既定値のまま保ち、明示時だけ監査済みLayer 1／ChartTable索引へ切り替えて回答CSV/ZIPを生成します。
- `scripts/build_answer_diagnostic_report.py`
  - 正解付き質問と回答実行ログから、検索順位・スコア・抽出本文・回答を問題ごとに確認できる診断レポートを生成します。

## 画像理解の設計

画像、グラフ、PivotTable、マーカーなどの表示上の情報は、
`design/sequential-multimodal-orchestration.md`の方針で処理します。
その前段として、`design/visual-classification-v1.md`の方針でPDFページ、
Office・Notebook埋め込み画像、単体画像を実体化し、文章、表、
グラフ、図解などを複数ラベルで分類します。
文字転記、視覚状態観測、意味統合を分離し、`gemma4:12b`を1件ずつ
逐次実行します。
分類層v1の出力は品質確認用であり、まだ検索経路には接続しません。
直接画像化できる235件を全件分類し、`ocr_text`へ分類した154画像は、
`design/ocr-observation-v1.md`の
二重OCRで文字と位置を独立観測します。一致しない読みは統合せず、
`unresolved`のまま両方を保存します。このOCR出力もまだ検索経路へ接続しません。

上記の大会向けbatch成果物とは別に、macOS Local Memory SearchのStep 7
source実装は、原本を外部へ送信せず、PDFの各ページと単体画像、
DOCX・XLSX・PPTX・Notebookの参照付き埋め込みラスター画像を
既存のローカルOCRへ接続しています。Office・Notebook埋め込み画像のraw bytesは、
表示representation・crop・透過・transformをまだ再現していないため、OCRが複数方式で一致しても
`[暫定読取]`に降格し、確定グラフから除外します。XLSX・PPTXのOOXMLネイティブグラフは、
関係パーツとハッシュに結合した保存済みcacheを
`verified_ooxml_chart_cache`として検索化します。ただし`verified`は
出典結合と構造検証を表し、Officeアプリでの最新再計算や業務上の正しさまで
保証するものではありません。PPTXのSmartArtは、参照できたテキスト要素と
`srcId`/`destId`の明示接続だけを保持し、接続typeの意味や配置から因果・階層を
推定しません。DOCX内のネイティブグラフ構造は未対応です。

同Readerの`gemma4:12b`による図・表・写真の質問非依存な視覚観測は
検索可能ですが、常に`[暫定読取]`であり、cross-document semantic graphの
Node/Edge候補および確定回答の単独根拠から除外します。OCR不足、cache欠落、
SmartArt端点不解決、非対応画像、または件数・容量・時間の安全上限到達時は
Documentを`partial`とし、欠落を成功扱いしません。Officeのスライド/ページ/
シート全体レンダリングと、独立したAgent 1/2の観測をAgent 3で融合する
完全経路は、引き続き設計段階です。

```bash
python scripts/build_visual_asset_manifest.py --help
python scripts/materialize_visual_assets.py --help
python scripts/classify_visual_assets.py --help
python scripts/validate_visual_asset_manifest.py --help
python scripts/validate_visual_classifications.py --help
python scripts/extract_ocr_observations.py --help
python scripts/validate_ocr_observations.py --help
python scripts/validate_reading_coverage.py --help
```

```bash
python scripts/run_visual_analysis.py \
  --image /path/to/image.png \
  --source-root /path/to/source-root \
  --out /path/to/visual-analysis-output \
  --model gemma4:12b

python scripts/validate_visual_analysis.py \
  /path/to/visual-analysis-output/analysis.json
```

工程ごとにモデルdigest、画像SHA-256、プロンプトversionを照合し、
完了済みの工程を再利用します。検証にはJSON Schema Draft 2020-12の
FormatCheckerも使い、キャッシュ利用と初回推論時刻を別々に記録します。

- `scripts/validate_submission.py`
  - ヘッダーなし2列、全index、空欄、1000トークン上限、ZIP内部名を提出前に検証します。
- `scripts/build_self_retrieval_eval.py`
  - 全形式共通規則で、検索配線確認用の自己検索評価セットを生成します。
- `scripts/evaluate_lexical_retrieval.py`
  - 正解IDを検索後に照合し、Recall@k、Hit@k、MRRを計算します。
- `scripts/finalize_human_retrieval_eval.py`
  - 原本確認済みの質問案を検証し、人手確認済み評価セットとして確定します。
- `scripts/remap_retrieval_eval_draft.py`
  - SearchUnit更新時に文書・種別・位置が同じ正解だけを新IDへ安全に対応付けます。
- `scripts/build_layer1_deliverables.py` / `scripts/validate_layer1_deliverables.py`
  - 原本台帳、正規化文書、失敗一覧、チャンク、評価レポートをレイヤー1成果物として固定・検証します。
- `scripts/build_chart_intermediate.py`
  - 検証済みChartTableを、画像Document、グラフEvidence、系列Evidence、Relationへ変換します。

## 実行例

開発・高忠実度Readerでは`python-docx`、`openpyxl`、`python-pptx`、`pypdf`、
NumPy、pandas、Pillow、jsonschemaを使用します。配布版のDOCX・XLSX・PPTXには
Python標準ライブラリのOOXML fallbackもあり、これらの追加パッケージが
ないMacでも本文・表・対応図表を制限付きで保持します。意味索引にはOllamaと
ローカルの`embeddinggemma`が追加で必要です。

```bash
python scripts/build_intermediate_records.py \
  --root /path/to/source-root \
  --out /path/to/new-output-directory

python scripts/validate_intermediate_records.py \
  /path/to/new-output-directory \
  --root /path/to/source-root

python scripts/validate_query_graph_records.py \
  /path/to/question-intent-contract-understanding-run-or-query-run.json

python scripts/build_question_understanding.py \
  /path/to/question-only.jsonl \
  --out /path/to/question-understanding-runs.jsonl \
  --model gemma4:12b

python scripts/build_search_units.py \
  --intermediate /path/to/new-output-directory \
  --out /path/to/new-search-output-directory

python scripts/validate_search_units.py \
  /path/to/new-search-output-directory \
  --intermediate /path/to/new-output-directory

python scripts/build_lexical_index.py \
  --search-output /path/to/new-search-output-directory \
  --out /path/to/new-index-directory

python scripts/validate_lexical_index.py \
  /path/to/new-index-directory \
  --search-output /path/to/new-search-output-directory

ollama pull embeddinggemma

python scripts/build_semantic_index.py \
  --search-output /path/to/new-search-output-directory \
  --out /path/to/new-semantic-index-directory \
  --model embeddinggemma \
  --resume

# 既存意味索引のSearchUnit列が完全なprefixなら、追加分だけを埋め込む場合
python scripts/build_semantic_index.py \
  --search-output /path/to/search-output-with-appended-units \
  --out /path/to/new-semantic-index-directory \
  --base-index /path/to/existing-semantic-index-directory \
  --resume

python scripts/validate_semantic_index.py \
  /path/to/new-semantic-index-directory \
  --search-output /path/to/new-search-output-directory

python scripts/search_lexical_index.py \
  --index /path/to/new-index-directory \
  --intermediate /path/to/new-output-directory \
  --query '検索したい内容' \
  --top-k 10

# 純粋なBM25基準線を再現する場合
python scripts/search_lexical_index.py \
  --index /path/to/new-index-directory \
  --query '検索したい内容' \
  --top-k 10 \
  --field-value-weight 0 \
  --parent-context-penalty 0

python scripts/search_hybrid.py \
  --lexical-index /path/to/new-index-directory \
  --semantic-index /path/to/new-semantic-index-directory \
  --intermediate /path/to/new-output-directory \
  --query '検索したい内容' \
  --top-k 10

python scripts/build_self_retrieval_eval.py \
  --search-output /path/to/new-search-output-directory \
  --out /path/to/new-evaluation-directory \
  --max-cases 100

python scripts/finalize_human_retrieval_eval.py \
  --search-output /path/to/new-search-output-directory \
  --draft /path/to/reviewed-draft.jsonl \
  --out /path/to/new-human-evaluation-directory

python scripts/remap_retrieval_eval_draft.py \
  --old-search-output /path/to/old-search-output-directory \
  --new-search-output /path/to/new-search-output-directory \
  --evaluation-set /path/to/old-evaluation-set.jsonl \
  --out-draft /path/to/remapped-reviewed-draft.jsonl

python scripts/evaluate_lexical_retrieval.py \
  --index /path/to/new-index-directory \
  --evaluation-set /path/to/new-evaluation-directory/evaluation-set.jsonl \
  --intermediate /path/to/new-output-directory \
  --semantic-index /path/to/new-semantic-index-directory \
  --k 1 3 5 10

# 同じ評価セットで意味検索だけを測定する場合
python scripts/evaluate_lexical_retrieval.py \
  --index /path/to/new-index-directory \
  --evaluation-set /path/to/new-evaluation-directory/evaluation-set.jsonl \
  --intermediate /path/to/new-output-directory \
  --semantic-index /path/to/new-semantic-index-directory \
  --semantic-only \
  --k 1 3 5 10

python scripts/build_layer1_deliverables.py \
  --root /path/to/source-root \
  --intermediate /path/to/new-output-directory \
  --search-output /path/to/new-search-output-directory \
  --evaluation-report /path/to/evaluation-bm25.json \
  --evaluation-report /path/to/evaluation-semantic.json \
  --evaluation-report /path/to/evaluation-hybrid.json \
  --out /path/to/layer1-deliverables

python scripts/validate_layer1_deliverables.py \
  /path/to/layer1-deliverables

python -m unittest discover -s tests -v

# 既存提出経路を変えずに、監査済みLayer 1 + ChartTable索引を回答経路で確認
# layer1-hybridは人手確認済み評価で最良だった固定weight RRFを使用
python rag/main.py --valid --dry-run --limit 5 \
  --retrieval-mode layer1-lexical

python rag/main.py --valid --dry-run --limit 5 \
  --retrieval-mode layer1-hybrid

python scripts/build_answer_diagnostic_report.py \
  --questions share/質問回答/questions_valid.csv \
  --run-log rag/logs/run_YYYYMMDD_HHMMSS.json \
  --reviews rag/logs/diagnostic_valid_reviews.json \
  --out-jsonl rag/logs/diagnostic_valid.jsonl \
  --out-md rag/logs/diagnostic_valid.md
```

回答診断の自動分類は、正解文字列が抽出コーパスや検索上位にあるかを使う
一次判定です。計算、比較、複数資料の列挙はMarkdown内の検索本文を目視し、
`human_review`欄で最終分類を確定します。

人手確認済み評価セットは、`query`、`relevant_search_unit_ids`、`category`、`review`を
持つJSONL下書きを`finalize_human_retrieval_eval.py`へ渡して確定します。生成した評価
セットとレポートは`artifacts/`へ保存できますが、大会データ由来のためGitには含めません。

## Phase 2.5 汎用質問・データ契約

QuestionClauseIR、QuestionIntentContract、質問非依存DataCatalogを分離し、
CatalogResolutionRunでのみ結合します。Catalogには行値、質問、回答、
relevanceを保存しません。SearchUnitの各表行が完全ヘッダー、同一列構成、
一意な`header: value`形式を満たす場合だけ、structured capabilityと列型を
質問非依存で宣言します。

```bash
python scripts/build_data_catalog.py \
  --documents artifacts/layer1-v1/intermediate/documents.jsonl \
  --search-units artifacts/layer1-v1/search/search_units.jsonl \
  --entries-out artifacts/phase2-5/data-catalog-entries.jsonl \
  --snapshot-out artifacts/phase2-5/data-catalog-snapshot.json

python scripts/validate_data_catalog.py \
  --entries artifacts/phase2-5/data-catalog-entries.jsonl \
  --snapshot artifacts/phase2-5/data-catalog-snapshot.json \
  --documents artifacts/layer1-v1/intermediate/documents.jsonl \
  --search-units artifacts/layer1-v1/search/search_units.jsonl

# 現行RAGを起動せず、3条件gateの差分だけを記録
python scripts/run_phase25_shadow.py \
  --qur /path/to/question-understanding-run.jsonl \
  --entries artifacts/phase2-5/data-catalog-entries.jsonl \
  --snapshot artifacts/phase2-5/data-catalog-snapshot.json \
  --clause-ir-out /path/to/question-clause-ir.jsonl \
  --resolution-out /path/to/catalog-resolution-run.jsonl

# resolvedの場合だけ再計算し、CLIは値を保存せず件数だけ表示
python scripts/execute_structured_resolution.py \
  --qur /path/to/question-understanding-run.jsonl \
  --clause-ir /path/to/question-clause-ir.jsonl \
  --resolution /path/to/catalog-resolution-run.jsonl \
  --entries artifacts/phase2-5/data-catalog-entries.jsonl \
  --snapshot artifacts/phase2-5/data-catalog-snapshot.json \
  --search-units artifacts/layer1-v1/search/search_units.jsonl
```

現スナップショットは340 Document、412,744 SearchUnitから1,028 Entryを作り、
136 Entryをstructured実行可能と認定します。この認定はCatalog作成時と
実行時の両方で再計算され、SearchUnit stream SHAが変われば実行を拒否します。
ただしPhase 2.5はまだshadow modeであり、現行`rag/main.py`の検索開始条件には
接続していません。

Layer 1成果物では原文と正規化後を別JSONLに保持します。原本台帳には各ファイルの
SHA-256を記録し、`layer1-state.json`には中間層・SearchUnit・評価レポートの入力hashと
チャンク設定を固定します。Unicode・空白・制御文字を
決定的に正規化し、3ページ以上で完全一致するPDF先頭／末尾行だけを重複ヘッダー・
フッター候補とします。初出は残し、以後を正規化側から除去して、その操作内容も
各レコードへ残します。

出力先が空でない場合は上書きせず停止します。中間データの出力先は、再帰的な
自己取り込みを防ぐため原本ルートの外側へ指定してください。

中断したビルドは、同じルートと出力先を指定して再開できます。

```bash
python scripts/build_intermediate_records.py \
  --root /path/to/source-root \
  --out /path/to/existing-output-directory \
  --resume
```

`build-state.json`には原本、ファイル単位シャード、集約JSONLの順序付き
SHA-256/件数/サイズと、Reader、OCR、ローカルVLMのモデルdigest・
prompt・loopback Ollama版、通常parser依存を含む
processing fingerprintを記録します。再開時は原本・シャード・fingerprintが
すべて一致する`success`/`partial`/`deferred`だけを再利用し、不一致と`failed`は
再処理します。ValidatorとLocal Memory変換入口は集約JSONLをstateと独立照合し、
追記や順序改変をfail-closedに拒否します。

## 現在の状態

基礎抽出器は質問文、案件名、record IDによる特別分岐を持ちません。
形式別の未対応情報が残っている間はDocumentを`partial`として記録します。
EvidenceとRelationはファイル単位シャードへ逐次書き出すため、全レコードを
Pythonのメモリへ保持しません。検索用派生層は段落チャンク、ヘッダー候補付き表行、
親見出し付きDOCX表行、スライド、PDFページ、Notebookセル、コードブロック、検証済み
ChartTableの要約・系列を`SearchUnit`へ変換し、元Evidence IDを保持します。
さらに、外部APIを使わないSQLite BM25索引を構築し、日本語文字n-gramによる
検索結果から元Evidenceまで追跡できます。提出までの先行経路として、既存の全資料
抽出キャッシュとローカル`gemma4:12b`を接続したAPIキー不要の回答生成も利用できます。

## データ管理

このリポジトリには次のものをコミットしません。

- 大会提供データおよび評価データ
- PDF、Office原本、ZIPアーカイブ
- 回答、提出ファイル、生成済み検索インデックス
- APIキー、認証情報、ローカル設定
### グラフの元データ優先復元（ChartTable）

グラフ画像は、まず生成元コードとデータを探します。対応する `savefig`と制約済みの pandas `groupby` パターンが見つかれば、ノートブックは実行せずに同じ集計だけを再計算し、質問非依存の `ChartTable` を作成します。元データが回収できないグラフだけ、逐次マルチモーダル解析へ送る想定です。

```bash
python scripts/build_chart_source_candidates.py \
  --root /path/to/analysis_project \
  --image figure_06.png \
  --out rag/chart-tables/figure_06/source-candidates.json

python scripts/recover_groupby_chart_table.py \
  --notebook /path/to/analysis_project/notebooks/01_eda.ipynb \
  --image /path/to/analysis_project/reports/figures/figure_06.png \
  --project-root /path/to/analysis_project \
  --out rag/chart-tables/figure_06/chart-table.json

python scripts/validate_chart_table.py \
  rag/chart-tables/figure_06/chart-table.json

python scripts/build_chart_intermediate.py \
  --chart-table rag/chart-tables/figure_06/chart-table.json \
  --root /path/to/analysis_project \
  --base-intermediate /path/to/base-intermediate \
  --out /path/to/chart-intermediate

python scripts/build_search_units.py \
  --intermediate /path/to/base-intermediate /path/to/chart-intermediate \
  --out /path/to/search-output-with-charts

python scripts/build_chart_views.py \
  --image /path/to/chart.png \
  --out rag/chart-views/chart-name
```

スキーマは `schemas/chart-table.schema.json` です。各値に `exact / estimated / unresolved` を持たせ、系列数・点数・軸との対応と、コードおよびデータの SHA-256 を検証可能な形で残します。質問文や正解は、候補探索にも表復元にも渡しません。
