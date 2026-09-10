"""Fixed two-method actual reader regression through existing F04 guard."""
import importlib.util
import json
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def worker():
    guard = load(HERE / "f04a-executor-run.v1.py", "password_propagation_guard")
    budget = load(HERE / "f05b-fixture-budget.v1.py", "password_propagation_budget")
    target = ROOT / "tests/test_password_failure_propagation.py"
    budget.AUTHORS = {str(target), str(ROOT / "distribution/macos-local-memory/tests/test_versioned_safe_index_e2e.py")}
    original = guard.load

    def redirect(path, name):
        if path.name == "test_document_version_resolver.py":
            return original(target, "password_propagation_tests")
        return original(path, name)

    guard.load = redirect
    with budget.enforce():
        return guard.worker("resolver")


def main():
    run_id = sys.argv[1]
    if not re.fullmatch(r"password-propagation-[0-9]{3}", run_id):
        raise SystemExit("invalid run ID")
    if sys.argv[2:] == ["--worker"]:
        return worker()
    if sys.argv[2:]:
        raise SystemExit("invalid options")
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "password_propagation_supervisor")
    result = supervisor.run_bounded(
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), run_id, "--worker"],
        HERE / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
