"""Four fixed app controls; launch only after product source freeze."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import re
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
TARGET = ROOT / "tests/test_notebook_metadata_application.py"
METHODS = (
    "test_unverified_notebook_stops_actual_bridge_and_preserves_public_generation",
    "test_changed_reader_contract_holds_old_generation_without_mutating_it",
    "test_explicit_rebuild_creates_new_generation_and_keeps_previous_bytes",
    "test_probe_and_schemas_are_in_existing_package_copy_contract",
)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def worker():
    # Sole extra preflight-authorized read, never execute the package script.
    package_path = ROOT / "distribution/macos-local-memory/build/build_package.sh"
    package_text = package_path.read_text(encoding="utf-8")
    guard = load(HERE / "f04a-executor-run.v1.py", "f11a_app_guard")
    budget = load(HERE / "f05b-fixture-budget.v1.py", "f11a_app_budget")
    budget.AUTHORS = {str(TARGET), str(ROOT / "distribution/macos-local-memory/tests/test_versioned_safe_index_e2e.py")}
    original_load = guard.load
    original_suite = unittest.defaultTestLoader.loadTestsFromModule
    target = None

    def redirect(path, name):
        nonlocal target
        if path.name == "test_document_version_resolver.py":
            target = original_load(TARGET, "f11a_app_controls")
            target.NotebookApplicationTests.package_copy_text = package_text
            return target
        return original_load(path, name)

    def selected(module, *args, **kwargs):
        if module is target:
            return unittest.TestSuite(module.NotebookApplicationTests(name) for name in METHODS)
        return original_suite(module, *args, **kwargs)

    guard.load = redirect
    with budget.enforce(), mock.patch.object(unittest.defaultTestLoader, "loadTestsFromModule", selected):
        return guard.worker("resolver")


def main():
    run_id = sys.argv[1]
    if not re.fullmatch(r"f11a-root-app-[0-9]{3}", run_id):
        raise SystemExit("invalid fixed scope run id")
    if "--worker" in sys.argv:
        return worker()
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f11a_app_supervisor")
    result = supervisor.run_bounded(
        [sys.executable, "-I", "-B", "-u", str(Path(__file__).resolve()), run_id, "--worker"],
        HERE / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
