# F11a fresh preflight v1 — Notebook text/state binding

Status: **contract preparation only; no implementation, semantic RED, or audit PASS**. Prepared by `/root/f05a_executor` on 2026-09-09. Product and existing tests were read statically, not imported or executed. This is the only file created by this subtask. Parent owns the fresh observation runner. Its reported observation002 still accepts missing/forged candidate metadata; an observation of that behavior is not a new-contract RED.

Adapter and Agentic Audit skills, including the audit reference and report schema, were read and used. They require a bounded frozen contract, literal preimplementation gold, retained failed attempts, independent review, and at most two formal repair rounds. No new agents, network, models, GUI, real materials, installs, or Git writes were used. All prior F05/F11 observation artifacts remain immutable history.

## 1. Decision summary and unresolved approval points

Recommended slice: independently bind the state object of each emitted textual Notebook Evidence to the exact original bytes whose digest identifies its Document, and preserve/compare that object at the direct SearchUnit boundary. Do not infer execution chronology or freshness. F11b (semantic projection, question shards, index, retrieval and answer display) remains a **mandatory, separate, unfinished** follow-on.

The following are recommendations, not additions already accepted into the task contract:

1. Add `validate_report` to both intermediate validators; preserve their old exact counts return only for a verified/not-applicable result. Notebook verification without originals must be machine-visible `UNVERIFIED`, not a successful counts-only return.
2. Parse/hash Notebook content from one bounded read snapshot. Keep shared pure helpers in the already shipped/fingerprinted `probe_intermediate_records.py`; do not add an unshipped module or change the protected package script.
3. Identify Notebook applicability from the Document source path/extension and canonical Evidence role/locator, checked against the caller's source-root snapshot. Producer `parser`/version strings must not turn verification off.
4. Retain the existing over-limit partial raw-text extraction, but do not classify it as parsed Notebook state. A mixed job containing such a source becomes `UNVERIFIED` and cannot pass the normal final Reader gate under the proposed wrapper. Retaining partial raw text and authorizing it for an index are different decisions.
5. Prefer preserving current one-Evidence/one-direct-SearchUnit text behavior. “Long source/output chunking” in the earlier proposal must mean an explicit no-truncation/no-new-chunking control here; application question chunk propagation belongs to F11b.

Root must freeze three open choices before implementation:

- **Text binding strength:** state/pointer-only attestation does not independently prove that Evidence raw text is the text at that pointer. Recommended stronger variant additionally reconstructs the current textual selector and compares raw text (including the existing Markdown data-URI replacement). The variant and completeness claim must be named explicitly; do not silently claim full original-text fidelity from a state-only comparison.
- **Parser amplification/resource bound:** retain the existing 64 MiB direct-text byte ceiling and depth 64 as the starting proposal, but these are not a proven Python parsed-object heap ceiling. Root must freeze a bounded token/node policy or a smaller attested-Notebook limit, with over-limit `UNVERIFIED`, before claiming a production memory bound. This investigation did not inspect real corpus sizes and does not justify an arbitrary new production cap.
- **Missing/invalid state facts:** recommend controlled failure for invalid typed counts and `UNVERIFIED` for missing code/execute-result counts, with a bounded diagnostic and no normal success. The earlier proposal allows failed/partial diagnostic treatment; root must select the exact behavior. Missing counts on markdown/raw cells and stream/display outputs are ordinary presence=false.

## 2. Fresh static observations (not new normative rules)

All paths below are repository-relative; line numbers refer to the SHA-frozen files in section 9.

