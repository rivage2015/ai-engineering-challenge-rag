# F05a exact implementation proposal after root scope decision

This supplements immutable preflight v1 (`9f39bdb8cad9b91bff9e4f95848c6bf090b2d0d97d9526c3bb4d9316f84af6cb`). Proposal only; root has not yet frozen the implementation contract or released product ownership. No tests or product changes have been made.

## Scope accepted for contract drafting

Root selected standalone resolver validation API/CLI plus the bootstrap validation immediately before Reader. Bootstrap supplies the same configured explicit decisions path it supplied to build. Executor proposes resolver/new pure tests; root owns bootstrap/application forgery tests/README/coordination. F05 overall remains open: historical generation decision snapshots, downstream Reader/projector revalidation, complete downstream partition reconstruction and F06/F19 remain outside this distinct invariant.

The guarantee is about graph bytes versus the input snapshots actually read at the validation gate. It does not claim every transient change between build and validation is detected. A shared decisions change visible to that gate causes digest mismatch; a change reverted before the gate may be unobservable. Historical generation validation must not implicitly load today's mutable decisions through this new API.

## Exact proposed validation algorithm

1. Add optional positional/keyword `decisions_path: Path | None = None` to `validate()` and `--decisions` to `validate` CLI. Preserve its status/errors return shape, two-argument syntax, builder function and existing public `load_inventory` / `load_decisions` seams.
2. Read graph bytes once and inventory bytes once, using `Path.read_bytes()` into local values. If an explicit decisions path exists, read it once; treat only `FileNotFoundError` as an empty decision snapshot with null digest to preserve first-build behavior. Other IO/UTF-8/parse failures produce structured `FAIL`. Do not query any graph path field with `Path`, `exists`, `resolve`, `stat`, `open`, etc. `None` input means the explicit empty decision snapshot; it never falls back to graph metadata.
3. Decode strict UTF-8. Parse graph/decisions with duplicate-key rejection through `object_pairs_hook`, and reject JSON constants NaN/Infinity/-Infinity. Apply the same parser to every nonempty inventory JSONL line. Inventory records must be objects. Duplicate string `relative_path` values are ambiguous and fail, including duplicate records ignored later by candidate eligibility. Other eligibility remains exactly `candidate(record)` behavior; this does not certify upstream Path inventory authenticity or original file bytes.
4. Decision JSON must be an object containing a `decisions` list. Every entry must be an object with string `group_id`, and IDs must be unique. Preserve existing decision-field stale semantics: missing/changed candidate-set or selected-source bindings remain stale rather than creating an automatic choice. Arbitrary `decided_by` text is provenance, not an authenticated identity.
5. Require exact graph top-level keys: `schema_version`, `resolver`, `resolver_version`, `created_at`, `source`, `policy`, `counts`, `groups`, `nodes`, `edges`, `graph_sha256`. Require exact `source` keys `inventory_path`, `inventory_sha256`, `decisions_path`, `decisions_sha256`. Source path fields are typed, non-authoritative metadata (`inventory_path` string; `decisions_path` string/null), never followed or certified as root identity. Require `created_at` to be an offset-aware ISO datetime string; no equality to validation time. Hash/schema/identity/semantic fields must match independently expected values. Group/candidate/node/edge unknown/missing fields fail through exact canonical comparison.
6. Compute the stored graph self hash from that parsed graph snapshot, excluding only `graph_sha256`, preserving `graph_hash_mismatch`. Compute inventory SHA-256 from exactly the bytes parsed, preserving `inventory_changed` on mismatch. Compute decisions SHA-256 from exactly the explicit bytes parsed or null for empty missing/None snapshot. A non-null stored decisions hash with no explicit decision argument fails `decision_authority_required`; a mismatching explicit digest fails `decisions_changed`. A path-only stored claim with null hash supplies no authority and may remain valid.
7. From parsed inventory only, call existing candidate extraction/family helpers, assemble all groups with the exact F03a eligibility rule and ordering, then call existing `resolve_group` with only the explicit decision map. Compute expected projection and counters solely from these newly reconstructed groups. Compare canonical JSON bytes for every complete `groups`, `nodes`, `edges`, `counts`, `policy` and resolver-identity value, so boolean `true` cannot equal integer `1`, or `1.0` equal `1`. All disposition coverage within version candidates must match the expected partition. Never copy expected fields from the submitted graph or call write-capable `build()`.
8. Avoid changing shared builder seams: use a validator-only pure reconstruction helper if needed. Existing policy primitives may be reused; this is reconstruction independent of submitted graph, not an independently implemented policy specification. Tests freeze literal policy outcomes; the separate auditor must use independent holdout fixtures.
9. Recommended resolver identity bump is `0.1.4`, reflecting strengthened resolver validation. Current graph policy content is unchanged; its exact expected object is fixed in trusted code. This requires literal assertion updates in `distribution/macos-local-memory/tests/test_document_version_resolver.py:78` and `tests/test_unmarked_version_candidates.py:284`. Root must allocate the latter explicitly or own that one-line change. No existing direct resolver validation call found in the old resolver tests needs an explicit decision argument: lines 80 and 257 are automatic graph cases. Reader validator calls are a different API and remain untouched.

