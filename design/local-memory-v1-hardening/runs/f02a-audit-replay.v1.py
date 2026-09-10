"""Independent replay of frozen F02a suites under unchanged bounded guards."""
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


def main():
    mode, run_id = sys.argv[1:3]
    if mode not in {"pure", "resolver", "e2e"} or not run_id.startswith("f02a-audit-replay-"):
        raise SystemExit("invalid frozen audit replay")
    if "--worker" in sys.argv:
        if mode == "pure":
            raise SystemExit("pure suite uses its own reviewed IO guard")
        guard = load(RUNS / "f04a-executor-run.v1.py", "f02a_audit_replay_guard")
        return guard.worker(mode)
    if mode == "pure":
        command = [sys.executable, "-I", "-B", str(ROOT / "tests/test_year_only_supersession.py")]
    else:
        command = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), mode, run_id, "--worker"]
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f02a_audit_replay_supervisor")
    result = supervisor.run_bounded(command, RUNS / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