| Surface | Current behavior and concrete boundary |
|---|---|
| `scripts/probe_intermediate_records.py:39` | Probe identity `intermediate-record-probe` / `0.7.1`; managed builder overrides it. |
| Probe `:70`, `:3219`, `:3260` | `.ipynb` is a direct-text suffix; over `MAX_DIRECT_TEXT_BYTES = 64 MiB` is routed to `extract_large_text`, emitting explicitly partial/unresolved raw text with character locators. It does not parse Notebook JSON. |
| Probe `:454`, `:2584`, `:6586` | `read_text` stat-checks then materializes bytes; Notebook parses permissive JSON, then `add_document` hashes a second file read. Hash and metadata are not currently tied to the same read. Stat alone does not cap bytes read after file growth. |
| Probe `:2652`, `:6748`, `:6769` | Evidence identity uses document/type/location/content digest, not native metadata. Source and saved-text outputs lack the proposed state. Empty source emits no cell Evidence, so its output must carry cell state directly. |
| Probe `:6686`, `:6746` | Markdown source data URIs are replaced by `[embedded image sha256=...]`; code data URIs are preserved. A validator cannot always compare Notebook `source` verbatim to existing Evidence raw text. Attachments have separate image records. |
| Probe `:6761` | Saved text selects `output.text` (string or joined strings), falling back to `data['text/plain']` only when the first selection is empty. Output order is stored order, not execution-count order. |
| `scripts/build_intermediate_records.py:51`, `:64`, `:1076` | Managed identity is `intermediate-record-extractor` / `0.11.0`; Probe is already in processing fingerprints. `process_file` catches extraction errors, discards uncommitted records, records failure, and separately checks source digest before commit. These outer freshness checks are not the new parser's same-snapshot claim. |
| `scripts/validate_intermediate_records.py:164`, `:218` | Existing JSONL strict parser rejects duplicate keys, constants, overflow-to-infinity floats and excessive depth. Probe cannot import this module for helpers because this module imports Probe. |
| Native intermediate `:428`, `:481`, `:558` | `validate(directory, source_root=None)` returns the exact three-key counts dict. With root it already computes hash and length from one `read_bytes`, then discards the bytes; no Notebook metadata reconstruction. |
| Streaming intermediate `:385`, `:443`, `:622` | Same counts API; schema-enabled and `published_schema=False` paths exist. Source validation currently stats and streams `digest_file`; adding a later Notebook reopen would introduce an avoidable digest/parse split. SQLite already provides bounded retained-record storage. |
| `scripts/build_search_units.py:72`, `:227`, `:270`, `:750`, `:846` | `display_value` prefers normalized text and strips edges. Notebook source/output become one direct unit each; `target_chars` does not chunk these records. Context is excluded from identity. `DocumentDeriver` receives only document ID, not the source Document. |
| Search builder `:906`, `:950` | Managed build knows source paths/state and verifies evidence-shard digests but does not invoke intermediate source validation. Merely copying state here is not source attestation. |
| `scripts/validate_search_units.py:35`, `:820`, `:964` | Context allowlist lacks notebook state; existing API returns exactly `records` and `counts_by_type`. It loads referenced Documents/Evidence and can independently compare the new context. It has no source-root parameter and cannot itself claim original-byte attestation. |
| `scripts/validate_search_units_streaming.py:72`, `:348`, `:375` | Pins Search builder `0.6.0`; SQLite stores complete Document/Evidence JSON, allowing per-unit state checks without a second global Notebook cache. |
| `schemas/evidence.schema.json:159` | Native properties are open; an optional schema field alone cannot enforce presence by source/Evidence role. |
| `schemas/search-unit.schema.json:77` | Context is closed; the one new field needs an explicit strict schema. Do not open all context keys. |
| Adaptive Validator `:99`, `:101`, `:111` | Adapter pin `0.7.0`, Search builder pin `0.6.0`; native structural producers explicitly include managed `0.11.0`. Adding a new managed identity is a deliberate compatibility edit, not proof of F11b propagation. |

## 3. Exact recommended report/API compatibility

Native signature: `validate_report(directory: Path, source_root: Path | None = None) -> dict[str, Any]`.

Streaming signature: `validate_report(directory: Path, source_root: Path | None = None, *, published_schema: bool = True) -> dict[str, Any]`.

