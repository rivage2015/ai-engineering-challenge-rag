"""F05b bounded compatibility regressions; v1 and all earlier logs stay immutable.

Inherited guard confines all writes, denies model/network/process operations and
limits source write_text bytes. It is not a cumulative budget for every producer
artifact; root's strengthened acceptance runner verifies that separate limit.
"""
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


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def worker(mode):
    if mode in {"e2e", "focused", "migration"}:
        return load(RUNS / "f18-test-run.v2.py", "f05b_collateral").worker(mode)
    guard = load(RUNS / "f04a-executor-run.v1.py", "f05b_guard")
    original = guard.load
    target = None
    filenames = {"gate": ROOT / "tests/test_version_graph_validation_e2e.py",
                 "runtime": ROOT / "distribution/macos-local-memory/tests/test_runtime_recovery.py"}
    def redirected(path, name):
        nonlocal target
        if path.name == "test_document_version_resolver.py":
            target = original(filenames[mode], "f05b_target")
            if mode == "runtime":
                target.builder = types.SimpleNamespace()
            return target
        return original(path, name)
    guard.load = redirected
    original_suite = unittest.defaultTestLoader.loadTestsFromModule
    def selected(module, *args, **kwargs):
        if module is target and mode == "runtime":
            names = ("test_build_switches_to_storage_copy_only_after_base_is_ready",
                     "test_build_honors_storage_disable_change_during_registration",
                     "test_clean_first_build_rebuilds_semantic_after_gemma_pull",
                     "test_shadow_orchestrator_exception_does_not_abort_main_index")
            return unittest.TestSuite(module.RuntimeRecoveryTests(name) for name in names)
        return original_suite(module, *args, **kwargs)
    with mock.patch.object(unittest.defaultTestLoader, "loadTestsFromModule", selected):
        return guard.worker("resolver")


if __name__ == "__main__":
    mode, run_id = sys.argv[1:3]
    assert mode in {"e2e", "focused", "migration", "gate", "runtime"}
    assert re.fullmatch(r"f05b-executor-[a-z0-9-]+", run_id)
    if "--worker" in sys.argv:
        raise SystemExit(worker(mode))
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f05b_supervisor")
    result = supervisor.run_bounded(
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), mode, run_id, "--worker"],
        RUNS / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)
