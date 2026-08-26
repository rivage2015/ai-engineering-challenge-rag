# 汎用コア境界監査

作成日: 2026-08-27
対象: `codex/visual-classification-v1` / `0457cd5`
目的: コンペ提出の再現性を壊さず、Mac内の未知フォルダと未知質問で動く汎用ローカル検索・回答エンジンへ再収束する。

## 1. 質問意図契約

- requested: 大規模移動の前に、現行実装を汎用コア、再利用可能な機能、コンペ固有、製品固有に仕分ける。
- not_requested: ファイル移動、既定経路の変更、コンペルールの削除、提出物の再生成。
- forbidden: 質問固有コードを汎用と認定すること、テストなしの一括移動、評価結果から実装を逆算すること。
- ambiguity: 48個のルールモジュールは、コンペ固有の結合と汎用的なOffice解析アルゴリズムを同一ファイル内に持つ可能性がある。
- proof_obligation: 分類はimport関係、CLI入力、固定質問、固定案件名・パス、テスト範囲で確認する。

## 2. Source Universeと被覆

確認済み:

- `rag/*.py` 61ファイル、40,401行
- `scripts/*.py` 71ファイル、46,171行
- `distribution/macos-local-memory/**/*.py` 4,177行
- `graph_contract_for_question` を持つモジュール48個
- `QUESTION` 定数を持つモジュール13個
- `question_independent: True` の記述37か所
- 抽出・SearchUnit・BM25・意味索引・RRF・評価の主要実装と対応テスト
- 配布版の抽出・安全分離・索引・回答・監査・UI・ビルド経路

未確認:

- 48ルール内部の全関数単位の汎用性
- `share/共有ドライブ` 以外でのRecall@k / Hit@k / MRR
- 別Macでの初回導入と実データ回帰

## 3. 検証済み事実

1. 配布版は `rag/` と `scripts/` をimportしていない。現在は独立実装である。
2. `rag/main.py:439-441` により、`legacy_answer_path` が無効な通常経路で structured candidate が有効になる。
3. `rag/main.py:691-707` で、structured candidateが`resolved`の場合はLLM生成を行わずその回答を採用する。
4. `design/local-answer-generation.md:6` の「専用分岐は持たない」と、13個の完全質問定数および案件固有値が併存している。宣言と実装のスコープ表示に不整合がある。
5. コンペ側にはSQLite BM25、日本語2/3-gram、field/parent-child再ランク、ローカル意味索引、適応的weighted RRF、Recall@k / Hit@k / MRRがある。
6. 配布版は固定加重和検索を使い、同等の検索評価ハーネスを持たない。ただし「品質が明確に劣る」との結論は、未知フォルダで比較評価するまで保留する。
7. `tests.test_layer1_pipeline` の12テストは `rag/.venv/bin/python` でPASSした。システムPythonではNumPy不足によりimportで停止する。

## 4. 仕分け

### A. 汎用コア候補

| 機能 | 現行ファイル | 判定 | 移行前の条件 |
|---|---|---|---|
| 汎用抽出 | `scripts/probe_intermediate_records.py` | 条件付き汎用 | パスからOfficeパスワード候補を自動生成する機能は製品で明示的opt-inにする |
| シャード・再開 | `scripts/build_intermediate_records.py` | 汎用 | `--root` / `--out` 必須で固定パスなし |
| SearchUnit生成 | `scripts/build_search_units.py` | 汎用 | 入出力Schemaとバージョンを固定 |
| 字句共通 | `scripts/lexical_search_common.py` | 汎用 | Unicode正規化とtokenizer版の回帰を維持 |
| BM25構築 | `scripts/build_lexical_index.py` | 汎用 | 索引state/hash検証を維持 |
| BM25検索 | `scripts/search_lexical_index.py` | 汎用 | field/parent-child再ランクを回帰テストで固定 |
| 出典追跡 | `scripts/retrieval_trace_common.py` | 汎用 | `source_evidence_ids`の参照整合性を維持 |
| Ollama埋め込み | `scripts/ollama_embedding_common.py` | 汎用 | loopback限定は製品側で二重確認 |
| 意味索引 | `scripts/build_semantic_index.py` | 汎用 | digest、resume、prefix reuseを維持 |
| 意味検索 | `scripts/search_semantic_index.py` | 汎用 | 不正ベクトル拒否を維持 |
| 検索統合 | `scripts/search_hybrid.py` | 汎用 | 適応的RRFの閾値を設定化し、既定値は評価で決める |
| 検索評価 | `scripts/evaluate_lexical_retrieval.py` | 汎用 | 汎用評価ケースSchemaを維持 |