Recommended report has exactly these keys (no object pretending to be a dict with hidden attributes):

```json
{
  "status": "PASS",
  "counts": {"document": 1, "evidence": 6, "relation": 6},
  "notebook_source_binding": {
    "status": "verified",
    "documents": 1,
    "evidence": 6,
    "reason_codes": []
  }
}
```

The numbers above describe a six-text-record example, not yet frozen fixture gold. `documents` counts distinct `.ipynb` Documents presented for validation, including empty/raw-fallback/failed ones. `evidence` counts presented `notebook_cell` or `text_block` Evidence belonging to them, including unresolved raw blocks; image/OCR/error records are excluded. Non-Notebook input gives `PASS`, `not_applicable`, zero/zero/empty reasons. All applicable originals successfully checked gives `PASS`/`verified`. Missing caller root or a bounded/unparsed source gives `UNVERIFIED`/`unverified`; reasons are a sorted unique list from `source_root_missing`, `notebook_source_over_limit`, `notebook_state_unparsed`, `notebook_execution_count_missing` (last subject to root choice). A verified state means only this declared textual slice, never visual completeness or output freshness.

Malformed record/state, inconsistent locator, unexpected metadata on unrelated Evidence, changed source digest, missing source file, parser-invalid JSON or mismatched expected facts raise `ValueError` as today; they do not yield a success report. Missing required state on a canonical Notebook record is a mismatch, not an optional-schema success. Prefix errors with a stable reason such as `notebook_state_mismatch` or `notebook_rebuild_required`; retain bounded location/type diagnostics.

Keep both existing `validate` signatures/return annotation and exact three-key success dict. Implement them as calls to `validate_report`: return only `report['counts']` on `PASS`; otherwise raise `ValueError('notebook_source_binding_unverified: ...')`. Existing non-Notebook callers and exact counts comparisons remain compatible. Do not use a default permissive flag to let new consumers omit root and succeed on Notebook input.

Intermediate CLIs call `validate_report` explicitly: print the report JSON; exit 0 only on `PASS`, exit 2 on `UNVERIFIED`; catch validation `ValueError` as explicit `FAIL`/exit 1 if root chooses a structured CLI error envelope. Streaming retains its separate `schema_validation` label (`draft202012` or `structural_contract_only`) in CLI output, outside the common report comparison. The Notebook semantic checks run in all three paths regardless of published-schema availability. Exact FAIL envelope is a root contract choice; old traceback compatibility is not needed to preserve the Python counts API.

Search builder/public validators need no new source-root/report API for the minimum **Evidence-bound** copy contract: preserve their existing return shapes, add strict role-aware context reconstruction. They must not describe their old `status: ok` as source-attested on its own. Source-bound F11a evidence consists of both intermediate `validate_report(..., root)` PASS and Search validation of the same immutable intermediate generation. Rewriting Evidence and Search context coherently must fail the intermediate source check, even if the Search-only comparison succeeds. If root instead requires standalone Search validation to establish source authority, that is a larger explicit API/caller change and remains unassigned.

## 4. Same-read authority, applicability, and digest coupling

Recommended internal helper in Probe: `read_notebook_snapshot(path: Path, *, max_bytes: int | None = None) -> tuple[bytes, str]`, returning `(raw_bytes, encoding)`. `None` resolves the current module ceiling at call time, so bounded tests can patch the seam; do not freeze it in a default argument. One binary open/read of at most ceiling+1, actual byte count check, strict decode under the existing detected encoding, no replacement-character success. A regular file/root-contained path is selected by the existing caller, never by notebook JSON, native metadata, or a JSON pointer interpreted as a filesystem path.

