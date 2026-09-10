# F05b API / snapshot / complete selection preflight

Prepared by `/root/f05a_executor`, 2026-09-09. Read-only proposal, not implementation, a frozen contract, or audit PASS. The only new file from this subtask is this document. F05a remains frozen for its separate audit at artifact `7b7414f3e58a62b6eb34fc71534da11eec51c1dd58c316326ba03e9ab436bf61`; no frozen source was edited, no tests/imports were run, and no original/shared decision data, models, network, GUI or extra agents were used. Adapter and Agentic Audit instructions already read in this task remain applicable.

The earlier F05b snapshot preflight observed historical resolver `c2b98254...`. This proposal was checked against current F05a resolver `4ca6df75ced0c44e7da4364509311aad21f81601ba9d42b88efc1f6a9b157734`, including its strict same-read validator and CLI failure handling. Root refinement `a6a5c2c6f5326c4847e1f4bcd290143270e651ee4c609eb962f6e5e080dfdeda` is the scope input.

## Recommendation: explicit mode on every versioned consumer

Keep the existing standalone resolver `validate(graph, inventory, decisions_path=None)` behavior as an explicit legacy wrapper. Introduce a new internal attestation entry point with a required keyword `decision_mode`; no default and no mode/path/hash inferred from graph or Reader state.

```python
attest(graph_path, inventory_path, *, decision_mode,
       decisions_path=None, expected_decisions_sha256=None)
```

Its exact modes should be:

| Mode | Required caller inputs | Missing decision file | Consumers allowed |
|---|---|---|---|
| `no_decisions` | No decision path/hash; graph decision digest must be null | No file is accessed | Explicit generic standalone no-decision use |
| `explicit_decisions` | Explicit caller path, no external expected digest | Empty/null only for genuine `FileNotFoundError`, preserving F05a | Existing standalone `validate(..., decisions_path)` wrapper only |
| `snapshot` | Explicit caller path and non-null 64-hex capture/registered expected digest | Failure; never empty/fallback | All new app versioned consumers |

For `snapshot`, compare the supplied expected digest against the exact decision bytes parsed by this invocation, in addition to the graph's recorded digest. The caller's digest is the external expectation; do not manufacture it by hashing the candidate snapshot immediately before passing it to attestation. Existing `validate()` returns only status/errors and maps its arguments to `no_decisions` or `explicit_decisions`, preserving all F05a tests. It must use the same attestation result, not read inputs a second time. Existing resolver CLI remains compatible; app validation can use explicit `--decision-mode snapshot --decisions PATH --decisions-sha256 HASH`.

Reader build, adaptive validation and index CLI should add common keywords/flags `version_authority_mode` / `--version-authority-mode`, `version_decisions_path` / `--version-decisions`, and `version_decisions_sha256` / `--version-decisions-sha256`. Consumer choices are only `no_decisions` and `snapshot`; do not expose `explicit_decisions` as a versioned app fallback.

Validate these combinations before source parsing/model work:

- No `version_graph` and no authority fields: existing generic unversioned behavior is retained. Explicit authority fields without graph fail.
- Present `version_graph` with omitted mode: `version_decision_authority_required`, even if graph/state say there are no decisions. This is a deliberate compatibility change for old versioned consumers, which must now state `no_decisions` explicitly or provide a snapshot.
- Mode `no_decisions`: graph required; path/hash arguments forbidden, including explicit null entries in strict contexts. Graph/state cannot turn this into Human authority.
- Mode `snapshot`: graph/path/hash all required; any missing/partial/null/wrong-type input fails. A missing snapshot fails even if shared decisions are available.

The app-specific `run_semantic_pipeline(..., *, decision_snapshot)` must have **no default** for its new keyword, validate the descriptor against its trusted `paths`/generation location, and always emit snapshot mode. Both invocations in `build_index`, including the model-ready branch, pass the same local descriptor variable. Index and registration paths likewise receive that variable; they do not obtain it from producer state. Omission therefore cannot become the generic no-decision mode.

