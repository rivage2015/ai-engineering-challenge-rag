# 視覚素材分類 v1

## 結論

OCR候補とグラフ候補を、最初から別々のファイルだと決めつけない。
ページまたは画像ごとに実体化し、内容を複数ラベルで分類してから、
転記、表構造化、グラフ復元、図の関係抽出、画像説明へ分岐する。

v1では分類の品質を確かめるところまでとし、分類器の出力はRAGの
`Evidence`や`SearchUnit`へ接続しない。

## 考え方

ファイル形式と内容の種類を分けて扱う。例えばPDFは入れ物であり、
その1ページが文章、表、グラフ、図の複合であることは普通にある。
そのため、1つだけ選ぶ単一ラベルではなく、複数ラベルにする。

分類ラベルは次のとおり。

- `text_document`: 文章、箇条書き、スライド文面
- `table`: 行列で意味が決まる表
- `chart`: 軸、凡例、系列、データ点を持つグラフ
- `diagram`: 箱、矢印、接続、配置に意味がある図
- `screenshot`: アプリや画面の操作状態
- `formula`: 数式が主要な情報
- `photo`: 実写画像
- `illustration`: 非実写の説明画やイコン
- `decoration`: 内容を増やさない装飾
- `unknown`: 安全に判断できないもの

## 処理の分岐

```text
原本ファイル
  -> ページ・画像ごとに実体化
  -> 複数ラベル分類
      -> text_document -> ocr_text
      -> table         -> table_structure
      -> chart         -> chart_source_recovery
      -> diagram       -> diagram_relations
      -> formula       -> formula_ocr
      -> photo/図     -> image_description
      -> decoration    -> skip
      -> unknown       -> review
```

グラフは画像から数値を読む前に、生成コード、CSV、XLSX、
ネイティブチャートを探す。元データから再計算できた値だけを
`exact`とし、モデルの目視推定は`estimated`、判断できない値は
`unresolved`とする。

## 安全規則

- 大会の質問文、回答、正解を分類器へ渡さない。
- ファイル名だけで内容ラベルを決定しない。
- 文字、罫線、軸、凡例が見える画像を装飾として捨てない。
- モデルの自己申告だけで正確性を上げない。
- 原本SHA-256、ページや埋め込みメンバー、レンダリング設定、
  モデルdigest、プロンプトversionを保存する。
- 同一署名の完了済み結果は再利用し、中断後に再開できるようにする。
- キャッシュ利用の有無、初回推論時刻、成果物を書き直した時刻を分けて保存する。
- 素材ごとの失敗は隠さず、`review`または明示的なエラーとして残す。
- Office ZIPは、entry数、1メンバー容量、合計展開量、圧縮率に上限を設け、
  全体をメモリへ展開せずストリームでハッシュする。

## v1の合格条件

1. 候補がファイル単位ではなく、PDFページ、Office埋め込み画像、
   単体画像の素材単位で一意になる。
2. 同じ入力から同じ代表バッチが選ばれる。
3. 画像と出力を並べ、16件の代表バッチを人が確認できる。
4. 分類結果はJSON Schemaと決定的validatorの両方に合格する。
5. 未検証の分類や抽出結果は現行の検索経路に混ぜない。

## 実装

次の3層に分けた。

1. `build_visual_asset_manifest.py`
   - Layer 1の原本台帳からPDFページ、Office・Notebook埋め込み画像、
     単体画像、将来レンダリングが必要な容器を発見する。
   - 原本と埋め込みバイトのSHA-256を固定し、重複を代表選出から除く。
2. `materialize_visual_assets.py`
   - PDFは指定ページだけをPopplerの`pdftoppm`で200 DPIのPNGにする。
   - Officeは安全なZIPメンバー、Notebookは指定されたbase64画像だけを実体化する。
   - NFC/NFD差、パス逸脱、symlink、ハッシュ不一致、内部名衝突を拒否する。
3. `classify_visual_assets.py`
   - ローカル`gemma4:12b`で画像を1件ずつ、質問非依存に分類する。
   - モデルには画像バイトと固定プロンプト以外を渡さない。
   - 決定的ルールで後段ルートと`estimated / unresolved`を決める。

## 実データでの結果

Layer 1台帳403ファイルから、視覚素材255件を発見した。

