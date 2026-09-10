# F11a collateral preflight v1 — static preparation only

Task `lms-v1-f11a-notebook-metadata-2026-09-09`。2026-09-09。独立 reviewer の静的 preparation。使用 skill: codex-graph-engineering-adapter / graph-engineering-agentic-audit。same_model_separate_context の手続的分離のみ。正式監査／製品 PASS ではない。

今回編集したのは新規本書と `runs/f11a-collateral-run.v1.py` のみ。製品・既存 tests・gold・過去 logs は変更していない。Python import、構文実行、test 実行、fixture 生成、モデル／network／GUI／installation は行っていない。親の追加 image 6 PASS 等は受領情報であり、本書による再実行結果ではない。

## 結論

root の全文 review 後に実行する最小候補は **10 methods / 4 separate runs**（focused 3、lineage 3、migration 3、security 1）。F05a root v2 の実 worker → F18 v2 → F04 の既存経路を保ち、固定 method selection と F05b の明示 fixture 予算を追加した新 runner を用意した。

origin helper 移動の直接的な非 Notebook 境界は既に固定された image-additional gold の non-Notebook control が担当する。今回の collateral はその補完として、current producer と旧 producer tuple、実 adaptive validation → index projector、code/schema 変更を受けた generation migration を確認する。全 suite・全形式の網羅や新しいモデル品質保証は狙わない。

SmartArt full-pipeline は今回の 16 KiB source 条件を保証できないため **選択しない**。これは test failure や unittest skip ではなく、未実行の scope/resource 保留である。

## 固定した方法と選択理由

| mode / class | exact methods | この変更への関係 |
| --- | --- | --- |
| focused / ImmutableLineageValidationTests | `test_first_creation_and_repeat_read_only_validation`; `test_source_and_reader_failures_preserve_lineage`; `test_projector_rejects_failed_revalidation_even_with_saved_pass` | 現 current Reader を実際に作り、繰返し検証の bytes/inode/mtime 不変、source/semantic 改変時の既存 lineage 保持、index が保存 PASS だけに依存しないこと。 |
| lineage / SemanticLineageRelationTests | `test_unsharded_table_row_promotes_exact_stable_fan_in`; `test_native_section_contains_requires_real_heading_evidence`; `test_full_validator_publishes_only_after_pass` | literal historical Search 0.6.0 / managed extractor 0.10.1 の保持、現 producer から Search/semantic/lineage 生成、実 index projection と捏造関係の拒否。 |
| migration / ReaderGenerationMigrationTests | `test_existing_step6_config_requires_reader_migration_without_mutation`; `test_current_generation_matches_code_processing_and_schema_bytes`; `test_builder_adapter_processing_or_schema_byte_change_requires_migration` | 旧 pointer を改変しない migration、現 processing/schema/producer bytes で current になる正例、Probe/schema/builder/adapter 等の resource hash 変更で migration になる対照。 |
| security / SecurityGraphPartitionTests | `test_partial_exclusion_holds_mixed_fan_in_atomically` | current producer/schema/index pins を経た genuine XLSX 多対一 lineage の安全な部分公開と混在 fan-in の atomic hold、実 safe-answer index と読取り policy。 |

runner の `TARGETS` が上記の exact selection と test source hash を固定し、target 読込直前に hash を照合する。別 suite の discovery、二重選択、method 不在、想定外 import は失敗として残る。未選択 methods や以前の whole-suite 件数を今回の実績に加えない。

## 読取と副作用の確認

