# Dated-HITL consent contract proposal v1 — root review required

2026-09-09. This is design and frozen-oracle preparation only. No product changes or test execution are authorized by this file. Same F02/F03/F04/F06/F13/F19 continuation; do not reset repair budgets, accepted historical slices or failed evidence. F11a remains paused at unexecuted generator v3; F11b remains mandatory.

## 1. Observed source, not new requirements

Resolver `distribution/macos-local-memory/engine/document_version_resolver.py` is currently 0.1.5, SHA256 `11218cc2b5170ba59a69203bcc549a50dc6eb6f88e487d191fb55a76048973a8`. At lines 367–424, a matching legacy selected path/source/set is still treated as Human authority and every other candidate becomes historical. At lines 752–784 the five-argument writer reads whichever graph is present, then saves its hashes without any displayed-revision input. Atomic replacement is not a concurrent read-modify-write CAS. `_validation_components` calls the same resolver from independent inventory/decision inputs (600–628), so its reconstruction will also need the new consent rule.

Observed server `distribution/macos-local-memory/app/local_memory_server.py`, SHA256 `190b05159d644f8ac4c1c117bd62b5b971295b6eac4feb5706c70cd3fc978180`: lines 646–705 render all unresolved groups, a selected-path radio and group ID but no displayed revision. Lines 2735–2759 call the old writer CLI. Request body cap remains `MAX_FORM_BYTES = 64 * 1024` (line 60). Observed bootstrap SHA256 `1e9a51a71544d4c3f8ad0c3fee1bf6f118804f37e7576e051b96daa75cf8b653`: generation names use `generation-[0-9a-f]{32}`; 3677 creates one, and 3755–3775 captures the immutable decision snapshot, builds/validates a graph and copies it to the shared review file. These current hashes differ from the earlier authority-review snapshot; do not relabel historical receipts as current executions.

The previous executor `dated-hitl-executor-residual-legacy-001` is an actual witness that old selection is not new same-work/current/use approval. Its old PASS means the gap existed, not that legacy reuse is acceptable. This proposal deliberately changes that behavior for dated candidate families.

## 2. First implementation slice: closed invariant and ownership

Proposed sole product target is the resolver. Keep graph schema 1.0, advance resolver identity to 0.1.6, and add an explicit per-record `decision_schema_version: "2.0"`. Preserve old stored bytes for history; do not fill new fields from marker strings, defaults, old actor labels, file timestamps or an existing active selection. Only dated families, as determined from the bound candidates' temporal signals, require this new schema in this slice. Undated legacy behavior remains unchanged and is a control, not a claim that all Human authenticity problems are solved.

Closed invariant: no dated group can become selected through a legacy, incomplete, malformed, denied, independent, deferred or stale-candidate consent record. A positive selection requires distinct same-work, current-applicability and content-use affirmations bound to the complete current candidate set and selected source hash. Existing graph reconstruction must derive the same result from its explicit inventory/decisions, not an embedded graph decision. Missing new inputs fail closed; no fallback to the legacy dated branch.

The first slice also adds a pure submission-preparation boundary: caller-trusted current revision versus submitted displayed revision must match before producing a record. It performs no file read/write and is not an atomic store CAS or Human-origin authenticator. The old five-argument writer must reject dated submissions before decision-store read/write because it has no displayed-revision or new consent proof. No new positive dated CLI writer is enabled in slice 1. The old dated UI is temporarily fail-closed until the required UI/CAS follow-on is implemented; do not display that refusal as success.

## 3. Exact proposed durable record and pure interfaces

New durable record has exactly these keys:

```json
{
  "decision_schema_version": "2.0",
  "group_id": "version_set_<32 lowercase hex>",
  "candidate_set_sha256": "<64 lowercase hex>",
  "relation": "same_work_revisions",
  "selected_relative_path": "手順2025.csv",
  "selected_source_sha256": "<64 lowercase hex>",
  "current_applicability_confirmed": true,
  "allow_ingest_index_answer": true,
  "reviewed_revision": {
    "generation": "generation-<32 lowercase hex>",
    "source_scope_sha256": "<64 lowercase hex>",
    "graph_sha256": "<64 lowercase hex>",
    "graph_file_sha256": "<64 lowercase hex>",
    "inventory_sha256": "<64 lowercase hex>",
    "candidate_set_sha256": "<64 lowercase hex>",
    "decisions_sha256": null,
    "resolver_version": "0.1.6"
  },
  "decided_by": "local-ui-human",
  "decided_at": "2026-09-09T12:00:00+00:00"
}
```

