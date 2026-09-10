# Dated-HITL consent gold and runner preflight v1

Preparation only, 2026-09-09. Parent full-source review and explicit release required before any execution or product edit. Adapter and Agentic Audit SKILL.md, audit contract and schema were reread completely. The new test and runner sources were read back completely. Existing supervisor was reread completely. No Python/test import or execution, app operation, network/model, original-document or real decision/config read occurred during this preparation.

## Fixed new sources

| Path (repo-relative unless runs basename) | SHA256 | Bytes / purpose |
| --- | --- | --- |
| `dated-hitl-consent-contract.v1.md` | `98e7fc8f677f43a9214f0ea88fed8f5a5ecaf57c48e7e111340141dba10bd799` | Proposed narrow record/display contract; root review pending |
| `tests/test_dated_consent_records.py` | `f85d05c018dabac206e5f7308b57b6742e96a7ad0214689828d224881179086d` | 18371 bytes; seed 10 + post-API 17 methods |
| `dated-hitl-consent-gold.v1.py` | `f85d05c018dabac206e5f7308b57b6742e96a7ad0214689828d224881179086d` | Preimplementation immutable identical test bytes |
| `dated-hitl-consent-run.v1.py` | `af6e14a47c71438dc51a56a84361cdab4a3636e5ab490444e8ddbeefa1744532` | 7346 bytes; fixed lists and source pins |
| `dated-hitl-consent-before-resolver.v1.py` | `11218cc2b5170ba59a69203bcc549a50dc6eb6f88e487d191fb55a76048973a8` | 35441 bytes; separate current 0.1.5 baseline, not the earlier 0.1.4 before |
| `scripts/run_local_memory_hardening_tests.py` | `6b3bde90b6ed649a82325f6b5ad9661756bef7b6e0303fa74c2d045fb26eb4a9` | Existing supervisor, unchanged, read fully and pinned by the new runner |

`cmp` returned exit 0 for test versus gold and current resolver versus new before snapshot. These are read-only byte comparisons, not test execution. No product or existing test bytes were edited. Original date gold SHA `6a9a877771117441b33cdb94f5bc4f459b067bf682c538dbade00593189c7154` remains unchanged. Existing prior before, after, all successful/failed runs and byte-check failures remain historical.

## Literal method split and expected baseline (not actual results)

`DatedConsentSeedTests` has 10 methods, all use existing resolver APIs. On unchanged 0.1.5, static expected outcome is 9 failing methods / 16 assertion failures from subtests plus one undated compatibility PASS, zero ERROR/SKIP. This is an expectation only; do not record semantic RED until the actual log is inspected after root release.

- Matching old dated selection; version tag alone; missing relation.
- Missing/false/integer/string current and use approvals.
- Missing or stale displayed candidate-set revision.
- Independent annual/defer with a retained legacy selected path cannot mark other documents obsolete.
- Actual old five-argument writer must refuse before decision-store access when new proof is missing. Its graph read is a literal fake text object; `load_decisions`/`atomic_json` are patched and their calls observed. Existing implementation reads/writes those seams, so the baseline is a semantic reachability failure, not an absent future API.
- Undated legacy selection is the positive compatibility control.

`DatedConsentPostApiTests` has 17 fixed methods and is never run as baseline RED. It requires both proposed APIs callable before selecting tests; absence is an infrastructure error, not an expected failure. Positive record and return expectations are literal independently assembled dictionaries with stdlib hashes, not copied from production's result. Coverage: exact complete record, independent/defer all-held, explicit false flags, honest prepare/no mutation, logical/raw graph/inventory revisions, generation/scope, absent versus present store revision, selected and nonselected source changes, fabricated matching set digest, policy mismatch, malformed/unknown revision fields with no path reads, record group/set/source/policy checks, strict bool/schema, existing duplicate/nonfinite decision JSON rejection, independent/defer preparation and recorded use denial.

The authoritative complete method lists are literal `SEED_METHODS` and `POST_METHODS` tuples in the pinned runner and method definitions in frozen gold. The runner checks exact equality against unittest discovery, not merely test count. No original assertion is removed, skipped, expectedFailure-decorated or edited to pass.

## Side effects and limits

