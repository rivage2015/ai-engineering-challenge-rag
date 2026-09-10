# F11a formal-review artifact preparation — draft v1

Same task `lms-v1-f11a-notebook-metadata-2026-09-09`; integration correction1 retained, formal audit repair round0. Adapter / Agentic Audit were used for source/delta provenance and executor/auditor separation. This handoff is **not** audit dispatch, semantic approval, or F11/V1 completion.

## Frozen pair

- `f11a-graph-artifact.v1.json`: `b0b63af872e7b669f791941bb8938e1a813f81e38c37c4f4aeece58f1f4b1843`
- `f11a-source-packet.v1.json`: `b5d9bd12bd109432d9fa61c3b40589115610349bfd9a9ab7977f381efda0ba45`
- Generator `f11a-final-artifact-generator.v2.py`: `19e9e45ca189f89a3aa28c3e3c6b5059e272605bc30ebb1ce253ab5a0fbe4f2e`

Paths above are in `design/local-memory-v1-hardening/runs/`. Root must verify the pair, current sources, retained gold and run receipts before dispatch. A later broader-coverage packet should be a new version, not an overwrite of these files.

The artifact has 11 nodes, 8 meaningful edges and 6 explicit required paths. Every node/edge has source-ID basis; source IDs are listed in the artifact and source packet. The packet has 170 explicitly selected files, 19 preserved run receipts/log groups, and 18 complete before→delta→after chains (10 initial product, 6 image correction, 1 index correction, 1 additive gold). For image-repaired products, initial deltas target the immutable pre-image snapshot, not the current file. Historical manifests are retained as historical evidence; they do not define current source authority.

Current claims remain separated: metadata30; app4; image3; legacy mocked-image3; additional image6; current-index-only pure9. The three residual methods are **not** three defended attacks or three added acceptance methods. They preserve the witnessed coherent-body, incomplete-membership and Search-only boundaries. Root's original boundary4 GREEN is historical, not a newly rerun current-source assertion in this packet.

## Mechanical generation and limitations

The generator used only standard-library bounded reads, JSON/AST parsing, SHA-256 and text-delta comparison. It did not import any product/test module, run tests, edit products, scan/discover arbitrary files, access originals, or call network/model/subprocess/GUI. It emits stdout; artifacts were saved through apply_patch.

Bounds: at most 250 listed files, 2 MiB per file, 24 MiB total reads, 30 seconds checked between reads, 2 MiB output. Actual selected reads: 170 files / 3,922,887 bytes. These are generation guards, not an OS/RSS guarantee. Direct relevant dependencies are listed, not asserted to be exhaustive transitive runtime discovery.

Generation verified current eleven product pins, preserved before hashes, all 18 forward/inverse deltas, permanent metadata gold2 byte equality, complete original gold1 AST after removal of only the predeclared additive class, exact selected method lists, all 19 result/start/log bindings and terminal footers. This is structural/provenance verification, not independent semantic audit.

Generator v1 stopped because original root RED progress had several methods on the same line; the line-start-only parser counted 2 instead of the footer's 4. Its code and `f11a-artifact-generation-attempt-001.log` remain unchanged. v2 reads exact progress tokens before tracebacks, retains strict footer/count/method/hash checks and succeeds. No original log, receipt, result, gold or product was changed. `f11a-artifact-generation-result.v1.json` records both attempts and the output pair.

Broader collateral under root review is not included or claimed. Formal audit remains pending. Raw body/pointer correspondence, whole membership, source-image membership, recognition/execution/freshness, actual packaged app/RSS guarantees and mandatory F11b downstream propagation remain excluded/open exactly as the contract states.
