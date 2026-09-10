"""F05a bounded suite with inherited IO guard and no app imports for pure tests."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import sys
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"
RED = (
    "test_rejects_empty_resealed_graph",
    "test_rejects_omitted_unmarked_candidate",
    "test_rejects_absent_inventory_candidate",
    "test_rejects_resealed_candidate_field_changes",
    "test_rejects_duplicated_or_reassigned_candidate",
    "test_rejects_fabricated_automatic_selection",
    "test_rejects_embedded_human_authority_without_reading_graph_path",
    "test_rejects_resealed_redundant_semantic_changes",
)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    mode, run_id = sys.argv[1:3]
    if mode not in {"red", "pure", "resolver"} or not re.fullmatch(r"f05a-executor-[a-z0-9-]{1,65}", run_id):
        raise SystemExit("invalid F05a executor run")
    if "--worker" in sys.argv:
        guard = load(RUNS / "f04a-executor-run.v1.py", "f05a_guard")
        if mode == "resolver":
            raise SystemExit(guard.worker("resolver"))
        original = guard.load
        target = None

        class EmptyHarness:
            def setUp(self):
                pass

            def doCleanups(self):
                pass

        def redirected(path, name):
            global target
            if path.name == "test_versioned_safe_index_e2e.py":
                # Preserve only the guard lifecycle; no E2E module is imported.
                return types.SimpleNamespace(VersionedSafeIndexE2E=EmptyHarness)
            if path.name == "test_document_version_resolver.py":
                target = original(ROOT / "tests/test_version_graph_reconstruction.py", "f05a_pure")
                target.builder = types.SimpleNamespace()
                return target
            return original(path, name)

        guard.load = redirected
        original_suite = unittest.defaultTestLoader.loadTestsFromModule

        def select(module, *args, **kwargs):
            if module is target and mode == "red":
                return unittest.TestSuite(module.DocumentVersionGraphValidationTests(name) for name in RED)
            return original_suite(module, *args, **kwargs)

        with mock.patch.object(unittest.defaultTestLoader, "loadTestsFromModule", select):
            raise SystemExit(guard.worker("resolver"))
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f05a_supervisor")
    result = supervisor.run_bounded(
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), mode, run_id, "--worker"],
        RUNS / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)
