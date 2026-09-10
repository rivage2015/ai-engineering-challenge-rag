# F11a app regression diagnosis v1 — read-only, not repair approval

## Scope and result

Root requested diagnosis of the three app errors in `f11a-root-app-001`, not the separate image-mock error. Adapter / Agentic Audit role separation is retained: this executor inspected code, frozen before/deltas and existing logs; it did not import product modules, execute tests, or edit product/test/frozen artifacts in this diagnosis. This new document is not an audit PASS or implementation authorization.

The likely common cause is a missed downstream producer-identity compatibility update. F11a changes the managed extractor identity from `0.11.0` to `0.12.0` for every format, and the adaptive Validator accepts/reconstructs that identity. The unchanged index projector still recognizes native containment and SmartArt producers only through `0.11.0`. It therefore filters valid new native relations out before exact comparison against independently reconstructed relations. Relation-ID generation itself remains coupled correctly to the new version on both producer and Validator sides.

## Existing runtime evidence versus static inference

- Existing app log: three ERROR, one PASS. All three errors reach `build_local_semantic_index.py:964`, `graph_structural_attested_relations_mismatch`, through `main:2657` → `project_verified_structural_graph:2035` → `_attest_security_context:1308` → `_attest_lineage_context`.
- `tests/test_notebook_metadata_application.py:33`, `:97`, `:113` all fail at the initial non-Notebook `bootstrap.build_index()`. The first test has not yet written its Notebook at line 40. Notebook UNVERIFIED stopping, migration protection, and explicit second-generation rebuild assertions are consequently not reached in these three methods. The package-copy method passes.
- The static explanation below is consistent with the exact failing comparison. This diagnosis did not retain/re-execute the temporary fixture or inspect its runtime relation dictionaries, so it does not claim a newly measured count or exact observed relation set.
- The independent root Notebook controls being GREEN does not erase this app regression. The original three ERROR outcomes and frozen F11a v1 sources/gold remain historical evidence.

## Identity coupling and failure path

| Boundary | Current source reference | Observation |
| --- | --- | --- |
| Managed producer | `scripts/build_intermediate_records.py:52`, `:1076–1097` | `EXTRACTOR_VERSION = "0.12.0"`; this is passed to Probe for all file formats, not only Notebook. Before snapshot was `0.11.0`. |
| Relation creation | `scripts/probe_intermediate_records.py:3035–3071` | The relation identity includes generator/version at `:3043–3050`; provenance carries the same version at `:3061–3067`. Native containment starts at `:3073`. |
| Independent Validator | `distribution/macos-local-memory/engine/validate_adaptive_semantic_graph.py:111–116`, `:638–674`, `:1025–1115` | The allowlist includes `0.12.0`; reconstruction uses build-state extractor/version and checks Evidence extractor/version against that state before rebuilding the relation identity/provenance. F11a's delta adds the tuple, not a new relation-ID formula. |
| Projector native classification | `distribution/macos-local-memory/engine/build_local_semantic_index.py:39–44`, `:586–606` | Native containment tuples stop at `0.11.0`; `0.12.0` returns false from `_is_explicit_verified_structural`. |
| Exact comparison | same index file `:940–964` | Independent relations are reconstructed at `:951–953`; supplied relations are filtered using the stale classification at `:958–962`, then full dictionaries are compared at `:963–964`. A nonempty new managed native relation set is thus excluded on only one side. |

The earlier source/Evidence/lineage validations in `_attest_lineage_context:881–938` do not throw in the supplied stack; the mismatch is at the structural comparison. Do not “repair” it by weakening that comparison, deriving authority from embedded producer claims, or reverting the new extractor identity.

## Other exact producer consumers in this bounded code search

The search of `scripts/` and `distribution/macos-local-memory/engine/` for literal `0.11.0` / `0.12.0` found the managed identity, adaptive allowlist, and these three projector pin sites; it is not a proof about unsearched external code.

1. `NATIVE_CONTAINMENT_PRODUCERS`, index `:39–44`: add the exact `("intermediate-record-extractor", "0.12.0", "native containment")` tuple.
2. `SMARTART_CONNECTION_PRODUCERS`, index `:45–56`: add the exact `0.12.0` tuple with the existing `native SmartArt srcId/destId connection` rule. The managed version bump also applies to PPTX SmartArt.
3. `_validate_attested_smartart_connection`, index `:683`: independently requires a generator version in `{0.10.1, 0.11.0}`. Adding the tuple alone would leave valid `0.12.0` SmartArt rejected here; the same exact version must be added while preserving all support checks.

`EXPLICIT_STRUCTURAL_PRODUCERS:57–59` is their union; this search found no additional direct consumer. `_is_explicit_verified_structural` is used at `:961` (source-attested full-set comparison), `:1152` (security partition), `:1531` (promoted relations), `:2056` (context requirement), and `:2310` (published edge eligibility). Updating its exact underlying tuples consistently covers these call sites; none of those checks needs relaxing.

## Smallest proposed repair and regression scope — not applied

