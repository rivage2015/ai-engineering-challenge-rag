"""F05b bounded selected collateral; preserve original policy residual labels."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import re
import sys
import types

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"
MODES = {"f05a-pure", "app", "unmarked", "unmarked-pure", "year", "focused", "lineage", "security", "migration", "e2e", "resolver"}

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

def worker(mode):
    if mode != "f05a-pure":
        return load(RUNS / "f05a-root-run.v2.py", "f05b_old_collateral").worker(mode)
    guard = load(RUNS / "f04a-executor-run.v1.py", "f05b_old_pure_guard")
    original = guard.load
    class EmptyHarness:
        def setUp(self):
            pass
        def doCleanups(self):
            pass
    def redirect(path, name):
        if path.name == "test_versioned_safe_index_e2e.py":
            return types.SimpleNamespace(VersionedSafeIndexE2E=EmptyHarness)
        if path.name == "test_document_version_resolver.py":
            target = original(ROOT / "tests/test_version_graph_reconstruction.py", "f05b_f05a_pure")
            target.builder = types.SimpleNamespace()
            return target
        return original(path, name)
    guard.load = redirect
    return guard.worker("resolver")

def main():
    mode, run_id = sys.argv[1:3]
    assert mode in MODES and re.fullmatch(r"f05b-root-[a-z0-9-]+", run_id)
    if "--worker" in sys.argv:
        return worker(mode)
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f05b_collateral_supervisor")
    if mode in {"unmarked-pure", "year"}:
        filename = "test_unmarked_version_candidates.py" if mode == "unmarked-pure" else "test_year_only_supersession.py"
        command = [sys.executable, "-I", "-B", str(ROOT / "tests" / filename)]
    else:
        command = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), mode, run_id, "--worker"]
    result = supervisor.run_bounded(command, RUNS / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1

if __name__ == "__main__":
    raise SystemExit(main())
