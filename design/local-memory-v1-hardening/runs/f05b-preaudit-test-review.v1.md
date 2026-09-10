# F05b bounded preimplementation test / contract review v1

Task: `local-memory-v1-f05b-snapshot-complete-selection`。Reviewer: `/root/f03a_independent_audit`。2026-09-09。

これは **テスト・契約の事前レビュー** であり、正式 Graph Artifact 監査、製品 PASS、実装の不具合判定ではない。製品 5 ファイル・executor の新規 pure test は読まず、テストや製品 import、テスト実行、追加 agent 作成をしていない。読み取り・hash/AST/既存ログの照合だけを行い、この新規文書だけを保存した。製品 freeze 後の正式監査は別途必要。

使用 skill: `codex-graph-engineering-adapter`（変更範囲・ソース・差分・安全性の点検）、`graph-engineering-agentic-audit`（契約、根拠、役割分離、未確認事項の保持）。後者の正式 PASS JSON は今回は発行しない。同一モデルの別コンテキストは手続き上の分離に留まる。

## 要点

原本の 6 ケースは、初回ログにおいて TypeError 等ではなく実際の「未実装／危険な受入」による assertion RED を示している。controls は元の 7 メソッドを変更せず 3 メソッド追加したことを独立 AST 照合できた。ただし現在の root 合計 16 メソッドを、規範契約全体の十分な受入証拠とは扱えない。

正式監査前に特に必要なのは、**登録の graph + snapshot 同時省略、CONFIG に固定された契約からの期待値取得、保存済み世代全体の不変性**を識別できる追加 oracle である。以下は root テスト集合での欠落／弱さであり、未読の executor テストや今後の互換回帰に存在しないとは断定していない。対応先と実行証跡を coverage map に割り当てればよい。元の gold、6 RED、7 controls の原本は保持し、補強は加算・版付きで記録する。

## 固定資料と独立に確認したこと

Workspace: `/Users/takashifukutomi/Documents/ChatGPT/AIエンジニアリングチャレンジ`。以下の `runs/` は `design/local-memory-v1-hardening/runs/` を指す。

| 対象 | SHA-256 |
|---|---|
| runs/f05b-task-contract.v1.md | e136a468b3ae2a16f094b3189788f7e86a4c5bd089458a27f7d963193af8fd1b |
| runs/f05b-contract-clarification.v1.md | e0b4384099759c4e60f42e16c774bdd14e743b5c8043583eea86a5ec2cff9483 |
| runs/f05b-api-preflight.v1.md | 8977281bb66d3e8b93472e2623c2d22094a120b8b768465d83e3a35d454e31da |
| runs/f05b-contract-refinement.v1.md | a6a5c2c6f5326c4847e1f4bcd290143270e651ee4c609eb962f6e5e080dfdeda |
| tests/test_decision_snapshot_e2e.py | 14ad1a5a53f9e5251f0947e20e33980eb5d62ec284a5283cef17d2d22d2ab27c |
| tests/test_decision_snapshot_controls.py（10 methods） | 3036775b63386c4a67dadadab93d495886a31a504fbc740b5a8f53a0a87763e0 |
| runs/f05b-root-gold.v1.md | 20ad6656a686e01fc8a39b9338f86f12fd578153077c53549fe165f642f2510a |
| runs/f05b-root-red-assessment.v1.md | 84d13dc4d2f4cc903556dd730e3ccb3e51aa4565364b6f46b21761be8b39691b |
| runs/f05b-root-controls-gold.v1.md | c5c56acf73a2f80100038250bb67e2ed13be32b62692a56cd6d8e067f68e3eee |
| runs/f05b-root-controls-gold.v2.md | 7376eac1fcc5e8b4e9de06ac386e08514f497543367bf46ec09f12fe0d06ad3e |
| runs/f05b-root-run.v1.py | 7dae3a260d07b88e344c367a28889796cfd5afaebdf417449432b7c9c0f241ae |
| runs/f05b-root-controls-run.v1.py | 2d243ea4329db1287f607909b4fd804942da35ca766c331face204c4805ce55a |
| runs/f04a-executor-run.v1.py | 7c6953d3e92c46ede699b81690a138fb50e74e51b79fc37d14f9fc44d6c4391b |
| scripts/run_local_memory_hardening_tests.py | 6b3bde90b6ed649a82325f6b5ad9661756bef7b6e0303fa74c2d045fb26eb4a9 |
| runs/f05b-root-gold-snapshots.v1.json | 30e4b0d6be4393a7ea4aaa75894f8f792d779ee0f95c523717682c48dff0a7ea |

