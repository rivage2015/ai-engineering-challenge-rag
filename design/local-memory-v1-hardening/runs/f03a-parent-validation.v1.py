"""Read-only formal audit/source/delta/log checks; JSON to stdout only.

This validates evidence consistency, not the semantic truth of an audit.
Artifact and report digests must be supplied from the independent handoff.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import runpy
import sys

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"
TASK = "lms-v1-00-hardening-2026-09-09-f03a"
CONTRACT_HASH = "1a5244d9fd4c537aeaaec88f44d358e24f1b1d56441beaf4ec60fe2a34e07924"
SCHEMA = Path("/Users/takashifukutomi/.codex/skills/graph-engineering-agentic-audit/assets/agentic-audit-report.schema.json")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            assert key not in result, (str(path), key)
            result[key] = value
        return result
    return json.loads(path.read_bytes(), object_pairs_hook=unique)


def main():
    artifact_name, artifact_hash, report_name, report_hash, evidence_hash = sys.argv[1:6]
    assert re.fullmatch(r"f03a-graph-artifact\.v[1-3]\.json", artifact_name)
    assert re.fullmatch(r"f03a-audit-report\.v[1-3]\.json", report_name)
    artifact_path, report_path = RUNS / artifact_name, RUNS / report_name
    assert digest(artifact_path) == artifact_hash
    assert digest(report_path) == report_hash
    artifact, report = read_json(artifact_path), read_json(report_path)
    schema = read_json(SCHEMA)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(report)
    assert artifact["task_id"] == report["task_id"] == TASK
    assert report["artifact_hash"] == artifact_hash
    assert artifact["audit_repair_round"] in (0, 1, 2)
    assert digest(RUNS / "f03a-task-contract.v1.md") == CONTRACT_HASH
    assert report["auditor_context"]["independence_level"] == "same_model_separate_context"
    namespaces = {}
    for key in ("nodes", "edges", "sources"):
        namespaces[key] = {item["id"]: item for item in artifact[key]}
        assert len(namespaces[key]) == len(artifact[key]) > 0
    nodes, edges, sources = (namespaces[key] for key in ("nodes", "edges", "sources"))
    assert len(set(nodes) | set(edges) | set(sources)) == len(nodes) + len(edges) + len(sources)
    for item in [*nodes.values(), *edges.values()]:
        assert item["basis"] and set(item["basis"]) <= set(sources)
    for edge in edges.values():
        assert edge["source"] in nodes and edge["target"] in nodes
        assert isinstance(edge["relation"], str) and edge["relation"].strip()
    for source in sources.values():
        path = Path(source["path"])
        assert path.is_absolute() and path.is_file()
        assert digest(path) == source["sha256"], source["id"]
    checks = report["checks"]
    assert checks and len({c["check_id"] for c in checks}) == len(checks)
    targets_seen = set()
    for check in checks:
        assert check["target_ids"] and set(check["target_ids"]) <= set(nodes) | set(edges)
        assert check["evidence_ids"] and set(check["evidence_ids"]) <= set(sources)
        targets_seen.update(check["target_ids"])
    assert set(nodes) | set(edges) <= targets_seen
    actual_blocking = sum(c["severity"] == "blocking" and c["verdict"] != "pass" for c in checks)
    actual_unresolved = sum(c["verdict"] == "unresolved" for c in checks)
    assert report["summary"]["blocking_failures"] == actual_blocking
    assert report["summary"]["unresolved_count"] == actual_unresolved
    assert report["summary"]["next_action"] == {"pass": "deliver", "revise": "revise", "blocked": "human_decision"}[report["status"]]
    if report["status"] == "pass":
        assert actual_blocking == actual_unresolved == 0
        assert all(c["verdict"] == "pass" and c["required_action"] is None for c in checks)

    integrity_path = RUNS / "f18-audit-integrity.v1.py"
    assert digest(integrity_path) == "ed13ff27e65d73a6152f6bc3000df0e68b91b168b650344500122466a7bd1dd6"
    reverse = runpy.run_path(str(integrity_path), run_name="f03a_inverse_helper")["reverse_unified"]
    delta_checks = []
    for current, before, patch in artifact["isolated_deltas"]:
        current_path, before_path, patch_path = (Path(sources[s]["path"]) for s in (current, before, patch))
        assert reverse(current_path.read_text(), patch_path.read_text()).encode() == before_path.read_bytes()
        delta_checks.append({"source": current, "before_sha256": digest(before_path), "after_sha256": digest(current_path), "inverse_matches": True})
    readme_delta = read_json(Path(sources["README_DELTA"]["path"]))
    assert readme_delta["task_id"] == TASK
    after_readme = Path(sources["README"]["path"]).read_text()
    assert digest(Path(sources["README"]["path"])) == readme_delta["after_sha256"]
    assert hashlib.sha256(reverse(after_readme, readme_delta["isolated_unified_diff"]).encode()).hexdigest() == readme_delta["before_sha256"]
    logs = []

    def check_run(path, expected_status, expected_methods):
        record = read_json(path)
        log_path = Path(record["log_path"])
        raw = log_path.read_bytes()
        assert digest(log_path) == record["log_sha256"]
        assert len(raw) == record["log_bytes"] <= 1048576
        assert record["status"] == expected_status and record["tests_reported"] == expected_methods > 0
        assert record["skipped_reported"] == record["expected_failures_reported"] == 0
        assert record["timeout_seconds"] == 30 and record["max_log_bytes"] == 1048576
        footer = re.search(rb"Ran (\d+) tests? in [0-9.]+s\s+(OK|FAILED(?: \([^\r\n]+\))?)\s*\Z", raw)
        assert footer and int(footer.group(1)) == expected_methods
        if expected_status == "passed":
            assert record["exit_code"] == 0 and footer.group(2) == b"OK"
        else:
            assert expected_status == "failed" and record["exit_code"] != 0
            assert footer.group(2).startswith(b"FAILED")
        logs.append({"path": str(path.relative_to(ROOT)), "result_sha256": digest(path), "log_sha256": digest(log_path), "methods": expected_methods, "status": expected_status})

    for run in artifact["required_runs"]:
        check_run(Path(sources[run["source_id"]]["path"]), run["status"], run["methods"])
    # Auditor provenance is independently authored and hashed by the report's
    # source_scope handoff. Read its explicit manifest only, not arbitrary paths.
    evidence_path = RUNS / report_name.replace("audit-report", "audit-evidence")
    assert digest(evidence_path) == evidence_hash
    evidence = read_json(evidence_path)
    assert evidence["task_id"] == TASK and evidence["artifact_sha256"] == artifact_hash
    assert evidence["sources_before"] == evidence["sources_after"] == {s["id"]: s["sha256"] for s in sources.values()}
    for item in evidence["additional_artifacts"]:
        path = Path(item["path"])
        assert path.is_absolute() and path.is_relative_to(RUNS)
        assert digest(path) == item["sha256"]
    for item in evidence["runs"]:
        path = Path(item["path"])
        assert path.is_absolute() and path.is_relative_to(RUNS)
        assert digest(path) == item["sha256"]
        check_run(path, item["status"], item["methods"])
    assert sum(r["methods"] for r in evidence["runs"]) == evidence["total_methods"]
    for path, expected in read_json(ROOT / "design/local-memory-v1-hardening/checkpoint.json")["protected_user_changes"].items():
        assert digest(ROOT / path) == expected
    print(json.dumps({
        "schema_version": "1.0", "task_id": TASK,
        "status": "pass_local_f03a" if report["status"] == "pass" else "valid_report_requires_" + report["status"],
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "artifact_sha256": artifact_hash, "report_sha256": report_hash,
        "audit_evidence_sha256": digest(evidence_path), "validator_sha256": digest(Path(__file__)),
        "audit_repair_round": artifact["audit_repair_round"],
        "source_count": len(sources), "node_count": len(nodes), "edge_count": len(edges),
        "strict_schema_and_all_references": "pass", "source_hashes_and_protected_user_hashes": "pass",
        "isolated_deltas": delta_checks, "readme_inverse": "pass", "logs": logs,
        "independent_methods": evidence["total_methods"],
        "scope": "F03a same-key unmarked candidate inclusion/mixed hold only. No whole F03/F05/HITL/freshness/product/model/package acceptance. Hash/schema consistency does not prove semantic truth.",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
