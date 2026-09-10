"""Fixed nine-method F11a existing-API RED/GREEN; no product edit authority.

Reuse the reviewed F04a confined-I/O/model/process guard and existing bounded
supervisor unchanged. Add the reviewed explicit test-write budget around it.
This is not an OS sandbox, total product artifact cap, or a production RSS claim.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
TARGET = ROOT / "tests/test_notebook_metadata_binding.py"
METHODS = (
    "test_g1_probe_emits_literal_six_states_and_preserves_source",
    "test_g2_missing_state_rejected_native",
    "test_g2_missing_state_rejected_stream_schema",
    "test_g2_missing_state_rejected_stream_structural",
    "test_g2_shape_valid_wrong_count_rejected_native",
    "test_g2_shape_valid_wrong_count_rejected_stream_schema",
    "test_g2_shape_valid_wrong_count_rejected_stream_structural",
    "test_g3_direct_search_copies_all_six_literal_states",
    "test_g6_non_notebook_retains_exact_old_counts",
)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def worker():
    guard = load(HERE / "f04a-executor-run.v1.py", "f11a_executor_guard")
    budget = load(HERE / "f05b-fixture-budget.v1.py", "f11a_executor_explicit_budget")
    # The unchanged budget implementation counts direct Path writes by these
    # reviewed authors; producer .open() writes remain confined, not counted.
    budget.AUTHORS = {
        str(TARGET),
        str(ROOT / "distribution/macos-local-memory/tests/test_versioned_safe_index_e2e.py"),
    }
    original_load = guard.load
    original_suite = unittest.defaultTestLoader.loadTestsFromModule
    target = None

    def redirect(path, name):
        nonlocal target
        if path.name == "test_document_version_resolver.py":
            target = original_load(TARGET, "f11a_executor_metadata_tests")
            # Required by the inherited dispatcher's run_tool assignment. The
            # selected nine tests never invoke this app/Reader dispatcher.
            target.builder = types.SimpleNamespace()
            return target
        return original_load(path, name)

    def selected(module, *args, **kwargs):
        if module is target:
            return unittest.TestSuite(module.NotebookMetadataBindingTests(name) for name in METHODS)
        return original_suite(module, *args, **kwargs)

    guard.load = redirect
    with budget.enforce(), mock.patch.object(
        unittest.defaultTestLoader, "loadTestsFromModule", selected,
    ):
        return guard.worker("resolver")


def main():
    run_id = sys.argv[1]
    if run_id not in {"f11a-executor-red-initial-001", "f11a-executor-green-initial-001"}:
        raise SystemExit("invalid fixed F11a run id")
    if "--worker" in sys.argv:
        return worker()
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f11a_executor_supervisor")
    result = supervisor.run_bounded(
        [sys.executable, "-I", "-B", "-u", str(Path(__file__).resolve()), run_id, "--worker"],
        HERE / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
