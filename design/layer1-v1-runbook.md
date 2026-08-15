# Layer 1 v1 実行・検証手順

## スコープ

対象はネイティブ文字データです。OCR本処理とグラフ画像の視覚解析は実行しません。
ChartTableは、元Notebookと元CSVから質問非依存で復元・検証済みのレコードだけを、
Layer 1完成後に追加接続します。

初回提出版の基準点はGit commit `fe408b05859d33bec1cdfd4167dfe1acaa31704f`です。
改善作業は`codex/layer1-charttable`で行い、`main`と`origin/main`は基準点のまま保持します。
`rag/out/submission.zip`とDesktop保全コピーはともに1,547 bytes、SHA-256
`30aa611b49493058371af7879df690698596a66ea601b9dfe9ed65756d35056c`で、上書きしていません。

## 固定入力と出力

- 原本ルート: `share/共有ドライブ`
- 中間層: `artifacts/layer1-v1/intermediate`
- SearchUnit: `artifacts/layer1-v1/search`
- BM25: `artifacts/layer1-v1/lexical-index`
- Embedding: `artifacts/layer1-v1/semantic-index`
- Layer 1成果物: `artifacts/layer1-v1/deliverables`
- ChartTable中間層: `artifacts/layer1-v1/chart-intermediate`
- ChartTable接続済みSearchUnit: `artifacts/layer1-v1/search-with-charts`
- ChartTable接続済みBM25: `artifacts/layer1-v1/lexical-index-with-charts`
- ChartTable接続済みEmbedding: `artifacts/layer1-v1/semantic-index-with-charts`

生成物は大会データ由来なのでGitへ追加しません。各state JSONに入力SHA-256、出力SHA-256、
件数、builder versionを保存します。

## 1. ネイティブ中間層

```bash
rag/.venv/bin/python scripts/build_intermediate_records.py \
  --root 'share/共有ドライブ' \
  --out artifacts/layer1-v1/intermediate \
  --run-at '2026-08-15T00:00:00+00:00'

rag/.venv/bin/python scripts/validate_intermediate_records_streaming.py \
  artifacts/layer1-v1/intermediate \
  --root 'share/共有ドライブ'
```

中断時は同じ入力と出力へ`--resume`を付けます。入力または完了済みshardのSHA-256が
一致しない場合は再利用しません。

## 2. SearchUnitとBM25

```bash
rag/.venv/bin/python scripts/build_search_units.py \
  --intermediate artifacts/layer1-v1/intermediate \
  --out artifacts/layer1-v1/search

rag/.venv/bin/python scripts/validate_search_units_streaming.py \
  artifacts/layer1-v1/search \
  --intermediate artifacts/layer1-v1/intermediate

rag/.venv/bin/python scripts/build_lexical_index.py \
  --search-output artifacts/layer1-v1/search \
  --out artifacts/layer1-v1/lexical-index

rag/.venv/bin/python scripts/validate_lexical_index.py \
  artifacts/layer1-v1/lexical-index \
  --search-output artifacts/layer1-v1/search
```

## 3. Embedding

Ollamaの`embeddinggemma`を使用します。APIへ送るのはローカルOllamaだけです。

```bash
rag/.venv/bin/python scripts/build_semantic_index.py \
  --search-output artifacts/layer1-v1/search \
  --out artifacts/layer1-v1/semantic-index \
  --model embeddinggemma \
  --batch-size 64 \
  --timeout 300 \
  --resume

rag/.venv/bin/python scripts/validate_semantic_index.py \
  artifacts/layer1-v1/semantic-index \
  --search-output artifacts/layer1-v1/search
```

EmbeddingはSQLiteキャッシュ、進捗JSON、ディスク常駐NumPy行列を使って再開できます。
OllamaがHTTP 400でバッチを拒否した場合は自動的に二分し、単一入力も失敗するときだけ停止します。
完了時にキャッシュと進捗ファイルを削除し、行列・文書・offsetのハッシュをstateへ固定します。

## 4. 同一Ground Truthで比較

Ground Truthは原本位置を人手確認した16件だけです。評価は検索後に正解SearchUnit IDを照合します。

