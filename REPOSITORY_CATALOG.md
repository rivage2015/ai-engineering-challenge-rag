# GitHub収録物目録

更新日: 2026-09-01
対象リポジトリ: `rivage2015/ai-engineering-challenge-rag`
目録更新時の親コミット: `6d5cbeb`
本更新を含む追跡ファイル: 362件

## この目録の目的

このリポジトリには、コンペ提出用RAG、汎用文書処理、ローカル検索アプリ、評価用資料、設計記録が同居しています。
本書は「名前が似ているが役割が違うもの」を区別し、次にどこを見ればよいかを判断するための案内板です。

状態の意味:

- **現行**: 現在の主経路として利用するもの
- **基盤**: 他の処理から利用される共通部品
- **評価用**: 正しさを測るための合成データまたはテスト
- **研究記録**: 判断経緯や実験結果。実行プログラムではない
- **履歴**: コンペ時点の再現・比較用。現在の主経路ではない

## 最初に見る場所

| 目的 | 入口 | 内容 |
|---|---|---|
| リポジトリ全体を知る | `README.md` | 技術構成、主要CLI、実行例 |
| GitHub上の収録物を把握する | `REPOSITORY_CATALOG.md` | 本書。系統、役割、状態、違い |
| ローカル検索アプリを使う | `distribution/macos-local-memory/docs/START-HERE.html` | 非技術者向けの開始案内 |
| ローカル検索アプリを開発する | `distribution/macos-local-memory/README.md` | ビルド方法、安全境界、生成物 |
| コンペ用回答経路を確認する | `rag/main.py` | 質問への回答と提出物生成の入口 |
| 汎用文書処理を確認する | `scripts/`、`schemas/` | 抽出、検索、検証とデータ契約 |
| 判断経緯を確認する | `design/` | 設計、調査、検証、引き継ぎ記録 |
| 回帰テストを確認する | `tests/` | 形式・関係・質問理解・安全性のテスト |

## 系統別カタログ

### 1. 汎用文書処理・検索基盤

| 項目 | 内容 |
|---|---|
| 状態 | **基盤・現行** |
| 主な場所 | `scripts/`（82件）、`schemas/`（28件）、`tests/`（95件） |
| 目的 | Word、Excel、PowerPoint、PDF、CSV、JSON、画像などを、質問非依存のEvidenceへ変換し、検索・検証可能にする |
| 主な出力概念 | `Document`、`Evidence`、`Relation`、`SearchUnit`、検索索引 |
| できること | 構造抽出、語彙検索、意味検索、ハイブリッド検索、質問契約、視覚分類、OCR観測、Schema検証 |
| 含まないもの | 利用者の原本、生成済みローカル索引、Ollamaモデル本体 |

代表的な入口:

- `scripts/build_intermediate_records.py`: 各形式から中間レコードを作る
- `scripts/build_search_units.py`: Evidenceから検索単位を作る
- `scripts/evidence_text_chunking.py`: 長いEvidenceを全文復元可能なexact shardへ分割する
- `scripts/build_lexical_index.py`: SQLite BM25索引を作る
- `scripts/build_semantic_index.py`: ローカル意味索引を作る
- `scripts/search_hybrid.py`: 語彙検索と意味検索を統合する
- `scripts/build_question_understanding.py`: 質問を契約と論理構造へ変換する
- `scripts/validate_*.py`: 各工程を決定論的に検証する

### 2. コンペ用RAG・回答ルール群

| 項目 | 内容 |
|---|---|
| 状態 | **履歴＋比較用の実装** |
| 主な場所 | `rag/`（68件） |
| 目的 | SIGNATE「AI ENGINEERING CHALLENGE」の質問に回答し、CSV／ZIPを生成する |
| 主な入口 | `rag/main.py`、`rag/answer.py`、`rag/layer1_index.py` |
| 特徴 | DOCX、XLSX、PPTX、PDF、Notebook、複数資料横断などの関係ルールを個別モジュールとして保持する |
| 注意 | 汎用ローカル検索アプリとは別系統。ここにある問題別ルールを、そのまま汎用コアへ移さない |

