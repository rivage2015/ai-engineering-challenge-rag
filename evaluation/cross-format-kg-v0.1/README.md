# Cross-format Knowledge Graph v0.1

DOCX、XLSX、PPTX、PDFに分かれた合成事実を、単一文書検索ではなく、検証済みの
semantic Edgeを通るKnowledge Graphとして回答に使えたかを判定する評価契約です。
実在人物・顧客・案件の情報は含みません。fixture生成、Phase 1 baseline、評価専用の
semantic Edge構築とPhase 2 E2E回答評価まで実行済みです。Phase 2はまだ
macOSローカル検索アプリの本番経路には接続していません。

既存の`evaluation/general-memory-v0.1/`は比較用の基準としてそのまま残します。

## ファイル

- `fixture-spec.json`: corpusの事実分割、入出力境界、観測項目、合格条件
- `corpus-manifest.json`: 固定した5入力のsize、SHA-256、source-set hash
- `cases.jsonl`: 既存の`evaluate_general_memory_shadow.py`で読めるPhase 1検索ケース
- `gold/expected-graph.jsonl`: corpusから生成されるべきNode/semantic Edgeの正解
- `gold/qa-cases.jsonl`: 回答、HOLD、実際のグラフ利用を判定するE2E正解
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
問い合わせず、限定した担当・時点・版差分の質問を決定論的にグラフ探索しています。
したがって「この合成5文書ではグラフを実際に使って正答できた」ことは示しますが、
任意の文書・任意の質問へ一般化できたことや、本番アプリへの統合完了はまだ示しません。

## offline / anti-hardcoding / rollback

- **Offline:** corpus生成後のbuild、回答、評価はネットワーク無効で実行し、cloud/API fallbackを禁止する。
  outbound attemptが1回でもあれば不合格にする。
- **Anti-hardcoding:** ファイル順序の変更、同型fixtureのID・人名・日付・ファイル名置換、質問の言い換えでも
  同じ関係構造から回答できることをpromotion前に確認する。元の固有値や質問全文を条件分岐へ埋め込まない。
  置換したcorpusで答えも追随し、旧答えを返した場合は不合格にする。
- **Rollback:** 新グラフは別snapshotへ原子的にpublishする。gate失敗時は
  `cross_document_semantic_graph=false`へ戻し、直前の正常snapshotを保持する。cross-document質問を
  flat検索の推測で埋めず、`graph_feature_rolled_back`として`HOLD`する。
