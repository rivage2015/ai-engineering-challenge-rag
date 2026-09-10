"""Emit an immutable F03a review packet to stdout; no filesystem writes."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"
P = "distribution/macos-local-memory/"
R = "design/local-memory-v1-hardening/runs/"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    files = {
        "CONTRACT": R + "f03a-task-contract.v1.md",
        "AUTHORIZATION": R + "f03a-executor-authorization.v1.json",
        "RESOLVER": P + "engine/document_version_resolver.py",
        "RESOLVER_BEFORE": R + "f03a-before-resolver.v1.py",
        "RESOLVER_DELTA": R + "f03a-resolver-delta.v1.patch",
        "RESOLVER_TEST": P + "tests/test_document_version_resolver.py",
        "RESOLVER_TEST_BEFORE": R + "f03a-before-resolver-tests.v1.py",
        "RESOLVER_TEST_DELTA": R + "f03a-resolver-tests-delta.v1.patch",
        "PURE_TEST": "tests/test_unmarked_version_candidates.py",
        "PURE_INITIAL": R + "f03a-test-gold-initial.v1.py",
        "PURE_CORRECTION1": R + "f03a-test-gold-correction1-snapshot.v1.py",
        "GOLD_DELTA1": R + "f03a-test-gold-correction.v1.patch",
        "GOLD_DELTA2": R + "f03a-test-gold-correction.v2.patch",
        "GOLD_NOTE1": R + "f03a-test-gold-correction.v1.json",
        "GOLD_NOTE2": R + "f03a-test-gold-correction.v2.json",
        "APP_TEST": "tests/test_unmarked_version_e2e.py",
        "E2E_TEST": P + "tests/test_versioned_safe_index_e2e.py",
        "YEAR_TEST": "tests/test_year_only_supersession.py",
        "FOCUSED_TEST": "tests/test_immutable_lineage_validation.py",
        "LINEAGE_TEST": "tests/test_semantic_lineage_relations.py",
        "MIGRATION_TEST": P + "tests/test_reader_generation_migration.py",
        "SECURITY_TEST": "tests/test_security_graph_partition.py",
        "README": P + "README.md",
        "README_DELTA": R + "f03a-root-readme-delta.v1.json",
        "READER": P + "engine/build_adaptive_semantic_graph.py",
        "VALIDATOR": P + "engine/validate_adaptive_semantic_graph.py",
        "PROJECTOR": P + "engine/build_local_semantic_index.py",
        "BOOTSTRAP": P + "app/bootstrap.py",
        "SERVER": P + "app/local_memory_server.py",
        "PARSER": "scripts/probe_intermediate_records.py",
        "BUILD_INTERMEDIATE": "scripts/build_intermediate_records.py",
        "ADAPTER": "scripts/adapt_layer1_to_local_memory.py",
        "SEARCH_BUILDER": "scripts/build_search_units.py",
        "INTERMEDIATE_VALIDATOR": "scripts/validate_intermediate_records.py",
        "INTERMEDIATE_STREAM_VALIDATOR": "scripts/validate_intermediate_records_streaming.py",
        "SEARCH_VALIDATOR": "scripts/validate_search_units.py",
        "SEARCH_STREAM_VALIDATOR": "scripts/validate_search_units_streaming.py",
        "PATH_BUILDER": P + "engine/build_path_graph.py",
        "PATH_VALIDATOR": P + "engine/validate_path_graph.py",
        "ANSWER": P + "engine/answer_local_memory_v2.py",
        "ANSWER_BASE": P + "engine/answer_local_memory.py",
        "QEG": P + "engine/question_evidence_graph.py",
        "FINAL_AUDIT": P + "app/final_answer_audit.py",
        "SECURITY_GATE": P + "engine/content_security_gate.py",
        "GUARD": R + "f04a-executor-run.v1.py",
        "DISPATCHER": R + "f18-test-run.v2.py",
        "ROOT_RUNNER": R + "f03a-root-run.v1.py",
        "EXECUTOR_RUNNER": R + "f03a-executor-run.v1.py",
        "SUPERVISOR": "scripts/run_local_memory_hardening_tests.py",
    }
    for name in ("document", "evidence", "relation", "search-unit"):
        files["SCHEMA_" + name.upper().replace("-", "_")] = "schemas/" + name + ".schema.json"
    required_runs = []
    run_specs = [
        ("ROOT_RED", "f03a-root-red-app-001", "failed", 4, "true app RED"),
        ("PURE_RED_INITIAL", "f03a-executor-red-pure-001", "failed", 19, "original gold RED, includes later corrected fixture assumptions"),
        ("PURE_GREEN_FAILED1", "f03a-executor-green-pure-001", "failed", 19, "first post-fix run: two fixture errors, retained"),
        ("PURE_RED_CORRECTION1", "f03a-executor-corrected-red-pure-001", "failed", 19, "correction1 replay against saved original resolver"),
        ("PURE_GREEN_FAILED2", "f03a-executor-green-pure-002", "failed", 19, "correction1 run: numbered-copy fixture still wrong, retained"),
        ("PURE_RED_FINAL", "f03a-executor-corrected-red-pure-002", "failed", 19, "final corrected gold replay against saved original resolver"),
        ("PURE_GREEN", "f03a-executor-green-pure-003", "passed", 19, "17 acceptance/control + 2 explicit residual witnesses"),
        ("RESOLVER_GREEN", "f03a-executor-green-resolver-001", "passed", 20, "existing resolver/Reader controls"),
        ("YEAR_GREEN", "f03a-executor-green-year-001", "passed", 11, "10 acceptance/control + 1 existing annual-current residual"),
        ("APP_GREEN", "f03a-root-green-app-001", "passed", 4, "new synthetic app wiring"),
        ("E2E_GREEN", "f03a-root-green-e2e-001", "passed", 17, "unchanged normal build/query/final audit controls, stub inference"),
        ("FOCUSED_GREEN", "f03a-root-green-focused-001", "passed", 13, "F18 collateral"),
        ("LINEAGE_GREEN", "f03a-root-green-lineage-001", "passed", 8, "lineage collateral"),
        ("MIGRATION_GREEN", "f03a-root-green-migration-001", "passed", 7, "reader migration collateral"),
        ("SECURITY_GREEN", "f03a-root-green-security-001", "passed", 1, "native XLSX security, Python3.9 only"),
    ]
    for sid, run_id, status, methods, classification in run_specs:
        result_path = RUNS / run_id / "result.json"
        record = json.loads(result_path.read_bytes())
        assert record["status"] == status and record["tests_reported"] == methods
        assert record["skipped_reported"] == record["expected_failures_reported"] == 0
        files[sid] = str(result_path.relative_to(ROOT))
        files[sid + "_LOG"] = str(Path(record["log_path"]).relative_to(ROOT))
        required_runs.append({"source_id": sid, "status": status, "methods": methods, "classification": classification})
    # Preserve all fixed executor/preflight/gold records, including failures.
    # Never include this packet itself or mutable progress files.
    used = set(files.values())
    for path in sorted(RUNS.glob("f03a-*")):
        relative = str(path.relative_to(ROOT))
        if path.is_file() and relative not in used and not path.name.startswith(("f03a-audit-", "f03a-graph-artifact", "f03a-validation-result")):
            files["RECORD_" + str(len(files))] = relative
            used.add(relative)
    sources = [{"id": sid, "path": str(ROOT / relative), "sha256": digest(ROOT / relative)} for sid, relative in files.items()]
    assert digest(ROOT / files["CONTRACT"]) == "1a5244d9fd4c537aeaaec88f44d358e24f1b1d56441beaf4ec60fe2a34e07924"
    assert digest(ROOT / files["RESOLVER"]) == "c2b98254ec28a82e8cc7b5ef3f5e780739bcc1b6983ef2a9252609156d4b672f"
    assert digest(ROOT / files["PURE_TEST"]) == "b4a9176f3f7bb723d701a548418be8647a5336dc7b88dd0707d03cfa70eb5b94"
    artifact = {
        "schema_version": "1.0", "task_id": "lms-v1-00-hardening-2026-09-09-f03a",
        "artifact_version": 1, "audit_repair_round": 0, "test_gold_corrections_before_audit": 2,
        "status": "awaiting_separate_audit",
        "task_contract": {"source_ids": ["CONTRACT", "AUTHORIZATION"], "maximum_repair_cycles": 2, "role_separation": "same_model_separate_context", "scope": "same-key unmarked candidate discovery and mixed-group hold only"},
        "nodes": [
            {"id": "N_DISCOVERY", "text": "All eligible same-key peers join a family with at least one marked member; unmarked-only and singleton families stay ungrouped. Existing key, eligibility gate, candidate shape and hash field selection stay unchanged.", "basis": ["CONTRACT", "RESOLVER", "RESOLVER_DELTA", "PURE_TEST", "PURE_RED_FINAL", "PURE_GREEN"]},
            {"id": "N_POLICY", "text": "Mixed marked/unmarked groups are held before automatic marker/year/version rules, without active/historical disposition or active_version edge. Existing all-marked F02a/F04a/numeric behavior remains.", "basis": ["RESOLVER", "RESOLVER_DELTA", "PURE_TEST", "PURE_GREEN", "RESOLVER_GREEN", "YEAR_GREEN"]},
            {"id": "N_HUMAN", "text": "A full-set/source-bound Human selection may choose a marked or unmarked member. Same-key additions and member changes in a still-formed group invalidate prior choices; stale decisions cannot be rescued automatically.", "basis": ["CONTRACT", "RESOLVER", "PURE_TEST", "APP_TEST", "PURE_GREEN", "APP_GREEN"]},
            {"id": "N_APP", "text": "Real in-process synthetic app builds exclude held families, retain unrelated evidence, and publish an explicitly chosen unmarked member. An all-held new build fails before projection and leaves prior CONFIG/index/source bytes unchanged. Existing stub normal query/final-audit and lineage/migration controls pass.", "basis": ["APP_TEST", "E2E_TEST", "READER", "BOOTSTRAP", "PROJECTOR", "ROOT_RED", "APP_GREEN", "E2E_GREEN", "FOCUSED_GREEN", "LINEAGE_GREEN", "MIGRATION_GREEN", "SECURITY_GREEN"]},
            {"id": "N_LIMITS", "text": "This is not whole F03/F05/HITL/freshness acceptance. Cross-key moves/renames/suffixes, all-unmarked identity, dissolved groups, copy-token quirks, independent annual retention and forged-graph reconstruction remain open. Two incorrect test filename assumptions were corrected with old gold/failures preserved and final RED replayed against the exact old resolver; product family normalization was not changed.", "basis": ["CONTRACT", "README", "README_DELTA", "PURE_TEST", "GOLD_NOTE1", "GOLD_NOTE2", "GOLD_DELTA1", "GOLD_DELTA2", "PURE_INITIAL", "PURE_CORRECTION1", "PURE_GREEN_FAILED1", "PURE_GREEN_FAILED2", "PURE_RED_FINAL"]},
        ],
        "edges": [
            {"id": "E_SET_TO_POLICY", "source": "N_DISCOVERY", "target": "N_POLICY", "relation": "provides the complete in-scope candidate set to the mixed-group guard", "basis": ["RESOLVER", "RESOLVER_DELTA", "PURE_TEST"]},
            {"id": "E_SET_TO_HUMAN", "source": "N_DISCOVERY", "target": "N_HUMAN", "relation": "binds the Human choice to all discovered members through the existing candidate-set hash", "basis": ["RESOLVER", "PURE_TEST", "APP_TEST"]},
            {"id": "E_POLICY_TO_READER", "source": "N_POLICY", "target": "N_APP", "relation": "held dispositions are excluded by the existing Reader selection policy", "basis": ["READER", "APP_TEST", "APP_GREEN"]},
            {"id": "E_HUMAN_TO_READER", "source": "N_HUMAN", "target": "N_APP", "relation": "a valid explicit choice changes the selected member to active for a fresh build", "basis": ["RESOLVER", "READER", "APP_TEST", "APP_GREEN"]},
        ],
        "isolated_deltas": [
            ["RESOLVER", "RESOLVER_BEFORE", "RESOLVER_DELTA"],
            ["RESOLVER_TEST", "RESOLVER_TEST_BEFORE", "RESOLVER_TEST_DELTA"],
            ["PURE_CORRECTION1", "PURE_INITIAL", "GOLD_DELTA1"],
            ["PURE_TEST", "PURE_CORRECTION1", "GOLD_DELTA2"],
        ],
        "required_runs": required_runs, "sources": sources,
        "proposed_output": "Independent formal audit JSON plus hashed source-before/after and extra-run evidence. No executor self-approval; parent checks full schema/hash/IDs/deltas/logs. No model/GUI/package/whole-product acceptance, no commit/push.",
    }
    print(json.dumps(artifact, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
