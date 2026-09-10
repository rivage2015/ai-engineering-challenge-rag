# F11a artifact refresh plan v1 — collateral receipts only

Task: `lms-v1-f11a-notebook-metadata-2026-09-09`. 2026-09-09. Executor preparation, not an audit report or acceptance. Skills used: codex-graph-engineering-adapter / graph-engineering-agentic-audit. Product and test files remain frozen. This turn read selected code, existing JSON/logs and SHA-256 only; it did not import products, execute tests/generator, regenerate an artifact, or change any existing file. `runs/` below means `design/local-memory-v1-hardening/runs/`; other paths are repository-relative.

## 1. Current conclusion and unchanged baseline

The draft v1 lacks four newly completed collateral batches. Their literal result is **10 methods: 9 PASS + 1 ERROR, 0 skips**, not 10 PASS. Focused, migration and security batches passed. The lineage batch failed before its first method's intended relation assertions; its other two methods passed. The reviewer is investigating the expected historical/current producer contract. No lineage resolution is asserted here.

The eleven current product hashes were freshly read and all match the independent literals in `runs/f11a-final-artifact-generator.v2.py:28–40`. In particular, adaptive Validator remains `c2587b685d06a1e8d1ec006bb2577be47168a0c14b072a22b3a90878c30563e3`, Search builder remains `55f0284acd6462260ba4962fa77246fb78090aed9b42554146e49723728c20df`, and index remains `c70f36d98012cca29877af72e4d345c30a555e1fef24e34ebc057b4f823e7229`.

Keep these existing files byte-identical, with their existing meaning:

| Preserved item | SHA-256 | Meaning |
| --- | --- | --- |
| `runs/f11a-graph-artifact.v1.json` | `b0b63af872e7b669f791941bb8938e1a813f81e38c37c4f4aeece58f1f4b1843` | Draft, 170 selected sources and 19 runs; not a final audit result. |
| `runs/f11a-source-packet.v1.json` | `b5d9bd12bd109432d9fa61c3b40589115610349bfd9a9ab7977f381efda0ba45` | Matching draft packet, including current eleven products and historical delta targets. |
| `runs/f11a-final-artifact-generator.v1.py` | `2ce23cf4863b9a5539eed9415ccec849fd69f5e678050af23572b16501d7669c` | Original failed generation implementation. |
| `runs/f11a-artifact-generation-attempt-001.log` | `65536dcf7a8f0af75617ae996dcee8521de88ecd113b57179d1f4cecda80bd5e` | Original strict progress-parser failure, not a product test failure. |
| `runs/f11a-final-artifact-generator.v2.py` | `19e9e45ca189f89a3aa28c3e3c6b5059e272605bc30ebb1ce253ab5a0fbe4f2e` | Generator which produced draft v1; not edited by this plan. |

The old draft is a valid historical preparation snapshot, not a newly refreshed artifact. Adding evidence later must not replace its old input identities with newer identities. Original metadata/app/index/image RED and setup failures, all before snapshots, forward/inverse deltas and gold remain in the later packet too.

## 2. Four additional runs and exact coverage

All four use existing `rag/.venv/bin/python`, `-I -B -u`, one worker per run, 30 seconds and 1,048,576 captured-log bytes. Actual start/finish times are UTC on 2026-09-09 between 08:07:15.043921 and 08:07:18.260954. Their original `started.json` and `result.json` contain commands and limits, but **do not themselves contain product input hashes**. A future packet must distinguish a current read/hash from proof of exact bytes at run time; retain root's frozen-product comparison as a separately attributed verification if root supplies a saved receipt.

Proposed run IDs use suffixes `_START`, `_RESULT`, `_LOG` as in the current packet:

