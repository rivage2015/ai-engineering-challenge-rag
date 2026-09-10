# F05b executor handoff — independent audit pending

2026-09-09. Contract local-memory-v1-f05b-snapshot-complete-selection. Adapter and Agentic Audit were used to separate preflight, fixed gold, implementation, retained failure evidence and independent acceptance. This is not self-approval or full V1 acceptance. Formal audit repairs used: 0. Parent's separate audit and source/log/schema checks remain required.

## Implemented bounded invariant

Resolver attest exposes required no_decisions / explicit_decisions / snapshot modes and exact detached success/failure payloads. Legacy validate preserves status/errors-only behavior and genuine explicit-file absence semantics. Snapshot mode hashes/parses one bounded read and requires the external expected hash. No graph metadata path is followed. Full Reader selection and exact ordered manifest/count/selection-limit comparison are reconstructed from the same attested inventory. Projector contexts are exact mode-specific key sets; omission, null, extras and incomplete authority cannot downgrade a versioned call. Detached checked authority reaches SQLite metadata.

Bootstrap captures present strict decision JSON byte-for-byte once; genuine absence alone creates the fixed empty payload. CONFIG cap defaults to 1048576 bytes, exact integer range 1..67108864, no truncation. Snapshot target is exclusive/no-follow; static generation and 01-path directories must be non-symlink real directories before capture. Existing target survives failed-generation cleanup. The same four-key descriptor is passed through resolver gate, both Reader builds, Validator, index and registration. Producer identity is 0.5.0, Reader contract schema 0.2. App registration independently requires snapshot authority even if producer graph binding is omitted; old saved versions migrate. Saved expectations come only from a contract whose raw SHA is already CONFIG-bound. Generic unversioned helper requires explicit legacy_unversioned=True.

These are per-invocation snapshots and two static directory checks, not atomic multi-file snapshots, all-ancestor locking, cryptographic Human authenticity, global filesystem races, lease/publication fixes, root-identity or all-format/model-quality claims. F06/F19 and previously recorded family/year residuals remain outside this invariant.

## Frozen source identities

- resolver: 14ecce2c01d68bfbfaa27d3022bfc6076a97465a331eb03443af0e6d50cc006f
- Reader: 5d2883e2a053776935d71b2486180b177078fe71b5d7a0a02bf8741e8e041c0a
- Validator: 17c5de11a6f8f958ea5d1db447840f5653128ef24314fad193a610f824c5a106
- projector: 95b44b327fb0e0c1e747c9d244b077950ebf5b61dd0a8bf56b33b1a6c0e163da
- bootstrap: e6248aae9ffa89e3cd6a43839af7f41e4d5941482f8f00a48f466de5524b526b
- new pure 28-method gold/test: ef223b71064f8c0094c906fa19855cc7a62f91054ecb405cb9404eb3e7f8787a

All five original before snapshots match the frozen contract. Six existing compatibility tests changed: resolver direct policy helper now gets full fixture inventory/no_decisions; E2E fixture creates real snapshot/explicit flags and adjusts stricter error reason assertions; migration captures synthetic empty snapshot; immutable-lineage test takes expected hash from checked registration; four runtime mocks assert exact descriptor and exercise real resolver authority with synthetic storage; F05a gate injection targets captured snapshot rather than shared mutable file. No unmarked test change was needed. Existing F05a historical snapshots and root-owned app gold were not edited.

## Evidence and attempts

Current relevant local results: pure28, resolver20, E2E17, migration7, focused13, initial-gate4, bounded runtime4, and static-path2 all PASS with zero skips. They are different bounded runs, not one whole-suite coverage claim. Parent separately owns app6, controls16 and collateral reruns.

All local attempted result/log directories are enumerated by f05b-executor-manifest.v1.json, including failures:

- runtime-001: test compatibility mechanical replacement caused SyntaxError before methods ran; failing whole test snapshot retained as f05b-executor-runtime-compat-attempt-001.py. Corrected only the four intended fixture calls; runtime-002 and final runtime-003 PASS4.
- focused-001: 12 PASS, 1 assertion FAIL because rejection reason changed for the legacy no-graph/producer-binding case. Restored the old error code in the unversioned Validator branch without changing that gold; focused-002 PASS13.
- path-red-001: two genuine assertion FAIL, zero ERROR, after static directory symlinks were accepted by capture. Gold 9eb71ea7c3218578c6ed77a78da8cff78557fae16ae64ee9423e52717ae39168; original log 8fccb73c034a699af19c0ba825202d207721d956a3db86695497c9aa3fa8498f. Original bootstrap before this fix is retained. Same gold path-green-001 PASS2; final E2E17 and runtime4 prove normal fixed directories remain usable.
- Earlier preaudit code consistency changes: projector new authority-combination CLI gate now raises ValueError, matching existing validation entry behavior and root frozen oracle; capture oversize failure explains configurable limit without truncation. These are recorded preaudit review changes, not formal repair round resets.
- Delta serialization v1 accidentally trimmed final space-only context lines in 3 diffs, causing read-only reverse-check errors. Original files/checks remain. Corrected v2 diffs preserve exact context; all 11 reverse checks PASS. The separate read-only inspector reconstructs original bytes and matches all 11 original hashes exactly. No source was modified by reverse checking.

Original gold26 is AST-identical after removing only two new methods; exact inverse returns original gold bytes. v2 adds nonregular snapshot control; v3 adds a literal post-read replacement of all three synthetic inputs while returned payload remains bound to original bytes. Neither new-API absence nor TypeError was counted as semantic RED. Initial six-method app RED belongs to root.

## Safety / rollback

Only synthetic tmp data and stub inference were used. No shared user decisions/originals, real network/model/process/GUI, credentials, installation, commit, push or production generation was accessed. Guarded child deadline30s and log1MiB; pure serializer explicitly counts <=1MiB, <=8KiB decisions, <=64KiB graph, <=16 records /16KiB inventory. Inherited compatibility runner confines writes and limits source write_text; it does not independently count every explicit fixture byte or product artifact. Parent's strengthened budget acceptance runs cover their designated authored fixtures; do not turn this into a whole-process resource guarantee.

For rollback, inspect current hashes and apply only the recorded 11 isolated inverse deltas; preserve unrelated dirty-tree changes. Exact inverse checks are in f05b-executor-inverse-result.v1.json. Root owns README/status and its separate inverse. Never roll back by replacing the repository or resetting the worktree. A previously published CONFIG/index is preserved on tested failed builds; immutable historical files and all failed evidence remain.
