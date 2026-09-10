# F11a scope decisions — preimplementation, 2026-09-09 13:04 JST

Status: design decisions, not a frozen executable task contract and not product acceptance. Root read all 167 lines of fresh-preflight v1 (SHA c269fb2f91584b78130969fe0fbddc8d18e05e9bbe5c00a2bcde0ec5b420d056). The executor's stronger body-binding recommendation is intentionally deferred, not implemented by implication.

## Selected narrow invariant

F11a will compare the metadata of each presented textual Notebook Evidence with the declared cell/output in the exact source bytes used for its Document digest. The report section must be named `notebook_metadata_binding`, not the broader `notebook_source_binding`. State provenance, body fidelity, membership completeness and freshness are distinct claims.

The machine-readable report must explicitly mark raw-text binding, complete Evidence membership and output freshness as **not verified by this check**, even when metadata is verified. Exact keys and complete report gold remain to be frozen. No consumer may interpret this result as complete source attestation. Saved execution counts are observations, not proof of execution order or freshness; null does not mean never executed historically.

SearchUnit checks only compare this metadata to their referenced Evidence, including same-ID context alteration and omission. They do not independently attest original source bytes. Full Evidence omission, SearchUnit membership, body re-signing and the application answer path remain explicit open work. F11b propagation into shards/index/retrieval/answer display is mandatory before claiming F11 complete.

## Compatibility and safety decisions

- Both intermediate validators should expose `validate_report`; existing `validate` returns its unchanged three-key counts only on PASS. UNVERIFIED must fail that wrapper and cause CLI nonzero, because the adaptive caller currently interprets CLI success as pass.
- `not_applicable` is allowed only when there are no Notebook Documents, not when the artifact's parser/locator/state is absent. Notebook Documents with zero textual records are still applicable and need a separately frozen coverage result, not a fabricated no-Notebook success.
- Missing originals, genuine partial/failed/unparsed extraction and attestation over-limit are UNVERIFIED. This can hold a mixed import job; preserving partial raw text does not imply index eligibility.
- Missing code/execute-result execution counts should remain observable as absent and cause UNVERIFIED, rather than fabricating null or zero. Invalid typed values and state/source mismatch are explicit failures. Missing optional stream/display counts are ordinary absent facts.
- Keep pure shared helpers in the already shipped Probe module; do not edit protected packaging files. A helper shared by producer and validators needs independently written literal gold, not helper-derived expectations.
- Do not parse and hash different reads. Streaming must not re-open after hashing to derive metadata. No global snapshot/race guarantee is claimed.

## Must resolve before product ownership

1. Freeze the exact report fields, status/reason precedence (including empty legitimate Notebook versus omitted records) and all literal positive/negative gold.
2. Freeze an attestation byte/node/depth policy that limits parser amplification, plus fixture budget and reviewed runner. The current 64 MiB ceiling alone is not a heap bound. No resource acceptance has been established by this preflight.
3. Freeze the ten-file product ownership list and before bytes, version pins, old-generation rejection and package-limitation tests. Do not overwrite old artifacts or claim checkout streaming tests certify the shipped streaming CLI.
4. Read independent `f11a-contract-review.v1.md` when saved. Then true semantic RED, minimal implementation, regressions and formal independent audit, at most two formal repairs for the invariant.

## Evidence and tool limitations

Fresh observation002 ran two observation methods/six validator calls with zero skips and reproduced the existing metadata-validation gap. It is not an implementation acceptance test. Receipt: `f11a-fresh-observation-result.v1.json`, SHA b2071444f339ef3f1d1459f18e10ad132c514baececddaf1f06e872bf7f8bbe4.

Root checkpoint syntax check first tried macOS plutil, which rejected JSON as a plist (exit 1); no file was changed. A read-only Ruby JSON.parse check then succeeded and verified status=active. This was a tooling mismatch, not a product test failure. `git diff --check` also passed. Preserve these distinctions on resume.

A read-only hash comparison first used Ruby `filter_map`, unavailable in the installed Ruby (exit 1); rerunning with `map.compact` succeeded for all 13 scripts/schemas/distribution entries in the preflight inventory, including its protected build and migration test entries. No product or test bytes were edited by either command.
