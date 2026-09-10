"""Unique bounded terminal-dot diagnostic run with repository scratch."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from run_local_memory_hardening_tests import run_bounded

scratch = ROOT / "artifacts/local-memory-v1-hardening/runs/f10-audit-dot-scratch-v1"
scratch.mkdir(mode=0o700, exist_ok=True)
os.environ["TMPDIR"] = str(scratch)
result = run_bounded(
    [str(ROOT / "rag/.venv/bin/python"), "-B", str(Path(__file__).with_name("f10-audit-dot-child.v1.py"))],
    ROOT / "artifacts/local-memory-v1-hardening/runs/f10-audit-dot-v1",
    cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
)
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(0 if result["status"] == "passed" else 1)
