# F11a task contract v1

Task: `lms-v1-f11a-notebook-metadata-2026-09-09`. Root/Executor/Auditor separation: same_model_separate_context. Formal repairs maximum 2. Frozen design contract; implementation permission is additionally gated by saved before bytes, literal test gold and root-approved runner/semantic RED. No product acceptance yet.

## Goal and exclusions

Carry and validate cell/source/saved-output facts on textual Notebook Evidence and direct SearchUnit context. Bind those facts to the same original bytes used for the Document digest. Never execute source code or infer output freshness/order from counts.

Metadata-only: raw text correspondence to its pointer, full Evidence/SearchUnit membership, saved-output freshness, execution history, hidden Office content, image/OCR/error/display-update state, application shards/index/retrieval/answer propagation and production RSS/performance are NOT certified. F11b and body/membership work remain mandatory open items. Coherent raw-text replacement and whole-record removal are residual witnesses, not acceptance cases. Existing no-truncation behavior remains unchanged.

## Exact state

`Evidence.native_properties.notebook_state` is copied to `SearchUnit.context.notebook_state` for direct textual Notebook units. Closed object, strict JSON types:

Common keys: `version:"1.0"`, `cell_index` positive integer, `cell_type` one of code/markdown/raw, `content_origin` cell_source or saved_output, `source_json_pointer`, `reader_execution:"not_executed"`, `cell_execution_count` presence object. Presence object is exactly `{present:false}` or `{present:true,value:null|nonnegative integer}`; bool is not integer.

Source state has only common keys, pointer `/cells/{zero_based_cell}`. Output state additionally has `output_index` positive integer, `output_type` stream/display_data/execute_result, `output_execution_count` presence object, `output_freshness:"unverified"`; pointer `/cells/{zero_based_cell}/outputs/{zero_based_output}`. Preserve actual stored array order and original count absence/null/value. Missing code-cell or execute_result count causes UNVERIFIED, not fabricated null; missing markdown/raw or stream/display count is ordinary absence. Invalid count types/negative values fail. Null is not proof of historical nonexecution.

Canonical source Evidence: notebook_cell, ordinal=cell_index, location notebook_cell_index and locator_text `cell=N`. Saved textual output: text_block, ordinal=output_index, location notebook_cell_index/object_index and locator_text `cell=N;output=M`. Strict type-aware equality, no Python true==1 shortcuts. Source/output raw text and existing image transforms stay unchanged; do not introduce chunks or empty Evidence. Whitespace-only text may have no direct SearchUnit.

## Resource policy before parsing

New constants in Probe, resolved at call time (patchable smaller test limits): `MAX_NOTEBOOK_METADATA_BYTES=8*1024*1024`, `MAX_NOTEBOOK_JSON_TOKENS=100000`, `MAX_NOTEBOOK_JSON_DEPTH=64`, `MAX_NOTEBOOK_NUMBER_CHARS=256`. These are conservative attestation limits, not measured optimal corpus limits or total RSS guarantees. Existing generic DIRECT_TEXT cap remains unchanged.

Read at most byte cap+1 in one binary snapshot before decode/JSON parse. Decode strictly using existing encoding detection; no replacement success. Before json.loads, scan decoded JSON lexically without materializing a token list: count each string token (including keys), primitive token and container opener as one; root container depth=1. Honor escaped quotes/backslashes; brackets/commas inside strings do not affect structure counts. Bound number token length before conversion. Reject over token/depth/number cap before building JSON objects. JSON parser still checks grammar, duplicate keys, nonfinite constants/float overflow, root/cell/output shapes, and string/list-of-string textual fields. Malformed JSON/counts produce controlled ValueError, never PASS. This is project validation, not official nbformat conformance.

Limits are distinct from malformed data. Byte/token/depth/number limit produces bounded diagnostic `notebook_metadata_resource_limit` and UNVERIFIED. Producer retains source text using existing explicitly partial raw-text fallback instead of attempting unrestricted JSON parse; do not turn that raw fallback into parsed Notebook state. Mixed jobs can consequently be held at validation. This compatibility cost must be documented. Genuine partial/failed extraction also cannot yield PASS. A known malformed field/state mismatch takes precedence over UNVERIFIED wherever it can be checked within the resource budget; no parsing past limits just to discover more contradictions.

One Notebook parsed object at a time. Release bytes/text/parsed state before next source. Streaming may use its existing temporary SQLite for pointer metadata, not a module-global path/mtime cache or all-source JSON cache. Number of retained metadata entries is bounded by preparse tokens. No aggregate job/OS memory claim.

## Applicability and original binding

Notebook Document applicability comes from normalized source.relative_path suffix `.ipynb`, case-insensitive, with source.extension agreement; producer parser/version/state omission cannot opt out. Actual root-contained source hash and byte length must match the exact bytes parsed. No hash then reopen or parse then independent hash in attested branch. Source absent/hash mismatch/size mismatch are failures, root intentionally absent is UNVERIFIED. Existing non-Notebook source validation remains unchanged.

For Notebook Documents, textual roles notebook_cell/text_block must be canonical and state required; unsupported textual-role/locator substitutions must not silently escape checks. Preserve legitimate separate image/OCR records as out of scope, reject textual state injected onto unrelated records. A rootless artifact still undergoes all existing structure checks and strict state/role checks before returning UNVERIFIED. Old Notebook state omission fails with `notebook_rebuild_required`; old version string is not an exemption. Genuine failed/partial raw fallback lacking canonical cells is UNVERIFIED, not rewritten; forged parsed-state shape is still failure. Genuine oversized source cannot be established rootlessly.

