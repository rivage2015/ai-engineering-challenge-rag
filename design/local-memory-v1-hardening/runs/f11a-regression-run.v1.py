"""Fixed synthetic regression gold; preserve every attempt in a fresh directory."""
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


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def worker(mode):
    target_path = HERE / ("f11a-" + mode + "-regression-gold.v1.py")
    guard = load(HERE / "f04a-executor-run.v1.py", "f11a_regression_guard")
    budget = load(HERE / "f05b-fixture-budget.v1.py", "f11a_regression_budget")
    budget.AUTHORS = {
        str(target_path), str(ROOT / "tests/test_local_embedded_visual_pipeline.py"),
        str(ROOT / "distribution/macos-local-memory/tests/test_versioned_safe_index_e2e.py"),
    }
    original_load = guard.load
    original_suite = unittest.defaultTestLoader.loadTestsFromModule
    target = None

    def redirect(path, name):
        nonlocal target
        if path.name == "test_document_version_resolver.py":
            target = original_load(target_path, "f11a_regression_gold")
            target.builder = types.SimpleNamespace()
            return target
        return original_load(path, name)

    def selected(module, *args, **kwargs):
        if module is target:
            cls = module.ProjectorProducerIdentityTests if mode == "app" else module.NotebookVisualBoundaryGold
            methods = module.METHOD_NAMES if mode == "app" else module.METHODS
            assert len(methods) == (9 if mode == "app" else 3)
            return unittest.TestSuite(cls(name) for name in methods)
        return original_suite(module, *args, **kwargs)

    guard.load = redirect
    with budget.enforce(), mock.patch.object(unittest.defaultTestLoader, "loadTestsFromModule", selected):
        return guard.worker("resolver")


def main():
    mode, run_id = sys.argv[1:3]
    if mode not in {"app", "image"} or not re.fullmatch("f11a-regression-" + mode + r"-[0-9]{3}", run_id):
        raise SystemExit("invalid fixed scope")
    if "--worker" in sys.argv:
        return worker(mode)
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f11a_regression_supervisor")
    result = supervisor.run_bounded(
        [sys.executable, "-I", "-B", "-u", str(Path(__file__).resolve()), mode, run_id, "--worker"],
        HERE / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