All record/revision keys are required and no unknown keys are accepted. SHA fields have exact lowercase-hex syntax; only `decisions_sha256` allows null, meaning absent store, not the digest of an empty store. Flags require actual bool, never integer 1 or strings. Actor and timestamp must be nonempty strings; they are audit labels, not proof of identity or freshness. Relation enum: `same_work_revisions`, `independent_records`, `defer`.

For same-work, selected path/hash must name an exact member. A false current flag or false use flag yields a held record, never positive selection. Missing/malformed fields are invalid, not an implicit denial-to-approval migration. Independent/defer records require selected path and hash null, both flags false. Independent means no supersession edge and no historical disposition: all original candidates remain retained and held until multi-active, per-record use permission is implemented. Defer is also retained/held. Do not force one annual record obsolete or use a selected path as a hidden tie-breaker. The first slice does not yet enable indexing both annual records.

Proposed APIs (these are not present yet):

```python
validate_dated_consent(key, candidates, decision) -> dict
prepare_dated_consent(key, candidates, submission, *, displayed_revision,
                      current_revision, actor, decided_at) -> dict
```

`validate_dated_consent` is pure and returns exactly `{"status": "ALLOW_SELECTION" | "HOLD", "reason_code": str, "selected_relative_path": str | None}`. `ALLOW_SELECTION` requires a fully valid same-work record with both flags true, exact group/set/selected source, and reviewed set equal to the current independently reconstructed candidate set. It means only this record meets the bounded selection rule, not that the app may answer now. `HOLD` always returns null selection. Proposed reason codes: `dated_consent_required` for absent/schema-less legacy; `dated_consent_invalid` for unknown schema, wrong shape/type, invalid selection, or incompatible independent/defer fields; `dated_consent_policy_changed` for a well-shaped old resolver revision; `stale_human_decision` for changed group/set/source/reviewed set; `dated_consent_current_not_confirmed`, `dated_consent_use_not_approved`, `independent_records_require_separate_use_review`, `human_deferred`; positive `human_confirmed_same_work_current_and_use`.

`resolve_group` uses this result whenever a decision is supplied for a dated group, with no legacy fallback. It otherwise retains automatic hold/conflict behavior. Positive selection stays one active member under the existing output shape; independent/defer always stay unresolved, all dispositions `needs_human_review`, no active-version graph edge. The public three-positional-argument `resolve_group` signature need not change.

`prepare_dated_consent` accepts submission with exactly relation, selected_relative_path, selected_source_sha256, current_applicability_confirmed, allow_ingest_index_answer. Actor/time are separate caller arguments, not taken from arbitrary submitted fields. It returns the exact durable record above. Trusted current revision is supplied by the caller, never synthesized from displayed fields, graph-controlled paths, decision fields or a self-hashed graph alone. Compare exact JSON types/values, not bool/int equality. Shape failure raises `ValueError("dated_consent_invalid")`; wrong current policy raises `dated_consent_policy_changed`; changed store digest raises `dated_consent_store_changed`; other displayed/current revision mismatch raises `dated_consent_display_stale`; current revision's candidate-set digest inconsistent with actual candidates or a stale selected source raises `dated_consent_source_changed`. Validate shapes, current policy, store equality, remaining revision equality, actual candidate bindings, then record semantics, in that order. No output record on failure; inputs are not mutated.

The first slice does not verify the external meaning of source_scope/generation or graph digests just because they have valid syntax. The pure prepare caller's explicit trusted current input is that trust boundary. Persisted reviewed graph hash is historical approval provenance, not the hash of the subsequently rebuilt graph: decisions change graph contents. Never demand that the later generation's graph hash equal the pre-decision display hash, and never silently replace the recorded display hash with the new graph's value.

Legacy `record_decision(graph_path, decisions_path, group_id, selected_path, actor)` must raise `ValueError("dated_consent_submission_required")` for dated groups before reading or writing the decision store. Undated behavior remains. This is the existing-API semantic RED for missing display proof. Positive submission/store persistence is expressly deferred, not implemented by copying prepared records into shared decisions during this slice.

