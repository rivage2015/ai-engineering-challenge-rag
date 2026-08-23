# OCR・Document AI・表・グラフ読み取り調査（2026-08-17）

## 1. 目的と固定ベースライン

この調査は、第4回提出を壊さず、そのスコアを上回るための視覚読み取り基盤を選ぶために行う。

- baseline commit: `44917ede59fcc30440398cda814fcb3c1ed33174`
- baseline title: `2026-08-17 第4回提出 スコア0.40000`
- baseline score: `0.40000`
- baseline submission: `rag/out/submission_graph_bold_test_audited_20260817_v16.zip`
- submission SHA-256: `1b0acb3bc98fbd82833c9560735b7af653f2ac34badb40b712e7f368c8e1bd47`

本調査の変更をベースライン回答経路へ直接入れない。候補は隔離環境で比較し、正解データ、過去回答、質問IDによる選択を禁止する。

## 2. 現状のボトルネック

現行は Apple Vision と Tesseract を独立実行し、生の読み取りを保持している。これは監査可能性の面では正しいが、次が未完了である。

- OCR 5,500行のうち、完全一致は1,113行、`unresolved`は4,387行。
- OCR対象154画像のうち、record全体が`needs_review`なのは138画像。
- OCR観測は`Evidence`、`SearchUnit`、検索、回答生成へ未接続。
- 拡大crop、解像度変更、傾き・歪み補正、縦書き・手書き専用retryが共通処理として未実装。
- 表を行・列・セル・見出し階層として汎用検索へ渡す処理が不足。
- グラフは一部だけsource recoveryまたは専用規則で読めるが、汎用chart-to-table経路はない。

## 3. 最も直接的な研究上の根拠

### 3.1 OHR-Bench / OCR Hinders RAG（ICCV 2025）

