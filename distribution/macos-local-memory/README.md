# Local Memory Search macOS package

> **開発マイルストーン:** Cross-document Semantic Graph — Independent Edge audit v0.4 (shadow-only)
>
> **配布状態:** source integrated / current DMG is still Storage-only v0.2

非技術者向けの未署名macOS試作パッケージです。GitHubの操作は不要です。

## ビルド

```bash
./distribution/macos-local-memory/build/build_package.sh
```

生成物:

- `deliverables/Local-Memory-Search-macOS-unsigned.dmg`
- `deliverables/Local-Memory-Search-macOS-unsigned.zip`
- `deliverables/Local-Memory-Search-macOS-unsigned.sha256.txt`

DMGにはユーザーデータ、既存索引、回答ログ、モデル本体を含めません。PaddleOCRについても、workerと固定版ロック・モデル照合manifestのみを含み、Python仮想環境、wheel、cache、モデルバイナリは同梱しません。

PaddleOCRを使うには、固定されたPython 3.12環境と照合済みモデルの一回限りの別途ローカル導入が必要です。導入されていない場合は、PaddleOCRは自動取得せず利用不可として停止します。導入後の推論はローカルで実行し、72依存のlock、2モデルのhash、CPU実行設定が一致しなければfail-closedに停止します。workerは暗黙のdownloadを持たず、macOSの`deny network` sandboxとPython socket guardの二重で推論中のIP通信を禁止します。

## 設計上の境界

