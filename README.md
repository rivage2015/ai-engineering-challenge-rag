# AI Engineering Challenge - Intermediate RAG Pipeline

SIGNATE「AI ENGINEERING CHALLENGE」向けに検討している、質問非依存の
中間データ構造と抽出処理の実装リポジトリです。

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
  - DOCX、XLSX、PPTX、PDFを再帰的に発見し、中間レコードを生成します。
- `scripts/probe_intermediate_records.py`
  - 少量の代表データでSchemaと抽出結果を確認する診断用プローブです。
- `scripts/validate_intermediate_records.py`
  - ID、ハッシュ、親子関係、原本参照、Relation端点を検証します。
- `scripts/build_search_units.py`
  - 完了済み中間データから、質問非依存の検索単位を逐次生成します。
- `scripts/validate_search_units.py`
  - 検索単位のID、本文ハッシュ、元Evidenceへの参照を検証します。
- `scripts/build_lexical_index.py`
  - SearchUnitからAPI不要のSQLite BM25索引を構築します。
- `scripts/search_lexical_index.py`
  - 日本語文字n-gramと英数字語を使って検索し、元Evidence参照を返します。
- `scripts/validate_lexical_index.py`
  - SQLite内部整合性、件数、入力SearchUnitのSHA-256を検証します。
- `scripts/build_self_retrieval_eval.py`
  - 全形式共通規則で、検索配線確認用の自己検索評価セットを生成します。
- `scripts/evaluate_lexical_retrieval.py`
  - 正解IDを検索後に照合し、Recall@k、Hit@k、MRRを計算します。

## 実行例

依存ライブラリとして`python-docx`、`openpyxl`、`python-pptx`、`pypdf`を使用します。

```bash
python scripts/build_intermediate_records.py \
  --root /path/to/source-root \
  --out /path/to/new-output-directory

python scripts/validate_intermediate_records.py \
  /path/to/new-output-directory \
  --root /path/to/source-root

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

python scripts/search_lexical_index.py \
  --index /path/to/new-index-directory \
  --query '検索したい内容' \
  --top-k 10

python scripts/build_self_retrieval_eval.py \
  --search-output /path/to/new-search-output-directory \
  --out /path/to/new-evaluation-directory \
  --max-cases 100

python scripts/evaluate_lexical_retrieval.py \
  --index /path/to/new-index-directory \
  --evaluation-set /path/to/new-evaluation-directory/evaluation-set.jsonl \
  --k 1 3 5 10
```

出力先が空でない場合は上書きせず停止します。中間データの出力先は、再帰的な
自己取り込みを防ぐため原本ルートの外側へ指定してください。

中断したビルドは、同じルートと出力先を指定して再開できます。

```bash
python scripts/build_intermediate_records.py \
  --root /path/to/source-root \
  --out /path/to/existing-output-directory \
  --resume
```

`build-state.json`には原本とファイル単位シャードのSHA-256が記録されます。
再開時は両方が一致する完了済みファイルをスキップし、未完了・失敗・変更済みの
ファイルだけを再処理します。

## 現在の状態

基礎抽出器は質問文、案件名、record IDによる特別分岐を持ちません。
形式別の未対応情報が残っている間はDocumentを`partial`として記録します。
EvidenceとRelationはファイル単位シャードへ逐次書き出すため、全レコードを
Pythonのメモリへ保持しません。検索用派生層は段落チャンク、ヘッダー候補付き表行、
スライド、PDFページを`SearchUnit`へ変換し、元Evidence IDを保持します。
さらに、外部APIを使わないSQLite BM25索引を構築し、日本語文字n-gramによる
検索結果から元Evidenceまで追跡できます。回答生成は検索評価後に接続します。

## データ管理

このリポジトリには次のものをコミットしません。

- 大会提供データおよび評価データ
- PDF、Office原本、ZIPアーカイブ
- 回答、提出ファイル、生成済み検索インデックス
- APIキー、認証情報、ローカル設定
