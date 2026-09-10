# F11a integration regression correction — gate 1

2026-09-09 15:30 JST. Same F11a task, not a renamed work item or acceptance. Parent reviewed both full diagnosis documents and both fixed gold sources. Existing v1 product packets and all failed runs remain immutable historical snapshots. This is the first integration correction round; formal artifact audit has not started. Maximum two correction rounds remains binding; do not evade it by naming errors separately.

## Recorded pre-repair results

- Original root boundaries: 4 PASS in f11a-root-green-001.
- Actual app regression: f11a-root-app-001, 1 PASS and 3 ERROR at initial non-Notebook structural relation attestation.
- Existing image regression: f11a-root-images-001, 2 PASS and 1 ERROR at VLM provisional text classification.
- New app gold SHA55dc6665d9fbfc9bd6721898ea7be73606560f8a7d0e2fc61e9a05839c222296: f11a-regression-app-001, 9 methods; 4 failing methods with 8 semantic assertion/subtest failures, 5 PASS, zero errors/skips. Log4650627a049baeb447cee09aa60286e32e920e58831972a8ec6eb84044abbc76.
- New image gold SHA75573e3ba7ea4a464b20feaf09b6a9e4d4a9133ceabe519399785f9638046913: f11a-regression-image-001, 3 methods, 2 assertion failures plus 7 errors including subtests, zero skips. Normal producer VLM baselines fail; the later forgery cases were not reached and are NOT defended. Logdfa209febe47a573e7111eae819b2fb60df34031601acb9ee9f51412d8089b46.

The new runner f11a-regression-run.v1.py selects only the literal gold methods, uses existing F04 guard, F05b explicit write budget, fresh run directories, 30-second and 1-MiB log ceilings. App used installed3.14, image installed3.9 with jsonschema. No real models, original documents, network, package build, or public index. Guard is not an OS/RSS sandbox.

## Released edit boundary

Executor f05a_executor owns ONLY distribution/macos-local-memory/engine/build_local_semantic_index.py for three exact0.12.0 additions: native tuple, SmartArt tuple, SmartArt support version set. Before95b44b327fb0e0c1e747c9d244b077950ebf5b61dd0a8bf56b33b1a6c0e163da preserved in f11a-app-regression-before-index.v1.py (verify actual prepared filename before use). Do not weaken source, relation, security, support comparisons or broaden arbitrary versions. Allowed runs: app gold fresh002 and original app4 fresh002 with reviewed wrappers. Stop for unexpected failure; save new delta/results without replacing v1 manifests. Rollback only exact new inverse delta against its repaired hash, never a Git reset.

Root owns image design review and status. Reviewer f03a_independent_audit is preparing f11a-image-repair-design.v1.md only, no product changes/runs. Image edit gate remains closed until parent lookup, applicability, metadata counting and streaming resource behavior are specified. Preserve VLM partial/provisional state; do not fabricate notebook_state or opt out merely on self-declared method.

Protected three user files and frozen plan hashes match at15:30. Saved runs have no unfinished started.json. Both old agents were completed before new bounded assignments. OS-wide process inventory was previously denied; no all-process claim. Sleep assertion PID17714 was actually active15:28:12 with678seconds left; it expires around15:39:30 and is not permanent.

Whole F11/V1, body binding, membership, freshness, F11b and package/real-model acceptance remain open. No commit/push/release.