Contract が過去 proposal の未決事項を解決し、clarification が成功 payload / None の扱い / 小型 capacity test seam を補う順序を確認した。production cap 1 MiB・許容上限 64 MiB は決定済みであり、proposal 末尾の「数値未決」を現時点の未決として再提示しない。

`f05b-root-gold-snapshots.v1.json` の全 3 source string を parse/hash し、app6 は 11,030 bytes / 上記 14ad hash、元 controls7 は 11,589 bytes / `eead732427b0a04e0162503bf6af272c85721071c32cc6099ef269698dea4ccf`、controls10 は 16,780 bytes / 上記 303677 hash と一致した。元 7 test method の AST はすべて現在の 10 method にそのまま含まれる。app6 と controls10 の snapshot は点検対象ファイルの bytes と一致した。

初回 `runs/f05b-root-red-v1/unittest.log`（`e13c9fc6f88ec8a60a7718695884c9f38e683a2f7075c94ca24b3690ff244b5a`）を読み、末尾 `Ran 6 tests ... FAILED (failures=6)` と 6 assertion trace を確認した。result は failed / exit 1 / 6 methods / skips 0 / expected failures 0 / 6,135-byte log / 30 秒・1 MiB 設定。これは既存証跡の読取確認であり、今回の再実行ではない。前製品 hash の再観測や現在動いている製品の監査を代替しない。

## 優先する追加 oracle

### R1 — 登録時の double omission を単独で識別する（必須）

根拠: API proposal の「app registration must require snapshot ... even if ... removes document_version_graph」、contract の登録要件。root controls `58–80` は正常登録 status、`193–210` は path 改変だけで、graph と snapshot を同時に落とすケースはない。

追加の最小組:

1. 正常な新規未公開世代を作り、root が保持する capture descriptor D0 を渡して同じ登録入口が成功する対照。
2. producer-state の `document_version_graph` と snapshot authority を同時に除去し、同じ新規 app 登録入口へ D0 を渡す。独立した trusted descriptor requirement／producer binding 不一致によって拒否し、generic unversioned 契約として登録しない。
3. 呼出し側 descriptor まで省略した変種も拒否する。post-API では required keyword の TypeError を API 形の負例として扱ってよいが、それだけで 2 の意味的境界を証明したことにしない。
4. genuinely unversioned な低水準 fixture は明示 `legacy_unversioned` のみで成功し、producer JSON の削除だけからその mode を選ばない。新 app の登録経路がこの逃げ道を呼ばないことも検出する。

拒否前に元の CONFIG/index/登録契約を hash 保存し、拒否後も不変・新しい current 登録なしを確認する。

### R2 — CONFIG の期待値と攻撃対象から作った自己期待値を区別する（必須）

根拠: proposal の saved generation verification、contract の「Verify CONFIG-bound contract before using its stored snapshot expectation」。controls `116–135` は snapshot 単独の末尾空白／削除であり、旧 graph digest とも不一致になる。この失敗だけでは、実装が candidate snapshot を今 hash して「期待値」として使っていても識別できない。

追加する独立した攻撃:

- D0 登録と CONFIG-bound 契約の hash を保存した後、snapshot D1、graph、producer authority 等の攻撃側自己 hash を整合的に更新する。CONFIG とその登録済み期待値 D0 は不変。saved verification が D1 自己期待値に追従せず拒否する。
- 登録契約自体を D1 に改変し、内部 self-hash があれば再計算するが、CONFIG の契約 hash / generation は旧値のままにする。CONFIG-bound contract 検証で拒否し、その未承認契約の expected digest / path を先に使わない。
- 正常な契約 D0 から実際に attestation へ渡った expected digest が D0 であることを spy / 到達点で確認する。snapshot 自体を先に読む副作用で D1 を expected として生成していないことを read order で検出する。
- trusted generation と別世代の path、同一 bytes の別世代 snapshot を分けて確認する。同一 digest は byte identity であり、世代／由来 identity の証明ではない。