- paper: [OCR Hinders RAG](https://arxiv.org/abs/2412.02592)
- official code/dataset: [opendatalab/OHR-Bench](https://github.com/opendatalab/OHR-Bench)

この論文は、OCRの評価を文字一致だけで終わらせず、retrievalとgenerationまで通して測定した点で本コンペに最も近い。

重要な知見:

- OCRノイズを`semantic noise`と`formatting noise`に分ける。
- どのOCR方式も、実世界文書を使ったRAG知識ベースの完全な構築には足りない。
- 最良方式でもground truth構造データに対してend-to-end性能が低下する。
- semantic noiseはretrievalとgenerationの両方を大きく悪化させる。
- edit distanceだけではRAG性能を説明できない。
- 表形式はHTML、Markdown、LaTeXでretrieval性能が異なる。
- VLMは表・グラフに強い場合がある一方、reading orderや高解像度密集文書に弱い。
- pipeline OCRはreading orderを規則で保持しやすいが、表・グラフの意味理解は弱い。

本コンペへの適用:

1. CER/WERだけで採用判定しない。
2. 正しいEvidenceのTop-k回収率と回答結果まで測る。
3. 表を単一表現へ潰さず、セルJSON、Markdown、bbox付き原観測を併存させる。
4. pipeline OCRとVLMを競合させるのではなく、役割分担させる。
5. VLMの生成結果をOCRの真値として無条件採用しない。

### 3.2 OmniDocBench（CVPR 2025）

- paper: [OmniDocBench](https://openaccess.thecvf.com/content/CVPR2025/html/Ouyang_OmniDocBench_Benchmarking_Diverse_PDF_Document_Parsing_with_Comprehensive_Annotations_CVPR_2025_paper.html)
- official repository: [opendatalab/OmniDocBench](https://github.com/opendatalab/OmniDocBench)

9種類の文書源、19種類のlayout、表・数式・reading order・手書きなどを別々に評価する。今回も一つの総合OCRスコアではなく、形式別に評価する必要がある。

本コンペへの適用:

- native PDF、scan PDF、slide、表、note、画像を別stratumに分ける。
- layout、text、table、reading orderを別metricで測る。
- OmniDocBenchの出力比較方法をPoC評価器の参考にする。

### 3.3 ChartQAPro / CharXiv

- paper/dataset: [ChartQAPro](https://arxiv.org/abs/2504.05506)
- official repository: [vis-nlp/ChartQAPro](https://github.com/vis-nlp/ChartQAPro)
- benchmark repository: [princeton-nlp/CharXiv](https://github.com/princeton-nlp/CharXiv)

ChartQAProでは、従来ChartQAで高得点のモデルも、実世界に近い多様なグラフ、仮定質問、非回答可能質問で大きく低下した。CharXivでも、実際の論文グラフ上ではchart専用モデルを含めて弱さが残る。

本コンペへの適用:

- chart VLMの回答をそのまま最終値にしない。
- まずsource data、Notebook、Excel、CSVから再計算する。
- sourceがない場合だけchart-to-tableを候補生成に使う。
- 軸、系列、単位、目盛り、bboxを保存し、画像へ再投影して検証する。
- 数値推定には誤差範囲と`estimated`を必須にする。

## 4. 候補技術の一次評価

### A. YomiToku

- official repository: [kotaro-kinoshita/yomitoku](https://github.com/kotaro-kinoshita/yomitoku)
- 位置づけ: 日本語特化OCR、layout、reading order、表構造、手書き、縦書き
- 出力: JSON、CSV、HTML、Markdown、searchable PDF
- local: `--lite` CPU経路あり。ブラウザ版はWebAssembly/WebGPUでローカル推論。
- current release investigated: v0.14.0、Table Semantic Parserを含む。
- license: code/modelともCC BY-NC-SA 4.0。競技利用の可否を利用規約上確認してからPoCする。
- limitation: scene text向けではない。低解像度に弱い。表semantic parserは罫線あり帳票が主対象。

評価: **ライセンス確認後の有力比較候補**。現行Apple Vision/Tesseractに対する第三の独立観測として使い、即座に真値へ昇格させない。技術適合度は高いが、最初の常設候補にはしない。

### A2. NDLOCR-Lite

- official repository: [ndl-lab/ndlocr-lite](https://github.com/ndl-lab/ndlocr-lite)
- 位置づけ: 日本語資料向けのtext detection、recognition、reading order
- input/output: 画像からJSON、XML、text、可視化
- Japanese: 活字、縦書き、手書きを対象。Apple M4での公式動作確認あり。
- license: CC BY 4.0。依存modelの条件はPoC前に個別確認する。
- strength: GPU不要で、今回のMac上で日本語の独立した第三観測を作りやすい。
- limitation: table semanticsとchart understandingは担当しない。

評価: **日本語文字認識の最初のPoC候補**。YomiTokuより利用条件が明快で、表・グラフから切り離したOCR比較に向く。

### B. PaddleOCR 3.x / PP-OCRv5 / PP-StructureV3

- technical report: [PaddleOCR 3.0](https://arxiv.org/abs/2507.05595)
- OCR paper: [PP-OCRv5](https://arxiv.org/abs/2603.24373)
- official repository: [PaddlePaddle/PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)
- 位置づけ: 小型二段OCR＋document layout＋table/formula/chart pipeline
- Japanese: PP-OCRv5 multilingualが日本語を明示対応。
- license: Apache 2.0。
- strength: 軽量、bboxとconfidence、line単位OCR、106言語、文書前処理。
- limitation: layoutや表の品質は個別に検証が必要。Paddle環境のmacOS互換性をPoCで確認する。

評価: **軽量日本語OCRの最優先比較候補**。PP-OCRv5を文字観測、PP-StructureV3をlayout/table候補として分離評価する。

### B2. PaddleOCR-VL 1.6

- paper: [PaddleOCR-VL 1.6](https://arxiv.org/abs/2606.03264)
- previous robustness paper: [PaddleOCR-VL 1.5](https://arxiv.org/abs/2601.21957)
- official repository/docs: [PaddlePaddle/PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)
- 位置づけ: PP-DocLayoutV3と0.9B VLMによるcoarse-to-fine document parsing
- input issues covered by official evaluation: scan、skew、warp、screen photograph、illumination
- tasks: text、formula、table、seal、spotting、chart
- license: PaddleOCR codeはApache 2.0。個別model cardもPoC前に確認する。
- strength: 0.9Bで、2026年6月公開論文はOmniDocBench v1.6で96.33を報告。
- limitation: benchmark値は今回の日本語社内文書上の精度を保証しない。macOS上の公式推論経路と依存関係を隔離環境で検証する必要がある。

評価: **高精度shadow parserの最優先候補**。PP-OCRv5の軽量line OCRと同じものとして扱わず、ページ全体の構造候補を作る二段目として比較する。

### B3. PP-OCRv6 small / medium

- paper: [PP-OCRv6](https://arxiv.org/abs/2606.13108)
- official repository: [PaddlePaddle/PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)
- 位置づけ: 軽量二段OCR。写真、screen capture、scene textを含む文字検出・認識。
- Japanese: small/mediumが日本語を含む多言語認識を対象にする。Apple上の速度と精度は実測が必要。
- license: Apache 2.0。

評価: **写真・画面・低品質文字の比較候補**。document layoutやtable structureは別処理に任せる。

### C. Docling / Granite Docling

- paper: [Docling technical report](https://research.ibm.com/publications/docling-an-efficient-open-source-toolkit-for-ai-driven-document-conversion)
- official repository: [docling-project/docling](https://github.com/docling-project/docling)
- model: [Granite Docling](https://www.ibm.com/granite/docs/models/docling)
- 位置づけ: unified structured document model、layout、reading order、TableFormer、複数OCR engine
- license: Docling toolkitはMIT。
- local: CPU、CUDA、MPSをサポートするモデルがあり、macOS Vision OCRも選択可能。TableFormerはCPU経路を使用可能。
- strength: RAGへ渡しやすいrich structure、bbox、table cells、複数format。
- limitation: OCR精度自体は選択したengineに依存。VLM出力の幻覚を別検査する必要がある。

評価: **既存Evidence/SearchUnitへ接続する統合器の最優先候補**。OCRを置き換えるより、ページ構造と表構造の共通表現として評価する。

### D. MinerU 2.5 / 3.x

- paper: [MinerU2.5](https://arxiv.org/abs/2509.22186)
- official repository: [opendatalab/MinerU](https://github.com/opendatalab/MinerU)
- 位置づけ: coarse-to-fineの高解像度document VLM＋pipeline parser
- input: PDF、image、DOCX、PPTX、XLSX
- output: LLM-ready Markdown/JSONと構造情報
- local: macOS 14+、Apple Silicon、pure CPU対応経路を公式READMEで案内。
- license: Apache 2.0ベースのMinerU Open Source License。追加条件をPoC前に確認する。
- strength: 複雑layout、table、formula、chart、cross-page table。
- limitation: pipeline/VLMで必要資源が大きく異なる。大きな統合面を持つため、最初から本線へ入れない。

評価: **難ページ向け二段目候補**。軽量候補で解決しないページだけにrouteするPoCが妥当。

### D2. Surya OCR 2

- official repository/model: [datalab-to/surya](https://github.com/datalab-to/surya)
- 位置づけ: 約650MのOCR、layout、reading order、table recognition統合モデル
- output: HTML付きJSON、polygon/bbox、confidence、table row/column/cell
- local: llama.cpp/MetalによるApple Silicon公式経路あり。
- license: codeはApache 2.0、weightsは修正版OpenRAIL-M。用途・組織規模条件を確認する。
- Japanese: 開発元は多言語・日本語評価を掲載するが、今回の資料で独自評価が必要。
- limitation: Macでは1pageあたりの時間が重くなり得る。scene textの担当ではない。

評価: **日本語を含むページ構造の比較候補**。ライセンス確認後、PaddleOCR-VL/Doclingと同じpage setでshadow実行する。

### E. PP-DocLayout / DocLayout-YOLO

- paper: [PP-DocLayout](https://arxiv.org/abs/2503.17213)
- paper: [DocLayout-YOLO](https://arxiv.org/abs/2410.12628)
- official code: [PaddlePaddle/PaddleX](https://github.com/PaddlePaddle/PaddleX)
- official code: [opendatalab/DocLayout-YOLO](https://github.com/opendatalab/DocLayout-YOLO)
- 位置づけ: OCR前のregion検出
- strength: text/table/figure/formula等をbboxへ分割できる。小型variantはCPU向け。
- limitation: 文字や表の意味は読まない。日本語固有の問題を直接解決しない。

評価: **region router候補**。既存visual classificationより細かいpage-region分割が必要な場合に比較する。

### F. TRivia-3B

- paper: [TRivia](https://arxiv.org/abs/2512.01248)
- official repository/model: [opendatalab/TRivia](https://github.com/opendatalab/TRivia)
- 位置づけ: table imageからHTML/Markdownを生成する3B VLM
- strength: scanned、photographed、borderless、merged-cell等を対象にする表専用モデル。
- limitation: 論文の学習は8×A100。推論も軽量OCRより重い。中国語・英語中心で、日本語表は独自評価が必要。

評価: **複雑表の後段候補**。最初のPoCには重く、YomiToku/Docling/Paddleで失敗した表だけに使用する設計がよい。

### G. Table Transformer

- paper/code: [microsoft/table-transformer](https://github.com/microsoft/table-transformer)
- 位置づけ: table detectionとcell structure recognition
- strength: bbox、row、column、functional analysisを決定的な構造として得られる。GriTS metricとPubTables-1Mを公開。
- limitation: OCRは別途必要。新しいVLMより古いが、監査可能な構造baselineとして有用。

評価: **表構造の非生成baseline候補**。VLMの表出力と比較する対照として使う。

### H. ChartGemma / ChartMoE / DePlot系

- paper/code: [ChartGemma](https://arxiv.org/abs/2407.04172)
- paper/site/code: [ChartMoE](https://chartmoe.github.io/)
- background/official explanation: [DePlot](https://research.google/blog/foundation-models-for-reasoning-on-charts/)
- 位置づけ: chart QA、chart-to-table、summary、fact checking
- strength: グラフ画像からtable/JSON/codeまたはQAへ変換できる。
- limitation: ChartQAPro/CharXivでは実世界chartで大幅な弱さが残る。自由回答は幻覚と数値誤差を検出しにくい。

評価: **最終回答器ではなく候補生成器**。source recovery不能なグラフで、chart-to-table結果を画像geometryへ再投影して検証する場合にのみ使う。

### H2. DePlot

- paper: [DePlot](https://aclanthology.org/2023.findings-acl.660/)
- official code: [google-research/deplot](https://github.com/google-research/google-research/tree/master/deplot)
- official model: [google/deplot](https://huggingface.co/google/deplot)
- 位置づけ: chart imageを線形化したtableへ変換する約0.3Bのchart-to-table model
- license: Apache 2.0。
- strength: 先にtable化するため、既存の決定計算・GraphPlanへ繋ぎやすい。
- limitation: 値ごとのbboxやconfidenceを直接返さず、日本語評価もない。単独出力を正本にできない。

評価: **グラフ数値化の第一比較器**。PaddleOCR-VLのchart出力と幾何学的復元の両方へ照合する。

### H3. ChartPointの設計思想

- paper: [ChartPoint](https://openaccess.thecvf.com/content/ICCV2025/html/Xu_ChartPoint_Guiding_MLLMs_with_Grounding_Reflection_for_Chart_Reasoning.html)
- 位置づけ: chart elementをbboxでgroundingし、cropとreflectionを経てreasoningする。
- limitation: 今回確認できた範囲では、公式code/weightsと利用条件が揃っていない。

評価: model導入ではなく、**全ての推定値へ根拠bboxを要求する設計**を取り込む。

## 4.1 日本語・実世界評価データ候補

### HakushoBench

- paper: [HakushoBench](https://arxiv.org/abs/2606.01132)
- dataset: [llm-jp/HakushoBench](https://huggingface.co/datasets/llm-jp/HakushoBench)
- 日本語政府白書の図表VQAで、今回の日本語報告書・図表質問に近い。
- 公開結果でもopen-weight modelの余地が大きく、一般chart benchmarkだけで採用判断しない根拠になる。

### JaWildText / eval_vertical_ja

- dataset: [llm-jp/JaWildText](https://huggingface.co/datasets/llm-jp/jawildtext)
- vertical evaluation: [llm-jp/eval_vertical_ja](https://github.com/llm-jp/eval_vertical_ja)
- 写真・領収書・手書き・縦書きの外部holdoutとして使用候補。
- 外部benchmarkと今回資料の両方で測定し、開発元の内部値だけで選ばない。

## 4.2 最終ショートリスト

| 導入段階 | 候補 | 担当 | 判断 |
|---|---|---|---|
| 最初の軽量PoC | NDLOCR-Lite | 日本語・縦書き・手書きの独立OCR | GPU不要。現行2系統へ追加する第三観測として試す |
| 最初の軽量PoC | PP-OCRv6 small/medium | 写真・画面・低品質文字 | bboxとconfidenceを保持し、scene text系を比較する |
| 最初の構造PoC | Docling | page・bbox・reading order・tableを共通JSONへ統合 | Evidence/SearchUnit接続の骨格として試す |
| 最初の高精度shadow | PaddleOCR-VL 1.6 | PDF・表・数式・chartを含むページ構造化 | 生成結果を正本にせず、raw・bbox・別OCRとの一致を検査する |
| 最初のchart PoC | DePlot | chart-to-table候補生成 | source recoveryと座標復元へ照合し、単独採用しない |
| 条件確認後 | Surya 2 / YomiToku | 日本語文書・表の追加比較 | weightsまたは競技利用条件を確認してから使う |
| 後段のみ | MinerU / TRivia / ChartMoE | 難ページ・複雑表・GPU比較 | 軽量PoCで未解決が残った部分だけに限定する |

## 5. 採用するアーキテクチャ仮説

単一モデルへ全資料を渡す方式は採用しない。次のcascadeをPoCで検証する。

1. native parserで読める文字・表・chart sourceはnativeを正本にする。
2. page imageをlayout regionへ分割する。
3. 日本語text regionを複数の軽量OCRで独立観測する。
4. 罫線表はYomiToku/Docling/Paddle、複雑表は後段TRivia候補で構造化する。
5. chartはsource recoveryを最優先し、sourceがなければchart-to-tableを候補生成に使う。
6. すべての候補へsource/page/bbox/model/version/hashを付与する。
7. 一致しない候補をLLMで勝手に一本化しない。
8. Evidenceはraw text、cell structure、Markdown、visual regionを併存させる。
9. retrievalで正しいEvidenceが取れることを確認してからanswerへ接続する。
10. 最終回答の数値は、可能な限り構造データから決定計算する。

## 6. PoCの優先順

### PoC-1: 日本語OCR（小さく、最優先）

- 現行 Apple Vision
- 現行 Tesseract jpn+eng
- NDLOCR-Lite
- PP-OCRv6 Japanese small/medium
- ライセンス確認後のYomiToku lite CPU

同じregion cropへ実行し、CER、重要語句recall、bbox、処理時間、失敗率を比較する。

### PoC-2: page layoutと表

- Docling + macOS Vision/TableFormer
- PaddleOCR-VL 1.6 shadow parser
- PP-StructureV3
- ライセンス確認後のSurya 2 / YomiToku Table Semantic Parser
- Table Transformerを非生成baselineとして使用

cell textだけでなく、row/column/span/header/reading orderを比較する。

### PoC-3: 難ページ

- MinerU pipeline/VLM
- 必要な表だけTRivia-3B

全ページへ常用せず、PoC-1/2の不一致・未解決ページだけを対象にする。

### PoC-4: グラフ

- source recoveryの再現率を先に測る。
- sourceなしの代表chartだけDePlotとPaddleOCR-VL 1.6を最初に比較する。
- 各値へ軸、系列、bbox、crop、許容誤差を結合するChartPoint型検証を行う。
- ChartGemma/ChartMoEは後期比較または傾向説明に限定する。
- table reconstruction、axis/legend/series extraction、数値誤差、unanswerable判定を評価する。

## 7. 評価指標

- OCR: CER、WER、重要field recall、line/bbox recall
- layout: region mAPまたはregion matching、reading-order accuracy
- table: TEDS、S-TEDS、GriTS、cell text accuracy、header binding accuracy
- chart: axis/legend/series exactness、chart-to-table cell accuracy、numeric relative error
- RAG: evidence recall@k、retrieval MRR、grounded answer accuracy
- operations: runtime、peak RAM、disk、failure/retry rate
- audit: hallucinated text/cell/value count、unresolved retention、provenance completeness

## 8. Go / No-Go条件

候補を本線へ導入するには、次をすべて満たす。

- 第4回ベースラインの既存正解候補を壊さない。
- representative holdoutで現行より重要field recallが改善する。
- 存在しない文字・セル・値の生成が増えない。
- source/page/bbox/model/version/hashを保存できる。
- 結果をEvidence/SearchUnitへ決定的に変換できる。
- ライセンス上、今回の競技利用が許される。
- ローカル環境で再現可能である。
- 正解データや質問文をOCR結果の補正へ使用しない。

## 9. 現時点の推奨

最初に試す順番は、**評価fixture作成 → NDLOCR-Lite／PP-OCRv6の軽量日本語比較 → Docling lossless JSON adapter → PaddleOCR-VL 1.6 shadow parser → DePlot＋座標検証**とする。

YomiTokuとSuryaは技術的には有力だが、weightsの利用条件を確定してから比較枠へ加える。MinerU、TRivia、ChartMoE、Qianfan-OCR等は、最初のMac PoCで未解決が残った場合の重量級・GPU比較へ回す。

MinerUは難ページへの二段目、TRiviaは複雑表への三段目とする。グラフVLMは最終回答へ直結せず、source recovery不能時の構造候補生成に限定する。
