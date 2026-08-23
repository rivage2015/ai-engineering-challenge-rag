# AIエンジニアリングチャレンジ 引き継ぎ

更新日: 2026-08-17 夜
対象: 第4回提出スコア0.40000を固定し、第5回提出後からOCR・表・グラフ・レイアウトのマルチモデルオーケストレーションへ進む作業

## 0. 新しいチャットへの最初の依頼

この文書を読んだ新しい担当は、いきなり実装や削除をしないでください。最初に次の依頼を実行してください。

> この引き継ぎを正本として扱ってください。まず現在の作業ディレクトリ、Git HEAD、dirty worktree、第4回・第5回の成果物とSHA-256を読み取り専用で照合してください。差異があれば実装せず報告してください。一致したら、第5回のスコア待ちであることを確認したうえで、未コミット作業を安全に分割・保全する案と、OCRオーケストレーターMVPの最初の代表実験を提案してください。質問は必要な場合も1つずつにしてください。

## 1. まず結論

- 第4回提出はスコア `0.40000` を獲得し、GitHubへコミット・push済み。これを壊さない基準版にする。
- 第5回提出は、第4回の具体回答58件をそのまま保ち、厳格に証明できた4件だけを追加した保守的な増分版。ユーザーが手動提出済みで、スコアは2026-08-18朝ごろ確認予定。再提出しない。
- 次の主戦場は、単一OCRモデルを選ぶことではない。各モデルをローカルで1本ずつ逐次実行し、質問と独立した生の観測を共通形式へ揃え、座標・構造・原本・算術で検証してからEvidenceへ昇格する「OCR／Document AIオーケストレーター」である。
- ただし、モデルごとの最終回答を多数決する設計にはしない。ネイティブ原本、独立した観測系統、幾何整合、構造検証、決定的演算を優先し、衝突は未解決のまま残す。
- 現在のOCRは画像化・実行・記録まではできているが、一般のEvidence／SearchUnit／検索／回答経路には未接続。ここをつなぐのが次の本体作業。

## 2. 作業場所とGit基準点

必ず次の実体を使う。

```text
/Users/takashifukutomi/Documents/ChatGPT/AIエンジニアリングチャレンジ
```

2026-08-17夜の確認値:

```text
branch: codex/visual-classification-v1
HEAD: 44917ede59fcc30440398cda814fcb3c1ed33174
commit: 2026-08-17 第4回提出 スコア0.40000
```

再開時の最初の読み取り専用コマンド:

```bash
cd '/Users/takashifukutomi/Documents/ChatGPT/AIエンジニアリングチャレンジ'
pwd
git status --short
git rev-parse HEAD
git log -1 --oneline
```

注意:

- Desktop側の古いコピーを作業場所と取り違えない。
- 現在のworktreeは意図的にdirty。`git reset --hard`、`git clean`、一括checkout、削除、移動をしない。
- `git add .` もしない。第5回実装、OCR PoC、設計文書が混在しているため、commitは明示的なファイル指定で分割する。
- remote pushはユーザーの明示承認後に行う。

## 3. ここまでに行ったこと

### 3.1 質問理解をグラフ化した

- 100問すべてで、元質問からQuestion Understanding Record、Question Intent Contract、候補branch、typed retrieval query、出力契約を作るGraphPlan経路を実装した。
- 元質問を保持したbranch-local queryで検索し、候補branch間は安定したround-robinで統合する。
- strict判定とadvisory判定を分離した。証明できない質問も検索・生成は可能だが、`strict_status=hold` を残し、証明済みと呼ばない。
- GraphPlanの形だけを使って正答扱いにはせず、決定的executorが解けた場合だけstructured candidateを採用する。
- 回答側でも出力契約を使い、list/scalar、件数、型、単位、小数桁、必要キーなどを検査する。形式違反は1回だけ修復し、再違反は安全側へ倒す。

### 3.2 資料固有ではなく、文法と原本構造に基づく決定ルールを増やした

主な対象:

- ExcelのPivot階層、フィルタ、条件付き書式、セル色、数式参照、回帰係数、日付範囲、担当表。
- CSV／JSON／Notebook／Python ASTを横断する決定的計算。
- DOCX／PPTXのネイティブ文字、変更差分、highlight、shape fill、表構造。
- image-only PDFの狭い質問文法に対する、ページ・領域・色・表の決定抽出。
- 全案件横断の契約／支払／略称／日付／担当など、完全列挙を証明できる場合だけ解くルール。

ルールは会社名、質問ID、正解文字列のhardcodeではなく、質問文法、scope binding、source completeness、型・一意性・算術整合を要求し、曖昧ならholdにする方針。

### 3.3 第4回提出を基準化した

第4回ZIP:

```text
rag/out/submission_graph_bold_test_audited_20260817_v16.zip
SHA-256: 1b0acb3bc98fbd82833c9560735b7af653f2ac34badb40b712e7f368c8e1bd47
score: 0.40000
```

この成果により順位が大きく上がった。したがって、「質問意図をグラフ化し、原本構造に合わせて決定的に実行する」という方向は維持する。

### 3.4 第5回提出を生成・手動提出した

第5回成果物:

```text
ZIP: rag/out/submission_graph_incremental_test_20260817_v17.zip
ZIP SHA-256: a32390d9a4a0ec1a6273869af901b2a65767df90ab06fe6379da8208600e040f

CSV: rag/out/predictions_graph_incremental_test_20260817_v17.csv
CSV SHA-256: 3c5941c7b0a3e205a3631212f08feeb4c67a539d132ab3b202c276911e80e03a

audit log: rag/logs/incremental_submission_20260817_v17.json
log SHA-256: a7b57a8ad47286fa6b9428c341c2af7403bd35c65a1c526353dc1bee9d31ed3d
```

生成方針:

- 第4回の100回答をbaseにした。
- baseが完全一致で `わかりません` の箇所だけ置換した。
- 既存の具体回答58件は全件保持した。
- 全100問のcandidate計算を完了してからbaseを読み、評価データへの逆流を避けた。
- strict pass、decision resolved、非空回答、output validator違反なしの全条件を満たしたものだけeligible。
- 19件がeligibleだったが、baseが `わかりません` だった4件だけ採用した。

採用した4件:

```text
q5  -> 6
q39 -> hum
q65 -> 相関係数が-0.99未満
q83 -> 0.38317
```

4件ともgraph strict pass、structured resolved、output validation pass、violationsなし。`adopted_count=4`、`changed_count=4`、gate bypassは0。

ユーザーが2026-08-17夜に手動提出済み。スコアは未確認。第5回を再度提出しない。

### 3.5 第5回で追加・修正した内容

- q5: 最終報告、leaderboard、metrics、run_summary、config、training code ASTを横断し、最良モデルのparameterを一意に再計算。3 JSONの型を含む完全一致とconstructorへの伝播を要求。`6.0`、bool、競合値はreject。
- q39: XLSX ChartEx内部schemaから実際の系列fieldを抽出。単なる表示titleではなく、構造に結びついたidentifierを返す。
- q65: visible sheet、意味名のexact一致、黄色ARGB `FFFFEB9C`、同色conditional formatting rule厳密1件、operatorと定数を検査。重複・unsupported rule・近似色はhold。
- q83: 係数表と対象行を一意に結び、標準化せずraw値のDecimal線形和を計算。最後だけROUND_HALF_UPで小数第5位。重複header、係数、table、ID行はhold。
- structured routing: certified extended grammarが一致した後は、generic table executorが先に誤処理しないよう順序を修正。extended source resolution失敗時はfail closed。
- output validator: source由来identifierの単一内部空白、例 `ZIP CODE` を受理。ASCII comma list `AI-05, AI-09, AI-08` を分割しつつ、数値 `1,234` は分割しない。
- incremental submission builder: no-overwrite、atomic、固定ZIP timestamp、厳密index/order/count、監査hash、base後読み、明示replace gateを実装。

### 3.6 現在の検証状態

