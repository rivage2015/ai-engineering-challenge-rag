"""Repeat unchanged synthetic observations with unbuffered child output.

v1 was exit 0 but no_tests: buffered stdout appeared after unittest's stderr
footer. Preserve that record. Only -u and the new owned output directory change.
Same preflight, 30-second wall bound and 1 MiB log bound apply.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from run_local_memory_hardening_tests import run_bounded

result = run_bounded(
    [sys.executable, "-u", "-B", str(HERE / "f11-next-observe.v1.py"), "-v"],
    HERE / "f11-next-observation-run.v2", cwd=ROOT,
    timeout_seconds=30, max_log_bytes=1024 * 1024,
)
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(0 if result["status"] == "passed" else 1)