A generic caller that deliberately chooses `no_decisions` has chosen a distinct contract; this cannot authenticate a new app generation. App registration must require snapshot mode from its trusted entry point even if an attacker removes `document_version_graph` from producer state. This requirement must not be conditional only on `state.get('document_version_graph')`. Retain any low-level legacy unversioned contract helper only behind an explicit `legacy_unversioned` argument; new app registration and saved current app status never select it from producer JSON. This closes the double omission of graph plus snapshot at app registration, while preserving genuine generic unversioned tests.

## Same-read attestation result

Recommended successful in-process return shape (names illustrative but freeze exact fields before implementation):

```json
{
  "status": "PASS",
  "errors": [],
  "inventory": {"sha256": "RAW_INVENTORY_DIGEST", "records": []},
  "version": {"groups": [], "dispositions": {}},
  "document_version_graph": {
    "path": "EXPLICIT_CALLER_GRAPH_PATH",
    "sha256": "RAW_GRAPH_DIGEST",
    "graph_sha256": "GRAPH_CANONICAL_SELF_DIGEST",
    "decision_authority": {
      "mode": "snapshot",
      "path": "EXPLICIT_CALLER_SNAPSHOT_PATH",
      "sha256": "CHECKED_EXPECTED_AND_ACTUAL_DIGEST",
      "byte_count": 123
    }
  }
}
```

For `no_decisions`, the authority object is exactly `{"mode":"no_decisions"}`. A failure result carries no usable inventory/groups/dispositions/binding; consumers must not recover them from a failed result. Alternatively throw a typed validation exception internally and let the compatible public wrapper translate it to status/errors. Pick one, do not mix unchecked optional payloads with a bare PASS.

Read graph/inventory/decisions once each into byte snapshots, strictly parse them with the existing F05a helpers, hash those same bytes, and compare the complete reconstructed graph. Return the **reconstructed** groups/dispositions and parsed inventory records, not a later graph read. Produce detached JSON-native values with exact field sets. Do not return graph-controlled path strings as authority. The binding path is constructed from the explicit caller path; the app validates fixed location before invoking this generic entry point.

This separates raw graph file SHA-256 from canonical graph self digest. Both are already used by current consumers and must not be swapped. The `byte_count` is measured from the attested raw decision snapshot, not an assumed production cap. Snapshot reads in different invocations may occur again; each invocation must use its own consistent attested snapshots. This is not an atomic multi-file filesystem snapshot or proof that no transient changes occurred.

## Reader selection and complete manifest/count reconstruction

Current `apply_document_version_policy(selected, graph_path, expected_inventory_sha256=None)` receives a filtered partial inventory and rereads graph/hash independently. Do not extend that weak input into an apparent full inventory proof. Add one shared pure selection helper over the attested inventory records and reconstructed disposition map, reusing current `select_inventory()` and its NFC ordering. Reader and Validator each invoke that helper after their own attestation.

Suggested orchestration is `select_attested_inventory(inventory_path, version_graph, *, authority...) -> {inventory_records, inventory_sha256, selected, selection_counts, document_version_graph}`. For versioned calls, it invokes `attest` once and derives selection entirely from its result. Reader's versioned branch must stop calling `read_jsonl(inventory)` or `sha256_file(inventory)` before/after it to obtain selection or identity. Validator likewise uses those returned records/digests for its existing source inventory checks and all later inventory mappings. Existing public `apply_document_version_policy` may remain for unversioned use; its versioned entry must require full explicit inventory/authority and forward to the new path, or the three direct test callers should migrate to the new helper. Do not silently validate using its partial `selected` list.

Exact reconstruction:

1. Feed the full explicit inventory to resolver reconstruction before Reader-supported filtering. Resolver and Reader allowlists differ; never derive version groups from only Reader-supported files.
2. `select_inventory(attested_records)` returns the existing pre-version selected records and selection counts (`selected`, `unsupported`, `policy_excluded`, `inventory_unresolved`). Keep existing path safety, sensitivity, generated-file rules and NFC sort semantics.
3. Apply the reconstructed version dispositions to that ordered list. Exclude only `historical`/`needs_human_review`; count `version_historical`, `version_needs_human_review`, `version_active`, `version_ungrouped` just as today. An unsupported version candidate participates in resolver decisions but is not counted as a Reader-selected file.
4. Preserve the current meaning of `selection_counts.selected`: it counts pre-version selected files. Merge version counts without rewriting that number to final selection size. Missing zero counters remain absent as in the current Counter representation; compare exact canonical JSON, not boolean/numeric-coercing dictionary equality.
5. Validator compares the entire ordered manifest `paths` with expected final paths, including omission, insertion, duplication and reordering. Check manifest `source_inventory_sha256`, state's `source_inventory.sha256`, exact `selection_counts`, and `selected_file_count` from these same reconstructed inputs. Existing stage/output/source/lineage checks remain necessary.
6. Also verify the five selection-derived limitation fields: unsupported, policy-excluded, inventory-unresolved, historical-held, review-held. Do not equate legitimate partial/failed parsing of an intended file with absence from the intended manifest. Parser-specific limitation counts remain governed by existing Reader validation.

Literal fixture: observed `Guide_ver1.csv`, `Guide_ver2.csv`, `Contact.txt`, `.env`, `blob.bin`, plus failed-read `Unseen.csv`. Expected final paths are `["Contact.txt", "Guide_ver2.csv"]`; selected_file_count is 2. Exact selection counts are `{"inventory_unresolved":1,"policy_excluded":1,"selected":3,"unsupported":1,"version_active":1,"version_historical":1,"version_ungrouped":1}`. Ver1 is historical; ver2 active. Excluding `.env`/blob/unseen is a positive control, whereas omitting Contact is a failure. All-held plus no unrelated eligible evidence continues to fail with the existing no-supported-files behavior, preserving the old published generation.

## Immutable app descriptor and propagation

Bootstrap alone captures into newly owned `generation/01-path/document-version-decisions.snapshot.json` after Path validation, before resolver build. Read the shared file once subject to a separately frozen production cap, strictly parse all its decision entries, and copy exactly the captured bytes. Only genuine initial missing shared input produces fixed bytes `{"schema_version":"1.0","decisions":[]}\n`. Preserve all old group decisions; no filtering, truncation or reserialization of present input. Create the snapshot exclusively and reject existing file/directory/symlink/dangling link. Failure must leave any existing target intact and stop the unpublished generation; do not modify shared input or repair old generations.

Capture returns one detached descriptor `{generation, path, sha256, byte_count}`. This descriptor is supplied to resolver build's decision path and the snapshot-mode validation gate, both semantic builds, projector lineage/security contexts, and registration. Build may retain its F05a public seam; the subsequent same-read gate verifies its result against the captured expected digest. A later shared D1 does not affect this D0 generation; the next explicit build captures D1 into a new location.

Recommended lineage context exact key sets:

- Legacy unversioned: existing `{output_dir, source_root, inventory}`.
- Explicit generic no-decision versioned: those three plus `{version_graph, version_authority_mode}`; mode must be `no_decisions`.
- Snapshot versioned: those five plus `{version_decisions_path, version_decisions_sha256}`; mode must be `snapshot`.

Update both `_attest_lineage_context` and `_attest_security_context` key gates. Null, extra keys, partial pairs or mode/type mismatch are errors; do not loosen to arbitrary supersets. The projector checks the returned attestation binding structure and requested authority, then carries only that detached value through security partition, projection report and SQLite metadata. Bare PASS or producer-state fields cannot fill missing snapshot data. Existing post-attestation state-swap tests should still demonstrate that a later producer value cannot replace it.