The worker imports the selected test/resolver and stdlib only before installing an audit hook. It checks test/gold bytes and the root-supplied reviewed resolver SHA; baseline `red` additionally hard-pins 0.1.5. After import, all `open`, directory listing/mutations, process creation, socket and ctypes audit events are denied. Tests use exactly two synthetic literal candidate records and cloned revisions, no original documents, directories, SQLite, application boot, HTTP server, actual decisions or models. The old writer's store helpers are mocked and restored by context managers; any real runtime open would fail the guard.

Each checked code input is at most 1 MiB. The runner checks the two fixed candidate records plus sample consent definition at most 8 KiB before tests. This is a tiny fixture-definition bound only, not cumulative allocation accounting, OS/RSS isolation or a new production request limit. Actual product request 64 KiB and existing configured 1 MiB / absolute 64 MiB decision snapshot limits remain as in the contract. Source imports have a normal frozen-code read/check/import sequence; this is not an adversarial same-byte module-loader attestation.

A worker alarm and supervisor both enforce 30 seconds. Supervisor captures stdout/stderr in a newly created mode-0700 run directory with a 1 MiB limit; timeout/output overflow stops only its new process group. It preserves started/result/log and interruption evidence and will not reuse existing output. The custom traceback formatter avoids source-file opens under the guard. No blocking model/GUI/dependency operation is present. No use of actual actor credentials, shared config or real decision store is required.

## Root-reviewable proposed first command — NOT executed

```text
/opt/homebrew/opt/python@3.14/bin/python3.14 -I -B design/local-memory-v1-hardening/runs/dated-hitl-consent-run.v1.py red dated-hitl-consent-red-seed-001 11218cc2b5170ba59a69203bcc549a50dc6eb6f88e487d191fb55a76048973a8
```

After actual semantic RED assessment and separate product release, fresh `seed` and `post` runs require the parent-reviewed resulting resolver SHA argument and their own fresh IDs. No broad existing suite is authorized by this preflight. Stop unexpected failure/error/resource termination for diagnosis and preserve every attempt. Do not change frozen gold without a separately named/additive correction and parent review.

## Anticipated old-test consequences and required follow-on

The requested safety change intentionally contradicts old dated approval expectations; this is not permission to edit them. `tests/test_dated_temporal_candidates.py:151` pins 0.1.5, and its legacy residual at 170–179 expects old Human selection. `tests/test_year_only_supersession.py:147` expects legacy dated Human selection; its stale legacy case at 155 currently expects the old stale-reason path. `distribution/macos-local-memory/tests/test_document_version_resolver.py:223` uses the old dated writer. Existing 0.1.4/policy literals in that test and `tests/test_unmarked_version_candidates.py:284` are already historical compatibility issues. These need a root-scoped additive current-version/consent cohort; do not relabel their old runs current after 0.1.6.

Undated Guide/ver1 fixtures in reconstruction and snapshot tests should retain legacy behavior, subject to actual later collateral verification. No such regression run happened here. Preserved source hashes: year test `70a1226cb30daa6692533d2c6f1fe270fe301a8f759c0db56a142acbc945b8c0`; unmarked `7bf83e2823742cc1449fdd69890d780e8c8b8daf69911905ce6cb20af2ad1f29`; reconstruction `33dc00f8c0cbe2bbf095c9236da32f74ea3ccc87a97f995b05005f999a8028ba`; snapshot `ef223b71064f8c0094c906fa19855cc7a62f91054ecb405cb9404eb3e7f8787a`; distribution resolver test `594089cdee5e0298c5c732e7da369417e14de7fea6282594acc43a55c6a4a126`.

Pure store-revision equality is explicitly not concurrent CAS. Caller-supplied current revision is not self-created from graph or submitted fields. Actual one-at-a-time UI, trusted display ticket, atomic store CAS, same-generation publication, multi-active independent-year use and pre/post-answer freshness remain required next slices. Their incompleteness blocks an end-to-end safety claim, but does not require a monolithic resolver/server patch for the initial record-validation invariant.

Rollback of preparation: stop using these new files; originals are untouched. A later product rollback requires the exact proposed delta and this new 0.1.5 baseline, never the earlier 0.1.4 snapshot or a broad git reset. No formal audit/self-approval or budget reset is asserted.