| Proposed run ID / directory | Exact methods (original class in runner) | Recorded outcome and bounded scope |
| --- | --- | --- |
| `COLLATERAL_FOCUSED` / `f11a-collateral-focused-001` | `ImmutableLineageValidationTests.test_first_creation_and_repeat_read_only_validation`; `test_source_and_reader_failures_preserve_lineage`; `test_projector_rejects_failed_revalidation_even_with_saved_pass` | 3 PASS; actual current Reader, read-only repeated validation, preservation after source/Reader failure, projector rejects failed revalidation. No whole focused suite claim. |
| `COLLATERAL_LINEAGE` / `f11a-collateral-lineage-001` | `SemanticLineageRelationTests.test_unsharded_table_row_promotes_exact_stable_fan_in`; `test_native_section_contains_requires_real_heading_evidence`; `test_full_validator_publishes_only_after_pass` | First ERROR, second and third PASS; batch `failed`, exit 1. Current lineage compatibility is unresolved. This is not an adversarial semantic RED or three passing controls. |
| `COLLATERAL_MIGRATION` / `f11a-collateral-migration-001` | `ReaderGenerationMigrationTests.test_existing_step6_config_requires_reader_migration_without_mutation`; `test_current_generation_matches_code_processing_and_schema_bytes`; `test_builder_adapter_processing_or_schema_byte_change_requires_migration` | 3 PASS; selected old-generation preservation and current resource identity/migration controls, not the full migration suite. |
| `COLLATERAL_SECURITY` / `f11a-collateral-security-001` | `SecurityGraphPartitionTests.test_partial_exclusion_holds_mixed_fan_in_atomically` | 1 PASS, no dependency skip; genuine synthetic XLSX through security/lineage/index, fixed embedding mock, read-only answer-index policy. No network answer generation or model quality claim. |

Fixture footers are retained literally. Focused: 4,043 explicit bytes / 13 writes, maximum source fixture 84 bytes, 3 source roots. Lineage: 3,560 / 3, maximum 28, 1 root. Migration: 134 / 5, maximum 44, 2 roots. Security: 5,370 / 3, maximum 5,033, 1 root. These are the reviewed explicit-test/source counters, **not all library/product writes, total RSS, or an OS sandbox guarantee**. Security's reviewed runner serializes the original Workbook into a bounded 16 KiB stream and then the counted Path write; it does not truncate or replace fixture content.

The error log points to `tests/test_semantic_lineage_relations.py:168`, then current `validate_adaptive_semantic_graph.py:1291`, ending `lineage_search_unit_provenance_invalid:su_d59a0eeffdd8f7a32a06d66ff2aeb9fe`. Static facts: the test helper's provenance literal is `0.6.0` at line 75; adaptive's current pin is `0.7.0` at line 101 and equality is checked at line 1293; Search builder's version is `0.7.0` at line 18. Contract line 82 fixes Search 0.7.0 and preserves historical tuples, but this plan does **not** resolve whether this old Search fixture or product acceptance rule should change. That decision remains with root/reviewer. Do not broaden an allowlist or edit the literal on this plan's authority.

## 3. Explicit additional source pins

Add these finite paths, not repository discovery or transitive filesystem scans. All hashes below were freshly observed. IDs are proposals; they must be reconciled with the future generator's unique-path check.

| Proposed source ID | Path | SHA-256 |
| --- | --- | --- |
| `COLLATERAL_RUNNER` | `runs/f11a-collateral-run.v1.py` | `5a5c78e853aa3b611c67d482d24be82330ec9c3154a88135bb8b6538dd2716d1` |
| `COLLATERAL_PREFLIGHT` | `runs/f11a-collateral-preflight.v1.md` | `9296f68a5bb939f855e042566993cb95a37fcf1ed8b8dda2b38938ade704c9d4` |
| `COLLATERAL_F05_ROUTE` | `runs/f05a-root-run.v2.py` | `e13a9c6d264a70a47bb35e84c037f97a49ea79513b512936bdd290edef78bfae` |
| `COLLATERAL_F18_ROUTE` | `runs/f18-test-run.v2.py` | `16bacec1eca35d00f99ac6f03b4971e79a128ededd98d1e96abae0829c0e1e38` |
| `FOCUSED_TEST` | `tests/test_immutable_lineage_validation.py` | `e6f051e4dbf1aea266b7c7cb3f62511c9f499a5d66d11a4f051b223b408a2e32` |
| `LINEAGE_TEST_RUN001` | `tests/test_semantic_lineage_relations.py` | `26f1624c0dcdddacd86f8cf21253247db9f3f330d764c9b9e4cad80f8e41f4d0` |
| `MIGRATION_TEST` | `distribution/macos-local-memory/tests/test_reader_generation_migration.py` | `c0b4774ecbc9e45fa17b0a983c9f9f23c1725001972ecd3026c5f5cf03fe8c0c` |
| `SECURITY_TEST` | `tests/test_security_graph_partition.py` | `ae9d957baacdd46b1225f8e6f10e4909fad153e412e6a622e4aa1bd25cb5be6d` |
| `SECURITY_BUILDER` | `distribution/macos-local-memory/engine/content_security_gate.py` | `3d657ee201022b43429ac765f18c4c3067b96c3962d0178097978ecffe90c2ad` |
| `SECURITY_VALIDATOR` | `distribution/macos-local-memory/engine/validate_content_security_gate.py` | `8c64cf146c076321e08b163a101dcf0fa12808bba0756966fc48abec1d0a0201` |
| `ANSWER_READER` | `distribution/macos-local-memory/engine/answer_local_memory.py` | `33f2b25e9d434e00b216be162d3e60dd8d322409cb108d07e26e455f4a1f34b2` |

