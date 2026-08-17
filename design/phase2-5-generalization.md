# Phase 2.5 質問形式とデータの汎用化 v0.1

## 状態

- 設計日: 2026-08-16
- 結論: typed compilerと決定的verifierを中心にしたhybrid方式を採用する
- 実装済み: Phase 2のfalse-ready抑止、Question Language Registry、QuestionClauseIR parser / builder / validator、source-only DataCatalog builder / validator、Catalog resolver / validator、3条件shadow gate、認定SearchUnit structured executor
- 未実装: RAG本線の検索開始条件への配線、structured実行結果のEvidence Bundle化、未支持質問文法と演算の追加
- 対象: 未知の質問形式と未知のデータを、正解漏洩なしで検索可能性まで判定する境界
- 非対象: 回答生成、正解値の推測、検索ランキングの最適化

Phase 2.5では、「対応できる質問を増やす」ことと、「解釈できない質問を
検索へ流さない」ことを分ける。未対応の表現は失敗ではなく、
`incomplete / unresolved`として保持する。カバレッジを上げるためにready条件を弱めない。

## 方式選定

| 方式 | false-ready | 質問カバレッジ | 保守性 | 締切リスク | 位置付け |
|---|---|---|---|---|---|
| regexの追加 | 全文消費と意味同値を別途証明しないと残る | 有限文法に限定 | 表現追加ごとに衝突しやすい | 低いが、穴を増やしやすい | 支持文法の高速laneに限定 |
| LLM-only | 省略、反転、幻覚的補完を決定的に否定できない | 高い | プロンプトとmodel更新で振れる | 高い | 不信な候補生成に限定 |
| hybrid typed compiler + deterministic verifier | 検証不能をholdできる | 候補提案と文法追加の両方で拡張可能 | 型、語彙、解決を分離できる | 中程度 | **採用** |

LLMは受理済み事実を決めない。使う場合も、句の候補分割や候補型の提案だけを行う。
受理する`QuestionClauseIR`、`QuestionIntentContract`、解決状態、ready判定は、
入力span、共有Registry、DataCatalog、参照整合による決定的検査で固定する。

## 既存false-ready修正の前提

Phase 2では、次の境界を先にfail-closedにした。Phase 2.5はこれを緩めず、
後段の二つの証明を追加する。

1. supported laneは、質問全体を対応文法で一意に消費できる場合だけreadyにできる。
2. supplied draftとmodel候補は提案であり、決定的な全文同値証明なしにreadyにしない。
3. ambiguityの枝が形式上すべて存在しても、質問全体の意味が各枝で保存された証明にはならない。
4. connector、否定、除外とその反転、比較演算子、件数、出力形、追加句の未消費はhold理由になる。
5. `intent_origin`と厳格化前DraftのSHA-256を保存し、model実行履歴と決定性の整合を検査する。

これらにより、現在の`QUR.intent_gate.status = pass`は「質問意図の局所契約に
合格した」ことだけを意味する。Phase 2.5完了後の検索readyと同義にしない。

## 2枝アーキテクチャ

QuestionとDataを独立にビルドし、検索直前の`CatalogResolutionRun`でだけ結合する。
DataCatalogは質問を読まず、QuestionClauseIRはDataCatalogを読まない。

```text
Question branch
  Question
    -> Registry-based parser / untrusted proposal
    -> deterministic typed compiler + semantic verifier
    -> QuestionClauseIR
    -> QuestionIntentContract
    -> QuestionUnderstandingRun / IntentGate
                                      \
                                       -> CatalogResolutionRun -> retrieval hold | ready
                                      /
Data branch
  source-only records
    -> DataCatalog builder + semantic validator
    -> DataCatalogEntry JSONL
    -> DataCatalogSnapshot
```

この結合点より前に、質問側の値をCatalogへ書き戻さない。解決結果も
QuestionClauseIRやDataCatalogEntryを更新せず、質問ごとの完了記録にする。

## 契約の境界

