# OCR観測 v1

## 結論

分類層で`ocr_text`へ振り分けた代表10画像を、Apple Visionと
Tesseractで独立に読む。両者が同じ場所を同じ文字列として観測した行だけを
`observed`とし、片方だけの文字、異なる文字、位置が対応しない文字は
`unresolved`のまま両方を保存する。

この層は「何と書いてあるか」の観測だけを行う。表の行列、グラフの意味、
座席同士の関係、質問に対する答えは決めない。出力は`Evidence`、
`SearchUnit`、現行RAGへまだ接続しない。

## 対象と流れ

対象は、検証済み視覚分類16件のうち`routes`に`ocr_text`を持つ10件である。
順序と件数は分類成果物に固定し、質問文、正解、回答履歴をOCRエンジンへ
渡さない。OCRエンジンが受け取るのは検証済み画像バイトだけである。

```text
materialized image + classification route
  -> 画像SHA・MIME・寸法・向きを検証
  -> Apple Vision OCR -----------------+
  -> Tesseract OCR（独立実行）----------+-> 位置対応 + NFC完全一致
                                           -> observed / unresolved
                                           -> OCRObservation JSONL
                                           -> まだ検索へ接続しない
```

PDF由来の6ページはPopplerで200 DPIのPNGへレンダリングした画像を読む。
6ページとも原PDFのテキスト層は空であり、ネイティブ文字抽出では代替できない。

## OCRエンジン

### Apple Vision

- Vision request revision 3
- `accurate`
- 認識言語は`ja-JP`、`en-US`
- `usesLanguageCorrection=true`
- `automaticallyDetectsLanguage=false`
- EXIF orientationをVisionへ渡す

`raw_text`にはVisionが返した第1候補をそのまま保存する。空文字判定のために
trimは使うが、保存文字列にはtrim、NFKC、句読点除去、誤字修正を行わない。

### Tesseract

- Tesseract 5.5.2
- 言語は`jpn+eng`
- OEM 1、PSM 3
- `preserve_interword_spaces=1`
- 生文字列はTXT、位置はTSVの行グループから取得
- 行confidenceは単語confidenceの最小値

TXTとTSVの行数が一致しない場合は、対応できた範囲だけを保存し、run全体を
`needs_review`にする。confidenceが高くても確定へ昇格させない。

## 観測契約

各画像につき`ocr_observation`を1件作る。

- 画像左上を`[0, 0]`、右下を`[1000, 1000]`とする座標を使う。
- 各engine runは、生の行文字列、bbox、confidence、警告、エラーを持つ。
- 比較時に許可する文字正規化はUnicode NFCだけである。
- bboxの重なりが小さい辺の面積に対して0.5以上の行だけを対応候補にする。
- 異なる2エンジンの対応行がNFC後に完全一致した場合だけ`observed`にする。
- 単独観測、文字差、位置差、engine警告、engine失敗は`unresolved`にする。
- 一致しない文字を多数決、confidence平均、LLM仲裁で1本へ統合しない。
- 全行が`observed`の画像だけ、record全体を`observed`にする。

これは厳しい「確定条件」であり、OCR精度の測定値ではない。例えば同じ文章でも、
一方が1行、もう一方が2行へ分割すれば`unresolved`になる。

## 再現性と安全性

画像SHA-256だけでなく、実行環境もrun署名へ含める。

- Vision: macOS version/build、CPU architecture、compile target、Swift compiler version、
  Swift wrapperの相対pathとSHA-256、path非依存のbuild signature
- Tesseract: 解決済み実行ファイルpathとSHA-256、tessdata path、
  `jpn`・`eng` traineddataの解決済みpathとSHA-256

validatorは現在の実ファイルを再hashし、成果物のruntime fingerprintと完全一致するか
確認する。OCRコード、OS build、Tesseract本体、辞書のどれかが変われば、旧cacheを
現行結果として再利用しない。

さらに、入力順、分類参照、画像バイト、MIME、寸法、50メガピクセル上限、
symlinkとpath逸脱、全hash・署名・ID、cache時刻を決定的に検証する。
JSON SchemaはDraft 2020-12とFormatCheckerを使う。
`jsonschema`が利用できない場合は手動検証だけで続行せず、明示的に停止する。