Recommended pure helper: `parse_notebook_snapshot(raw: bytes, encoding: str) -> dict[str, Any]`, yielding the validated parsed object, no filesystem/runtime/metadata probes. Hash and byte length are computed by callers from this same raw object. `Probe.add_document(..., *, source_snapshot: bytes | None = None)` uses its hash/length when provided; other formats retain old behavior. Its mtime remains filesystem metadata, not part of a same-byte claim. Producer validates Notebook structure/state facts before emitting textual/visual child records. Managed outer pre/post digest checks remain unchanged; no claim that every possible concurrent rewrite is detected.

Intermediate native reuses its already required source bytes for the Notebook parse, rather than reading again. Streaming substitutes the same bounded read/hash/parse operation for its Notebook `stat + digest_file` branch; non-Notebook streaming stays unchanged. For over-limit Notebook input, do not do an unlimited digest pass followed by a parse attempt just to report source attestation: report unverified/over-limit. File mutation may cause an old or new snapshot to be read; acceptance requires the digest of the actual parsed snapshot to equal the Document claim. No global file lock or all-ancestor race guarantee is introduced.

Applicability is based on normalized Document `source.relative_path` suffix `.ipynb`; `source.extension` must agree and contradictory Notebook locators/roles on non-Notebook documents fail. With explicit root, verify containment, actual source hash and byte size before interpreting state. Within a parsed Notebook:

- `notebook_cell` must map to canonical `{notebook_cell_index: N, locator_text: 'cell=N'}`, ordinal N, origin `cell_source`, pointer `/cells/N-1`.
- Saved textual `text_block` must map to cell N/output M, object index M, ordinal M, exact `cell=N;output=M`, origin `saved_output`, pointer `/cells/N-1/outputs/M-1`.
- A text block cannot escape applicability by removing notebook_state, indices or by changing its parser to `bounded-text-stream`. If the original is parseable/in-bound, a raw character-locator Notebook block is not a canonical saved-output record and must fail or be explicitly unverified, never PASS.
- Actual over-limit source plus raw/unparsed locator may retain the existing partial artifact and cannot receive invented cell/output state. Rootless artifacts cannot establish that the over-limit branch was genuine. They remain unverified.
- Image/OCR/error/display-update state is outside this slice; attaching this textual state to such records is rejected. Their absence is not a claim of complete Notebook extraction.

Use the exact object from the original proposal, with strict source/output variants and strict presence objects. Compare canonical JSON (or strict type-aware recursive equality); Python `True == 1` is not acceptable. No new self-declared metadata digest creates authority. Document.source.sha256 couples state to source bytes; existing raw-content digest still identifies content, and complete serialized Evidence/shard hashes carry metadata through managed artifacts. Context is outside SearchUnit identity, so compare the object even when the unit ID is unchanged.

## 5. Parser, raw text, cache and chunking contract proposals

Strict Notebook JSON requirements should be a narrow prerequisite to attestation, not a claim of full nbformat validation: reject duplicate keys at every level, NaN/Infinity constants, finite-float overflow such as `1e309`, recursion/depth over 64, invalid text decode, non-object root, non-list `cells`, non-object cells/outputs, invalid string/list-of-string source/text fields, and invalid typed counts. Catch parser recursion/overflow as controlled `ValueError`. Do not execute notebook code or import a kernel/nbformat package. Preserve source key order only where semantic (cells/outputs arrays); never sort by counts.

Proposed minimum shape: cell_type is code/markdown/raw; missing source defaults to empty text as today, outputs defaults to empty list, present outputs must be an array of objects. Text selection preserves the current text-then-text/plain precedence. No coercion with `str()` of numbers/objects/null. Null/nonnegative exact-int execution counts are accepted, excluding bool; negative, float and numeric-string counts fail. Missing code/execute-result count emits at most 256 characters of type/pointer/value diagnostic and is not ordinary success (choice in section 1). Missing stream/display output count or markdown/raw cell count remains explicit `{present:false}`. Do not infer “never executed historically” from null/missing.

