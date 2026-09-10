# F05b independent holdout gold v1 — prepared, not run

Task: `local-memory-v1-f05b-snapshot-complete-selection`。2026-09-09、Auditor `/root/f03a_independent_audit`。

Source: `design/local-memory-v1-hardening/runs/f05b-audit-holdouts.v1.py`。
SHA-256 `15554e5da8649cd71860b33197519dd815141accb97ba46f35b535e900477603`、25,227 bytes、静的 AST で13 test methods。

これは固定公開 API から準備した literal gold であり、実行結果、正式監査、製品 PASS ではない。製品／共有 fixture の import・実行は行っていない。source freeze と guard-ready の別承認前には走らせない。原本を保持し、fixture/API 接続の訂正が必要なら理由・旧hash・加算差分・新gold版を残す。API未準備の TypeError を semantic RED 又は望ましい拒否に数えない。ただし明示的な required keyword 省略だけを狙う下記 A5 の TypeError は post-API の形検査である。

## 固定入力・成功値

- 通常 source は `Guide_ver1.csv`（old/alice）、`Guide_ver2.csv`（current/bob）、`Contact.txt`（independent front desk）。literal normal selected evidence は Contact と Guide_ver2。
- snapshot が初回不在の場合の bytes は正確に `{"schema_version":"1.0","decisions":[]}\n`。expected digest はこの固定 bytes から作る。攻撃後のファイルから期待値を作らない。
- 2-family case だけ `Plan_ver1.csv`（prior/charlie）と `Plan_ver2.csv`（new/dana）を追加。Human の固定選択は `Guide_ver1.csv` と `Plan_ver2.csv`。
- consumer fixture は normative 6 records: 上記3 files、`.env`、`blob.bin`、read_failed `Unseen.csv`。最終 ordered paths は `["Contact.txt","Guide_ver2.csv"]`、selected_file_count は JSON integer `2`。
- exact counts は `{"inventory_unresolved":1,"policy_excluded":1,"selected":3,"unsupported":1,"version_active":1,"version_historical":1,"version_ungrouped":1}`。追加の zero counters は許さない。
- exact selection-derived limitations は `{"unsupported_files":1,"policy_excluded_files":1,"inventory_unresolved_files":1,"historical_version_files_held":1,"version_files_needing_human_review":0}`。canonical JSON で数値型も区別する。

## A — IndependentSnapshotApplicationHoldouts（8 methods）

| ID / method | 固定 oracle |
|---|---|
| A1 `test_capture_exact_present_bytes_retains_two_groups` | 実 app 正常世代から2つの既知グループに固定Human選択を記録する。present JSON を indent=3＋末尾2space/newlineへ整形して shared に置き、次世代 snapshot と shared はその raw bytes の完全一致。2 decisions を保持し、graphの2 groupsはHuman、索引 evidence は Contact/Guide_ver1/Plan_ver2。timestamp/group ID は入力fixtureの識別子として使い、選択の期待値は返された結果から作らない。 |
| A2 `test_capture_rejects_strict_invalid_present_json_before_resolver` | 7 raw payload（非JSON、root配列、不正UTF-8、duplicate decisions key、nested duplicate key、NaN、1e999）が、実Path validate通過後・resolver到達前に拒否される。現在publicationと保存世代全byte-map、攻撃したshared bytesを不変に保つ。resolver到達はExpected ErrorではなくUnexpectedWorkとなる。 |
| A3 `test_capture_rejects_shared_symlink_and_directory` | shared を owned regular payloadへのsymlink、その後directoryにする。どちらもPath validate後・resolver前で拒否。symlink target bytes、空directory、旧世代全体／publicationは不変。実FIFOや外部pathは使わない。 |
| A4 `test_capture_preserves_existing_directory_and_symlink_targets` | 実Path validate直後に新世代の固定snapshot targetへ directory / regular-owned-file向きsymlink / dangling symlink を植える。captureが既存targetを置換せずresolver前に拒否。旧世代全体・publication・backing bytes不変。rootの既存regular-file sentinelとは別の3種。 |
| A5 `test_pipeline_rejects_descriptor_omission_types_and_wrong_generation` | 正常app実行から公開pipelineのactual argsとdescriptorを捕捉する。descriptor省略はTypeError。別generation、byte_count=True、同値float、sha=None、shared path、同じ場所へ正規化可能な`..` path、extra key、generation欠落は最初のcommandへ到達せず拒否。保存世代全体・publication不変。正常descriptorはexact4keys・D0・generation・固定pathと一致。 |
| A6 `test_pipeline_rejects_01_path_symlink_to_other_generation_identical_bytes` | 正常世代の01-pathをowned退避名へ移し、同じbytesを持つ別synthetic generation/01-pathへのsymlinkに置換する。呼出しdescriptorの文字列/generation/digestはD0のまま。公開pipelineはcommand前に拒否し、退避元・symlink targetの全bytesとpublicationを変えない。同digestではgeneration pathの真正性を代用できない静的祖先symlinkケース。全祖先・raceの保証へ拡張しない。 |
| A7 `test_next_d1_generation_preserves_all_d0_generation_bytes` | D0 snapshotと旧世代全file-map、旧indexを固定。sharedにGuide_ver1を選ぶD1を置いてもD0 statusはcurrent、監視中shared openなし。明示次buildは新generation・exact D1 snapshot・Contact/Guide_ver1 evidence。D0 generation全byte-mapと旧indexは成功したD1build後も不変。現在CONFIGがD1になるのは正規動作。 |
| A8 `test_model_ready_shared_change_between_calls_keeps_captured_d0` | model unavailable→availableとimage条件をstubし、実pipeline2回目の直前にsharedをD1（空決定だがD0とは別の整形bytes）へ変更。両descriptorは同一D0 digest/byte_count、snapshotはEMPTYのまま、sharedはD1のまま、model-ready semantic出力。実モデルは使わない。capture関数の内部呼出し回数を証明したとは記述しない。 |