## 全154画像の結果

実行結果は次のとおり。

| 項目 | 結果 |
| --- | ---: |
| OCR対象 | 154画像 |
| engine run | 308件 |
| engine失敗 | 0件 |
| consensus行 | 5,500行 |
| 2エンジン完全一致 | 1,113行 |
| unresolved | 4,387行 |
| record全体が`observed` | 16画像 |
| record全体が`needs_review` | 138画像 |

長文ページ、グラフ、表、座席図ではVisionの方が多くの文字を拾うことがあるが、
誤字、数字への偽prefix、行分割差も確認した。Tesseractにも欠落と誤字がある。
そのため、138画像を自動的に正しい本文へ一本化せず`needs_review`に止めた。
これは未処理ではなく、二つの独立読みによる安全な観測完了状態である。

全件OCR成果物のSHA-256は
`3e7acbb6f7fab86e3a3f3cffe246d531bb45caa751321bdc65c5e7dfd22ca33b`、
255素材の被覆証明は`reading-coverage.json`に保存し、未被覆0件を確認した。

## 再現コマンド

macOSのApple Vision、Tesseract 5.5.2、`jpn`・`eng` traineddata、Pillow、
jsonschemaが必要である。

```bash
rag/.venv/bin/python scripts/extract_ocr_observations.py \
  --assets artifacts/visual-classification-v1/materialized-full-batch.jsonl \
  --classifications artifacts/visual-classification-v1/classifications-full.jsonl \
  --output artifacts/ocr-observation-v1/ocr-observations-full.jsonl \
  --asset-root artifacts/visual-classification-v1 \
  --cache-dir artifacts/ocr-observation-v1/cache

rag/.venv/bin/python scripts/validate_ocr_observations.py \
  artifacts/ocr-observation-v1/ocr-observations-full.jsonl \
  --assets artifacts/visual-classification-v1/materialized-full-batch.jsonl \
  --classifications artifacts/visual-classification-v1/classifications-full.jsonl \
  --asset-root artifacts/visual-classification-v1

rag/.venv/bin/python scripts/validate_reading_coverage.py \
  --manifest artifacts/visual-classification-v1/visual-assets.jsonl \
  --materializable artifacts/visual-classification-v1/materializable-batch.jsonl \
  --materialized artifacts/visual-classification-v1/materialized-full-batch.jsonl \
  --classifications artifacts/visual-classification-v1/classifications-full.jsonl \
  --observations artifacts/ocr-observation-v1/ocr-observations-full.jsonl \
  --inventory artifacts/layer1-v1/deliverables/text_inventory.csv \
  --native-raw artifacts/layer1-v1/deliverables/native_text_raw.jsonl \
  --source-root share/共有ドライブ \
  --asset-root artifacts/visual-classification-v1

rag/.venv/bin/python -m unittest discover -s tests -v
```

## 残る未対応

- `unresolved`領域の拡大切り出し、解像度変更、縦書き専用再試行は未実装である。
- PDFページ末尾で文章が次ページへ続く場合も、現在はページごとの可視範囲だけを
  観測する。現在のwarningは`unresolved`件数までで、ページ端継続を専用warningには
  していない。ページをまたぐ本文連結は後段の構造化で扱う。
- 表のセル対応、グラフの軸・系列、座席図の人物とPODの関係は、
  それぞれ`table_structure`、`chart_source_recovery`、`diagram_relations`で扱う。
- OCR候補を意味解釈して本文へ統合する処理は未実装である。
- `Evidence`、`SearchUnit`、検索、回答生成は意図的に未接続である。
- Visionの実行バイナリは実行前にbuild metadataのSHA-256と照合する。
  path非依存の署名には、wrapper、Swift compiler、targetから作るbuild signatureを使う。
  実行バイナリ自体のpath依存SHA-256はOCR recordへ保存しないため、同じ信頼領域で
  binaryとbuild metadataの両方を整合するよう改ざんする脅威は、
  record単独の検証対象外である。
