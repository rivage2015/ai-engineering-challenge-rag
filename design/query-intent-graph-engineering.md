# 質問側グラフエンジニアリング v0.2

## 状態

- 設計日: 2026-08-16
- 状態: Phase 1契約とPhase 2質問理解エンジン実装済み（v0.2）
- 対象: 質問解釈から検索、根拠検査、回答、出力検査まで
- 非対象: 検索索引の再生成、検索器・回答器への実配線

設計ルールの版とレコード形式の版は分離する。Phase 1で公開する
`QuestionIntentContract`、`QuestionUnderstandingRun`、`QueryRun`の`schema_version`は`0.1`、この設計契約を
識別する`runtime_metadata.rule_version`は`v0.2`とする。一方の版から他方を推測しない。

## 目的

質問文をそのまま検索入力にせず、回答AIにも質問文だけを単独で渡さない。先に「何を求めているか」
「何を求めていないか」「どの回答を禁止するか」を型付きの契約にし、
複数の解釈候補を並列に検証する。

回答は、質問からEvidenceまでの意味付き経路と、質問型ごとの証明条件の両方を
満たした場合だけ許可する。根拠から必須出力に関する解釈を一意に決められない場合は、
無理に推測せず「わかりません」に相当する`abstain`を返す。

## 位置付け

本設計のグラフエンジニアリングは、固定語彙をグラフDBに入れることではない。
次のコンパイラ型手順をRAGの質問側に適用する。

```text
decompose
  -> typed nodes
  -> meaningful edges
  -> parallel candidates
  -> audit
  -> primary path
  -> compress / render
```

| IG-GEの手順 | RAGでの対応 |
|---|---|
| `decompose` | QuestionIntentContractへ分解する |
| typed `N` | 対象、scope、演算、出力、Evidenceを型付きノードにする |
| meaningful `E` | 出典付きの文脈、演算依存、支持、矛盾の関係だけを張る |
| `independent -> parallel` | 解釈候補をCandidateQueryPathの別枝にする |
| `audit` | Intent、Evidence、Proof、Answerability、Outputを別々に検査する |
| `primary` | Evidence確認後にだけPrimaryQueryPathを選ぶ |
| `compress / copy` | AnswerPlanで許可Claimを圧縮し、決定的整形または根拠限定生成を行う |

物理ストレージは当面JSON / JSONLでよく、グラフDBの導入は必須としない。

## レイヤー分離

```text
原本
  -> Observation / Document / Evidence / Relation
  -> SearchUnit / index

質問
  -> 質問意図グラフ
  -> 検索候補経路
  -> Evidenceとの照合
  -> 回答経路グラフ
  -> 回答
```

### 1. 原本・根拠レイヤー

- 質問非依存で作る。
- ObservationとEvidenceは質問や正解に合わせて書き換えない。
- 原本の位置、SHA-256、抽出方法へ逆追跡できるようにする。
- `exact / observed / estimated / unresolved`を混同しない。
- 詳細は[中間データ共通構造 v0.1](intermediate-data-schema.md)と
  [検索用派生層 v0.2](search-derived-layer.md)に従う。

### 2. 質問意図・回答経路レイヤー

- 質問ごとに作る派生データとし、検索索引へ入れない。
- Evidenceを読み取るが、Evidenceへ書き戻さない。
- `documents.jsonl / evidence.jsonl / relations.jsonl / SearchUnit`へ質問ノードを混ぜない。
- 既存のRelationはDocument / Evidence間の永続関係専用とし、質問経路に流用しない。
- 検索前は複数の候補経路を保持する。
- Evidenceの確認後にだけ主経路を選ぶ。
- 質問契約、経路、採否理由、使用Evidenceを`QueryRun`に残す。

### 3. 回答表現レイヤー

- 個数、ID、値、Yes/Noは原則として決定的に整形する。
- 説明文は許可済みClaimだけを言語化する。
- 回答AIに事実選択、再集計、未解決値の補完をさせない。
- 生成後の回答も契約とEvidenceに対して再検査する。

## 不変条件

1. `questions_valid.csv`の正解列、過去回答、診断レポートを検索根拠に使わない。
2. 質問の明示内容を、弱い別名、意味類似、他の質問で上書きしない。
3. 独立したSIGNATE質問の前後行を会話文脈として使わない。
4. 希望していないと推定した内容は、原則として非優先に留める。
5. 禁止は根拠と機械検査方法を持つルールだけにする。
6. 不明な対象、範囲、単位、値を「もっともらしい候補」で埋めない。
7. 検索スコアをEvidenceの信頼度や回答の正解確率とみなさない。
8. Top-kの取得件数を`count`や`list all`の全件性証明にしない。
9. 後続演算は直前の出力集合を継承し、明示指示なしに全体集合へ戻さない。
10. 結果が偶然同じでも、誤った演算子、丸め、対象集合を合格にしない。
11. 手動検証ケースは評価fixtureとし、案件名、質問文、ファイル名ごとの専用分岐を実装しない。

## 全体ワークフロー

```text
Question
  -> QuestionIntentContract
  -> QueryContextGraph
  -> CandidateQueryPath[]
  -> IntentGate
  -> RetrievalPlan[]
  -> logical parallel retrieval
  -> RetrievalHit[]
  -> RetrievedEvidenceBundle[]
  -> CandidateEvaluation[]
  -> PrimaryQueryPath
  -> ProofObligation
  -> AnswerabilityGate
  -> AnswerPlan
  -> DeterministicRenderer | GroundedGenerator
  -> OutputValidator
  -> accept | bounded_regenerate | abstain
  -> QueryRun
```

## 1. QuestionIntentContract

### 役割