For the stronger text-binding variant, independently reconstruct raw source/output strings with the above selector and compare their content digests/locators alongside state. Markdown data-URI replacement must preserve the existing deterministic digest-token rule at Probe `:6610–6623`, including malformed payload handling, without calling `add_notebook_image`, OCR or visual observation. Code literal data URIs remain untouched. Do not change image record emission in this task. This transform is additional implementation/test scope requiring root approval; a state-only variant must disclose its weaker body claim.

Cache recommendation: no module-global cache keyed by path/mtime or artifact-supplied digest. Parse at most one Notebook source snapshot at a time, derive only bounded pointer/state/expected-text-digest entries, and release raw/decoded/parsed objects before moving to the next Document. Streaming stores these entries in its existing temporary SQLite database, keyed by document ID + canonical pointer; it need not retain all Notebook JSON or full duplicate raw text. Native can validate each document against its already loaded Evidence and discard the derived map, rather than accumulating a second entire-source cache. Reusing a pure parsed object inside one snapshot operation is allowed. A source-read instrumentation control must prove no hash-then-reopen path for Notebook validation.

The current implementation's 64 MiB bytes and depth 64 are explicit starting limits, not an aggregate job/heap bound. A million tiny JSON nodes can amplify memory before postparse checks; parent must fix this unresolved guard before production implementation is called bounded. Tests use a patched small byte ceiling (e.g. 8 KiB) and a small nested fixture; they do not create a 64 MiB fixture or pretend 8 KiB is the production cap. Cache/spill, total fixture bytes and wall/output limits must be frozen with the runner.

No new Notebook text chunking is recommended: preserve one textual Evidence and one nonempty direct SearchUnit per source/output, even above `target_chars`; compare Search text to current `display_value` semantics (`normalized_text` preferred, edge strip). Whitespace-only Evidence may legitimately produce no SearchUnit. All actually emitted unit fragments must carry identical state if a later phase introduces chunking. F11b must independently retain state across question shards; not solved here.

## 6. Minimum proposed edits, identities and old generations

Proposed product ownership set is exactly the seven scripts listed in the task, two schemas, and `distribution/macos-local-memory/engine/validate_adaptive_semantic_graph.py` for identity pins only. Within managed build, prefer only the managed version bump unless a snapshot compatibility seam is strictly required. Do not edit adapter projection, bootstrap, index, answering, or protected package script for F11a.

Recommended identity bumps: direct Probe `0.7.1 → 0.8.0`; managed extractor `0.11.0 → 0.12.0`; Search builder `0.6.0 → 0.7.0`; update both Search validator pins and adaptive Search pin, deliberately add managed `0.12.0` structural tuple. Preserve historical structural tuples unless a separately justified incompatibility demands removal. Adapter remains `0.7.0` because its projection is not fixed in F11a. Existing content/Document IDs are unchanged when original bytes and content are unchanged; Search builder bump changes Search IDs under the existing identity payload.

Do not bless old Notebook metadata absence using its old producer version. Old Notebook artifacts are read-only historical and need explicit rebuild to satisfy this contract; no in-place metadata backfill. Old non-Notebook count validation remains compatible. Existing app generation code/schema fingerprints cause migration when these shipped files change; confirm with migration controls, not by touching published generations. The snapshot-size and invalid-parser changes may make previously indexable partial Notebook jobs non-passable; report that compatibility impact explicitly.

Packaging is an important constraint: `distribution/macos-local-memory/build/build_package.sh:41–68` explicitly copies scripts; a brand new scripts helper is not automatically included. Probe, both intermediate validators, native Search validator and both schemas are already listed. `validate_search_units_streaming.py` is currently absent from that copy list; checkout parity tests do not prove a shipped streaming Search CLI. Preserve this existing packaging limitation and do not claim to fix it. Probe is already covered by managed `PROCESSING_CODE_FILES` and bootstrap `READER_PROCESSING_CODE_FILES:54`, so an internal pure helper needs no protected build-script or fingerprint-list edit.