## 4. Mandatory sequenced follow-ons, not slice-1 coverage

1. UI, one question at a time: relation (same work / independent / defer), then currently applicable candidate for same-work, then explicit permission to ingest, index and answer with that exact content. No prechecked approval. Decline/defer must not start a content pipeline. Show relative paths, date-likeness caveat, source revision, complete candidate revision and review generation. Independent records retain both; a future separate per-record permission flow can grant both without inventing supersession.
2. Trusted review descriptor: pin the raw graph bytes, logical graph digest, explicit inventory bytes, fixed review generation identity and source-scope identity at display. Use a server-side review ticket tied to CSRF/session, not hidden-field claims as authority. At submit, re-read/attest the explicit current inventory/graph snapshots and compare to that ticket. Do not follow graph metadata to choose paths or invent current generation. The shared review file can precede published generation and is not itself CONFIG publication authority.
3. Real concurrent CAS: under an exclusive application decision lease, read one bounded strict-JSON store snapshot, compare its exact bytes digest to expected `decisions_sha256` (absent distinct from empty), merge without losing other group records, and atomically replace while still owning the lease. Same-store revision reused by a second writer must conflict, even for another group, unless an explicit reread/review retry occurs. Successful file replacement alone is not CAS. Store envelope advances to 2.0 only in this writer slice; retain old records as inactive legacy history. Deterministic two-writer and duplicate-submit tests are required.
4. Same generation/decision revision: capture accepted store bytes into a new generation and propagate the same descriptor through actual Reader/model-ready replay/index. D0 approval must not be announced as D1 published; changed approval revision between capture and publish must hold/requeue or publish an explicitly stale generation that cannot answer. Check current authority before answering and again before final display. Immutable D0 historical artifacts are not rewritten on revocation.
5. Freshness and no fallback: changed/deleted sources or new family members invalidate complete-set approval; query/index-only hashing cannot discover new peers. Unknown/unreadable scope must hold before models and not fall back to an older active index or legacy answer route. Independent-year multi-use, unknown families/singletons/extensions and richer candidate discovery remain required broader work, not closed by strict record shape.

## 5. Test/gold, resource and rollback gate

Add `tests/test_dated_consent_records.py` only; preserve `tests/test_dated_temporal_candidates.py` and its original residual assertion unchanged as historical evidence. Freeze separate before resolver bytes for this next slice, not the prior 0.1.4 snapshot. Current resolver SHA is the 0.1.5 value in section 1. Source/method/gold manifest and runner must be fully reviewed by root before any execution. Missing new API/TypeError is never semantic RED.

Existing-API seed class exercises old matching legacy decisions, missing relation/current/use/display binding, stale reviewed candidate set, independent/defer with a selected-path legacy escape, and old writer without display proof; the undated legacy path is a control. A separate post-API class tests literal positive and held records, exact current/displayed mismatch for graph/raw graph/inventory/set/scope/generation/store/policy, changed selected source, strict bool/type/unknown key, null-vs-empty store distinction and no input mutation. It is not run before API implementation. Positive records are independently assembled from fixed literal candidates and stdlib canonical hashes, never from production's consent output.

All tests remain in-memory. Writer seed uses fake graph text and patched store helpers with observed read/write counts; no actual decision file is opened. Runner pins test/gold SHA and exact class/method list, imports only selected stdlib resolver/test code, then denies file, network, process and directory IO. Root-selected before run pins 0.1.5 resolver; post runs require separately frozen current source hash. Outer 30 seconds / 1 MiB log; fixed tiny fixtures, no real documents, app, config, model, GUI, broad scan or package installation. Existing request 64 KiB and configurable store snapshot 1 MiB / absolute 64 MiB limits are preserved, not replaced by an arbitrary production truncation cap. Tiny fixture ceilings are test limits, not claims about product request enforcement.

After explicit implementation release, only resolver plus new tests are proposed ownership. A future scope broadening needs a root addendum. Rollback of a new product delta must require its exact current hash and restore only its separate before snapshot/inverse; keep all gold/RED/failures/history. No implementation, test run, migration or rollback has occurred in this preparation.

Adapter/Agentic Audit preserve scope, literal expectations, before evidence and independent review. This proposal is not an approval schema already deployed, not proof of concurrent CAS, not Human authenticity, not end-to-end answer freshness and not formal audit acceptance.