| 契約 | 入力 | 保持するもの | 保持しないもの | 完了条件 |
|---|---|---|---|---|
| Question Language Registry v0.1 | コードレビュー済みの有限定義 | target語彙、operator、cardinality、connector、operation option、version、digest | 完全な質問、source固有値、回答、検索データ | builderとvalidatorが同じdigestを使う |
| QuestionClauseIR v0.1 | 質問文とRegistry | 句span、role、normalized value、polarity、QIC path、coverage、provenance | source候補、カタログ照合、Evidence、回答 | 全spanとQIC bindingが検証され`coverage.status = complete` |
| QuestionIntentContract / QUR v0.1 | 質問と検証済み句候補 | requested、not_requested、forbidden、ambiguity、候補経路、IntentGate | データ上の実存判定、検索hit、回答 | `QUR.intent_gate.status = pass` |
| DataCatalogEntry / Snapshot v0.1 | Document、Evidence、SearchUnit等のsource-only record | source identity、address、source-derived scope label、field定義、capability、availability、入力と設定digest | 質問、回答、行値、query-specific relevance | canonical JSONL、ID、digest、参照、禁止情報非混入が検証済み |
| CatalogResolutionRun v0.1 | QIC / QUR、QuestionClauseIR、DataCatalogSnapshot | 解決対象、catalog entry / label / field参照、match basis、capability検査、候補、理由、入力digest | Catalogへの追記、行値、検索結果、回答 | 検索必須参照が一意に解決し`final_status = resolved` |

`CatalogResolutionRun`は質問単位のJSON recordとし、monolithにしない。
DataCatalogEntryは増分更新とstream digestの再計算を可能にするためJSONL、
DataCatalogSnapshotはそのstream全体を1件のmanifestで固定する。

## DataCatalog禁止情報

DataCatalogは「どこに何の構造があり、何の演算を実行できるか」だけを持つ。
次の情報はEntry、Snapshot、provenance、自由記述欄のいずれにも入れない。

- 質問文、質問ID、質問から抽出した語、質問ごとの候補
- 正解、回答、過去回答、人手の回答辞書
- 行値、cell値、サンプル値、一意値一覧、値のスニペット
- 件数以外の統計量、分布、集計結果、正解に結びつく特徴
- embedding、query vector、意味類似スコア、relevanceラベル、順位
- 手作業alias、質問由来の同義語、案件別・ファイル別の特別対応
- `metadata`、`notes`、`extra`など禁止情報を運べる自由形のobjectまたは自由文

sourceから決定的に取得したpath、file name、container name、field name、
declared metadataの正規化labelは許可する。ただし、これらを質問や正解に合わせて
追加・書き換えしない。

## ready条件

Phase 2.5完了後の検索readyは、次のAND条件で固定する。

```text
ready_for_retrieval =
  QUR.intent_gate.status == pass
  AND QuestionClauseIR.coverage.status == complete
  AND CatalogResolutionRun.final_status == resolved
```

さらに、各recordはJSON Schemaと決定的semantic validatorの両方に合格し、
QUR、ClauseIR、Catalog snapshot、ResolutionRunのIDとdigestが同じ実行を指さなければならない。
一つでも欠ける、再計算できない、参照先が存在しない、複数候補を一意に解決できない場合は
`clarification_required`または`abstained`とし、検索を開始しない。

`resolved`は文字列類似の最高スコアを意味しない。必須scope、target、fieldとその型、
必要なpredicate / graph operatorがCatalog内の参照とcapabilityに一意に対応し、
その対応根拠を決定的に再検査できる状態とする。

## 実装境界と未実装項目

| 要素 | 現在の状態 | まだ保証していないこと |
|---|---|---|
| Phase 2 QIC / QUR | builder、semantic validator、回帰testあり | Phase 2.5の3条件gate、実検索への配線 |
| Question Language Registry | module、version、canonical digest API、builder / validatorの共有化あり | 未知表現の自動理解 |
| QuestionClauseIR v0.1 | JSON Schema、3支持文法のparser / builder、span / ID / coverage / QIC binding validatorあり | 未登録文法の全文理解 |
| DataCatalogEntry v0.1 | source-only builder、ID / source ref / 禁止情報validator、canonical JSONL、厳格行profileによる型 / capability推定あり | 欠損・改行値・列変更を含む表のstructured実行 |
| DataCatalogSnapshot v0.1 | stream digest / count / input / configの決定的再計算validatorあり | 部分更新専用の高速増分builder |
| CatalogResolutionRun v0.1 | 決定的resolver、semantic validator、ID / provenance、実行recordあり | RAG本線が消費する実行adapter |
| End-to-end ready gate | shadow runnerがQUR、ClauseIR、Catalog Resolutionの3条件を再計算し、retrievalを起動せずready / holdを記録 | RAG開始条件へのlive配線、holdの長期運用ログ |
| Structured SearchUnit executor v0.1 | 完全ヘッダー行のfilter / project / list、数値型のmean / argmin_all、同率全件、exact Decimal計算 | 自由なSQL、join、欠損値方針、実行結果の永続契約 |

