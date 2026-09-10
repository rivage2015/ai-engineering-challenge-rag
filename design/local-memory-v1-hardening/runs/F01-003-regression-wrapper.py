"""Reproduce related F01 regressions under a bounded synthetic CLI dispatcher."""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[3]


def main():
    if "--worker" not in sys.argv:
        spec = importlib.util.spec_from_file_location(
            "bounded_f01_supervisor", ROOT / "scripts/run_local_memory_hardening_tests.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.run_bounded(
            [sys.executable, "-B", str(pathlib.Path(__file__).resolve()), "--worker"],
            ROOT / "artifacts/local-memory-v1-hardening/runs/f01-f07-repair1-related-regression-001",
            cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
        )
        print(result)
        return 0 if result["status"] == "passed" else 1
    sys.path.insert(0, str(ROOT / "distribution/macos-local-memory/tests"))
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
        return subprocess.CompletedProcess(command, 0, harness.run_cli(command), "")

    try:
        spec = importlib.util.spec_from_file_location(
            "lineage_regression", ROOT / "tests/test_semantic_lineage_relations.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        suite = unittest.TestSuite((
            unittest.defaultTestLoader.loadTestsFromModule(module),
            unittest.defaultTestLoader.loadTestsFromModule(migration),
        ))
        with (
            mock.patch.object(subprocess, "run", side_effect=dispatch),
            mock.patch.object(migration, "load_bootstrap", side_effect=isolated_bootstrap),
        ):
            result = unittest.TextTestRunner(verbosity=2).run(suite)
    finally:
        harness.doCleanups()
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
