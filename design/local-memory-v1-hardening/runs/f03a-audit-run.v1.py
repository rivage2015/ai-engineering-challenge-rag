"""Independent F03a audit: frozen-source checks and guarded synthetic suites.

Only this supervisor launches one reviewed Python worker. New audit records are
exclusive; workers reuse the inspected F04a/F18 guards and in-process app CLI.
This is a procedural audit harness, not OS isolation or release acceptance.
"""
from __future__ import annotations
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"
ARTIFACT = RUNS / "f03a-graph-artifact.v1.json"
ARTIFACT_HASH = "b5d7d0f487be6efafb6a1dca7f024ba303ed52ba791ed6cae9a9dc6e877dbd14"


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
    sources = artifact["sources"]
    source_ids = {item["id"] for item in sources}
    assert len(source_ids) == len(sources) == 97
    hashes = {}
    for item in sources:
        path = Path(item["path"])
        assert path.is_absolute() and path.is_relative_to(ROOT)
        hashes[item["id"]] = digest(path)
        assert hashes[item["id"]] == item["sha256"], item["id"]
    nodes = {item["id"] for item in artifact["nodes"]}
    edges = {item["id"] for item in artifact["edges"]}
    assert len(nodes) == 5 and len(edges) == 4
    for item in [*artifact["nodes"], *artifact["edges"]]:
        assert item["basis"] and set(item["basis"]) <= source_ids
    for edge in artifact["edges"]:
        assert edge["source"] in nodes and edge["target"] in nodes
        assert edge["relation"].strip()
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "audit_capture_supervisor")
    source_map = {item["id"]: item for item in sources}
    historical = []
    for requirement in artifact["required_runs"]:
        result_path = Path(source_map[requirement["source_id"]]["path"])
        result = json.loads(result_path.read_text())
        log_path = Path(result["log_path"])
        content = log_path.read_bytes()
        count, outcome, details = supervisor.terminal_summary(content.decode("utf-8"))
        assert result["log_sha256"] == hashlib.sha256(content).hexdigest()
        assert result["log_bytes"] == len(content) <= 1048576
        assert count == requirement["methods"] == result["tests_reported"]
        assert requirement["status"] == result["status"]
        assert result["timeout_seconds"] == 30
        assert not details.get("skipped", 0) and not details.get("expected failures", 0)
        assert (outcome == "OK") == (result["status"] == "passed")
        historical.append({**requirement, "outcome": outcome, "details": details,
                           "command": result["command"], "log_sha256": result["log_sha256"]})
    value = {"task_id": artifact["task_id"], "artifact_sha256": ARTIFACT_HASH,
             "label": label, "sources": hashes, "historical_runs": historical,
             "node_ids": sorted(nodes), "edge_ids": sorted(edges)}
    target = RUNS / f"f03a-audit-sources-{label}.v1.json"
    with target.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"path": str(target), "sha256": digest(target),
                      "source_count": len(hashes), "historical_runs": historical}, ensure_ascii=False, indent=2))


def worker(mode):
    if mode in {"app", "holdout"}:
        guard = load(RUNS / "f04a-executor-run.v1.py", "audit_application_guard")
        original = guard.load
        target = ROOT / "tests/test_unmarked_version_e2e.py" if mode == "app" else RUNS / "f03a-audit-holdouts.v1.py"

        def redirect(path, name):
            return original(target, "audit_target_tests") if path.name == "test_document_version_resolver.py" else original(path, name)

        guard.load = redirect
        return guard.worker("resolver")
    return load(RUNS / "f18-test-run.v2.py", "audit_existing_dispatcher").worker(mode)


def main():
    mode = sys.argv[1]
    if mode in {"before", "after"}:
        capture(mode)
        return 0
    assert mode in {"pure", "year", "resolver", "app", "e2e", "focused", "migration", "holdout"}
    run_id = sys.argv[2]
    assert re.fullmatch(r"f03a-audit-[a-z0-9-]+", run_id)
    if "--worker" in sys.argv:
        return worker(mode)
    assert digest(ARTIFACT) == ARTIFACT_HASH
    if mode in {"pure", "year"}:
        filename = "test_unmarked_version_candidates.py" if mode == "pure" else "test_year_only_supersession.py"
        command = [sys.executable, "-I", "-B", "-u", str(ROOT / "tests" / filename)]
    else:
        command = [sys.executable, "-I", "-B", "-u", str(Path(__file__).resolve()), mode, run_id, "--worker"]
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "audit_supervisor")
    result = supervisor.run_bounded(command, RUNS / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
