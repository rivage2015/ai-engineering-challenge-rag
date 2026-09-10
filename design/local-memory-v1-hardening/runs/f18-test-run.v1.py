"""Reuse the reviewed frozen F04a guards for F18 small synthetic suites.

Product code is not patched by this wrapper. The only substitution redirects
the old resolver-suite slot to the selected F18 test file. The original F01
dispatcher still stubs runtime metadata, models and process boundaries.
"""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def worker(mode):
    guard = load(RUNS / "f04a-executor-run.v1.py", "f18_guard")
    original_load = guard.load
    targets = {
        "focused": ROOT / "tests/test_immutable_lineage_validation.py",
        "lineage": ROOT / "tests/test_semantic_lineage_relations.py",
        "resolver": ROOT / "distribution/macos-local-memory/tests/test_document_version_resolver.py",
    }

    def redirected(path, name):
        if mode in targets and path.name == "test_document_version_resolver.py":
            target = original_load(targets[mode], "f18_target")
            if mode == "lineage":
                target.builder = target.adaptive_builder
            return target
        return original_load(path, name)

    guard.load = redirected
    return guard.worker("e2e" if mode == "e2e" else "resolver")


def main():
    mode, run_id = sys.argv[1:3]
    if mode not in {"focused", "lineage", "resolver", "e2e"} or not run_id.startswith("f18-"):
        raise SystemExit("invalid F18 run")
    if "--worker" in sys.argv:
        return worker(mode)
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f18_supervisor")
    result = supervisor.run_bounded(
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), mode, run_id, "--worker"],
        RUNS / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
