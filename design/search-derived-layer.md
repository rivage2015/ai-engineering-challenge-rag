# 検索用派生層 v0.2

## 目的

原本に忠実な`Evidence`を変更せず、全文検索・ベクトル検索へ投入しやすい
`SearchUnit`を別レイヤーとして生成する。変換は質問、案件名、ファイル名、record IDに
依存しない共通規則だけで行い、すべての検索単位から元Evidenceへ戻れるようにする。

## 変換規則

| 入力Evidence | SearchUnit | 規則 |
|---|---|---|
| DOCXのheading / paragraph | paragraph_chunk | 見出し境界、表境界、目標文字数で段落をまとめる |
| XLSX・DOCX・PPTXのtable_cell | table_row | 同じ行をまとめ、先頭の非空行をヘッダー候補として後続行へ付与する。DOCX表は直前の節見出しも付与する |
| PPTXのshape | slide_text | 同じスライドの非空テキストをまとめ、スライド種別を機械的に付与する |
| PDFのpage | page_text | 文字を抽出できたページをページ単位で保持する |

表の先頭行は「ヘッダーである」と断定せず、`first_non_empty_row_candidate`という
機械的な候補として記録する。画像だけのPDFページはOCR実装まで検索単位を作らず、
元のpage Evidenceと`content_ref`を保持する。

## 追跡可能性

`source_evidence_ids`は検索文字列の生成に使ったEvidence IDをすべて持つ。
ヘッダー候補は`context.header_evidence_ids`、親見出しは
`context.container_heading_evidence_ids`にも分けて記録する。IDは文書ID、
種別、locator、Evidence ID列、検索文字列SHA-256、builder versionから決定的に生成する。

## v0.2検証

代表4形式から51 SearchUnitを生成し、同一入力の再ビルドでJSONLと
ビルド状態がバイト一致した。大規模XLSXは8,683行のSearchUnitと索引を
件数変化なしで再構築し、整合性検証に合格した。

人手確認済み16問では、v0.1で上位10件に入らなかった2問のうち、
スライドの表番号が1位、完了済みアクションが4位に入った。Hit@5と
Recall@5はともに1.0となった。

## 現時点で行わないこと

- 質問別・案件別・ファイル名別の特別処理
- 正解データや手作業による回答辞書の作成
- LLMによる意味分類、要約、因果関係の推定
- 埋め込み生成、ランキング、回答生成

次段階では、「完了済み」のような列条件を上位化する、質問非依存の
フィールド重み付けと親子検索を比較する。その後、必要ならAPI不要の
ローカル意味検索を併用する。
