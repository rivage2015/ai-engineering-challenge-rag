"""F05a v2 adds previously guarded pure policy collateral; v1 stays immutable."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import re
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
    if mode in {"app", "unmarked"}:
        guard = load(RUNS / "f04a-executor-run.v1.py", "f05a_guard")
        original = guard.load
        target = ROOT / "tests" / ("test_version_graph_validation_e2e.py" if mode == "app" else "test_unmarked_version_e2e.py")
        guard.load = lambda path, name: original(target, "f05a_target") if path.name == "test_document_version_resolver.py" else original(path, name)
        return guard.worker("resolver")
    return load(RUNS / "f18-test-run.v2.py", "f05a_collateral").worker(mode)


def main():
    mode, run_id = sys.argv[1:3]
    assert mode in {"app", "unmarked", "e2e", "resolver", "focused", "lineage", "migration", "security", "unmarked-pure", "year"}
    assert re.fullmatch(r"f05a-root-[a-z0-9-]+", run_id)
    if "--worker" in sys.argv:
        return worker(mode)
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f05a_supervisor")
    if mode in {"unmarked-pure", "year"}:
        filename = "test_unmarked_version_candidates.py" if mode == "unmarked-pure" else "test_year_only_supersession.py"
        command = [sys.executable, "-I", "-B", str(ROOT / "tests" / filename)]
    else:
        command = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), mode, run_id, "--worker"]
    result = supervisor.run_bounded(
        command,
        RUNS / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
