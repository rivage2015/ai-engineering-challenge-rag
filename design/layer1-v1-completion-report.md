# Layer 1 v1 完了報告

最終更新: 2026-08-15

## 結論

Layer 1のネイティブ文字抽出、中間層、SearchUnit、BM25、Embedding、Hybrid、
Retrieval評価、監査成果物を完成させた後、検証済みChartTableを同じ経路へ追加接続した。
OCR本処理とグラフ画像の視覚解析は開始していない。

## 1. 調査した現在の実装

- 既存回答経路: `rag/chunks.jsonl` 2,125 chunks + `rag/index.py` BM25
- 既存回答生成: ローカルOllama `gemma4:12b`または明示時のみOpenAI
- 既存意味検索: 小規模検証artifactのみで、全資料の回答経路には未接続
- ChartTable: 元Notebookと元CSVから復元・検証済みだったが、Evidence/SearchUnit未接続
- 初回提出ZIP: 1,547 bytes、SHA-256 `30aa611b49493058371af7879df690698596a66ea601b9dfe9ed65756d35056c`

## 2. 実装済みだったもの

- Office/PDFの質問非依存なDocument/Evidence/Relation抽出
- 再開可能なファイル単位shard
- SearchUnitとSQLite BM25
- ローカルOllama回答生成、提出ZIP検証、回答診断
- ChartTable復元・検証と逐次視覚解析の設計

## 3. 今回変更したもの

- CSV/TSV、JSON/XML、Markdown/テキスト、コード、Notebookを全件抽出対象へ追加
- Officeのheader/footer/note/style/comment/image、暗号化Officeのローカル復号を補完
- raw/normalized分離、品質問題集約、PDF反復edgeの初出保持型正規化
- 大規模用ストリーミングvalidator、ディスク常駐・再開可能Semantic indexを追加
- 同一の人手確認済みGround Truthで5方式を比較可能にした
- 検索結果へfile/page/sheet/slide/section/chunk/evidence textを付与
- 検証済みChartTableをDocument/Evidence/Relation、SearchUnit、BM25、Embedding、Hybridへ接続
- 既存回答経路を既定値のまま保ち、明示時だけLayer 1索引へ切り替えるadapterを追加

## 4. 変更ファイル

- ルート・文書: `.gitignore`、`README.md`、`design/layer1-v1-runbook.md`、
  `design/layer1-v1-completion-report.md`
- 回答経路: `rag/main.py`、`rag/layer1_index.py`、`rag/requirements.txt`
- schema: `schemas/search-unit.schema.json`
- build: `scripts/build_intermediate_records.py`、`scripts/probe_intermediate_records.py`、
  `scripts/build_search_units.py`、`scripts/build_semantic_index.py`、
  `scripts/build_chart_intermediate.py`、`scripts/build_layer1_deliverables.py`
- retrieval/eval: `scripts/search_lexical_index.py`、`scripts/search_semantic_index.py`、
  `scripts/search_hybrid.py`、`scripts/ollama_embedding_common.py`、
  `scripts/evaluate_lexical_retrieval.py`、`scripts/finalize_human_retrieval_eval.py`、
  `scripts/remap_retrieval_eval_draft.py`、`scripts/retrieval_trace_common.py`
- validation: `scripts/validate_intermediate_records.py`、
  `scripts/validate_intermediate_records_streaming.py`、`scripts/validate_search_units.py`、
  `scripts/validate_search_units_streaming.py`、`scripts/validate_semantic_index.py`、
  `scripts/validate_layer1_deliverables.py`
- tests: `tests/test_layer1_pipeline.py`

## 5. テスト内容

