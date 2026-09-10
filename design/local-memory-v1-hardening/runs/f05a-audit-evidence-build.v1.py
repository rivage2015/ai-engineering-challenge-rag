"""Independent F05a evidence assembly; reads frozen sources, creates audit files only.

No product imports or edits. In-memory bidirectional delta application and AST
checks are independent of the parent inverse helper. Existing schema/Ruby tools
run locally with 10-second deadlines and capped captured output, no installation.
"""
from __future__ import annotations
import ast
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"
SKILLS = Path("/Users/takashifukutomi/.codex/skills")
TASK = "lms-v1-00-hardening-2026-09-09-f05a"
ARTIFACT = RUNS / "f05a-graph-artifact.v1.json"
ARTIFACT_HASH = "7b7414f3e58a62b6eb34fc71534da11eec51c1dd58c316326ba03e9ab436bf61"
REPORT = RUNS / "f05a-audit-report.v1.json"
EVIDENCE = RUNS / "f05a-audit-evidence.v1.json"
INTEGRITY = RUNS / "f05a-audit-integrity.v1.json"
VALIDATION = RUNS / "f05a-audit-local-validation.v1.json"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def digest(path):
    return sha(path.read_bytes())


def read(path):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            assert key not in value, (path, key)
            value[key] = item
        return value
    return json.loads(path.read_bytes(), object_pairs_hook=unique)


def write_new(path, value):
    assert path.parent == RUNS and path.name.startswith("f05a-audit-")
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    assert len(raw) < 1048576
    with path.open("xb") as handle:
        handle.write(raw)


def apply_delta(source, patch, reverse=False):
    """Apply exact unified hunks to an in-memory string, in either direction."""
    lines = source.splitlines(keepends=True)
    diff = patch.splitlines(keepends=True)
    assert diff[0].startswith("--- ") and diff[1].startswith("+++ ")
    cursor = 0
    result = []
    index = 2
    while index < len(diff):
        match = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n", diff[index])
        assert match, diff[index]
        old_start, old_count, new_start, new_count = (int(x) if x is not None else 1 for x in match.groups())
        index += 1
        old, new = [], []
        while index < len(diff) and not diff[index].startswith("@@ "):
            line = diff[index]
            assert line[0] in " +-", line
            if line[0] in " -":
                old.append(line[1:])
            if line[0] in " +":
                new.append(line[1:])
            index += 1
        assert len(old) == old_count and len(new) == new_count
        start, count, expected, replacement = (new_start, new_count, new, old) if reverse else (old_start, old_count, old, new)
        position = start if count == 0 else start - 1
        assert cursor <= position <= len(lines)
        assert lines[position:position + count] == expected
        result.extend(lines[cursor:position])
        result.extend(replacement)
        cursor = position + count
    result.extend(lines[cursor:])
    return "".join(result)


def check_run(path, status, methods):
    result = read(path)
    started = read(path.with_name("started.json"))
    for key, value in started.items():
        if key != "status":
            assert result[key] == value, (path, key)
    assert started["status"] == "started"
    raw = Path(result["log_path"]).read_bytes()
    assert sha(raw) == result["log_sha256"]
    assert len(raw) == result["log_bytes"] <= 1048576
    assert result["status"] == status and result["tests_reported"] == methods
    assert result["timeout_seconds"] == 30 and result["max_log_bytes"] == 1048576
    assert result["skipped_reported"] == result["expected_failures_reported"] == 0
    footer = re.search(rb"Ran (\d+) tests? in [0-9.]+s\s+(OK|FAILED(?: \([^\r\n]+\))?)\s*\Z", raw)
    assert footer and int(footer[1]) == methods
    assert (footer[2] == b"OK") == (status == "passed") == (result["exit_code"] == 0)
    return {"path": str(path), "sha256": digest(path), "status": status, "methods": methods,
            "log_sha256": result["log_sha256"], "started_sha256": digest(path.with_name("started.json")),
            "footer": footer[2].decode(), "elapsed_seconds": result["elapsed_seconds"]}


