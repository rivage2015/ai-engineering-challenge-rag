"""Parent consistency checks for a frozen F05a audit; stdout only.

Strict schema, source/hash/reference/delta/log checks do not prove audit truth.
Exact artifact/report/evidence digests are supplied from the independent handoff.
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
TASK = "lms-v1-00-hardening-2026-09-09-f05a"
SCHEMA = Path("/Users/takashifukutomi/.codex/skills/graph-engineering-agentic-audit/assets/agentic-audit-report.schema.json")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            assert key not in result, (path, key)
            result[key] = value
        return result
    return json.loads(path.read_bytes(), object_pairs_hook=unique)


def main():
    artifact_name, artifact_hash, report_name, report_hash, evidence_hash = sys.argv[1:6]
    assert re.fullmatch(r"f05a-graph-artifact\.v[1-3]\.json", artifact_name)
    assert re.fullmatch(r"f05a-audit-report\.v[1-3]\.json", report_name)
    artifact_path, report_path = RUNS / artifact_name, RUNS / report_name
    evidence_path = RUNS / report_name.replace("audit-report", "audit-evidence")
    for path, expected in ((artifact_path, artifact_hash), (report_path, report_hash), (evidence_path, evidence_hash)):
        assert digest(path) == expected, path
    artifact, report, evidence = map(read_json, (artifact_path, report_path, evidence_path))
    schema = read_json(SCHEMA)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(report)
    assert artifact["task_id"] == report["task_id"] == evidence["task_id"] == TASK
    assert report["artifact_hash"] == evidence["artifact_sha256"] == artifact_hash
    assert digest(RUNS / "f05a-task-contract.v1.md") == "89960be4254752ba3ae387610359c3f6f8200cb57c3d7a9190398c723473e4df"
    assert digest(RUNS / "f05a-contract-addendum-001.v1.md") == "5cc85d11170e4068099ab31284807c5e10cfb4e804c37104379333fa688fb4bd"
    assert artifact["audit_repair_round"] in (0, 1, 2)
    assert report["auditor_context"]["independence_level"] == "same_model_separate_context"
    nodes, edges, sources = ({i["id"]: i for i in artifact[k]} for k in ("nodes", "edges", "sources"))
    for key, values in (("nodes", nodes), ("edges", edges), ("sources", sources)):
        assert len(values) == len(artifact[key]) > 0
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
        if "bytes" in source:
            assert path.stat().st_size == source["bytes"]
    checks = report["checks"]
    assert checks and len({c["check_id"] for c in checks}) == len(checks)
    covered = set()
    for check in checks:
        assert check["target_ids"] and set(check["target_ids"]) <= set(nodes) | set(edges)
        assert check["evidence_ids"] and set(check["evidence_ids"]) <= set(sources)
        covered.update(check["target_ids"])
    assert set(nodes) | set(edges) <= covered
    blocking = sum(c["severity"] == "blocking" and c["verdict"] != "pass" for c in checks)
    unresolved = sum(c["verdict"] == "unresolved" for c in checks)
    assert report["summary"]["blocking_failures"] == blocking
    assert report["summary"]["unresolved_count"] == unresolved
    assert report["summary"]["next_action"] == {"pass": "deliver", "revise": "revise", "blocked": "human_decision"}[report["status"]]
    if report["status"] == "pass":
        assert blocking == unresolved == 0
        assert all(c["verdict"] == "pass" and c["required_action"] is None for c in checks)
    helper = RUNS / "f18-audit-integrity.v1.py"
    assert digest(helper) == "ed13ff27e65d73a6152f6bc3000df0e68b91b168b650344500122466a7bd1dd6"
    reverse = runpy.run_path(str(helper), run_name="f05a_inverse_helper")["reverse_unified"]
    inverse_results = []
    for current_id, before_id, patch_id in artifact["isolated_deltas"]:
        current, before, patch = (Path(sources[k]["path"]) for k in (current_id, before_id, patch_id))
        assert reverse(current.read_text(), patch.read_text()).encode() == before.read_bytes()
        inverse_results.append({"source": current_id, "before_sha256": digest(before), "after_sha256": digest(current)})
    root_deltas = read_json(Path(sources["ROOT_DELTAS"]["path"]))
    assert root_deltas["task_id"] == TASK
    for item in root_deltas["deltas"]:
        path = Path(item["path"])
        assert digest(path) == item["after_sha256"]
        assert hashlib.sha256(reverse(path.read_text(), item["isolated_unified_diff"]).encode()).hexdigest() == item["before_sha256"]
        inverse_results.append({"source": str(path.relative_to(ROOT)), "before_sha256": item["before_sha256"], "after_sha256": item["after_sha256"]})
    logs = []

    def check_run(path, status, methods):
        record = read_json(path)
        raw = Path(record["log_path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == record["log_sha256"]
        assert len(raw) == record["log_bytes"] <= 1048576
        assert record["status"] == status and record["tests_reported"] == methods > 0
        assert record["skipped_reported"] == record["expected_failures_reported"] == 0
        assert record["timeout_seconds"] == 30 and record["max_log_bytes"] == 1048576
        footer = re.search(rb"Ran (\d+) tests? in [0-9.]+s\s+(OK|FAILED(?: \([^\r\n]+\))?)\s*\Z", raw)
        assert footer and int(footer.group(1)) == methods
        if status == "passed":
            assert record["exit_code"] == 0 and footer.group(2) == b"OK"
        else:
            assert status == "failed" and record["exit_code"] != 0 and footer.group(2).startswith(b"FAILED")
        logs.append({"path": str(path.relative_to(ROOT)), "result_sha256": digest(path), "log_sha256": record["log_sha256"], "methods": methods, "status": status})

    for item in artifact["required_runs"]:
        check_run(Path(sources[item["source_id"]]["path"]), item["status"], item["methods"])
    assert evidence["sources_before"] == evidence["sources_after"] == {s["id"]: s["sha256"] for s in sources.values()}
    for item in evidence["additional_artifacts"]:
        path = Path(item["path"])
        assert path.is_absolute() and path.is_relative_to(RUNS)
        assert digest(path) == item["sha256"]
    for item in evidence["runs"]:
        path = Path(item["path"])
        assert path.is_absolute() and path.is_relative_to(RUNS) and digest(path) == item["sha256"]
        check_run(path, item["status"], item["methods"])
    assert sum(r["methods"] for r in evidence["runs"]) == evidence["total_methods"]
    for path, expected in read_json(ROOT / "design/local-memory-v1-hardening/checkpoint.json")["protected_user_changes"].items():
        assert digest(ROOT / path) == expected
    print(json.dumps({"task_id": TASK, "status": "pass_local_f05a" if report["status"] == "pass" else "valid_report_requires_" + report["status"],
        "checked_at_utc": datetime.now(timezone.utc).isoformat(), "artifact_sha256": artifact_hash, "report_sha256": report_hash,
        "audit_evidence_sha256": evidence_hash, "validator_sha256": digest(Path(__file__)), "audit_repair_round": artifact["audit_repair_round"],
        "source_count": len(sources), "node_count": len(nodes), "edge_count": len(edges),
        "strict_schema_and_references": "pass", "current_and_auditor_source_hashes": "pass", "protected_user_hashes": "pass",
        "inverse_deltas": inverse_results, "logs": logs, "independent_methods": evidence["total_methods"],
        "scope": "F05a explicit-input standalone resolver reconstruction and immediate bootstrap gate only. No downstream Reader/projector post-gate tamper closure, immutable decision snapshot, UI authenticity, global races, source freshness or whole F05/V1/release acceptance."}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
