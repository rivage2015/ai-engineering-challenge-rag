# F05 next preflight: stored candidate graph is not an inventory proof

2026-09-09 08:39 JST. Root investigation only. F03a product sources remain frozen for a separate auditor. No F05 product implementation is authorized by this document.

## Fixed observations and scope

Use only two tiny synthetic inventory records: `Guide_ver1.csv` and `Guide.csv`, valid-looking observed document entries with fixed hashes/timestamps. The actual F03a resolver build must produce a held mixed group. No source document is present or read; this is a resolver inventory/JSON boundary test, not full Path-source attestation.

After that normal build, create separate synthetic graph copies and recompute their self hash:

1. Empty groups/nodes/edges and zero counts.
2. Turn the held mixed group into a fabricated automatic active selection and consistent projection.
3. Replace a candidate with an absent path/hash, then recompute set/projection/self hashes.

Expected current gap observation: each graph passes `document_version_resolver.validate` against the unchanged synthetic inventory, because that validator checks inventory-file hash, graph self hash, node endpoints and basic active count but not source-reconstructed candidate membership or the actual decision. Passing these observation assertions reproduces a missing validation boundary; it is NOT acceptance of the forged graph and not an app exploitation or full F05 proof.

## Limits and safe execution

Root may create only new `runs/f05-next-*` files. Invoke actual builder/validator functions on a tiny owned temporary directory under the existing F04a guard, with the already reviewed F03a/F01 in-process dispatch scaffold. No Reader/model/GUI/network/workers beyond the single supervised Python child; 30-second wall and 1 MiB logs. Fixture inventory under 16 KiB and graph outputs under 64 KiB each, assert original inventory bytes unchanged. The inherited guard is a Python test aid, not an OS or total-RAM/disk boundary.

Before source is frozen F03a resolver SHA-256 `c2b98254ec28a82e8cc7b5ef3f5e780739bcc1b6983ef2a9252609156d4b672f`. Validate this exact hash during the observation. No production CONFIG/index, source data, credentials, protected files, commit/push or source edits. No rollback needed; preserve all observations for a later immutable implementation contract.

## Future contract questions, not decisions to implement now

- Source-reconstruct expected family membership and bound candidate fields from explicitly supplied inventory; reject omitted, additional, duplicated or changed candidates even if all stored hashes were recalculated.
- Structural candidate completeness and valid Human intent are separate obligations. A validator must not trust a graph-provided decision-file path to fetch new authority. Any Human-decision attestation requires an explicit trusted input/binding and a caller/CLI contract.
- Keep F03a exact family boundaries, year hold, all-marked controls, full-set Human/stale semantics and old-generation preservation. Do not equate matching candidates or hashes with actual document identity, freshness or correct supersession.
- Untrusted JSON shape/duplicate keys, malformed groups and counters, source-root/freshness, F06/F19 generation/lease and publication remain distinct required checks. No global F05 closure from one membership comparison.
