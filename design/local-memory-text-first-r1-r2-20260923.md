# 本文・表先行 R1–R2 実行記録

作業ID: LMS-TEXT-FIRST-R1-R2-20260923 / 2026-09-23

## 承認と境界

直前に提案した「資料1冊・4問」の比較に対し、ユーザーが「計画は…良さげ」「一個ずつ」「まずは細かいところから普通のRAGを作ろう」と承認。`local-memory-text-first-refine-plan-20260923.md` の R1–R2 を小さな工程に分けて進める。旧計画の参考扱い、AGENTS.md と controlled-execution-plan の保護条項は変えない。

今回の最初のチェックポイント R2a は Reader の opt-in 本文先行、未読位置の記録、再開分離、従来 adapter への誤投入防止。R2b の coverage/世代契約/質問・監査への接続が終わるまで、検索用出力を公開しない。これは R2 の途中段階であり R1–R2 完了や利用可能とは報告しない。

既存app、CONFIG、本番・試用索引、原本、モデル、既存の回答順・監査は変更しない。commit/push なし。外部送信なし。既存の未コミット差分を保持する。

## 比較対象

- 代表資料: ユーザー指定済みの2026年度業務資料 XLSX 1冊。原本を読取り専用で使用し、成果物は repo の無視対象 `.tmp/local-memory-text-first-20260923/` に分離。
- 4問: バリスタ稼働時間と案内、年齢別料金、飴配りの注意、配膳の基本手順。業務名は比較データだけに置き、Reader/検索コードに埋め込まない。
- 正解要件・根拠は本問比較前に固定し、索引/モデル入力に正解表を混ぜない。
- 過去の速度ログはReader/回答器の版が異なるため、同条件の速度改善率を計算する基準には使わない。
- 準備は before/after 各最大1回、回答は各4問。修正後の追加比較は1巡まで。現段階では Reader の after 抽出1回までとし、合成試験を先に行う。

## R2a の編集集合

- 本カード、親リファイン計画の承認追記（計画本文の条件は変更しない）。
- `scripts/probe_intermediate_records.py`: default方式を変えず、text-first指定で画像のOCR/意味観測を行わず未読所在を記録。対応していない形式は明示的に拒否。
- `scripts/build_intermediate_records.py`: opt-in引数、処理fingerprintへのpolicy結合、再開時の混在防止。
- `schemas/document.schema.json`: 読取policyと未読範囲の厳密な型。
- `scripts/validate_intermediate_records.py`、`scripts/validate_intermediate_records_streaming.py`: 未読coverageと出典の検査。既存schema fallbackでも情報を落とさない。
- `scripts/adapt_layer1_to_local_memory.py`: R2b未完のtext-first世代を拒否。未読ゼロでも拒否し、従来世代だけ現行動作を維持。
- 新規 `tests/test_text_first_reader.py`、`tests/test_text_first_build_contract.py`: 読取内容、未読位置、失敗、再開、拒否境界の試験。
- `.tmp/local-memory-text-first-20260923/` の検証用ファイル/結果。私的本文を追跡ファイルへ保存しない。

## 合格条件と次の接続

1. 本文・セル・表・見出し・出典位置・明示的関係が従来native抽出と一致する。
2. text-firstではOCR・VLM起動0回。画像の所在、原本hash、対応Evidenceを保持。後回しと実際の抽出失敗を区別する。
3. 既存default挙動と安全検査を維持。policyが変わった再開で旧shardを再利用しない。
4. 未読情報を理解しない従来adapterへtext-firstを渡せない。原本/app/index未変更を検証する。
5. 実資料の抽出時間を測る。抽出時間を索引完成時間や回答時間と呼ばない。4問のGemma回答確認が済むまで実用合格にしない。

R2bではこのカードの結果を踏まえ、選択対象全体のcoverage（検索Evidenceゼロの文書も含む）、世代契約、Gemma入力、既存監査/表示への正確な編集集合を事前記載して接続する。R3/R4、追加画像読取UI、配布アプリ反映はここでは開始しない。

## 状態

R2a完了 / R1–R2全体は未完。4問の回答試験、質問側coverage接続、索引公開は未実行。ここで一工程の結果を利用者へ返す。

### 実施結果