- `f05a-root-run.v2.py` と `f18-test-run.v2.py` 全文を読了。四 mode はすべて F04 worker に到達する。lineage/security の builder alias、migration の bootstrap.run と subprocess.run の in-process dispatch は既存のまま。
- `f04a-executor-run.v1.py`、`f05b-fixture-budget.v1.py`、`scripts/run_local_memory_hardening_tests.py` 全文を読了。F04 は専用 `/private/tmp/f04a-fixtures-*` 配下へ tempdir を固定し、code/runtime/schema と owned tmp 以外の open、socket/HTTP/process 呼出しを拒否する。監督役だけが新しい Python worker を起動し、その process group の timeout/log overflow を処理する。既存 user process を止めない。これは OS sandbox の安全性証明ではない。
- shared `test_versioned_safe_index_e2e.py` の module 初期化、setUp、module loader、model stubs、run_cli を全文読了（1–156）。setUp は own tmp CONFIG を作成し、bootstrap の SUPPORT/STATE/decision pointers をそこへ向ける。run_cli は実 CLI parsing/main を呼び、return 0/None を確認する。Reader の metadata/model discovery/Paddle/password discovery は既存 stubs、処理 code identity 自体は実 bytes。
- focused の module/helpers/選択 methods を読了。小さい CSV と inventory、tmp Reader output、保存 output の hash/bytes/metadata を扱う。source と semantic の改変は当該 test の自作 tmp のみ。選択外の chmod/symlink/concurrent cleanup 注入や別 app E2E は起動しない。
- lineage の module 初期化と helper 1–160、選択 methods 162–218、316–612 を読了。二つは literal in-memory records、full-validator は 28-byte CSV、tmp Reader/lineage、`:memory:` SQLite を利用する。SQL 挿入は fixture の semantic Evidence。モデル呼出しは不要。意味上の Oracle は既存 test のままであり、producer-derived ID helper を利用する古い control を新しい independent literal holdout と呼ばない。
- migration 全 265 行を読了。選択された `_current_config` は小型 memo、actual path/version/decision snapshot/semantic/security contract を own tmp に構成する。index は literal sentinel。既存 F18 `CompletedProcess(...,0,...)` stub の前で shared run_cli が実 main の正常終了を要求するため、失敗 CLI を単に 0 に替えて通す経路ではない。選択外の diagnose/UI server method は呼ばない。
- security 全 403 行を読了。3-row XLSX と tiny CSV のみ。actual security builder/validator、SQLite graph projection、index main を呼ぶが embed は test 内の固定 2-vector mock。answer loader は生成した tmp index のみ読む。HTTP answer generation をしない。

## XLSX 保存予算の補足

security の `Workbook.save(source / "mixed-security.xlsx")` は ZIP API を使い、既存 F05b の Path.write_* だけでは source bytes を数えられない。このため新 runner の security mode に限り、保存 transport を次のように扱う。

1. 保存先は guard が固定した tempdir 内、親名 `source`、名前 `mixed-security.xlsx`、未存在の path に限定。
2. **同じ実 `Workbook.save`** を `SourceZipBuffer`（BytesIO）へ実行する。各 write 前に書込み終端が 16,384 bytes を超えないか検査し、超過は AssertionError。削除／短縮／別 Workbook への置換はしない。
3. 完成 ZIP を Path.write_bytes で保存し、runner 自身を F05b.AUTHORS に追加して計数する。CSV と合わせた当該 source root の累計も既存 16 KiB cap に掛かる。

既存 library の `openpyxl/workbook/workbook.py:373–386` と `writer/excel.py:279–295` を読取し、save が file-like を ZipFile へ渡すことを確認した。未実行なので、実際の生成 byte 数や、この環境で確実に成功することはまだ主張しない。cap 超過は fixture/resource failure として保存し、失敗を避けるため上限を黙って緩めない。openpyxl の XML 作業 file は guard 内の tmp に留まるが、F05b は全 library/product file と全 RSS の総量 cap ではない。

この transport 補足は test 期待値・Workbook 内容・製品 parser/validators を mock するものではない。Workbook の default modified timestamp 等は従来同様生成時に決まり、test は実保存された bytes を hash して source contract を作る。

## Runtime / skip / run ID

候補 runtime は既に親が用いた `/Users/takashifukutomi/Documents/ChatGPT/AIエンジニアリングチャレンジ/rag/.venv/bin/python`（Python 3.9）。read-only file inventory で jsonschema、openpyxl、pptx の package files を確認したが、import は試していない。新依存は導入しない。

security の既存 `@unittest.skipUnless(Workbook is not None, ...)` は変更せず、dependency 不足ならその一件は本来の skip のまま残す。監督役は `completed_with_skips` として記録し、新 runner の外側 exit code も 0 にしない。9 個の他 mode 成功＋security skip を 10 PASS にしない。import/loader/syntax error、timeout、log/fixture overflow もすべて元の result/log を保持する。

Root の全文 review 後にのみ、以下の各 mode を **別 worker** で実行できる候補とする。1 mode 当たり 30 s、captured log 1 MiB、explicit test writes 1 MiB、source root 累計 16 KiB。runner は実行対象を増やさない。

| mode | candidate fresh run ID | expected methods |
| --- | --- | --- |
| focused | `f11a-collateral-focused-001` | 3 |
| lineage | `f11a-collateral-lineage-001` | 3 |
| migration | `f11a-collateral-migration-001` | 3 |
| security | `f11a-collateral-security-001` | 1（skip の可能性を分離） |

呼出し形式は `"<上記既存python>" -I -B "<workspace>/design/local-memory-v1-hardening/runs/f11a-collateral-run.v1.py" MODE RUN_ID`。既存 run directory は再利用しない。出力は既存 supervisor が新規 run directory 内にのみ作る。JSON やログの事後修正はしない。

