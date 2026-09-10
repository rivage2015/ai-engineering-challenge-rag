# F05a executor preflight: explicit inventory and decision authority

Status: read-only implementation proposal, not a frozen task contract or acceptance. Prepared by `/root/f05a_executor` on 2026-09-09. No product/test edits, imports, or test execution were performed in this preflight. The adapter and audit skills and both required audit references were read. Core principles are unchanged.

## Current evidence

- The live resolver is version `0.1.3`, SHA-256 `c2b98254ec28a82e8cc7b5ef3f5e780739bcc1b6983ef2a9252609156d4b672f`.
- `candidate()` (line 146) derives all 11 candidate fields from inventory records. `build()` (line 386) retains existing `family_key()` boundaries, requires at least two eligible candidates and at least one version signal, and resolves the complete sorted set.
- `validate()` (line 441) checks the explicit inventory file hash, graph self hash, node IDs/endpoints and active counts. It does not reconstruct candidate membership, candidate fields, policy decisions, projection, or counters.
- The saved F05 observation reports three self-consistent forgeries accepted: empty graph; fabricated automatic active choice for a mixed held family; absent-in-inventory candidate. The three executions were performed earlier by root; they were not rerun here. They prove the inventory/JSON-boundary gap, not application exploitation or source-document attestation.
- The existing `validate(graph_path, inventory_path)` API has no trusted decision input. `build()` records a decision path even when that explicitly provided file does not exist, in which case its decision hash is null and decisions are empty. A path string in the graph therefore cannot itself establish either authority or the presence of decisions.
- `resolve_group()` permits a valid explicit Human decision before automatic policy, and holds a stale decision without falling back to automatic selection. F02a year-only hold, F03a mixed-family hold and F04a single-version conflict checks must remain unchanged.

## Recommended smallest closed invariant

At the resolver inventory/JSON boundary, accept a stored graph only if all its semantic output is exactly the deterministic result of the explicitly supplied inventory snapshot and explicitly supplied optional decision snapshot under the current resolver policy. Reconstruct first; do not infer expected membership or authority from graph contents.

1. Read each caller-authorized input once into an immutable snapshot, hash those bytes and parse those same bytes. The graph source digest must bind to those exact inventory bytes. This avoids comparing one inventory read to a digest of a different read. Original document contents and paths are not opened.
2. Reconstruct candidate records from inventory, group by the unchanged family key, apply the unchanged group inclusion rule, and sort by the current byte order. Compare every group and all candidate fields, including `size_bytes` even though the old candidate-set hash excludes it. Reject omitted, added, duplicated, reassigned and altered candidates or groups.
3. Resolve only from the reconstructed candidate set and caller-provided decision records. Compare status, selected path, resolution basis, reason, conflicts, candidate-set digest and every candidate disposition. A validly self-hashed automatic or Human selection is insufficient.
4. Reconstruct nodes, edges and counts from the reconstructed groups; compare their complete canonical arrays/objects. This closes redundant-output drift, forged edges and partial or duplicated candidate dispositions. Expected graph policy and resolver identity must be fixed by trusted code, not copied from the stored graph.
5. A malformed required graph container returns a deterministic failure, never success by an empty fallback. Reject duplicate JSON object keys and ambiguous duplicate inventory relative paths / decision group IDs if the frozen contract includes arbitrary malformed JSON. Otherwise explicitly constrain the contract to unambiguous well-formed input, retain malformed-input work as required, and do not claim full F05.
6. Keep validation read-only. Do not call `build()` with a temporary output, `atomic_json()`, or `record_decision()` from validation. Extract a pure reconstruction function that consumes already parsed trusted input. Reusing existing candidate/policy primitives is independence from the submitted graph, not an independent implementation of supersession policy; fixed literal test oracles and a separate auditor must cover policy regressions.

The full F05 plan also requires complete downstream partition reconstruction. Exact version-candidate disposition coverage is closed here, but proving every eligible inventory document is represented exactly once across Reader allowed/historical/held partitions requires the caller/Reader boundary. Root is inspecting that boundary in parallel. Until it is included and tested, report only a scoped resolver invariant, leaving full F05 open. Do not use this split to reset repair limits for the same failing invariant.

## Trust boundary and compatibility proposal

Preferred API addition: `validate(graph_path, inventory_path, decisions_path=None)` and an optional validation CLI `--decisions`. Existing two-argument calls remain syntactically valid and mean no decision authority was supplied. The return shape remains `{"status": "PASS" | "FAIL", "errors": [...]}`.

