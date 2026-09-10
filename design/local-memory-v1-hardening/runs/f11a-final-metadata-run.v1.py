"""Fresh-ID entry to unchanged reviewed9/21/3 workers, after source freeze."""
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


def main():
    mode, run_id = sys.argv[1:3]
    if mode not in {"initial", "post", "residual"} or not re.fullmatch(
        "f11a-final-" + mode + r"-[0-9]{3}", run_id
    ):
        raise SystemExit("invalid fixed mode/run ID")
    if "--worker" in sys.argv:
        version = "v1" if mode == "initial" else "v2"
        worker = load(HERE / ("f11a-executor-run." + version + ".py"), "f11a_final_worker")
        return worker.worker() if mode == "initial" else worker.worker(mode)
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f11a_final_supervisor")
    result = supervisor.run_bounded(
        [sys.executable, "-I", "-B", "-u", str(Path(__file__).resolve()), mode, run_id, "--worker"],
        HERE / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