Registration derives the fixed snapshot path from its trusted generation, compares all producer descriptor path strings to that exact path **before** any path operation, and rejects symlink/directory/alternate generation paths. Include its identity and snapshot mode in `generation_artifacts` and the CONFIG-bound contract; bump the relevant Reader contract/producer identity. For saved generation verification, first validate the registered contract's bytes/hash and generation against CONFIG, then take the expected snapshot digest from that registered contract. Do not take it from current producer state or freshly hash the file as its own expectation. Missing pre-F05b versioned snapshot means explicit migration/rebuild, never copying today's shared decision file into the old generation. Identical bytes copied from another generation are not distinguishable by digest alone; test trusted fixed path/generation identity separately.

## Concrete current caller/mock changes

All paths below are under `distribution/macos-local-memory/` unless starting with root `tests/`. Line numbers were checked in this read-only preflight; freeze them again with final ownership.

| Caller/test | Required limited change |
|---|---|
| `app/bootstrap.py:2165,3680,3709` | Required `decision_snapshot` keyword on run_semantic_pipeline; same variable at first/model-ready calls; snapshot flags on Reader/Validator commands. Capture once before resolver lines3662/3668 and pass the snapshot, not shared support path. |
| `app/bootstrap.py:3715,3720` | Registration receives capture descriptor; index CLI gets mode/path/hash. `write_reader_generation_contract:584` and body/status callers at592/645 must not infer app snapshot requirement from producer version_binding. |
| `engine/build_adaptive_semantic_graph.py:192,328,352,496` | Replace versioned partial-list graph read with explicit full-inventory attestation and selection; add mode/path/hash API/CLI and current-producer fields. |
| `engine/validate_adaptive_semantic_graph.py:2558,2584,2623,3048,3077` | Same-read attestation, complete expected manifest/counts, explicit authority; detached result includes checked descriptor. Keep initialize_lineage behavior unchanged. |
| `engine/build_local_semantic_index.py:83,834,1240,2451,2537` | Exact context alternatives in both gates; pass mode/path/hash to actual adaptive Validator; validate report shape and propagate detached binding. Add CLI flags before any partial index file mutation. |
| `tests/test_document_version_resolver.py:158,271,288` under distribution | Direct partial-list helper calls must use full explicit fixture inventory plus deliberate `no_decisions`, or new attested-selection helper. Retain literal active/year/current conflict gold. |
| Same`:342/351,388/393,427/431` | Three Reader/Validator fixture pairs explicitly choose standalone `no_decisions`; no fake snapshot authority from returned graph fields. F05a standalone direct validate tests remain unchanged. |
| `tests/test_versioned_safe_index_e2e.py:238` under distribution | `unpublished_reader(versioned=True)` creates actual empty snapshot and passes authority to Reader/Validator/projector fixtures. Preserve distinct versioned-omission test by removing only its target argument after constructing a legitimate context. `versioned=False` remains genuine legacy. |
| Same`:271,277,281,293,319,353,361,369,422` | Explicit legacy mode for low-level unversioned contract control; normal versioned argument lists include complete descriptor so wrong/missing graph tests fail for their intended reason. Exact context tests add missing-mode/pair cases. Bare PASS mock remains rejected. Post-attestation spoof includes snapshot fields. Intentional omission tests assert failure before publication, even if stable error code changes for an earlier stricter gate. |
| `tests/test_reader_generation_migration.py:81,111,116,123` under distribution | Current fixture captures empty snapshot once, builds with it, passes required pipeline/registration descriptor; old fixtures lacking snapshot stay old and produce migration. |
| Root `tests/test_immutable_lineage_validation.py:196,200` | Published app generation calls receive the descriptor from its already authenticated registration/fixed location; legacy no-graph calls remain unchanged. Preserve read-only generation/source failure checks. |
| `tests/test_runtime_recovery.py:30,1169,1401,2494,3673` under distribution | Common semantic fixture gains explicit version/snapshot fields where it models a new app generation. Four fake_semantic functions accept the required keyword and assert fixed path/generation/digest; model-ready case2494 records descriptor equality across both calls. Do not merely accept `**kwargs` and ignore authority. Their fake Path/resolver command dispatch must construct the fixed snapshot/graph fixture intentionally. |
| Root `tests/test_version_graph_validation_e2e.py` | F05a gate command-injection tests must target the new explicit snapshot path rather than assuming shared file changes should alter a captured generation; retain original F05a artifact/test snapshots. Preserve real semantic forgery rejection at the initial gate. |
| Root `tests/test_unmarked_version_e2e.py` / existing Human E2E389 | Shared decisions remain written by the fixture as Human input; each new app build captures it. Assert old generation keeps D0 and new generation captures D1, preserving stale/nonselected-peer controls. |