```bash
# 純粋BM25
rag/.venv/bin/python scripts/evaluate_lexical_retrieval.py \
  --index artifacts/layer1-v1/lexical-index \
  --evaluation-set artifacts/layer1-v1/evaluation/finalized/evaluation-set.jsonl \
  --intermediate artifacts/layer1-v1/intermediate \
  --field-value-weight 0 \
  --parent-context-penalty 0 \
  --k 1 3 5 10 \
  --out artifacts/layer1-v1/evaluation/evaluation-bm25-v3.json

# BM25 + 汎用表値／親子rerank
rag/.venv/bin/python scripts/evaluate_lexical_retrieval.py \
  --index artifacts/layer1-v1/lexical-index \
  --evaluation-set artifacts/layer1-v1/evaluation/finalized/evaluation-set.jsonl \
  --intermediate artifacts/layer1-v1/intermediate \
  --k 1 3 5 10 \
  --out artifacts/layer1-v1/evaluation/evaluation-lexical-reranked-v3.json

# Embeddingのみ
rag/.venv/bin/python scripts/evaluate_lexical_retrieval.py \
  --index artifacts/layer1-v1/lexical-index \
  --evaluation-set artifacts/layer1-v1/evaluation/finalized/evaluation-set.jsonl \
  --intermediate artifacts/layer1-v1/intermediate \
  --semantic-index artifacts/layer1-v1/semantic-index \
  --semantic-only \
  --k 1 3 5 10 \
  --out artifacts/layer1-v1/evaluation/evaluation-semantic.json

# 固定weight RRF
rag/.venv/bin/python scripts/evaluate_lexical_retrieval.py \
  --index artifacts/layer1-v1/lexical-index \
  --evaluation-set artifacts/layer1-v1/evaluation/finalized/evaluation-set.jsonl \
  --intermediate artifacts/layer1-v1/intermediate \
  --semantic-index artifacts/layer1-v1/semantic-index \
  --semantic-weight 0.25 \
  --k 1 3 5 10 \
  --out artifacts/layer1-v1/evaluation/evaluation-hybrid-fixed.json

# 最新の適応weight RRF
rag/.venv/bin/python scripts/evaluate_lexical_retrieval.py \
  --index artifacts/layer1-v1/lexical-index \
  --evaluation-set artifacts/layer1-v1/evaluation/finalized/evaluation-set.jsonl \
  --intermediate artifacts/layer1-v1/intermediate \
  --semantic-index artifacts/layer1-v1/semantic-index \
  --semantic-weight 0.25 \
  --adaptive-semantic \
  --k 1 3 5 10 \
  --out artifacts/layer1-v1/evaluation/evaluation-hybrid-adaptive.json
```

## 5. Layer 1成果物

```bash
rag/.venv/bin/python scripts/build_layer1_deliverables.py \
  --root 'share/共有ドライブ' \
  --intermediate artifacts/layer1-v1/intermediate \
  --search-output artifacts/layer1-v1/search \
  --evaluation-report artifacts/layer1-v1/evaluation/evaluation-bm25-v3.json \
  --evaluation-report artifacts/layer1-v1/evaluation/evaluation-lexical-reranked-v3.json \
  --evaluation-report artifacts/layer1-v1/evaluation/evaluation-semantic.json \
  --evaluation-report artifacts/layer1-v1/evaluation/evaluation-hybrid-fixed.json \
  --evaluation-report artifacts/layer1-v1/evaluation/evaluation-hybrid-adaptive.json \
  --out artifacts/layer1-v1/deliverables

rag/.venv/bin/python scripts/validate_layer1_deliverables.py \
  artifacts/layer1-v1/deliverables
```

成果物は`text_inventory.csv`、raw/normalized JSONL、品質問題一覧、追跡可能なchunk、
評価CSV・要約・失敗分析・実験ログの9点です。台帳に原本SHA-256を持たせ、
`layer1-state.json`に中間層・SearchUnit・評価レポートの入力hashと`target_chars`を固定します。

## 6. 検証済みChartTableの追加接続

```bash
rag/.venv/bin/python scripts/build_chart_intermediate.py \
  --root 'share/共有ドライブ' \
  --chart-table rag/chart-tables/figure_06/chart-table.json \
  --base-intermediate artifacts/layer1-v1/intermediate \
  --out artifacts/layer1-v1/chart-intermediate

rag/.venv/bin/python scripts/build_search_units.py \
  --intermediate artifacts/layer1-v1/intermediate artifacts/layer1-v1/chart-intermediate \
  --out artifacts/layer1-v1/search-with-charts

rag/.venv/bin/python scripts/build_lexical_index.py \
  --search-output artifacts/layer1-v1/search-with-charts \
  --out artifacts/layer1-v1/lexical-index-with-charts

rag/.venv/bin/python scripts/build_semantic_index.py \
  --search-output artifacts/layer1-v1/search-with-charts \
  --out artifacts/layer1-v1/semantic-index-with-charts \
  --base-index artifacts/layer1-v1/semantic-index \
  --batch-size 3 \
  --timeout 300 \
  --resume

rag/.venv/bin/python scripts/validate_semantic_index.py \
  artifacts/layer1-v1/semantic-index-with-charts \
  --search-output artifacts/layer1-v1/search-with-charts \
  --base-index artifacts/layer1-v1/semantic-index
```

追加接続前後で、base SearchUnit JSONLが結合版の完全なbyte prefixであることを確認します。
その条件を満たす場合だけ、意味索引のbase 412,744行を再利用し、追加3行だけを埋め込みます。

既存提出経路は既定値`baseline`のままです。監査済み索引を回答経路で使う場合だけ、
明示的に次を指定します。

```bash
rag/.venv/bin/python rag/main.py --valid --dry-run --limit 5 \
  --retrieval-mode layer1-lexical

rag/.venv/bin/python rag/main.py --valid --dry-run --limit 5 \
  --retrieval-mode layer1-hybrid
```

Layer 1モードは上位結果を既存の回答用`Chunk`へ変換し、原本ファイルと位置を実行ログへ
残します。会社名が質問にある場合は対象会社へ文書を絞り、会社名自体は内容検索語から
外します。これにより会社名の反復より質問本文を優先します。`layer1-hybrid`は、16問で
最良だった固定weight RRFを使用します。適応weight RRFは比較用評価として保持します。

## 7. 回帰検証

```bash
rag/.venv/bin/python -m unittest discover -s tests -v
git diff --check
rag/.venv/bin/python -m pip check
```

最終件数と評価値は、すべての処理が完了した時点のcompletion reportへ記録します。