## 7. Literal RED/control plan and side effects

Freeze one new focused permanent Notebook test file, exact method list, literal object gold and all fixture bytes before product changes. Do not derive gold by calling the proposed producer helper. Missing API/import/TypeError is preparation evidence, not semantic RED. First semantic RED candidates against current code: (a) current Probe output must contain the hand-written first source/output state; (b) an existing valid artifact with omitted/forged state must be rejected by current validator calls; (c) direct Search derivation must preserve a hand-written candidate state. Preserve current observation evidence separately.

Mandatory controls/attacks to freeze: exact all-six-record state ordering from the existing tiny fixture; counts 7/2/null and output 6 without freshness inference; equal-count and zero controls; false/string/negative/float counts; missing versus explicit null; markdown/raw/source-only; empty source with text output; multiple outputs and exact text precedence; source raw-text/hash unchanged; long source/output and whitespace-only behavior; remove/change state with recomputed record/shard hashes; wrong pointer/index/type/order; irrelevant Evidence injection; source digest/size mismatch; same-path changed snapshots without stale cache; one read snapshot instrumentation; strict duplicate/nonfinite/overflow/depth/decode failures; injected over-limit raw fallback and valid in-limit control; old Notebook no silent upgrade; rootless report and counts-wrapper failure; native/schema-stream/structural-stream parity; native/stream Search context equality including same-ID changes. Strong text variant also needs altered-body-with-resealed-hashes and Markdown data-URI controls. Whole-record deletion/completeness is not established by checking each presented record; if root wants completeness, explicitly add expected-member reconstruction and diagnostics/sample handling before freeze.

Existing test side effects are not uniformly safe: `tests/test_layer1_pipeline.py:254–272` runs the managed builder in a subprocess in class setup; `:946–964` repeats a CLI builder for formulas. These can enter processing-fingerprint/runtime/model discovery despite a selected test being small. Do not run the whole class unreviewed. The existing large-text boundary at `:518` uses an injected eight-byte cap on JSON (not Notebook); preserve it. Its exact counts assertion at `:339` and native/stream Search equality at `:967` must remain unchanged for non-Notebook input.

`tests/test_local_embedded_visual_pipeline.py:313` uses a tiny synthetic code cell with null execution count and mocked image readers. Relevant methods are `test_notebook_embedded_image_preserves_cell_and_source_lineage`, `test_notebook_referenced_markdown_attachment_is_visually_read`, and `test_notebook_code_data_uri_is_preserved_and_not_treated_as_displayed` (`:568`, `:595`, `:635`). Keep their image/attachment contracts; do not run actual OCR/visual tools. `tests/test_semantic_question_shards.py:464` deliberately mocks Search validation for an adversarial adapter test; it is not a state-source oracle. Migration tests at `distribution/macos-local-memory/tests/test_reader_generation_migration.py:162`, `:207` exercise current code/processing/schema bytes and should need no literal version relaxation.

Proposed future run guard: one child, frozen method allowlist, fresh temp-only synthetic roots, no actual network/model/Office/GUI subprocess; stub fingerprints/password discovery/visual workers as required after reviewing exact call sites. Start at 30 seconds and 1 MiB captured output per batch, cap cumulative explicit fixture writes (suggest 1 MiB for this tiny slice), and retain every result/log including missing APIs and early failures. Parent must freeze the actual guard and optional-dependency behavior; required skips are not PASS. No run was attempted by this agent in this preflight.

## 8. Rollback and completion boundary

Before implementation preserve exact current bytes and inverse deltas for only the assigned ten product files and separately assigned compatibility hunks. Gold/runner/fixtures and before snapshots are immutable; additions need new versioned evidence. Never restore the dirty shared worktree from HEAD, mutate originals/published generations, or overwrite F05b evidence. If stopped, retain the unpublished failure evidence and reverse only reviewed owned deltas. The current preflight needs no product rollback.