すべての downstream stage を無目的に再署名する必要はないが、狙った trusted-expectation gate に到達する対照と検出点を残す。別の古い stage hash エラーで止まっただけなら、その gate の証拠と限定する。攻撃のため CONFIG の信頼 anchor まで都合よく D1 に更新して受理を期待してはならない。

### R3 — canary と no-reopen の観測点を強くする（必須）

controls `193–210` は登録後の state bytes を直接変更している。そのため登録済み state hash 不一致だけで migration になっても成功する。観測された「この実行では canary 未アクセス」は有用だが、「path string を比較してから filesystem operation」という規範処理へ到達した証拠とは限らない。

新規登録入口など、未登録 state に stale registered hash がない条件で producer canary を渡す追加ケースを置き、正常対照と descriptor 比較への到達を確認する。saved verification の場合は、その狙いの先行 gate が満たされていることを示す。既存の早期 integrity rejection 例は削除しない。

controls `99–105` の共有 D1/corrupt/missing guard は `Path.open` のみ。`os.open` / `io.open` / descriptor-based read はこれを迂回でき、F04a guard は tmp 内共有ファイルの読取を許す。ノーフォロー実装を想定した open/read 計数を追加し、Path API だけの guard を「一切 reopen なし」と一般化しない。canary も `open/stat/resolve` だけでなく、実際の `os.open/stat/lstat/readlink` や descriptor 経路を対象にする。常時失敗する mock にせず、trusted explicit inputs は通す。

### R4 — old generation 全体の保全と genuine legacy migration（必須）

`e2e.assert_preserved:64–67` は CONFIG と旧 SQLite のみ。controls `82–114` は shared 変更中のその 2 ファイルと D0 snapshot を確認するが、D1 build 後は D0 snapshot 以外の旧世代 artifacts を比較しない。controls `116–135` は現行世代の snapshot 欠落であり、pre-F05b producer/contract identity の旧 versioned 世代を独立に作った migration 対照ではない。

追加ケースでは旧世代の graph、inventory、Reader state、manifest、lineage/登録契約、snapshot、index（小型 fixture の保存対象）を byte/hash map にする。shared D1/corrupt/missing の read-only 確認、失敗 capture/登録、成功した次世代 D1 build の後も旧 map が不変であることを検証する。D1 build 後に現在 CONFIG が D1 へ更新されることと、保存した旧世代・旧 index の非改変を混同しない。

旧 versioned 世代に snapshot がない状態で shared D1 が存在しても、明示 migration/rebuild となり、旧世代へ snapshot をコピー／作成／修復しない。genuine legacy unversioned success と別ケースにする。既存 migration/immutable-lineage テストが担当するなら、今回の新 API に対応させた固定 before/delta と実行 method を正式 artifact に割り当てる。

## 既存 oracle の補強・証拠の限界