- 原本は読み取りのみ。
- パス棚卸し→形式・領域別Reader→位置付きEvidence→関係を保つSearchUnit→コンテンツ安全分離→安全索引の順で作る。
- Validatorは処理の目的ではなく、読取りの欠落や不確実性を見つけ、次のReader・再読・留保へ分岐させるために使う。
- XLSXはセル位置と数式をnative構造から読む。画像はApple Visionと、利用可能な場合のTesseractをローカルで切り替える。
- 画像の独立した複数観測の一致は高信頼、単独観測や同一engine内の一致は暫定とする。位置付きOCRが空な場合は導入済みGemmaの座標なし全体文字起こしも暫定で残し、暫定Evidenceだけの確定回答を機械検証で停止する。
- XLSXの数式は式とファイル保存時の値を別Evidenceで保持し、未再計算であることを明記してcell/rowの両検索経路へ渡す。
- 長い抽出結果は全形式共通のsemantic境界で1,600文字以下のexact shardへ置き換え、hashと文字offsetで全文復元を検証する。埋め込みや回答監査でpacketの後半を黙って切らない。
- AIを読取に使ったかはLayer 1 Evidenceのprovenanceから派生し、semantic stateの申告とvalidatorで照合する。
- 初回はReader/security検証後にモデルを取得し、Gemmaが新規取得され画像がある場合だけ、別の空ディレクトリでsemantic/securityを再構築・再検証してから公開する。
- 最終Reader/security世代から従来のsafe-answer indexを先に原子的公開する。その後、`cross_document_semantic_graph_shadow_enabled=true`の場合だけ、同じ世代の`04-semantic-graph-shadow/`へcross-document semantic graphを生成する。候補ディレクトリ内のSQLite・全Evidence/Node/Edge hashに加え、Content Security Gateと全6出力を入力から再生成して照合し、合格後だけディレクトリ単位で確定する。
- shadow graphは`index_path`、検索、回答、最終監査へ渡さず、`used_for_index=false`、`used_for_answers=false`として観測する。空グラフ、契約不一致、timeoutなどはshadowだけを`held`にし、公開済みsafe-answer indexと回答提供を止めない。したがって、この段階では本番データで意味グラフを生成・測定できるが、回答にはまだ利用しない。
- shadow検証合格後、`cross_document_semantic_graph_storage_enabled=true`の場合だけ、先に公開した元safe-answer indexをSQLite backup APIで`05-semantic-answer-index.building/`へコピーし、名前空間を分けた`semantic_graph_*`表に検証済みgraphを保存する。元indexのEvidence・embedding・従来Graphの不変性とContent Security結合を再検査し、合格後だけ`05-semantic-answer-index/`として確定し、CONFIGの`index_path`とstorage登録を切り替える。
- Step 2のsemantic graphは保存-onlyであり、`retrieval_enabled=false`、`used_for_answers=false`を維持する。Step 3では従来の計画・検索・Gemma回答・最終監査が完了した後だけ、実質問を別プロセスのquery candidateへ複製する。新SQLiteの`semantic_graph_nodes`、`semantic_graph_edges`、`semantic_graph_edge_evidence`を1 read transactionで検証・走査し、候補回答、使用Edge、support Evidence、HOLD理由を監査済み回答記録へ後付けする。query candidateの情報は最終監査の入力にしない。
- query candidateは`cross_document_semantic_graph_query_candidate_enabled=false`で即時停止できる。停止時はcandidate runtimeを起動せず、非対象質問ではsemantic SQLiteを開かない。どちらも既存回答経路だけを使う。候補は常に`used_for_answers=false`であり、Step 4aの独立Edge監査が合格してもまだユーザー回答へは昇格しない。
- Step 4aは`cross_document_semantic_graph_edge_audit.py`を別プロセスで実行し、candidateが申告した使用Edgeとsupport Evidenceを信頼せず、保存済みSQLiteから決定論的に再構築して照合する。監査結果もshadow-onlyで、既存の回答本文と最終採否には使わない。`cross_document_semantic_graph_independent_edge_audit_enabled=false`で独立監査だけを即時停止でき、明示した`false`は再構築と中断復旧でも維持する。回答への昇格は次の別ゲートであり、現在も無効のままとする。
- 世代にbuild IDとowner PIDを持たせ、起動時に中断を判定する。未公開の中断世代だけを整理して再実行へ案内し、公開済み世代はreadyに復旧する。
- SQLite safe-answer index schema `0.3`は、検証済みの`graph_nodes`と`graph_edges`、Graph hash、安全partitionを埋め込みと同じ未公開DBへ書き、全検査成功後だけ`graph_status=validated_safe_partition`、`graph_retrieval_enabled=true`として原子的に公開する。prompt-library indexは`schema_only`のまま回答には使わない。
- semantic validatorはSearchUnitとLayer 1 Evidenceから`derived_from`を独立再構築し、完全なfan-inだけを`semantic-lineage-relations.jsonl`へ昇格する。長文shardや未投影binaryを含むfan-inは理由付きで保留する。
- native structural RelationはLayer 1の`parent_evidence_id`と`preceding_heading_evidence_id`から再構築し、IDだけでなくRelation全フィールドと集合の完全一致を要求する。呼び出し側が`structural`や許可済みproducer名を自己申告してもEvidence間Edgeに昇格できない。
- ChartTable containmentは専用のsource-bound再構築contractができるまで`not_explicit`として保留する。producer名だけでverified Edgeにしない。
- Content Security Gateのvalidatorは入力から分類と6成果物を独立再生成し、自己整合した偽の`safe-answer-evidence.jsonl`も拒否する。safe Graphは安全な枝だけを残し、1つでも除外sourceを含むlineage fan-inは全Edgeを原子的に保留する。
- 保留されたderived Evidenceが回答用safe streamに残る場合、Nodeは削除せず`unresolved`と理由を付ける。除外Evidenceをendpoint/supportに持つstructural Relationは付け替えず保留し、全明示Relationを`promoted`または`held`のどちらかに完全分割する。
- 認可済みDocument／Evidenceと、source-bound validatorをprojector自身が再実行して得たnative structural／verified lineage Relationを、索引builderとbootstrapへ接続する。回答v1/v2、Question Evidence Graph、最終監査は同じGraph hash・安全partition・埋め込み空間を再検査し、`unresolved` Evidenceを取得・再挿入・引用できない。回答cacheは、現在のGraphからcanonical回答を完全再構築する契約ができるまでfail-closedで無効化する。呼び出し側が作ったPASS辞書だけではRelationを投入できない。
- 回答は `gemma4:12b`、別コンテキストの最終監査も `gemma4:12b`、埋め込みは `embeddinggemma:latest`。
- 回数・合計質問は、ベクトル検索の前にQuestion Evidence Graphを作る。同じSQLite read snapshotの永続GraphをDocumentから`contains`順方向・`derived_from`逆方向へ実際に辿り、使用したNode hashとRelation IDを質問Graphへ固定する。その上で質問の対象、`SUM`範囲、各行、再集計値、保存値をNode/Edgeで結び、一致したEvidenceを回答実行者へ先頭挿入する。
- 構造化レコード参照は、現在`owner / review_date / unit_cost / seats / budget`の5項目に限定する。質問に明記された項目、一意の対象行、必要なら`Approved / Final / Finalized`状態を確認し、項目ごとにrow・header・value Evidenceをverified explicit `derived_from`で結ぶ。各枝のEvidenceは対応する項目の検索先頭に限定して挿入する。
- 担当者の時点参照は、肯定形の「誰が担当していたか」という1項目質問に限定する。「N年前」を実行時のAsia/Tokyo基準日から日単位で決定し、`question -> time_point -> assignment_period -> record -> field -> value`のQuestion Evidence Graphを構築する。明記された業務名と同一行の担当開始日・担当終了日・担当者セルのlineageを必須とし、両端inclusiveで該当する1行だけを選ぶ。除外した候補期間も`falls_outside`としてGraphと監査に固定する。Excelの日付セルが生成する真夜中のISO datetimeは日付として受理する。期間情報を持つ担当表へ時点なしで質問した場合、期間欠落・重複・逆転、行座標や対象の未解決、担当者が式・未定・不明、未対応の時刻表現、否定・交代・前任・複数項目質問は`hold`にする。汎用の`Status`列は担当期間の有効性と決めつけず、時点判定は検証済み期間で行う。
- 時点付き担当表の現アダプタは、Excel型の`sheet_name + row_index + cell`と検証済み行lineageが対象。座標のない配列・未定義ヘッダー、PDF/DOCX/CSVの表コンテナは推測回答せず`hold`とし、形式ごとのcontainer adapterが次の実装範囲。
- 対象名そのものが`Final / Draft / 最終版 / 旧版`のような状態語1語だけの場合は、版指定と一意に区別できないため`hold`にする。
- 構造化集計がない、範囲が欠ける、暫定読取を含む、保存値と再集計が違う、または複数候補が曖昧な回数質問は `hold`にし、通常検索へ逃がさない。
- 構造化レコード参照で、質問要求の計画漏れ、未対応項目、状態や対象の曖昧性、必須lineage欠落、Graph枝の未使用がある場合も`hold`にし、通常検索へ逃がさない。
- 回答と監査の間で、質問契約・主張グラフ・Evidence参照を決定論的に検証する。`aggregate_count / record_lookup`の最終文面は検証済み分岐から機械的に再構成し、別の回数・担当者・否定表現の追加を監査で拒否する。
- 最終監査はQuestion Evidence Graphの選択・検証EvidenceをSQLiteから再読込し、保存Graph traversalを再構築する。集計では保存値と再集計値、レコード参照ではGraph枝とfield runの1対1、検索先頭挿入、回答値と枝の値の一致、値セルEvidenceの支持を機械検査する。別コンテキスト監査まで含む全ゲートがPASSした場合だけOrchestratorが回答をacceptする。
- 監査完了後のログに、回答コンテキストと最終監査コンテキストの実行役割を別々に記録する。
- WebとOllamaはloopbackに限定。HomebrewのCLI-only Ollamaも検出し、daemon停止時は専用ログ付きで`ollama serve`をloopback起動する。
- 資料内の命令文は命令として実行しない。
- 監査不合格は「わかりません」に停止する。

