#!/usr/bin/env python3
"""Bounded F03a pure and existing resolver runs; immutable run IDs only."""
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


if __name__ == "__main__":
    mode, run_id = sys.argv[1:3]
    if mode not in {"pure", "resolver", "year"} or not re.fullmatch(r"f03a-executor-[a-z0-9-]{1,60}", run_id):
        raise SystemExit("invalid F03a executor run")
    if "--worker" in sys.argv:
        guard = load(RUNS / "f04a-executor-run.v1.py", "f03a_existing_guard")
        raise SystemExit(guard.worker("resolver"))
    if mode in {"pure", "year"}:
        filename = "test_unmarked_version_candidates.py" if mode == "pure" else "test_year_only_supersession.py"
        command = [sys.executable, "-I", "-B", str(ROOT / "tests" / filename)]
    else:
        command = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), mode, run_id, "--worker"]
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f03a_supervisor")
    result = supervisor.run_bounded(command, RUNS / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)
