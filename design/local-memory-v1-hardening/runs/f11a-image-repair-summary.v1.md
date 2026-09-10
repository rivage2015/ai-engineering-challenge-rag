# F11a integration correction1 — image/text boundary handoff

## Authority, scope, and result

Implemented under `f11a-image-repair-gate.v1.md` (`0b9e6252a549c29989f6209e5a2258f3a5edbfe5c10098fe807f919bca475d4b`) and the complete design sections 3–6 (`b63e7fdc3161aed6cb61466770ecfe720018f8872df429a357dbb9817b9afc48`). This is the image component of the same F11a **integration correction1**, not a new task, reset repair counter or formal acceptance. Adapter / Agentic Audit required the isolated before/gold/delta trail and separate root/auditor acceptance.

Only the six assigned products were edited. Fresh independent image gold 3/3 and unchanged legacy image 3/3 pass, each with zero ERROR, SKIP or expected failures. Positive producer baselines now reach the gold's native-output label/parent attacks and visual state/parent/origin attacks. Previous 001 failures remain untouched; normal-baseline errors are not retrospectively counted as defended negatives.

## Implementation boundary

- Probe now exposes the closed `classify_notebook_evidence(..., parent_lookup=None)` kinds `native_text`, `visual_text`, `unparsed_text`, and `not_applicable` (`scripts/probe_intermediate_records.py:2941`). The compatible `notebook_evidence_state` wrapper returns only state while retaining validation (`:3005`).
- Visual exclusion checks the two exact provisional methods, quality/marker/question-independent fields, absence of the notebook-state key, actual parent image ID/type/same Document, canonical origin, parent producer locator and child observation/transcript locator (`:2842`). Visual labels alone cannot opt native canonical cell/output text out. Invalid candidates raise; they do not silently become unparsed/not-applicable.
- The former Search `_visual_origin_errors` helper moved to Probe (`:2732`), with byte-identical function text, identical suffix mapping / hash pattern and an explicit Search import alias. No circular import, new shipped module or changed non-Notebook helper semantics.
- `notebook_document_binding` (`:3015`) excludes valid visual records only from Notebook metadata checked/unchecked counts. Raw record counts still include them; partial/failed `notebook_extraction_incomplete`, rootless/resource limits and zero-text `no_textual_records_checked` remain. Source metadata/raw-body/membership guarantees were not expanded.
- Intermediate native passes its existing Evidence map; intermediate stream uses a parameter-bound one-row SQLite query. Search native accepts optional parent lookup and defaults to its real map; Search stream provides its one-row SQLite lookup even when parent image is not a unit source ID.
- `DocumentDeriver` preserves four positional arguments and adds keyword-only `parent_lookup` / `document` (`scripts/build_search_units.py:278`). Both `consume` and `add_direct_text` invoke the strict helper. Its private fallback stores JSON snapshots of Notebook image records only; duplicate IDs reject, `flush_image` does not discard this lookup, and `finish` clears it. An external lookup avoids another store. This supports existing parent-before-child/deferred emission, not arbitrary unindexed stream ordering.
- Index remains the earlier correction hash `c70f36d9…`. Schemas, managed extractor, adaptive Validator, app, all gold and version/resource constants were not edited by this image correction.

## Fresh runs, actual outcomes and durable evidence

Paths below are relative to `design/local-memory-v1-hardening/runs/`. Every run retains its `started.json`, complete `unittest.log`, and `result.json`.

| Run | Interpreter / result | Time / log size | Log SHA-256 | Result SHA-256 |
| --- | --- | --- | --- | --- |
| `f11a-regression-image-001` | Root original 3.9; 3 methods, 2 FAIL + 7 ERROR in subcases; baseline regression retained | Original receipt | `dfa209febe47a573e7111eae819b2fb60df34031601acb9ee9f51412d8089b46` | `b2c2bf6e0ab99a522ccc78e59dc9b8ad9363a2360868cda4ec4bd42ec75a5577` |
| `f11a-root-images-001` | Root original 3.14; 3 methods, 1 ERROR / 2 PASS | 0.188288 s / 2585 bytes | `c438ee4a714abc862d6a022df1d21bade07cf48098ad6d37d305006eab10be1e` | `984c182b0e3eaa8ea70c54e118c28050ee1d9a12f44a6d471f27d4dfd411ca13` |
| `f11a-regression-image-002` | Installed 3.9 with jsonschema; **3 PASS**, 0 ERROR/SKIP | 0.550352 s / 615 bytes | `823f3ab7dbca0c3d0556115ec6061334fc9c1177e9f18a7cb1698e8d8a1a99a7` | `8c09b06af4e7452cd0f9512dbdad3a5e63d5d6bd217aae3adcb74f39ad5bf948` |
| `f11a-root-images-002` | Installed 3.14; **3 PASS**, 0 ERROR/SKIP | 0.202992 s / 813 bytes | `eac45c987ba4e6a00d7c9231ea5d75f4231fb5528e54608f99a4685143b40aa9` | `cce8fd82db89dbb13386a2c32e33820e1764947d1a588d1dc7907288e6723ba3` |

