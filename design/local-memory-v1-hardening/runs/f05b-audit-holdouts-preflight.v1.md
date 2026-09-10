# F05b independent holdout preparation / execution preflight v1

2026-09-09。Task `local-memory-v1-f05b-snapshot-complete-selection`。担当 `/root/f03a_independent_audit`。

## 現在の状態

`f05b-audit-holdouts.v1.py` と matching goldを準備し、stdlib `ast.parse` による静的構文・13methodsの列挙だけを実施した。製品・共有test harness・holdout自体のimport、test実行、subagent追加はゼロ。製品やroot/executorテストは編集していない。これは正式監査／PASS報告ではない。

使用skillは `codex-graph-engineering-adapter` と `graph-engineering-agentic-audit`。前者により公開APIへの限定、所有権・guard・差分保存を扱い、後者により実行前gold、実行者との分離、未確認の保持と正式監査の別dispatchを守った。

Fixed source SHA-256: `15554e5da8649cd71860b33197519dd815141accb97ba46f35b535e900477603`（25,227 bytes）。gold名は `f05b-audit-holdouts-gold.v1.md`。

準拠資料:

| 資料 | SHA-256 |
|---|---|
| f05b-task-contract.v1.md | e136a468b3ae2a16f094b3189788f7e86a4c5bd089458a27f7d963193af8fd1b |
| f05b-contract-clarification.v1.md | e0b4384099759c4e60f42e16c774bdd14e743b5c8043583eea86a5ec2cff9483 |
| f05b-api-preflight.v1.md | 8977281bb66d3e8b93472e2623c2d22094a120b8b768465d83e3a35d454e31da |
| f05b-contract-refinement.v1.md | a6a5c2c6f5326c4847e1f4bcd290143270e651ee4c609eb962f6e5e080dfdeda |

## 起動前の必須条件

1. coherentな製品5ファイル、shared `test_versioned_safe_index_e2e.py`、dispatch、root/executor必要テストをfreezeし、最終source packetを別途受領する。今回の準備中は動く製品を読んで期待値を作っていない。
2. 最終版shared fixtureのside effectsと呼出し接続を読む。使用seamは既存 `VersionedSafeIndexE2E.setUp/doCleanups`、`h.base/source/module/run_cli/bootstrap`、app `build_index`、`run_semantic_pipeline`、`reader_generation_contract_status`。pipeline private引数を推測せず、正常実行からactual positional argsを捕捉してnegativeへ渡す。
3. 新しい専用 `f05b-audit-*` run ID、単一 supervised child、30秒、1MiB combined log、Python3.14 `-I -B`。最初はmethod又は小群単位でよく、13全体を同時に走らせる必要はない。失敗/timeout/budget違反のrunを残す。新しいrunnerをこの準備では作っていない。
4. reviewed F04a network/process/tmp IO guardと、root `f05b-fixture-budget.v1.py` の `enforce()`を両方有効にする。root helperの `AUTHORS` にはこの正確な `runs/f05b-audit-holdouts.v1.py` が既に登録されていることを読取確認した。別名v2へ変えるならrootへAUTHORS追加を依頼する。
5. loaderはguard/budget有効後にholdoutをloadし、**明示的に `GUARDED_RUN_APPROVED = True`** を設定してからsuiteを開始する。setUpはこれがTrueでなければ製品importより前に失敗する。直接 `python holdouts.py` はmainで終了し、実行を許さない。
6. descriptor path/generation identity、exact-context metadata gates、既存helper署名はfreeze後に照合する。fixture/API mismatchはpreflight未完了又はtest errorとして保存し、desired rejectionと数えない。元source/goldを保持して版付き訂正する。

## fixture / IO 上限と範囲

