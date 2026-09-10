# F03a immutable task contract

Task ID: `lms-v1-00-hardening-2026-09-09-f03a`. Orchestrator: root. Scope fixed 2026-09-09 08:19 JST, before product changes. This follows accepted F02a/F04a; it is a new candidate-discovery defect, not a retry of their audit failures. Maximum independent-audit repair cycles: 2.

## Goal and exact boundary

For a valid Path inventory containing unique, observed document-file records in the existing DOCUMENT_SUFFIXES allowlist, include signal-free peers when their **unchanged existing family_key** matches a family containing at least one marked file. A marked file has a nonempty explicit_years, explicit_versions, current_markers, historical_markers or draft_markers list. Keep all those lists and all existing candidate fields unchanged in meaning.

- A family with at least two candidates and at least one marked candidate becomes a version group containing every eligible same-key candidate, including unmarked ones.
- A family with no marked candidate remains ungrouped, even if normalized copy names match; a single candidate remains ungrouped. Do not stop ordinary unmarked-only Reader input.
- A mixed marked/unmarked group without a valid full-set human choice is entirely held, with no selected path, no active_version edge and no historical disposition. Use reason `unmarked_candidate_requires_human_review`, before existing current/year/version automatic selection. This prevents an already marked current file from automatically demoting a newly discovered unmarked peer.
- A valid explicit human choice bound to the complete current candidate-set hash and selected source hash may select either a marked or unmarked candidate. Existing stale-human-decision precedence remains: added/changed/removed candidates in a still-formed family invalidate the old choice, with `stale_human_decision` and candidate_set_changed when applicable.
- Candidate ordering and group/graph projection stay deterministic. Do not change family_key, candidate hash field representation, record_decision semantics or old all-marked automatic branches except the declared resolver identity/policy description.
- Existing eligibility filters for kind/read_status/suffix/hash presence remain. No arbitrary or unreadable inventory record becomes a candidate by this change.

The normalized filename-family edge means candidate grouping, not proof that files are revisions of one document. Different suffixes, unrelated parent/stem keys, arbitrary renamed/moved files and all-unmarked document relationships remain F03 follow-ons; annual-versus-revision intent and multiple status signals remain F02/F04. F05 independent reconstruction of forged version graphs, F06/F19 review publication/lease, and F13 current-source freshness are not fixed here. Group dissolution down to one/zero member remains ungrouped; this scope does not promise a new Human review surface for dissolved groups.

## Required acceptance tests

1. True preimplementation RED for year+unmarked, version+unmarked and current+unmarked same-key families; all candidates held after fix. Include neutral English/Japanese names and input permutations, not literal production filenames.
2. All-unmarked/single/unrelated-parent/unrelated-stem/different-suffix controls remain ungrouped; current-vs-historical and numeric-only positive selection plus F02a/F04a controls stay unchanged. Invalid kind/read_status/suffix/missing-hash controls remain filtered.
3. Human can choose either member of a full mixed set; additions and changes to unselected unmarked candidates invalidate that decision in a still-formed family. A previously bound all-marked decision becomes stale when a same-key unmarked peer is added. Keep exact candidate path sets and source hashes as the oracle.
4. Root synthetic app wiring tests: mixed family excluded from Reader/safe SQLite but unrelated contact still eligible; full-set human choice can include the unmarked file; later unmarked addition after a bound decision returns the whole family to review. Check graph binding, source-byte preservation and fresh unpublished generations. Existing full build/query/final-audit positives remain required controls with stub models only.
5. Full existing resolver/Reader suite, existing 17-method stub app E2E, year-only tests and focused lineage/migration regressions must pass (residual witnesses counted separately). No required skip or expected failure counts as acceptance.
6. Independent auditor reviews contract, exact delta, immutable artifact/source hashes and results; reruns focused/all-marked/app controls and supplies extra holdouts. Parent verifies full audit schema, every hash/ID/edge/basis reference, terminal test counts and status-summary consistency before scoped acceptance.

## Ownership and before state

- Executor `f02_contract_review`: `distribution/macos-local-memory/engine/document_version_resolver.py`, new `tests/test_unmarked_version_candidates.py`, and the existing resolver test's exact identity assertion only. May create new `runs/f03a-*` executor records except root-owned names below. Other existing tests must not be rewritten to make them pass.
- Root: new `tests/test_unmarked_version_e2e.py`, root runner/audit artifact/validation records, the package README's version-candidate explanation, and progress/checkpoint. Root does not edit resolver implementation while executor owns it.
- Before resolver SHA-256: `9a7443862c015ed768007ef5a7b21bd3bc197c9bb7a3d7ed2f7b39b33374359d`.
- Before resolver test SHA-256: `b7c323f48f10488ef2dc245dacc265ad0dd5b77ea37e50b9e4b32504bda7b293`.
- Before existing app E2E SHA-256: `41540a858d17f8abb322c15353032ec4dd3099eac4e767090f7b9dbabfbcf64a` (read-only).
- Before README SHA-256: `aa088a5b6ac8e2d83398d6adba8e36864d18808415cc002f03fe246b7e630c0b`.
- Recheck exact bytes before editing. Resolver version 0.1.2 → 0.1.3 and policy text must disclose the new mixed-set hold. Existing bootstrap already fingerprints resolver bytes; preserve and test migration behavior.

## Safety, evidence and rollback

Small synthetic inputs only, max 1 MiB source writes and logs per guarded suite, 30-second wall budget, one small worker per invocation. Reuse inspected F04a guard/F01 in-process CLI dispatch; models/HTTP/network/subprocess/GUI forbidden in workers. Python3.14 for application wiring; 3.9 only where installed optional dependencies are required, without claiming full app compatibility. Guard is not OS isolation or full resource acceptance.

Freeze fixture/gold/RED outputs before minimum product fix. Preserve prior files and failures; new run IDs only. Save exact before snapshots and isolated deltas; rollback only these assigned hunks after rechecking current bytes, never checkout/reset the whole dirty file. Do not commit, push, publish, load real Desktop data, touch production CONFIG/indexes, credentials, protected build/docs files or install dependencies. Product release and whole V1 acceptance remain open.