The final three are direct selected security-test module dependencies (`tests/test_security_graph_partition.py:45–60`), not F11a edited products. Reuse already present `GUARD`, `FIXTURE_BUDGET`, `SUPERVISOR`, `HARNESS`, `BOOTSTRAP`, `READER`, `AVALIDATOR`, `INDEX`, `ADAPTER`, product/schema and Probe helper source IDs; do not give the same path a second ID. The loaded security answer module does not imply its network answer-generation methods were called. Do not include the migration server as an exercised dependency solely because its unselected loader exists.

The 11 additions above plus 12 run files below are the minimum concrete additions identified here. Any future root diagnosis, gate, before snapshot, inverse delta, corrected test/runner or rerun is separately named and pinned only after it exists. Do not invent a final source total or silently expand the 250-file / 2 MiB-per-file / 24 MiB-total / 30-second / 2 MiB-output generator bounds.

| Proposed source ID | Exact path under `runs/` | SHA-256 |
| --- | --- | --- |
| `COLLATERAL_FOCUSED_START` | `f11a-collateral-focused-001/started.json` | `42b3706b236bb7651a3fc771d08202ccc3f4cac5fe306aaab3b0234a95ab5e2a` |
| `COLLATERAL_FOCUSED_RESULT` | `f11a-collateral-focused-001/result.json` | `ad4dfcd19329771bd662ac320a3a5547407a54fd2d960f71ba6c9b8ed9eadb8c` |
| `COLLATERAL_FOCUSED_LOG` | `f11a-collateral-focused-001/unittest.log` | `e78b6061f8dfad7da0680af18163e6976157f9f5c0eda650637664d187c03808` |
| `COLLATERAL_LINEAGE_START` | `f11a-collateral-lineage-001/started.json` | `32f57e4fa487e19ca417859ff63b15d13dcec692d4789fc76560d3a08cc29b3d` |
| `COLLATERAL_LINEAGE_RESULT` | `f11a-collateral-lineage-001/result.json` | `d2fd145407cde83f5ae43a43c6e57e7c3bb23774e86c3dbd08285204edeb0b42` |
| `COLLATERAL_LINEAGE_LOG` | `f11a-collateral-lineage-001/unittest.log` | `f76a721c2110cf1f388479f2edcc6c1337c9f8fcb14773fa56b9d54149f01cd2` |
| `COLLATERAL_MIGRATION_START` | `f11a-collateral-migration-001/started.json` | `208366a36707508d056702c24c95c4bd3cdd02e4e826c7406c6bc8137f1d3dab` |
| `COLLATERAL_MIGRATION_RESULT` | `f11a-collateral-migration-001/result.json` | `3f637d5071183c7240c876fed02bc8af332e19e139fb0bbd02b56f2ecd7b5b2a` |
| `COLLATERAL_MIGRATION_LOG` | `f11a-collateral-migration-001/unittest.log` | `8baf4e5f75850857836a95743f093978776d15542b98591cca6b898c6a0583b2` |
| `COLLATERAL_SECURITY_START` | `f11a-collateral-security-001/started.json` | `5c905c304329f0d8dfc6ac85397c25163cd49b0268af51728dc04a747c7b8ee6` |
| `COLLATERAL_SECURITY_RESULT` | `f11a-collateral-security-001/result.json` | `da02fd6c3aa05d6f56f4e061eb22609e22f62e19136aa5f46921634a16e5452e` |
| `COLLATERAL_SECURITY_LOG` | `f11a-collateral-security-001/unittest.log` | `f65ab176f9aba9feb14f60a47825a91a538519f7d5ed7d763012397df20280a3` |

## 4. Generator changes to propose for root review, not implement now

