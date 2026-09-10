#!/usr/bin/env python3
"""Use reviewed H0 supervisor for one pure-resolver F02a baseline RED run."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
RUNNER_PATH = ROOT / "scripts/run_local_memory_hardening_tests.py"
SPEC = importlib.util.spec_from_file_location("f02a_bounded_supervisor", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


if __name__ == "__main__":
    result = runner.run_bounded(
        [sys.executable, "-I", "-B", str(ROOT / "tests/test_year_only_supersession.py")],
        ROOT / "design/local-memory-v1-hardening/runs/f02a-red-001",
        cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # Actual failure stays nonzero: defect reproduction is not acceptance.
    raise SystemExit(0 if result["status"] == "passed" else 1)