2026-08-17夜、現在のworktreeで再実行:

```text
Ran 481 tests in 32.106s
OK
```

第5回関連targeted suiteも76/76成功。`py_compile`、差分のwhitespace check、成果物hash照合も成功。

## 4. 現在のdirty worktree

2026-08-17夜の `git status --short` は次のとおり。これはユーザーの作業を含むため、そのまま保全する。

```text
 M rag/answer.py
 M rag/score_candidate_rules.py
 M rag/structured_candidate.py
 M tests/test_question_graph_runtime.py
 M tests/test_structured_candidate.py
?? design/multimodel-evidence-answer-orchestrator.md
?? design/ocr-document-ai-research-2026-08-17.md
?? design/test100-model-coverage-2026-08-17.md
?? rag/analysis_artifact_rules.py
?? rag/excel_native_rules.py
?? schemas/docling-poc-run.schema.json
?? schemas/ocr-poc-manifest.schema.json
?? schemas/ocr-poc-run.schema.json
?? schemas/pp-doclayout-poc-run.schema.json
?? schemas/question-capability-matrix.schema.json
?? scripts/build_incremental_submission.py
?? scripts/build_ocr_poc_manifest.py
?? scripts/build_question_capability_matrix.py
?? scripts/evaluate_ocr_poc.py
?? scripts/merge_ocr_poc_runs.py
?? scripts/ocr_poc_adapters.py
?? scripts/ocr_poc_contract.py
?? scripts/run_docling_poc.py
?? scripts/run_ocr_poc.py
?? scripts/run_ocr_poc_ndlocr.py
?? scripts/run_ocr_poc_paddle.py
?? scripts/run_pp_doclayout_poc.py
?? tests/test_analysis_artifact_rules.py
?? tests/test_build_incremental_submission.py
?? tests/test_docling_poc.py
?? tests/test_excel_native_rules.py
?? tests/test_ocr_poc.py
?? tests/test_ocr_poc_ndlocr.py
?? tests/test_ocr_poc_paddle.py
?? tests/test_pp_doclayout_poc.py
?? tests/test_question_capability_matrix.py
```

重要:

- 第5回のCSV、ZIP、logは `.gitignore` の対象で、通常のGit statusには出ない。ファイルがないと判断しない。
- OCR／Docling／PP-DocLayoutのモデルweightや生成JSONLも主にignored artifact。勝手に消さない。
- commit前に、追跡対象コードと、重い生成物・cache・weightsを分ける。

## 5. OCR・視覚資料について、現状どこまでできているか

### 5.1 できていること

- native DOCX、PPTX、XLSX、CSV／TSVの文字・セル・表・一部書式はかなり安定して抽出できる。
- 視覚素材255件を発見し、235件を直接画像化。materialize failureは0。
- 視覚分類は202 classified、33 needs_review、0 failed。
- OCR対象154画像をApple VisionとTesseractで各1回、合計308 run。engine failureは0。
- `reading coverage uncovered=0`、つまり全255素材に何らかの読み取り経路を割り当てた。
- 一部の狭いPDF文法では、原本を直接renderし、OCRと幾何・色・表構造を検査する決定ルールが稼働している。

### 5.2 まだできていないこと

- `uncovered=0` は正確に文字化できた意味ではない。
- OCR 5,500 consensus lineのうち、Apple VisionとTesseractの完全一致は1,113、unresolvedは4,387。
- record全体でobservedは16、needs_reviewは138。
- OCR observationは現在 `evidence_connected:false`、`search_unit_connected:false`。一般検索と回答生成へは流れていない。
- スキャンPDF、画像埋込、スマホ写真、手書き日本語、縦書きの一般精度は未証明。
- 実データに手書き／スマホ写真のheld-out正解セットがない。手書き対応済みとは言わない。
- グラフや表で「数値が文字として書かれていない」場合は、OCRだけでは解けない。座標、軸、凡例、系列、色、罫線、bar長、pixel-to-value変換、構造検証が別途必要。

## 6. OCR比較PoCの確定結果