Existing F05a pure tests and resolver wrapper semantics need no weakening. This is a concrete signature/mock shortlist, not a claim every test in the repository was read. Before execution, search all exact callsites again and assign any additional required test file explicitly. Do not run the broad runtime/package suites without side-effect review.

## Literal first RED plan, using current APIs

Freeze a separately named dedicated test file and actual method list after F05a acceptance. The following first methods can produce meaningful RED without passing unsupported new arguments:

1. `test_new_app_generation_materializes_empty_decision_snapshot`: existing valid app seed/build with shared decision absent. Assert the fixed file exists and its bytes equal the fixed empty payload, then require the same digest in graph/state/registration/metadata. Current failure should be the first file-existence assertion; no claim about downstream checks from that alone.
2. `test_current_versioned_consumer_rejects_omitted_authority`: use current valid versioned Reader/Validator direct entry with a real graph and no new kwargs. Require failure because new versioned consumption must declare authority. Current acceptance yields assertion RED; an eventual new-keyword TypeError is never acceptance evidence.
3. `test_reader_rejects_resealed_active_after_initial_gate`: complete the actual initial resolver gate, then replace a held mixed group's graph with a fully resealed false active selection before the Reader entry. Require the Reader to reject before `validate_inventory_binding`/source parsing is reached. A test-only accepted-entry sentinel can distinguish gate acceptance from an unrelated later failure. Prior CONFIG/index/source bytes stay unchanged.
4. `test_validator_rejects_coherent_eligible_contact_omission`: temporarily patch only the producer's current `select_inventory` during fixture production to omit Contact and coherently adjust counts, creating actual intermediate/semantic outputs for that shorter list. Restore the selector before Validator invocation. Full inventory still contains Contact. Require Validator failure based on full expected selection, even though the shortened manifest, stage hashes and outputs agree. Literal unsupported/sensitive/unobserved exclusions remain valid controls. The producer mutation models omission and must not remain patched during the independent consumer check.
5. `test_projector_rejects_missing_snapshot_authority_before_index_changes`: start from the legitimate versioned fixture and a prior index sentinel; use the existing projector CLI graph argument but omit authority fields, requiring failure before an output/.building file is replaced. Do not use downstream model failure as the oracle.

Additional post-API fixed controls are mandatory: explicit standalone no-decision PASS; missing/changed snapshot FAIL; D0 generation invariant under shared D1/corruption/removal; next build uses D1; valid Human choice of either member and full-set stale behavior; counted one-read attestation for graph/inventory/decisions with second-read substitution; graph/state metadata canary zero open/stat/resolve; model-ready same descriptor; exact count/manifest ordering/duplicate attacks; legacy unversioned success and old versioned migration; snapshot exclusive-create preservation. An attack stopped at the initial F05a gate does not count as downstream F05b coverage.

## Production decision limits: one unresolved contract choice

Current server sets `MAX_FORM_BYTES = 64 * 1024` at line60 and rejects oversized POST Content-Length at2672. Each document-version POST records one selected group. Resolver `load_decisions` at132 reads the entire store; `record_decision` at599 reads the map, replaces one group, and serializes all groups. No inspected byte/count cap or pruning bounds the accumulated store. One request's 64KiB maximum therefore does not bound the total file, and direct resolver CLI has no corresponding request-body cap. No user store was inspected, so legitimate observed maxima are unknown.