- `--reading-policy text_first_v1` を追加。既定 `full` の方式を維持。
- Officeの本文・表、文字PDF、構造化/通常テキストに適用。画像は所在だけ保持してOCR/VLMなし。未確認のnotebook等はこのモードでは明示的に拒否する。
- Documentのpolicy＋pending visual inventoryを厳密なschema/通常validator/streaming fallbackで検査。Evidence ID、文書/画像hash、所在、一覧欠落を照合。
- policyを処理fingerprintへ結合。切替時の再開は旧shardを流用しない。失敗でshardを破棄する際も未読参照を同期し、failedの原因は保持。
- adapterは全text-first世代を拒否。画像0でも通さない。R2b未完成の資料を「全部読めた」として回答へ送らない。

### 実資料1冊の測定（after reader 1回、質問0回）

- 原資料hash: `5b4dd2db65550605a9e1f8f7518701537ede577053641fc691afd5823424e76e`、4,528,786 bytes。前後で不変。
- 20シート、非空セル1,125。元Excelのセル位置/値と全件一致。結合範囲146、その他を含めEvidence1,314／Relation1,314。
- Reader呼出全体 **4.158737秒**。strict schema/lineage検証 **0.466714秒**。索引作成・Gemma回答・最終監査の時間は含めない。
- OCR0回、VLM0回。画像13ファイルの14掲載箇所を追加読取待ちとして保持。失敗文書0、文書statusは未読を含むためpartial。
- Readerはbundled Python3.12/openpyxl経路で実行。そこでjsonschemaがないことが検証段階で判明したため、抽出を繰り返さず既存 `.venv-paddleocr` のstrict validatorで保存済出力を検査しPASS。追加インストールなし。
- 出力と測定: `.tmp/local-memory-text-first-20260923/after-reader/`、`reader-measurement.json`。Git無視対象。元資料内容は追跡ファイルへ保存していない。
- 旧ログとはReader/回答器等の条件が違うため、高速化率は算出していない。

### 検証と残件

- 今回実行の一括回帰 **188テストPASS**（新規28、再開15、generic reader17、PDF11、visual68、adaptive OCR43、Evidence schema6）。期待値の都合のよい変更なし。
- 別途Notebook binding試験に2件失敗あり。担当がHEADのvalidatorコードへメモリ内だけ差し戻した対照でも同じ失敗を再現。今回は修正せず、全テスト成功とは扱わない。
- 通常appと試用appのreader hashは前記録と一致。原本、app、CONFIG、本番/試用索引を未変更。commit/pushなし。
- 注意: `none_pending`は「生成済み画像Evidenceに追加読取待ちがない」の意味で、原本全体を完全読取済みという保証ではない。既存PPTのlayout/master等、未解決位置がwarningに留まるものもある。R2bでnative失敗/非対応も含む別の対象全体coverageへつなぐ必要がある。
- 次の承認済み作業: R2bの正確な編集集合を固め、未読・失敗範囲を選択対象全体→索引世代→回答/既存監査へ結合する。その後に同条件before/afterの4問を実Gemmaで比較。別回答器や監査省略で代用しない。

## 追記: JSON固定保存（LMS-READING-SNAPSHOT-20260923）

直接の承認根拠: ユーザーの「文字をJSONで出力」「データを固定して、後からそれを読みに行く」。既存の抽出JSONLを再読取り・LLM要約せずに単一JSONへ束ね、後段はその固定データを入力とする方向を採用する。

今回の編集対象は本カード、新規 `scripts/freeze_reading_snapshot.py` と `tests/test_reading_snapshot.py`、生成物 `deliverables/local-memory-json-20260923/reading-snapshot.json` のみ。既存Reader/adapter/索引/CONFIG/app、原資料、モデルは変更しない。新しい原資料取込・Gemma試験・commit/pushは0回。

本文・値・位置・書式/構造情報・全Relation・原本hash・読取policy・未読状態をそのまま保存。元buildの完成状態/整合性/厳密schemaを確認し、export前後で入力JSONLが変わっていないか照合。固定JSONにはsnapshot IDと内容hashを付け、上書きを拒否する。loaderは元資料を開かず、保存JSONの内容hash・schema・参照関係を検査して読む。これを原本が現在も最新版であるという保証にしない。