質問・gold・predictionから独立した、人手確認済み21 cropを作成。scan PDF 4、Office embedded 7、standalone chart 8、notebook 2。これは診断用であり、unseen full-page benchmarkではない。

4 engine、計84 run:

| Engine | Exact | Micro CER | Important span recall | 平均推論時間 | 判定 |
|---|---:|---:|---:|---:|---|
| Apple Vision | 21/21 | 0 | 31/31 | 約156ms | この診断cropでは最高。ただし選定バイアスがありautomatic winnerにしない |
| Paddle PP-OCRv6 medium Japanese | 17/21 | 0.03162 | 29/31 | 約118ms | 精度と速度のバランスが良い。`値→值`等の字形混同あり |
| NDLOCR-Lite | 14/21 | 0.11067 | 28/31 | 約519ms | 日本語候補の補助系統。timeout/raw polygon等が未完 |
| Tesseract 5.5.2 | 12/21 | raw 0.59684 | 26/31 | 約99ms | 20 completed、1 needs_review。空白や日本語断片が弱い |

主要artifact:

```text
artifacts/ocr-poc-v0.1/manifest.verified.jsonl
SHA-256: 9c22e4df9c370b14f4c13644b34a6ba63430c0be62444174d5209a2eea8770ac

artifacts/ocr-poc-v0.1/baseline-runs.jsonl
SHA-256: 0d994b9f3e88951db31768461df03c9c8fa1a24afa9c0c6dafbfacb1b736e21e

artifacts/ocr-poc-v0.1/paddle-runs.jsonl
SHA-256: b81196f8a2fd6a52d7af1e79424a0848b14d06713ef788e0c2139854bbe4212a

artifacts/ocr-poc-v0.1/ndlocr-runs.jsonl
SHA-256: 563f1a3727d2dbc885ad1ccb4943320cceed018dc1fcc13c502a49052e98756c

artifacts/ocr-poc-v0.1/combined-runs.jsonl
SHA-256: 39e5a242b3226056862275820dc3a06bc72c2aec22142a1bb1a252ed1022141f

artifacts/ocr-poc-v0.1/combined-report.json
SHA-256: 1e3bdbdfddef48a56b3a7bb13c29a281b9e9b9cf26ef1ebcd2742025b54cb1df
```

注意: `artifacts/ocr-poc-v0.1/ndlocr-smoke-runs.jsonl` は旧schemaのstale artifact。最終比較に使わない。正本は `ndlocr-runs.jsonl`。

## 7. Docling／表構造／レイアウトPoC

### 7.1 Docling 2.115.0

採用位置づけ:

- DocumentGraph、provenance、Markdown／JSON exportの器として有望。
- 単独OCRや単独TableFormerの出力を真実として本線置換しない。

実測:

- clean Office tableは目視8行×3列=24セル。
  - Docling + Tesseract: 外形8×3は合うが、非空セル11/24。
  - Docling + OCRMac: 8×3、24/24セルを保持。文字間空白も大幅改善し、速度も速い。
- complex two-column PDF page:
  - Tesseract版は33×13、OCRMac版は34×12の巨大1表へ誤統合。
  - 本来は左右2つの物理table blockと周辺本文。
  - これはOCRの文字精度より、page segmentation／region routingの問題。

Artifacts:

```text
artifacts/ocr-poc-v0.1/docling-runs.jsonl
SHA-256: 697883cb512cc29b4412b6af7dcc2ed484f2b821b8f1465f7f2905b15543da89

artifacts/ocr-poc-v0.1/docling-ocrmac-runs.jsonl
SHA-256: a01da21dfb971b2921b4089a0a58d2f6afc6dc047589a42df5b4a479858d7696
```

結論: macOSではDoclingのOCR engineにOCRMacを使う価値は高い。しかしcomplex pageは先に領域分割してから、crop単位でTableFormerとOCRを走らせる必要がある。

### 7.2 PP-DocLayoutV3

