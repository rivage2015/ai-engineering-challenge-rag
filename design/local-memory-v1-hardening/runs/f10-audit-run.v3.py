"""Final repair2 scoped replays: immutable original guard/test scripts."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from run_local_memory_hardening_tests import run_bounded

mode = sys.argv[1]
assert mode in {"focused", "regression", "legacy", "adversarial", "dot"}
scratch = ROOT / "artifacts/local-memory-v1-hardening/runs/f10-audit-scratch-v3"
scratch.mkdir(mode=0o700, exist_ok=True)
os.environ["TMPDIR"] = str(scratch)
child = "f10-audit-dot-child.v1.py" if mode == "dot" else "f10-audit-child.v1.py"
command = [str(ROOT / "rag/.venv/bin/python"), "-B", str(Path(__file__).with_name(child))]
if mode != "dot":
    command.append(mode)
result = run_bounded(
    command, ROOT / "artifacts/local-memory-v1-hardening/runs" / ("f10-audit-" + mode + "-v3"),
    cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
)
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(0 if result["status"] == "passed" else 1)
