"""Read-only F05b artifact construction; stdout JSON for root apply_patch.

Checks current sources, retained logs, inverse patches and root gold snapshots.
No imports of product/test modules and no test execution or product writes.
"""
from pathlib import Path
from datetime import datetime, timezone
import ast
import hashlib
import json
import re
import runpy

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"
TASK = "local-memory-v1-f05b-snapshot-complete-selection"

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def read(path):
    return json.loads(path.read_bytes())

def main():
    manifest_path = RUNS / "f05b-executor-manifest.v1.json"
    assert sha(manifest_path) == "4f5e3c14e3a16f4863e6cb445717eabd8246584077b1467771117421622d80a2"
    manifest = read(manifest_path)
    assert manifest["task_id"] == TASK and manifest["formal_audit_repairs"] == 0
    checkpoint = read(ROOT / "design/local-memory-v1-hardening/checkpoint.json")
    for path, expected in checkpoint["protected_user_changes"].items():
        assert sha(ROOT / path) == expected
    assert sha(ROOT / checkpoint["plan"]) == checkpoint["plan_sha256"]
    for item in manifest["files"]:
        path = ROOT / item["path"]
        assert sha(path) == item["sha256"] and path.stat().st_size == item["bytes"], str(path)
    sources, path_ids = {}, {}
    def add(sid, path):
        path = Path(path)
        assert path.is_absolute() and path.is_file(), str(path)
        if str(path) in path_ids:
            return path_ids[str(path)]
        assert sid not in sources
        sources[sid] = dict(id=sid, path=str(path), sha256=sha(path), bytes=path.stat().st_size)
        path_ids[str(path)] = sid
        return sid
    previous = {item["id"]: item for item in read(RUNS / "f05a-graph-artifact.v1.json")["sources"]}
    changed = {str(ROOT / item[0]) for item in manifest["isolated_deltas"]}
    keep = "YEAR_TEST FOCUSED_TEST LINEAGE_TEST MIGRATION_TEST SECURITY_TEST READER VALIDATOR PROJECTOR SERVER PARSER BUILD_INTERMEDIATE ADAPTER SEARCH_BUILDER INTERMEDIATE_VALIDATOR INTERMEDIATE_STREAM_VALIDATOR SEARCH_VALIDATOR SEARCH_STREAM_VALIDATOR PATH_BUILDER PATH_VALIDATOR ANSWER ANSWER_BASE QEG FINAL_AUDIT SECURITY_GATE GUARD DISPATCHER SUPERVISOR SCHEMA_DOCUMENT SCHEMA_EVIDENCE SCHEMA_RELATION SCHEMA_SEARCH_UNIT RESOLVER RESOLVER_TEST BOOTSTRAP E2E_TEST UNMARKED_TEST UNMARKED_APP_TEST".split()
    for sid in keep:
        item = previous[sid]
        if item["path"] not in changed:
            assert sha(Path(item["path"])) == item["sha256"], sid
        add(sid, item["path"])
    names = {
        "CONTRACT": RUNS / "f05b-task-contract.v1.md",
        "CLARIFICATION": RUNS / "f05b-contract-clarification.v1.md",
        "COORDINATION": RUNS / "f05b-coordination-addendum.v1.md",
        "API_PREFLIGHT": RUNS / "f05b-api-preflight.v1.md",
        "REFINEMENT": RUNS / "f05b-contract-refinement.v1.md",
        "PREAUDIT_REVIEW": RUNS / "f05b-preaudit-test-review.v1.md",
        "ROOT_APP_TEST": ROOT / "tests/test_decision_snapshot_e2e.py",
        "ROOT_CONTROLS": ROOT / "tests/test_decision_snapshot_controls.py",
        "PURE_TEST": ROOT / "tests/test_decision_snapshot_attestation.py",
        "F05A_PURE": ROOT / "tests/test_version_graph_reconstruction.py",
        "F05A_GATE_TEST": ROOT / "tests/test_version_graph_validation_e2e.py",
        "RUNTIME_TEST": ROOT / "distribution/macos-local-memory/tests/test_runtime_recovery.py",
        "README": ROOT / "distribution/macos-local-memory/README.md",
        "EXECUTOR_MANIFEST": manifest_path,
        "EXECUTOR_SUMMARY": RUNS / "f05b-executor-summary.v1.md",
        "ROOT_RESULTS": RUNS / "f05b-root-results-check.v1.json",
        "ROOT_RESULTS_CHECKER": RUNS / "f05b-root-results-check.v1.py",
        "ROOT_README_DELTA": RUNS / "f05b-root-readme-delta.v1.json",
        "FIXTURE_BUDGET": RUNS / "f05b-fixture-budget.v1.py",
        "ROOT_RUNNER": RUNS / "f05b-root-run.v2.py",
        "ROOT_RUNNER_ORIGINAL": RUNS / "f05b-root-run.v1.py",
        "ROOT_CONTROLS_RUNNER": RUNS / "f05b-root-controls-run.v1.py",
        "COLLATERAL_RUNNER": RUNS / "f05b-root-collateral-run.v1.py",
        "INVERSE_HELPER": RUNS / "f18-audit-integrity.v1.py",
        "FREEZE_GENERATOR": Path(__file__),
    }
    for sid, path in names.items():
        add(sid, path)
    for i, item in enumerate(manifest["files"]):
        add("EXEC_FILE_%03d" % i, ROOT / item["path"])
    for name in ("f05b-root-gold.v1.md", "f05b-root-red-assessment.v1.md", "f05b-root-readme-delta.v1.py",
                 "f05b-root-controls-gold.v1.md", "f05b-root-controls-gold.v2.md", "f05b-root-controls-gold.v3.md", "f05b-root-controls-gold.v4.md",
                 "f05b-root-provenance.v1.py", "f05b-root-provenance.v2.py", "f05b-root-provenance.v3.py",
                 "f05b-root-gold-snapshots.v1.json", "f05b-root-gold-snapshots.v2.json", "f05b-root-gold-snapshots.v3.json"):
        add("ROOT_AUX_%03d" % len(sources), RUNS / name)
    # Root gold snapshots are validated without executing their historical
    # reconstruction scripts, which intentionally require older live revisions.
    packets = [read(RUNS / ("f05b-root-gold-snapshots.v%d.json" % version)) for version in (1, 2, 3)]
    snapshots = packets[0]["snapshots"] + [packets[1]["snapshot"], packets[2]["snapshot"]]
    for snapshot in snapshots:
        raw = snapshot["source"].encode()
        assert hashlib.sha256(raw).hexdigest() == snapshot["sha256"] and len(raw) == snapshot["bytes"]
        methods = {node.name + "." + child.name: hashlib.sha256(ast.dump(child, include_attributes=False).encode()).hexdigest()
                   for node in ast.parse(raw.decode()).body if isinstance(node, ast.ClassDef)
                   for child in node.body if isinstance(child, ast.FunctionDef) and child.name.startswith("test_")}
        assert methods == snapshot["methods"]
    for old, new in zip(snapshots[1:], snapshots[2:]):
        assert all(new["methods"][key] == value for key, value in old["methods"].items())
    assert sha(names["ROOT_APP_TEST"]) == snapshots[0]["sha256"]
    assert sha(names["ROOT_CONTROLS"]) == snapshots[-1]["sha256"]
    inverse = runpy.run_path(str(names["INVERSE_HELPER"]), run_name="f05b_inverse")["reverse_unified"]
    deltas = []
    for triple in manifest["isolated_deltas"]:
        current, before, patch = (ROOT / name for name in triple)
        assert inverse(current.read_text(), patch.read_text()).encode() == before.read_bytes(), triple
        deltas.append([path_ids[str(ROOT / name)] for name in triple])
    readme_delta = read(names["ROOT_README_DELTA"])
    assert sha(names["README"]) == readme_delta["after_sha256"]
    reconstructed = inverse(names["README"].read_text(), readme_delta["isolated_unified_diff"])
    assert reconstructed == readme_delta["before_source"]
    assert hashlib.sha256(reconstructed.encode()).hexdigest() == readme_delta["before_sha256"]
    attempted = [(ROOT / item["path"], item["status"], item["tests"]) for item in manifest["runs"]]
    attempted += [(Path(item["result_path"]), item["status"], item["methods"]) for item in read(names["ROOT_RESULTS"])["records"]]
    required_runs = []
    for i, (path, status, count) in enumerate(attempted):
        value = read(path)
        log = Path(value["log_path"])
        raw = log.read_bytes()
        assert value["status"] == status and value["tests_reported"] == count
        assert sha(log) == value["log_sha256"] and len(raw) == value["log_bytes"] <= 1048576
        assert value["timeout_seconds"] == 30 and value["max_log_bytes"] == 1048576
        footer = re.search(rb"Ran (\d+) tests? in [0-9.]+s\s+(OK|FAILED)(?: \(([^\r\n]*)\))?\s*\Z", raw)
        if count is None:
            assert status == "failed" and footer is None and b"SyntaxError" in raw
        else:
            assert footer and int(footer[1]) == count
            if status == "passed":
                assert footer[2] == b"OK" and footer[3] is None and value["exit_code"] == 0
                assert value["skipped_reported"] == value["expected_failures_reported"] == 0
        sid = add("RUN_%03d" % i, path)
        add("LOG_%03d" % i, log)
        add("STARTED_%03d" % i, path.parent / "started.json")
        required_runs.append(dict(source_id=sid, status=status, methods=count))
    def node(sid, text, basis):
        return dict(id=sid, text=text, basis=basis)
    nodes = [
        node("N_CAPTURE", "Each new app generation exclusively captures one bounded strict decision snapshot, with explicit descriptor and static generation/01-path non-symlink directory checks. Missing shared input alone gives fixed empty bytes; existing targets and previous publication are preserved on tested failures.", ["BOOTSTRAP", "ROOT_CONTROLS", "CONTRACT"]),
        node("N_ATTEST", "Required modes distinguish no decisions, legacy explicit input and generation snapshot. Snapshot hash expectation is external; three explicit inputs are parsed/hashed from one read each and reconstructed results are returned detached. Failed attestation has no usable payload.", ["RESOLVER", "PURE_TEST", "CLARIFICATION"]),
        node("N_SELECTION", "Reader and Validator derive the full ordered eligible inventory from attestation, retaining normal exclusions and checking manifest, inventory identity, exact counts and five selection limitations. Coherent Contact omission and post-initial-gate false activation are rejected.", ["READER", "VALIDATOR", "ROOT_APP_TEST", "ROOT_CONTROLS"]),
        node("N_PROJECT", "Versioned projector requires explicit exact authority context and checked detached binding; omission and bare PASS do not confer authority. Normal synthetic query/final-audit and security/lineage controls remain relevant.", ["PROJECTOR", "E2E_TEST", "ROOT_APP_TEST", "ROOT_CONTROLS"]),
        node("N_REGISTER", "New app registration requires the capture descriptor even when producer graph binding is absent. Saved verification first checks CONFIG-bound contract bytes and obtains its expected snapshot, never today's shared decisions. Old incompatible generations request migration.", ["BOOTSTRAP", "MIGRATION_TEST", "ROOT_CONTROLS", "RUNTIME_TEST"]),
        node("N_CONTROLS", "Original root6 semantic RED became GREEN, additive root16 controls and pure28 passed in bounded synthetic runs. Static-directory RED2, intermediate compatibility errors, skipped then native rerun, source deltas and all original gold are retained. Formal audit is still required.", ["ROOT_RESULTS", "EXECUTOR_MANIFEST", "EXECUTOR_SUMMARY", "FIXTURE_BUDGET", "PREAUDIT_REVIEW"]),
        node("N_LIMITS", "Only F05b is proposed for scoped acceptance. Human authenticity, freshness, cross-key family/year residuals, publication leases, all-ancestor/global races, total resource guarantees, all formats/real models/GUI/package and full V1 remain open.", ["CONTRACT", "README", "YEAR_TEST", "UNMARKED_TEST", "COORDINATION"]),
    ]
    links = [("CAPTURE", "ATTEST", "provides explicit captured decision bytes and expected identity", ["BOOTSTRAP", "RESOLVER"]),
             ("ATTEST", "SELECTION", "supplies same-read full inventory and reconstructed dispositions", ["RESOLVER", "READER", "VALIDATOR"]),
             ("SELECTION", "PROJECT", "requires independently checked complete selection before indexing", ["VALIDATOR", "PROJECTOR", "ROOT_APP_TEST"]),
             ("CAPTURE", "REGISTER", "anchors a fixed decision expectation to a named app generation", ["BOOTSTRAP", "ROOT_CONTROLS"]),
             ("CONTROLS", "LIMITS", "bounds test evidence and keeps residuals separate from acceptance", ["ROOT_RESULTS", "EXECUTOR_SUMMARY", "README"])]
    edges = [dict(id="E_" + left + "_" + right, source="N_" + left, target="N_" + right, relation=relation, basis=basis) for left, right, relation, basis in links]
    for item in nodes + edges:
        assert set(item["basis"]) <= set(sources)
    artifact = dict(schema_version="1.0", task_id=TASK, artifact_version=1, audit_repair_round=0,
                    status="awaiting_separate_audit", task_contract=dict(source_ids=["CONTRACT", "CLARIFICATION", "COORDINATION"], maximum_repair_cycles=2, role_separation="same_model_separate_context"),
                    nodes=nodes, edges=edges, sources=list(sources.values()), isolated_deltas=deltas, required_runs=required_runs)
    print(json.dumps(dict(artifact=artifact, preflight=dict(status="integrity_pass_not_formal_acceptance", checked_at_utc=datetime.now(timezone.utc).isoformat(),
                     manifest_files_checked=len(manifest["files"]), source_count=len(sources), inverse_deltas_checked=len(deltas)+1,
                     result_logs_checked=len(attempted), root_gold_methods=[6,7,10,13,16], protected_hashes="pass")), ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
