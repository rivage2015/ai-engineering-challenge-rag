"""Bounded F02a runs using immutable pure tests and the accepted F18 guard.

The fully reviewed F04a worker is the guard F18 reused: worker network/process
launches are denied; fixture source writes total <=1 MiB; IO stays in reviewed
code/runtime and fresh synthetic roots. Only this supervisor creates a worker.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main():
    mode, run_id = sys.argv[1:3]
    if mode not in {"pure", "resolver", "e2e"} or not re.fullmatch(r"f02a-[a-z0-9-]{1,60}", run_id):
        raise SystemExit("invalid F02a run")
    if "--worker" in sys.argv:
        guard = load(RUNS / "f04a-executor-run.v1.py", "f02a_guard")
        return guard.worker(mode)
    if mode == "pure":
        command = [sys.executable, "-I", "-B", str(ROOT / "tests/test_year_only_supersession.py")]
    else:
        command = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), mode, run_id, "--worker"]
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f02a_supervisor")
    result = supervisor.run_bounded(command, RUNS / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
