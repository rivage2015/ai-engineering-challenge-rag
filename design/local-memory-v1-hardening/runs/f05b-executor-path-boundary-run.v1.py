"""Two fixed preaudit static-path methods under inherited synthetic guards."""
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

if __name__ == "__main__":
    run_id = sys.argv[1]
    assert re.fullmatch(r"f05b-executor-[a-z0-9-]+", run_id)
    if "--worker" in sys.argv:
        guard = load(RUNS / "f04a-executor-run.v1.py", "f05b_path_guard")
        original = guard.load
        guard.load = lambda path, name: original(RUNS / "f05b-executor-path-boundary-probe.v1.py", "f05b_path_tests") if path.name == "test_document_version_resolver.py" else original(path, name)
        raise SystemExit(guard.worker("resolver"))
    runner = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f05b_path_supervisor")
    result = runner.run_bounded([sys.executable, "-I", "-B", str(Path(__file__).resolve()), run_id, "--worker"],
                                RUNS / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)
