# F05a ownership addendum 001

The immutable base contract is `f05a-task-contract.v1.md`, SHA-256 `89960be4254752ba3ae387610359c3f6f8200cb57c3d7a9190398c723473e4df`. No scope or acceptance criterion is weakened.

Read-only preflight found a second literal resolver-version assertion in `tests/test_unmarked_version_candidates.py` (before SHA-256 `b4a9176f3f7bb723d701a548418be8647a5336dc7b88dd0707d03cfa70eb5b94`, line 284). Root is authorized to change only that assertion from `0.1.3` to `0.1.4` after true RED and implementation authorization. Preserve an exact scoped delta and before hash. No other changes to that existing test or its earlier F03a snapshots/artifacts. The executor does not own this file.

The new executor pure test remains the base-contract path `tests/test_version_graph_reconstruction.py`; the alternative filename in the proposal is not assigned. Nonfinite JSON constants are malformed JSON within the base contract, not accepted numbers. This addendum is fixed before any F05a product edit or test run.