質問文を検索可能で検査可能な契約へ分解する。主要要素は
`requested`、`not_requested`、`forbidden`、`ambiguity`の4種類とする。

### スキーマ概要

```yaml
question_intent_contract:
  schema_version: "0.1"
  record_type: question_intent_contract
  question_id: null
  original_question: null
  requested:
    target:
      surface: null
      canonical_type: null
      instance: null
    scope:
      container: null
      location: null
      time_or_version: null
      filters: []
      source: explicit | conversation | unknown
      match_mode: exact_normalized | exact | range | semantic_candidate | unknown
    operation_graph:
      operation_graph_id: null
      nodes: []
      edges: []
    requested_outputs:
      - output_id: null
        source_operation_ref: null
        return_field: count | identifier | name | value | status | description | reason | procedure | comparison_result | boolean | unknown
        cardinality:
          mode: single | multiple | all | unknown
          expected_count: null
        answer_shape:
          container: scalar | list | key_value | table | prose | yes_no | unknown
          value_type: integer | number | string | identifier | boolean | unknown
          unit: null
          precision: exact | approximate | unspecified
    derived_summary:
      operation: count | list | retrieve | compare | calculate | explain | procedure | verify | unknown
      return_fields: []
      cardinality: single | multiple | all | mixed | unknown
  not_requested: []
  forbidden:
    global: []
    query: []
    evidence: []
  ambiguity: []
```

`requested.scope`を範囲の正本とし、`target`に重複したscopeは保持しない。
`operation_graph`と`requested_outputs`は単純な質問でも正本とする。単純な質問は1演算ノード、1出力で表す。
`operation`、`return_field`、`cardinality`などの要約値は独立した正本にせず、必ずこの2項目から導出する。

### 空値と実行状態

- `null`は「非該当」に限定する。
- `unknown`は「該当するが、質問からは解釈できない」を表す。
- 工程が未実行であることを`null`で表さず、`QueryRun`のstage statusで表す。
- 本文のYAMLにある`null`のうち単なる未記入例は、公開JSON Schemaでこの規則に従う型へ置き換える。

### requested

- `target`: 何に対して操作するか。
- `scope`: 案件、文書、場所、期間、版、フィルタ。
- `operation_graph`: 単一または複数演算の順序、入出力、依存関係。
- `requested_outputs`: 出力ごとの返却項目、件数、形、型、単位、精度。
- `derived_summary`: 検索・監査表示用の導出値。独立して書き換えない。

正規化できない対象は推測で確定せず`unknown`とし、候補は
`ambiguity`へ送る。質問にないscopeを検索都合で足さない。

### not_requested

`not_requested`は禁止ではなく、返答に必要ではないと推定した内容である。
検索時は原則として`exclude`ではなく`deprioritize`に使う。

```yaml
not_requested:
  - item: null
    reason: null
    confidence: high | medium | low
    handling: omit | deprioritize | include_only_if_needed
```

- 質問から明確に外れる内容だけを登録する。
- 低信頼の推定を強制的な除外に使わない。
- 根拠の説明に必要な情報まで機械的に削除しない。
- 「返すな」と明示された内容は`forbidden.query`とする。

### forbidden

```yaml
forbidden_rule:
  rule_id: null
  category: global | query | evidence
  prohibition: null
  basis: null
  basis_ref: null
  applies_to: [intent, retrieval, generation, validation]
  check:
    validator_id: null
    params: {}
  on_violation: reject | regenerate | abstain
```

#### global

- 資料にない値や関係を作らない。
- `unresolved`を推測で埋めない。
- 相関を因果として断定しない。
- 質問に合わせてObservationやEvidenceを書き換えない。
- 正解データや過去回答をEvidenceとして使わない。

#### query

- 操作、対象型、返却項目、件数、範囲、形式を取り違えない。
- 明示されたフィルタの比較演算子を変えない。
- 明示された対象集合を無断で広げない。

#### evidence

- `estimated`を`exact`として返さない。
- 単位不明の値へ単位を付けない。
- 異なるversion、scope、unitの値を混ぜない。
- 出典へ戻れない主張を確定回答に使わない。

Phase 1で実行可能として登録する`validator_id`は次の12個に閉じる。
未登録IDは検査を省略せずエラーにする。Phase 1では各`params`を空objectに固定し、
パラメータ付きValidatorは版を上げてから追加する。

| category | validator_id | 必須stage | 検査対象 |
|---|---|---|---|
| global | `claims_supported_by_evidence` | generation, validation | 回答ClaimがEvidence参照を持つ |
| global | `unresolved_never_promoted` | generation, validation | `unresolved`を確定値へ昇格しない |
| global | `causality_requires_source_relation` | generation, validation | 因果主張が出典上の関係を持つ |
| global | `evidence_is_read_only` | retrieval, generation, validation | 質問層からEvidenceへ書き戻さない |
| global | `answer_sources_are_excluded` | retrieval, validation | 正解・過去回答・提出物を根拠にしない |
| query | `operator_preserved` | intent, validation | 比較演算子と演算順を変えない |
| query | `hard_scope_not_expanded` | intent, retrieval, validation | 明示scopeを無断で広げない |
| query | `output_contract_match` | intent, generation, validation | 返却項目、件数、形、単位を守る |
| evidence | `estimated_not_exact` | retrieval, generation, validation | `estimated`と`exact`を混同しない |
| evidence | `unit_requires_evidence` | retrieval, generation, validation | 根拠のない単位を補わない |
| evidence | `compatible_evidence_only` | retrieval, generation, validation | scope、version、unitが異なる根拠を直接統合しない |
| evidence | `provenance_required` | retrieval, generation, validation | 使用Claimを原本位置へ逆追跡できる |

