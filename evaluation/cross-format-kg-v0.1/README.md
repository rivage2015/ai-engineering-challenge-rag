# Cross-format Knowledge Graph v0.1

DOCX、XLSX、PPTX、PDFに分かれた合成事実を、単一文書検索ではなく、検証済みの
semantic Edgeを通るKnowledge Graphとして回答に使えたかを判定する評価契約です。
実在人物・顧客・案件の情報は含みません。fixture生成、Phase 1 baseline、評価専用の
semantic Edge構築とPhase 2 E2E回答評価まで実行済みです。macOSローカル検索アプリの
ソースには、本番質問を複製して意味グラフを走査するStep 3 query candidate、
必要Edge・support Evidenceを別プロセスで決定論的に再構築するStep 4a独立Edge監査、
それらと別の信頼・回答契約をすべて再検査するStep 5 Answer promotion v0.5まで接続しています。
候補と監査結果は昇格後も`used_for_answers=false`で、全ゲート合格時の昇格recordだけが`true`になります。
ローカルDMG／ZIPもAnswer promotion v0.5として再生成済みです。

既存の`evaluation/general-memory-v0.1/`は比較用の基準としてそのまま残します。

## ファイル

- `fixture-spec.json`: corpusの事実分割、入出力境界、観測項目、合格条件
- `corpus-manifest.json`: 固定した5入力のsize、SHA-256、source-set hash
- `cases.jsonl`: 既存の`evaluate_general_memory_shadow.py`で読めるPhase 1検索ケース
- `gold/expected-graph.jsonl`: corpusから生成されるべきNode/semantic Edgeの正解
- `gold/qa-cases.jsonl`: 回答、HOLD、実際のグラフ利用を判定するE2E正解
- `anti-hardcoding-variant.json`: 別名・別値・別時点の同型corpusへの変換契約
- `gold/anti-hardcoding-qa-cases.jsonl`: 変換後の値と質問言い換えに対する期待値
- `corpus/`: builderが生成した5ファイル。唯一のbuild入力
- `baseline/phase1-current-system.json`: 現行Reader・検索経路の実測要約
- `baseline/phase2-semantic-overlay.json`: 評価専用semantic graph経路の実測要約

Goldのsource referenceは、生成後のEvidence IDを先取りせず、常に
`path + locator + exact_phrase selector`で指定します。`gold_edge_key`はEvaluator内の照合キーであり、
実行時グラフのEdge IDではありません。

## 情報境界

| 段階 | 入力してよいもの | 入力禁止 |
|---|---|---|
| 抽出・索引・グラフ構築 | `corpus/**`だけ | `corpus-manifest.json`、`cases.jsonl`、`fixture-spec.json`、`gold/**`、質問、期待回答 |
| 回答 | freeze/publish済みのcorpus由来artifactと、その時にEvaluatorが渡す質問1件 | `corpus-manifest.json`、`gold/**`、期待回答、gold edge key |
| 評価 | 回答とtraceを受領した後に、このディレクトリのgoldを照合 | なし |

質問は索引とグラフをfreezeした後に初めて回答系へ渡します。Gold、質問文、期待値を
抽出prompt、graph builder、検索用cache key、回答promptの隠しcontextへ混入させてはいけません。

## fixtureの再生成と固定確認

```bash
python3 scripts/build_cross_format_kg_docx_pdf_fixtures.py \
  --out evaluation/cross-format-kg-v0.1/corpus
node scripts/build_cross_format_kg_office_fixtures.mjs \
  --out evaluation/cross-format-kg-v0.1/corpus \
  --preview-dir /tmp/cross-format-kg-v0.1-previews
python3 scripts/validate_cross_format_kg_fixture.py \
  --dataset evaluation/cross-format-kg-v0.1 --validate-corpus
python3 scripts/validate_cross_format_kg_fixture.py \
  --dataset evaluation/cross-format-kg-v0.1 \
  --validate-manifest evaluation/cross-format-kg-v0.1/corpus-manifest.json
```

builderを2つの空ディレクトリで実行し、5ファイルすべてがbyte単位で一致することを確認済みです。
内容を意図して変更した場合だけ、`--write-manifest ... --overwrite`でmanifestを更新します。

## Phase 1: baseline検索

目的は、5形式のcorpusがローカル抽出経路から消えず、質問に必要なsource fileを検索候補へ
出せるかを先に確認することです。corpus生成後に、既存評価器を変更せず次の形で実行できます。

```bash
rag/.venv/bin/python scripts/evaluate_general_memory_shadow.py \
  --dataset evaluation/cross-format-kg-v0.1 \
  --out /tmp/cross-format-kg-v0.1-baseline
```

