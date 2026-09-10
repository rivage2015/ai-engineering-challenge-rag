"""Re-audit repair1 with unchanged child guards and repository-only scratch."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from run_local_memory_hardening_tests import run_bounded

mode = sys.argv[1]
assert mode in {"focused", "regression", "legacy", "adversarial"}
scratch = ROOT / "artifacts/local-memory-v1-hardening/runs/f10-audit-scratch-v2"
scratch.mkdir(mode=0o700, exist_ok=True)
os.environ["TMPDIR"] = str(scratch)
result = run_bounded(
    [str(ROOT / "rag/.venv/bin/python"), "-B", str(Path(__file__).with_name("f10-audit-child.v1.py")), mode],
    ROOT / "artifacts/local-memory-v1-hardening/runs" / ("f10-audit-" + mode + "-v2"),
    cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
)
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(0 if result["status"] == "passed" else 1)