def check(check_id, kind, targets, reason, evidence_ids, severity="info"):
    return {"check_id": check_id, "check_type": kind, "target_ids": targets,
            "verdict": "pass", "severity": severity, "reason": reason,
            "evidence_ids": evidence_ids, "required_action": None}


def main():
    assert all(not p.exists() for p in (REPORT, EVIDENCE, INTEGRITY, VALIDATION))
    assert digest(ARTIFACT) == ARTIFACT_HASH
    artifact = read(ARTIFACT)
    assert artifact["task_id"] == TASK and artifact["audit_repair_round"] == 0
    sources = {item["id"]: item for item in artifact["sources"]}
    assert len(sources) == len(artifact["sources"]) == 115
    paths = {key: Path(value["path"]) for key, value in sources.items()}
    expected = {key: item["sha256"] for key, item in sources.items()}
    current = {key: digest(path) for key, path in paths.items()}
    before = read(RUNS / "f05a-audit-sources-before.v1.json")
    after = read(RUNS / "f05a-audit-sources-after.v1.json")
    assert expected == current == before["sources"] == after["sources"]
    assert before["artifact_sha256"] == after["artifact_sha256"] == ARTIFACT_HASH
    assert expected["CONTRACT"] == "89960be4254752ba3ae387610359c3f6f8200cb57c3d7a9190398c723473e4df"
    assert expected["ADDENDUM"] == "5cc85d11170e4068099ab31284807c5e10cfb4e804c37104379333fa688fb4bd"

    deltas = []
    for current_id, before_id, patch_id in artifact["isolated_deltas"]:
        old_raw, new_raw = paths[before_id].read_bytes(), paths[current_id].read_bytes()
        patch = paths[patch_id].read_text()
        assert apply_delta(old_raw.decode(), patch).encode() == new_raw
        assert apply_delta(new_raw.decode(), patch, reverse=True).encode() == old_raw
        deltas.append({"source_id": current_id, "before_id": before_id, "patch_id": patch_id,
                       "before_sha256": sha(old_raw), "after_sha256": sha(new_raw), "forward_and_inverse": "pass"})
    for item in read(paths["ROOT_DELTAS"])["deltas"]:
        path = Path(item["path"])
        new_raw = path.read_bytes()
        patch = item["isolated_unified_diff"]
        old = apply_delta(new_raw.decode(), patch, reverse=True)
        assert sha(old.encode()) == item["before_sha256"]
        assert sha(new_raw) == item["after_sha256"]
        assert apply_delta(old, patch).encode() == new_raw
        deltas.append({"path": str(path), "before_sha256": item["before_sha256"],
                       "after_sha256": item["after_sha256"], "forward_and_inverse": "pass"})
    assert len(deltas) == 6

    def functions(path):
        return {node.name: node for node in ast.parse(path.read_text()).body if isinstance(node, ast.FunctionDef)}
    old_functions, new_functions = functions(paths["EXEC_FILE_14"]), functions(paths["RESOLVER"])
    preserved = sorted(set(old_functions) - {"build", "validate", "main"})
    for name in preserved:
        assert ast.dump(old_functions[name]) == ast.dump(new_functions[name]), name
    old_policy = next(value for node in ast.walk(old_functions["build"]) if isinstance(node, ast.Dict)
                      for key, value in zip(node.keys, node.values) if isinstance(key, ast.Constant) and key.value == "policy")
    policy_return = next(node.value for node in ast.walk(new_functions["validation_policy"]) if isinstance(node, ast.Return))
    assert ast.dump(old_policy) == ast.dump(policy_return)
    class InlinePolicy(ast.NodeTransformer):
        def visit_Call(self, node):
            if isinstance(node.func, ast.Name) and node.func.id == "validation_policy":
                assert not node.args and not node.keywords
                return copy.deepcopy(old_policy)
            return self.generic_visit(node)
    normalized_build = InlinePolicy().visit(copy.deepcopy(new_functions["build"]))
    assert ast.dump(normalized_build) == ast.dump(old_functions["build"])
    def tests(path):
        return {n.name: ast.dump(n) for n in ast.walk(ast.parse(path.read_text()))
                if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")}
    old_gold, new_gold = tests(paths["EXEC_FILE_06"]), tests(paths["PURE_TEST"])
    assert len(old_gold) == 23 and len(new_gold) == 25
    assert all(new_gold[key] == value for key, value in old_gold.items())
    added = sorted(set(new_gold) - set(old_gold))
    assert added == ["test_rejects_overflow_numeric_in_unused_decision_metadata", "test_validate_cli_missing_inputs_returns_structured_failure"]
    assert paths["PURE_TEST"].read_bytes() == paths["EXEC_FILE_15"].read_bytes()

    historical = [check_run(paths[item["source_id"]], item["status"], item["methods"])
                  for item in artifact["required_runs"]]
    assert len(historical) == 16
    method_counts = {"pure": 25, "resolver": 20, "app": 4, "e2e": 17, "unmarked": 4,
                     "unmarked-pure": 19, "year": 11, "holdout": 11}
    independent = [check_run(RUNS / f"f05a-audit-{name}-001/result.json", "passed", count)
                   for name, count in method_counts.items()]
    assert sum(item["methods"] for item in independent) == 111
    write_new(INTEGRITY, {"task_id": TASK, "artifact_sha256": ARTIFACT_HASH,
        "source_count": len(current), "all_source_hashes_unchanged": True, "bidirectional_deltas": deltas,
        "unchanged_function_asts": preserved, "build_ast_after_inlining_identical": True,
        "policy_dictionary_ast_identical": True, "original_gold_methods_unchanged": sorted(old_gold),
        "additive_gold_methods": added, "final_gold_snapshot_exact": True,
        "historical_runs": historical, "independent_runs": independent})

    checks = [
        check("F05A_INTENT", "intent", ["N_LIMITS", "E_GATE_LIMITS"],
              "The contract and addendum authorize only explicit-input standalone reconstruction plus bootstrap's immediate pre-Reader gate. This scoped pass is not full F05, V1, release, Reader/projector post-gate closure, immutable historical decision generations, or current-source attestation. Root must independently validate this report before scope acceptance.",
              ["CONTRACT", "ADDENDUM", "README"], "warning"),
        check("F05A_AUTHORITY", "node", ["N_INPUTS", "E_INPUTS_REBUILD"],
              "Validation authority comes only from caller graph/inventory/optional decision byte snapshots. No explicit or missing optional decision input is empty with no digest; a recorded decision digest requires matching explicit input. Present malformed, denied or directory input fails. Stored source path strings are validated diagnostic metadata, never followed/resolved/statted. Pure25 and independent canary/denied-IO/missing-authority holdouts confirm the boundary.",
              ["CONTRACT", "RESOLVER", "PURE_TEST"]),
        check("F05A_SNAPSHOT", "dependency", ["N_INPUTS", "E_INPUTS_REBUILD"],
              "Source inspection and independent instrumentation confirm one read_bytes call per present explicit input, with both parsing and digest derived from those captured bytes; loaders and secondary hash APIs are forbidden in the tests. A controlled inventory change immediately after its single read passes for the captured matching snapshot and fails on the next validation against changed bytes, correctly distinguishing snapshot equality from freshness or an absence-of-races guarantee.",
              ["RESOLVER", "PURE_TEST", "CONTRACT"]),
        check("F05A_STRICT_JSON", "safety", ["N_INPUTS", "N_REBUILD"],
              "Strict parsing rejects malformed UTF-8/JSON and required shapes, nested duplicate object keys, duplicate inventory paths and decision group IDs before collapse, NaN/Infinity and floating-point overflow. Independent cases also put duplicates/nonfinite values in otherwise unused metadata and duplicated ineligible paths/unused decision IDs. Normal invalid-input/file errors return FAIL while AssertionError guards propagate. Valid U+2028 filenames and LF/CRLF/CR inventories preserve existing text-reader semantics; this is not full inventory-schema or arbitrary resource-limit certification.",
              ["RESOLVER", "PURE_TEST", "CONTRACT"]),
        check("F05A_MEMBERSHIP", "node", ["N_REBUILD", "E_INPUTS_REBUILD"],
              "Expected candidates and complete families are rebuilt from explicit inventory via the unchanged eligibility, family key, at-least-two/one-signal and resolve_group rules, not submitted groups. Empty/partial/extra/duplicate/reassigned output and fabricated activation are rejected after attacker resealing. Independent two-family tests remove an entire resolved family with coherent counts/nodes/edges and still fail; genuine held and numeric-selected families pass beforehand.",
              ["RESOLVER", "PURE_TEST", "EXEC_FILE_11", "EXEC_FILE_13"]),
        check("F05A_FULL_OUTPUT", "calculation", ["N_REBUILD"],
              "Canonical JSON comparison covers complete groups/candidates, nodes, edges, policy, identity and counts; self hash, explicit inventory/decision digests, exact top-level/source fields and metadata types are checked. created_at is only a nonempty string, not a regenerated oracle. Independent list reversal, candidate size/time int-to-float, version int-to-bool and count int-to-float attacks fail. Existing candidate-set hash intentionally excludes size_bytes, but full candidate comparison includes it.",
              ["RESOLVER", "PURE_TEST", "RESOLVER_TEST"]),
        check("F05A_HUMAN_CONTROLS", "edge", ["N_CONTROLS", "E_INPUTS_CONTROLS"],
              "Explicit matching decisions preserve legitimate Human selection of either member and existing stale full-set precedence; missing/wrong/deleted authority and fabricated Human or stale outcomes fail. Pure25, resolver20, unmarked app4 and independent explicit-Human/canary tests pass. This authenticates correspondence to bytes, not the actor, UI review, lease/revision, or human values.",
              ["RESOLVER", "PURE_TEST", "RESOLVER_TEST", "UNMARKED_APP_TEST", "CONTRACT"]),
        check("F05A_READ_ONLY", "safety", ["N_INPUTS", "N_REBUILD"],
              "The validator calls neither build, atomic_json nor record_decision and has no source-document access or write path. Pure tests and independent holdouts forbid those APIs and legacy loaders/hash rereads, check input bytes unchanged, and forbid path open/read_text/stat/lstat/exists/resolve while a valid explicit snapshot succeeds. These scoped guards are not a universal filesystem isolation proof.",
              ["RESOLVER", "PURE_TEST", "GUARD", "CONTRACT"]),
        check("F05A_APPLICATION_GATE", "causality", ["N_GATE", "E_REBUILD_GATE"],
              "Bootstrap's single-line integration supplies the same configured decisions path as its preceding build. Four independently rerun actual bootstrap command-path attacks (empty graph, fabricated active choice, absent-source candidate, changed explicit decisions) reject at the immediate validator before Reader/model/projector/review replacement; prepared prior CONFIG/index/review/source bytes remain unchanged. Historical root RED has four assertion failures at the isolated gate, not downstream-error substitution.",
              ["BOOTSTRAP", "APP_TEST", "ROOT_GOLD", "RUN_06", "LOG_06", "RUN_07", "ROOT_DELTAS"]),
        check("F05A_REGRESSION", "coverage", ["N_CONTROLS", "N_GATE"],
              "Fresh independent required runs are pure25, resolver20, app4, normalE2E17, unmarkedApp4, unmarkedPure19 and year11, all passed without skips/expected failures; the last two contain three explicit residual witnesses. Eleven independent holdouts also pass. Total 111 methods is 108 acceptance/control/regression plus 3 residual witnesses, not 111 accepted behaviors. Normal synthetic query/final-audit and legitimate Human app controls remain successful.",
              ["PURE_TEST", "RESOLVER_TEST", "APP_TEST", "E2E_TEST", "UNMARKED_APP_TEST", "UNMARKED_TEST", "YEAR_TEST"]),
        check("F05A_GOLD_INTEGRITY", "evidence", ["N_REBUILD", "N_CONTROLS"],
              "Exact bidirectional additive diff and independent AST checks retain all original23 pure methods unchanged and add only CLI missing-input and overflowing-float tests; final25 is byte-identical to frozen gold v2. Before-code semantic RED contains 8 methods/26 assertion failures/0 errors, including omitted members and fabricated choice. Pre-audit review full25 retains its 2 overflow assertion failures and 2 CLI FileNotFoundError errors; these are not mislabeled as the original semantic RED. Final25/resolver20 successes are separately retained and rerun.",
              ["EXEC_FILE_06", "EXEC_FILE_11", "EXEC_FILE_13", "EXEC_FILE_15", "EXEC_FILE_18", "EXEC_FILE_20", "EXEC_FILE_22", "EXEC_FILE_33", "EXEC_FILE_35", "EXECUTOR_SUMMARY"]),
        check("F05A_DELTA_SCOPE", "evidence", ["N_REBUILD", "N_GATE", "N_CONTROLS"],
              "Independent in-memory forward/reverse application verifies all six captured deltas against exact before/after bytes or recorded before hashes. Every old resolver function except build/validate/main has identical AST; build becomes AST-identical after inlining the unchanged policy dictionary. Existing resolver and unmarked tests change only 0.1.3 to 0.1.4 assertions, bootstrap adds only explicit --decisions, and README adds the bounded validation explanation. No unrelated semantic policy or gold weakening is indicated by these scoped deltas.",
              ["RESOLVER", "EXEC_FILE_14", "EXEC_FILE_16", "RESOLVER_TEST", "EXEC_FILE_21", "EXEC_FILE_27", "PURE_TEST", "EXEC_FILE_06", "EXEC_FILE_22", "ROOT_DELTAS", "ADDENDUM"]),
        check("F05A_FAILURE_HONESTY", "evidence", ["N_LIMITS", "N_CONTROLS"],
              "All sixteen historical/final result records match their actual terminal footer, status, method count, exit code, started metadata, log digest and budgets. Original semantic RED, review failures and root app RED remain failures. The executor's prematurely successful inverse-check summary v1 and three failed default-strip checks are preserved; v2 explicitly corrects the record after -p0 read-only checks. Independent in-memory inverse checks now confirm the deltas without relying on the premature claim. No independent test attempt failed or was discarded.",
              ["EXECUTOR_SUMMARY_ORIGINAL", "EXECUTOR_SUMMARY", "EXECUTOR_DELTA_CHECKS", "EXECUTOR_MANIFEST", "EXEC_FILE_13", "EXEC_FILE_20", "RUN_06"], "warning"),
        check("F05A_SOURCE_BINDING", "basis", ["N_INPUTS", "N_REBUILD", "N_GATE", "N_CONTROLS", "N_LIMITS"],
              "The immutable artifact hash matches the handoff. All 115 unique source IDs resolve to current bytes with their declared hashes before and after the independent runs, and again at evidence assembly. Node/edge bases refer to actual packet IDs, endpoints exist, and all five nodes/four edges have substantive audit coverage. New audit scripts, holdouts, snapshots, integrity/schema records and eight complete run triples are hash-bound in the companion evidence rather than falsely inserted into the frozen artifact's source namespace.",
              ["CONTRACT", "ADDENDUM", "EXECUTOR_MANIFEST", "PARENT_VALIDATOR", "ROOT_RUNNER_V2"]),
        check("F05A_EDGE_SEMANTICS", "edge", ["E_INPUTS_REBUILD", "E_REBUILD_GATE", "E_INPUTS_CONTROLS", "E_GATE_LIMITS"],
              "The four edges describe code-backed dependencies and a scope limit, not unsupported causality: explicit bytes supply reconstruction; mismatch blocks the immediate gate; explicit matching decision bytes determine existing Human/stale results; passing this gate does not attest later files. Control success and rejection evidence support these exact relations without claiming all downstream consumers invoke reconstruction.",
              ["RESOLVER", "BOOTSTRAP", "PURE_TEST", "APP_TEST", "UNMARKED_APP_TEST", "CONTRACT", "README"]),
        check("F05A_RUN_SAFETY", "safety", ["N_INPUTS", "N_GATE", "N_CONTROLS"],
              "Tests used reviewed F04a/F18 safety dependencies and the local supervisor, one synthetic child per invocation, exclusive new audit run IDs, 30-second deadlines and 1 MiB combined logs. All eight runs exited normally within budget. Pure fixtures enforce 16 records/16 KiB inventory, 8 KiB decisions, 64 KiB graph and 1 MiB cumulative serialization. No network/models/GUI/installation/production documents/CONFIG/index, privilege escalation, commit or push was used; source packet hashes stayed fixed. The guard is not OS isolation or total RAM/disk/process-inventory certification.",
              ["CONTRACT", "GUARD", "DISPATCHER", "SUPERVISOR", "ROOT_RUNNER_V2", "PURE_TEST", "APP_TEST"]),
        check("F05A_RESIDUAL_BOUNDARY", "coverage", ["N_LIMITS", "E_GATE_LIMITS"],
              "Three retained witness methods observe current-marker/year behavior and cross-key/kanji-year-family limitations; their passing assertions are not desired-behavior acceptance. Post-gate graph substitution, downstream complete Reader partitions/projector revalidation, immutable saved decision snapshots, Human authenticity/lease, source freshness, broader format/model/GUI/package work and global races remain outside this pass and require follow-on work. README and contract state these limits consistently.",
              ["YEAR_TEST", "UNMARKED_TEST", "README", "CONTRACT", "EXECUTOR_SUMMARY"], "warning"),
        check("F05A_INDEPENDENCE", "evidence", ["N_LIMITS"],
              "The Auditor used the Codex Graph Engineering Adapter for scoped read-only code/diff/test verification and Agentic Audit for contract/artifact/source-based checks and the exact report schema. Executor private reasoning was not used. Fresh independent holdouts and a bounded supplemental static reviewer add checking, but same-model separate context is procedural separation, not independent factual authority. Ruby/schema/hash consistency cannot establish the semantic truth of this judgment; root validation remains a separate required step.",
              ["CONTRACT", "PARENT_VALIDATOR"], "warning"),
    ]
    node_ids = {item["id"] for item in artifact["nodes"]}
    edge_ids = {item["id"] for item in artifact["edges"]}
    assert len(node_ids) == 5 and len(edge_ids) == 4
    assert set().union(*(set(c["target_ids"]) for c in checks)) == node_ids | edge_ids
    assert all(set(c["evidence_ids"]) <= set(sources) for c in checks)
    scope = [ARTIFACT, *[paths[key] for key in ("CONTRACT", "ADDENDUM", "RESOLVER", "PURE_TEST", "RESOLVER_TEST", "BOOTSTRAP", "APP_TEST", "README", "PARENT_VALIDATOR")],
             SKILLS / "codex-graph-engineering-adapter/SKILL.md",
             SKILLS / "graph-engineering-agentic-audit/SKILL.md",
             SKILLS / "graph-engineering-agentic-audit/references/agentic-audit.md",
             SKILLS / "graph-engineering-agentic-audit/assets/agentic-audit-report.schema.json",
             Path(__file__).resolve(), RUNS / "f05a-audit-run.v1.py", RUNS / "f05a-audit-holdouts.v1.py",
             RUNS / "f05a-audit-sources-before.v1.json", RUNS / "f05a-audit-sources-after.v1.json",
             INTEGRITY, VALIDATION, EVIDENCE, *[Path(item["path"]) for item in independent]]
    report = {"schema_version": "1.0", "audit_id": "f05a-independent-audit-v1-round0",
        "task_id": TASK, "artifact_hash": ARTIFACT_HASH,
        "auditor_context": {"independence_level": "same_model_separate_context", "source_scope": [str(path) for path in scope]},
        "status": "pass", "checks": checks,
        "summary": {"blocking_failures": 0, "unresolved_count": 0, "next_action": "deliver"}}
    write_new(REPORT, report)

    schema = SKILLS / "graph-engineering-agentic-audit/assets/agentic-audit-report.schema.json"
    ruby = SKILLS / "graph-engineering-agentic-audit/scripts/validate_audit_report.rb"
    strict_code = "import json,sys; from jsonschema import Draft202012Validator; s=json.load(open(sys.argv[1])); r=json.load(open(sys.argv[2])); Draft202012Validator.check_schema(s); Draft202012Validator(s).validate(r); print('VALID: strict Draft202012 report schema')"
    commands = [["/usr/bin/ruby", str(ruby), "--artifact", str(ARTIFACT), "--report", str(REPORT)],
                [str(ROOT / "rag/.venv/bin/python"), "-I", "-B", "-c", strict_code, str(schema), str(REPORT)]]
    validations = []
    for command in commands:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=10)
        assert len(result.stdout) + len(result.stderr) <= 1048576
        validations.append({"command": command, "exit_code": result.returncode,
                            "stdout": result.stdout.decode(), "stderr": result.stderr.decode()})
    write_new(VALIDATION, {"task_id": TASK, "artifact_sha256": ARTIFACT_HASH,
                          "report_sha256": digest(REPORT), "schema_sha256": digest(schema),
                          "ruby_validator_sha256": digest(ruby), "checks": validations})
    assert all(item["exit_code"] == 0 for item in validations), validations
    assert {key: digest(path) for key, path in paths.items()} == expected
    auxiliary = []
    for path in sorted(RUNS.glob("f05a-audit-*")):
        if path == EVIDENCE:
            continue
        if path.is_dir():
            children = sorted(path.iterdir())
            assert {child.name for child in children} == {"started.json", "result.json", "unittest.log"}
        else:
            children = [path]
        for child in children:
            assert child.is_file() and child.is_relative_to(RUNS)
            auxiliary.append({"path": str(child), "sha256": digest(child)})
    residuals = ["ResidualWitnessTests.test_cross_key_moves_renames_and_suffix_changes_leave_old_group_omitted",
                 "ResidualWitnessTests.test_year_kanji_parent_is_not_silently_normalized_more_broadly",
                 "YearOnlySupersessionTests.test_current_marker_year_residual_is_observable"]
    evidence = {"task_id": TASK, "artifact_sha256": ARTIFACT_HASH,
        "created_at_utc": datetime.now(timezone.utc).isoformat(), "audit_repair_round": 0,
        "sources_before": before["sources"], "sources_after": after["sources"],
        "additional_artifacts": auxiliary, "runs": independent, "total_methods": 111,
        "acceptance_control_regression_methods": 108, "residual_witness_methods": 3,
        "required_methods": 100, "additional_independent_holdout_methods": 11,
        "residual_test_names": residuals, "independent_failed_test_attempts": [],
        "historical_result_log_records_checked": 16, "scoped_bidirectional_deltas_checked": 6,
        "independence_level": "same_model_separate_context",
        "limits": {"wall_seconds_per_test_worker": 30, "combined_log_bytes_per_test_worker": 1048576,
                   "inventory_records_per_pure_fixture": 16, "inventory_bytes": 16384,
                   "decision_bytes": 8192, "graph_bytes": 65536, "cumulative_pure_serialization_bytes_per_process": 1048576,
                   "source_fixtures": "synthetic owned temporary roots only; app fixtures <=16 KiB per case",
                   "write_scope": "new f05a-audit-* records only, plus synthetic temporary fixtures",
                   "guard_scope": "reviewed Python guards and supervisor; not OS isolation, source freshness or total RAM/disk guarantee",
                   "excluded": ["network", "models", "GUI", "installation", "real source documents", "production CONFIG/index", "extra permissions", "commit", "push"]},
        "semantic_scope": "Standalone explicit-input resolver reconstruction and immediate bootstrap gate only. No whole F05/V1/release or post-gate/immutable-generation/Human-authenticity/freshness/global-race acceptance.",
        "mechanical_validation_limit": "Local Ruby and strict Draft202012 passed; this does not replace parent consistency checks or prove semantic judgment."}
    write_new(EVIDENCE, evidence)
    print(json.dumps({"report": {"path": str(REPORT), "sha256": digest(REPORT)},
                      "evidence": {"path": str(EVIDENCE), "sha256": digest(EVIDENCE)},
                      "total_methods": 111, "acceptance_control_regression": 108, "residual_witnesses": 3,
                      "additional_artifacts": len(auxiliary), "status": "scoped_audit_pass_parent_validation_required"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
