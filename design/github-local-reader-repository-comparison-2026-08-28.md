# 無料GitHubリポジトリ比較：日本企業データ読取基盤

- 調査日: 2026-08-28
- 対象: 「日本企業で使われるデータ形式調査・最終報告」の優先度A・B
- 比較軸: ライセンス、完全ローカル実行、Docker、Mac対応、保守状況、精度根拠
- 注意: ライセンス欄は技術選定用の一次整理であり、法的助言ではない。製品化・再配布時は依存関係まで含めて再確認する。

## 1. 結論

単一のリポジトリですべての形式を高精度に読む構成は、現時点では成立しない。推奨は、次の役割分担である。

1. **形式識別: DROID/PRONOM**
2. **主読取エンジン: Docling**
3. **広形式・旧形式フォールバック: Apache Tika**
4. **日本語OCR・複雑レイアウト: PaddleOCR**
5. **スキャンPDFの検索可能化: OCRmyPDF + Tesseract**
6. **旧Office変換: LibreOfficeを隔離実行**
7. **メール保管: libpff**
8. **Access: MDB Tools**
9. **圧縮ファイル: libarchive**
10. **一太郎: OpenJTDを実験枠で評価**
11. **DocuWorks: Mac完結OSSは保留。Windows補助経路が必要**

主読取エンジンはDoclingを第一候補とする。PDFのレイアウト、表、読み順、画像を統一JSONへ保持でき、Mac・ローカル・機密環境を公式に対象としているためである。ただし、Doclingだけに依存せず、Apache Tikaを広形式フォールバックとして併用する。

## 2. 評価記号

| 記号 | 意味 |
|---|---|
| ◎ | 公式に明確な対応があり、今回の用途に適する |
| ○ | 対応可能。設定や依存関係の確認が必要 |
| △ | 条件付き、実験的、または限定的 |
| × | 今回の条件では実用困難 |
| ? | 公開情報だけでは確認不足 |

保守状況は、2026-08-28時点のリリース、コミット、Issue/PR、公式文書を基にした。星の数は人気の参考にすぎず、採用判断には使わない。

## 3. 総合比較