`applies_to`は任意指定ではなく、このregistryの必須stage集合と完全一致させる。
上流中断でstageが`skipped`ならそのstageの結果だけを省略し、別stageで代用しない。

`forbidden.evidence`のルール定義は初期契約に含めるが、違反判定は
RetrievedEvidenceBundle生成後まで実行中の一時状態として`not_evaluated`とする。

`forbidden_rule`は禁止の定義であり、検査状態を保持しない。検査結果は
`ForbiddenCheckResult`として分離し、完了時の`QueryRun`に記録する。

```yaml
forbidden_check_result:
  rule_id: null
  stage: intent | retrieval | generation | validation
  validator_id: null
  validator_version: null
  status: pass | violation | error
  subject_refs: []
  evidence_ids: []
  details: {}
  action_taken: none | reject | regenerate | abstain
```

完了ログでは、実行したstageに適用されるルールは必ず結果を持つ。
上流の中断により検査自体を実行しなかった場合は、結果を`null`や
`not_evaluated`で補わず、対応stageを`skipped`とする。未知の`validator_id`、不正な
`params`、Validatorの`error`は無視せずfail-closedとする。

「何となく違う」は禁止にせず、`not_requested`または`ambiguity`に残す。

### ambiguity

```yaml
ambiguity:
  - field: target | operation | return_field | scope | answer_shape
    issue: null
    candidates:
      - value: null
        confidence: high | medium | low
        basis: null
    impact: high | medium | low
    resolution:
      - retrieve_parallel
      - resolve_from_evidence
      - answer_with_qualification
      - abstain
```

- confidenceだけで一つに決めない。
- Evidenceで区別できる候補は別の枝として検索する。
- Evidenceでも区別できず、曖昧性が必須出力を左右するなら`abstain`とする。
- 限定付き回答を許可するのは、曖昧性が非必須の補足情報だけに残り、
  必須出力のProofObligationはすべて満たす場合に限る。

`answer_with_qualification`はPrimaryQueryPathの一意性を省略する例外ではない。
operation、target、scope、requested outputsの主経路は一意であり、未解決なのが
回答の成否に影響しない補足情報だけの場合に使う。

Phase 1にはambiguityと解決Evidenceを結ぶID参照がまだないためfail-closedにし、
`impact: high`のambiguityを残したQueryRunは`accepted`にしない。Phase 2でambiguity ID、
候補枝、解決Evidenceの参照契約を追加後、解消を証明できるものだけを許可する。

## 2. QueryContextGraph

質問の文字列だけでは解決できない略語、指示語、対象、範囲を、
出典付きの文脈エッジとして保持する。

### 優先順位

```text
1. question_explicit
2. conversation_explicit
3. source_local
4. source_metadata
5. semantic_candidate
```

下位の文脈は上位の明示内容を上書きしない。上位の明示値と矛盾する下位候補は
`rejected_context`とする。最上位の根拠自体が複数解釈を持つ場合、または同一優先度の根拠同士が
衝突する場合だけ`ambiguity`へ戻す。

```yaml
context_edge:
  from: null
  to: null
  relation: null
  source_type: question_explicit | conversation_explicit | source_local | source_metadata | semantic_candidate
  source_ref: null
  support_level: high | medium | low
```

`support_level`は文脈エッジの根拠強度であり、回答の正解確率ではない。

### エンティティ解決

```yaml
entity_resolution:
  explicit_full_name: hard_scope
  exact_alias: hard_scope
  ambiguous_partial_alias: soft_candidate
```

例えば完全な会社名が質問にある場合、共通部分の「青葉」を理由に
別案件をhard scopeへ追加しない。

## 3. CandidateQueryPath

曖昧性ごとに独立した候補経路を作る。検索前の段階で`primary`を決めない。

```yaml
candidate_query_path:
  branch_id: null
  parent_question_id: null
  candidate_intent: {}
  assumptions: []
  status: pending | completed | failed
  evidence_ids: []
  result: null
  error: null
```

### 論理並列と物理並列

- 候補は常に論理的に並列な枝として保持する。
- ローカルOllamaでは`max_concurrency: 1`とし、枝を順次実行できる。
- APIバックエンドでは同じ契約のまま`bounded_parallel`へ切り替えられる。
- 同時実行数、timeout、retry、停止条件は設定で制御する。
- 1枝の失敗で他の枝を破棄しない。

## 4. IntentGate

Retrievalの前に、各候補が検索可能な契約になっているかを確認する。

### 通過条件

- `operation_graph`が検索計画へ変換できる。
- `target`、`requested_outputs`、`scope`が必要な粒度で解決している。
- 質問の明示内容と矛盾していない。
- 事前に検査できる型違反や`forbidden`違反がない。
- 解決可能な曖昧性が枝に分けられている。

Evidenceの有無はこのゲートでは判定しない。
対話環境では必要に応じて聞き返し、一問完結環境では枝分けまたは`abstain`とする。

## 5. RetrievalPlan

検索計画は元質問の生文字列ではなく、各枝のQuestionIntentContractから作る。

```yaml
retrieval_plan:
  branch_id: null
  must:
    target_types: []
    return_fields: []
    scope_filters: []
    coverage_requirement: none | exhaustive | authoritative_aggregate | authoritative_enumeration
  lexical:
    required_terms: []
    optional_terms: []
  semantic:
    intent_text: null
    concepts: []
  relation_traversal:
    start_nodes: []
    relation_types: []
    target_nodes: []
  structured:
    fields: []
    filters: []
    aggregation: null
    scan_mode: top_k | exhaustive
  deprioritize: []
  exclude: []
```

