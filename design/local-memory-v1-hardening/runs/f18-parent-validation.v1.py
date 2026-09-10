"""Read-only parent checks; emits JSON, never edits artifacts or product files."""
import hashlib
import json
from pathlib import Path
import re
from datetime import datetime, timezone

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"
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
    artifact_path = RUNS / "f18-graph-artifact.v1.json"
    report_path = RUNS / "f18-audit-report.v1.json"
    evidence_path = RUNS / "f18-audit-evidence.v1.json"
    artifact, report, evidence = map(read_json, (artifact_path, report_path, evidence_path))
    schema = read_json(SCHEMA)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(report)
    task = "lms-v1-00-hardening-2026-09-09-f18"
    assert artifact["task_id"] == report["task_id"] == evidence["task_id"] == task
    assert digest(artifact_path) == report["artifact_hash"] == evidence["artifact_sha256"] == "50fbc6b89d17065f9f8920954088998e68d499770515b02a998d5274a087f005"
    assert digest(report_path) == "0349a0d8b9b73806aef241760e874c4062983c7af91f925aece65ad0a2ea4e86"
    assert report["status"] == "pass"
    assert report["summary"] == {"blocking_failures": 0, "unresolved_count": 0, "next_action": "deliver"}
    assert report["auditor_context"]["independence_level"] == "same_model_separate_context"
    sources = {item["id"]: item for item in artifact["sources"]}
    nodes = {item["id"]: item for item in artifact["nodes"]}
    edges = {item["id"]: item for item in artifact["edges"]}
    assert len(sources) == len(artifact["sources"]) == 37
    assert len(nodes) == len(artifact["nodes"]) == 6
    assert len(edges) == len(artifact["edges"]) == 4
    assert len(set(sources) | set(nodes) | set(edges)) == 47
    targets = set(nodes) | set(edges)
    for item in list(nodes.values()) + list(edges.values()):
        assert item["basis"] and set(item["basis"]) <= set(sources)
    for edge in edges.values():
        assert edge["source"] in nodes and edge["target"] in nodes
        assert edge["relation"].strip()
    check_ids = set()
    for check in report["checks"]:
        assert check["check_id"] not in check_ids
        check_ids.add(check["check_id"])
        assert check["verdict"] == "pass" and check["required_action"] is None
        assert check["target_ids"] and set(check["target_ids"]) <= targets
        assert check["evidence_ids"] and set(check["evidence_ids"]) <= set(sources)
    before = {item["id"]: item for item in read_json(RUNS / "f18-audit-before.v1.json")["sources"]}
    after = {item["id"]: item for item in evidence["sources"]}
    for sid, source in sources.items():
        actual = digest(Path(source["path"]))
        assert actual == source["sha256"] == before[sid]["sha256"]
        assert actual == after[sid]["expected_sha256"] == after[sid]["before_sha256"] == after[sid]["after_sha256"]
    assert set(before) == set(after) == set(sources)
    log_checks = []

    def check_result(path, expected_status, expected_count):
        record = read_json(path)
        log_path = Path(record["log_path"])
        log = log_path.read_bytes()
        assert record["status"] == expected_status and record["tests_reported"] == expected_count
        assert record["log_bytes"] == len(log) and record["log_sha256"] == digest(log_path)
        assert record["skipped_reported"] == record["expected_failures_reported"] == 0
        assert record["timeout_seconds"] == 30 and record["max_log_bytes"] == 1048576
        if expected_status == "passed":
            assert record["exit_code"] == 0
            footer = re.search(rb"Ran (\d+) tests? in [0-9.]+s\s+OK\s*\Z", log)
            assert footer and int(footer.group(1)) == expected_count
        else:
            assert record["exit_code"] != 0
            assert re.search(rb"Ran 2 tests in [0-9.]+s\s+FAILED \(failures=4\)\s*\Z", log)
        log_checks.append({"path": str(path.relative_to(ROOT)), "result_sha256": digest(path), "log_sha256": digest(log_path), "status": expected_status, "methods": expected_count})
        return record

    for sid, count in (("RED", 2), ("FOCUSED_RUN", 13), ("E2E_RUN", 15), ("LINEAGE_RUN", 8), ("RESOLVER_RUN", 17), ("MIGRATION_RUN", 7), ("SECURITY_RUN", 1)):
        check_result(Path(sources[sid]["path"]), "failed" if sid == "RED" else "passed", count)
    expected_audit = [13, 15, 8, 17, 7, 8]
    assert len(evidence["auditor_executed_runs"]) == len(expected_audit)
    for run, count in zip(evidence["auditor_executed_runs"], expected_audit):
        path = ROOT / run["path"]
        record = check_result(path, "passed", count)
        assert run["result_sha256"] == digest(path)
        assert run["log_sha256"] == record["log_sha256"]
        assert run["tests_reported"] == count
    assert sum(expected_audit) == evidence["auditor_python314_test_methods"] == 68
    holdout = RUNS / "f18-adversarial-holdouts.v1.py"
    assert digest(holdout) == evidence["holdout_source_sha256"]
    checkpoint = read_json(ROOT / "design/local-memory-v1-hardening/checkpoint.json")
    for path, expected in checkpoint["protected_user_changes"].items():
        assert digest(ROOT / path) == expected
    extra_names = ["f18-audit-before.v1.json", "f18-audit-evidence.v1.json", "f18-adversarial-holdouts.v1.py", "f18-audit-callers-subreview.v1.json", "f18-audit-integrity.v1.py"]
    print(json.dumps({
        "schema_version": "1.0", "task_id": task,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "pass_local_f18", "audit_repair_round": 0,
        "artifact_sha256": digest(artifact_path), "report_sha256": digest(report_path),
        "draft_2020_12_schema": {"path": str(SCHEMA), "sha256": digest(SCHEMA), "result": "pass"},
        "parent_validator_sha256": digest(Path(__file__)),
        "checks": {"artifact_and_task_binding": "pass", "all_37_current_sources": "pass", "all_node_edge_basis_and_audit_references": "pass", "status_summary_consistency": "pass", "all_13_result_log_bindings_and_terminal_footers": "pass", "protected_user_hashes": "pass"},
        "additional_audit_sources": [{"path": str((RUNS / name).relative_to(ROOT)), "sha256": digest(RUNS / name)} for name in extra_names],
        "logs": log_checks, "auditor_python314_methods": 68,
        "executor_native_xlsx_python39": "1 method log verified; not independent reexecution or application compatibility",
        "limits": "Local F18 contract only. Structural and hash verification do not prove semantic truth. Independent human-equivalent approval is not claimed. Full package/embedded/model/GUI/all-format/global-race/power-loss/resource acceptance remains open. No production index or CONFIG mutation, commit or push."
    }, indent=2))


if __name__ == "__main__":
    main()
