"""Root F05b app/controls with explicit fixture counters and original guards."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import re
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
    assert mode in {"app", "controls"}
    assert re.fullmatch(r"f05b-root-[a-z0-9-]+", run_id)
    if "--worker" in sys.argv:
        entry = "f05b-root-run.v1.py" if mode == "app" else "f05b-root-controls-run.v1.py"
        budget = load(RUNS / "f05b-fixture-budget.v1.py", "f05b_explicit_budget")
        with budget.enforce():
            return load(RUNS / entry, "f05b_original_worker").worker()
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f05b_supervisor_v2")
    result = supervisor.run_bounded([sys.executable, "-I", "-B", str(Path(__file__).resolve()), mode, run_id, "--worker"],
                                    RUNS / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1

if __name__ == "__main__":
    raise SystemExit(main())
