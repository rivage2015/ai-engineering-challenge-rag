# 適応型Document Reader v4

最終更新: 2026-09-01

## 結論

このReaderの目的は、抽出規則へ入力を合わせることではない。原本をできるだけ正確に読み、
位置と不確実性を保ったまま、後段が質問・検索・回答に使えるEvidenceへ渡すことである。

入力を一律の文字列配列へ変換しない。形式、原本構造、失敗原因、未読領域に応じてReaderを
切り替える。Validatorは出力を捨てるだけの門ではなく、次に試すReaderを決めるための観測器とする。

## 成功条件

処理成功は「validatorがPASSした」だけではない。次の全条件で判定する。

1. 原本hashと位置へ戻れる。
2. 読むべき領域が列挙され、各領域が終端状態を持つ。
3. 読めた内容はEvidenceになり、関係を失わないSearchUnitへ到達する。
4. SearchUnitが安全判定、索引、質問経路へ到達する。
5. 読めなかった内容は消えず、原因と次の再読戦略を持つ。

文書の処理coverageは次で測る。

```text
terminal_coverage = terminal_regions / expected_regions
trusted_coverage  = (exact_regions + high_regions) / expected_regions
question_coverage = indexed_searchable_regions / expected_searchable_regions
```

`terminal_coverage = 1.0`は未解決領域を含み得る。したがって`trusted_coverage`と
`question_coverage`を別に報告し、処理完了と読取品質を混同しない。

## 変更する規則

### 削除する

- 全入力を座標のない全文文字列配列へ統合する。
- 入力形式にかかわらず同じReaderを使う。
- 行数固定、挿入削除禁止、exact文字列anchorで対応付ける。
- OCR完全一致の行だけを検索可能にする。
- PDFをページ単位でnativeかOCRのどちらか一方に固定する。
- 画像を恒久的にファイル名・metadataだけで終わらせる。
- geometryが曖昧なら観測全体を捨てる。
- 同じ条件の読取を固定回数だけ繰り返す。

### 維持する

- 原本を変更しない。処理前後のSHA-256、size、mtimeを検査する。
- 原本内容、質問、索引、回答を外部へ送らない。
- 原観測を上書きせず、採用結果と分離して残す。
- 資料内の命令文を実行しない。
- extractor、model、prompt、前処理、位置、hashの来歴を残す。
- 正解、既存回答、質問別anchorを抽出器へ渡さない。
- 不明な内容を推測で確定しない。

### 意味を変える

| 旧規則 | v4での意味 |
|---|---|
| fail closed | 出力を消さず、`exact/high`への昇格を止めて再読・`provisional`・`unresolved`へ送る |
| native first | 文書全体の排他選択ではなく、領域ごとの第一候補とする |
| question independent | 質問から答えを作らない。質問後の再読は対象領域の優先順位付けにだけ使える |
| no normalization | raw観測は不変。検索・比較用の派生正規化は別フィールドで許可する |
| confidence | モデル自己申告ではなく、native構造、独立観測、競合、coverageから品質区分を決める |

## Readerの分岐

| 入力・状態 | 第一Reader | 不足時のReader | 採用単位 |
|---|---|---|---|
| XLSX | OOXML/native cell reader | 表示値renderer、埋込画像Reader | workbook / sheet / row / cell / formula |
| DOCX | OOXML paragraph/table/relationship reader | 埋込画像・描画Reader | section / paragraph / table / cell / image |
| PPTX | OOXML shape/table/connector reader | slide renderの領域Reader | slide / shape / table / connector |
| text PDF | bbox付きnative text | 文字がない、壊れた、疎な領域だけOCR | page / block / line / word |
| scanned table | table detectorとcell segmentation | 傾き、拡大、bordered/borderless、OCR/VLM切替 | table / row / cell |
| general image | text detectorとbbox付きOCR | 回転、拡大、コントラスト、行crop、別engine | block / line / word |
| chart | 元データ、chart XML、系列参照 | 軸・凡例・label・plotを分けた視覚Reader | chart / axis / series / point |
| diagram | native shapeとconnector | label OCRと空間edge推定 | shape / label / edge |
| low quality | failure classifier | 未使用の前処理・segment・engine・context | unresolved span |

Readerの選択は拡張子だけで終わらない。magic、OOXML part、native text量、画像領域、
文字化け、構造coverageを観測して、同じ文書・ページ内でも領域ごとに切り替える。

## 共通データ契約

```text
regions.jsonl       原本由来の安定region_id、親子関係、logical locator、bbox
observations.jsonl  Readerごとの生観測、実行条件、model/prompt/preprocess hash
evidence.jsonl      採用内容、品質区分、競合span、採用Observation、searchable
relations.jsonl     contains、reading_order、header_of、formula_of、connector等
coverage.json       expected、accepted、provisional、unresolved、欠落region_id
search_units.jsonl  質問に必要な関係を保った行・段落・ページ・画像packet
```

