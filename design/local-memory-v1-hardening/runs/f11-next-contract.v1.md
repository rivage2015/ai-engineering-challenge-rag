# F11 next contract proposal v1: Notebook saved execution facts

Status: **proposed; implementation not started; no product or audit PASS**. Prepared by `/root/f11_reader_review`, 2026-09-09 06:15 JST. Existing authorization permits bounded synthetic work, but this assigned subtask is read-only product review. All files created by this agent have the `design/local-memory-v1-hardening/runs/f11-next-` prefix. Product parser, schemas and tests were not modified.

## Decision and evidence

Smallest useful next slice: **F11a, textual Notebook source/saved-output state through Layer 1 Evidence and SearchUnit**. This closes an auditable Reader boundary without selecting a policy for hidden content or claiming that historical output is fresh/stale. F11 as a whole remains open until application projection, indexed retrieval and answer presentation are covered, and images/display metadata are addressed separately.

| Finding | Evidence and scope |
|---|---|
| XLSX native lacks worksheet state | `probe_intermediate_records.py:3862-3870` creates worksheet Evidence with title/dimensions but no `sheet_state`. Static observation only; no installed optional library invoked. |
| XLSX fallback already retains state | `probe_intermediate_records.py:4201-4212` emits `native_properties.source_member` and `state = sheet@state or visible`. Do not duplicate F10 or claim every XLSX route lacks state. Row/column visibility and child metadata propagation are not established by this existing worksheet field. |
| PPTX native/fallback lack slide display state | Native slide creation at `:4334-4341`, fallback at `:4770-4786`; neither emits `sld@show` or an equivalent fact. No GUI/Office rendering or hidden-slide semantics were tested. |
| Notebook counts/state absent | `:6747-6780` emits source `cell_type/encoding` and saved-text `output_type` only. Synthetic fixture has cell counts 7, 2, null and saved result count 6; all are absent from resulting Evidence. Original cell/output order and text remain intact. |
| Empty-source output cannot inherit a source record | Cell 3 has empty source and a saved text output. Reader emits only output Evidence at `cell=3;output=1`; no parent `notebook_cell` exists. Synthetic observation. |
| Direct SearchUnit loses candidate native metadata | `build_search_units.py:749-851` forwards source IDs/locators/text, not arbitrary native fields. A copied in-memory sentinel was dropped from all 6 units. This sentinel was test data, not a proposed final API or product edit. |
| Application projection also needs a later change | `adapt_layer1_to_local_memory.py:552-595` constructs a whitelist projection; generic native state is absent. It already records `adapter.execution_policy = never_execute`, which describes adapter policy, not saved per-cell execution facts. `:615-619` does not reproject notebook/text SearchUnits, so Notebook SearchUnit state alone will not reach semantic Evidence. Static observation only. |
| SQLite/prompt downstream are not ready for a new field | Semantic allowlist at `build_local_semantic_index.py:184-188` excludes a notebook state object. Graph-node payload keeps an authorized source record (`:1858-1874`), but current retrieval context (`answer_local_memory_v2.py:1115`) formats text/path/locator only. No claim of end-to-end propagation. |

Observed source A and saved output A differ as strings. This alone does not establish stale output, wrongdoing, execution chronology, correctness or current usability. A null count means a null value was saved; it is not proof that a cell was never historically executed. Counts 7 then 2 are preserved in notebook document order, not sorted into a speculative execution history.

## Exact proposed F11a representation

Proposed field: `native_properties.notebook_state` on each emitted textual `.ipynb` Evidence; copy the same validated object to `SearchUnit.context.notebook_state`. Values come from the exact source JSON cell/output selected by the existing 1-based locator. Do not modify `content.raw_text`, `content.sha256`, original ordinal or location to embed explanatory prose.

Example for the first saved text output:

```json
{
  "version": "1.0",
  "cell_index": 1,
  "cell_type": "code",
  "content_origin": "saved_output",
  "source_json_pointer": "/cells/0/outputs/0",
  "reader_execution": "not_executed",
  "cell_execution_count": {"present": true, "value": 7},
  "output_index": 1,
  "output_type": "execute_result",
  "output_execution_count": {"present": true, "value": 6},
  "output_freshness": "unverified"
}
```

- Source text uses `content_origin = cell_source`, pointer `/cells/N`, the saved `cell_type`, and the presence/value form of the cell count. Omit output-only fields. Markdown/raw cells may have `{ "present": false }`; do not invent a code execution count.
- Saved text output carries all the fields shown, including its cell count even when no source Evidence was emitted. Stream/display output without its own count carries `{ "present": false }`; an explicit null remains `{ "present": true, "value": null }`.
- Presence objects have exactly `{present:false}` or `{present:true,value:<source value>}`. Accepted count values are null or nonnegative JSON integers, excluding bool and numeric strings. Invalid count values must yield explicit failed/partial diagnostics and may not silently become an ordinary success. Preserve a bounded diagnostic of the invalid source value; do not coerce to zero/null. Missing code count must be explicitly diagnosed; missing stream/display count is ordinary.
- `reader_execution = not_executed` applies only to the current reader action. `output_freshness = unverified` is mandatory on saved output; neither mismatched nor equal counts establish freshness. Hidden/excluded/secret/stale classifications are out of scope.
- No new source text is synthesized. No empty source cell Evidence is created just to hold metadata. No Notebook, formula or shell code executes. No kernel metadata/network request is necessary.

## F11a changes and required verification

Candidate implementation files are a **proposal only**, to be freshly assigned and rehashed before editing:

1. `scripts/probe_intermediate_records.py`: snapshot per-cell metadata before output iteration; attach it directly to textual source/output Evidence. Add validated typed helper(s) with no runtime/metadata probes. Keep visual outputs/attachments explicitly outside this slice. Bump extractor identity as required for regeneration; `scripts/build_intermediate_records.py` overrides the Probe extractor version and fingerprints source code, so verify both CLI and direct modes invalidate an old reader generation.
2. `schemas/evidence.schema.json`: define a strict optional `notebook_state` object. Runtime validators must require it on the new reader generation's textual Notebook records, rather than letting optional JSON schema silently accept omission. Do not require Notebook fields for ordinary `text_block` from other formats.
3. `scripts/validate_intermediate_records.py` and `scripts/validate_intermediate_records_streaming.py`: validate shape, type, role, source JSON pointer and existing locator agreement. With `source_root`, re-read only the exact already hash-bound `.ipynb` source under existing size/depth limits and derive the expected count/type facts independently. Without source root, explicitly report source binding as unverified; do not upgrade schema/hash consistency to source truth. Missing/different state with recalculated record/shard hashes must fail source-attested validation.
4. `scripts/build_search_units.py`: propagate the validated object for direct Notebook source/text units. Existing `make_unit` identity excludes context; do not treat unchanged IDs as proof of state correctness. Avoid arbitrary `native_properties` pass-through.
5. `schemas/search-unit.schema.json`, `scripts/validate_search_units.py` and `scripts/validate_search_units_streaming.py`: add the exact context key/schema and compare every copied field to the referenced Notebook Evidence. A same-ID state change, removed state, wrong cell/output index, or notebook metadata on unrelated evidence must fail. Any builder identity change must update the pinned expectations in both validators and `validate_adaptive_semantic_graph.py`; do not blindly expand acceptance.
6. Add a focused new permanent Notebook state test file after ownership assignment. Cover the predeclared fixture, equal and mismatched counts, decreasing document-order counts, explicit null, missing vs null, zero vs false, malformed count/string, empty source with text output, source-only/markdown/raw cells, multiple outputs, long source/output chunking, unchanged raw text/hash, and source byte preservation. Use exact independently written gold objects, not the producer helper as the oracle.
7. Regression selection should include relevant `tests/test_layer1_pipeline.py`, `tests/test_semantic_question_shards.py`, `tests/test_local_embedded_visual_pipeline.py`, and Reader generation migration tests after preflight. Build CLI calls must stub processing fingerprint/Ollama metadata/password discovery/visual workers as existing hardening tests do; never call a real model to check this contract. Direct Probe observations below did not run the CLI or schemas.

Accept F11a only after the failing positive contract tests turn green, the source/metadata mutation negatives reject, both in-memory and streaming validator paths agree, no required skip is counted as pass, and a separate agent audits the immutable contract, diff, fixture, test results and source hashes. Maximum repair cycles remains 2. This document is not that audit.

## Explicit follow-on boundary, required before F11 completion

Extend `adapt_layer1_to_local_memory.py:adapt` to project only validated Notebook state onto semantic Evidence and each question shard; update `validate_adaptive_semantic_graph.py:expected_semantic_evidence` to independently reconstruct it. This code is shared with F18 work and is not owned by this agent. Add the field to strict index contracts only with corresponding source-bound validation (`build_local_semantic_index.py`), preserve it in immutable graph payloads, and then carry it through retrieved rows and the quoted evidence formatter. Query-time rendering should distinguish source text from saved output and state “saved output; not re-executed; freshness unverified” without altering the quoted source. F15 framing protections must apply to any new metadata serialization.

Add stub-only source -> managed Reader -> SearchUnits -> adapter -> adaptive validation -> safe-index -> retrieval/formatting tests before claiming this reaches answers. Recheck `tests/test_local_graph_index_schema.py`, generation migration, answer framing and source-record payload verification. Do not publish a metadata field that the strict index rejects. Separate source-bound facts from any later policy for whether/how hidden content or saved outputs may answer a particular question.

Notebook image output/OCR state, unrepresented error output (`ename/evalue/traceback`), display updates, malformed/duplicate-key Notebook JSON, XLSX native/fallback display parity, hidden rows/columns, and PPTX slide state remain explicit untested or unimplemented F11/F12/Reader follow-ons. This slice does not certify complete Notebook support.

## Reproduction and rollback

Reviewed scope/limits: `f11-next-preflight.v1.md`. Fixture SHA-256 `123ff664e24f2cea7c3caac841e139f8574e7b2f9667701dd16e63f5086d80ad`. Observation script SHA-256 `b1513e1f0d188f05545b4087cb4fd26472a815d29fb33eededc2c69a385594ef`.

Initial supervised v1 child exited 0, but stdout buffering put observation messages after the terminal unittest footer. The supervisor correctly recorded `no_tests`, not PASS. The immutable v1 logs remain. `python3 -B design/local-memory-v1-hardening/runs/f11-next-rerun.v2.py` repeated the unchanged observer with `-u` and a fresh run directory: **3 observations, zero skips, 0.083649 s, 1072-byte log**, bounded to 30 s/1 MiB. A green observation test confirms current missing metadata, not a repaired product. The source bytes were compared before/after each observation.

No product rollback is required for this study. Keep its evidence as an append-only record. For a future implementation, preserve exact before hashes/diffs and reverse only that newly assigned hunk set if rejected; do not restore the whole shared Reader from HEAD. Generate fresh unpublished reader/index generations; never alter published generations or originals. Retain accepted F08/F09/F10/F15 changes and protected build/docs dirty files.