Phase 1の合格条件は、5ファイルすべてが抽出され、両抽出経路の
`expected_phrase_coverage.all_pass=true`、各ケースの`all_relevant_at_5=1`、
`external_network_used=false`であることです。`hit@1`、MRR、速度、peak RSSは記録しますが、
この小さなfixtureでは比較用の観測値です。

期待される限界があります。既存評価器は検索・抽出のbaselineであり、回答生成もsemantic Edgeの
traversalも実行しません。また5文書に対するtop-k=5なので、`all_relevant_at_5`は品質上限を
示さず、欠落検知のsmoke gateにすぎません。このPhase 1だけで「Knowledge Graphを使った」とは
判定しません。安全隔離fixtureも本セットにはないため、安全性は既存v0.1で継続確認します。

### 2026-09-03の実測

- 4形式・5ファイルは両Reader経路で全件抽出成功
- Layer 1 adapterは期待文字列を全件保持
- 旧Distribution readerはXLSXの日付型セルをExcel serialで公開し、日付の期待文字列gateに不合格
- 実SQLite BM25の`all_relevant_at_5` は3/5。残り2件は質問に現れない社員IDと氏名対応PDFを接続できなかった
- 既存RelationはDistributionが16本、Layer 1が130本生成したが、すべて構造・語彙系で、`ASSIGNED_TO`、`IDENTIFIES_PERSON`、`SUPERSEDES`、`CONTRADICTS`は0本
- semantic Edge traversalと回答生成は未評価。従って現状は`BASELINE_ONLY_NOT_GRAPH_PROOF`

実測値の正規化結果は`baseline/phase1-current-system.json`に固定します。

## Phase 2: graph / E2E gate

Graph buildでは`gold/expected-graph.jsonl`のcanonical tupleとsource referenceを、build完了後に
Evaluatorが照合します。全expected Edgeが`verified`であり、参照先のexact phraseが実ファイルで
解決できなければpublish不可です。DRAFT v1の内容を現行事実へ昇格させず、APPROVED v2がv1を
`supersedes`し、v1 Claimがcurrent Claimと`contradicts`することを別のEdgeとして保持します。

E2Eのaccepted回答は、少なくとも次をすべて満たした場合だけ合格です。

- `gold/qa-cases.jsonl`の期待値とdecisionが一致する
- trace上のdistinct visited documentがケース指定数以上（全acceptedケースで2以上）
- 回答に使ったsemantic Edgeが1本以上あり、すべて`verified`
- traceのEdge tupleとsource referenceが、後段Evaluatorでrequired gold Edgeに一致する
- required Edgeを評価用graph copyから1本除いたcounterfactualでは、同じ断定回答を返さず`HOLD`になる
- 時点を指定しない担当者質問は、open intervalを勝手に「現在」とみなさず`reference_time_required`で`HOLD`する

観測必須項目は`graph_snapshot_id`、question hash、visited node/Edge hash、Edge status、
visited document path、解決済みsource reference、decision、経過時間、peak RSS、外部通信回数です。
GoldのIDをtraceへコピーするのではなく、Evaluatorがcanonical tupleと出典から事後照合します。

実行例です。`--phase1-dir`には、Phase 1が生成した`semantic-documents.jsonl`と
`safe-answer-evidence.jsonl`のあるディレクトリを指定します。

```bash
python3 scripts/evaluate_cross_format_kg_phase2.py \
  --dataset evaluation/cross-format-kg-v0.1 \
  --phase1-dir /tmp/cross-format-kg-v0.1-baseline/layer1-adapter \
  --out /tmp/cross-format-kg-v0.1-phase2
```

### 2026-09-03の実測

- 安全確認済みEvidence 144件から、13 Node・16 semantic Edgeの質問非依存SQLiteを構築
- Gold Edgeは14/14一致。通常5問は4件`ACCEPTED`、時点なし1件`HOLD`
- 必須Edgeを物理的に1本ずつ除いたhash-validな独立SQLite 29個で、29/29件が`HOLD`
- 回答に未使用のEdgeを除くnegative controlは1/1件`ACCEPTED`で、回答・facts・relations・proof Edgeは不変
- 元SQLiteは30回のcounterfactual後もSHA-256不変
- builderとanswererをsocket/DNS遮断下で実行し、外部通信試行は実測0
- 関連回帰テスト48件が全件合格