```text
requested     -> must
not_requested -> deprioritize
forbidden     -> exclude
ambiguity     -> parallel branches
```

必要に応じてlexical、semantic、relation traversal、structuredを併用する。
各チャネルの結果は後段でEvidence単位に統合する。

現行の回答経路で実行可能なモードは`layer1-lexical`と`layer1-hybrid`である。
pure semanticは現時点で評価用とし、`relation_traversal`と`structured_executor`は将来の実装対象とする。
`count`の枝は`exhaustive`または`authoritative_aggregate`、`cardinality: all`の一覧は
`exhaustive`または`authoritative_enumeration`を要求する。Top-k検索だけで計画を終了しない。

### RetrievalHit

SearchUnitは検索入力の永続単位であり、検索出力と呼ばない。
検索出力は次の一時データとする。

```yaml
retrieval_hit:
  branch_id: null
  channel: lexical | semantic | hybrid | relation_traversal | structured
  rank: null
  score: null
  search_unit_id: null
  document_id: null
  source_evidence_ids: []
  locator: {}
  evidence_text: null
```

`search_unit_id`と`source_evidence_ids`はAnswerPlanまで落とさず、Claimから原本への追跡に使う。

## 6. RetrievedEvidenceBundle

```yaml
retrieved_evidence_bundle:
  query_branch_id: null
  evidence_nodes:
    - evidence_id: null
      discovered_by: []
      role: supporting | contradicting | contextual | rejected
      target_match: null
      scope_match: null
      exactness: exact | observed | estimated | unresolved
      source_ref: null
  evidence_edges:
    - from: null
      to: null
      relation: supports | contradicts | duplicates | derived_from
  conflicts: []
  rejected_evidence: []
```

- 同じEvidenceが複数チャネルで見つかっても、複数の独立根拠として数えない。
- 同じtarget、scope、version、unitのEvidenceだけを直接統合する。
- 矛盾は平均化や多数決で消さず`conflicts`へ残す。
- 除外Evidenceにも理由を持たせる。
- 主根拠は必ず原本へ逆追跡できるものにする。
- 確定回答に使うEvidenceはPrimary branch内の`supporting`かつtarget/scope一致に限る。
- `conflicts`または`rejected_evidence`に含まれるEvidenceをProofやClaimへ昇格しない。
- `exact` Claimは`exact` Evidenceだけから作り、`observed / estimated / unresolved`から作らない。

## 7. CandidateEvaluationとPrimaryQueryPath

Evidence取得後に、候補経路を監査する。

### 先に適用する失格条件

```text
explicit_conflict
forbidden_violation
type_mismatch
evidence_path_missing
```

### 通過候補の比較観点

```text
explicit_match
context_support
evidence_support
provenance_quality
exactness
```

総合スコアだけで無理に一つを選ばない。失格条件を通過した候補の中に
一つだけ明確な優位経路がある場合に`resolved`とする。

```text
resolved    : 一意に優位な根拠経路がある
ambiguous   : 同程度の候補が残る
unsupported : 質問からEvidenceまでの根拠経路がない
```

数値重みと選択閾値は現時点で固定せず、valid 30問の失敗類型を確認してから調整する。
必須のoperation、target、scope、requested outputs、allowed claimsが同じで、
非必須の補足情報だけが異なる枝は`equivalent_for_answer`として1つの等価クラスに統合する。
その差分は`required_qualifiers`に残し、同格枝の競合としてPrimaryQueryPathの一意性を壊さない。

## 8. ProofObligation

検索結果が「質問に答えるのに十分か」を、質問型ごとの成立条件で確認する。

```yaml
proof_obligation:
  operation_graph_ref: null
  requirements:
    - requirement_id: null
      operation_ref: null
      output_ref: null
      description: null
      required: true
      status: satisfied | unsatisfied | indeterminate
      evidence_ids: []
  coverage:
    method: none | full_scan | authoritative_aggregate | authoritative_enumeration
    scanned_count: null
    matched_count: null
    exhaustive: false
    evidence_ids: []
  overall:
    status: satisfied | unsatisfied | indeterminate
```

| 質問型 | 必須の証明条件 |
|---|---|
| `count` | scope内の完全集合、または権威ある明示集計がある |
| `list` | 指定件数を満たし、`all`なら全件性を証明できる |
| `retrieve` | 対象と属性が一意に対応する |
| `compare` | 指標、単位、期間、条件が一致するか、変換を証明できる |
| `calculate` | 入力値、式、単位があり、再計算できる |
| `explain` | 資料内に説明関係があり、相関から因果を作っていない |
| `procedure` | 手順、条件、必要な分岐を確認できる |
| `verify` | 対象主張を支持または否定するEvidenceがある |

`cardinality.mode: all`は`full_scan`または`authoritative_enumeration`で、
`exhaustive: true`かつ件数整合を必須とする。`count`は`full_scan`または
`authoritative_aggregate`（列挙を伴う場合は`authoritative_enumeration`）を必須とする。
Top-kや`coverage.method: none`は全件性の証明として扱わない。各必須requested outputは、
対応するoperationとEvidenceを持つrequired proof requirementで個別に覆う。

必須条件が`unsatisfied`、または回答を左右する項目が`indeterminate`なら、
証明は未成立とする。

## 9. AnswerabilityGate

### 通過条件

1. 必須の質問フィールドが解決している。
2. `resolved`のPrimaryQueryPathが1つある。
3. 質問からEvidenceまでの意味付き経路がある。
4. Evidenceの出典、scope、version、exactnessが回答条件を満たす。
5. ProofObligationが`satisfied`である。
6. 適用対象の`forbidden`違反が0件である。

