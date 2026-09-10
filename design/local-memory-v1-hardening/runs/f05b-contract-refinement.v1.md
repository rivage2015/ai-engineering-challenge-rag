# F05b contract refinement — proposal, not implementation

2026-09-09 10:43 JST, root. This supplements `f05b-snapshot-preflight.v1.md`; it does not change that historical observation or the frozen F05a contract. F05a is in separate-context audit at artifact `7b7414f3e58a62b6eb34fc71534da11eec51c1dd58c316326ba03e9ab436bf61`. No F05b product edits or tests have run. Freeze final ownership/before hashes/acceptance gold only after F05a is accepted.

## Outcome to preserve

A single new application generation uses one explicitly captured decision snapshot throughout version resolution, Reader selection, validation, projection and registration. Its Reader input list must be exactly the eligible result reconstructed from the full explicit inventory and that snapshot. Matching self-hashes and absence of held documents alone are insufficient. A later change to the shared decision file does not change this generation; a subsequent explicit build captures a new snapshot.

This is not a claim that the selected Human choice is authentic or latest, that family classification is correct across renamed documents, that the original source tree is still current, or that every filesystem race is prevented. F06/F19 publication revision and Human/root identity remain separate required work.

## Additional issue identified in current source

`validate_adaptive_semantic_graph.py:2636` checks that manifest paths belong to inventory and do not intersect submitted held/historical paths. It does not reconstruct the full expected Reader selection and compare the whole ordered manifest. Thus rejecting held-member insertion and proving no eligible-member omission are different conditions. This is static evidence of a missing check, not a tested successful app attack.

`build_adaptive_semantic_graph.py:170` already provides `select_inventory`, with sensitive/generated/unsupported/unobserved selection reasons and NFC ordering. Reuse the current deterministic policy rather than invent a second allowlist. The version resolver candidate set and the Reader-supported set differ: full inventory must enter version reconstruction before supported-reader filtering. Legitimate partial parser output must not be confused with omission of a file from the intended manifest.

## Proposed boundaries to freeze

1. Resolver internal attestation reads explicit graph/inventory/decision bytes once, strictly parses and validates them, and returns the verified graph selection, inventory records, raw digests and detached binding from those same reads. The existing standalone `validate()` shape remains compatible by wrapping that result. It must not validate then reopen graph/inventory/decisions for selection or compute an expected digest from the file being verified.
2. Reader applies its existing selection policy to those returned inventory records, then verified dispositions. Persist exact expected manifest order and selection counts. Validator independently recomputes the same result from its explicit inputs and rejects missing/extra/duplicate/reordered manifest entries and changed count fields, while retaining existing source/lineage checks.
3. All versioned consumers explicitly declare decision authority. New app generations require the fixed snapshot path and capture-time SHA-256; missing either is failure, not implicit no-decision behavior. Generic standalone no-decision validation remains distinct. If supporting a no-decision versioned CLI, make its deliberate mode explicit so omission cannot downgrade a new application generation. Decide this signature before editing mocks.
4. Bootstrap alone captures decisions after Path validation and before resolver build. Existing shared file: bounded single read, strict parse, exact byte copy. Only genuine initial absence produces canonical empty payload. Permission/read/JSON/duplicate errors fail. Snapshot creation is exclusive, refuses symlink/directory/existing files, does not write shared decisions, and never repairs a saved generation. No permission escalation, OS immutable flags or global locking claim.
5. Pass one detached descriptor through every CLI, both semantic builds including the model-ready branch, and index lineage/security validation. Preserve strict allowed-context key sets. A bare PASS report or producer metadata must not supply missing authority.
6. Register fixed snapshot identity in the Reader generation contract and its existing CONFIG binding. Read only the expected generation-local location, never an arbitrary producer JSON path. Compare the stored path string before path operations. Existing versioned generations lacking the required snapshot report rebuild/migration; do not copy current shared decisions into them. Preserve old files and CONFIG/index on failure.
7. Use the verified detached binding through security partition, projection report and SQLite metadata. Do not reread producer state after attestation to replace that binding. State the remaining race and multi-file generation limits instead of extending this local guarantee to all artifacts.

