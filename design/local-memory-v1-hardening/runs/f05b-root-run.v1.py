"""F05b app RED under the reviewed synthetic guards and bounded supervisor."""
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


def worker():
    guard = load(RUNS / "f04a-executor-run.v1.py", "f05b_guard")
    original = guard.load
    target = ROOT / "tests/test_decision_snapshot_e2e.py"
    guard.load = lambda path, name: original(target, "f05b_target") if path.name == "test_document_version_resolver.py" else original(path, name)
    return guard.worker("resolver")


def main():
    run_id = sys.argv[1]
    assert re.fullmatch(r"f05b-root-[a-z0-9-]+", run_id)
    if "--worker" in sys.argv:
        return worker()
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f05b_supervisor")
    result = supervisor.run_bounded(
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), run_id, "--worker"],
        RUNS / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