どれか一つでも満たさなければ回答を許可しない。
必須出力に影響する`unresolved`やEvidence衝突、`forbidden`違反は必ず`abstain`とする。
非必須の補足情報だけに不確実性が残る場合は、必須出力のProofObligationが成立していれば
限定付き回答を許可できる。

### 不回答理由コード

```text
intent_ambiguous
target_unknown
scope_unknown
no_supporting_evidence
conflicting_evidence
extraction_unresolved
forbidden_conflict
```

`abstain`は検索が面倒な場合の退避ではない。信頼できる
「質問 -> Evidence -> 回答」経路が成立しない場合の校正済み不回答である。

## 10. AnswerPlan

検索結果全体をそのまま回答AIへ渡さず、使用できるClaimを先に固定する。

```yaml
answer_plan:
  output_plans:
    - output_id: null
      answer_mode: deterministic | grounded_generation
      answer_shape: {}
      allowed_claim_ids: []
  allowed_claims:
    - claim_id: null
      value: null
      unit: null
      exactness: exact | observed | estimated | unresolved
      evidence_ids: []
  required_qualifiers: []
  forbidden_rule_ids: []
```

### deterministic

- 個数、ID一覧、設定値、真偽値、構造化計算結果に使う。
- 計算と並べ替えは検証済みプログラムが実行する。
- LLMに再計算、丸め、値の選択をさせない。

### grounded_generation

- 理由、説明、手順、文章による比較に使う。
- `allowed_claims`以外の事実を追加しない。
- 必要な限定表現は`required_qualifiers`で指定する。

回答AIへ渡す情報は次に限定する。

```text
元質問
QuestionIntentContract
PrimaryQueryPath
成立済みProofObligation
AllowedClaims
必要なEvidence
ForbiddenRules
OutputPlans
```

[ローカル回答生成 v0.1](local-answer-generation.md)のEvidenceに含まれない内容を作らない原則は
維持する。

## 11. OutputValidator

回答生成後に、回答が質問契約、AnswerPlan、Evidenceに従っているかを確認する。

```yaml
output_validation:
  checks:
    operation_match: pass | fail | unknown
    target_match: pass | fail | unknown
    requested_outputs_match: pass | fail | unknown
    per_output:
      - output_id: null
        return_field_match: pass | fail | unknown
        cardinality_match: pass | fail | unknown
        answer_shape_match: pass | fail | unknown
    scope_match: pass | fail | unknown
    allowed_claims_only: pass | fail | unknown
    exactness_match: pass | fail | unknown
    forbidden_violations: []
  status: pass | fail | indeterminate
  action: accept | regenerate | abstain
```

1. 型、件数、形式、単位、値、IDを決定的に検査する。
2. 残る意味上の契約違反を検査する。
3. 失敗時は違反項目だけを渡し、上限付きで再生成する。
4. 上限後も失敗したら`abstain`とする。

Validator自身は回答の事実を修正、追加、再計算しない。

## 12. OperationGraphと複合計算

単純な質問も1ノードのOperationGraphで表す。複合質問は単一の`calculate`ラベルに
圧縮せず、演算DAGと複数の`requested_outputs`として保持する。

```yaml
requested:
  operation_graph:
    operation_graph_id: graph_biomedical_mean_nearest
    nodes:
      - operation_id: op1
        operator: filter
        input_refs: [source_set]
        predicate:
          field: EducationField
          operator: eq
          value: Marketing
        output_ref: marketing_set
      - operation_id: op2
        operator: filter
        input_refs: [marketing_set]
        predicate:
          field: MonthlyIncome
          operator: gt
          value: 10000
        output_ref: filtered_set
      - operation_id: op3
        operator: project
        input_refs: [filtered_set]
        fields: [Age]
        output_ref: age_values
      - operation_id: op4
        operator: mean
        input_refs: [age_values]
        calculation_precision: exact_unrounded
        output_ref: mean_value
      - operation_id: op5
        operator: argmin_all
        input_refs: [filtered_set, mean_value]
        candidate_set_ref: filtered_set
        distance: absolute
        field: Age
        tie_policy: all
        output_ref: nearest_rows
      - operation_id: op6
        operator: project
        input_refs: [nearest_rows]
        fields: [id]
        output_ref: nearest_ids
    edges:
      - {from: op1, to: op2}
      - {from: op2, to: op3}
      - {from: op3, to: op4}
      - {from: op2, to: op5}
      - {from: op4, to: op5}
      - {from: op5, to: op6}
    scope_inheritance:
      default: inherit_previous_output
      reset_requires: explicit_instruction
  requested_outputs:
    - output_id: mean_age
      source_operation_ref: op4
      return_field: value
      display_precision: null
    - output_id: nearest_ids
      source_operation_ref: op6
      return_field: identifier
      cardinality:
        mode: all
```

### 必須検査

- 各演算の入力と出力が参照できる。
- 後続演算の`candidate_set_ref`が明示される。
- 全体集合へ戻るには質問内の明示指示がある。
- 「すべて」の同率候補は`tie_policy: all`で保持する。
- 中間計算は丸め前の値で行う。
- 回答表示の丸めは計算精度と分ける。
- 比較演算子は文字列ではなく型付きenumとして監査する。

## 13. QueryRun

質問ごとの実行全体を、後から再現できる単位で保存する。

