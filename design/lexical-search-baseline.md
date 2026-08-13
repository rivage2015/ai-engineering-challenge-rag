# API不要の語彙検索基準線 v0.1

## 目的

`SearchUnit`に対し、外部API・LLM・追加の形態素解析辞書を使わずに再現可能な
検索基準線を作る。回答生成とは分離し、検索結果には元Evidence IDとlocatorを残す。

## 方式

- 索引保存: Python標準ライブラリのSQLite
- ランキング: BM25（`k1=1.2`, `b=0.75`）
- 日本語: Unicode正規化後、連続する日本語文字の2-gramと3-gram
- 英数字: 小文字化した語単位トークン
- 同点処理: SearchUnitの入力順に対応するSQLite rowid順

索引は`SearchUnit`本文だけを対象とする。質問、案件名、ファイル名、record ID、正解例を
ランキング規則へ組み込まない。索引状態には入力SearchUnitと状態ファイルのSHA-256を
記録し、別バリデータでSQLite内部と入力の同一性を確認する。

## 出力

検索結果はスコアに加え、`search_unit_id`、`document_id`、`unit_type`、`locator`、
`source_evidence_ids`、本文スニペットをJSONで返す。この段階では回答文を生成しない。

## 次の評価

検索対象全体で一般的な質問セットを用意し、Recall@kとMRRを測る。結果を確認してから、
表の構造スコア、ローカル埋め込み、再ランキングの追加効果を比較する。