| 論点 | 現在の証拠・限界 | 加算する確認 |
|---|---|---|
| downstream RED の拒否理由 | e2e `145–155`, `184–196`, `221–224` は広い ValueError / 非空 reason。初回 RED は安全でない到達を識別しているが、GREEN 時は別の権限／lineage mismatch により成功できる余地がある | 正常 authority の同一経路が通る対照、実際に偽造した component の error family、対象 gate 到達 sentinel を追加。初期 F05a gate での拒否を downstream と数えない |
| coherent omission | e2e `165–183` は producer selector のみ変更・復元確認・Contact の存在・短縮 manifest を literal に検査していて強い | helper 移動後も injection が実際に効き、consumer 側では復元済みであることを維持。authority 不足や古い unrelated stage digest による拒否で代用しない |
| literal counts の型 | controls `244–252` の Python `assertEqual` は `True == 1`, `2.0 == 2` を許す | canonical JSON 又は厳密 type 比較で final count、全 count、5 limitations を固定する追加 oracle。既存 original assertions は保持 |
| complete-selection の負例 | controls `257–290` は 8 count/limitation と 3 manifest 変種を検査する。ただし selected_file_count、state source_inventory digest、missing/extra/zero counters は直接攻撃していない | 規範の各字段を列挙し、bool/int/float、欠落／extra、不要 zero counter、selected_file_count を追加。manifest stage hash 以外の先行 integrity 条件でも拒否できるため、目的の reconstruction 到達を識別 |
| resolver と Reader の対象集合差 | 六項目 literal fixture は supported CSV の version set と unsupported blob を含むが、「Reader 非対応でも resolver 候補」の構成を直接示さない | 現行 policy で該当する小型 fixture を先に literal 化し、full inventory を先に version reconstruction へ渡すことを検出。policy を拡張して gold を捏造しない |
| model-ready 二段 | controls `137–153` は 2 回の descriptor 値／path／generation と model-ready 出力位置を比較する | 二段の間に shared を D1 に変えても D0 を維持し、capture が一度であること、hash / byte_count が D0 と一致することを追加。単に二回同じ誤値を返す比較で終わらせない |
| capacity | controls `155–172` は tiny exact boundary 対照があり、invalid config と one-byte-over を扱う。一方例外種別は広い | oversize は `decision_snapshot_too_large`、invalid config は明示 config error を確認し、capture前の停止と source/saved generation 保全を区別。False/null/JSON型の追加 matrix と最大値の設定受理は大きな実ファイルなしで試験 |
| path-only snapshot 定位 | 正常例の location は config/path_graph_path 由来 | app の trusted generation から固定 `01-path/...snapshot.json` を導出していることを別 oracle にする。alternate generation/shared/`..`/symlink/directory、descriptor extra/missing/wrong types を区別 |
| present input exact copy | D1 は record_decision が生成する通常の 1 group payload | 空白・key order を保った複数 group の小型 present payload を用い、全 bytes をコピーして pruning/再serialization しない確認。capture malformed/duplicate/unreadable と初回 genuine absence を区別 |

`snapshot_kwargs:43–46` の fallback は現在ファイルを hash して expected を作るため、今後の負例には使わない。点検した現在の呼出しはすべて明示 expected を渡しており、現負例がこの fallback で無効になっているとの指摘ではない。期待値は攻撃前の capture／検証済み登録契約から固定する。

## 規範 post-API coverage の割当てが必要な残り

root16に見えない以下は未読の executor pure / 互換回帰 / 後の independent holdout へ割り当て、その source hash と method 名を正式 artifact で示す。今回これらを製品の未実装と判定しない。

- attest exact success / FAIL-only payload、detached JSON-native result、`no_decisions` / wrapper-only `explicit_decisions` / `snapshot` の全組合せ、厳密 key/null/type、F05a wrapper 不変。
- graph / inventory / decisions 各一回の read と substitution、raw digest と canonical graph digest の区別。Reader/Validator が検証後に別読みして selection を差し替えないこと。
- graph/state canary を authority にしないこと、projector の lineage/security exact context alternatives、bare PASS 拒否、post-attestation state swap 後も detached binding を projection report / SQLite metadata へ維持すること。
- snapshot exclusive create（既存通常 file だけでなく symlink/dangling link/directory）、input no-follow/regular-only、permission/strict duplicate/nonfinite/malformed、capture limit と attest absolute limit。小型 seam を使い実 FIFO blocking や大ファイルを必要としない設計にする。
- Human がいずれの候補も選べること、追加候補・選択／非選択資料の変更による次回 stale。mixed/year controls と既知 residual は区別して保持。
- 正常 query / final-audit、partial parser semantics、all-held no-supported の旧公開維持、version identity bump に伴う migration、scope内の既存回帰。

## 実行安全性の事前条件

両 root runner は既存 F04a guard を使い、単一 child / 専用 run ID / 30秒 / 1MiBログ / `-I -B` を設定している。supervisor は新規 run directory と started/result を exclusive create する。今回これらを起動していない。

一方 F04a の累積 source counter は tmp 内 `source` 配下の `Path.write_text` だけを数える。root controls は `Path.write_bytes`、決定 JSON、state/manifest JSON を直接書き、現在は inventory の16KiB・D1の8KiB・RED forged graphの64KiBといった個別上限しかコードから確認できない。**明示生成 fixture 全体の 1MiB/process を機械的に保証しているとは言えない**。これは実際の超過を観測した指摘ではない。