```yaml
query_run:
  schema_version: "0.1"
  record_type: query_run
  question_id: null
  original_question: null
  question_intent_contract: {}
  stage_statuses:
    intent: completed | failed | skipped
    context: completed | failed | skipped
    candidate_paths: completed | failed | skipped
    intent_gate: completed | failed | skipped
    retrieval: completed | failed | skipped
    candidate_evaluation: completed | failed | skipped
    proof: completed | failed | skipped
    answerability: completed | failed | skipped
    answer_planning: completed | failed | skipped
    generation: completed | failed | skipped
    output_validation: completed | failed | skipped
  query_context_graph: {}
  candidate_query_paths: []
  intent_gate: {}
  retrieval_runs: []
  retrieval_hits: []
  retrieved_evidence_bundles: []
  candidate_evaluations: []
  primary_query_path: null
  proof_obligation: {}
  answerability_gate: {}
  answer_plan: {}
  forbidden_check_results: []
  output_validation: {}
  final_answer: null
  final_status: accepted | abstained | failed
  runtime_metadata:
    rule_version: "v0.2"
    models:
      - role: intent | embedding | generation | validation
        name: null
        digest: null
    indexes:
      - kind: lexical | semantic | relation | structured
        sha256: null
    backend: local_sequential | api_bounded_parallel
    parallel_config: {}
```

ログには使用したルールversion、モデル名とdigest、索引のSHA-256、
実行バックエンド、並列設定も含める。

Phase 1の`QueryRun` Schemaは、`accepted / abstained / failed`のいずれかへ到達した
完了ログ専用とする。中途checkpointの保存形式はPhase 1に含めず、将来別Schemaで
定義する。完了前に中断した場合も`failed`または`abstained`の完了ログとし、
未実行の後続stageは対応する`stage_statuses.<stage>: skipped`で記録する。
意図分解そのものが失敗した場合も、元質問と`unknown`を使った最小のschema-validな
QuestionIntentContractを残し、`stage_statuses.intent: failed`と`errors`へ失敗理由を記録する。

## 手動検証で固定した要件

正解列は読まず、質問文と共有ドライブの一次資料だけで確認した。
以下の2問は汎用契約の評価fixtureであり、この質問やファイル名に対する専用分岐は作らない。

### 検証1: AYMのPLにおけるフェーズ別タスクID

質問:

> AYMのPLにおいて、探索的分析・仮説整理フェーズに一致するタスクIDをすべて挙げてください。

一次資料:

- `プロジェクト/青葉与信マネジメント株式会社/02.計画/スケジュール.xlsx`
- sheet1の実ヘッダー: A2=`タスクID`、B2=`フェーズ`
- 原本SHA-256: `7145be50436e041aa9fda6117e460052d8ecdd7cd66c481e0abb5a120c807219`

契約と証明:

```yaml
requested:
  target:
    canonical_type: task
  scope:
    filters:
      - field: phase
        operator: eq
        value: 探索的分析・仮説整理
    match_mode: exact_normalized
  operation_graph:
    operation_graph_id: graph_aym_phase_tasks
    nodes:
      - operation_id: op1
        operator: filter
      - operation_id: op2
        operator: project
        field: task_id
    edges:
      - from: op1
        to: op2
  requested_outputs:
    - output_id: task_ids
      source_operation_ref: op2
      return_field: identifier
      cardinality:
        mode: all
coverage:
  scanned_count: 23
  matched_count: 4
  exhaustive: true
allowed_values: [T09, T10, T11, T12]
```

グラフ上の主経路は`Task --in_phase--> Phase`とする。

却下する競合経路:

- `MS3`: Milestone IDであり、要求されたTask IDと型が違う。
- フェーズ名: 絞り込み条件であり、返却項目ではない。
- 会議や関連タスク参照: フェーズの全件性を証明できない。
- 意味的に近い別フェーズのタスク: 構造的な所属関係を上書きできない。
- 別文書: 質問の`PLにおいて`というscopeを超える。

### 検証2: フィルタ、平均、最近傍IDの複合計算

質問:

> 青葉バイオメディカル機器のtrain.csvにおいて、EducationFieldがMarketingかつMonthlyIncomeが10000より大きいデータを抽出し、Ageの平均値を計算してください。その平均値に最も近い年齢のidをすべて答えてください。

一次資料:

- `プロジェクト/株式会社青葉バイオメディカル機器/03.データ/train.csv`
- データ735行（ヘッダーを除く）、33列
- 原本SHA-256: `447e1d1c28a921cc2b83a19001a9b961add4f1ed75463897a563b2ed4b14fc22`
- `プロジェクト/株式会社青葉バイオメディカル機器/04.分析/analysis_project/data/train.csv`は同一SHA-256の重複コピー

同一SHA-256の2ファイルは`duplicates`関係で統合し、2つの独立根拠として数えない。

演算経路:

```text
train.csv
  -> EducationField == Marketing
  -> MonthlyIncome > 10000
  -> filtered_set: 13 rows
  -> sum(Age): 601
  -> mean(Age): 601 / 13 = 46.23076923076923...
  -> argmin_all(abs(Age - exact_mean)) within filtered_set
  -> nearest age: 46
  -> project id
```

許可される出力:

```text
平均値: 601/13（約46.23）
ID: train_0077, train_0216, train_0242, train_0722
```

必須監査:

- `MonthlyIncome > 10000`の`gt`を`gte`に変えない。
- 最近傍探索の`candidate_set_ref`は13行の`filtered_set`とする。
- 全735行に戻って近傍探索しない。
- `601/13`を丸めずに距離計算する。
- 年齢46の同率4件を`tie_policy: all`で返す。
- 平均値とID一覧の2出力を`requested_outputs`に保持する。
- 完全な会社名をhard scopeとし、「青葉」の部分一致で別案件を混ぜない。

このケースでは誤った`gte`や先丸めでも偶然同じ最終IDになる。
したがって、最終値だけでなくOperationGraph自体を検査する。

## 現行パイプラインとの差分

