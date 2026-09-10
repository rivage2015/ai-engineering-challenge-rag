"""Strict local report validation; stdout only, no product imports."""
from pathlib import Path
import hashlib
import importlib.util
import json
from jsonschema import Draft202012Validator

RUNS = Path(__file__).resolve().parent
SCHEMA = Path("/Users/takashifukutomi/.codex/skills/graph-engineering-agentic-audit/assets/agentic-audit-report.schema.json")
spec = importlib.util.spec_from_file_location("f05b_audit_strict_json", RUNS / "f05b-audit-integrity.v1.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
read = lambda path: helper.strict(path.read_bytes())
artifact_path, report_path = RUNS / "f05b-graph-artifact.v1.json", RUNS / "f05b-audit-report.v1.json"
artifact, report, schema = read(artifact_path), read(report_path), read(SCHEMA)
Draft202012Validator.check_schema(schema)
Draft202012Validator(schema).validate(report)
assert report["task_id"] == artifact["task_id"] and report["artifact_hash"] == hashlib.sha256(artifact_path.read_bytes()).hexdigest()
assert report["status"] == "pass" and report["summary"] == {"blocking_failures": 0, "unresolved_count": 0, "next_action": "deliver"}
sources = {item["id"] for item in artifact["sources"]}
targets = {item["id"] for key in ("nodes", "edges") for item in artifact[key]}
seen = set()
for check in report["checks"]:
    assert check["target_ids"] and set(check["target_ids"]) <= targets
    assert check["evidence_ids"] and set(check["evidence_ids"]) <= sources
    assert check["verdict"] == "pass" and check["required_action"] is None
    seen.update(check["target_ids"])
assert seen == targets
assert len({check["check_id"] for check in report["checks"]}) == len(report["checks"])
print(json.dumps({"status": "strict_schema_references_and_summary_valid_not_semantic_verdict",
    "artifact_sha256": report["artifact_hash"], "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
    "checker_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    "schema_sha256": hashlib.sha256(SCHEMA.read_bytes()).hexdigest(),
    "checks": len(report["checks"]), "covered_nodes": len(artifact["nodes"]), "covered_edges": len(artifact["edges"])}, indent=2))
