"""Fixed four-method F11a semantic RED/GREEN under existing reviewed guard."""
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


def main():
    run_id = sys.argv[1]
    if run_id not in {"f11a-root-red-001", "f11a-root-green-001"}:
        raise SystemExit("invalid fixed run id")
    if "--worker" in sys.argv:
        guard = load(HERE / "f04a-executor-run.v1.py", "f11a_root_guard")
        original = guard.load

        def redirect(path, name):
            if path.name == "test_document_version_resolver.py":
                return original(ROOT / "tests/test_notebook_metadata_boundaries.py", "f11a_root_boundaries")
            return original(path, name)

        guard.load = redirect
        return guard.worker("resolver")
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f11a_root_supervisor")
    result = supervisor.run_bounded(
        [sys.executable, "-I", "-B", "-u", str(Path(__file__).resolve()), run_id, "--worker"],
        HERE / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