`region_id`は認識文字列から作らない。原本SHAと、sheet/cell、page+bbox、slide/shape、
構造pathなどから決定する。再読で文字が変わっても同じ領域へ別Observationを追加できる。

Evidenceの最低項目は次とする。

```text
document_id, region_id, evidence_id
source_sha256, source_locator, geometry
raw_text または raw_value
quality_tier, searchable, uncertainty_spans
accepted_observation_ids, competing_observation_ids
extractor/model/version/prompt_hash/preprocess
```

## 品質区分

| tier | 条件 | 質問経路 |
|---|---|---|
| exact | 検証済みnative値・構造 | 通常回答に利用 |
| high | 独立した複数観測が同じregionで一致し、競合spanがない | 通常回答に利用 |
| provisional | 読める候補はあるが単独観測または局所競合がある | 検索可能。回答時に留保 |
| blank | native空値、または複数観測が無文字で一致 | 空白として構造を維持 |
| unresolved | 戦略を変えても候補がない | coverage gapとして保持 |

一部の文字だけが競合する場合、行全体を捨てない。合意spanと競合spanを分け、競合候補を残す。

## 再読ループ

```text
DISCOVERED
  -> NATIVE_PROBED
  -> REGIONIZED
  -> OBSERVED
  -> RECONCILED
       -> exact / high / provisional / blank
       -> RETRY_PENDING
            -> preprocessingを変更
            -> segmentationを変更
            -> engineを変更
            -> context範囲を変更
            -> OBSERVED
            -> unresolved
  -> COVERAGE_AUDITED
  -> SEARCH_UNIT
  -> SECURITY_GATE
  -> INDEXED
  -> QUESTION
```

同一条件の再実行は禁止する。再読では少なくとも
`preprocess / segment / engine / context`の1要素を変える。標準予算は初回と最大4戦略だが、
予算は正しさの判定基準ではない。改善が止まった領域だけを高コストReaderまたは人確認へ送る。

## 現在のXLSXへ適用する最小経路

```text
XLSX原本
  -> native sheet/cell/formula/merge抽出
  -> cell位置付きEvidence
  -> header文脈を含むrow SearchUnit
  -> Layer 1 adapter
  -> content security gate
  -> local embedding index
  -> Question Evidence Graph
     -> aggregate_count: 対象、SUM範囲、全行coverage、再集計値、保存値
     -> record_lookup: 一意の対象行、状態、項目別row/header/value経路
  -> 必須RelationとEvidenceを機械検証
  -> Graph選択Evidenceを実行者へ先頭挿入
  -> Claim Graphと別コンテキスト最終監査
  -> 回答、Evidence ID、sheet/row locator
```

画像OCRはこの経路の主Readerではない。native cellに値がある領域は`exact`とし、
埋込画像、描画オブジェクト、native構造とrender表示が一致しない領域だけを画像Readerへ送る。

## 実装の現在地

- Macアプリのbootstrapは、旧来の単純抽出器ではなく、棚卸し→Layer 1抽出→検証→SearchUnit→adapter→安全判定→索引の経路を使う。
- XLSXは`openpyxl`がある場合のnative Readerに加え、配布Macの標準ライブラリだけで動くOOXML Readerを持つ。sheet、cell座標、raw数値表記、数式、結合範囲、defined nameを抽出する。数式は式と「ファイル保存時の値・未再計算」を別Evidenceで保持し、どちらもcellとrowの質問経路へ渡す。
- 画像はApple Visionの`primary / literal / fast_sparse`と、利用可能な場合のTesseract `PSM 3 / PSM 6`を失敗原因で切り替える。Pillowのない配布PythonでもPNG/JPEG/TIFF/BMPの安全検査とサイズ取得を行える。
- 独立したengine群の文字・位置一致だけを`high`とし、同一engine内の一致と単独観測は`provisional`として残す。両者は別の`image_text_packet`へ分離し、tierと出典のschema・validatorを持つ。
- 座標付きReaderが1行も得られない画像は、導入済みの`gemma4:12b`に質問非依存の全画像転記を依頼する。座標を捏造せず`unlocated / provisional`として検索可能にし、model digest、prompt hash、実行条件を残す。モデル未導入時にこのReaderが自動downloadを行うことはない。
- `provisional`は検索可能だが、`[暫定読取]`だけが値を支持する主張は、最終LLM監査の前に決定論的なclaim validatorが拒否する。
- 回数・合計質問は`question_evidence_graph.py`で回答前Graphを作る。読取済みEvidence本文をNode、対象・数式・範囲・再集計・回答の関係を根拠付きEdgeにし、artifact hashとEvidence本文hashを独立再検証する。
- 構造化レコード参照も`question_evidence_graph.py`を必須経路とする。現在は`owner / review_date / unit_cost / seats / budget`に限定し、質問に明記された項目と一意の構造化行を対応させ、必要なら`Approved / Final / Finalized`状態を確認する。各項目はverified explicit `derived_from`でrowからheaderとvalue Evidenceへ分岐し、検索・項目監査・最終監査が同じ枝を消費したことを機械検査する。
- 回数質問でGraphを作れないときは未対応のまま通常検索へ逃がさず`hold`にする。合計候補の曖昧性、行欠落、値競合、暫定Evidence、本文hash不一致も同様に停止する。
- 構造化レコード参照で質問要求の計画漏れ、未対応項目、状態の否定・非最終版指定、複数候補、または必須lineage欠落を検出した場合も`hold`にし、通常検索で部分回答しない。
- 長文、長い表行、画像packetを形式ごとの例外にせず、adapterの質問境界で最大1,600文字のexact shardへ置き換える。元投影hash、`[start,end)`、chunk hashで全文復元を独立Validatorが検査し、埋め込み入力の打切りが1件でもあれば索引を公開しない。回答監査にEvidence IDを渡すときは、packet全文を入れるか全体を外し、途中切断を禁止する。
- 一部ファイルの抽出失敗で全体を捨てない。失敗ファイルの中間Evidenceはファイル単位でrollbackし、他の読取可能なファイルを後段へ渡す。
- 初回buildでGemmaが読取後に導入可能になった場合は、別の空semantic/security世代へ再読・再検証してから公開する。中断世代はbuild IDとPIDで復旧し、公開済みを削除しない。