- CSVから中間層、SearchUnit、BM25までの小規模E2E
- ChartTable追加時のbase SearchUnit byte-prefix不変性
- Semantic indexの完全prefix再利用と追加行だけの埋め込み
- OllamaがHTTP 400で拒否した埋め込みバッチの安全な二分再試行
- cosine scoreの分割計算、float64累積、クエリ・score有限性検査
- raw/normalized対応、成果物hash、chunk前後リンク
- CP932とUTF-16の誤判定防止
- confirmed Ground Truthと検索結果trace
- PDF反復header/footer候補の初出保持
- Layer 1回答adapterからChartTable系列全文への到達
- 回答adapterが最良評価の固定RRFを使用すること
- 単一入力の旧SearchUnit stateもhash付きで検証できる後方互換
- 全Python AST、全CLI `--help`、`git diff --check`、`pip check`
- 全量中間層、SearchUnit、BM25、Semantic indexのstate/hash/count検証

## 6. テスト結果

- `python -m unittest discover -s tests -v`: 12 tests passed
- baseline `rag/main.py --valid --dry-run --limit 1`: passed
- Layer 1 lexical / best fixed Hybrid dry-run: passed
- 全量中間層、base/結合SearchUnit、base/結合BM25、base/結合Semantic、成果物9点: passed
- ChartTable schemaと元CSV 27,128行からの62点再計算: mismatch 0
- Python AST 144件、CLI `--help` 38件、`pip check`、`実行.command`構文、`git diff --check`: passed
- 初回提出ZIP: 100行、最大保守上限430 / 1000、1,547 bytes、hash不変

## 7. 全資料件数

Inventory対象は403ファイル。内訳はネイティブ抽出対象340、画像54、未対応`.lock` 9。
複数layerを持つファイルがあるため、下記OCR/図表件数とは排他的ではない。

## 8. native_text件数

340文書。Evidence 3,085,524件、Relation 3,085,586件を生成した。内容参照だけのEvidenceを除き、
raw / normalizedは各3,085,341件、反復PDF edgeの正規化操作は12件。

## 9. 正常抽出件数

313文書。

## 10. 一部成功件数

27文書。警告、画像、ネイティブ文字層のないPDFページなどをissuesへ分離した。

## 11. 抽出失敗件数

0文書。

## 12. OCR対象件数

`processing_layer=ocr_required`は23ファイル。OCR本処理は未実行。

## 13. 図表解析対象件数

`processing_layer=graph_required`は74ファイル。検証済みChartTable 1件以外の画像解釈は未実行。

SearchUnitは412,744件。内訳はtable row 406,948、text 3,361、paragraph 1,529、
slide 385、notebook 211、code 202、PDF page 108。

## 14. BM25評価

人手確認済み16件、検索後照合。

| 方式 | Hit@1 | Hit@3 | Hit@5 | Hit@10 | MRR |
|---|---:|---:|---:|---:|---:|
| 純粋BM25 | 0.4375 | 0.6875 | 0.6875 | 0.6875 | 0.5521 |
| BM25 + 汎用rerank | 0.3750 | 0.7500 | 0.8125 | 0.8125 | 0.5542 |

## 15. Embedding評価

ローカル`embeddinggemma`、768次元。412,744件中298,845件をAPI生成、同一文面113,899件を
キャッシュ再利用した。全ベクトルはfiniteかつL2正規化済み。

| 方式 | Hit@1 | Hit@3 | Hit@5 | Hit@10 | Recall@10 | MRR |
|---|---:|---:|---:|---:|---:|---:|
| Embedding cosine | 0.4375 | 0.6875 | 0.8125 | 0.8750 | 0.8438 | 0.5802 |

## 16. Hybrid評価

| 方式 | Hit@1 | Hit@3 | Hit@5 | Hit@10 | Recall@10 | MRR |
|---|---:|---:|---:|---:|---:|---:|
| 固定RRF、semantic weight 0.25 | 0.4375 | 0.8125 | 0.8125 | 0.8750 | 0.8438 | 0.6224 |
| 適応RRF | 0.4375 | 0.7500 | 0.8125 | 0.8750 | 0.8750 | 0.6094 |

## 17. 最良のchunk設定