## Cross-document semantic graph shadow

成功時のshadow成果物は、公開世代内の`04-semantic-graph-shadow/`に次の4ファイルとして残ります。

- `semantic-graph.sqlite3`: 質問非依存の意味グラフ候補
- `semantic-graph-state.json`: Builderの入力hash・logical snapshot・件数
- `semantic-graph-validation.json`: SQLite整合性、全record hash、安全入力との結合を再検査した結果
- `shadow-run-state.json`: `complete`、処理時間、SQLiteサイズ、Node／Edge件数。メインの`state.json`と世代markerにも同じ観測要約を残す

`held`時は候補SQLiteを削除し、理由を記録した`shadow-run-state.json`だけを残します。rollbackは設定の`cross_document_semantic_graph_shadow_enabled`を`false`にして再構築します。設定項目がない既存環境では有効へ移行しますが、明示した`false`は維持します。shadow pathは本番CONFIGへ公開されないため、無効化前に生成済みのSQLiteも回答経路からは到達できません。

shadow中にアプリ自体が終了した場合、次回起動で公開済みsafe-answer indexを保持したまま、固定名の未完成候補だけを削除して`held`へ復旧します。ディレクトリ確定まで完了していた場合は完成shadowを削除せず、その状態を復元します。

safe-answer indexとready状態を先に公開するため、shadowの完了待ちは回答開始を遅らせません。ただし同じMac上で同期観測を続けるため、bootstrap worker全体の終了時間、CPU負荷、世代内ディスク使用量は増えます。

