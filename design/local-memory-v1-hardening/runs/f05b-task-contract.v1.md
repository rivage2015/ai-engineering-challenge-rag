# F05b frozen task contract

Task ID: local-memory-v1-f05b-snapshot-complete-selection. Root, 2026-09-09.
This contract follows accepted F05a, not a full V1 acceptance. Formal Executor–Auditor repairs: at most two for this invariant; metadata corrections are retained separately. No product edit is released until root records genuine current-source RED and freezes acceptance test bytes/methods.

## Required invariant and normative specifications

A new app generation uses one captured decision snapshot from resolver through Reader, Validator, projector and CONFIG-bound registration. Each versioned consumer reconstructs the exact ordered Reader selection and counts from its full explicit inventory and the attested decisions. Neither graph/state metadata nor omission of authority can downgrade a new app generation. Saved generations use their registered expectation, not today's shared decisions or a freshly computed self-expectation.

The complete requirements, normal controls, negative controls, scope limits and callsite list in `f05b-api-preflight.v1.md` (SHA-256 `8977281bb66d3e8b93472e2623c2d22094a120b8b768465d83e3a35d454e31da`) and `f05b-contract-refinement.v1.md` (`a6a5c2c6f5326c4847e1f4bcd290143270e651ee4c609eb962f6e5e080dfdeda`) are normative, except the choices resolved below. These files remain append-only. F06/F19 Human authenticity, publication revision/leases, source-root identity, cross-key family classification, whole-tree freshness and global filesystem races remain open. No all-format or real-model quality claim.

## Resolved choices

- Resolver `attest(graph_path, inventory_path, *, decision_mode, decisions_path=None, expected_decisions_sha256=None)` uses the proposal's exact three modes. It returns the proposed detached successful payload with exact keys. Failure returns only `{"status":"FAIL","errors":[nonempty strings]}`; no usable partial payload. Public `validate` preserves its F05a status/errors-only signature and semantics by wrapping this path. Graph/inventory/decisions are each read once per attestation; their hashes and parsed selection come from those bytes.
- Consumers use `version_authority_mode`, `version_decisions_path`, `version_decisions_sha256` and the proposed CLI names. Only `no_decisions` and `snapshot` are permitted; a version graph with omitted mode fails. Missing graph plus any authority fails. Exact context key sets and strict null/type/extra-key rejection follow the proposal. New app pipeline requires keyword-only `decision_snapshot`, without a default.
- Snapshot descriptor exact keys: `generation`, `path`, `sha256`, `byte_count`. The generation identity is the existing generation directory name. Snapshot path is fixed at `01-path/document-version-decisions.snapshot.json` in that generation. Present input is copied byte-for-byte after strict full validation; genuine initial absence uses `{"schema_version":"1.0","decisions":[]}\n`. Capture is exclusive, bounded, no-follow and regular-file-only; do not repair existing targets or old generations.
- Production capture default: 1,048,576 bytes for decision metadata, configurable with app CONFIG key `max_decision_snapshot_bytes`. Accept only a JSON integer (not bool), from 1 through 67,108,864. Missing key selects the default; invalid values fail explicitly. This is a conservative new resource policy, NOT an observed user-store maximum or the per-request UI limit. Larger legitimate stores fail with `decision_snapshot_too_large`, preserve all bytes and explain that the limit can be increased within the maximum. No pruning/truncation. Snapshot-mode attestation has an absolute 67,108,864-byte read ceiling, so every allowed capture is consumable; legacy explicit-decision wrapper limits are unchanged. Test capacity using injected small limits, not large real fixtures. No server/UI or environment-variable configuration expansion.
- Reuse the Reader selection policy in one shared pure helper. Freeze literal full fixture gold: final paths `["Contact.txt","Guide_ver2.csv"]`; selected_file_count 2; counts `{"inventory_unresolved":1,"policy_excluded":1,"selected":3,"unsupported":1,"version_active":1,"version_historical":1,"version_ungrouped":1}`. Verify all five selection-derived limitation fields. Exact canonical JSON comparison prevents bool/number coercion. No loss of parser partial-result semantics.
- New app registration requires captured authority independently of producer-state graph presence. Any retained generic unversioned registration helper requires explicit `legacy_unversioned`; no mode inferred from JSON. Bump producer/contract identity. Verify CONFIG-bound contract before using its stored snapshot expectation. Missing old versioned snapshot is migration/rebuild, not automatic repair.