資料が更新されたら別snapshotを作り、既存版を無断上書きしない。後段での新版採用確認は従来のルールを維持する。再出力したJSONLを旧build-stateと組み合わせて完成世代を装わない。text-firstの回答接続が未完であることも維持する。

合格条件: 元JSONLと全レコードの一致、1,125セルと14未読箇所の保持、原資料なしでのloader成功、変更/破損/未完成入力/既存出力への上書き拒否、実資料をGit対象外へ保存。機密内容はテストや計画書へ埋め込まない。

状態: 固定保存・再読込みの工程は完了。R2bの回答接続は未実施。

### JSON固定保存の結果

- `deliverables/local-memory-json-20260923/reading-snapshot.json` に出力。3,435,319 bytes、所有者読取専用 `0400`。既存ファイルへの上書きは拒否し、新版は別ファイルとして保存する。
- snapshot ID: `reading_1b42f78613a360105c2bf0a3e3cfd415713b5b6b90b7d4a4087b63c70ab13ea9`。
- ファイルSHA-256: `c82c8ce1e46a240f53b7a5de819db5ae2552fca660461f27a56268273eec5b8b`。
- Document 1件、Evidence 1,314件、Relation 1,314件。元JSONLと全レコード一致。1,125セル、14未読画像箇所をそのまま保持。単一JSONの再読込み・厳密検査も成功。
- 新規15テスト、text-first関連28テスト、計43テストを主担当が実行してPASS。合成原本を移動してもexport/loadが成功し、元資料を開かないことを確認。変更検知、参照切れ、policy不整合、未完成入力、上書き、回答可能状態への誤昇格を拒否。
- 内容hashは変更検知用であり、抽出内容の意味的正しさ・最新性・改ざん者のいないことの保証ではない。64 MiBを超える単一JSON化は明示的に停止し、既存JSONLは維持する。
- この工程の原資料再抽出・モデル呼出しは0回。Git無視対象であることを確認。原本/app/CONFIG/索引未変更、commit/pushなし。
- 再開地点: R2bで固定JSONを索引・回答経路へ接続する。現時点のJSONは読取り結果であり、回答可能な索引とは扱わない。

## 追記: 固定JSONのアプリ内部接続テスト

ユーザーの「このJSONをアプリに読ませてテストしてみましょう」により、`local-memory-snapshot-app-test-20260923.md` の範囲で分離索引を作り、既存アプリ内部のGemma回答・最終監査を実行した。

- 固定JSON→専用importer→既存SearchUnit/構造関係→安全索引まで成功。初回索引作成21.035秒、準備稼働時間28.548秒。元Excelの再抽出なし、画像14箇所は未読として維持。
- 4問中まず1問を実行し、最終監査込み114.288秒。技術的に処理は完了したが、項目監査の形式契約違反で回答は不合格（insufficient）。残り3問は未実行。
- 同一実行のOllamaログに、通常項目監査だけcontext4096で入力4807→2051等の切り詰めがある。最終監査は8192であり、段階間の容量設定差を確認。必要な内容はJSON/索引と選択済み行Evidenceにあるが、値セル単体選択の課題も別に残る。
- 次案は通常項目監査でも8192を明示する限定修正と再試験。容量変更の承認前には進めない。詳細、残件、再開先は上記カードを参照。
- R1–R2全体は未完。内部接続、実回答合格、GUI受入を区別する。アプリ上書き、原資料変更、commit/pushなし。

### 容量指定の限定比較（承認済み1回、2026-09-23）

上記8192提案への「おｋ」に基づき、通常snapshot項目監査だけ8192を明示。同一JSON・索引・質問を1回再試験した。再抽出や索引更新なし。

- 一括監査入力4807トークンを切り詰めず受信、個別fallback不要。最終監査も8192で正常終了。全体時間は初回114.29秒、今回80.70秒。1回ずつの比較なので恒常的な性能は未評価。
- 回答は時刻を返したが、曜日が欠け、案内のプレースホルダーが残った。内部answered/verifiedでも、人間が事前固定した回答要件は未達。**全文到達は改善、実用回答の合格は未達**と区別する。
- 新規8件を含む関連52テストPASS。今回の承認範囲は完了。検索や回答要件の追加改修、追加実試験、インストール版更新は未実施。詳細と再開候補はsnapshotカードのCR-SNAPSHOT-CONTEXT-8192結果参照。
