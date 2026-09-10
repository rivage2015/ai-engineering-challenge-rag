"""Run the fixed investigation using a preflighted guarded worker."""
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
    if run_id not in {"f11a-binding-observation-001", "f11a-binding-observation-002"}:
        raise SystemExit("invalid investigation run id")
    if "--worker" in sys.argv:
        guard = load(HERE / "f04a-executor-run.v1.py", "f11a_binding_guard")
        original = guard.load

        def redirected(path, name):
            if path.name == "test_document_version_resolver.py":
                return original(HERE / "f11a-binding-observe.v1.py", "f11a_binding_observer")
            return original(path, name)

        guard.load = redirected
        return guard.worker("resolver")
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f11a_binding_supervisor")
    result = supervisor.run_bounded(
        [sys.executable, "-I", "-B", "-u", str(Path(__file__).resolve()), run_id, "--worker"],
        HERE / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
