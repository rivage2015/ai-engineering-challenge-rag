"""F03a root preflighted synthetic suites, bounded and IO guarded."""
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
    if mode not in {"app", "e2e", "resolver", "focused", "lineage", "migration", "security"} or not re.fullmatch(r"f03a-root-[a-z0-9-]+", run_id):
        raise SystemExit("invalid root F03a run")
    if "--worker" in sys.argv:
        if mode != "app":
            return load(RUNS / "f18-test-run.v2.py", "f03a_root_dispatcher").worker(mode)
        guard = load(RUNS / "f04a-executor-run.v1.py", "f03a_root_guard")
        original = guard.load

        def redirected(path, name):
            if path.name == "test_document_version_resolver.py":
                return original(ROOT / "tests/test_unmarked_version_e2e.py", "f03a_root_app_tests")
            return original(path, name)

        guard.load = redirected
        return guard.worker("resolver")
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f03a_root_supervisor")
    result = supervisor.run_bounded(
        [sys.executable, "-I", "-B", "-u", str(Path(__file__).resolve()), mode, run_id, "--worker"],
        RUNS / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
