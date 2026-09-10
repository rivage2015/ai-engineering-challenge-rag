#!/usr/bin/env python3
"""Reviewed H0 supervisor for read-only F03 baseline observations."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "f03a_next_supervisor", ROOT / "scripts/run_local_memory_hardening_tests.py",
)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


if __name__ == "__main__":
    result = runner.run_bounded(
        [sys.executable, "-I", "-B", str(ROOT / "design/local-memory-v1-hardening/runs/f03a-next-probe.v1.py")],
        ROOT / "design/local-memory-v1-hardening/runs/f03a-next-baseline-001",
        cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)