`rag/*_rules.py`は、コンペで見つかった誤読パターンを再現・検証するためのルール群です。
例: 時点差、旧版／新版、表の強調、グラフ系列、担当者とタスク、複数資料の金額関係。

### 3. macOSローカル検索アプリ

| 項目 | 内容 |
|---|---|
| 状態 | **現行PoC** |
| 主な場所 | `distribution/macos-local-memory/`（28件） |
| 目的 | 利用者のMac内の資料を外部へ送らず、曖昧な質問から検索・回答する |
| 対象 | テキスト、Office、PDF、画像などから生成したローカルEvidence |
| 埋め込み | `embeddinggemma:latest` |
| 回答 | `gemma4:12b` |
| 監査 | 同じ`gemma4:12b`を別コンテキストの監査役として使用 |
| 機械検証 | 質問契約、主張グラフ、Evidence ID、対象関係、時制、回答投影を検証 |
| 停止方針 | 不整合や根拠不足は、誤答を返さず「わかりません」へ停止 |

内部の違い:

| 名称 | ファイル | 役割 |
|---|---|---|
| 起動・初期設定 | `app/bootstrap.py`、`app/launch.sh` | 環境診断、モデル確認、索引構築 |
| ローカル画面 | `app/local_memory_server.py` | loopback限定の検索画面と処理統括 |
| 回答生成v2 | `engine/answer_local_memory_v2.py` | 質問分解、検索、項目監査、回答投影 |
| Question Evidence Graph | `engine/question_evidence_graph.py` | 回数・合計質問の対象、範囲、coverage、再集計と保存値を結ぶ |
| 主張グラフValidator | `app/claim_graph_validator.py` | LLM監査前の決定論的な関係検証 |
| 最終監査 | `app/final_answer_audit.py` | 別コンテキストで回答とEvidenceを敵対的に点検 |
| パスグラフ | `engine/build_path_graph.py` | ファイルとフォルダの案内板を作る |
| 意味グラフ | `engine/build_semantic_graph.py` | 資料内容をEvidenceとして構造化する |
| 適応型Reader接続 | `engine/build_adaptive_semantic_graph.py` | 形式別Readerの中間記録を現行semantic境界へ接続する |
| 適応型Reader検証 | `engine/validate_adaptive_semantic_graph.py` | 由来、hash、対応範囲、安全境界をfail-closedで検査する |
| 安全分離 | `engine/content_security_gate.py` | 資料中の命令らしい記述を回答用Evidenceから隔離する |
| 意味索引 | `engine/build_local_semantic_index.py` | ローカル埋め込み索引と、未接続のGraph schema/projectorを作る |
| パッケージ生成 | `build/build_package.sh` | 未署名DMG／ZIPを作る |

### 4. General Memory評価セット

| 項目 | 内容 |
|---|---|
| 状態 | **評価用** |
| 主な場所 | `evaluation/general-memory-v0.1/`（27件） |
| 目的 | 既存経路と新しい汎用経路を、同じ既知正解で比較する |
| 中身 | 人工的に作ったMarkdown、CSV、DOCX、XLSX、PPTX、PDF、PNGと質問ケース |
| 安全性 | 実在人物、顧客、社内資料を含まない合成データ |
| 主な評価 | 曖昧検索、複数資料、旧版／新版、時点競合、画像OCR、プロンプトインジェクション隔離 |

これは検索対象として配布する「知識」ではなく、システムが正しく動くか測る試験問題です。

### 5. 設計・調査・引き継ぎ記録

| 項目 | 内容 |
|---|---|
| 状態 | **研究記録** |
| 主な場所 | `design/`（30件） |
| 目的 | なぜその設計にしたか、何を検証したか、次に何をするかを残す |
| 主な分野 | 中間Schema、Layer 1、検索評価、Graph Engineering、OCR、画像理解、日本企業形式調査、GitHubリポジトリ比較 |

重要な文書:

- `design/general-core-boundary-audit-2026-08-27.md`: 汎用コアと特化ルールの境界
- `design/query-intent-graph-engineering.md`: 質問側のGraph Engineering
- `design/japanese-enterprise-format-survey-final-2026-08-28.md`: 日本企業のデータ形式調査
- `design/github-local-reader-repository-comparison-2026-08-28.md`: 無料ローカル読取リポジトリ比較
- `design/local-reader-poc-safety-preflight-2026-08-28.md`: ローカル読取PoCの安全確認
- `design/multimodel-evidence-answer-orchestrator.md`: Evidence・回答・監査の役割分離
- `design/adaptive-document-reader-v4.md`: 形式別Reader、位置なしfallback、semantic shard、Question Evidence Graphの現行設計
- `design/local-image-orchestration-evaluation-plan-2026-08-31.md`: 完全ローカル画像読取オーケストレーションの段階的評価計画

### 6. GitHubに残っているコンペ履歴物

| 場所 | 内容 | 状態 |
|---|---|---|
| `rag/out/*.zip` | v16、v17、v43の提出ZIP | **履歴** |
| `rag/out/*.csv` | v43の予測CSV | **履歴** |
| `rag/logs/end_of_day_20260819.md` | 作業終了時の検討記録 | **履歴** |
| `rag/logs/incremental_submission_20260819_v43.json` | v43生成時の記録 | **履歴** |

これらは最新ローカル検索アプリの出力ではありません。コンペ時点の比較・再現用です。

## 名前が似ているものの違い

| 名称 | 意味 | 保存するもの |
|---|---|---|
| Path Graph | デパートの階層案内板 | フォルダ、ファイル、親子関係、パス |
| Semantic Graph | 各売場の内容説明 | 資料から観測した文章・表・意味Evidence |
| Evidence Graph | 回答根拠の関係図 | 主張、根拠、対象、時点、支持／競合関係 |
| Claim Graph | 一回の回答を検査する小さなグラフ | 質問項目、回答値、Evidence ID、時制、対象種別 |
| Question Contract | 質問が何を要求しているかの契約 | 対象、属性、時点、必須項目、全件性 |
| SearchUnit | 検索しやすくした派生単位 | Evidence参照を持つ検索本文 |
| Index | 本でいう索引・しおり | 語彙またはベクトルによる検索位置 |
| Audit | 回答を疑って確認する工程 | 未支持主張、対象違い、時点違い、停止判定 |

## GitHubに含めているもの／含めていないもの

### 含めている

- 再利用可能なPython、JavaScript、Shell等のソースコード
- JSON Schema
- 設計・調査記録
- 合成評価データ
- macOS配布パッケージを作るためのソース
- 一部のコンペ履歴物

### 原則として含めていない

- 利用者の原本資料
- 顧客・社内の実データ
- 生成済みローカル索引
- Ollamaモデル本体
- DMG／ZIPの配布ビルド
- 実行時キャッシュ、回答ログ、秘密情報
- 大会から提供された原データと評価データ

ローカルの`artifacts/`、`deliverables/`、`output/`、`tmp/`は`.gitignore`対象であり、通常はGitHubへ上がりません。

## 更新時の管理ルール

新しい機能や成果物をGitHubへ追加したときは、次の順番で更新します。

1. この目録の該当系統へ「名称・役割・状態」を追記する
2. 現行／評価用／研究記録／履歴のどれかを明記する
3. 入口となるファイルを一つ指定する
4. 利用者データや生成物を含めていないか確認する
5. 既存のREADMEとモデル名・実行経路が一致するか確認する
6. `git status --short`と`git diff --check`を確認する

追跡中ファイルの現状確認:

```bash
git -c core.quotepath=false ls-files
git status --short
git log --oneline -15
```

## 現在の整理上の注意

- `rag/`はコンペ向けの履歴・比較系統、`distribution/macos-local-memory/`はローカル検索アプリです。
- `scripts/`、`schemas/`、`tests/`が汎用基盤であり、特定問題への回答値を埋め込む場所ではありません。
- `design/`は実装ではなく、設計判断と検証結果です。
- `evaluation/general-memory-v0.1/`は試験問題であり、利用者のナレッジではありません。
- SQLiteの`graph_nodes`・`graph_edges`と独立projectorは実装済みですが、検索経路はまだ`schema_only`で無効です。
- DMG、モデル、利用者資料がGitHubに入っているわけではありません。