最初の controls 実行前に、test-owned serialization/write の累積 budget と各決定／graph cap を加算記録する runner wrapper または限定 fixture helper を用意する。artifact生成物や内部 parser 出力全体のメモリ／ディスク保証へ言い換えない。fixture の継承先 `test_versioned_safe_index_e2e.py` は executor による signature 対応があり得るので、正式実行前にその最終版の import/dispatch side effects を再点検する。今回の root test hash 一致は、動く依存関係の安全性 freeze まで保証しない。

## 次の境界

root が加算 controls と担当対応表を固定し、executor が実装・許可された互換差分を完了した後で全 source を freeze する。正式 auditor はその artifact のみから、安全な独立 rerun / holdout と正式 schema 報告を行う。本レビューの所見を通過扱いへ自動変換せず、実装／試験の不足は次の固定証拠で解決する。最大2回の正式修復枠はまだ開始していない。本レビューだけでは full F05 / V1 / release を受理しない。

## 同日追記 — root controls v3 の加算を確認

上記本文の固定 controls は v2 / `303677...` / 10 methods である。本文作成中に root が 3 methods を追加したため、以下を追記する。本文の観測を差し替えたり、未実行の加算を試験成功へ昇格したりしない。

- v3 source: `tests/test_decision_snapshot_controls.py` SHA-256 `62fec8a6f18c575be5015ddace1af5d1421d0311ca5dfd8e3912720ab8c5eef5`、22,259 bytes、13 methods。app 原本6は `14ad1a5a...` のまま、root 合計19 methodsとなる。
- v3 gold: `runs/f05b-root-controls-gold.v3.md`。snapshot packet v2: `runs/f05b-root-gold-snapshots.v2.json` SHA-256 `b4bee90824407a5ee7e9aa97534c6adca5d295da178ea9959f77d337a334abc8`。
- snapshot 全 source bytes の hash/length、現ファイル一致を確認し、元10メソッドの AST が13メソッドにそのまま含まれることを独立に照合した。追加3メソッド本文も読んだ。製品・テストの実行は今回もしていない。

加算が扱う対象:

1. `test_new_app_registration_rejects_removed_version_binding`（293行）は実 app 登録を intercept し、入ってきた trusted args/kwargs を転送して graph と nested authority を除去する。R1 の実登録 double-omission 用 oracle が追加された。初回正常 build と到達リストがあり、元出版の CONFIG/index を保全する。**未実行**であり、descriptor 自体の省略負例、明示 legacy-unversioned 正例、path-canary 比較への到達例は別に残る。
2. `test_self_consistent_d1_producer_forgery_does_not_replace_registered_d0`（317行）は同じ ver2 を選ぶ legitimate Human D1 により Reader 選択を変えず snapshot/graph/state を更新し、D0 の契約と CONFIG を維持する。R2 の自己整合 producer 置換拒否という外部境界を狙い、非攻撃 semantic/lineage bytes と攻撃後 bytes の非修復まで比較する。一方、登録 state/raw graph hash mismatch でも拒否可能なので、これを expected digest の内部取得元の証明とは解釈しない。D1 **新規 build** 後の旧世代全体不変と genuine 旧版 migration は引き続き別対象。
3. `test_unregistered_contract_change_rejected_before_snapshot_access`（355行）は Python audit-event `open` を監視するため Path.open 限定より広い。未登録変更の拒否と snapshot open 前の停止を狙う。ただし改変箇所が `producer_records.builder.version = synthetic-forged-version` なので、**CONFIG hash を見ず producer identity 不一致だけで migration しても成功できる**。既存例を残し、現行 producer identity・同一 generation・D1 の相互整合を保つ contract/snapshot/graph/state と再計算済み logical hash に対し CONFIG の登録 hash だけ D0 を維持する companion、または CONFIG-bound 検証の到達／先行関係を特定する観測を追加すると区別できる。監視は path引数付き Python open event の範囲であり、既存 FD や OS 全体を証明しない。

### v3 後も正式 freeze までに担当証拠が必要な最小残件

