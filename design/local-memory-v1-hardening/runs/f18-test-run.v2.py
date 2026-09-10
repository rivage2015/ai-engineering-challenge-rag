"""F18 guarded runner with reviewed security/migration regression slots.

Keeps v1 and its records immutable. Guard and the real F01 CLI dispatcher are
reused; only metadata/model/process boundaries are stubbed. Not an OS sandbox.
"""
from __future__ import annotations
import contextlib
import importlib.util
import json
from pathlib import Path
import sys
import types
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
    guard = load(RUNS / "f04a-executor-run.v1.py", "f18_guard")
    original_load = guard.load
    targets = {
        "focused": ROOT / "tests/test_immutable_lineage_validation.py",
        "lineage": ROOT / "tests/test_semantic_lineage_relations.py",
        "resolver": ROOT / "distribution/macos-local-memory/tests/test_document_version_resolver.py",
        "security": ROOT / "tests/test_security_graph_partition.py",
        "migration": ROOT / "distribution/macos-local-memory/tests/test_reader_generation_migration.py",
    }
    with contextlib.ExitStack() as patches:
        def redirected(path, name):
            if mode not in targets or path.name != "test_document_version_resolver.py":
                return original_load(path, name)
            target = original_load(targets[mode], "f18_target")
            if mode in {"lineage", "security"}:
                target.builder = target.adaptive_builder
            if mode == "migration":
                target.builder = types.SimpleNamespace()
                original_bootstrap = target.load_bootstrap

                def isolated_bootstrap():
                    bootstrap = original_bootstrap()
                    bootstrap.run = lambda command, log=None: target.builder.run_tool("migration", command, None, log)
                    return bootstrap

                def dispatch(command, **_kwargs):
                    stdout = target.builder.run_tool("migration", command, None, None)
                    return target.subprocess.CompletedProcess(command, 0, stdout, "")

                patches.enter_context(mock.patch.object(target, "load_bootstrap", side_effect=isolated_bootstrap))
                patches.enter_context(mock.patch.object(target.subprocess, "run", side_effect=dispatch))
            return target

        guard.load = redirected
        return guard.worker("e2e" if mode == "e2e" else "resolver")


def main():
    mode, run_id = sys.argv[1:3]
    if mode not in {"focused", "lineage", "resolver", "e2e", "security", "migration"} or not run_id.startswith("f18-"):
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
