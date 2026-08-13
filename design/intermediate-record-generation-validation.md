# 中間レコード機械生成・再現性検証

## 目的

共通Schema v0.1が設計上だけでなく、実ファイルから機械生成したレコードでも
成立するかを確認した。これは抽出器の診断であり、回答用インデックスの生成ではない。

## 実装

- `scripts/probe_intermediate_records.py`
  - 任意の`--root`と複数の`--input`を受け取る。
  - ファイル名、案件名、質問文、record IDによる分岐を持たない。
  - 拡張子ごとにDOCX、XLSX、PPTX、PDFを同一方針で処理する。
  - `Document`、`Evidence`、包含を表す`Relation`をJSON Linesへ出力する。
  - 診断用の葉要素上限を設け、Documentを必ず`partial`として記録する。
- `scripts/validate_intermediate_records.py`
  - 外部パッケージを使わずに、必須項目、ID形式、一意性、内容ハッシュ、
    親子参照、Document参照、Relation端点を検査する。
  - Document、Evidence、RelationのIDを構成要素から再計算し、保存IDとの一致を検査する。
  - `--root`指定時は原本のサイズとSHA-256も再計算する。

## 対象と条件

構造的特徴で選んだ代表5ファイルを使用した。

- DOCX 1件
- XLSX 1件
- PPTX 1件
- テキスト層を持つPDF 1件
- テキスト層を持たないPDF 1件

葉要素の上限は文書ごとに40件とした。ページ、スライド、ワークシート、表、
ピボット、グラフなど、親子構造の検証に必要なコンテナ要素は上限外とした。

## 結果

| 検査 | 結果 |
|---|---|
| Document生成数 | 5 |
| Evidence生成数 | 183 |
| Relation生成数 | 183 |
| 必須項目・ID形式・ID一意性 | 合格 |
| 内容SHA-256再計算 | 合格 |
| 原本サイズ・SHA-256再計算 | 合格 |
| 安定ID再計算 | 合格 |
| EvidenceからDocumentへの参照 | 参照切れ0件 |
| Evidence親子参照 | 参照切れ0件、文書跨ぎ0件 |
| Relation始点・終点 | 参照切れ0件 |
| 固定時刻での2回生成 | 3つのJSONLすべてバイト単位で一致 |

2回の生成物で得たSHA-256は次のとおり。

```text
documents.jsonl  597eac4eaa11fe9464c146e0acefc7bcf78d98ec3127a11f963d1bb5fbf021d5
evidence.jsonl   7cc5db07e88921863540833e0762871c3525c95cbaa21e56f411c3eccbfe6d26
relations.jsonl  f5f1d4491a7f60058c002499e9e8553cf1620277fda349e4a9f0de785872b00f
```

生成されたEvidence種別は、`paragraph`、`heading`、`table`、`table_cell`、
`worksheet`、`pivot_table`、`chart`、`slide`、`shape`、`page`である。

画像PDFは空のまま捨てず、4ページを`page` Evidenceとして保持した。
各ページには原本参照と`text_layer_present=false`を保存し、Documentには
`OCR deferred for 4 page(s) without a text layer`を警告として記録した。

## 出力の隔離

検証出力は`/private/tmp/aiec-probe-final1`と
`/private/tmp/aiec-probe-final2`に作成した。既存の`rag/chunks.jsonl`、
回答処理、提出物には接続していない。原本ファイルも変更していない。

## 現時点の境界

- これは少量サンプル用プローブであり、全件抽出器ではない。
- OCRは未実装で、必要性と欠落を明示するところまでである。
- DOCXのコメント、run単位書式、ヘッダー・フッター、PPTXのノート、
  XLSXの拡張グラフ参照先などは完全抽出していない。
- 実行環境にDraft 2020-12対応のJSON Schema検証パッケージがないため、
  今回はプロジェクト固有の不変条件検証器を使用した。Schema全項目に対する
  標準検証器の適用は、依存関係を確定する段階で追加する。
- 抽出時刻は事実として保存する。同一入力のバイト比較では、同じ`--run-at`を
  明示して時刻差だけを除外する。

## 判断

Document・Evidence・Relationの3構造、原本位置、安定ID、親子Relationという
中間データの骨格は実ファイルで成立した。この後の上限なし基礎抽出器と検証結果は
`design/baseline-intermediate-extractor.md`に記録する。
