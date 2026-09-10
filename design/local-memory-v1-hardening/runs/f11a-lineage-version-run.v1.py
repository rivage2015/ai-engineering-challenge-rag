"""Two fixed lineage version controls, existing guarded 30-second worker."""
import importlib.util
import json
from pathlib import Path
import re
import sys
import types
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
TARGET = HERE / "f11a-lineage-version-gold.v1.py"
METHODS = (
    "test_current_version_preserves_complete_original_fan_in_assertions",
    "test_historical_version_is_rejected_with_exact_provenance_error",
)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def worker():
    guard = load(HERE / "f04a-executor-run.v1.py", "f11a_lineage_version_guard")
    budget = load(HERE / "f05b-fixture-budget.v1.py", "f11a_lineage_version_budget")
    budget.AUTHORS = {
        str(TARGET), str(ROOT / "tests/test_semantic_lineage_relations.py"),
        str(ROOT / "distribution/macos-local-memory/tests/test_versioned_safe_index_e2e.py"),
    }
    original_load = guard.load
    target = None
    selections = 0

    def redirect(path, name):
        nonlocal target
        if path.name == "test_document_version_resolver.py":
            if target is not None:
                raise AssertionError("duplicate lineage target load")
            target = original_load(TARGET, "f11a_lineage_version_gold")
            target.builder = types.SimpleNamespace()
            return target
        return original_load(path, name)

    def selected(module, *args, **kwargs):
        nonlocal selections
        if target is None or module is not target or selections:
            raise AssertionError("unexpected lineage suite discovery")
        selections += 1
        return unittest.TestSuite(module.LineageVersionGold(name) for name in METHODS)

    guard.load = redirect
    with budget.enforce(), mock.patch.object(unittest.defaultTestLoader, "loadTestsFromModule", selected):
        outcome = guard.worker("resolver")
    if selections != 1:
        raise AssertionError("lineage suite not executed")
    return outcome


def main():
    if len(sys.argv) not in (2, 3):
        raise SystemExit("fresh run ID required")
    run_id = sys.argv[1]
    if not re.fullmatch(r"f11a-lineage-version-[0-9]{3}", run_id):
        raise SystemExit("invalid fixed scope")
    if len(sys.argv) == 3:
        if sys.argv[2] != "--worker":
            raise SystemExit("invalid worker option")
        return worker()
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f11a_lineage_version_supervisor")
    result = supervisor.run_bounded(
        [sys.executable, "-I", "-B", "-u", str(Path(__file__).resolve()), run_id, "--worker"],
        HERE / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