## C — IndependentSnapshotConsumerHoldouts（5 methods）

| ID / method | 固定 oracle |
|---|---|
| C1 `test_reader_opens_each_attested_input_once_and_uses_literal_selection` | Readerの1 invocation中、explicit inventory/graph/decisionそれぞれPython path-openが正確に1回。2回目はsentinel failure。結果は上記literal paths・integer2・exact counts/5limitations。既存rootのcoherent omissionとは別に読取回数と厳密型を確認する。 |
| C2 `test_validator_opens_each_attested_input_once_per_invocation` | 正常Reader＋初期lineage検証の後、通常Validatorの別1 invocationで各3入力openが正確に1回、status PASS、semantic全file-map不変。初期invocationの読取を混ぜない。 |
| C3 `test_reader_wrong_snapshot_expectation_stops_before_source_open` | 正常fixtureと実snapshot D0に対して外部expectedを64文字の0へ変更。ReaderはValueError、元sourceのopenに到達せずsource bytes不変。omitted modeのroot REDを再使用せず、明示snapshot期待値の違いを攻撃する。 |
| C4 `test_strict_count_types_extra_zero_missing_and_inventory_digest_fail` | literal成功を先に確認し、通常Validatorへ6変種: unsupported=1.0、extra version_needs_human_review=0、review limitation=False、state inventory digest=0*64、selected_file_count=2.0、version_ungrouped削除。各回original stateから生成し拒否を要求。other lineage gateと目的比較の到達分離はfreeze後のコード/ログ点検事項として残す。 |
| C5 `test_generic_unversioned_success_is_distinct_from_authority_without_graph` | genuine graphなしReader/Validatorは成功し、ordered manifestはContact/Guide_ver1/Guide_ver2、integer3。graphなしにsnapshot authorityだけを渡すValidatorはValueError。低水準registrationの`legacy_unversioned`正例や真正旧版migrationをこのケースで代替しない。 |

## 解釈を固定する限界

1. 13は**準備したmethod数**であり、成功件数ではない。subTest数・実行件数・失敗種別は後のsupervised result/logから別に記録する。
2. C1/C2は実際のPython path-open計数＋第2open拒否とliteral outputを確認する。途中で異なるbytesを返すstream-level substitution、既存FD、低レベルrelative dir_fd経路まで自動で覆ったとは言わない。fixtureが計数できない実装経路を使うならguard接続を明示訂正し、0回を免除しない。
3. A6は静止した01-path祖先symlinkだけ。既存OS FDや全ancestor/global race、source-root/Human authenticityは範囲外。
4. Runtime shared fixtureのhelperと登録APIが移動中のため、genuine pre-F05b old-generation migrationと明示legacy registrationはこのv1に推測実装しない。既存migration回帰への割当て又は別版fixture準備を要する。
5. 原本root6・controls各gold・executor acceptanceは変更しない。本holdoutの合格だけで契約全体を受理しない。
