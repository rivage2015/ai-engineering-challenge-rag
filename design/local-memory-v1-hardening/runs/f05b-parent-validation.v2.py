"""Read-only parent checks; report integrity is not proof of product truth."""
from pathlib import Path
from datetime import datetime, timezone
import contextlib
import ast
import hashlib
import io
import json
import math
import re
import runpy
import sys
import subprocess
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"
TASK = "local-memory-v1-f05b-snapshot-complete-selection"
SCHEMA = Path("/Users/takashifukutomi/.codex/skills/graph-engineering-agentic-audit/assets/agentic-audit-report.schema.json")

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def read(path):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            assert key not in value, (str(path), key)
            value[key] = item
        return value
    def number(value):
        result = float(value)
        assert math.isfinite(result)
        return result
    def invalid(value):
        raise ValueError("nonfinite JSON constant: " + value)
    return json.loads(path.read_bytes(), object_pairs_hook=unique,
                      parse_float=number, parse_constant=invalid)

def main():
    artifact_name, artifact_hash, report_name, report_hash, evidence_hash = sys.argv[1:6]
    assert re.fullmatch(r"f05b-graph-artifact\.v[1-3]\.json", artifact_name)
    assert re.fullmatch(r"f05b-audit-report\.v[1-3]\.json", report_name)
    paths = [RUNS / artifact_name, RUNS / report_name,
             RUNS / report_name.replace("audit-report", "audit-evidence")]
    for path, expected in zip(paths, [artifact_hash, report_hash, evidence_hash]):
        assert re.fullmatch(r"[0-9a-f]{64}", expected) and sha(path) == expected
    artifact, report, evidence = map(read, paths)
    schema = read(SCHEMA)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(report)
    assert artifact["task_id"] == report["task_id"] == evidence["task_id"] == TASK
    assert report["artifact_hash"] == evidence["artifact_sha256"] == artifact_hash
    assert artifact["audit_repair_round"] in (0, 1, 2)
    assert report["auditor_context"]["independence_level"] == "same_model_separate_context"
    nodes, edges, sources = ({item["id"]: item for item in artifact[key]} for key in ("nodes", "edges", "sources"))
    known = set(nodes) | set(edges) | set(sources)
    assert len(known) == sum(len(artifact[key]) for key in ("nodes", "edges", "sources"))
    for item in [*nodes.values(), *edges.values()]:
        assert item["basis"] and set(item["basis"]) <= set(sources)
    for edge in edges.values():
        assert edge["source"] in nodes and edge["target"] in nodes and edge["relation"].strip()
    for item in sources.values():
        path = Path(item["path"])
        assert path.is_absolute() and sha(path) == item["sha256"], item["id"]
        assert path.stat().st_size == item["bytes"]
    checks, covered = report["checks"], set()
    assert checks and len({item["check_id"] for item in checks}) == len(checks)
    for item in checks:
        assert item["target_ids"] and set(item["target_ids"]) <= known
        assert item["evidence_ids"] and set(item["evidence_ids"]) <= set(sources)
        covered.update(item["target_ids"])
    assert set(nodes) | set(edges) <= covered
    blocking = sum(item["severity"] == "blocking" and item["verdict"] != "pass" for item in checks)
    unresolved = sum(item["verdict"] == "unresolved" for item in checks)
    assert report["summary"]["blocking_failures"] == blocking
    assert report["summary"]["unresolved_count"] == unresolved
    assert report["summary"]["next_action"] == {"pass":"deliver", "revise":"revise", "blocked":"human_decision"}[report["status"]]
    if report["status"] == "pass":
        assert blocking == unresolved == 0
        assert all(item["verdict"] == "pass" and item["required_action"] is None for item in checks)
    # Reconstruct the frozen artifact, checking retained manifest, all inverse
    # deltas, old gold AST and all original attempted logs including skips/errors.
    assert artifact["artifact_version"] == 1, "later product repair needs an explicit revised freeze checker"
    # Frozen AST hashes were authored under Python3.14, whose ast.dump is
    # different from Python3.9 used here for the installed schema library.
    regenerated = json.loads(subprocess.check_output(
        ["/opt/homebrew/bin/python3", "-I", "-B", str(RUNS / "f05b-freeze.v1.py")],
        cwd=ROOT, timeout=30))
    assert regenerated["artifact"] == artifact
    gold_sources = [RUNS / ("f05b-executor-gold-test.v%d.py" % version) for version in (1, 2, 3)]
    method_maps = []
    for path in gold_sources:
        method_maps.append({node.name + "." + child.name: ast.dump(child, include_attributes=False)
            for node in ast.parse(path.read_text()).body if isinstance(node, ast.ClassDef)
            for child in node.body if isinstance(child, ast.FunctionDef) and child.name.startswith("test_")})
    assert list(map(len, method_maps)) == [26, 27, 28]
    for old, new in zip(method_maps, method_maps[1:]):
        assert all(new.get(key) == value for key, value in old.items())
    assert gold_sources[-1].read_bytes() == (ROOT / "tests/test_decision_snapshot_attestation.py").read_bytes()
    assert evidence["sources_before"] == evidence["sources_after"] == {key:item["sha256"] for key,item in sources.items()}
    for item in evidence["additional_artifacts"]:
        path = Path(item["path"])
        assert path.is_absolute() and path.is_relative_to(RUNS) and sha(path) == item["sha256"]
    auxiliary_paths = {item["path"] for item in evidence["additional_artifacts"]}
    integrity_script = RUNS / "f05b-audit-integrity.v1.py"
    integrity_result = RUNS / "f05b-audit-integrity-result.v1.json"
    assert {str(integrity_script), str(integrity_result)} <= auxiliary_paths
    repeated_integrity = json.loads(subprocess.check_output(
        ["/opt/homebrew/bin/python3", "-I", "-B", str(integrity_script)], cwd=ROOT, timeout=30))
    assert repeated_integrity == read(integrity_result)
    logs = []
    for item in evidence["runs"]:
        path = Path(item["path"])
        assert path.is_absolute() and path.is_relative_to(RUNS) and sha(path) == item["sha256"]
        result = read(path)
        raw = Path(result["log_path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == result["log_sha256"]
        assert len(raw) == result["log_bytes"] <= 1048576
        assert result["timeout_seconds"] == 30 and result["max_log_bytes"] == 1048576
        assert result["status"] == item["status"] and result["tests_reported"] == item["methods"]
        footer = re.search(rb"Ran (\d+) tests? in [0-9.]+s\s+(OK|FAILED)(?: \(([^\r\n]*)\))?\s*\Z", raw)
        assert item["methods"] is not None and footer and int(footer[1]) == item["methods"] > 0
        if item["status"] == "passed":
            assert footer[2] == b"OK" and footer[3] is None and result["exit_code"] == 0
            assert result["skipped_reported"] == result["expected_failures_reported"] == 0
        else:
            assert item["status"] == "failed" and footer[2] == b"FAILED" and result["exit_code"] != 0
        logs.append(dict(path=str(path), sha256=sha(path), log_sha256=result["log_sha256"], status=item["status"], methods=item["methods"]))
    assert sum(item["methods"] for item in logs) == evidence["total_methods"]
    print(json.dumps(dict(task_id=TASK, status="pass_local_f05b" if report["status"] == "pass" else "valid_report_requires_"+report["status"],
        checked_at_utc=datetime.now(timezone.utc).isoformat(), artifact_sha256=artifact_hash,
        report_sha256=report_hash, audit_evidence_sha256=evidence_hash, validator_sha256=sha(Path(__file__)),
        source_count=len(sources), strict_schema_hash_references="pass", historical_integrity=regenerated["preflight"],
        independent_attempted_methods=evidence["total_methods"], audit_logs=logs,
        scope="F05b bounded synthetic snapshot authority and complete selection only. No whole V1/all-format/real-model/package/freshness/Human-authenticity/global-race acceptance."), ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
