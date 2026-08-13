# 代表ファイルによるSchema検証

## 目的と選定ルール

Document・Evidence・Relationの共通構造が実データを表現できるか確認した。

代表ファイルは質問文、正解、record IDを参照せず、ファイル内部に含まれる
表、書式、シート、ピボット、グラフ、図形、ページなどの構造的特徴だけで選んだ。
この記録は設計検証専用であり、回答生成時の参照先一覧や質問別ルールとして使用しない。

## 選定した代表

| 形式 | 選定理由 | 観測した主な構造 |
|---|---|---|
| DOCX | 表と文字書式が多い | 7ページ、358段落、8表、64行、219セル、太字run 101件、使用段落スタイル7種類 |
| XLSX | 異なるシート構造とピボット・拡張グラフを含む | 4シート、2ピボット、拡張グラフ部品3件、描画部品1件、大規模データシート |
| PPTX | 表とネイティブグラフを含む | 18スライド、図形265件、5表、99セル、グラフ1件、系列2件 |
| テキストPDF | 全ページにテキスト層を持つ | 18ページ、全ページ抽出可能、ページごとのレイアウト・表・グラフ |
| 画像PDF | テキスト層を持たない | 4ページ、原本表示は可能、OCR前は抽出文字0件 |

DOCXには単一ファイルで全書式種別が揃わないため、全体走査でコメント、ハイライト、
下線、埋め込み画像を持つ別ファイルが存在することも確認した。特定質問への対応ではなく、
形式機能の存在確認として扱う。

## 共通構造への対応

### DOCX

- ファイル全体: Document
- 段落・見出し: Evidence `paragraph` / `heading`
- 表・行・セル: Evidence `table` / `table_row` / `table_cell`
- 太字・下線・ハイライト: Evidenceの`style`または`style_span`
- コメント: Evidence `comment`と、対象EvidenceへのRelation
- ヘッダー・フッター: Evidence `header` / `footer`
- 原本位置: paragraph、table、row、columnの各index

OOXMLだけでは段落の表示ページが確定しない場合がある。レンダリング後にページ位置を
推定した場合は、抽出方法とconfidenceを明記し、段落indexを一次位置として保持する。

### XLSX

- ブック全体: Document
- シート: Evidence `worksheet`
- セル: Evidence `table_cell`
- 数式: セルの子Evidence `formula`
- ピボット: Evidence `pivot_table`
- フィルター: Evidence `filter`
- グラフと系列: Evidence `chart` / `chart_series`
- セル範囲参照: Relation `lineage`
- シート・行・セルの包含: Relation `structural`

拡張グラフ`chartEx`は一般的なExcelライブラリからグラフとして取得できない場合がある。
OOXML部品、リレーション、参照元を`source_member`と`native_properties`へ保持する必要がある。

### PPTX

- プレゼンテーション全体: Document
- スライド: Evidence `slide`
- 図形: Evidence `shape`
- 表とセル: Evidence `table` / `table_cell`
- グラフと系列: Evidence `chart` / `chart_series`
- ノート: Evidence `speaker_note`
- 図形位置: `geometry`のEMU座標
- スライドと要素の包含: Relation `structural`

文字列だけでは、色、位置、グルーピング、チャート系列、表構造が失われる。
検索用テキストとは別にEvidenceとして保持する。

### PDF

- PDF全体: Document
- 各ページ: Evidence `page`
- テキストブロック: ページの子Evidence `text_block`
- 埋め込み・レンダリング画像: Evidence `image`
- OCR結果: Evidence `text_block`、抽出方法をOCRとして記録
- ページ内位置: `geometry`のptまたはpx座標

画像PDFは文字が抽出できない場合でも、ページEvidenceと画像`content_ref`を残す。
OCR未実施をDocumentの`partial`または`deferred`として記録し、ファイルを索引から消さない。

## 検証で追加した項目

- `page`, `slide`, `worksheet`
- `header`, `footer`, `speaker_note`
- `merged_range`, `filter`, `pivot_table`, `data_validation`, `defined_name`
- `connector`, `hyperlink`, `field`, `tracked_change`
- object・series位置
- MIME type
- style ID、テーマ色、配置情報
- 原形式固有の事実を保存する`native_properties`
- グラフ参照元などを表すRelation `lineage`
- 版間比較を表すRelation `comparison`

## 結論

3種類の基本構造は維持できる。形式別に別々のデータモデルを作る必要はない。

ただし、検索可能な共通項目と、原形式固有の機械抽出事実を両方保持する必要がある。
共通化できない情報を削除せず`native_properties`と原本参照へ残し、後段で追加解析できる
構造とする。

少量の実レコード生成と、IDの安定性、親子関係、原本位置、再実行一致の検証は
`design/intermediate-record-generation-validation.md`に記録した。
