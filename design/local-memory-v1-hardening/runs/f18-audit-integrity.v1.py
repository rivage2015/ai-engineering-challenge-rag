"""Read-only, stdlib verification of F18 audit source/log/delta provenance."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def reverse_unified(current, patch):
    lines = patch.splitlines(keepends=True)
    original = []
    current_lines = current.splitlines(keepends=True)
    cursor = 0
    position = 0
    while position < len(lines):
        header = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", lines[position])
        if not header:
            position += 1
            continue
        start = int(header.group(3)) - 1
        original.extend(current_lines[cursor:start])
        position += 1
        before, after = [], []
        while position < len(lines) and not lines[position].startswith("@@"):
            line = lines[position]
            if line[:1] in {" ", "-"}:
                before.append(line[1:])
            if line[:1] in {" ", "+"}:
                after.append(line[1:])
            position += 1
        assert current_lines[start:start + len(after)] == after
        original.extend(before)
        cursor = start + len(after)
    original.extend(current_lines[cursor:])
    return "".join(original)


def main():
    artifact_path = RUNS / "f18-graph-artifact.v1.json"
    artifact = json.loads(artifact_path.read_bytes())
    baseline = json.loads((RUNS / "f18-audit-before.v1.json").read_bytes())
    before = {record["id"]: record for record in baseline["sources"]}
    sources = [{
        "id": source["id"],
        "expected_sha256": source["sha256"],
        "before_sha256": before[source["id"]]["sha256"],
        "after_sha256": sha(Path(source["path"]).read_bytes()),
    } for source in artifact["sources"]]
    product_delta = []
    for entry in json.loads((RUNS / "f18-product-delta.v1.json").read_bytes())["files"]:
        current = (ROOT / entry["path"]).read_text()
        reconstructed = reverse_unified(current, entry["isolated_unified_diff"])
        product_delta.append({
            "path": entry["path"], "before_sha256": sha(reconstructed.encode()),
            "after_sha256": sha(current.encode()),
            "before_matches": sha(reconstructed.encode()) == entry["before_sha256"],
            "after_matches": sha(current.encode()) == entry["after_sha256"],
        })
    log_bindings = []
    for source in artifact["sources"]:
        if Path(source["path"]).name != "result.json":
            continue
        record = json.loads(Path(source["path"]).read_bytes())
        log = Path(record["log_path"]).read_bytes()
        log_bindings.append({
            "id": source["id"], "status": record["status"],
            "tests_reported": record["tests_reported"],
            "result_sha256": sha(Path(source["path"]).read_bytes()),
            "log_sha256": sha(log),
            "log_binding_matches": sha(log) == record["log_sha256"] and len(log) == record["log_bytes"],
        })
    audit_runs = []
    for name in ("f18-audit-001-focused", "f18-audit-002-e2e", "f18-audit-003-lineage", "f18-audit-004-resolver", "f18-audit-005-migration", "f18-adversarial-001-holdouts"):
        path = RUNS / name / "result.json"
        record = json.loads(path.read_bytes())
        log = Path(record["log_path"]).read_bytes()
        audit_runs.append({
            "path": str(path.relative_to(ROOT)),
            "status": record["status"], "tests_reported": record["tests_reported"],
            "skipped_reported": record["skipped_reported"],
            "expected_failures_reported": record["expected_failures_reported"],
            "result_sha256": sha(path.read_bytes()), "log_sha256": sha(log),
            "log_binding_matches": sha(log) == record["log_sha256"] and len(log) == record["log_bytes"],
            "timeout_seconds": record["timeout_seconds"], "max_log_bytes": record["max_log_bytes"],
        })
    result = {
        "task_id": artifact["task_id"], "audit_round": 0,
        "artifact_sha256": sha(artifact_path.read_bytes()),
        "all_37_sources_unchanged_and_match": len(sources) == 37 and all(row["expected_sha256"] == row["before_sha256"] == row["after_sha256"] for row in sources),
        "sources": sources, "product_delta_reconstruction": product_delta,
        "frozen_result_log_bindings": log_bindings, "auditor_executed_runs": audit_runs,
        "auditor_python314_test_methods": sum(row["tests_reported"] for row in audit_runs),
        "holdout_source_sha256": sha((RUNS / "f18-adversarial-holdouts.v1.py").read_bytes()),
        "limitations": ["No Python3.9 security reexecution; inspected and hash-bound the executor's 1-method native XLSX result.", "No full package, embedded SmartArt runtime, models, GUI, originals, production configuration/index or global snapshot acceptance."],
    }
    print(json.dumps(result, indent=2))
    assert result["all_37_sources_unchanged_and_match"]
    assert all(row["before_matches"] and row["after_matches"] for row in product_delta)
    assert all(row["log_binding_matches"] for row in log_bindings + audit_runs)
    assert all(row["status"] == "passed" and row["skipped_reported"] == row["expected_failures_reported"] == 0 for row in audit_runs)


if __name__ == "__main__":
    main()