JSON Schemaの合格だけでは、値の型と閉じた形しか保証しない。
現在はspanと原文の一致、IDの再計算、参照先、全句カバレッジ、Catalogの値非混入、
JSONLのbyte digest、解決の一意性を各semantic validatorが再計算する。
ただし、これらはまだRAG本線の検索開始条件に配線していないため、
Phase 2.5 recordが作れることだけで現行RAGの検索readyを意味しない。

## 移行順

1. ✓ Phase 2のfalse-ready回帰を固定し、readyとholdの現状を保存する。
2. ✓ QuestionClauseIRのsemantic validatorを実装する。span、順序、重複、coverage、ID、Registry digest、QIC path bindingを再計算する。
3. ✓ 現在のsupported grammarからClauseIRを作る決定的parser / builderを実装し、未対応表現は`incomplete`で保持する。
4. ✓ DataCatalogのsemantic validatorとsource-only builder、canonical JSONL / Snapshot builderを実装する。
5. ✓ CatalogResolutionRunの決定的resolverとsemantic validatorを実装する。
6. ✓ 3条件ready gateをshadow modeで実行し、現行の検索を変更せずready / holdを記録する。
7. 次: adversarial mutationとopaque holdoutの受け入れ条件を通した後だけ、3条件gateを検索開始条件に切り替える。
8. 安全境界を維持したままRegistryとtyped grammarを追加し、hold率を別指標として改善する。

LLM候補laneは、決定的laneと同じsemantic validatorによる全文証明が成立するまで
shadow / proposalに留める。導入順の都合でこの条件を外さない。

## 受け入れ条件

### 1. zero false-ready

- 固定回帰とadversarial mutation suiteでfalse-readyが0件である。
- 追加句、否定、演算子反転、件数変更、scope変更、出力変更、参照改ざんをすべてholdする。
- Schemaまたはsemantic validatorを実行できない場合はpassにしない。
- false-ready 0は定義済みsuiteとholdoutの受け入れ基準であり、未知文法の完全理解を意味しない。

### 2. opaque holdout

- 現在は未登録名のmetamorphic置換を一覧問50組、複合計算問20組で実行し、Catalog生成からstructured実行まで70組全件を通過している。
- この70組は回帰テストであり、parser / builder実装者から本文を隠した独立sealed holdoutの代替にはしない。
- parser / builder実装者から本文、source label、期待回答を隠した未見setで評価する。
- 評価時に正解、過去回答、個別対応辞書をbuilder / resolverの入力に渡さない。
- 支持文法はClauseIR completeとQICの意味同値を満たし、未支持文法はfalse-readyではなくholdする。
- 新しいsourceの名前やfield名はDataCatalogから解決し、Registryへ個別追加しない。

### 3. 増分snapshot

- 同一のsource-only inputsとbuild configは、Entry JSONL、Entry ID、stream digest、Snapshot IDを再現する。`generated_at`はID導出から外す。
- sourceが1件変更されたとき、影響するEntryだけが変わり、非影響EntryのIDとcanonical bytesは不変である。
- sourceの追加・削除後に、record count、sort order、stream digest、input digest、参照整合を再計算する。
- 削除済みsourceへの孤立参照や、古いEntryの残留を許可しない。

## 汎用化の判定

- **新しいデータ**: 固有名に依存せずCatalogの生成、scope / field解決、認定表のstructured実行まで対応する。実データ340文書・412,744 SearchUnitから1,028 Entryを決定的に再生成し、136 Entry / 593 fieldをstructured / typedと認定。未知名の実表に対してscope / field結合、13行再検査、2演算実行まで確認済み。値はCatalogとshadow logへ永続化しない。
- **新しい質問形式**: Registryとtyped grammarに追加され、ClauseIRの全文証明を通せる形式に限定する。
- **特化 / hardcode**: 完全な質問、固有のsource名、field名、record ID、回答値による分岐は禁止する。

したがってPhase 2.5の目標は、未知入力を無条件にreadyにすることではない。
質問非依存のDataCatalogと全文検証可能なClauseIRを使い、
「検索可能」と「わからない」の境界を、未知データでも再現することである。
