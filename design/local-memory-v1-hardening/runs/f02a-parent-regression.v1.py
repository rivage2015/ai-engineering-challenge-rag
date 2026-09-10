"""Parent collateral regressions using the reviewed F18/F04a guarded workers.

Preflight: four small synthetic suites only. Imports, worker subprocess/model
and network boundaries are guarded by the frozen F18 v2 dispatcher and F04a
guard. No user data, app GUI, actual model, package build or production config.
The bounded supervisor owns one worker and a unique run directory. Not an OS
sandbox or evidence of whole-product RSS/disk limits.
"""
import importlib.util
import json
from pathlib import Path
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
    if mode not in {"focused", "lineage", "migration", "security"} or not run_id.startswith("f02a-parent-"):
        raise SystemExit("invalid parent regression scope")
    if "--worker" in sys.argv:
        return load(RUNS / "f18-test-run.v2.py", "f02a_parent_dispatcher").worker(mode)
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f02a_parent_supervisor")
    result = supervisor.run_bounded(
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), mode, run_id, "--worker"],
        RUNS / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