## Fixed gold required before implementation

Use bounded synthetic CSV version candidates plus unrelated contact evidence; normal known-answer query/final audit must still work. Each new fixture's expected result precedes the run and retains original failure logs. New-argument TypeError is not semantic RED.

- New app generation has fixed empty/actual snapshot and the same digest throughout graph, Reader, Validator result, index metadata and generation registration. Current missing snapshot yields a direct assertion RED.
- Shared D0 → D1 change after capture leaves the first generation on D0. Next explicit generation uses D1. Changing/removing/corrupting shared data must not cause an old generation to reopen it.
- Swap/reseal a false graph after initial resolver gate; reject before reading the falsely active source or, at minimum, before projection/publication as explicitly specified per injection point. Preserve a prior prepared valid CONFIG/index. Initial-gate rejection does not count as downstream rejection.
- Remove an otherwise eligible unrelated file from manifest and coherently update producer counts/hashes; full expected-selection check rejects it. Keep a legitimate unsupported/sensitive/unobserved fixture excluded by its original policy as a false-positive control.
- Missing/changed snapshot and supplied expected-hash mismatch fail; never fall back to the shared file. Copying identical bytes from another generation is not cryptographically distinguishable solely by SHA-256, so test trusted path/generation identity separately and do not claim byte provenance from equal digests.
- Graph/state metadata canaries are not read/stat/resolved. Explicit trusted paths may be read; distinguish these two roles in test guards. Check alternate generation, shared path, `..`, symlink and directory cases without source-data access.
- A simulated second read returns different bytes; selection must use the first verified snapshots. Input-read counters cover inventory too, not only graph/decisions. Separate validation invocations may reread; each invocation must bind its own result.
- Valid Human selection of either member remains possible. New candidate or selected/nonselected source content change invalidates stale choice on next build. Annual and mixed-set holds remain; the known annual-retention/cross-key problems stay recorded, not silently reclassified as desired behavior.
- Legacy unversioned controls remain; old versioned generations return explicit migration. Model-ready second Reader receives the same descriptor under stubs, with no model download/inference.
- Failure keeps prior CONFIG/index/source bytes and saved lineage unchanged. Snapshot-create failures do not truncate an existing target. Read-only verification creates no snapshot or repair output.

## Implementation ownership and verification proposal

Five product files from the preflight: resolver, adaptive builder, adaptive validator, index projector, bootstrap; README and only signature-affected tests. No server/Keychain/build-package/protected-doc edits. Assign a single product owner for mutually dependent API changes; root owns separately named app tests and audit orchestration. Do not start multiple editors on shared validator/Reader files.

Keep focused runners synthetic, one supervised child, 30 seconds and 1 MiB logs. Source test fixtures <=16 KiB, decision fixtures <=8 KiB, graph fixtures <=64 KiB, <=16 inventory records and <=1 MiB explicitly counted serialized fixture writes. These are test budgets, not invented production file-size limits. Before choosing any production decision-file cap, inspect existing UI/request limits and large legitimate decision-store behavior; document a clear fail reason rather than truncating or dropping old decisions.

Regression list: F05a pure and immediate gate, resolver/Reader controls, app safe-index E2E, F03a unmarked app/pure, F02a annual controls, immutable-lineage, migration, focused relation/security tests, and the bounded runtime-recovery/model-ready fixtures whose actual side effects have been reviewed. Do not run all package/runtime tests without that review. Formal separate-context audit and parent schema/hash/log/delta validation are mandatory; maximum two repairs for the frozen invariant.

Current source readings for this refinement: `build_adaptive_semantic_graph.py:170,192,328`, `validate_adaptive_semantic_graph.py:2558,2636`, `build_local_semantic_index.py:83,834,1240`, `bootstrap.py:434,2165,3562`. Their hashes are in the F05a artifact's READER/VALIDATOR/PROJECTOR/BOOTSTRAP entries and will be rechecked after audit before final F05b freeze. This proposal is not a test result or permission to bypass F05a acceptance.
