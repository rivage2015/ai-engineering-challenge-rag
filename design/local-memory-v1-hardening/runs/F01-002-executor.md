# F01 / F07 executor handoff

Status: awaiting separate audit. No commit. No product acceptance assertion.

Changed production files: `engine/build_local_semantic_index.py` and `app/bootstrap.py`, both under `distribution/macos-local-memory/`. The former accepts and propagates explicit `--version-graph`, keeps strict legacy/versioned contexts, and stores the validated version binding. The latter supplies the argument, fingerprints resolver/projector code, and binds the same-generation version artifact in Reader registration. It does not infer a missing version path from a producer assertion. Base index publication already records a complete-file SHA-256, which also covers the new metadata; no new Keychain operation was added.

Tests: new `test_versioned_safe_index_e2e.py` runs 13 synthetic cases through real CLI parsing and production functions with only process/inference boundaries mocked. `test_reader_generation_migration.py` now creates the version graph before invoking the version-bound Reader, repairing its old fixture drift. Root `tests/test_semantic_lineage_relations.py` is unchanged and its 8 cases passed under the same guarded dispatcher.

Durable bounded full-wiring result: `artifacts/local-memory-v1-hardening/runs/f01-f07-executor-green-001/result.json`, 13 tests, 0 skips, exit 0. Python 3.9 build-only result: `artifacts/local-memory-v1-hardening/runs/f01-f07-python39-build-green-001/result.json`, 11 selected tests, 0 skips, exit 0. Full query execution requires a supported interpreter: the initial Python 3.9 query attempt failed at existing `zip(strict=True)` in `answer_local_memory.py`, so no Python 3.9 query PASS is claimed.

The following exact related-regression wrapper produced 15 PASS / 0.208 seconds on `/opt/homebrew/bin/python3` (3.14.6); this run's raw log existed only in tool output, not a file. Rerun under the bounded supervisor if durable raw evidence is required. It is important not to run the original migration fixture unguarded for this no-model test contract: normal Reader fingerprinting can query local model metadata.

```python
import importlib.util
import pathlib
import subprocess
import sys
import unittest
from unittest import mock
root = pathlib.Path.cwd()
pkgtests = root / 'distribution/macos-local-memory/tests'
sys.path.insert(0, str(pkgtests))
import test_versioned_safe_index_e2e as isolated
import test_reader_generation_migration as migration
harness = isolated.VersionedSafeIndexE2E()
harness.setUp()
original_loader = migration.load_bootstrap
def isolated_bootstrap():
    result = original_loader()
    result.run = harness.run_cli
    return result
def dispatch(command, **kwargs):
    return subprocess.CompletedProcess(command, 0, harness.run_cli(command), '')
try:
    spec = importlib.util.spec_from_file_location('lineage_regression', root / 'tests/test_semantic_lineage_relations.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    suite = unittest.TestSuite((unittest.defaultTestLoader.loadTestsFromModule(module), unittest.defaultTestLoader.loadTestsFromModule(migration)))
    with mock.patch.object(subprocess, 'run', side_effect=dispatch), mock.patch.object(migration, 'load_bootstrap', side_effect=isolated_bootstrap):
        result = unittest.TextTestRunner(verbosity=2).run(suite)
finally:
    harness.doCleanups()
raise SystemExit(not result.wasSuccessful())
```

Scope limits: no changes to year/version semantics, resolver implementation, shared Reader parser, F18 Validator publication behavior, or user-dirty build/docs files. Cross-document semantic feature flags are explicitly disabled in these wiring tests, so no Keychain access or semantic trust publication is claimed. All sources and app state are temporary synthetic fixtures. Model responses are fixed mocks, not independently correct semantic judgments. A generic positive answer traverses the real final audit contracts, while the ambiguous owner question remains rejected even when the fake model offers a supported value.

The adapter skill guided file ownership, integration, regression and rollback boundaries. The audit skill requires the separate auditor to decide; this executor record is not that decision. Full source hashes and limits are in `F01-002-executor.json`. Preserve both RED and intermediate-failure notes when resuming; do not overwrite prior runs.