No textual Evidence checked for any Notebook Document is UNVERIFIED with `no_textual_records_checked`, even for a legitimately empty Notebook. This is a visibility rule, not proof that missing records were detected. Nonzero presented records do not certify full membership.

## Exact intermediate API and report

Native: `validate_report(directory, source_root=None)`. Streaming: same plus existing keyword-only `published_schema=True`. On structural/semantic failure raise ValueError. Otherwise exact shared report:

```json
{
  "status": "PASS",
  "counts": {"document": 1, "evidence": 6, "relation": 6},
  "notebook_metadata_binding": {
    "status": "verified",
    "documents": 1,
    "checked_evidence": 6,
    "unchecked_evidence": 0,
    "reason_codes": [],
    "scope": {
      "raw_text_binding": "not_verified",
      "complete_membership": "not_verified",
      "output_freshness": "not_verified"
    }
  }
}
```

Counts retain existing semantics; displayed numbers above are only the fixed six-text fixture. Binding.documents counts presented Notebook Documents, checked_evidence counts canonical textual records actually compared to source facts, unchecked_evidence counts applicable presented textual records not compared. Missing count can still be compared as absent and counted checked while triggering UNVERIFIED. Reason codes sorted unique, allowed: `source_root_missing`, `notebook_metadata_resource_limit`, `notebook_state_unparsed`, `notebook_execution_count_missing`, `no_textual_records_checked`. Any reason means outer UNVERIFIED and binding unverified; no Notebook Documents means PASS/not_applicable, both evidence counts/documents zero and reasons empty. PASS means all existing checks plus the declared metadata slice only.

Existing validate signatures and exact old counts return remain on PASS; otherwise ValueError prefix `notebook_metadata_binding_unverified`. No permissive default flag. Both CLIs print shared report and exit 0 PASS/2 UNVERIFIED. ValueError yields `{status:"FAIL",error:<bounded to 512 characters>}` and exit 1. Other programming exceptions are not swallowed as successful reports. Streaming keeps its existing schema_validation label as a CLI-only extra. All three native/schema-stream/structural-stream paths enforce same semantics.

Search builder copies strict state, both Search validators compare it to referenced Evidence including same-ID context alteration/omission and unrelated injection. Their existing return shapes stay unchanged: this is Evidence-bound, not original attestation. Source-bound receipt requires intermediate validation of the same immutable generation as Search input. Do not claim adaptive `status:pass` certifies body/freshness; test CLI UNVERIFIED stops the existing adaptive caller before downstream build.

## Ownership and identities

Executor alone may later edit these ten files after explicit root RED gate:
1. scripts/probe_intermediate_records.py
2. scripts/build_intermediate_records.py
3. scripts/validate_intermediate_records.py
4. scripts/validate_intermediate_records_streaming.py
5. scripts/build_search_units.py
6. scripts/validate_search_units.py
7. scripts/validate_search_units_streaming.py
8. schemas/evidence.schema.json
9. schemas/search-unit.schema.json
10. distribution/macos-local-memory/engine/validate_adaptive_semantic_graph.py (identity pins only).

Versions: Probe 0.8.0, managed extractor 0.12.0, Search builder 0.7.0; synchronize Search validator/adaptive pins and new managed structural tuple. Preserve historical tuples; they do not exempt Notebook metadata checks. Adapter stays 0.7.0, no app/index/answer/product build script changes. Helpers stay inside already shipped/fingerprinted Probe. Existing missing packaged streaming Search validator remains disclosed; checkout parity is not packaged acceptance.

Executor owns NEW `tests/test_notebook_metadata_binding.py` and NEW `runs/f11a-executor-*` preparation/gold/before/runner/evidence files. Root owns separate NEW boundary controls and contract/status. Auditor owns separate holdouts/report only, never executor gold. Existing tests may not be edited without bounded hunk authorization. Before bytes must be saved via apply_patch and hashes verified, not restored from HEAD. Preserve all original dirty changes and historical artifacts.

## Test gate and completion

Before product edits, freeze literal six-state fixture gold and tests for review G1–G6, including equal/zero/null/missing, invalid counts, wrong metadata/pointer/role/extension, rootless/partial/zero, preparse caps/escapes, same-read/no-stale-cache, both Search validators, both intermediate schema modes, CLI/caller gate, old generation/package controls. Preserve original raw text/hash/long text and Notebook image mocked controls. Helper-derived expected state forbidden. Freeze permanent tests and original AST before semantic RED. API-missing failures are not semantic RED.

Runner: one child per suite, 30s, captured output <=1MiB, total explicit synthetic fixture writes <=1MiB per batch, temp-only source/CONFIG, no models/network/external executables/GUI/actual import. Small injected limits only, never 8MiB production fixture. Root must read runner and test side effects before granting execution. Reuse reviewed guard/supervisor without weakening. All attempts/logs including skip/import/tool errors retained; required skip is not PASS.

Then minimal fix → original gold/regressions → immutable graph artifact/source hashes → separate-context formal audit → parent schema/hash/reference/result/gold verification. No self-approval. Product formal repair count starts 0. Rollback only owned reviewed inverse deltas, never reset shared tree or original/public data. Whole V1 and full F11 remain open after this slice.