今回評価した採用設定は、文書構造を優先し、段落結合だけ`target_chars=1200`、overlap 0、
表は列名付き1行、PowerPointは1スライド、PDFは1ページ、コードは80行、Notebookは1セル。
短い構造単位は無理に連結・分割せず、単一の長い段落も切断しない。全方式をこの同一設定で
比較したため、異なるtoken上限間の優劣は未評価である。

## 18. 最良のRetrieval設定

5方式をHit@1、MRR、Hit@10の順で比較し、固定RRF
`BM25-field-parent+local-semantic-RRF`を最良設定とした。適応RRFはRecall@10が0.8750へ
上がった一方、MRRとHit@3が固定RRFを下回ったため、無条件には採用しない。

## 19. 初回baselineとの差

- baseline: `rag/chunks.jsonl` 2,125件 + 既存BM25、既定回答経路
- Layer 1: 412,744 SearchUnit、原本位置trace、BM25・Embedding・Hybrid、評価・失敗分析
- ChartTable接続後: 412,747 SearchUnit
- SIGNATEへの再提出・再採点はしていないため、総合スコア差は未計測
- baseline既定値と初回提出ZIPは変更していない

## 20. 失敗パターン

- BM25は複数段落をまたぐ質問、状態語が反復する表行、短いスライド語句で取りこぼした。
- field-aware rerankは請求表とスライドをTop 5へ改善した一方、既に1位の一部表行を下げた。
- 5方式合計でTop 5外は17 method-case。Embeddingは状態語の表行と短いスライド、Hybridは
  複数段落・状態語の表行に失敗が残った。全件は`text_error_analysis.md`に記録した。

## 21. 未解決問題

- PDFはページ単位のネイティブ文字を保持したが、段組み・図との位置関係は次層の対象。
- 評価は人手確認済み16問であり、共有ドライブ全体を代表する大規模評価ではない。
- 品質issueは163件（deferred 97、unresolved 64、resolved 2）。OCR/レイアウト/空文字を
  次フェーズへ明示的に残した。
- Layer 1を使った回答生成の全30問再評価とSIGNATE再提出は未実施。
- pytest/ruff/CIはなく、標準`unittest`と各全量validatorで検証している。

## 22. Human reviewが必要な項目

- Ground Truth 16問とChartTable 3問は全件確認済みで、今回のRetrieval比較に未確認正解はない。
- OCR候補、未検証の図表候補、PDFレイアウト警告は次フェーズで人手確認対象になる。
- 既定回答経路をLayer 1へ切り替える前に、valid 30問の回答品質レビューが必要。

## 23. OCRフェーズへ進む際の推奨事項

`text_inventory.csv`の`ocr_required`と`text_extraction_issues.csv`のページ位置を入力にし、
ネイティブ文字があるページを再OCRしない。OCR結果は同じDocumentへ新しいEvidenceとして追加し、
原画像・ページ・confidenceを保持する。OCR専用の人手確認済み評価セットを追加し、今回と同じ
BM25／Embedding／Hybrid比較を再実行する。図表はOCR文字列とChartTableを混同しない。

## ChartTable追加接続

- 画像Document: 1
- Chart Evidence: 1
- Chart-series Evidence: 2
- Relation: 3
- 追加SearchUnit: chart summary 1 + chart series 2
- 結合後SearchUnit: 412,747
- base 812,511,150 bytesは結合版の完全なbyte prefix
- lexical 3問: BM25 Hit@1 0.6667 / Hit@10 1.0000 / MRR 0.7222
- lexical rerank 3問: Hit@1 0.6667 / Hit@3 1.0000 / MRR 0.7778
- Embedding、固定RRF、適応RRFはいずれも3問でHit@1 / Hit@10 / MRR 1.0000
- 結合Semantic indexはbase 412,744行を全値・文書・offset完全prefixとして再利用し、3行だけ生成
- 回答adapterの会社名付き質問では、件数系列が1位、`20日 / 1,612件`を含む全文801文字を保持

## 停止位置

OCR本処理と未検証グラフ画像解析の開始直前で停止する。既定の提出経路と初回提出ZIPは
変更していない。再実行方法は`design/layer1-v1-runbook.md`を参照。
