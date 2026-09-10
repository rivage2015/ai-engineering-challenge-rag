"""Six fixed reviewed additional image controls; no inherited discovery."""
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
TARGET = HERE / "f11a-image-additional-gold.v1.py"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def worker():
    guard = load(HERE / "f04a-executor-run.v1.py", "f11a_additional_guard")
    budget = load(HERE / "f05b-fixture-budget.v1.py", "f11a_additional_budget")
    budget.AUTHORS = {
        str(TARGET), str(HERE / "f11a-image-regression-gold.v1.py"),
        str(ROOT / "tests/test_local_embedded_visual_pipeline.py"),
        str(ROOT / "distribution/macos-local-memory/tests/test_versioned_safe_index_e2e.py"),
    }
    original_load = guard.load
    original_suite = unittest.defaultTestLoader.loadTestsFromModule
    target = None

    def redirect(path, name):
        nonlocal target
        if path.name == "test_document_version_resolver.py":
            target = original_load(TARGET, "f11a_additional_gold")
            target.builder = types.SimpleNamespace()
            return target
        return original_load(path, name)

    def selected(module, *args, **kwargs):
        if module is target:
            assert len(module.METHODS) == 6
            return unittest.TestSuite(module.NotebookVisualAdditionalGold(name) for name in module.METHODS)
        return original_suite(module, *args, **kwargs)

    guard.load = redirect
    with budget.enforce(), mock.patch.object(unittest.defaultTestLoader, "loadTestsFromModule", selected):
        return guard.worker("resolver")


def main():
    run_id = sys.argv[1]
    if not re.fullmatch(r"f11a-image-additional-[0-9]{3}", run_id):
        raise SystemExit("invalid fixed scope")
    if "--worker" in sys.argv:
        return worker()
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f11a_additional_supervisor")
    result = supervisor.run_bounded(
        [sys.executable, "-I", "-B", "-u", str(Path(__file__).resolve()), run_id, "--worker"],
        HERE / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