同時に必要なSchema/validator:

- `schemas/document.schema.json`
- `schemas/evidence.schema.json`
- `schemas/relation.schema.json`
- `schemas/search-unit.schema.json`
- `schemas/retrieval-eval-case.schema.json`
- `scripts/validate_intermediate_records*.py`
- `scripts/validate_search_units*.py`
- `scripts/validate_lexical_index.py`
- `scripts/validate_semantic_index.py`

### B. 再利用可能なCapability候補

48ルールを丸ごと`contest/`へ移すと、汎用化できるOffice・PDF・Notebook解析まで失う。次の三層に分解してから判定する。

1. 入力検出: 完全質問文、案件名、パス、固定色名など
2. 汎用観測: Office XML、セル書式、図形配置、数式、図表、PDFページなど
3. 回答投影: 集合、比較、差分、合計、丸め、出力形式

層2と層3は`capabilities/`候補。層1がコンペの完全質問や固定案件に依存する場合は`contest/bindings/`候補とする。

### C. コンペ固有と確定できる結合

`QUESTION` 定数がある13モジュール:

- `cross_project_personnel_graph_rules.py`
- `docx_rank_ratio_rules.py`
- `encrypted_plan_workload_rules.py`
- `milestone_role_task_rules.py`
- `notebook_axis_tick_rules.py`
- `notebook_date_chart_rules.py`
- `one_hot_eligibility_rules.py`
- `pptx_feature_legend_rules.py`
- `pptx_kpi_status_rules.py`
- `pptx_scope_exclusion_rules.py`
- `pptx_unfinished_action_rules.py`
- `project_id_inventory_rules.py`
- `tm_actual_hours_settlement_rules.py`

上記は少なくとも質問検出部分を`contest/bindings/`へ隔離する。その内部の観測・演算は個別にCapability候補として再監査する。

その他、完全質問定数がなくても、会社名・案件名・特定パスが固定されたモジュールは`contest/bindings/`候補とする。現時点の字面検出では17モジュールが該当したが、コメントやテストデータの誤検出があり得るため、個別関数監査前に確定件数として使わない。

### D. 製品固有として維持する資産

- `distribution/.../engine/content_security_gate.py`
- `distribution/.../app/final_answer_audit.py`
- `distribution/.../engine/build_path_graph.py`
- `distribution/.../app/bootstrap.py`
- `distribution/.../app/local_memory_server.py`
- `distribution/.../app/launch.sh`
- `distribution/.../build/build_package.sh`
- 原本読み取り専用、loopback限定、モデル役割分離、監査不合格時の「わかりません」

## 5. 修正した推奨依存方向

```text
core/                 質問非依存の抽出・記憶・検索・評価
  ↑
capabilities/         媒体固有だが案件非依存の観測・演算
  ↑                    ↑
contest/bindings/     コンペ質問・案件への結合
distribution/         安全分離・回答・監査・UI・配布
```

禁止方向:

- `core/` → `capabilities/`, `contest/`, `distribution/`
- `capabilities/` → `contest/`, `distribution/`
- `distribution/` → `contest/`

DMG構築時は、上位の`core/`と必要な`capabilities/`をパッケージ内へベンダーする。配布先のMacがリポジトリ構造に依存しないようにする。

## 6. 安全な移行順序