## SmartArt を今回選択しない根拠

`tests/test_local_embedded_visual_pipeline.py:1078–1464` の `test_pptx_smartart_text_and_raw_connections_reach_search` を静的に確認した。Presentation() default template に slide と SmartArt XML を加え、Probe → Search → adaptive validator → security → index projector に渡す実 full-pipeline で、producer/index pin collateral としては有用。一方で source は Path.write_* ではなく Presentation.save / ZipFile.writestr で作られる。

既存 runtime の `pptx/templates/default.pptx` は stat で **34,030 bytes**（SHA 下表）だった。最終生成 PPTX の byte 数を実測したわけではなく、その template size だけから厳密な最終サイズを断定はしない。ただし現行 recipe の最終 source ≤16 KiB を静的に保証できず、今回それを作る実行はしていない。ZIP 生成を現行カウンタに載せずに「16 KiB 以下」と主張しない。

本 runner に SmartArt mode はない。必要なら別契約で bounded 小型 fixture/gold を固定するか、root が明示的に資源条件を再決定してから新 runner を作る。古い test を短縮したり、skip decorator を追加したり、既存 template/assets を変更することは提案しない。現在の 10 methods は SmartArt 完全 pipeline の代用／全形式の受理ではない。

## 固定参照

`runs/` は `design/local-memory-v1-hardening/runs/`。下記は read-only SHA-256 確認。新 runner は全文 174 行を読み戻したが、未実行／未 compile。

| path | SHA-256 |
| --- | --- |
| `runs/f11a-collateral-run.v1.py` | `5a5c78e853aa3b611c67d482d24be82330ec9c3154a88135bb8b6538dd2716d1` |
| `runs/f05a-root-run.v2.py` | `e13a9c6d264a70a47bb35e84c037f97a49ea79513b512936bdd290edef78bfae` |
| `runs/f18-test-run.v2.py` | `16bacec1eca35d00f99ac6f03b4971e79a128ededd98d1e96abae0829c0e1e38` |
| `runs/f04a-executor-run.v1.py` | `7c6953d3e92c46ede699b81690a138fb50e74e51b79fc37d14f9fc44d6c4391b` |
| `runs/f05b-fixture-budget.v1.py` | `f4c4a647997334385fb02edc2b120624cda9b0da95e438164eacdca0c80b24af` |
| `scripts/run_local_memory_hardening_tests.py` | `6b3bde90b6ed649a82325f6b5ad9661756bef7b6e0303fa74c2d045fb26eb4a9` |
| `tests/test_immutable_lineage_validation.py` | `e6f051e4dbf1aea266b7c7cb3f62511c9f499a5d66d11a4f051b223b408a2e32` |
| `tests/test_semantic_lineage_relations.py` | `26f1624c0dcdddacd86f8cf21253247db9f3f330d764c9b9e4cad80f8e41f4d0` |
| `tests/test_security_graph_partition.py` | `ae9d957baacdd46b1225f8e6f10e4909fad153e412e6a622e4aa1bd25cb5be6d` |
| `distribution/macos-local-memory/tests/test_reader_generation_migration.py` | `c0b4774ecbc9e45fa17b0a983c9f9f23c1725001972ecd3026c5f5cf03fe8c0c` |
| `distribution/macos-local-memory/tests/test_versioned_safe_index_e2e.py` | `bd967cb2e4d22aa3985d59514a72e22e42e9e9d03841dbc6b80164a69c9f97ff` |
| `tests/test_local_embedded_visual_pipeline.py` | `892658aa919cad1dd35a7fdc19aba9c8b0ef1b931e83f3137fc6112b5a150508` |
| `rag/.venv/lib/python3.9/site-packages/pptx/templates/default.pptx` | `e10cc9e120961f6bd4074a373c9c80d2a06c497157e8f4972977b7bea83a8f34` |
| `rag/.venv/lib/python3.9/site-packages/openpyxl/workbook/workbook.py` | `a1a12bbd21f5a94a615003ce6530878f6507c8a783aac8f60f270a083720a3b3` |
| `rag/.venv/lib/python3.9/site-packages/openpyxl/writer/excel.py` | `ea2a179f78521c72149e45b6c02c8180f0380a770ee855cbe72192033b2aa7a6` |

製品 source hash は Executor の今後の immutable artifact で固定し、正式 Auditor が独立に確認する。本 preflight はその代用ではない。親 review で runner の不足が見つかった場合も本版は保存し、別版で理由を示す。