1. Preserve generator v1, failed attempt, generator v2 and draft pair as historical selected files. Prepare a NEW generator version and NEW artifact/packet version only after root release. Their IDs should not masquerade as original artifacts. Retain all 19 original run entries; add four, not replace any.
2. Separate a run's source epoch/currentness from its recorded outcome and claim role. The v2 implementation at lines 289–291 requires every `current_*` classification to be batch PASS. A truthful current failed lineage batch must instead have explicit expected `status=failed`, `exit_code=1`, `tests=3`, `errors=1`, `skips=0`, `role=unresolved_regression_not_acceptance`; retain the two `ok` method outcomes without turning the whole batch green. Do not evade the check by calling this new current-source failure historical.
3. Preserve all strict checks for current passing runs. Add expected status/footer/error-count consistency for each new run, against independent fixed expectations and immutable logs. Hash, byte count, command/start coherence and explicit bounds remain mandatory. A failed run's inclusion is evidence preservation, not acceptance.
4. Extend exact method parsing for real unittest docstring progress, without modifying logs. Security log line 1 names the method, line 2 is its docstring followed by `... ok`; v2's same-line `(...) ...` expression at line 295 would find zero methods. Parse exact selected method headers before traceback/footer, allowing the separately recorded docstring line, and validate the corresponding terminal `ok`/`ERROR`. Also retain support for original root RED's concatenated progress headers. Do not count traceback `ERROR:` labels, docstring text, or repeated assertion headers as methods. Freeze tiny literal parser controls before any new generation: security multiline one; focused single-line three; lineage three with one ERROR; original root concatenated four; missing/duplicate/mismatched method negative.
5. Extract the collateral runner's literal `TARGETS` by bounded AST, compare its four exact source hashes and method selections with independent literals and each log. Extend selected test AST index with the exact ten methods; the unchanged full files remain pinned. This does not authorize module import or test execution.
6. Do not reinterpret copied current hashes as run-time attestations. Source roles should distinguish current product/dependency/test bytes, historical test snapshot at failed run, historical delta targets and run receipts. If root changes the lineage fixture after diagnosis, first preserve its current `26f1624c...` bytes and delta, then point run001 to that before snapshot and any new run to the new test and runner pins. Do not rewrite run001 to reference a corrected test.

## 5. Graph and required-path refresh

Every change below needs both source/test and original result/log basis IDs; a runner selection or preflight alone is not proof of a successful behavior.

- `N_COMPAT` / `G6_COMPAT`: add migration's three actual current controls with `MIGRATION_TEST`, `COLLATERAL_MIGRATION_RESULT/LOG`, `BOOTSTRAP` and actual product dependencies. Keep existing app4 and static package checks distinct. Do not upgrade to a built-app/package acceptance claim.
- Add a narrow immutable-lineage/safe-projection collateral claim (or extend an existing identity/gate node with the same precision): focused3 and security1 exercise existing guards under current producer bytes. Basis includes the selected tests, full route/guard chain, result/log and security dependencies. The relation is “collateral guards remain exercised under current F11a producer bytes,” not “Notebook metadata has reached final answers.”
- Add an explicit unresolved lineage compatibility node or pending subclaim, with `LINEAGE_TEST_RUN001`, `AVALIDATOR`, `SBUILD`, `CONTRACT` and `COLLATERAL_LINEAGE_RESULT/LOG`. A required collateral-review path must retain its unresolved status until root diagnosis and any authorized correction/rerun close it. The two passing native/full-validator methods are separately described, never used to erase the failing table-row control.
- `N_EVIDENCE`: retain metadata **30**, app **4**, image **3**, legacy-image **3**, additional-image **6**, current-index-only pure **9**, and now collateral **9 PASS + 1 ERROR** as separate batches. Do not aggregate away scope, mock status or partial batch failure.
- `N_RESIDUAL` remains exactly the **3 passing residual witnesses**, not three defended attacks. Metadata body correspondence, complete membership and original image membership remain excluded. F11b propagation through app/shards/index/retrieval/answers remains mandatory open. Security's answer-index policy test does not close F11b.
- Replace the unspecific “broader collateral not listed” pending text with exact tested selection, unresolved lineage, and unexecuted scope: SmartArt full pipeline was not selected under the 16 KiB source bound; existing pure index SmartArt checks are not its substitute. All-format, real-model, GUI, distribution-build, RSS and whole-V1 acceptance remain unclaimed.

## 6. Root decisions and release conditions

Await reviewer's lineage diagnosis and root's contract decision; this plan makes no fixture or product repair recommendation beyond preserving the evidence boundary. If a change is authorized, retain the same F11a task, original correction/repair accounting and every failed run; do not rename the issue to reset a repair budget. Source hashes and rerun requirements must be frozen explicitly before calling a revised packet final.

Before a new generation: root reviews the complete new generator and literal parser controls; confirms explicit source list/bounds, before/inverse and gold preservation, and the lineage disposition; permits a bounded read-only generator invocation. Parent deterministic source/edge/path checks and separate-context formal audit follow a saved artifact. This plan is neither that release nor a formal dispatch. No new artifact was generated and no formal PASS is claimed.