| 候補 | 主な役割 | ライセンス | 完全ローカル | Docker | Mac | 保守状況 | 公開精度根拠 | 判定 |
|---|---|---|---:|---:|---:|---|---|---|
| [DROID](https://github.com/digital-preservation/droid) | 形式・版の識別 | New BSD | ◎ | △ | ◎ Java 21 | 活発、PRONOM更新継続 | 内部・コンテナシグネチャ。抽出精度ではなく識別範囲を評価 | 採用 |
| [Docling](https://github.com/docling-project/docling) | PDF、Office、画像、メール等の構造抽出 | MIT、モデル別確認 | ◎ | ○ | ◎ x86_64/arm64 | 非常に活発、2026年も高頻度リリース | 公開評価ツールと技術報告あり。全形式共通の単一スコアはなし | 主エンジン候補 |
| [Apache Tika](https://github.com/apache/tika) | 1000超形式の識別・テキスト・メタデータ抽出 | Apache-2.0 | ◎ | ◎ 公式multi-arch | ◎ Java/arm64 Docker | Apacheで継続、4.x系 | パーサ単位のテスト中心。レイアウト精度の統一指標なし | 採用 |
| [Unstructured](https://github.com/Unstructured-IO/unstructured) | 多形式の分割・構造化 | Apache-2.0 | ○ | ◎ Apple Silicon対応 | ○ | 活発、2026年リリースあり | 公開の同条件横断スコアは未確認 | 比較実験 |
| [MarkItDown](https://github.com/microsoft/markitdown) | 軽量なMarkdown変換 | MIT、任意依存は別 | ○ | ○ Dockerfile | ◎ Python | 活発 | READMEが高忠実度変換用ではないと明記。統一ベンチなし | 軽量補助 |
| [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) | 日本語OCR、表・レイアウト・数式・図表 | Apache-2.0 | ◎ | ○ | ○/△ | 非常に活発、2026-07更新 | PaddleOCR-VL-1.6はOmniDocBench v1.6で96.3%との公式報告 | OCR第一候補 |
| [OCRmyPDF](https://github.com/ocrmypdf/OCRmyPDF) | スキャンPDFにOCR層・PDF/Aを付与 | MPL-2.0、依存別確認 | ◎ | ◎ x64/arm64公式 | ◎ Homebrew | 活発、v17.4.1（2026-04） | OCR精度は主にTesseract依存。PDF処理の回帰テストは豊富 | 採用 |
| [Tesseract](https://github.com/tesseract-ocr/tesseract) | 画像OCRの安定した基礎エンジン | Apache-2.0 | ◎ | △ | ◎ | 安定継続、v5系 | 100超言語。日本語の今回資料に直結する統一スコアなし | フォールバック |
| [LibreOffice core](https://github.com/LibreOffice/core) | 旧DOC/XLS/PPT等の変換 | MPL-2.0/LGPLv3+ | ◎ | △ | ◎ | 非常に活発 | 互換性試験はあるが、原本レイアウト完全一致は保証されない | 隔離変換 |
| [libarchive](https://github.com/libarchive/libarchive) | ZIP、7z、RAR、LZH等の展開 | BSD系 | ◎ | 不要/○ | ◎ macOS標準採用 | 活発、3.8.7（2026-04） | 抽出のテスト対象。内容理解の精度指標ではない | 採用 |
| [libpff](https://github.com/libyal/libpff) | PST/OST/PABの抽出 | LGPL-3.0-or-later | ◎ | △ | ○ 要ビルド | 継続中だが公式Statusはalpha | 対応形式は明記。完全性の公開統一スコアなし | 条件付き採用 |
| [MDB Tools](https://github.com/mdbtools/mdbtools) | MDB/ACCDBの表・スキーマ抽出 | ライブラリLGPL、CLI/GUI GPL | ◎ | △ | ◎ Homebrew | 中程度、2026-01更新 | DBオブジェクト完全再現の指標なし。フォーム・マクロは別扱い | 条件付き採用 |
| [OpenJTD](https://github.com/KimEJ/OpenJTD) | JTD/JTT/JTTCの調査・テキスト抽出 | Apache-2.0 | ◎ | △ | ○ Rust | 新規・実験的、105 commits | 完全レンダラではないと明記。公開精度ベンチなし | 実験枠 |
| [xdwlib](https://github.com/hayasix/xdwlib) | DocuWorks API操作・テキスト化 | ZPL-2.1 | △ | × | × | 小規模、README更新2024-10 | DocuWorks本体へ依存。独立パーサではない | Mac基盤では不採用 |

## 4. 基盤候補の比較

### 4.1 Docling

**強み**

- PDFのページ、読み順、表構造、画像、数式を統一した文書モデルにできる。
- PDF、DOCX、PPTX、XLSX、HTML、ODF、EML、MSG、画像、音声、XBRLなどを扱う。
- ローカル実行とair-gapped環境を公式に掲げる。
- macOS x86_64/arm64を公式にサポートする。
- OCRエンジンをTesseract、EasyOCR、RapidOCR、macOS Vision等から選べる。
- JSONへ座標・種類・関係を残せるため、後段のグラフ化と出典表示に向く。

**弱み・注意**

- 初回にモデルを取得する構成がある。完全オフライン運用前にモデルを固定・事前配置する必要がある。
- 表構造モデルのMPS対応には制限があり、MacではCPUへ落ちる処理がある。
- 多機能なため、軽い文書にも同じパイプラインを使うと遅い。
- MITはコード本体のライセンスで、利用モデルのライセンスは別途確認が必要。

**判断**: 今回の「文字と表・図を関係付きで読む」目的に最も近い。主エンジン候補。

### 4.2 Apache Tika

**強み**

- 1000を超えるファイル形式を識別し、テキストとメタデータを抽出する。
- DOC/XLS/PPTなど旧Office、メール、アーカイブ、埋め込みオブジェクトに強い。
- Apache-2.0で企業利用しやすい。
- 公式DockerはApple Silicon向けarm64を含む。
- fullイメージにTesseract、日本語言語パック、GDAL等を含められる。
- 4.xではクラッシュ隔離プロセスと再帰抽出を強化している。

**弱み・注意**

- 文書の意味・レイアウトを高精度に復元することより、広い形式から内容を取り出すことが中心。
- 複雑な表や図表の関係理解はDocling/PaddleOCRより弱い。
- JavaランタイムまたはDockerが必要。

**判断**: 主エンジンではなく、「Doclingで読めない形式」「旧Office」「埋め込み」のフォールバックに最適。

### 4.3 Unstructured

**強み**

- PDF、Office、画像、メール等を要素へ分割する実績がある。
- ローカルPythonライブラリとApple Silicon対応Dockerがある。
- 文書分割、チャンク化、RAG投入までの部品がそろう。

**弱み・注意**

- 全形式対応ではLibreOffice、Poppler、Tesseract等のシステム依存が多い。
- OSSライブラリとクラウド/商用機能の境界を設定で明確にする必要がある。
- 今回の日本語資料に対する公開横断ベンチマークは確認できなかった。

**判断**: Doclingとの実データ比較対象。最初から両方を本番採用しない。

### 4.4 MarkItDown

**強み**

- 軽く、PDF、Office、CSV/JSON/XML、ZIP、画像等をMarkdownへ変換できる。
- Pythonだけで試しやすく、Mac対応に問題が少ない。
- 形式別の任意依存を選べる。

**弱み・注意**

- README自身が、高忠実度の文書変換を目的としていないと説明している。
- 位置、座標、表セルの厳密な証拠保持には不足する。
- OCRプラグインの依存にはPyMuPDFなど別ライセンスが含まれるため、閉鎖商用システムでは依存関係の再確認が必要。
- OCRのVLM設定によっては外部APIを使い得る。完全ローカル設定を明示しなければならない。

**判断**: 軽量プレビュー、簡単なテキスト形式、主エンジン失敗時の比較出力に限定する。

## 5. OCR候補の比較

### PaddleOCR

- 日本語、表、数式、チャート、印影、歴史資料まで対象を広げている。
- PP-StructureV3は座標を含む構造化JSON/Markdownを返せる。
- 公式発表ではPaddleOCR-VL-1.6がOmniDocBench v1.6で96.3%。ただし、これは今回の日本語企業文書100件における正答率ではない。
- Mac対応はあるが、PaddlePaddleの版、Apple Silicon、推論バックエンドによる相性を実機確認する。
- Dockerは利用可能だが、モデル・ランタイムの組み合わせが多く、TikaやOCRmyPDFより導入が重い。

**判断**: 難しいスキャン、写真、表、押印、複合レイアウトの第一候補。

### OCRmyPDF + Tesseract

- スキャンPDFへ検索可能なテキスト層を追加し、PDF/Aを生成できる。
- Homebrewと公式arm64 Dockerがあり、Mac導入が容易。
- 原画像を保ちながらOCR層を追加する用途に強い。
- OCR精度はTesseractと前処理、言語指定、画像品質に依存する。
- OCRmyPDF coreはMPL-2.0だが、Ghostscript等の依存やWebサービス例には別ライセンスがある。

**判断**: 文書保存と検索可能化には採用。複雑な表の意味復元はPaddleOCR/Doclingへ渡す。

## 6. 日本固有形式の調査結果

### 一太郎

[OpenJTD](https://github.com/KimEJ/OpenJTD) はJTD/JTT/JTTCを対象にしたApache-2.0のRust実装で、コンテナ調査、テキスト抽出、JSON/Markdown/PDF出力を進めている。

ただし、リポジトリ自身が「完全なレンダラ・エディタではない」と明記している。星数や実績もまだ小さく、現段階では次の扱いに限定する。

- 原本を変更しない調査用パーサ
- 抽出可否を判定する実験枠
- JustSystems製品または正式変換結果との照合
- 失敗時は「未対応」と記録し、推測で補完しない

### DocuWorks

[xdwlib](https://github.com/hayasix/xdwlib) はDocuWorks文書を扱えるが、Windows、DocuWorks 7以降、DocuWorks APIを必要とする。独立したXDW/XBD/XCTパーサではなく、MacとDockerだけでは動かない。

**現時点の結論**

- Mac完結の成熟した無料OSSは確認できなかった。
- 正確性を優先する場合、ライセンス済みDocuWorksを入れたWindows補助端末/VMでPDF・画像・テキストへ変換する。
- 変換後データと原本ハッシュをMac側へ戻し、出典関係を保持する。
- これは「完全OSS対応」ではなく、製品依存の補助経路として明記する。

## 7. 推奨ルーティング

```text
原本（read-only）
  ↓
DROID + MIME + 拡張子で形式・版を識別
  ↓
安全ゲート（暗号化、マクロ、埋め込み命令、巨大ファイル、圧縮爆弾）
  ↓
  ├─ PDF/DOCX/XLSX/PPTX/画像/EML/MSG → Docling
  ├─ 難しいスキャン・表・写真          → PaddleOCR
  ├─ スキャンPDFの検索可能化           → OCRmyPDF/Tesseract
  ├─ 旧DOC/XLS/PPT・広形式・埋め込み   → Apache Tika
  ├─ 変換が必要な旧Office              → 隔離LibreOffice
  ├─ PST/OST                            → libpff → EML化 → Docling/Tika
  ├─ MDB/ACCDB                          → MDB Tools
  ├─ ZIP/7z/RAR/LZH                     → libarchive → 中身を再識別
  ├─ JTD/JTT/JTTC                       → OpenJTD実験 + 正式変換照合
  └─ XDW/XBD/XCT                        → Windows/DocuWorks補助経路
  ↓
共通Evidence JSON
  ↓
検索インデックス／関係グラフ／回答監査
```

## 8. 完全ローカル運用の条件

「ローカル対応」と書かれていても、初回起動時にモデルや追加資源をダウンロードする候補がある。閉鎖環境へ入れる前に次を固定する。

- リポジトリのコミットまたはリリース版
- Python/Java/Rustランタイムの版
- Dockerイメージのdigest
- OCR・レイアウトモデルの実ファイルとハッシュ
- 言語データの版
- 依存パッケージ一覧とライセンス
- ネットワーク遮断下での起動・処理テスト
- 外部URL、外部API、テレメトリを無効化した設定

## 9. 「精度」の比較方法

各リポジトリが公表する数字は、対象データセットと評価指標が異なるため、横並びにはできない。採用前に日本企業資料の小さな共通評価セットを作る。

### 最小評価セット

- PDF: テキストPDF5件、スキャン5件、段組み・表・図混在5件
- Word: DOCX5件、旧DOC3件
- Excel: XLSX5件、旧XLS3件、複数シート・数式・結合セルを含む
- PowerPoint: PPTX5件、旧PPT3件
- 画像: 写真、FAX、TIFF、手書き各3件
- メール: EML/MSG各3件、PST/OST各1件
- 日本固有: XDW/JTDを権利上利用可能なサンプル各3件
- 圧縮: ZIP/LZH/7z各2件

### 指標

| 領域 | 指標 |
|---|---|
| 形式識別 | 正しい形式・版を識別できた割合、誤判定率 |
| 文字 | CER（文字誤り率）、重要固有名詞の正解率 |
| 表 | セル値正解率、行列対応、TEDS等の構造類似度 |
| 構造 | 見出し、段落、ページ、シート、スライド、読み順の一致 |
| 関係 | メールと添付、図とキャプション、表と注記の接続正解率 |
| 完全性 | 抽出漏れ、埋め込みファイル漏れ、非表示領域の扱い |
| 安全性 | マクロ・文書内命令を実行しない、圧縮爆弾等を止める |
| 性能 | 1ページ/1ファイルの時間、ピークメモリ、失敗率 |
| 再現性 | 同版・同入力で同じ結果と根拠位置を返せるか |

### 採用判定

- 公開ベンチマークは候補選定に使う。
- 本採用は、自分たちの評価セットで決める。
- 1つの総合点だけでなく、形式ごとの勝者を採用する。
- 同じ原本に対する2エンジンの不一致を監査対象にする。

## 10. 次の実行順序

1. DROID、Docling、Apache Tika、PaddleOCR、OCRmyPDFをインストールせずに版・依存・モデル容量を確定する。
2. 権利上問題のない20件程度の小さな評価セットを選ぶ。
3. Docling対Apache Tika対Unstructuredを同一ファイルで比較する。
4. 日本語OCRはPaddleOCR対Tesseract/macOS Visionで比較する。
5. 勝者を形式ルーターへ登録する。
6. PST/OST、MDB/ACCDB、JTD、XDWは別PoCに分ける。

## 11. 最終推奨

最初のPoCは次の5本に絞る。

| 順位 | リポジトリ | 理由 |
|---:|---|---|
| 1 | Docling | 構造・座標・表・画像をEvidence JSONへつなぎやすい |
| 2 | Apache Tika | 旧形式と広形式のフォールバックとして強い |
| 3 | PaddleOCR | 日本語画像・複雑レイアウトの精度候補 |
| 4 | OCRmyPDF | スキャンPDFを安全に検索可能化しやすい |
| 5 | DROID | 拡張子に依存しない入口判定を作れる |

UnstructuredとMarkItDownは比較対象には残すが、最初の本番構成へ同時採用しない。日本固有形式は別枠とし、成熟度不足を無理にLLMで補完しない。

## 12. 主な根拠

- [Docling GitHub](https://github.com/docling-project/docling)
- [Docling model catalog](https://github.com/docling-project/docling/blob/main/docs/usage/model_catalog.md)
- [Apache Tika GitHub](https://github.com/apache/tika)
- [Apache Tika Docker](https://github.com/apache/tika-docker)
- [Unstructured GitHub](https://github.com/Unstructured-IO/unstructured)
- [Microsoft MarkItDown GitHub](https://github.com/microsoft/markitdown)
- [PaddleOCR GitHub](https://github.com/PaddlePaddle/PaddleOCR)
- [OCRmyPDF GitHub](https://github.com/ocrmypdf/OCRmyPDF)
- [OCRmyPDF Docker documentation](https://github.com/ocrmypdf/OCRmyPDF/blob/main/docs/docker.md)
- [Tesseract GitHub](https://github.com/tesseract-ocr/tesseract)
- [DROID GitHub](https://github.com/digital-preservation/droid)
- [libarchive GitHub](https://github.com/libarchive/libarchive)
- [libpff GitHub](https://github.com/libyal/libpff)
- [MDB Tools GitHub](https://github.com/mdbtools/mdbtools)
- [OpenJTD GitHub](https://github.com/KimEJ/OpenJTD)
- [xdwlib GitHub](https://github.com/hayasix/xdwlib)

