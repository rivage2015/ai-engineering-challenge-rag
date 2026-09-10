# F11a executor coherent handoff v1

Status: **executor implementation and bounded local checks complete; awaiting root controls and independent audit, not self-approval**. Frozen metadata-only contract/addendum and implementation gate were followed. Formal product audit repairs: 0. Root owns app/migration/package/mocked-image controls and acceptance routing. F11b, body binding and complete membership remain required open follow-ons.

## Implemented boundary

Only the ten assigned product files changed. Probe now reads at most the 8 MiB metadata ceiling + 1 byte, lexically checks tokens/depth/number length before JSON parsing, rejects ambiguous/malformed JSON and typed facts, and derives Notebook metadata plus Document digest/length from the same read snapshot. Limits retain the existing explicitly partial raw-text fallback; missing execution counts remain explicit absence with a warning and UNVERIFIED metadata binding. No notebook code/kernel/model/visual execution was added.

Both intermediate validators expose the exact `validate_report` API. The old `validate` returns exact original counts only on PASS. Rootless, incomplete, resource-limited, missing-count and zero-checked Notebook cases return the frozen machine-readable UNVERIFIED report; known malformed/mismatched state still fails. Original-byte metadata checking processes one Notebook at a time; the streaming implementation queries its existing SQLite Evidence storage instead of retaining all Notebook sources. Ordinary non-Notebook source checks remain as before. CLIs produce PASS/0, UNVERIFIED/2 and bounded FAIL/1; streaming retains its schema label.

Direct SearchUnits copy only strict Notebook state. Both Search validators compare type-aware state/role/locator to referenced Evidence, including same-ID context mutation. This is **Evidence-bound** comparison, not standalone original attestation. No raw-text reconstruction or full expected-member enumeration was added. Probe/managed/Search versions and the two Search/adaptive pins were updated exactly as contracted; adaptive changes are only its Search version pin plus managed 0.12.0 tuple, preserving historical tuples. Strict optional schema fields complement runtime mandatory applicability checks. Helpers are inside already shipped/fingerprinted Probe; protected package/app/index/answer/README and existing test files were not edited.

## All executor functional attempts (no hidden reruns)

| Run directory | Result | Explicit fixture bytes / writes | Log SHA-256 |
|---|---|---|---|
| `f11a-executor-red-initial-001` | 9 methods: 8 semantic FAIL, 1 PASS; 0 error/skip | 9642 / 10 | `c2fbfa10d2f6ba4815a6ce7c07064ce01a939e64e47c73fd89ec4989afd42f6d` |
| `f11a-executor-green-initial-001` | 9 PASS; 0 error/skip | 9642 / 10 | `96eebb75872e63aafc87b264444858cde518d7155133d64451c5bdcc5c0c493e` |
| `f11a-executor-post-green-001` | 21 PASS; 0 error/skip | 69927 / 68 | `77d83a1467ae695659d11347374d5c9ed2eb812d174b7d9a251d31f999e18828` |
| `f11a-executor-residual-green-001` | 3 residual witnesses observed; 0 error/skip | 3584 / 4 | `f501feeb38005bdbcd2bb1a134c478a7cb4079da2cc1d9f5463ba648773ae1c0` |

All directories are under `design/local-memory-v1-hardening/runs/`, with immutable started/result/log files. All four full logs were read. The 30-second/1-MiB supervisor and reviewed no-external-I/O guard were maintained; each batch finished in under a second. Fixture source maxima stayed below the injected 16 KiB per-case guard (post maximum 9222 bytes). Budget counts explicit authored fixture writes, not all product output/RSS. There were no preaudit failed GREEN runs or test-fixture corrections. Original nine RED failures were semantic, not missing APIs or import errors.

The 3 residuals demonstrate coherent body replacement, nonzero record omission, and Search-only coherent facts forgery can still pass their deliberately narrower checks. They are **not 3 defended attacks**, nor may they be added to the 30 acceptance methods as a completeness claim. The latter coherent forgery is rejected when the separate intermediate original metadata check runs.

## Immutable freeze and rollback evidence

The current source packet is `f11a-executor-coherent.v1.json` SHA `fa13e1e5be75b0d4b908d247a8e00abaeedd0c84966f1072d28602a327c9798c`. All three GREEN/residual runs use those same ten hashes. `f11a-executor-freeze-check.v1.py` was run only as read-only AST/JSON/delta processing, with result `f11a-executor-freeze-result.v1.json`: all ten forward deltas reproduce current bytes; all ten inverse deltas reproduce exact before bytes; all coherent hashes remain unchanged. Removing only the additive test class reproduces the entire initial nine-method AST, and permanent test bytes equal gold v2 SHA `ab2256c31b04bc8e8ec9a3c5c294d1e1d98071e38e9969c62bee3718c95bc41e`.

Every before source and unified delta is new/owned `f11a-executor-*`. They include preexisting accepted dirty work, not a HEAD reconstruction. A separate additive gold v2 diff preserves original v1; no original gold/test method was changed. The manifest uses `files`, `runs`, and `isolated_deltas` for parent verification. Rollback, if requested, means only these ten reviewed inverse deltas, never resetting the shared tree or changing original/public generations.

Skills used: Codex Graph Engineering Adapter and Graph Engineering Agentic Audit. They required the explicit before/gold/RED gates, separate residual accounting, retained evidence and independent acceptance. This handoff is not the separate formal audit, not packaged streaming Search support, not a production performance/RSS result, and not completion of full F11 or Local Memory V1.