```text
model: PaddlePaddle/PP-DocLayoutV3_safetensors
revision: 97d101e6db2642e162a1d05392d1b0231c91033e
weight SHA-256: 5ea422c6cc5fe759a47e1357c35639b58173508e025a3131cbe4b6ac59e2b85e
weight size: 133,270,468 bytes
```

- closed schema、offline runner、input/model/config/hash、raw bbox／polygon／reading order、record integrityを実装。
- CPUは約0.42〜0.52秒／画像で決定的再実行に成功。
- clean tableは1 tableを検出。
- complex pageはthreshold 0.5でも巨大table 1件のまま。0.1では重複・重なりが増え、恣意的選択が必要になるため不採用。
- MPSはTransformers 5.14.0のmodel forward内でfloat64の2D sinusoidal position embeddingを生成し、MPS非対応で失敗。
- upstream monkeypatchは行わず、failure recordを保持し、PoCではCPUを明示する。

Artifacts:

```text
CPU run SHA-256: 5df2670ea25764db328019240353e96ca819582c4b489326e5d30ee498ad9e6a
CPU repeat SHA-256: ae5076a43e055d6a522337397184427d591f46e5850b809a6c6ea656c1ed9a00
MPS failure SHA-256: 9d5c28c28e77cfc5164173f5e87218d3a4711a5951e119c375e4a42ce95223f0
```

結論: 高速なlayout候補だが、現known complex fixtureを解けていないので本線昇格不可。

## 8. 100問能力表の現状

既存artifact:

```text
artifacts/test100-capability-matrix-v0.1/question-capability-matrix.jsonl
SHA-256: 6d94fa28bb49eb1b18bc753f45a88c629fe191ea8d0b4a2b9bc2b52b031d18ec

CSV SHA-256: b70c68ee1a61540d6fff4a8645cf1f41b1eb726a64adc3838feda2f64f36efd1
Markdown SHA-256: d55fe0a339989db07e0c07fa6a74f5adb57e2e7270279e084b6706b3d1c407dd
```

この表は第5回の4ルール追加前に作られ、15 certified／85 unprovenだった。現在は第5回builderで19 eligibleが確認されているためstale。次チャットで最初に再生成し、v16とv17を別axisで記録する。

注意:

- certifiedはsource、operation graph、output contractが機械的に証明された意味。
- leaderboardで正解だったことを意味しない。
- goldを見て能力タグやOCR routingを作らない。

旧matrixの主なgap:

- semantic evidence reasoning: 26
- multi-document reasoning: 21
- office structure: 15
- structured deterministic: 8
- chart-to-table: 7
- code semantics: 3
- PDF layout: 3
- spatial grounding: 2

## 9. 1.0獲得者についての現在の仮説

作業仮説としては妥当:

- 問題群に現れる文書・画像・表・グラフ・コード形式を分類する。
- 形式ごとに得意なOCR、layout、table、chart、VLM、native parser、deterministic executorを用意する。
- 単一モデルが読めない資料を、別モデルまたは原本再計算で補完する。
- 結果を共通Evidenceへ統合し、質問意図グラフから必要な観測と演算を選ぶ。

ただし、未確認なのは「1.0獲得者が実際にその実装をしている」という点。これは推測として扱う。こちらでは100問能力表とheld-out実験で、モデル追加ごとのcoverage増分を実証する。

## 10. 目標アーキテクチャ

```text
原本／画像
  ↓
質問非依存のAsset解析
  ↓
role別engine registry
  ↓
ローカルで1 engineずつ逐次実行・cache
  ↓
各engineのraw Observation sidecar
  ↓
座標・region・reading orderによるalignment
  ↓
独立性groupを考慮した一致／衝突判定
  ↓
native_exact / observed_consensus /
structurally_verified / unresolved
  ↓
DocumentGraph／EvidenceGraph
  ↓
昇格済みEvidenceだけをshadow SearchUnitへ
  ↓
QuestionGraph
  ↓
決定的Operation DAG
  ↓
AnswerGraph
  ↓
出力契約validator・renderer
```

重要な原則:

1. モデルは原則として最終回答ではなく観測を返す。
2. Apple VisionとDocling OCRMacのように同じ基盤に依存する結果を、独立2票として数えない。
3. native XLSX／CSV／JSON／codeから再計算できる値は、画像推定より優先する。
4. confidenceの高低だけで「全部読めた」と判定しない。
5. `count` や `すべて` は完全coverageの証明がある場合だけ返す。
6. 衝突や欠落を消さず、`unresolved` として保持する。
7. OCRの読みをLLMが黙って綺麗に直さない。

## 11. 次チャットで行う手順

### Step 1: 現状を読み取り専用で再確認

- cwd、HEAD、branch、dirty statusを照合。
- v16／v17 ZIP、v17 CSV／logのhashを照合。
- v17は提出済み・score pendingとして記録し、再提出しない。
- 481 testsが現状でも通るか確認する。

### Step 2: 未コミット作業を壊さず分離・checkpoint化

推奨commit単位:

1. 第5回: graph runtime／output validator／q5・q39・q65・q83 rules／tests／incremental builder。
2. OCR PoC: manifest、closed schema、adapters、runner、evaluator、Docling、PP-DocLayout、tests。
3. 設計・能力表: research document、orchestrator design、capability matrix builder／schema／tests。

実行前に各ファイルの担当範囲を再確認し、ignored artifactやmodel weightsをcommit対象に入れない。pushはユーザー承認を取る。

### Step 3: 100問能力表を現在コードで再生成

- 第5回4ルールを反映し、15→19の変化を再計算。
- v16、v17、current solverを別列にする。
- どの問題がnative extraction、OCR、layout、table、chart、multi-doc、spatial、semantic、answer-formatのどこで止まっているかを分類。
- モデル導入は「未解決能力を何問埋めるか」で選ぶ。

### Step 4: OCRオーケストレーターMVPをshadow modeで実装

最小構成:

- `engine-registry` closed schema:
  - engine／model／version／revision／license／weight hash
  - independence group
  - supported roles
  - device／timeout／cache policy
- `observation` closed schema:
  - asset SHA、page／region、raw text、bbox／polygon、order
  - engine fingerprint、config hash、raw output hash
  - status、warnings、unresolved
- cache key:
  - `asset_sha256 + region + engine_fingerprint + preprocess_profile`
- subprocess逐次runner:
  - 一度に1モデル
  - hard timeout
  - resume/cache
  - failureを記録し、黙ってskipしない
- adapter:
  - Apple Vision
  - Tesseract
  - PaddleOCR
  - NDLOCR
  - Docling OCRMac／TableFormer
- alignment:
  - IoU、center distance、reading order、cell relation
  - raw値をすべて残す
- promotion:
  - `native_exact`
  - `observed_consensus`
  - `structurally_verified`
  - `unresolved`

### Step 5: Evidenceから検索までのP0断線を修正

現在の問題:

- OCRを別intermediate layerとして追加すると、既存SearchUnit builderが同一`document_id`重複を拒否する。
- intermediateごとの`run_at`一致も要求するため、別runのvisual Evidenceをそのまま足せない。
- Layer1IndexがChunk化時に`search_unit_id`、`document_id`、`source_evidence_ids`を落としている。

必要な修正:

1. 同一source SHAのDocumentを決定的に統合する `merge_intermediate_layers` を作る。
2. raw/native/visual Evidenceを1つのDocumentへ統合し、重複や衝突を保持する。
3. SearchUnitとretrieval resultにEvidence IDsを残す。
4. AnswerGraphまで根拠線を運ぶ。
5. まずshadow indexで検証し、現行baselineを置換しない。

### Step 6: 最初の代表実験

最初は2種類を固定:

1. clean Office 8×3表
   - 既知のpositive control。
   - region 1、8×3、24セル、bbox、reading order、cell text coverageを測る。
2. complex two-column PDF page
   - 既知のnegative fixture。
   - 本来は左右2 physical table region＋周辺本文。
   - layoutで2 regionへ分け、その後crop別にTableFormerとOCRMac／Paddleを走らせる。

