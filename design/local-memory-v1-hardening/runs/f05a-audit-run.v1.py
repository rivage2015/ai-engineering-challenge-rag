"""F05a independent audit capture and exclusive bounded test execution.

Reviewed F04a/F18 guards and F05a root dispatch keep workers synthetic. One
worker, 30 seconds, 1 MiB logs per invocation; no OS-isolation claim.
"""
from __future__ import annotations
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import types

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"
ARTIFACT = RUNS / "f05a-graph-artifact.v1.json"
ARTIFACT_HASH = "7b7414f3e58a62b6eb34fc71534da11eec51c1dd58c316326ba03e9ab436bf61"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture(label):
    assert label in {"before", "after"}
    assert digest(ARTIFACT) == ARTIFACT_HASH
    artifact = json.loads(ARTIFACT.read_text())
    sources = {item["id"]: item for item in artifact["sources"]}
    assert len(sources) == len(artifact["sources"]) == 115
    hashes = {}
    for key, item in sources.items():
        path = Path(item["path"])
        assert path.is_absolute() and path.is_relative_to(ROOT)
        hashes[key] = digest(path)
        assert hashes[key] == item["sha256"], key
        assert path.stat().st_size == item["bytes"], key
    nodes, edges = [{item["id"] for item in artifact[k]} for k in ("nodes", "edges")]
    assert len(nodes) == 5 and len(edges) == 4
    for item in [*artifact["nodes"], *artifact["edges"]]:
        assert item["basis"] and set(item["basis"]) <= set(sources)
    for edge in artifact["edges"]:
        assert edge["source"] in nodes and edge["target"] in nodes
        assert edge["relation"].strip()
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f05a_audit_capture_supervisor")
    historical = []
    for requirement in artifact["required_runs"]:
        result_path = Path(sources[requirement["source_id"]]["path"])
        result = json.loads(result_path.read_text())
        raw = Path(result["log_path"]).read_bytes()
        count, outcome, details = supervisor.terminal_summary(raw.decode("utf-8"))
        assert hashlib.sha256(raw).hexdigest() == result["log_sha256"]
        assert len(raw) == result["log_bytes"] <= 1048576
        assert count == requirement["methods"] == result["tests_reported"]
        assert result["status"] == requirement["status"]
        assert result["timeout_seconds"] == 30 and result["max_log_bytes"] == 1048576
        assert not details.get("skipped", 0) and not details.get("expected failures", 0)
        assert (outcome == "OK") == (result["status"] == "passed")
        assert (result["exit_code"] == 0) == (result["status"] == "passed")
        historical.append({**requirement, "outcome": outcome, "details": details, "log_sha256": result["log_sha256"]})
    value = {"task_id": artifact["task_id"], "artifact_sha256": ARTIFACT_HASH,
             "label": label, "sources": hashes, "historical_runs": historical}
    target = RUNS / f"f05a-audit-sources-{label}.v1.json"
    with target.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"path": str(target), "sha256": digest(target), "sources": len(hashes), "historical_runs": historical}, ensure_ascii=False, indent=2))


def worker(mode):
    if mode in {"pure", "holdout"}:
        guard = load(RUNS / "f04a-executor-run.v1.py", "f05a_audit_guard")
        original = guard.load
        target = ROOT / "tests/test_version_graph_reconstruction.py" if mode == "pure" else RUNS / "f05a-audit-holdouts.v1.py"

        class EmptyHarness:
            def setUp(self):
                pass

            def doCleanups(self):
                pass

        def redirect(path, name):
            if path.name == "test_versioned_safe_index_e2e.py":
                return types.SimpleNamespace(VersionedSafeIndexE2E=EmptyHarness)
            if path.name == "test_document_version_resolver.py":
                module = original(target, "f05a_audit_target")
                module.builder = types.SimpleNamespace()
                return module
            return original(path, name)

        guard.load = redirect
        return guard.worker("resolver")
    return load(RUNS / "f05a-root-run.v2.py", "f05a_audit_root_dispatch").worker(mode)


def main():
    mode = sys.argv[1]
    if mode in {"before", "after"}:
        capture(mode)
        return 0
    assert mode in {"pure", "holdout", "resolver", "app", "e2e", "unmarked", "unmarked-pure", "year"}
    run_id = sys.argv[2]
    assert re.fullmatch(r"f05a-audit-[a-z0-9-]+", run_id)
    if "--worker" in sys.argv:
        return worker(mode)
    assert digest(ARTIFACT) == ARTIFACT_HASH
    if mode in {"unmarked-pure", "year"}:
        filename = "test_unmarked_version_candidates.py" if mode == "unmarked-pure" else "test_year_only_supersession.py"
        command = [sys.executable, "-I", "-B", "-u", str(ROOT / "tests" / filename)]
    else:
        command = [sys.executable, "-I", "-B", "-u", str(Path(__file__).resolve()), mode, run_id, "--worker"]
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f05a_audit_supervisor")
    result = supervisor.run_bounded(command, RUNS / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