- The optional decision path originates from a trusted caller setting or an explicit CLI argument. A caller must never populate this argument by extracting `graph.source.decisions_path`.
- The validator never opens, resolves, stats or traverses the graph's stored decision path. That field is provenance metadata only. A canary test should make any such access fail.
- Without an explicit decision snapshot, reconstruction uses no Human decisions. Genuine automatic/held graphs remain valid; graphs claiming Human or stale-Human effects fail unless their effect matches the empty-decision reconstruction and their decision hash is null. A non-null stored decision hash cannot be validated without the caller's snapshot.
- A path-only stored claim with null decision hash is compatible with the current builder's missing-file behavior and need not be rejected solely because the path string is non-null. It still provides no authority.
- With an explicit decision file, the stored decision digest must match the exact bytes parsed; current selected-source and complete-set stale semantics remain intact. Whether an explicitly missing decision file is permitted as an empty snapshot or must fail is to be frozen with the callers. Neither choice permits graph-controlled fallback.
- If validation becomes stricter about resolver version/policy, root should choose a resolver version bump and explicit rebuild/migration behavior. Do not silently authenticate old-policy generations as current policy, and do not rewrite preserved old generations.
- `record_decision()` presently trusts the self-hashed graph when recording a selection. This preflight does not establish the displayed graph's Human-intent integrity, source root, review generation, lease or authorization. Those are F06/F19 required boundaries and must be named if unchanged.

An alternative is to pass already authenticated immutable decision records plus their snapshot binding instead of a path. That avoids every validator caller doing filesystem discovery, but it is a larger API change. Choose one explicit authority contract before product edits, and propagate it through all validating callers rather than weakening validation for callers that lack authority.

## Proposed pure test oracles, fixed before implementation

Use direct imports of the resolver only and tiny synthetic inventory JSONL; do not invoke Reader, application bootstrap, model metadata, GUI or real document parsing. Keep independent test canonical JSON/SHA helpers. The builder may supply normal graphs to mutate, but expected selection, reasons and membership must be literal assertions independent of `resolve_group()`/`graph_projection()` results. Acceptance tests must assert `FAIL` on the old resolver for the following security cases; saved observation assertions that forgeries pass are historical gap evidence and must not be edited into acceptance tests.

| Case | Fixed expectation |
|---|---|
| `Guide_ver1.csv` plus `Guide.csv`, both observed | One family; exact two paths; both held; no active edge; reason `unmarked_candidate_requires_human_review` |
| Empty groups/nodes/edges, zero counters, resealed self hash | FAIL against the unchanged two-record inventory |
| Delete only the unmarked peer, reseal candidate digest, projection and graph | FAIL; complete candidate membership is still required |
| Replace a candidate path/hash with `NeverInInventory.csv`/64 zeroes, reseal all hashes | FAIL |
| Change source hash, size, mtime, birthtime or parsed version/marker fields with all stored hashes resealed | FAIL for each bound field, including size outside old candidate-set digest |
| Duplicate or move a candidate to a fabricated second group with otherwise consistent projection | FAIL |
| Convert the mixed held family to an automatic active `Guide.csv`, reseal everything | FAIL |
| Convert the mixed held family to a Human active choice; forged decision file is reachable only through graph metadata | FAIL; zero reads/stat/resolve of that path |
| Same valid Human choice, caller explicitly supplies matching decision snapshot | PASS; selected literal path, other historical, basis `human` |
| Same graph with decision snapshot omitted, wrong bytes, or wrong selected-source binding | FAIL |
| Human choice becomes stale when either peer changes | Correct rebuilt held graph PASS; a forged continued active selection FAIL |
| Modify only policy, counter, reason, conflict, edge, node, disposition or expected ID and reseal graph | FAIL; assert each redundant semantic component is covered |
| `Guide_ver1.2.csv` plus `Guide_ver1.10.csv` | PASS; version 1.10 active and 1.2 historical |
| `Guide2024.csv` plus `Guide2025.csv` | PASS; both held, `year_order_does_not_establish_supersession` |
| `current/Guide_ver1.csv` plus `Guide_ver2.csv` | PASS; both held, `current_marker_conflicts_with_latest_version` |
| All-unmarked peers, singleton, unrelated family, unsupported/non-observed records | Exact existing group-inclusion behavior preserved; no manufactured group; malformed ambiguity treated separately |
| Reverse inventory order or add a genuinely unrelated singleton | Same existing family membership and decisions; explicit source digest changes as appropriate |
| Malformed graph/duplicate-key container, if included in frozen input domain | Structured FAIL, no exception success fallback |

