# F11a integration regression correction 1 — index identity compatibility

## Authority and outcome

Root authorized only three exact `0.12.0` additions to `distribution/macos-local-memory/engine/build_local_semantic_index.py` after reading the frozen diagnosis/gold and obtaining the predicted semantic RED. This is F11a integration regression correction **1**, not a renamed issue, formal audit acceptance, or executor self-approval. Adapter / Agentic Audit required the before check, minimal isolated delta, immutable original evidence, bounded verification and separate acceptance handoff.

Only the index product was edited. The independent regression nine-method gold and unchanged root app four-method gold now both pass, with zero errors, skips or expected failures. In particular, the original three app methods now pass the initial non-Notebook build and reach their Notebook UNVERIFIED gate / migration / rebuild assertions. No unexpected failure occurred during these two authorized runs.

## Exact isolated change

- Add `("intermediate-record-extractor", "0.12.0", "native containment")` to `NATIVE_CONTAINMENT_PRODUCERS` (current line 44).
- Add the `0.12.0` tuple with the unchanged `native SmartArt srcId/destId connection` rule to `SMARTART_CONNECTION_PRODUCERS` (current lines 57–61).
- Add `0.12.0` to the existing graph-side SmartArt support verifier's version set (current line 689).

No producer names/rules, old versions, endpoint/support checks, relation-ID construction, full-set source attestation comparisons, security checks, Notebook handling or app logic were loosened. The original ten F11a products and all three gold files remain unchanged at this correction checkpoint. Probe/Search were not edited; root owns the separate image regression correction.

Before editing, `cmp` of the current index and `f11a-app-regression-before-index.v1.py` succeeded and both SHA-256 values were `95b44b327fb0e0c1e747c9d244b077950ebf5b61dd0a8bf56b33b1a6c0e163da`. After editing, a full-text mechanical comparison applied only the three literal replacements to before bytes and obtained exactly the current bytes; the reverse replacements obtained exactly the before bytes. `cmp` of the current product and newly saved after snapshot also succeeded. This check executes no product code.

## Durable attempts — originals retained

All paths below are relative to `design/local-memory-v1-hardening/runs/`. Each directory retains `started.json`, full `unittest.log`, and `result.json`; neither original RED nor original app ERROR was overwritten.

| Run | Actual outcome | Elapsed supervisor seconds / log bytes | Log SHA-256 | Result SHA-256 |
| --- | --- | --- | --- | --- |
| `f11a-regression-app-001` (root before correction) | 9 methods; 8 semantic assertion/subtest failures across 4 methods, 5 passing methods; 0 ERROR/SKIP | 0.149866 / 11734 | `4650627a049baeb447cee09aa60286e32e920e58831972a8ec6eb84044abbc76` | `1dff7f0c4641b8e8f64a0d3c2cefb44ab10cb33d40649d4bb6d65fa43122a1e9` |
| `f11a-root-app-001` (root before correction) | 4 methods; 3 initial-build ERROR, 1 PASS | Original result retained | `25739d107455264f0a0ddc9ae3f739c706356bd83d1b80cba82ec02bda8c7ef3` | `71135a9bd898e78e0e2290f5e5435281b7983ceb08de5eac1bd903397b317499` |
| `f11a-regression-app-002` (this correction) | 9 PASS, 0 ERROR/SKIP/expected failures | 0.136989 / 1733 | `ba78147e4da20a28f22096bd46eb305aa229401fb77f91c4deeea336ce14b624` | `3ec9fa37fea523b164fd71f9e706c38daa28b3da7554dfa4e1d00cc13f32ea01` |
| `f11a-root-app-002` (this correction) | 4 PASS, 0 ERROR/SKIP/expected failures | 0.711900 / 1009 | `6deedf2d51f0c478f50672815f4720ee4290d4f3c46565cadda050da8f7824b2` | `2025bbc1d11b8bcccae145a823bba377dd79f1c99962e43afd53056241d961bf` |

Executed, from the repository root, after reading both complete runners and their budget/supervisor dependencies:

```text
/opt/homebrew/opt/python@3.14/bin/python3.14 -I -B design/local-memory-v1-hardening/runs/f11a-regression-run.v1.py app f11a-regression-app-002
/opt/homebrew/opt/python@3.14/bin/python3.14 -I -B design/local-memory-v1-hardening/runs/f11a-root-app-run.v1.py f11a-root-app-002
```

Both use one supervised worker, 30 seconds and 1 MiB log ceiling, reviewed F04 temporary-file/read/network/process guards and F05b explicit fixture budget. The pure run records zero explicit fixture writes. The app run records 265 explicitly authored bytes / 10 writes, three source-fixture roots with maximum 137 bytes. Ordinary product artifacts are confined to temporary roots by the guard; these counters are **not** total disk/RSS/process isolation guarantees. No network/model/GUI/real-document/production-state operations, dependency installation, package build, commit or push were performed.

## Frozen files and remaining limits

| Artifact | SHA-256 |
| --- | --- |
| Current index / `f11a-index-regression-after.v1.py` | `c70f36d98012cca29877af72e4d345c30a555e1fef24e34ebc057b4f823e7229` |
| `f11a-app-regression-before-index.v1.py` | `95b44b327fb0e0c1e747c9d244b077950ebf5b61dd0a8bf56b33b1a6c0e163da` |
| `f11a-index-regression-delta.v1.patch` | `3c44582b9a06ee7570664d687766b413dc5430a7aec2d56c411bf5ea833523da` |
| `f11a-app-regression-gold.v1.py` (9 methods, unchanged) | `55dc6665d9fbfc9bd6721898ea7be73606560f8a7d0e2fc61e9a05839c222296` |
| `tests/test_notebook_metadata_application.py` (root 4, unchanged) | `b254860584650f376400a50d3890b9cc6074633427a565787b91aae11965496a` |
| `tests/test_notebook_metadata_binding.py` (original 33, unchanged) | `ab2256c31b04bc8e8ec9a3c5c294d1e1d98071e38e9969c62bee3718c95bc41e` |
| `f11a-regression-run.v1.py` | `12969e040d7b5797dfbaf9fe39e99d7b2b44d0ad5945a0c09c301d0597d813e6` |
| `f11a-root-app-run.v1.py` | `f5a9e437fc0f28474eb24ceca8abb43c70634302710159aa056a2938c2e18539` |

`f11a-index-regression-coherent.v1.json` pins all eleven product hashes at the correction checkpoint, the new before/after/delta, gold, runners and durable runs. The original F11a v1 packet remains immutable historical evidence. The original 33-method metadata suite was **not rerun** in this correction; its hash is unchanged, and its previous results must not be presented as fresh verification. Full source-bound SmartArt pipeline, broad collateral and independent audit remain for root-authorized follow-up. The separate image regression is not closed here. Metadata-only limits, body/membership completeness gaps and mandatory F11b follow-on remain open.

Rollback, if root requires it: verify the index still has the exact `c70f36d9…` hash, then apply only the inverse of the isolated delta and require the resulting full bytes/hash to equal `95b44b32…`. Preserve all other worktree edits and every original snapshot/gold/result. No rollback was performed.