The observed text/CSV app regression needs the native tuple. The minimum complete all-format compatibility repair for this global version bump is one additional product file, `build_local_semantic_index.py`, with exactly the three producer-version additions above. This file was outside the frozen original ten-file F11a edit scope: root must explicitly approve a scope addendum and preserve its current before bytes/hash before repair. Keep all historical versions, names/rules, deterministic/status/type constraints, relation identity checks, full-set equality, source attestation, security partitioning and SmartArt Evidence support validation intact. This is identity compatibility, not F11b Notebook-state propagation into app presentation or index content.

Proposed additive literal regression controls:

- `0.12.0` native `contains` and `section_contains` are recognized; historical `0.7.0`, `0.8.0`, `0.10.1`, `0.11.0` remain recognized. Unknown/fabricated versions, generator names, rules/types and non-verified/non-deterministic records remain rejected.
- `0.12.0` SmartArt exact producer classification and the existing full graph-side support verifier accept a bounded coherent synthetic fixture; prior `0.10.1` / `0.11.0` remain accepted. Unknown version and mismatched support/raw endpoints remain rejected. This synthetic graph-side check is not itself source authentication.
- Re-run the unchanged root app four-method gold after authorization, requiring the first three to reach and satisfy their actual later Notebook/migration/rebuild assertions, not merely clear setup.
- Existing `tests/test_local_graph_index_schema.py:515–534` calls its fixture “current” but hardcodes `0.11.0` at line 519. Preserve that historical control; add `0.12.0` rather than rewriting it to hide the compatibility gap.
- Existing `tests/test_local_embedded_visual_pipeline.py:1078` (`test_pptx_smartart_text_and_raw_connections_reach_search`) uses the real adaptive build at `:1177`, graph-side positive support verification at `:1269`, and tamper rejection at `:1280–1298`, then security/index projection. It is a useful later integration regression; its complete fixture/dependency/resource side effects must be reviewed and bounded before execution. No such run was performed here.

Rollback proposal: save a new byte-exact before snapshot of the index file, freeze the new delta, and if rejected apply only that inverse delta against the exact repaired hash. Do not reset the worktree or mutate original F11a v1 snapshots/gold. Existing image error remains a separate investigation. F11b remains a mandatory open follow-on. No formal repair counter or approval status is changed by this document.

## Fresh SHA-256 inventory

Paths beginning `runs/` below are relative to `design/local-memory-v1-hardening/`.

| Source / artifact | SHA-256 |
| --- | --- |
| `distribution/macos-local-memory/engine/build_local_semantic_index.py` | `95b44b327fb0e0c1e747c9d244b077950ebf5b61dd0a8bf56b33b1a6c0e163da` |
| `distribution/macos-local-memory/engine/validate_adaptive_semantic_graph.py` | `c2587b685d06a1e8d1ec006bb2577be47168a0c14b072a22b3a90878c30563e3` |
| `scripts/build_intermediate_records.py` | `8d9dd31a7acba7d6b8f3e5a841b86565ea3e6e85d932fa08f77e3f3641ae59dd` |
| `scripts/probe_intermediate_records.py` | `5a3c443a76f02b198367c036017967a33a75f22ec0826091ab5c8b54a988baa4` |
| `scripts/adapt_layer1_to_local_memory.py` | `0ef2f634966e9be671628889b82e13baf8b57d14a6b649d7ffaee981279f9214` |
| `distribution/macos-local-memory/engine/build_adaptive_semantic_graph.py` | `5d2883e2a053776935d71b2486180b177078fe71b5d7a0a02bf8741e8e041c0a` |
| `tests/test_notebook_metadata_application.py` | `b254860584650f376400a50d3890b9cc6074633427a565787b91aae11965496a` |
| `tests/test_local_graph_index_schema.py` | `993771b3c3d3c7a83eeb70ecabc2947f45d421d0b9c5cc02625c8ab2bddcc83e` |
| `tests/test_local_embedded_visual_pipeline.py` | `892658aa919cad1dd35a7fdc19aba9c8b0ef1b931e83f3137fc6112b5a150508` |
| `runs/f11a-executor-before-managed.v1.py` | `7312ebdffc7221ef989195afd03bdbbe34f945582cb25b9020d64b19316bdabe` |
| `runs/f11a-executor-before-adaptive-validator.v1.py` | `17c5de11a6f8f958ea5d1db447840f5653128ef24314fad193a610f824c5a106` |
| `runs/f11a-executor-coherent.v1.json` | `fa13e1e5be75b0d4b908d247a8e00abaeedd0c84966f1072d28602a327c9798c` |
| `runs/f11a-executor-summary.v1.md` | `264cde0d0cb1cdf7757fabbd8ca03b2d4be9a6904d86c6320154cd4a5169b686` |
| `runs/f11a-root-app-001/unittest.log` | `25739d107455264f0a0ddc9ae3f739c706356bd83d1b80cba82ec02bda8c7ef3` |
| `runs/f11a-root-app-001/result.json` | `71135a9bd898e78e0e2290f5e5435281b7983ceb08de5eac1bd903397b317499` |