- `write` helperが全ての明示Path byte writeを累積1MiB/processで計数。synthetic sourceはcase累積16KiB、decisionsは8KiB、inventory6records/16KiB、graph64KiB。strict JSONの7 payload、nonregular target3種、descriptor8変種、counter6変種はすべてtiny。
- resolverのfixture graph / Human decision作成では `atomic_json` を一時的にtest-owned bounded writerへ置き換える。app本体の正常production artifact生成はこの置換対象でない。root budget helperと既存tmp guardも併用する。
- old-generation byte-mapは全regular fileを観測するが、4MiBのin-memory observation capを持つ。これはfixture-write1MiBを増やすものでも、product生成物全ディスク/プロセスメモリの保証でもない。
- symlink/directory/rename/unlinkはharnessのowned tmpだけ。既存ユーザーgeneration・shared decisions・CONFIG/indexには一切触れない。symlink targetは同じowned tmp内にしか作らない。FIFO、実network、subprocess/model/GUI、インストール、追加権限、commit/pushは不要。
- audit hooksはactive flagをfinallyで解除する。C1/C2の計数はPath/io/osのPython `open` eventのpath引数を対象とする。relative dir_fdや既存FDを覆えない場合は最終実装に合わせて計数器を正しく接続する必要があり、未観測の0を合格へ変換してはならない。
- A5/A6のinvalid descriptor／static01-path symlinkで最初のcommandに達したら `UnexpectedWork(BaseException)` が伝播する。通常ValueErrorとして隠して合格にしない。

## 残る coverage matrix（v1の13件だけで充足を主張しない）

| 規範対象 | このv1が追加する範囲 | 別途固定・確認が必要な範囲 |
|---|---|---|
| exact capture / strict input | A1 2group整形bytes保持、A2 strict7、A3 shared symlink/dir、A4 existing nonregular3 | duplicate group IDs、permission/read failures、FIFOをblockingさせない小型seam、max-cap固有理由はexecutor/root固定methodへ割当て |
| trusted generation / descriptor | A5 required keywordとshape/path/generation、A6 static01-path symlink同bytes | root新登録canary/double omission/CONFIG anchorの最終gold、他祖先・already-open FD・global racesはこのv1の証明外 |
| immutable old generation | A7 D1成功後のD0全byte-map、A8 二段間shared変更 | genuine pre-F05b identity/missing snapshot migration、明示legacy registration、copy/repair禁止の既存migration fixture接続 |
| same read / selection | C1/C2 各consumerの3入力1openとliteral output、C3 wrong expected前source非open | stream-level substitution、attest成功/FAIL-only shape・detachment、wrapper3modes、relative dir_fd read attributionの最終実装対応 |
| exact counts / manifests | C4 float/extra-zero/missing/False/state inventory hash、canonical literal | root coherent omission/manifest variantsの目的gate到達、resolver-vs-Reader allowlist差、partial parser semantics、他required fieldは担当既存testに対応付け |
| generic/no-decision distinction | C5 true unversioned成功とgraphなしauthority拒否 | generic registrationのexplicit legacy、strict serialized null/key/mode alternativesはexecutor/互換試験を確認 |
| projector / final output | A1/A7 actual index evidence、A8 actual model-ready pipelinesはstub inference | exact lineage/security context gates、bare PASS拒否、post-attestation state swap、detached report/SQLite binding、normal query/final-auditは別required回帰 |
| Human / current policy | A1/A7の固定Human選択 | Human両候補、added/selected/nonselected stale、year/mixed/residual区別、F05a wrapper/gate・policy不変を別regressionで保持 |

正式Artifact/Auditor/Validatorの最大2修復手順は別dispatchから。今回の準備を製品受理や正式監査開始へ読み替えない。

## 準備完了時の追記

読取点検した root budget helper は `f05b-fixture-budget.v1.py` SHA-256 `f4c4a647997334385fb02edc2b120624cda9b0da95e438164eacdca0c80b24af`。wrapperがtest-authored Path writesを1MiBで累積計数し、このholdout sourceの正確なpathをAUTHORSに含む。ROOT6/controlsの実行結果は本準備で再検証していない。

固定EMPTY（末尾LFを含む）のSHA-256は `9f46ff32538b233d97b0145256e52fcf3bdb21706b505d5b0969f6055d6970c6`。これはstdlibでliteral bytesだけをhashした値であり、product snapshotから導出していない。