1. **Baseline**: 既存コンペ出力、Layer 1テスト、配布版テスト、汎用検索評価を固定する。
2. **Gate first**: 物理移動の前に、コンペルールを明示フラグ下に置く。既定値の変更は回帰値を取得してから行う。
3. **Copy behind adapter**: 最初は元ファイルを移動せず、一つの機能だけをアダプター背後で共有する。
4. **Shadow comparison**: 旧経路と新経路を同一入力で並行実行し、抽出数、source hash、上位検索、Recall@k、速度、メモリを比較する。
5. **Switch**: 等価または改善が証明できた機能だけ新経路へ切り替える。
6. **Physical move last**: import先とテストが安定してから、最後にファイルを移動する。

## 7. Answerabilityと次の一手

- Answerability: 「何を共通化すべきか」の第一段階は回答可能。
- 未解決: 48ルールの関数単位の分解境界、汎用評価データ、旧新経路の品質差。
- 次の一手: 未知フォルダ用の小さな評価セットと、旧配布検索/コンペLayer 1検索のシャドー比較ハーネスを先に作る。

この監査ではコードの移動、既定経路の変更、コンペ出力の更新は行っていない。

## 8. 小規模シャドー評価の実施結果

`evaluation/general-memory-v0.1` に、実在人物・顧客・社内情報を含まない人手作成の
合成資料10件と評価ケース6件を固定した。通常検索5件の内訳は、単一資料3件、
複数資料1件、時点競合1件。安全隔離1件は検索指標から分離した。

オフライン実測:

| 経路 | Hit@1 | Hit@3 | MRR | Source Recall@3 |
|---|---:|---:|---:|---:|
| 配布版・語彙/トークン代理検索 | 0.60 | 1.00 | 0.80 | 1.00 |
| Layer 1・実SQLite BM25 | 0.80 | 1.00 | 0.90 | 1.00 |

注意:

- 配布版は意味ベクトルを含まない明示的な代理評価であり、最終ハイブリッド性能の比較ではない。
- Layer 1は実BM25だが、LLMによる回答生成・複数資料統合は未評価。
- プロンプトインジェクション資料は配布版ゲートで`quarantine`となり、安全ストリームには出なかった。
- 同じ資料はLayer 1単体の検索では露出した。したがってLayer 1を安全ゲートなしで製品経路に接続してはならない。
- 実行時にPython 3.9互換性の問題を2件検出し、意味を変えない互換修正を行った。

検証:

- `tests.test_general_memory_shadow_eval`: 1件PASS
- `tests.test_layer1_pipeline`: 12件PASS
- 合計13件PASS
- `git diff --check`: PASS

移行判断:

- 4万行規模の一括移動は引き続き不可。
- Layer 1の検索基盤は、少なくとも小規模な通常検索では共通化候補として残せる。
- 次は安全ゲートを境界として固定したまま、汎用抽出の一機能だけをアダプター背後で共有し、
  同じ評価を再実行する。

## 9. Layer 1抽出アダプターのシャドー接続

`scripts/adapt_layer1_to_local_memory.py` を追加し、Layer 1の中間Document/Evidenceを
配布版互換Evidenceへ一方向変換した。既定の配布経路は変更していない。変換後のEvidenceは
回答索引へ直接渡さず、既存の`content_security_gate.py`を必須境界として通した。

アダプターの制約:

- Layer 1 build stateが`complete`でなければ停止
- source root、相対パス、原本SHA-256、ファイルサイズを再検証
- Document/Evidence IDの一意性と参照整合性を検証
- 原文命令は常に`never_execute`
- 安全ゲート未通過の出力から回答索引を作らない
- 元ファイル変更後の古いEvidence流用を反例テストで拒否

合成資料10件での結果:

| 経路 | Hit@1 | Hit@3 | MRR | Source Recall@3 | 期待語句被覆 |
|---|---:|---:|---:|---:|---:|
| 既存配布版代理検索 | 0.60 | 1.00 | 0.80 | 1.00 | PASS |
| Layer 1アダプター→安全ストリーム代理検索 | 0.60 | 1.00 | 0.80 | 1.00 | PASS |
| Layer 1実BM25 | 0.80 | 1.00 | 0.90 | 1.00 | n/a |

安全結果:

- 既存配布版: 危険資料を`quarantine`、安全ストリームへの露出なし
- Layer 1アダプター経路: 同じく`quarantine`、安全ストリームへの露出なし
- Layer 1単体: 危険資料が検索候補へ露出

最終回帰:

- アダプター/シャドー評価: 2件PASS（原本変更拒否の反例を含む）
- Layer 1: 12件PASS
- macOS配布版: 5件PASS
- 合計19件PASS

判断:

- テキスト・CSVのLayer 1抽出を製品互換Evidenceへ変換する最小アダプターは、
  この小規模セットでは検索指標・必要語句・安全隔離を維持した。
- ただし本番切替の証明ではない。Office/PDF、画像、レイアウト、暗号化、抽出失敗を未評価。
- 次段階は既定経路を変えず、DOCX/XLSX/PPTX/PDFの合成ケースを一形式ずつ追加する。

## 10. DOCX合成ケースの追加結果

既定経路を変更せず、DOCX 3件を追加した。

- 確定版: 本文に講演テーマ、表に会場・配布資料・担当を配置
- 旧案: 類似語を持つ旧テーマ・旧会場・旧担当を配置
- 未信頼資料: 文書内命令と秘密情報の要求を配置

評価セットは資料13件、評価ケース8件になった。通常検索6件、安全隔離2件である。
DOCX本文は、現在のLibreOffice headless環境で日本語システムフォントがPDF化時に
解決されないため英語の合成資料とした。したがって、ここで確認したのはDOCXの
構造・抽出・検索・安全隔離であり、日本語DOCXの表示互換ではない。

オフライン実測:

| 経路 | Hit@1 | Hit@3 | MRR | Source Recall@3 |
|---|---:|---:|---:|---:|
| 既存配布版・語彙/トークン代理検索 | 0.667 | 1.000 | 0.833 | 1.000 |
| Layer 1アダプター→安全ストリーム代理検索 | 0.500 | 1.000 | 0.750 | 1.000 |
| Layer 1・実SQLite BM25 | 0.833 | 1.000 | 0.917 | 1.000 |

DOCX個別監査:

- 既存配布版は確定版DOCXを1位で取得し、期待語句3件をすべて保持した。
- Layer 1実BM25も確定版DOCXを1位で取得した。
- Layer 1は表を`table_row`として保持し、`Handout / Audit checklist / Communications / Inoue`
  を同じ検索単位に結合した。本文、表セル、行見出しの出典位置も保持された。
- 現アダプターの代理検索では確定版が2位、旧案が1位になった。セル単位Evidenceへ投影する際に
  Layer 1の行コンテキストを検索単位として保持していないことが原因候補である。
- 未信頼DOCXは既存配布版とアダプター経路の両方で`quarantine`となり、安全ストリームには出なかった。
- 同じ未信頼DOCXはLayer 1単体の生検索では1位に出たため、安全ゲート境界は引き続き必須である。

検証:

- DOCX 3件をLibreOfficeで1ページずつ描画し、本文・表・余白を目視確認: PASS
- 評価ケースJSON Schema: PASS
- DOCX生成再現性を含むアダプター/シャドー評価: 3件PASS
- Layer 1: 12件PASS
- macOS配布版: 5件PASS
- 合計20件PASS

判断:

- DOCXの本文抽出、表構造、出典位置、安全隔離は小規模合成ケースで確認できた。
- ただしアダプターはLayer 1の`table_row` SearchUnitを製品互換検索単位としてまだ保持していない。
- 次はXLSXへ進む前に、表の行コンテキストをアダプター経路で保持し、確定版が旧案より上位になるかを
  同じケースで再評価する。
