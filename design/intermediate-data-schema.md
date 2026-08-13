# 中間データ共通構造 v0.1

## 目的

共有ドライブ内の未知の案件・資料・質問が追加されても、同じ処理方針で抽出・検索・回答できる中間表現を定義する。

中間データは次の3種類に分ける。

1. `Document`: 原本ファイルと抽出処理の管理情報
2. `Evidence`: 原本から機械的に抽出した検索・回答根拠
3. `Relation`: DocumentまたはEvidence間の構造・版・意味上の関係

## 設計原則

- 特定の質問、record ID、案件名、ファイル名に依存する項目や分岐を持たない。
- 原本から得た事実と、LLM・ルールによる解釈を混ぜない。
- すべてのEvidenceから元ファイルと原本位置へ戻れるようにする。
- 原文は`content.raw_text`または`content.raw_value`へ保持し、正規化値で上書きしない。
- 大きな画像・表・バイナリは`content_ref`へ外出しし、チェックサムで同一性を保証する。
- ページ、スライド、行、列、セル、Notebookセル、コード行は1始まりとする。
- 色は可能な限り8桁の大文字ARGB（例:`FFFFA500`）で保存する。
- ファイルパスは共有ドライブルートからの相対パスとし、Unicode NFCへ正規化する。
- `confidence`は抽出または推論の確からしさであり、回答の正解確率とはみなさない。
- 手作業で作成した質問別の正解候補・参照場所・抽出条件を格納しない。

## 事実と解釈

機械的に観測できる内容はEvidenceの`content`、`style`、`geometry`へ保存する。

例:

- セルB12の値が「モデル再学習」である
- セルB12の背景色が`FFFFA500`である
- スライド3の図形が座標`x=120, y=300`にある

意味付けはEvidenceの`annotations`またはRelationへ保存し、生成方法と確信度を必須にする。

例:

- セルB12はタスク名である可能性が高い
- 旧版Documentは新版Documentの前バージョンである
- 2つの人物名Evidenceは同一人物を指す可能性がある

## 保存形式

初期実装ではJSON Linesを使用する。

```text
intermediate/
  documents.jsonl
  evidence.jsonl
  relations.jsonl
  assets/
```

各行は対応するJSON Schemaで検証する。

- `schemas/document.schema.json`
- `schemas/evidence.schema.json`
- `schemas/relation.schema.json`

## 安定ID

- `document_id`: 正規化相対パスと原本SHA-256から決定的に生成する。
- `evidence_id`: `document_id`、evidence種別、原本位置、原文チェックサムから決定的に生成する。
- `relation_id`: 始点、関係種別、終点、生成方法から決定的に生成する。

同じ原本を同じ抽出器で再処理した場合、同じIDが得られることを目標とする。

## v0.1で扱うEvidence種別

- `page`, `slide`, `worksheet`
- `header`, `footer`, `speaker_note`
- `text_block`, `paragraph`, `heading`
- `table`, `table_row`, `table_cell`, `merged_range`
- `formula`, `filter`, `pivot_table`, `data_validation`, `defined_name`
- `chart`, `chart_series`
- `image`, `shape`, `connector`
- `comment`, `style_span`, `hyperlink`, `field`, `tracked_change`
- `code_block`, `notebook_cell`
- `metadata`
- `other`

未知形式は`other`として痕跡を残し、黙って捨てない。

各形式に固有で共通項目へ正規化しきれない機械抽出事実は、
`native_properties`へ保持する。これは原本の事実専用であり、意味解釈は
`annotations`またはRelationへ分離する。

## v0.1の境界

- 人物・組織・タスクなどの正規化Entityは、代表ファイルで必要性を確認してから追加する。
- ベクトル埋め込みはEvidenceから派生する検索インデックスとし、原本事実には含めない。
- 回答候補と回答判定は中間データとは別レイヤーにする。
- 正解付き検証データおよび提出対象の質問回答は、Evidenceの原本データとして索引化しない。

## 代表ファイル検証

形式ごとの代表ファイルを、設問内容ではなく内部構造の種類と数で選定して検証した。
結果とv0.1への反映内容は`design/representative-schema-validation.md`に記録する。