この節は実装済みの機能ではなく、v0.2実装で解決すべき差分を示す。

1. 現行は生の質問文を主に検索し、QuestionIntentContractがない。
2. 現行は検索前の候補経路とEvidence後の主経路を分離していない。
3. 現行のSearchUnitは「WBSタスク一覧」を表ヘッダー候補とし、
   実ヘッダーの「タスクID / フェーズ」という列意味を失うケースがある。
4. 現行の案件別名解決は「青葉」のような弱い部分一致で明示scopeを広げる場合がある。
5. 現行検索だけではCSV全行のfilter / aggregate / argminといった構造計算を表現できない。
6. 現行の`Layer1Index.search()`は豊富な検索結果を旧`Chunk`型へ変換する際に、
   `search_unit_id`と`source_evidence_ids`を回答経路から落とす。
7. ProofObligation、AnswerabilityGate、AllowedClaims、OutputValidatorは本設計の契約として未実装である。

## 実装順序

### Phase 1: 契約とスキーマ

1. `QuestionIntentContract`と`QueryRun`のJSON SchemaをDraft 2020-12で定義する。
2. 質問非依存のEvidenceとの境界をテストする。
3. 決定的に検査できる`forbidden`ルールを先に実装する。

実装先:

- `schemas/question-intent-contract.schema.json`
- `schemas/query-run.schema.json`
- `scripts/validate_query_graph_records.py`
- `tests/test_query_graph_contracts.py`

Phase 1では候補差分と`ambiguity`を結ぶ参照がまだないため、QueryRun内の
`candidate_intent`は埋め込んだ契約の`requested`と同一でなければならない。
Phase 2でambiguity IDと許可差分を追加してから、根拠のある候補枝の差分だけを解禁する。

Phase 1のValidatorは、Claimの参照、型、単位、exactness、件数と出力契約の構造整合までを
決定的に検査する。Claim値と外部Evidence本文の値照合はPhase 4の実行器で、自由文
`final_answer`の意味がAllowedClaimsだけから成ることの検査と決定的Rendererとの値一致は
Phase 5の実OutputValidatorで実装する。Phase 1の
`output_validation.status`はその実行結果を格納する契約であり、意味検査器そのものではない。

Phase 1の検査は次の二層で固定する。両方の通過を契約適合の必須条件とする。

1. JSON Schema: 型、必須項目、enum、ID形式、未知プロパティ禁止を検査する。
2. 決定的semantic validator: ID一意性と参照解決、OperationGraphのDAGと演算子条件、
   `derived_summary`の再計算、forbiddenの実行完全性、Evidence境界を検査する。

どちらかが実行できない、または検査中に例外が起き結果を確定できない場合は、
適合とみなさずfail-closedとする。

### Phase 2: 質問分解と候補経路

1. `requested / not_requested / forbidden / ambiguity`を出力する。（実装済み）
2. QueryContextGraphの優先順位とエンティティ解決を実装する。（実装済み）
3. CandidateQueryPathとIntentGateを実装する。（実装済み）
4. 手動検証2問で契約と候補枝を固定する。（実装済み）

Phase 2は、検索前専用の完了記録`QuestionUnderstandingRun`を出力する。
検索後の根拠・証明・回答を持つ`QueryRun`には流用しない。

```text
question-only input
  -> deterministic supported lane (full match only)
  -> otherwise untrusted IntentDraft
  -> deterministic compiler
  -> QuestionIntentContract
  -> QueryContextGraph
  -> CandidateQueryPath[]
  -> IntentGate
  -> ready_for_retrieval | clarification_required | abstained | failed
```

- モデルはID、forbidden registry、出典参照、派生要約、ゲート結果を決めない。
- supported lane v0.1は、質問全体が一意に一致する次の形だけをモデルより先に処理する。値や組織名ではなく文法とOperationGraphの形で判定する。
  - `FがVに一致するIDをすべて`の`filter -> project`
  - `V + (フェーズ|ステータス|状態|カテゴリ|区分|種別|段階)に一致するIDをすべて`の逆順filter
  - `eqとgtで絞る -> mean -> argmin_all -> IDをすべて`の複合DAG
- supported laneはscope、条件、出力を1つに決められ、質問全体を消費できる場合だけ使う。OR、否定、追加句、重複条件、未対応語彙は黙って切り捨てずモデル候補へ回し、その候補も契約に適合しなければ検索へ渡さない。
- ambiguityのない候補は、supported laneが独立に生成した正本と、scope、predicate、OperationGraphの順序とoption、requested output、`not_requested`が完全に一致する場合だけ`ready_for_retrieval`にする。局所監査に違反すれば`abstained`、違反はないが質問全体との意味同値を証明できなければ`question_equivalence_unproven`で`clarification_required`にする。
- 比較演算子、件数、出力形、scope、候補値は質問中の実在spanへ逆追跡できる場合だけ採用する。
- targetの型はモデルの自己申告ではなく、`TaskID -> task`、`RowID -> row`など、質問中の対象表現に対する決定的語彙対応で確定する。対応外または複数型が同順位になる場合は検索可能とみなさない。
- 明示scope、ファイル名、比較関係、出力、主要演算は、モデル申告とは別に元質問から検出し、Draftが黙って落とした場合も検索へ渡さない。
- `FieldがAまたはB`のような単一フィルタ値のORは`in [A, B]`の1経路とする。scope、演算、または出力の`AまたはB`は解釈候補とし、全候補枝を作れなければ検索へ渡さない。
- 文字通りの一致条件は通常`exact_normalized`、明示的な完全一致指定があるときだけ`exact`とし、モデルに選ばせない。
- 質問中にない候補、未言及の演算、役割を偽装したspan、自由文への回答・Evidence搬送を拒否する。
- 複数候補は全直積を論理枝とし、枝数上限超過時は黙って間引かず`abstained`にする。
- 明示的なambiguityは、各candidateが質問spanとbasisに結びつき、候補の全直積と各枝の差分を完全に保持できる場合に限り、複数の検索候補として残す。検索前にprimaryは決めない。
- `intent_origin`に`supported_lane / supplied_draft / structured_model / compiler_fallback`を保存し、厳密化前の閉じたIntentDraftを`intent_input_sha256`で固定する。由来、モデル実行履歴、`deterministic`の整合もvalidatorが再計算する。
- 現在の実装が決定的に根拠を確認できない表現は、モデルのconfidenceで通さず`clarification_required`または`abstained`にする。