## Ownership and phase gates

Single product Executor `/root/f05a_executor` owns only these mutually dependent files after explicit release:

1. `distribution/macos-local-memory/engine/document_version_resolver.py` before `4ca6df75ced0c44e7da4364509311aad21f81601ba9d42b88efc1f6a9b157734`
2. `distribution/macos-local-memory/engine/build_adaptive_semantic_graph.py` before `19b5c55c64959dc136cd4d6b2081151ed1e39560874fb083e9796edb8812adfa`
3. `distribution/macos-local-memory/engine/validate_adaptive_semantic_graph.py` before `49138ecc11bde5709f129e879cbc5140717dca716a8563ebafe77dda4e37187d`
4. `distribution/macos-local-memory/engine/build_local_semantic_index.py` before `2bfaf8174e8249087d720e49070c9812ce9ecf93b21dd5276f057e145965dfb2`
5. `distribution/macos-local-memory/app/bootstrap.py` before `cad0bfee06fbdf0af0319d6fed2d467310ef697e848a9b6d170eacf061f31b55`

Executor may update only signature-affected tests listed in the normative proposal, preserving frozen F05a evidence and literal gold; record before bytes/hash, forward/inverse delta and reason for every compatibility change. Additional files require root assignment first. Executor owns new `tests/test_decision_snapshot_attestation.py` and append-only `runs/f05b-executor-*` evidence. Root owns new `tests/test_decision_snapshot_e2e.py`, `runs/f05b-root-*`, contract, README and durable status. Auditor `/root/f03a_independent_audit` performs separate-context read-only product review and owns separately named audit tests/reports only after artifact freeze. Never overlap product editors.

Phase A: root freezes/runs app acceptance RED on unchanged F05a source. Executor may prepare additive unit acceptance tests and their literal gold, but no product or existing-test edits and no concurrent root-dependent fixture changes. Freeze those tests before implementation. Missing new API/TypeError is not semantic RED; first RED uses current APIs.

Phase B: root explicitly releases product ownership after RED and before-hash check. Implement minimal cross-layer fix and all fixed controls, then focused regression. No mandatory oracle deletion or changing gold to match product. Retain every failed run. Test files can gain controls with an additive recorded revision.

Phase C: freeze artifact/source packet; separate audit; parent validates schema, references, hashes, report status, actual terminal logs and inverse/source preservation. A local pass covers only this invariant. Max two formal repairs, then preserve unresolved blocker without resetting budget.

## Test and operational limits

Only synthetic tmp fixtures and stub inference. Each supervised child: 30 seconds, 1 MiB log, no real network/model/subprocess/GUI/credentials/source imports; inherited reviewed guards. Synthetic sources <=16 KiB/case, decisions <=8 KiB, graph <=64 KiB, inventory <=16 records/16 KiB, explicitly generated fixture bytes <=1 MiB/process. These are test limits, not whole-process memory/disk/OS sandbox guarantees. Review runtime/package side effects before selecting bounded cases. Use exclusive run IDs; never overwrite failed evidence.

Required initial attacks: missing new-generation snapshot; omitted Reader/Validator authority; resealed false-active graph after genuine initial gate and before source read; coherent eligible Contact omission; projector authority omission before prior index replacement. Required post-API controls are all those enumerated in the normative proposal, including same-read substitution/canaries, D0/D1 stability, model-ready descriptor identity, stale Human choices, strict manifest/counts, exclusive-create preservation, legacy and migration. Initial-gate rejection does not prove downstream rejection.

Preserve user changes, protected build/docs and previous accepted scopes. No commits, push, release, real-data indexing, network/models, permission escalation or permanent system changes in this continuation. Maintain resumable checkpoint and periodic honest progress reports.