Structured failure should preserve specific legacy hash errors and use stable codes for source/JSON shape, duplicate keys/IDs, authority and semantic mismatches. It must not swallow interrupts or change errors into empty valid graphs. Exact error-code naming beyond the legacy codes can be frozen by root with the contract.

## Exact proposed dedicated pure test methods

New file: `tests/test_document_version_graph_validation.py`, class `DocumentVersionGraphValidationTests`. A test-local canonical JSON/SHA helper reseals mutations independently of resolver hash functions. Baseline graphs may come from the real builder, but assertions use literal expected paths/reasons/status, not builder-generated expected values. Fixture paths are synthetic only. Every successful build control is validated as well as inspected.

Initial true-RED subset uses the existing two-argument API and must fail by assertion (`FAIL` expected, current `PASS` observed), with zero import/errors/skips:

1. `test_rejects_empty_resealed_graph`
2. `test_rejects_omitted_unmarked_candidate`
3. `test_rejects_absent_inventory_candidate`
4. `test_rejects_resealed_candidate_field_changes` (fixed fields: source hash, size, mtime, birthtime, explicit years, versions, current/historical/draft markers)
5. `test_rejects_duplicated_or_reassigned_candidate` (two fixed mutations)
6. `test_rejects_fabricated_automatic_selection`
7. `test_rejects_embedded_human_authority_without_reading_graph_path`
8. `test_rejects_resealed_redundant_semantic_changes` (fixed policy/count/group reason/conflict/disposition/node/edge mutations, including true-for-one)

Additional fixed acceptance/control methods after implementation:

9. `test_accepts_mixed_hold_and_exact_membership` — Guide.csv + Guide_ver1.csv, two held, reason `unmarked_candidate_requires_human_review`, no active edge.
10. `test_accepts_numeric_version_control` — Guide_ver1.2.csv + Guide_ver1.10.csv, literal 1.10 active.
11. `test_accepts_year_hold_control` — Guide2024.csv + Guide2025.csv, both held with year-supersession reason.
12. `test_accepts_current_version_conflict_control` — current/Guide_ver1.csv + Guide_ver2.csv, both held with current/version-conflict reason.
13. `test_accepts_existing_group_exclusion_controls` — all-unmarked same family, singleton, unrelated family, unsupported/non-observed records preserve explicit expected group membership.
14. `test_accepts_explicit_human_choice_and_rejects_missing_or_wrong_authority` — current mixed group; matching explicit snapshot PASS, omitted/wrong snapshot FAIL; exact selected path and historical peer.
15. `test_stale_human_choice_holds_when_either_peer_changes` — changed selected or other peer; correctly rebuilt held graph PASS, forged active graph FAIL.
16. `test_accepts_missing_explicit_decision_file_and_ignores_path_only_metadata` — explicitly missing configured path and no-authority validation of null-hash path-only metadata remain compatible; any graph metadata access is forbidden by canary.
17. `test_hashes_and_parses_each_authorized_snapshot_once` — instrument reads of the three exact caller paths to return fixed bytes once; forbid re-read, write-capable builder helpers and graph-controlled paths. This is a snapshot-read test, not a global filesystem-race guarantee.
18. `test_rejects_duplicate_json_keys_and_nonfinite_constants` — fixed graph/inventory/decision duplicates and constants; no arbitrary fuzz.
19. `test_rejects_malformed_graph_shapes_and_identity` — fixed root/source/groups/nodes/edges/count type and missing/extra field cases, unknown version/schema, malformed/naive created_at.
20. `test_rejects_duplicate_inventory_paths_and_decision_group_ids`
21. `test_rejects_inventory_and_graph_hash_changes` — legacy hash error codes remain present.
22. `test_inventory_order_and_unrelated_addition_preserve_existing_family` — independent literal family status under reordered inventory and unrelated singleton addition.
23. `test_validate_cli_explicit_decision_authority` — in-process `main()` with fixed argv/captured stdout, matching explicit file returns 0; omitted explicit authority for Human graph returns 1. No subprocess launched by the test.

When the old baseline lacks the new API/CLI, methods 14–23 are not used to claim true RED. Every method including all subtests must pass after implementation. Do not skip or downgrade unsupported API errors into expected failures.

## Guard, evidence and rollback

One supervised Python child per suite, 30 seconds and 1 MiB log cap; direct resolver import only. Fixtures max 16 records, 16 KiB inventory, 8 KiB decisions, 64 KiB graph and 1 MiB cumulative serialized writes per process. The new harness must count JSON writes separately from the F04a source-only counter. No real source documents, model/metadata, network, GUI, credentials, app production state or child-worker launches. Read the existing supervisor fully before invoking it and freeze run wrapper bytes before execution.

Root freezes source hash `c2b98254ec28a82e8cc7b5ef3f5e780739bcc1b6983ef2a9252609156d4b672f`, exact test methods/ownership, old graph/test snapshots and guard. Preserve original true RED, every failed run and scoped inverse diff. Validate input/source hashes and result-log bindings after each run. No product writes until that freeze. Rollback is only the new assigned delta after checking no concurrent drift; original F03a artifacts and prior dirty changes remain unchanged. Formal audit repair limit two applies to the frozen invariant and cannot be reset by renaming it.
