"""Build hash-bound audit evidence on stdout for apply_patch preservation."""
from pathlib import Path
import ast
import hashlib
import importlib.util
import json

RUNS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("f05b_evidence_integrity", RUNS / "f05b-audit-integrity.v1.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
read = lambda path: helper.strict(path.read_bytes())
sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
artifact_path = RUNS / "f05b-graph-artifact.v1.json"
artifact = read(artifact_path)
before = read(RUNS / "f05b-audit-sources-before.v1.json")
after = read(RUNS / "f05b-audit-sources-after.v1.json")
expected = {item["id"]: item["sha256"] for item in artifact["sources"]}
assert len(expected) == 210 and before["sources"] == after["sources"] == expected
assert before["artifact_sha256"] == after["artifact_sha256"] == sha(artifact_path) == helper.ARTIFACT_HASH
for item in artifact["sources"]:
    assert sha(Path(item["path"])) == item["sha256"]
records = [helper.log_record(path) for path in sorted(RUNS.glob("f05b-audit-*/result.json"))]
residuals = [
    ("UNMARKED_TEST", "ResidualWitnessTests.test_cross_key_moves_renames_and_suffix_changes_leave_old_group_omitted", "unmarked-pure"),
    ("UNMARKED_TEST", "ResidualWitnessTests.test_year_kanji_parent_is_not_silently_normalized_more_broadly", "unmarked-pure"),
    ("YEAR_TEST", "CurrentMarkerResidualWitnessTests.test_current_marker_year_residual_is_observable", "year"),
]
source_paths = {item["id"]: Path(item["path"]) for item in artifact["sources"]}
witnesses = []
for sid, name, mode in residuals:
    assert name in helper.methods(source_paths[sid].read_bytes())
    log = RUNS / ("f05b-audit-" + mode + "-001") / "unittest.log"
    assert name.split(".", 1)[1] in log.read_text()
    witnesses.append({"source_id": sid, "method": name, "log_path": str(log),
                      "log_sha256": sha(log), "classification": "residual_observed_not_acceptance"})
total = sum(record["methods"] for record in records)
failed = sum(record["methods"] or 0 for record in records if record["status"] != "passed")
assert len(records) == 18 and total == 205 and failed == 0
additional = []
for path in sorted(RUNS.glob("f05b-audit-*")):
    if path.name == "f05b-audit-evidence.v1.json":
        continue
    for file in ([path] if path.is_file() else sorted(path.iterdir())):
        assert file.is_file() and not file.is_symlink() and file.is_relative_to(RUNS)
        additional.append({"path": str(file), "sha256": sha(file)})
assert len({item["path"] for item in additional}) == len(additional)
integrity = read(RUNS / "f05b-audit-integrity-result.v1.json")
assert integrity["audit_runs"] == records
print(json.dumps({"task_id": artifact["task_id"], "artifact_sha256": sha(artifact_path),
    "sources_before": before["sources"], "sources_after": after["sources"],
    "additional_artifacts": additional,
    "runs": [{key: record[key] for key in ("path", "sha256", "status", "methods")} for record in records],
    "total_methods": total, "acceptance_methods": total - len(witnesses), "residual_methods": len(witnesses),
    "failed_attempt_methods": failed, "failed_attempts": 0, "skipped_methods": 0, "expected_failures": 0,
    "independently_authored_holdout_methods": 20, "formal_product_repair_round": 0,
    "residual_witnesses": witnesses,
    "historical_attempts": integrity["historical_runs"],
    "historical_counts_not_in_audit_totals": True,
    "fixture_budgets": [{"result_path": record["path"], **record["fixture_budget"]}
                        for record in records if record["fixture_budget"] is not None],
    "safety": {"synthetic_tmp_only": True, "per_child_timeout_seconds": 30, "per_child_max_log_bytes": 1048576,
        "synthetic_source_bytes_per_case": 16384, "synthetic_decision_bytes": 8192,
        "synthetic_graph_bytes": 65536, "synthetic_inventory_records": 16, "synthetic_inventory_bytes": 16384,
        "explicit_f05b_fixture_bytes_per_process": 1048576,
        "guard_scope": "Reviewed F04 tmp/network/process guards and frozen F05b AUTHORS budget. Original collateral tests retain their separately reviewed inherited guards; budget output zero is not zero actual bytes. No whole-OS or all-product-artifact disk/RAM cap claim.",
        "forbidden_actions_performed": []},
    "limits": ["Same-model separate context is procedural separation only",
        "Historical schema0.1 registration uses actual old serializer/status and current synthetic stage resources, not an entire old-app replay",
        "Python open-count hooks are not global kernel FD/race proofs",
        "No F06/F19 Human or root authenticity, freshness, leases, cross-key/year retention or global-race closure",
        "No all-F05/all-V1/all-format/real-model/package/GUI/release acceptance"],
    "skills_used": ["codex-graph-engineering-adapter", "graph-engineering-agentic-audit"]}, ensure_ascii=False, indent=2))