実装先:

- `schemas/question-intent-draft.schema.json`
- `schemas/question-understanding-run.schema.json`
- `scripts/build_question_understanding.py`
- `scripts/validate_query_graph_records.py`
- `tests/test_question_understanding_engine.py`

Phase 2は`rag/main.py`へまだ接続しない。代表質問の契約と不回答境界を
目視確認してから、Phase 3のRetrievalPlan変換・論理並列実行を決める。

#### 2026-08-16実機確認

正解列、過去回答、一次資料を入力せず、上記の代表2問の質問文だけをCLIへ渡した。

| 質問 | 終了状態 | OperationGraph | IntentGate |
|---|---|---|---|
| AYM / PLのタスクID全件 | `ready_for_retrieval` | `filter -> project` | `pass / retrieve` |
| 2条件・平均・最近傍ID | `ready_for_retrieval` | `filter -> filter -> project -> mean -> argmin_all -> project` | `pass / retrieve` |

- `validate_query_graph_records.py`は2レコードとも`status: ok`。
- 両レコードとも`deterministic: true`、`answer_data_used: false`、`past_answers_used: false`。
- supported laneのためintent modelを呼ばず、runtimeのmodelは`question-understanding-compiler:0.1`のvalidation役だけ。
- 同一入力を再実行し、一覧問88個、複合問110個の生成IDがすべて一致。
- 両レコードとも`intent_origin: supported_lane`、`intent_input_sha256`は64桁hexで、検証器が再計算して一致を確認。
- `final_answer`、`retrieval_hits`、`answer_plan`、`primary_query_path`などの回答層キーは0件。
- この確認は質問理解契約の検査であり、検索結果や最終回答の正しさを示すものではない。

### Phase 3: 検索配線

1. RetrievalPlanを現行の語彙、意味、将来のrelation traversal / structured検索へ配線する。
2. 論理並列を実装し、ローカルでは順次実行する。
3. 追跡IDを保持するtyped `RetrievalHit`を実装する。
4. RetrievedEvidenceBundleの重複除去、scope整合、矛盾保存を実装する。
5. CandidateEvaluation後にだけPrimaryQueryPathを選ぶ。

### Phase 4: 証明と回答ゲート

1. 質問型ごとのProofObligationを実装する。
2. 複合計算のOperationGraph実行器を実装する。
3. AnswerabilityGateと理由コードを実装する。
4. AnswerPlan、決定的整形、根拠限定生成を実装する。

### Phase 5: 出力検査と評価

1. OutputValidatorの決定的検査を実装する。
2. 上限付き再生成と`abstain`を実装する。
3. valid 30問を正解漏洩なしに失敗類型別評価する。
4. 検索、抽出、意図、証明、生成、形式のどこで失敗したかをQueryRunから判定する。

## 受け入れ条件

- 質問を生文字列のまま検索入力にせず、回答AIにも質問だけを単独で渡さない。
- `requested / not_requested / forbidden / ambiguity`がすべて記録される。
- 質問の明示scopeが弱い別名や意味類似で広がらない。
- 解釈候補が少なくとも論理的に並列化される。
- Evidence取得前にPrimaryQueryPathを固定しない。
- 質問型ごとのProofObligationを満たさない回答を通さない。
- 事実回答は許可済みClaimだけで作る。
- 回答後に型、件数、scope、exactness、禁止を検査する。
- 必須出力に影響する不明、矛盾、未解決、禁止衝突は必ず`abstain`にする。
- 限定付き回答は、必須出力の証明が成立し、不確実性が非必須の補足情報に限られる場合だけ許可する。
- 両手動検証の契約、経路、中間演算、除外理由をQueryRunで再現できる。
- すべてのClaimからEvidenceと原本位置へ逆追跡できる。

## 未固定事項

次は実装とvalid 30問の失敗分析を行う前に数値を固定しない。

- CandidateEvaluationの数値重みとPrimaryQueryPath選択閾値
- API利用時の最大並列数
- 枝ごとのtimeout、retry、停止条件
- 回答再生成の上限回数
- 計算結果の標準表示桁数
- 対話環境で聞き返す条件と、一問完結環境で`abstain`する条件
- OutputValidatorの意味検査を行うルールまたはモデル
- `observed`を回答に使える質問型と必須の限定表現

## 関連設計

- [中間データ共通構造 v0.1](intermediate-data-schema.md)
- [検索用派生層 v0.2](search-derived-layer.md)
- [Layer 1 v1 実行・検証手順](layer1-v1-runbook.md)
- [Layer 1 v1 完了報告](layer1-v1-completion-report.md)
- [検索評価設計 v0.1](retrieval-evaluation.md)
- [APIキー不要のローカル意味検索 v0.1](local-semantic-search.md)
- [ローカル回答生成 v0.1](local-answer-generation.md)
- [逐次マルチモーダル画像理解 v0.1](sequential-multimodal-orchestration.md)
