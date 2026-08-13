# AI Engineering Challenge - Intermediate RAG Pipeline

SIGNATE「AI ENGINEERING CHALLENGE」向けに検討している、質問非依存の
中間データ構造と抽出処理の実装リポジトリです。

大会から提供されたデータ、評価データ、提出物、生成済みインデックスは含みません。
原本ファイルを`Document`、抽出根拠を`Evidence`、構造的な関係を`Relation`として
保持し、後段の検索用派生データと回答生成から分離します。

## 構成

```text
design/   設計方針と実ファイルでの検証記録
schemas/  Document・Evidence・RelationのJSON Schema
scripts/  抽出、診断、整合性検証CLI
```

## 主なスクリプト

- `scripts/build_intermediate_records.py`
  - DOCX、XLSX、PPTX、PDFを再帰的に発見し、中間レコードを生成します。
- `scripts/probe_intermediate_records.py`
  - 少量の代表データでSchemaと抽出結果を確認する診断用プローブです。
- `scripts/validate_intermediate_records.py`
  - ID、ハッシュ、親子関係、原本参照、Relation端点を検証します。

## 実行例

依存ライブラリとして`python-docx`、`openpyxl`、`python-pptx`、`pypdf`を使用します。

```bash
python scripts/build_intermediate_records.py \
  --root /path/to/source-root \
  --out /path/to/new-output-directory

python scripts/validate_intermediate_records.py \
  /path/to/new-output-directory \
  --root /path/to/source-root
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
Pythonのメモリへ保持しません。検索用の表領域・行・意味単位への集約は、
原本Evidenceとは別の派生層として追加する予定です。

## データ管理

このリポジトリには次のものをコミットしません。

- 大会提供データおよび評価データ
- PDF、Office原本、ZIPアーカイブ
- 回答、提出ファイル、生成済み検索インデックス
- APIキー、認証情報、ローカル設定
