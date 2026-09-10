"""Read-only parent formal/source/delta/log validation; emits JSON only."""
import hashlib
import json
from pathlib import Path
import re
import runpy
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"


def main():
    common_path = RUNS / "f18-parent-validation.v1.py"
    assert hashlib.sha256(common_path.read_bytes()).hexdigest() == "1a3ffa143792dd4eeb262f0bd7d27aea65cec6e8350ed84b697cfaba6598d963"
    common = runpy.run_path(str(common_path), run_name="f02a_parent_common")
    digest, read_json = common["digest"], common["read_json"]
    integrity_path = RUNS / "f18-audit-integrity.v1.py"
    assert digest(integrity_path) == "ed13ff27e65d73a6152f6bc3000df0e68b91b168b650344500122466a7bd1dd6"
    reverse = runpy.run_path(str(integrity_path), run_name="f02a_parent_reverse")["reverse_unified"]
    artifact_path, report_path = RUNS / "f02a-graph-artifact.v1.json", RUNS / "f02a-audit-report.v1.json"
    artifact, report = read_json(artifact_path), read_json(report_path)
    schema = read_json(common["SCHEMA"])
    common["Draft202012Validator"].check_schema(schema)
    common["Draft202012Validator"](schema).validate(report)
    assert digest(report_path) == sys.argv[1]
    task = "lms-v1-00-hardening-2026-09-09-f02a"
    assert artifact["task_id"] == report["task_id"] == task
    assert digest(artifact_path) == report["artifact_hash"] == "ea9a5c9616ef181fa8c75c5fd383ca536deabd4514c2b9e78c3d9d8d542f9ff8"
    assert report["status"] == "pass"
    assert report["summary"] == {"blocking_failures": 0, "unresolved_count": 0, "next_action": "deliver"}
    assert report["auditor_context"]["independence_level"] == "same_model_separate_context"
    sources = {item["id"]: item for item in artifact["sources"]}
    nodes = {item["id"]: item for item in artifact["nodes"]}
    edges = {item["id"]: item for item in artifact["edges"]}
    assert len(sources) == len(artifact["sources"]) == 52
    assert len(nodes) == len(artifact["nodes"]) == 6
    assert len(edges) == len(artifact["edges"]) == 4
    assert len(set(sources) | set(nodes) | set(edges)) == 62
    for item in list(nodes.values()) + list(edges.values()):
        assert item["basis"] and set(item["basis"]) <= set(sources)
    for edge in edges.values():
        assert edge["source"] in nodes and edge["target"] in nodes and edge["relation"].strip()
    check_ids = set()
    for check in report["checks"]:
        assert check["check_id"] not in check_ids
        check_ids.add(check["check_id"])
        assert check["verdict"] == "pass" and check["required_action"] is None
        assert check["target_ids"] and set(check["target_ids"]) <= set(nodes) | set(edges)
        assert check["evidence_ids"] and set(check["evidence_ids"]) <= set(sources)
    for source in sources.values():
        assert digest(Path(source["path"])) == source["sha256"]
    executor = read_json(Path(sources["EXECUTOR"]["path"]))
    assert executor["parent_task_id"] == task and executor["task_id"] == task + "-implementation"
    run_before = read_json(RUNS / "f02a-audit-before.v1.json")
    run_after = read_json(RUNS / "f02a-audit-after.v1.json")
    assert len(run_before["files"]) == len(run_after["files"]) == 16
    before_files = {item["path"]: item for item in run_before["files"]}
    assert set(before_files) == {item["path"] for item in run_after["files"]}
    for item in run_after["files"]:
        assert digest(ROOT / item["path"]) == item["sha256"] == before_files[item["path"]]["sha256"]
    for name in ("f02a-audit-artifact-before.v1.json", "f02a-audit-artifact-after.v1.json"):
        observed = read_json(RUNS / name)
        assert observed["artifact_sha256"] == digest(artifact_path)
        assert observed["source_count"] == 52
        assert len(observed["sources"]) == 52
        assert {item["id"] for item in observed["sources"]} == set(sources)
        for item in observed["sources"]:
            source = sources[item["id"]]
            assert item["path"] == source["path"]
            assert item["observed_sha256"] == source["sha256"]
            if "expected_sha256" in item:
                assert item["expected_sha256"] == source["sha256"]
    deltas = []
    for current, before, patch in (("RESOLVER", "RESOLVER_BEFORE", "RESOLVER_DELTA"), ("RESOLVER_TEST", "RESOLVER_TEST_BEFORE", "RESOLVER_TEST_DELTA"), ("E2E_TEST", "E2E_TEST_BEFORE", "E2E_TEST_DELTA")):
        reconstructed = reverse(Path(sources[current]["path"]).read_text(), Path(sources[patch]["path"]).read_text())
        assert reconstructed.encode() == Path(sources[before]["path"]).read_bytes()
        deltas.append({"source": current, "before_sha256": sources[before]["sha256"], "after_sha256": sources[current]["sha256"], "exact_inverse_matches": True})
    readme_delta = read_json(Path(sources["README_DELTA"]["path"]))
    before_readme = reverse(Path(sources["README"]["path"]).read_text(), readme_delta["isolated_unified_diff"])
    assert hashlib.sha256(before_readme.encode()).hexdigest() == readme_delta["before_sha256"]
    assert sources["README"]["sha256"] == readme_delta["after_sha256"]
    logs = []

    def check_run(path, count, failure_footer=None):
        record = read_json(path)
        log_path = Path(record["log_path"])
        log = log_path.read_bytes()
        assert len(log) == record["log_bytes"] and digest(log_path) == record["log_sha256"]
        assert record["tests_reported"] == count
        assert record["skipped_reported"] == record["expected_failures_reported"] == 0
        assert record["timeout_seconds"] == 30 and record["max_log_bytes"] == 1048576
        if failure_footer is None:
            assert record["status"] == "passed" and record["exit_code"] == 0
            suffix = rb"OK"
        else:
            assert record["status"] == "failed" and record["exit_code"] != 0
            suffix = re.escape(failure_footer.encode())
        footer = re.search(rb"Ran (\d+) tests? in [0-9.]+s\s+" + suffix + rb"\s*\Z", log)
        assert footer and int(footer.group(1)) == count
        logs.append({"path": str(path.relative_to(ROOT)), "result_sha256": digest(path), "log_sha256": digest(log_path), "status": record["status"], "methods": count})

    for sid, count, footer in (("RED", 11, "FAILED (failures=5)"), ("E2E_INITIAL_FAILED", 17, "FAILED (errors=1)"), ("PURE_RUN", 11, None), ("RESOLVER_RUN", 20, None), ("E2E_RUN", 17, None), ("FOCUSED_RUN", 13, None), ("MIGRATION_RUN", 7, None), ("LINEAGE_RUN", 8, None), ("SECURITY_RUN", 1, None)):
        check_run(Path(sources[sid]["path"]), count, footer)
    for name, count in (("f02a-audit-replay-001-pure", 11), ("f02a-audit-replay-002-resolver", 20), ("f02a-audit-replay-003-e2e", 17), ("f02a-audit-holdouts-001", 15)):
        check_run(RUNS / name / "result.json", count)
    audit_evidence_path = RUNS / "f02a-audit-evidence.v1.json"
    assert digest(audit_evidence_path) == "04a480884f66d0b79817adb9a89fdda9450fd7b726274d64f96396c575e7285b"
    audit_evidence = read_json(audit_evidence_path)
    assert audit_evidence["task_id"] == task and audit_evidence["artifact_sha256"] == digest(artifact_path)
    assert audit_evidence["auditor_executed_methods"] == 63
    assert audit_evidence["acceptance_control_executions"] == 60
    assert audit_evidence["residual_witness_executions_not_acceptance"] == 3
    assert len(audit_evidence["artifacts"]) == 18
    for item in audit_evidence["artifacts"]:
        path = Path(item["path"])
        assert digest(path) == item["sha256"] and len(path.read_bytes()) == item["bytes"]
    for item in audit_evidence["run_results"]:
        path = RUNS / item["run_id"] / "result.json"
        record = read_json(path)
        assert digest(path) == item["result_sha256"]
        assert record["log_sha256"] == item["log_sha256"]
        assert record["tests_reported"] == item["tests_reported"]
    for path, expected in read_json(ROOT / "design/local-memory-v1-hardening/checkpoint.json")["protected_user_changes"].items():
        assert digest(ROOT / path) == expected
    extra_names = ["f02a-audit-before.v1.json", "f02a-audit-after.v1.json", "f02a-audit-artifact-before.v1.json", "f02a-audit-artifact-after.v1.json", "f02a-audit-replay.v1.py", "f02a-audit-evidence.v1.json", "f02a-implementation-manifest.v1.json"]
    print(json.dumps({
        "schema_version": "1.0", "task_id": task, "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "pass_local_f02a", "audit_repair_round": 0,
        "artifact_sha256": digest(artifact_path), "report_sha256": digest(report_path),
        "draft_2020_12_schema": {"path": str(common["SCHEMA"]), "sha256": digest(common["SCHEMA"]), "result": "pass"},
        "parent_validator_sha256": digest(Path(__file__)),
        "helper_hashes": {str(path.relative_to(ROOT)): digest(path) for path in (common_path, integrity_path)},
        "checks": {"task_artifact_binding": "pass", "all_52_current_sources_and_auditor_formal_snapshots": "pass", "all_graph_and_report_references": "pass", "status_summary": "pass", "three_product_test_deltas_and_readme_baseline": "pass", "all_13_logs_and_terminal_footers": "pass", "additional_18_auditor_artifacts_and_counts": "pass", "protected_user_hashes": "pass"},
        "isolated_deltas": deltas,
        "additional_audit_sources": [{"path": str((RUNS / name).relative_to(ROOT)), "sha256": digest(RUNS / name)} for name in extra_names],
        "logs": logs, "auditor_python314_methods": {"executions": 63, "acceptance_or_control": 60, "residual_witness_not_acceptance": 3},
        "root_collateral": "13 focused + 7 migration + 8 lineage (Python3.14), 1 native XLSX security (Python3.9 only; not full application compatibility).",
        "limits": "F02a final-year-branch hold only. Structural/hash verification does not prove semantic truth. Independent annual retention, current/multitoken signals, candidate discovery, independent partition, review publication and temporal freshness remain open. Old CONFIG/index preservation is not source freshness attestation. No whole-product/format/model/GUI/package/resource acceptance or commit/push."
    }, indent=2))


if __name__ == "__main__":
    main()