1. **trust / registration:** 上記 producer-identity-valid な CONFIG anchor 変種、検証済み登録契約から D0 を渡す期待値／read-order 観測、descriptor省略、新appにlegacyを推論させない正負対照、new-registration path-canary到達。
2. **世代保全:** 旧世代全 byte-map を shared D1/corrupt/missing、各失敗、新しいD1世代成功後に比較。真正な旧versioned無snapshotのmigration/no-copy/no-repairとlegacy-unversioned成功を分離。
3. **同一読取と伝搬:** attest三入力の一回読取・substitution、Reader/Validatorの後読取禁止、raw/self digest区別、projector exact contexts・barePASS・post-attestation detached binding、モデル二段の間のshared変更／一度のcapture。
4. **完全selection:** strict numeric/types のliteral oracle、selected_file_count/state inventory digest、missing/extra/zero counters、全5limitations、manifest全順序・重複・挿入・欠落、先行条件を満たした対象gate確認、resolver/Reader allowlist差とpartial parserの維持。
5. **snapshot境界:** exact-copy複数group、strict malformed/duplicate/nonfinite、no-follow/regular-only、既存file/symlink/dangling/directory保全、trusted固定path/generation変種、tiny capacity limitの固有理由。shared no-reopen guard は Path.open 以外も対象にする。
6. **実行前安全性:** test-owned JSON/binary fixture の累積1MiBと個別capを機械計数し、更新される継承fixture/dispatchの最終版を副作用点検。既存ROOT runnerの30秒/1MiBログだけを全fixture上限としない。
7. **互換制御:** Human両候補・追加/選択/非選択変更のstale、year/mixedと残存witnessの区別、F05a wrapper/gate、query/final-audit、移行・lineage・security等。executor/既存回帰のどの固定methodが担当するか明記する。

以上は追加実行の担当分けに使うチェックリストであり、全項目を root の同じファイルへ重複実装する要求ではない。正式監査の source freeze までに、当該チェックを識別する固定 oracle と実行証跡を割り当てる。

### 終了時の残件表と root の補足案

root は、正常な契約・世代 bytes をすべて固定したまま caller config の `registration.sha256` だけを別の valid 64-hex に変え、snapshot open 前の hash-mismatch migration を要求する companion を予定している。これは producer identity の変化を伴わず、「CONFIG hash を無視すると current になる」対照を作れるため、上記 v3 例の識別不足に対する小さい補強案となる。new-registration metadata canary も追加予定との連絡を受けた。これら予定版は本レビューでは未読・未実行であり、既存 v3 を置換しない。

| 残件 | 引継ぎ時の状態 | 正式 freeze 時に必要な証拠 |
|---|---|---|
| CONFIG anchor / 新規登録境界 | double omission と producer D1 例は v3 に追加、CONFIG-only hash と新規 canary は root 予定 | 各対象を識別する literal oracle、正常対照、非アクセス／非修復、実行結果 |
| old generation / migration / legacy | root v3 は攻撃外 semantic bytes 保全を追加。D1 新世代後の旧世代全体・真正旧版は別 | 全対象 byte-map、旧versioned欠snapshot migration、shared非読取・no repair、明示legacy正例 |
| same-read / detached / contexts | 規範あり、executor pure/互換テストは今回未読 | 三入力計数とsubstitution、consumer後読取禁止、厳密context、projector state-swap、モデル二段 |
| selection 完全性と型 | root v2 のliteral/変種と原本omissionあり、一部型・字段・到達は追加待ち | 上記残件4のcoverage mapと対応method、全5limitations、partial parser対照 |
| capture / path / capacity | root正常・既存file・tinycapあり、その他境界は未割当て確認 | exact-copy/strictJSON/no-follow/世代path/小型境界・固有理由、actual IO guard |
| guard / fixture budget | 30秒/1MiBログは既存runnerで固定、全明示fixture計数は未確認 | 最終依存副作用点検、累積/個別capの機械計数、専用run IDと失敗保存 |
| 残り互換回帰 | 契約列挙済み、今回実行なし | Human/stale、year/mixed+residual区別、F05a、query/final-audit、lineage/security/migrationの担当固定method |

本件の限定レビューをここで終了する。以後の設計・テスト追加を継続点検する依頼ではなく、次回は別に dispatch される source-freeze 後の正式監査で扱う。