F11a can only be accepted after a frozen variant closes its declared invariant, positive and mutation controls pass in all required native/stream modes with no required skips, source hashes match the artifact, and an independent auditor reviews it. F11 as a whole remains OPEN even then: app-visible source-versus-saved-output labels, preserved facts through question shards/index/retrieval, error/image/display state and the other proposed hidden-content work are not completed here.

## 9. Fresh SHA-256 inventory

Read-only shell hashing during this preflight produced:

```text
73b9ad531d4e184b47636f0a7558e07cc44688d1499ddd666b8dc58610f830f7  scripts/probe_intermediate_records.py
7312ebdffc7221ef989195afd03bdbbe34f945582cb25b9020d64b19316bdabe  scripts/build_intermediate_records.py
0fc6f8bb42fae82ae0bec3b84ca319907bc2436aa58a1d59426f9ea525d4ab69  scripts/validate_intermediate_records.py
d998ec20f3511583603fbf4199160c47772c98f65e53e2274a23dc1f0a6e7f9f  scripts/validate_intermediate_records_streaming.py
a14ffa6f5c4b04fd63ed3b95ba8b6892ca5c11f1cee050b323861c009c6eaaa1  scripts/build_search_units.py
bd673618bdf7c56a3e0c6c19330ad8b3ff0fcb69d7b0d4f4930808fa5289394d  scripts/validate_search_units.py
40dbaa9b8b50d060b26bd672322830d87fdb26fb0ae2a356694a85a75edfcdd1  scripts/validate_search_units_streaming.py
81db1ba15565b890fa885806d013854380267ec37d578b1bf7997fd86cac5bc5  schemas/evidence.schema.json
47ed1bf91425c59f87c9672470b0684fddb935e103d845123e54346dd72fae9f  schemas/search-unit.schema.json
17c5de11a6f8f958ea5d1db447840f5653128ef24314fad193a610f824c5a106  distribution/macos-local-memory/engine/validate_adaptive_semantic_graph.py
10989f5f941c1e567a7a8c7fe82f2d5d5d88e66345d4503a1600c7c9827bd02f  distribution/macos-local-memory/build/build_package.sh
e6248aae9ffa89e3cd6a43839af7f41e4d5941482f8f00a48f466de5524b526b  distribution/macos-local-memory/app/bootstrap.py
63619ca448d88352beca3ee8e4679b25f762b3a12298398603c33f03f9f7255e  tests/test_layer1_pipeline.py
892658aa919cad1dd35a7fdc19aba9c8b0ef1b931e83f3137fc6112b5a150508  tests/test_local_embedded_visual_pipeline.py
f803e255155ed753ab083ae4ec295df54904981bcb40b3c854334287356a5578  tests/test_semantic_question_shards.py
c0b4774ecbc9e45fa17b0a983c9f9f23c1725001972ecd3026c5f5cf03fe8c0c  distribution/macos-local-memory/tests/test_reader_generation_migration.py
e497b146e97ef48463203aaef5f0dd1fd60564d4d159b2075b8f9b760c8a190a  design/local-memory-v1-hardening/RESUME.md
dcaf578165ff542e7a79b7875076c91d1a3759590072339ccb1c9e539d3219d3  design/local-memory-v1-hardening/checkpoint.json
a98164a5ccb16eb2ea5ec9bf3779ffe3b0ac962ec1c55d141574d4264f921d46  design/local-memory-v1-hardening/runs/f11-next-contract.v1.md
fb910114baf135430907071c973498a0ac3027096bc0eed314d7a9dd12ee27d8  design/local-memory-v1-hardening/runs/f11a-binding-observation-result.v1.json
```

RESUME/checkpoint are coordination files and can advance under root ownership after these reads; their listed hashes identify the state examined, not a claim to freeze root's future updates. Adaptive Validator hash is the current accepted F05b source, not the historical pre-F05a resolver snapshot.