この合格は`PHASE2_SEMANTIC_GRAPH_PROOF_PASS_EVALUATION_ONLY`です。Gemmaや外部APIには
問い合わせず、限定した担当・時点・版差分の質問を決定論的にグラフ探索しています。実行時候補はProject、Work、正確な時点句、質問核の限定文法に合う質問面だけを受理し、それ以外は意味を推測せず`HOLD`にします。
したがって「この合成5文書ではグラフを実際に使って正答できた」ことは示しますが、
任意の文書・任意の質問へ一般化できたことは示しません。本番アプリのソースは、検証済みgraphのstorage-only保存、Step 3 query candidate、Step 4a独立Edge監査に加え、Step 5の回答昇格まで実装しています。昇格対象は`owner / assignment_change / version_change`の3操作だけです。accepted candidate、独立Edge auditの`PASS`、candidateとauditの結合、最新CONFIG、Keychain世代rootとmanifestで固定した全artifact、実行基準日、回答schemaが全て合格した場合に限り、昇格recordが`used_for_answers=true`となって回答を切り替えます。成功recordはclosed trust receiptと初期・最新・最終CONFIG hashを保存します。実5文書fixtureで3操作をruntime候補から独立監査・trust・昇格まで通し、`version_change`は12 Edge・48の異なるEvidenceを7 Evidenceで全Edge被覆して昇格することを回帰化しています。候補と監査自体は`false`のままです。その他は従来の監査済み回答をdeep-equalで保持し、回数質問「2026年8月に何回稼働したか」の13回は従来経路のままです。

## offline / anti-hardcoding / rollback

- **Offline:** corpus生成後のbuild、回答、評価はネットワーク無効で実行し、cloud/API fallbackを禁止する。
  outbound attemptが1回でもあれば不合格にする。
- **Anti-hardcoding:** ファイル順序の変更、同型fixtureのID・人名・日付・ファイル名置換、質問の言い換えでも
  同じ関係構造から回答できることをpromotion前に確認する。元の固有値や質問全文を条件分岐へ埋め込まない。
  置換したcorpusで答えも追随し、旧答えを返した場合は不合格にする。
- **Rollback:** 新グラフは別snapshotへ原子的にpublishする。本番保存は先に公開済みの元indexを残し、`cross_document_semantic_graph_storage_enabled=false`で無効化できる。gate失敗時は
  `cross_document_semantic_graph_shadow_enabled=false`へ戻し、直前の正常snapshotを保持する。cross-document質問を
  flat検索の推測で埋めず、`graph_feature_rolled_back`として`HOLD`する。Step 3のdual-runだけを止める場合は`cross_document_semantic_graph_query_candidate_enabled=false`とし、既存回答経路はそのまま維持する。
  Step 4aの独立Edge監査だけを止める場合は`cross_document_semantic_graph_independent_edge_audit_enabled=false`とする。Step 5の昇格だけは`cross_document_semantic_graph_answer_promotion_enabled=false`にすると次の質問から停止し、従来の監査済み回答へ戻る。完全停止はshadow・storage・candidate・独立監査の各flagも`false`にする。新規設定の昇格flagは`true`、既存設定でkeyが欠落する場合は明示的な再構築をCTAとし、再構築時に`true`へ移行する。明示的な`false`は再構築・復旧後も保持する。ローカルDMG／ZIPもAnswer promotion v0.5として再生成済みである。
- **Current trust root:** Step 5は世代ごとの非秘密SHA-256をmacOS login Keychainにcreate-onlyで保存し、manifestから保存用SQLite、storage state、元safe-answer indexの全artifactとGraph識別子を再検査する。これは同一ユーザー権限のmalware、管理者、コード改ざん、過去のtrusted世代のreplayは防がない。Keychain root削除機能は未実装で、Application Support削除後もrootは残る。macOS実機のlogin Keychain試験は未実施である。

### anti-hardcoding gateの実行

```bash
rag/.venv/bin/python scripts/evaluate_cross_format_kg_anti_hardcoding.py \
  --dataset evaluation/cross-format-kg-v0.1 \
  --out /tmp/cross-format-kg-v0.1-anti-hardcoding \
  --python rag/.venv/bin/python
```

このゲートは元5ファイルから、列挙順・ファイル名・Project/Work/Employee/Personの値・
有効期間を変えた別corpusをローカルで作り、Layer 1抽出からグラフ構築・回答まで再実行します。
2026-09-04実測は5問中4件`ACCEPTED`、基準日不足1件`HOLD`、旧ORION固有値の
Layer 1・graph・回答への漏洩0、outbound network試行0で`PASS`でした。本ゲートは回答昇格の
必須前提ですが、これ自体は本番回答を昇格せず、appやDMGも変更しません。
