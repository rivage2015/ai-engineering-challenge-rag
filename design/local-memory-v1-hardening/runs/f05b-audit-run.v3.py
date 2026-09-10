"""Independent F05b guarded runs and before/after frozen-source capture.

One worker per call, 30 seconds, 1 MiB logs, exclusive audit run IDs. Requires
root review before execution. No product writes outside synthetic guarded tmp.
"""
from __future__ import annotations
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"
ARTIFACT = RUNS / "f05b-graph-artifact.v1.json"
ARTIFACT_HASH = "721db8b316bd22101ddcc9738d4006684eb50d9270af4a05e64c6b58af73b987"
HOLDOUT = RUNS / "f05b-audit-holdouts.v1.py"
HOLDOUT_HASH = "15554e5da8649cd71860b33197519dd815141accb97ba46f35b535e900477603"
SUPPLEMENT = RUNS / "f05b-audit-supplement.v1.py"
SUPPLEMENT_HASH = "f8bc30cf0d813aadf63f13f94ad8eed09c0b3816f7656ca59986792985a21ac3"
DIRECT = {"pure": ROOT / "tests/test_decision_snapshot_attestation.py",
          "app": ROOT / "tests/test_decision_snapshot_e2e.py",
          "controls": ROOT / "tests/test_decision_snapshot_controls.py",
          "holdout": HOLDOUT,
          "supplement": SUPPLEMENT,
          "path": RUNS / "f05b-executor-path-boundary-probe.v1.py"}
COLLATERAL = {"f05a-pure", "unmarked", "unmarked-pure", "year", "focused", "lineage", "security", "migration", "e2e", "resolver", "gate", "runtime"}


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes():
    assert digest(ARTIFACT) == ARTIFACT_HASH
    artifact = json.loads(ARTIFACT.read_bytes())
    assert artifact["task_id"] == "local-memory-v1-f05b-snapshot-complete-selection"
    assert artifact["audit_repair_round"] == 0
    values = {}
    for source in artifact["sources"]:
        path = Path(source["path"])
        assert path.is_absolute() and path.is_relative_to(ROOT)
        assert source["id"] not in values
        values[source["id"]] = digest(path)
        assert values[source["id"]] == source["sha256"], source["id"]
        assert path.stat().st_size == source["bytes"], source["id"]
    assert len(values) == 210
    assert digest(HOLDOUT) == HOLDOUT_HASH
    assert digest(SUPPLEMENT) == SUPPLEMENT_HASH
    return values


def worker(mode, case):
    if mode in COLLATERAL:
        if mode in {"gate", "runtime"}:
            return load(RUNS / "f05b-executor-run.v2.py", "f05b_audit_compat").worker(mode)
        return load(RUNS / "f05b-root-collateral-run.v1.py", "f05b_audit_collateral").worker(mode)
    guard = load(RUNS / "f04a-executor-run.v1.py", "f05b_audit_guard")
    original_load = guard.load
    target = None

    class EmptyHarness:
        def setUp(self):
            pass
        def doCleanups(self):
            pass

    def redirected(path, name):
        nonlocal target
        if mode == "pure" and path.name == "test_versioned_safe_index_e2e.py":
            return types.SimpleNamespace(VersionedSafeIndexE2E=EmptyHarness)
        if path.name == "test_document_version_resolver.py":
            target = original_load(DIRECT[mode], "f05b_audit_target")
            target.builder = types.SimpleNamespace()
            if mode == "holdout":
                target.GUARDED_RUN_APPROVED = True
            if mode == "supplement":
                target.APPROVED = True
            return target
        return original_load(path, name)

    original_suite = unittest.defaultTestLoader.loadTestsFromModule
    def selected(module, *args, **kwargs):
        if module is target and case is not None:
            return unittest.defaultTestLoader.loadTestsFromName(case, module)
        return original_suite(module, *args, **kwargs)
    guard.load = redirected
    with mock.patch.object(unittest.defaultTestLoader, "loadTestsFromModule", selected):
        return guard.worker("resolver")


def main():
    mode = sys.argv[1]
    if mode in {"before", "after"}:
        value = {"task_id": "local-memory-v1-f05b-snapshot-complete-selection",
                 "artifact_sha256": ARTIFACT_HASH, "label": mode, "sources": source_hashes()}
        # Artifact bytes are returned for apply_patch, never written here.
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    assert mode in set(DIRECT) | COLLATERAL
    run_id = sys.argv[2]
    assert re.fullmatch(r"f05b-audit-[a-z0-9-]{1,65}", run_id)
    case = None
    if "--case" in sys.argv:
        case = sys.argv[sys.argv.index("--case") + 1]
        assert mode == "holdout" and re.fullmatch(r"IndependentSnapshot(?:Application|Consumer)Holdouts\.test_[a-z0-9_]+", case)
    if "--worker" in sys.argv:
        budget = load(RUNS / "f05b-fixture-budget.v1.py", "f05b_audit_budget")
        with budget.enforce():
            return worker(mode, case)
    source_hashes()
    if mode in {"unmarked-pure", "year"}:
        filename = "test_unmarked_version_candidates.py" if mode == "unmarked-pure" else "test_year_only_supersession.py"
        command = [sys.executable, "-I", "-B", "-u", str(ROOT / "tests" / filename)]
    else:
        command = [sys.executable, "-I", "-B", "-u", str(Path(__file__).resolve()), mode, run_id, "--worker"]
        if case is not None:
            command.extend(["--case", case])
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f05b_audit_supervisor")
    result = supervisor.run_bounded(command, RUNS / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
