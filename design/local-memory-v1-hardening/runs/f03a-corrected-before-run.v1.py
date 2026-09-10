#!/usr/bin/env python3
"""Re-run corrected F03a gold against immutable BEFORE resolver, in memory only.

No product rollback. Read source snapshots before the test installs its IO
guard, redirect only the test's exact resolver read, and execute its unchanged
CLI suite. The resulting metadata reports the actual before-source SHA-256.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"
TEST = ROOT / "tests/test_unmarked_version_candidates.py"
PRODUCT = ROOT / "distribution/macos-local-memory/engine/document_version_resolver.py"
BEFORE = RUNS / "f03a-before-resolver.v1.py"


if __name__ == "__main__":
    if "--worker" in sys.argv:
        before_bytes, test_bytes = BEFORE.read_bytes(), TEST.read_bytes()
        assert len(before_bytes) + len(test_bytes) <= 1048576
        assert hashlib.sha256(before_bytes).hexdigest() == "9a7443862c015ed768007ef5a7b21bd3bc197c9bb7a3d7ed2f7b39b33374359d"
        assert hashlib.sha256(test_bytes).hexdigest() == "aa7da7f0a308dec79a398678c18d41d246c26a514d6fc88dedcf1cba431100d3"
        original_read = Path.read_bytes

        def redirected_read(path):
            return before_bytes if path == PRODUCT else original_read(path)

        Path.read_bytes = redirected_read
        try:
            exec(compile(test_bytes, str(TEST), "exec"), {"__name__": "__main__", "__file__": str(TEST)})
        finally:
            Path.read_bytes = original_read
    else:
        spec = importlib.util.spec_from_file_location("f03a_corrected_red_supervisor", ROOT / "scripts/run_local_memory_hardening_tests.py")
        assert spec is not None and spec.loader is not None
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        result = runner.run_bounded(
            [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--worker"],
            RUNS / "f03a-executor-corrected-red-pure-001", cwd=ROOT,
            timeout_seconds=30, max_log_bytes=1048576,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result["status"] == "passed" else 1)