Root should add independent application/Reader controls that the published partition preserves exactly one disposition per applicable record, refuses forged graph publication, and retains the prior valid generation. Resolver-only PASS cannot establish those effects.

## Resource guard and stop conditions

Before running tests, root freezes exact method names, files, before hashes and output directory. Reuse `scripts/run_local_memory_hardening_tests.py` with one supervised Python child, `-I -B`, 30-second wall limit and 1 MiB combined log cap. New pure fixtures: at most 16 inventory records, 16 KiB inventory bytes, 8 KiB decisions, 64 KiB per graph and 1 MiB cumulative serialized writes across each test process. No threads, subprocess children, network, source documents or production CONFIG/index are needed.

The existing F04a IO guard was inspected. It rejects ordinary worker socket/process launches and restricts reads/writes to reviewed paths, but its cumulative source byte counter only covers `Path.write_text` when `source` occurs in the path components. Inventory/graph JSON writes therefore require their own explicit counter before serialization; do not claim they were already capped by that guard. The guard is a Python test aid, not an OS sandbox, total-RAM/disk bound or SIGKILL recovery guarantee. Do not import the full app dispatcher for a pure resolver suite unless needed by separately assigned regression tests.

Stop the scoped test run on source hash drift, input-byte mutation, unexpected access/process/network or budget violation. Preserve the failed result, stderr and input. Missing/malformed results, import errors, skips and expected failures are not acceptance. Formal Executor–Auditor repair cycles remain capped at two for the frozen invariant. Unresolved required paths remain open/blocked with their exact reason.

## Ownership and rollback

Current executor ownership is only new `runs/f05a-executor-preflight*` files. No product/test ownership has been released. Recommended later executor scope is the resolver, a new dedicated pure test file and new F05a executor run/evidence files; root owns callers and other agreed integration tests. Freeze these exact names before editing.

Save immutable before bytes, before hashes and the exact scoped diff before implementation. Roll back only newly assigned F05a hunks against their captured before state after ensuring no concurrent owner changed the files; never restore the whole dirty worktree or an old F03a file snapshot blindly. Preserve all F03a source snapshots/artifacts as historical. No rollback is needed for this preflight; retain the proposal.

## Observed source hashes

These were read from the live workspace during this preflight, not taken as current from older acceptance records. RESUME/checkpoint are mutable coordination files and may change after this observation.

| Source | SHA-256 |
|---|---|
| `engine/document_version_resolver.py` under distribution | `c2b98254ec28a82e8cc7b5ef3f5e780739bcc1b6983ef2a9252609156d4b672f` |
| `distribution/macos-local-memory/tests/test_document_version_resolver.py` | `7600fef6fa1978c6f07042ce9418e20efa88b47d0cc523b1a647cb0a1ad35c9a` |
| `tests/test_unmarked_version_candidates.py` (hash only, not used as a test oracle) | `b4a9176f3f7bb723d701a548418be8647a5336dc7b88dd0707d03cfa70eb5b94` |
| `design/local-memory-v1-hardening/RESUME.md` | `d89dae84dfaabbb458cb955575b2c2a98880bc57b55643039687f239e92ea956` |
| `design/local-memory-v1-hardening/checkpoint.json` | `d03fc50d5be551ca259fef965382cc2246651772313c92237b6d7a45363142bf` |
| `design/local-memory-search-v1-00-hardening-plan-2026-09-09.md` | `e49801e3af62fb0a4e8f73a1379d105c2ff5b62946606d69ddf2522f4935f456` |
| `runs/f05-next-preflight.v1.md` | `b8d67bba90c0341769d6b3ca7aa536bf363e989b9faf956d1acafc49321a6d9d` |
| `runs/f05-next-observe.v1.py` | `62044f0c28000bae7e78f7d50e400144235be926de5ec444eceb7d7c2df1d2f2` |
| `runs/f05-next-observation-result.v1.json` | `3446822347196222dd1838153e48fb6e64a9cf39da103a5b1450cd7e7cb27338` |
| `scripts/run_local_memory_hardening_tests.py` (contract/guard use must be reread before execution) | `6b3bde90b6ed649a82325f6b5ad9661756bef7b6e0303fa74c2d045fb26eb4a9` |

No implementation or independent audit approval is asserted here. Root must resolve the caller authority and downstream-partition scope, freeze the exact contract and ownership, then authorize true RED and implementation under the already authorized overnight work.