## Cross-document semantic graph storage-only

Step 2が合格すると、同じ公開世代の`05-semantic-answer-index/`に次の2ファイルが残ります。

- `safe-answer-index.sqlite3`: 元safe-answer indexの全内容を保ったまま、検証済み`semantic_graph_*`表を追加した保存用SQLite
- `semantic-answer-index-state.json`: 元index、shadow、Content Security、保存後SQLiteのhash、件数、`storage_only=true`、`retrieval_enabled=false`、`used_for_answers=false`の証明

保存またはCONFIG登録が失敗した場合は、先に公開済みの元`safe-answer-index.sqlite3`をそのまま回答に使います。rollbackは`cross_document_semantic_graph_storage_enabled=false`にして再構築します。明示した`false`は維持され、保存処理もCONFIGのstorage登録も実行しません。元indexは削除せず同世代に保持します。

## Cross-document semantic graph query candidate

Step 3では、validated storage-only登録のDB・state・元indexのパス、hash、snapshot ID、件数、世代を再検査できた場合だけ、既存の最終監査後に実際の質問を`cross_document_semantic_graph_runtime.py`へ複製します。対応済みなのは、時点付き担当者、担当交代、承認済み版変更の3操作です。担当者質問の「5年前」のような正確な単一相対年は、従来回答が記録し最終監査後も一致した`question_reference_date`だけを実行基準日に使い、同じ基準日を候補traceとrun IDへ結合します。「約5年前」「5〜6年前」「5年前から」などの近似・範囲・境界表現と、担当交代／版変更へ質問時点を付ける組合せは、時点を黙って無視せず候補を`HOLD`にします。誤った一点化を避けるため、候補として受理する質問面は`Project名 -> Work名 -> 正確な時点句 -> 担当者質問`、または各非時点操作の限定文法へ固定し、それ以外の日本語表現は推測せず`HOLD`にします。現段階では質問文だけからProjectとWorkを一意に決められる必要があり、「この業務」のような前会話の指示対象を引き継ぐ処理は未実装です。Node／Edge／support Evidenceと各hashを再検査し、必要Edgeを辿った候補結果を回答JSONの`cross_document_semantic_graph_query_candidate`へ記録します。Edge不足、競合、改ざん、基準日の不一致、登録不一致は通常検索で補わず、候補側だけを`HOLD`にします。

これは本番質問上のdual-run観測であり、候補の`answer_text`を画面の回答として表示しません。画面には候補status、使用Edge数、`used_for_answers=false`と独立Edge監査の観測結果だけを表示します。候補・独立監査の各別プロセスはデフォルト30秒上限（設定可能範囲1〜120秒）で、timeout・起動失敗・不正出力は候補側なら`HOLD`、独立監査側なら`REJECT`とし、監査済みの既存回答は変えません。

## Cross-document independent Edge audit

Step 4aはquery candidateの後に、候補と同じ質問・基準日・登録済みstorageを使い、別プロセスで必要Edge、support Evidence、回答値を独立再構築します。candidate申告と完全一致した場合だけ監査を`PASS`とし、Edge不足・競合・改ざん・バインド不一致・監査プロセスの異常は独立監査を`REJECT`にします。候補も既存回答も変更しません。

この段階は**shadow-only**です。独立監査が`PASS`でも`used_for_answers=false`を維持し、既存のGemma回答と最終採否は変更しません。回答への昇格は別の次ゲートで、その実装と評価は未実施です。独立監査だけのrollbackは`cross_document_semantic_graph_independent_edge_audit_enabled=false`です。現在のDMG／ZIPはStep 4aを含む再生成を行っておらず、Storage-only v0.2のままです。

Step 4aの改ざん検出は、CONFIGに登録されたhashとDB・state・元indexの一致を信頼根とします。これら全成果物をローカル管理者が自己整合的に一括書き換える攻撃は現段階の脅威範囲外です。将来、独立監査の合格を回答へ昇格する前に、別保管したroot hashまたは署名済みmanifestを追加する必要があります。