Recommendation: capture accepts a caller-configured `max_decision_snapshot_bytes` and reads at most limit+1 from one opened input before strict parsing; excess yields an explicit `decision_snapshot_too_large` failure and leaves shared/old state intact. Do not truncate, keep only recent groups, or equate 8KiB synthetic fixture budget with production policy. Also reject directory/nonregular shared input before reading rather than potentially blocking on a FIFO; this is a bounded file-input rule, not total OS race protection.

**The numeric production default is intentionally unresolved.** Root should freeze a documented configurable default and a synthetic capacity/control case before implementation, after deciding acceptable store growth and capture memory. The inspected 64KiB POST bound provides no defensible total-store number by itself. This is the only remaining API-contract choice identified here; it is not user approval, product implementation or an audit blocker yet. Small test cap injection remains 8KiB within the inherited 30-second/1MiB-log and fixture-write budgets.

## Current read-only source hashes

| Repository-relative path | SHA-256 |
|---|---|
| `distribution/macos-local-memory/engine/document_version_resolver.py` | `4ca6df75ced0c44e7da4364509311aad21f81601ba9d42b88efc1f6a9b157734` |
| `distribution/macos-local-memory/engine/build_adaptive_semantic_graph.py` | `19b5c55c64959dc136cd4d6b2081151ed1e39560874fb083e9796edb8812adfa` |
| `distribution/macos-local-memory/engine/validate_adaptive_semantic_graph.py` | `49138ecc11bde5709f129e879cbc5140717dca716a8563ebafe77dda4e37187d` |
| `distribution/macos-local-memory/engine/build_local_semantic_index.py` | `2bfaf8174e8249087d720e49070c9812ce9ecf93b21dd5276f057e145965dfb2` |
| `distribution/macos-local-memory/app/bootstrap.py` | `cad0bfee06fbdf0af0319d6fed2d467310ef697e848a9b6d170eacf061f31b55` |
| `distribution/macos-local-memory/app/local_memory_server.py` | `3acb859916ccb9fb1a1c26cc0ebfa511bea2d8114fa83103ace40183fa899ee6` |
| `distribution/macos-local-memory/tests/test_document_version_resolver.py` | `111b9ab6473145dbe0d43904219fb2355d9695e7e018798f1a75c5df11140d03` |
| `distribution/macos-local-memory/tests/test_versioned_safe_index_e2e.py` | `41540a858d17f8abb322c15353032ec4dd3099eac4e767090f7b9dbabfbcf64a` |
| `distribution/macos-local-memory/tests/test_reader_generation_migration.py` | `7d92425d39eb0316687ca9b8b0250768444652dcd39b7dfa9324380396b72d80` |
| `distribution/macos-local-memory/tests/test_runtime_recovery.py` | `f4a146b54526f279bf6923029e1887c7bea0298680913b768496502ade89ae77` |
| `tests/test_immutable_lineage_validation.py` | `89da7502c783e78fa3f3f11ba25a0f09680d35602548ea858d203ff9c8339989` |
| `tests/test_version_graph_validation_e2e.py` | `d401950ee20a9d88e13f81bcb551ce3f05ef403e289e0ffa0364a9e93914a9b8` |
| `design/local-memory-v1-hardening/runs/f05b-snapshot-preflight.v1.md` | `6bbf34e9457b3ccac39bdc71bb462d631a406f86014e6c4f565c7056c4f43630` |
| `design/local-memory-v1-hardening/runs/f05b-contract-refinement.v1.md` | `a6a5c2c6f5326c4847e1f4bcd290143270e651ee4c609eb962f6e5e080dfdeda` |

No source freeze is released by this proposal. F05a must be accepted first; root then freezes F05b exact signatures, cap/limits, ownership, literal gold and before hashes. The full F05/V1 requirements and F06/F19 limitations remain as in the root refinement.