2026-09-01の現行2シートXLSXでは、標準ライブラリ経路でcell `90/90`が行SearchUnitへ到達した。数式6件は式と保存値の対応を保持し、row SearchUnit `43`、semantic Evidence `146`、埋め込み打切り`0`、SQLite integrity `ok`。各シートから2件ずつ自動選択したretrievalはHit@1 `4/4`、Hit@5 `4/4`、数式保存値の追加1件はHit@1 `1/1`だった。

非公開の検証用XLSXに対する回数質問では、Question Evidence Graphが`対象 -> SUM範囲 -> 全行coverage -> 再集計値 -> 保存値`の経路を作り、両値の一致を確認して回答を生成した。決定論的Claim Graph検証は`pass`、全集計Evidenceを見た別コンテキストの`gemma4:12b`最終監査は`verified`。これは同じモデルの手続き的分離であり、独立モデルによる監査ではない。また、1つの検証用XLSXに対するend-to-end成功であり、他形式・他数式への正答率を示すものではない。非公開資料のファイル名・質問文・実値はGitHubに収録しない。

2026-09-03の非公開検証用XLSXに対する構造化レコード参照では、5項目それぞれに`row -> header -> value`のGraph枝を作り、回答実行者が値セルEvidenceを支持根拠として消費した。Question Evidence Graph、Graph retrieval trace、決定論的Claim Graph、別コンテキストの`gemma4:12b`監査はすべて`pass / verified`、Orchestratorは回答を`accepted`とした。これも1つの検証用XLSXと対応済み5項目に限るend-to-end成功であり、汎用GraphRAGや他形式への正答率を示すものではない。

未完了事項:

- `region/observation/quality_tier/coverage`の正式schemaを追加する。
- XLSXのheaderを「最初の非空行」だけで決めず、複数候補と結合・書式・型から判定する。
- DOCX/PPTX/PDFのnative Reader依存を未導入Macへ同梱するか、形式ごとの標準ライブラリReaderを追加する。
- Office埋込画像、PDFの領域混合、chart/diagram Readerを質問経路へ接続する。
- 一部の座標付きOCR行は得られたがcoverageが低い画像に対し、局所crop、前処理、全画像VLMをどの条件で追加するかを実装する。現行の全画像Gemma fallbackは座標付き読取が0行のときだけ起動する。
- 未使用・held-out画像群で、読取coverage、CER、位置対応、確定回答の誤昇格率を評価する。
- 上記1問に加え、値競合、欠落範囲、複数集計候補、`SUM`以外の数式、OCRのみの表を含むend-to-end質問評価を拡張する。

依存がない場合に簡易文字列へ黙って戻し、完全読取と表示してはならない。利用可能なReaderで
読めた領域は後段へ渡し、未読形式・領域は`complete_with_limits`とcoverageへ残す。

## 受入条件

- 原本SHA、size、mtimeが処理前後で不変。
- 全DocumentとEvidenceが原本位置へ戻れる。
- XLSXは宣言sheet数と処理sheet数が一致する。
- 明示非空cellのseen件数とemitted件数が一致する。
- 検索対象cellが最低1つのrow SearchUnitへ到達する。
- adapter、security gate、indexの件数とhashが連続する。
- SQLite integrity checkが`ok`。
- 質問結果がEvidence IDとlocatorを返す。
- 本文、画像、OCR結果をstdout・診断metadataへ出さない。
- 失敗・部分成功・未対応を`complete`へ格上げしない。