Exact authorized commands, from repository root:

```text
rag/.venv/bin/python -I -B design/local-memory-v1-hardening/runs/f11a-regression-run.v1.py image f11a-regression-image-002
/opt/homebrew/opt/python@3.14/bin/python3.14 -I -B design/local-memory-v1-hardening/runs/f11a-root-image-run.v1.py f11a-root-images-002
```

Both use the reviewed wrappers, one supervised worker, 30-second and 1-MiB log ceilings, F04 confinement/network/process guards and F05b cumulative explicit fixture budget. The first records 145538 explicitly authored bytes / 45 writes, maximum source fixture 392 bytes; the second 14472 bytes / 7 writes, maximum source fixture 418 bytes. No test retry, gold alteration, dependency installation, real model/network/GUI/user-document/production-state access, package build, commit or push occurred. One malformed orchestration-JavaScript invocation failed before any tool action during helper preparation; it was not a product/test execution or a semantic RED.

## Current product hashes and recovery

| Product | SHA-256 |
| --- | --- |
| `scripts/probe_intermediate_records.py` | `1320cda390ab671dc2df82678ed2237e12aa4828b29666e3fb3d5b8a734f456d` |
| `scripts/build_search_units.py` | `55f0284acd6462260ba4962fa77246fb78090aed9b42554146e49723728c20df` |
| `scripts/validate_search_units.py` | `c1bb29855c8b390c4af8334e9641af0ad069279cc7ea86faf25fddd85c7f500b` |
| `scripts/validate_search_units_streaming.py` | `54851bb4500a8eb82a9dada65c5bec7cbcdce212fd70852f430b3f703ac24059` |
| `scripts/validate_intermediate_records.py` | `91573a6216a1cf1814c36452e36752c08af46ef6dd393020830f7d82df87ae31` |
| `scripts/validate_intermediate_records_streaming.py` | `e7f5a53dc9822f8cccd1f0fa60c3c6873605575b4297e4c888b350dbfe56928a` |

Before any edit, six new `f11a-image-repair-before-{id}.v1.py` snapshots were saved by apply_patch and each compared byte-exact to its product and design hash. Six new after snapshots and isolated `f11a-image-repair-{id}-delta.v1.diff` files are retained. `f11a-image-repair-mechanical-check.v1.json` records complete forward/inverse delta equality and exact after snapshot comparisons. `f11a-image-repair-coherent.v1.json` pins current products, all before/after/deltas, guard/gold and all attempts. No historical F11a artifacts were modified.

Rollback only after verifying every current target hash: apply the six isolated inverses and compare full bytes/hashes against their recorded before snapshots. Do not reset the worktree or undo the independent index correction. No rollback was executed.

## Explicit remaining work and non-claims

The original metadata33 and app regressions were not rerun by this image executor; root owns fresh postfreeze batches. Additional controls for multi-image deferred lookup lifetime, current unlocated transcript, data-URI visual prefix, missing/cross-document parents and non-Notebook preservation remain subject to root/reviewer control selection; these six passing methods do not by themselves establish every section7(a–e) path. Legacy attachment behavior was exercised, but do not infer complete image-format coverage.

The per-Document image map is O(that Notebook's image records), not an aggregate RSS/job-disk guarantee. Coherently forged visual bodies/parents/origins, original image membership, complete child/chunk membership, VLM execution or recognition correctness remain outside metadata-only attestation. Index/app-visible Notebook-state propagation is the mandatory separate F11b follow-on and remains open. No complete F11/V1, package or formal audit PASS is claimed.
