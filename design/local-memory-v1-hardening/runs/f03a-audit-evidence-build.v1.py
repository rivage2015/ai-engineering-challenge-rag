"""Generate exclusive audit provenance from actual hashes and test footers.

No product execution: reads frozen code and audit records, checks exact deltas,
and creates only the new audit evidence JSON. Does not manufacture test status.
"""
from __future__ import annotations
import ast
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text())


def apply_delta(text, patch, reverse=False):
    source = text.splitlines(keepends=True)
    changes = patch.splitlines(keepends=True)
    assert changes[0].startswith("--- ") and changes[1].startswith("+++ ")
    output, cursor, index = [], 0, 2
    while index < len(changes):
        match = re.fullmatch(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@[^\n]*\n", changes[index])
        assert match, changes[index]
        start = int(match.group(2 if reverse else 1)) - 1
        assert start >= cursor
        output.extend(source[cursor:start])
        cursor = start
        index += 1
        while index < len(changes) and not changes[index].startswith("@@ "):
            marker, content = changes[index][0], changes[index][1:]
            assert marker in " +-"
            consume = marker in (" +" if reverse else " -")
            emit = marker in (" -" if reverse else " +")
            if consume:
                assert source[cursor] == content
                cursor += 1
            if emit:
                output.append(content)
            index += 1
    return "".join([*output, *source[cursor:]])


artifact_path = RUNS / "f03a-graph-artifact.v1.json"
artifact = read(artifact_path)
sources = {s["id"]: s for s in artifact["sources"]}
expected = {key: item["sha256"] for key, item in sources.items()}
before = read(RUNS / "f03a-audit-sources-before.v1.json")
after = read(RUNS / "f03a-audit-sources-after.v1.json")
assert before["sources"] == after["sources"] == expected
for item in sources.values():
    assert digest(Path(item["path"])) == item["sha256"], item["id"]
assert digest(artifact_path) == before["artifact_sha256"] == after["artifact_sha256"]
delta_checks = []
for current, old, patch in artifact["isolated_deltas"]:
    current_path, old_path, patch_path = [Path(sources[key]["path"]) for key in (current, old, patch)]
    assert apply_delta(old_path.read_text(), patch_path.read_text()).encode() == current_path.read_bytes()
    assert apply_delta(current_path.read_text(), patch_path.read_text(), True).encode() == old_path.read_bytes()
    delta_checks.append({"source": current, "before": old, "patch": patch, "forward_and_inverse_match": True})
readme_delta = read(Path(sources["README_DELTA"]["path"]))
readme_before = apply_delta(Path(sources["README"]["path"]).read_text(), readme_delta["isolated_unified_diff"], True)
assert hashlib.sha256(readme_before.encode()).hexdigest() == readme_delta["before_sha256"]


def functions(path):
    return {node.name: node for node in ast.parse(path.read_text()).body if isinstance(node, ast.FunctionDef)}


old_functions = functions(Path(sources["RESOLVER_BEFORE"]["path"]))
current_functions = functions(Path(sources["RESOLVER"]["path"]))
unchanged = ("family_key", "_family_component", "markers", "candidate_set_hash", "resolve_group", "graph_projection", "record_decision", "load_inventory", "load_decisions", "validate")
for name in unchanged:
    assert ast.dump(old_functions[name]) == ast.dump(current_functions[name]), name
assert [ast.dump(node) for node in old_functions["automatic_selection"].body] == [ast.dump(node) for node in current_functions["automatic_selection"].body[1:]]

run_specs = (("pure", 19, 2), ("resolver", 20, 0), ("year", 11, 1), ("app", 4, 0),
             ("e2e", 17, 0), ("focused", 13, 0), ("migration", 7, 0), ("holdout", 9, 0))
records, additional_paths = [], []
for mode, methods, residual in run_specs:
    path = RUNS / f"f03a-audit-{mode}-001/result.json"
    result = read(path)
    log = Path(result["log_path"])
    raw = log.read_bytes()
    footer = re.search(rb"\nRan (\d+) tests? in [0-9.]+s\n\nOK\s*\Z", raw)
    assert footer and int(footer.group(1)) == methods == result["tests_reported"]
    assert result["status"] == "passed" and result["exit_code"] == 0
    assert result["skipped_reported"] == result["expected_failures_reported"] == 0
    assert result["timeout_seconds"] == 30 and result["max_log_bytes"] == 1048576
    assert len(raw) == result["log_bytes"] <= 1048576
    assert digest(log) == result["log_sha256"]
    records.append({"path": str(path), "sha256": digest(path), "status": result["status"],
                    "methods": methods, "residual_witness_methods": residual,
                    "acceptance_control_or_collateral_methods": methods - residual,
                    "suite": mode, "log_sha256": digest(log)})
    additional_paths.extend((path.parent / "started.json", log))
additional_paths.extend(RUNS / filename for filename in (
    "f03a-audit-run.v1.py", "f03a-audit-holdouts.v1.py", "f03a-audit-report.v1.json",
    "f03a-audit-sources-before.v1.json", "f03a-audit-sources-after.v1.json",
    "f03a-audit-evidence-build.v1.py",
))
evidence = {
    "task_id": artifact["task_id"], "artifact_sha256": digest(artifact_path),
    "sources_before": before["sources"], "sources_after": after["sources"],
    "additional_artifacts": [{"path": str(path), "sha256": digest(path)} for path in additional_paths],
    "runs": records, "total_methods": sum(r["methods"] for r in records),
    "acceptance_control_or_collateral_methods": sum(r["acceptance_control_or_collateral_methods"] for r in records),
    "residual_witness_methods": sum(r["residual_witness_methods"] for r in records),
    "independent_holdout_methods": 9,
    "holdout_subcases": {"all_suffix_signal_pairs": 50, "all_marked_differential_pairs": 66},
    "historical_run_records_verified": len(before["historical_runs"]),
    "delta_checks": delta_checks, "readme_inverse": "matches_original_hash",
    "unchanged_function_ast_checks": list(unchanged),
    "automatic_selection_after_new_guard": "matches_saved_before_function_body",
    "fixture_corrections": "Two documented filename corrections; no assertion or test-method removal. Final-gold RED is retrospective; original RED and failures are retained.",
    "role_separation": "same_model_separate_context; additional read-only child static review is procedural and not independent source evidence",
    "limits": [
        "F03a same-key candidate discovery and mixed-set hold only; not whole F03/V1/product acceptance",
        "All new application wiring runs use Python3.14 and synthetic in-process inference stubs",
        "Prior lineage8 and Python3.9 security1 logs verified but not rerun as independent tests",
        "No real models, network, GUI, installation, production CONFIG/index, commit, push or escalation",
        "Guard is not OS isolation; source cap hooks Path.write_text under source paths and fixtures are reviewed small",
        "F05 forged graph reconstruction, F06/F19 review publication/lease and F13 source freshness remain open",
    ],
}
assert evidence["total_methods"] == 100
assert evidence["acceptance_control_or_collateral_methods"] == 97 and evidence["residual_witness_methods"] == 3
target = RUNS / "f03a-audit-evidence.v1.json"
with target.open("x", encoding="utf-8") as handle:
    json.dump(evidence, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
print(json.dumps({"path": str(target), "sha256": digest(target),
                  "report_sha256": digest(RUNS / "f03a-audit-report.v1.json"),
                  "total_methods": evidence["total_methods"],
                  "frozen_sources_unchanged": len(expected)}, indent=2))
