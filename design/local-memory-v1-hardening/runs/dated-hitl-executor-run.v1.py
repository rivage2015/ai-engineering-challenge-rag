"""Fixed pure dated-HITL batches; 30 seconds / 1 MiB durable output.

Stdlib resolver/test imports happen before an audit hook forbids runtime file,
directory, network and process IO. This is not an OS sandbox or RSS guarantee.
No actual source documents, app, config, shared decisions or models are used.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import signal
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
RUNS = Path(__file__).resolve().parent
PROFILES = {
    "initial": ("tests/test_dated_document_human_gate.py",
                "ee62aaf42632bca032c80a40a54893a817cd78f72c5d95e1c1078ba96a69586a",
                "DatedDocumentHumanGateTests", 5),
    "temporal": ("tests/test_dated_temporal_candidates.py",
                 "6a9a877771117441b33cdb94f5bc4f459b067bf682c538dbade00593189c7154",
                 "DatedTemporalCandidateTests", 16),
    "legacy": ("tests/test_dated_temporal_candidates.py",
               "6a9a877771117441b33cdb94f5bc4f459b067bf682c538dbade00593189c7154",
               "DatedLegacyDecisionResidualTests", 1),
    "year": ("tests/test_year_only_supersession.py",
             "70a1226cb30daa6692533d2c6f1fe270fe301a8f759c0db56a142acbc945b8c0",
             "YearOnlySupersessionTests", 10),
}


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class PureResult(unittest.TextTestResult):
    def _exc_info_to_string(self, error, test):
        return error[0].__name__ + ": " + str(error[1]) + "\n"


def worker(mode):
    relative, expected, class_name, count = PROFILES[mode]
    path = ROOT / relative
    raw = path.read_bytes()
    if len(raw) > 1048576 or hashlib.sha256(raw).hexdigest() != expected:
        raise AssertionError("fixed test source drift/size")
    resolver = ROOT / "distribution/macos-local-memory/engine/document_version_resolver.py"
    resolver_raw = resolver.read_bytes()
    if len(resolver_raw) > 1048576:
        raise AssertionError("resolver source size")
    target = load(path, "dated_hitl_" + mode)
    cls = getattr(target, class_name)
    methods = unittest.defaultTestLoader.getTestCaseNames(cls)
    if len(methods) != count:
        raise AssertionError("fixed method count changed")
    suite = unittest.TestSuite(cls(method) for method in methods)
    print(json.dumps({"mode": mode, "test_sha256": expected,
                      "resolver_sha256": hashlib.sha256(resolver_raw).hexdigest(),
                      "methods": methods, "original_document_reads": False,
                      "residual_not_acceptance": mode == "legacy"}), flush=True)

    def deny(event, args):
        if event == "open" or event.startswith(("socket.", "subprocess.", "ctypes.")):
            raise AssertionError("pure runtime IO forbidden:" + event)
        if event in {"os.system", "os.fork", "os.forkpty", "os.posix_spawn", "os.exec",
                     "os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.symlink",
                     "os.link", "os.truncate", "os.listdir", "os.scandir"}:
            raise AssertionError("pure runtime filesystem/process forbidden:" + event)

    def deadline(signum, frame):
        raise TimeoutError("pure 30-second deadline")

    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(30)
    sys.addaudithook(deny)
    result = unittest.TextTestRunner(verbosity=2, resultclass=PureResult).run(suite)
    signal.alarm(0)
    return 0 if result.wasSuccessful() else 1


def main():
    mode, run_id = sys.argv[1:3]
    if mode not in PROFILES or not re.fullmatch(r"dated-hitl-executor-[a-z0-9-]+-[0-9]{3}", run_id):
        raise SystemExit("invalid fixed batch")
    if len(sys.argv) == 4 and sys.argv[3] == "--worker":
        return worker(mode)
    if len(sys.argv) != 3:
        raise SystemExit("mode and fresh ID required")
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "dated_hitl_supervisor")
    result = supervisor.run_bounded(
        [sys.executable, "-I", "-B", "-u", str(Path(__file__).resolve()), mode, run_id, "--worker"],
        RUNS / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