| 素材種別 | 件数 |
| --- | ---: |
| PDFのOCR対象ページ | 112 |
| Office埋め込み画像 | 15 |
| Notebook埋め込み画像 | 54 |
| 単体画像 | 54 |
| 将来レンダリング対象の容器 | 20 |

既知の同一バイト重複8件を記録し、代表バッチ16件には含めていない。
全255件のうち、直接画像化できる235件を全件実体化した。PDF 112ページ、
Office画像15件、Notebook画像54件、単体画像54件で、失敗は0件だった。
Office内のEMF 3件は、実行バイナリSHA-256を含むLibreOfficeのrenderer identityを
署名へ固定してPNG化した。残る20件のvisual containerは、既存Layer 1で本文を
ネイティブ抽出済みである。

複数ラベルの分類結果は次のとおり。

| ラベル | 件数 |
| --- | ---: |
| `text_document` | 9 |
| `chart` | 7 |
| `illustration` | 2 |
| `table` | 1 |
| `diagram` | 1 |
| `decoration` | 1 |

全235件の分類は`classified` 202件、`needs_review` 33件、`failed` 0件だった。
primary typeはtext_document 116、chart 96、screenshot 14、diagram 5、table 2、
illustration 1、unknown 1である。154件を`ocr_text`へルーティングした。

代表16画像をCodexで目視照合し、全体の内容ラベルは16件すべて妥当だと判定した。
特に、ファイル台帳上はグラフ候補だったが、実画像は
「カテゴリ列は見つかりませんでした」という1件と、
「日付特徴量の推移」「利用可能な日付列はありません」という1件を、
ファイル名ではなく`text_document`と判定できた。

公開した2つのJSON Schemaは`jsonschema`のDraft 2020-12とFormatCheckerで検証し、
決定的validatorでも入力台帳、画像バイト、MIME、寸法、各SHA-256、モデルdigest、
署名、IDを再計算した。全件validatorは235画像の入力順、画像バイト、分類署名を
すべて再検証した。再実行では234件をcacheから復元し、初回にtimeoutした1件だけを
個別再実行して、最終的にfailed 0件とした。

## 再現コマンド

```bash
rag/.venv/bin/python scripts/build_visual_asset_manifest.py \
  --inventory artifacts/layer1-v1/deliverables/text_inventory.csv \
  --root share/共有ドライブ \
  --out artifacts/visual-classification-v1/visual-assets.jsonl \
  --batch-out artifacts/visual-classification-v1/representative-batch.jsonl \
  --materializable-out artifacts/visual-classification-v1/materializable-batch.jsonl \
  --batch-size 16

rag/.venv/bin/python scripts/materialize_visual_assets.py \
  --root share/共有ドライブ \
  --input artifacts/visual-classification-v1/materializable-batch.jsonl \
  --out-dir artifacts/visual-classification-v1/images \
  --output artifacts/visual-classification-v1/materialized-full-batch.jsonl \
  --soffice /opt/homebrew/bin/soffice \
  --dpi 200

rag/.venv/bin/python scripts/validate_visual_asset_manifest.py \
  --manifest artifacts/visual-classification-v1/visual-assets.jsonl \
  --batch artifacts/visual-classification-v1/representative-batch.jsonl \
  --materializable-batch artifacts/visual-classification-v1/materializable-batch.jsonl \
  --materialized-full-batch artifacts/visual-classification-v1/materialized-full-batch.jsonl \
  --inventory artifacts/layer1-v1/deliverables/text_inventory.csv \
  --root share/共有ドライブ \
  --batch-size 16

rag/.venv/bin/python scripts/classify_visual_assets.py \
  artifacts/visual-classification-v1/materialized-full-batch.jsonl \
  --out artifacts/visual-classification-v1/classifications-full.jsonl \
  --cache-dir artifacts/visual-classification-v1/classification-cache \
  --model gemma4:12b

rag/.venv/bin/python scripts/validate_visual_classifications.py \
  artifacts/visual-classification-v1/classifications-full.jsonl \
  --assets artifacts/visual-classification-v1/materialized-full-batch.jsonl

rag/.venv/bin/python -m unittest discover -s tests -v
```

## 残る未対応

- `visual_container` 20件はネイティブ本文抽出で被覆した。スライド配置、
  シート領域、ネイティブチャートの関係づけは意味・構造化段階で扱う。
- `table_structure`、`diagram_relations`、`image_description`の実抽出は
  次段階とする。
- 本層は意図的に`Evidence`、`SearchUnit`、検索経路へ未接続である。
