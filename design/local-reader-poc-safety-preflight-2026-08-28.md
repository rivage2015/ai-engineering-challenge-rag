# ローカル読取PoC・安全事前確認

- 確認日: 2026-08-28
- 方針: 新規インストール、モデル取得、Docker導入、外部送信を行わずに現状を確認する
- 対象: DROID、Docling、Apache Tika、PaddleOCR、OCRmyPDF/Tesseract

## 1. 結論

新しい5製品を一括導入する必要はない。現在のMacには、過去のPoC成果と主要なネイティブ依存がすでにあり、まずそれを再利用するのが最も安全である。

安全な開始順は次の通り。

1. 既存Apple Vision、Tesseract、PaddleOCR、Doclingの成果を正本として固定する。
2. 既存の21件評価セットと36件のテストを再利用する。
3. Docling/PaddleOCRの実行環境だけを、既に評価した版で隔離再構築する。
4. DROID/Tika用JavaとDockerは、必要性が確認されるまで導入しない。
5. OCRmyPDFも、スキャンPDFの検索可能化テストを始める時点まで導入しない。

## 2. Macの確認結果

| 項目 | 確認結果 |
|---|---|
| 機種 | MacBook Pro `Mac17,2` |
| SoC | Apple M5、10 cores |
| メモリ | 24 GB |
| 空き容量 | 約703 GiB |
| Homebrew | 6.0.17 |
| システムPython | 3.14.6 |
| 既存プロジェクトvenv | Python 3.9.6、Pillow 11.3.0 |
| Docker | 未導入 |
| Java runtime | 未導入 |

容量とメモリはPoCに十分である。ただし、Docling/PaddleOCRが過去に動いたPython 3.12環境は現在の通常パスからは確認できない。システムPython 3.14へ直接追加せず、専用Python 3.12環境を再作成する方が安全である。

## 3. すでに導入されている依存

| ソフト | 現在の版 | 用途 |
|---|---:|---|
| Tesseract | 5.5.2 | OCR基準、回帰検知 |
| tesseract-lang | 4.1.0 | 日本語等の言語データ |
| Poppler | 26.07.0 | PDF描画・抽出 |
| LibreOffice | 26.2.4 | 旧Officeの隔離変換候補 |
| libarchive | 3.8.8 | ZIP、LZH、7z等の展開 |

現段階でDockerを入れなくても、PDF描画、OCR、旧Office変換、圧縮展開の基礎はそろっている。

## 4. 過去のPoCで確認済みの版

### PaddleOCR

| 項目 | 固定値 |
|---|---|
| paddleocr | 3.7.0 |
| paddlepaddle | 3.3.0 |
| paddlex | 3.7.0 |
| Python | 3.12.13 |
| pipeline | PP-OCRv6 medium / japan |
| text detection model | PP-OCRv6_medium_det、62,298,334 bytes |
| text recognition model | PP-OCRv6_medium_rec、76,862,530 bytes |
| 実行 | CPU、10 threads、fp32 |
| 外部接続 | 評価時の推論はローカル。モデルは事前取得物 |

パッケージと2モデルだけで記録上約589 MB。Python環境の共通依存を含めると、実際の環境はこれより大きくなる。

### Docling

| 項目 | 固定値 |
|---|---|
| docling | 2.115.0 |
| docling-core | 2.91.0 |
| docling-ibm-models | 3.14.0 |
| docling-parse | 7.13.0 |
| ocrmac | 1.0.1 |
| torch | 2.13.0 |
| torchvision | 0.28.0 |
| Python | 3.12.13 |
| layout model | docling-layout-heron、171,766,045 bytes |
| table model | TableFormer accurate v2.3.0、212,765,448 bytes |
| 外部サービス | 無効 |

記録済みモデルは約385 MB。Torch等の環境容量は別途必要。

## 5. 既存精度結果

### OCR 21件の診断用評価

| engine | 完全一致 | text CER | span recall | warm p50 |
|---|---:|---:|---:|---:|
| Apple Vision | 21/21 | 0.0000 | 1.0000 | 152.19 ms |
| PaddleOCR PP-OCRv6 medium japan | 17/21 | 0.0316 | 0.9355 | 約99 ms |
| NDLOCR-Lite | 14/21 | 0.1107 | 0.9032 | 492.66 ms |
| Tesseract 5.5.2 | 12/21 | 0.2213 | 0.8387 | 92.13 ms |

この21件は診断用に選ばれた領域であり、手書き、自然写真、縦書き、ページ全体の読み順、表構造、RAG回答精度を証明しない。自動的な勝者選定は無効のままとする。

### Doclingの実測

- clean Office table: 目視8行×3列、24セル。
- Docling + Tesseract: 外形8×3、非空セル11/24。
- Docling + OCRMac: 8×3、24/24セル。
- complex two-column PDF: 左右の表を分離できず、33×13または34×12の巨大表へ誤統合。

したがって、Docling + OCRMacは有力だが、複雑ページでは事前領域分割が必要である。

## 6. テスト確認

システムPython 3.14ではPillowが未導入のため、対象テストはimport時に停止した。これはコード不具合ではなく実行環境の不一致である。

既存の `rag/.venv` では次を確認した。

```text
Pillow 11.3.0
36 tests
Ran 36 tests in 1.084s
OK
```

対象テスト:

- `tests.test_docling_poc`
- `tests.test_ocr_poc`
- `tests.test_ocr_poc_paddle`
- `tests.test_pp_doclayout_poc`

新しいパッケージは追加していない。

## 7. 未導入候補の安全判定

| 候補 | 現状 | 直ちに必要か | 判断 |
|---|---|---:|---|
| DROID | Java 21なし | いいえ | 形式識別PoC直前まで保留 |
| Apache Tika | Java/Dockerなし | いいえ | Doclingで読めない旧形式の試験時まで保留 |
| OCRmyPDF | 未導入 | いいえ | スキャンPDF評価時に限定導入 |
| Docker Desktop/代替 | 未導入 | いいえ | ネイティブPoCを先に行う |

Dockerを先に入れると、容量、バックグラウンドサービス、ネットワーク、イメージ依存が増える。現状ではネイティブMac環境を先に使う方が単純で監査しやすい。

## 8. 次に許可してよい最小変更

次回の実装は一度に1つだけ行う。

### 推奨する第1変更

プロジェクト専用のPython 3.12仮想環境を作り、過去に評価済みのDocling版だけを固定して再現する。

条件:

- システムPythonを変更しない。
- `rag/.venv`を上書きしない。
- 新しい専用ディレクトリを使う。
- 依存版をlockファイルへ保存する。
- モデルは指定ディレクトリへ置き、ハッシュを照合する。
- 推論時の外部サービスを無効にする。
- 既存の3件程度でsmoke testし、結果が一致しなければ停止する。
- 既存PoC成果を上書きせず、新しいartifact版へ出力する。

### その後

1. PaddleOCRの評価済み環境を別venvで再現。
2. DoclingとPaddleOCRの複雑ページ領域分割を比較。
3. DROID/Tikaは旧Office・未知形式の評価セットが準備できてから追加。

## 9. 停止条件

次のどれかが起きたら自動継続しない。

- 依存版がApple M5/Python 3.12で解決しない。
- 記録済みモデルハッシュと取得物が一致しない。
- 推論中に意図しない外部通信が必要になる。
- 既存3件のsmoke testが以前の結果と一致しない。
- 既存artifactまたは原本を上書きしそうになる。
- ライセンスが比較報告と異なる。
- 追加容量が事前見積りを大幅に超える。

