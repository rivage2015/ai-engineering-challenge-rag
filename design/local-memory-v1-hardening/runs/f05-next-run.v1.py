"""One reviewed, bounded F05 gap observation run with the inherited IO guard."""
import importlib.util
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    if "--worker" in sys.argv:
        guard = load(HERE / "f04a-executor-run.v1.py", "f05_guard")
        original = guard.load

        def redirected(path, name):
            if path.name == "test_document_version_resolver.py":
                return original(HERE / "f05-next-observe.v1.py", "f05_observer")
            return original(path, name)

        guard.load = redirected
        raise SystemExit(guard.worker("resolver"))
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f05_supervisor")
    result = supervisor.run_bounded(
        [sys.executable, "-I", "-B", "-u", str(Path(__file__).resolve()), "--worker"],
        HERE / "f05-next-observation-001", cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)