評価項目:

- table region count exact
- region IoU／polygon F1
- reading order
- rows／columns／cells／span
- OCR text CER／important-field recall
- 重複・幻覚・欠落
- repeat determinism
- provenance／hash completeness
- failure／unresolvedの保持

PP-DocLayoutV3がこのfixtureでgateを通らなければ、同一fixtureでPP-StructureV3、Table Transformer、条件確認後にSurya／MinerUをshadow比較する。モデルを増やすこと自体を目的にしない。

### Step 7: 能力gapに応じてモデルを追加

- 表・レイアウト: pre-segmentation + Docling/TableFormer。必要ならPP-StructureV3、Table Transformer、Surya、MinerUを比較。
- グラフ: まずNotebook／CSV／Excel／codeから再計算。画像しかない時だけchart-to-table候補を使い、軸・系列・pixel geometryで検証。Granite Vision、PP-Chart2Table等はcandidate generatorとして評価。
- 空間図: q44／q58型。Qwen3-VL-4B GGUF／Metal等は質問への直接回答ではなく、seat／name／table／orientationのbbox・point Observationだけ生成し、右側／向かいは決定的seat graphで判定。
- 手書き: 実写真・日本語手書き・傾き・低照度・縦書きの人手正解付きheld-outを先に作り、その後に専用前処理とモデル比較をする。

### Step 8: 昇格gateを通ったものだけproductionへ

必須条件:

- 質問、gold、predictionから独立した抽出。
- 文書family単位のheld-out。
- input、renderer、crop、model、config、raw outputのhash。
- hard timeoutとfailure record。
- 複数回の決定的一致。
- clean positiveで非退行。
- complex negativeの改善を数値で証明。
- unresolvedを消さない。
- Evidence／SearchUnit／retrieval／AnswerGraphのprovenanceが連続。
- 既存提出を悪化させないshadow比較。

## 12. 絶対に守る禁止事項

- `share/質問回答/questions_valid.csv`、gold、正解列、leaderboard結果、過去predictionを、抽出、OCR補正、rule作成、fixture選定に使わない。
- 質問ID、企業名、正解文字列で分岐しない。
- OCRを質問文に合わせて読み直し、都合のよい文字へ補正しない。
- 多数決だけで衝突を消さない。
- Apple VisionとOCRMacを独立2票として数えない。
- confidenceだけで完全性を認証しない。
- top-k retrievalを「すべて」の根拠にしない。
- native sourceから再計算できる場合にVLM推定を優先しない。
- PP-DocLayoutの低threshold重複boxを恣意的に選ばない。
- MPSエラーを黙ったCPU fallbackやmonkeypatchで隠さない。
- model download、外部push、提出を無断で行わない。
- dirty/untracked/ignored成果物を削除・clean・resetしない。

## 13. 次回セッションの最初の報告形式

新しい担当は、現状確認後に次の順で短く報告する。

1. Git／成果物の照合結果: 一致・不一致。
2. 第5回の状態: 提出済み、score pendingまたは確定score。
3. worktree保全上の注意。
4. その日に行う1つ目の実験。
5. 実験のpass／hold条件。
6. ユーザーに確認する質問は1つだけ。

## 14. 再開点

最も安全で価値の高い再開点は次のとおり。

1. 現状のhashと481 testsを確認。
2. 第5回スコアを記録する。ただし結果を使って質問別正解を逆算しない。
3. dirty worktreeを分割commitする案を提示し、ユーザー承認後にcommit／push。
4. capability matrixを現在コードで再生成。
5. clean表とcomplex PDFの2fixtureに対し、逐次OCR／layout Observationを共通schemaへ集約するshadow orchestrator MVPを作る。
6. complex pageを2 physical regionsへ分離できるモデルを同一gateで比較する。
7. promoted Evidenceをshadow SearchUnitへ接続し、検索・回答までprovenanceが切れないことを証明する。

ここまでできてから、次のモデルを追加する。モデル数ではなく、100問能力表の未解決セルが実際に減ったかで判断する。
