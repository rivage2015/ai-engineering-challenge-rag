# 検索用派生層 v0.1

## 目的

原本に忠実な`Evidence`を変更せず、全文検索・ベクトル検索へ投入しやすい
`SearchUnit`を別レイヤーとして生成する。変換は質問、案件名、ファイル名、record IDに
依存しない共通規則だけで行い、すべての検索単位から元Evidenceへ戻れるようにする。

## 変換規則

| 入力Evidence | SearchUnit | 規則 |
|---|---|---|
| DOCXのheading / paragraph | paragraph_chunk | 見出し境界と目標文字数で段落をまとめる |
| XLSX・DOCX・PPTXのtable_cell | table_row | 同じ行をまとめ、先頭の非空行をヘッダー候補として後続行へ付与する |
| PPTXのshape | slide_text | 同じスライドの非空テキストをまとめる |
| PDFのpage | page_text | 文字を抽出できたページをページ単位で保持する |

表の先頭行は「ヘッダーである」と断定せず、`first_non_empty_row_candidate`という
機械的な候補として記録する。画像だけのPDFページはOCR実装まで検索単位を作らず、
元のpage Evidenceと`content_ref`を保持する。

## 追跡可能性

`source_evidence_ids`は検索文字列の生成に使ったEvidence IDをすべて持つ。
ヘッダー候補は`context.header_evidence_ids`にも分けて記録する。IDは文書ID、種別、
locator、Evidence ID列、検索文字列SHA-256、builder versionから決定的に生成する。

## 現時点で行わないこと

- 質問別・案件別・ファイル名別の特別処理
- 正解データや手作業による回答辞書の作成
- LLMによる意味分類、要約、因果関係の推定
- 埋め込み生成、ランキング、回答生成

次段階ではこの派生層を検証後、BM25などの語彙検索を基準線として追加し、必要なら
API不要のローカル埋め込みを併用する。
