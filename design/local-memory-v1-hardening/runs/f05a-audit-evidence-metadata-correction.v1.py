"""Preserve unpublished audit v1 and fix one evidence-only residual class label.

This creates v2 records exclusively. No test, product, v1 audit, contract or
artifact bytes are changed; no test outcome or scoped audit judgment changes.
"""
from __future__ import annotations
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"
SKILLS = Path("/Users/takashifukutomi/.codex/skills")
ARTIFACT = RUNS / "f05a-graph-artifact.v1.json"
REPORT = RUNS / "f05a-audit-report.v2.json"
EVIDENCE = RUNS / "f05a-audit-evidence.v2.json"
VALIDATION = RUNS / "f05a-audit-local-validation.v2.json"
CORRECTION = RUNS / "f05a-audit-metadata-correction.v1.json"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_bytes())


def write_new(path, value):
    assert path.parent == RUNS and path.name.startswith("f05a-audit-")
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    assert len(raw) < 1048576
    with path.open("xb") as handle:
        handle.write(raw)


def main():
    original_report = RUNS / "f05a-audit-report.v1.json"
    original_evidence = RUNS / "f05a-audit-evidence.v1.json"
    assert digest(original_report) == "abcba38370198842719be602b1a0bfb84d365c85a49a48b6e9ba608a71343152"
    assert digest(original_evidence) == "a75243f82defeeacbe516aa4c6ff10f76543d54030bf8fa98616d7f21d3c3980"
    artifact, report, evidence = map(read, (ARTIFACT, original_report, original_evidence))
    assert digest(ARTIFACT) == report["artifact_hash"] == evidence["artifact_sha256"] == "7b7414f3e58a62b6eb34fc71534da11eec51c1dd58c316326ba03e9ab436bf61"
    expected = {item["id"]: item["sha256"] for item in artifact["sources"]}
    source_paths = {item["id"]: Path(item["path"]) for item in artifact["sources"]}
    assert len(expected) == 115
    assert {key: digest(path) for key, path in source_paths.items()} == expected == evidence["sources_before"] == evidence["sources_after"]
    for item in evidence["additional_artifacts"]:
        assert digest(Path(item["path"])) == item["sha256"]
    residuals = []
    for key, class_name in (("UNMARKED_TEST", "ResidualWitnessTests"), ("YEAR_TEST", "CurrentMarkerResidualWitnessTests")):
        classes = [node for node in ast.parse(source_paths[key].read_text()).body if isinstance(node, ast.ClassDef) and node.name == class_name]
        assert len(classes) == 1
        residuals.extend(class_name + "." + node.name for node in classes[0].body if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"))
    assert len(residuals) == 3
    assert residuals[:2] == evidence["residual_test_names"][:2]
    assert residuals[2] == "CurrentMarkerResidualWitnessTests.test_current_marker_year_residual_is_observable"
    original_label = evidence["residual_test_names"][2]
    assert original_label == "YearOnlySupersessionTests.test_current_marker_year_residual_is_observable"
    correction = {"task_id": evidence["task_id"], "artifact_sha256": evidence["artifact_sha256"],
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "type": "auditor_evidence_metadata_correction_before_final_handoff",
        "original_report": {"path": str(original_report), "sha256": digest(original_report)},
        "original_evidence": {"path": str(original_evidence), "sha256": digest(original_evidence)},
        "field": "residual_test_names[2]", "before": original_label, "after": residuals[2],
        "reason": "Final self-review compared source class declarations and detected an incorrect class qualifier. The method identity and 1 year residual count were already correct. The original v1 files and builder are retained unchanged, and v2 evidence derives all residual names from frozen test AST classes.",
        "unchanged": ["artifact and all 115 source hashes", "all eight independent test runs", "111 total / 108 acceptance-control-regression / 3 residual methods", "all scoped check verdicts", "formal repair round 0"],
        "product_or_test_edits": False}
    write_new(CORRECTION, correction)
    report["audit_id"] = "f05a-independent-audit-v2-round0"
    replacements = {str(RUNS / "f05a-audit-evidence.v1.json"): str(EVIDENCE),
                    str(RUNS / "f05a-audit-local-validation.v1.json"): str(VALIDATION)}
    report["auditor_context"]["source_scope"] = [replacements.get(path, path) for path in report["auditor_context"]["source_scope"]]
    report["auditor_context"]["source_scope"].extend([str(original_report), str(original_evidence), str(CORRECTION), str(Path(__file__).resolve())])
    honesty = next(item for item in report["checks"] if item["check_id"] == "F05A_FAILURE_HONESTY")
    honesty["reason"] += " Auditor v1 had an evidence-only year-residual class-label typo detected before final handoff; original v1 files are retained unchanged and the correction is explicit in audit metadata v1 and report/evidence v2. No result, count or scoped verdict changed."
    write_new(REPORT, report)
    schema = SKILLS / "graph-engineering-agentic-audit/assets/agentic-audit-report.schema.json"
    ruby = SKILLS / "graph-engineering-agentic-audit/scripts/validate_audit_report.rb"
    strict_code = "import json,sys; from jsonschema import Draft202012Validator; s=json.load(open(sys.argv[1])); r=json.load(open(sys.argv[2])); Draft202012Validator.check_schema(s); Draft202012Validator(s).validate(r); print('VALID: strict Draft202012 report schema')"
    commands = [["/usr/bin/ruby", str(ruby), "--artifact", str(ARTIFACT), "--report", str(REPORT)],
                [str(ROOT / "rag/.venv/bin/python"), "-I", "-B", "-c", strict_code, str(schema), str(REPORT)]]
    validations = []
    for command in commands:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=10)
        assert len(result.stdout) + len(result.stderr) < 1048576
        validations.append({"command": command, "exit_code": result.returncode, "stdout": result.stdout.decode(), "stderr": result.stderr.decode()})
    write_new(VALIDATION, {"task_id": evidence["task_id"], "artifact_sha256": evidence["artifact_sha256"],
                          "report_sha256": digest(REPORT), "schema_sha256": digest(schema),
                          "ruby_validator_sha256": digest(ruby), "checks": validations})
    assert all(item["exit_code"] == 0 for item in validations)
    assert {key: digest(path) for key, path in source_paths.items()} == expected
    auxiliary = []
    for path in sorted(RUNS.glob("f05a-audit-*")):
        if path == EVIDENCE:
            continue
        children = sorted(path.iterdir()) if path.is_dir() else [path]
        for child in children:
            assert child.is_file() and child.is_relative_to(RUNS)
            auxiliary.append({"path": str(child), "sha256": digest(child)})
    evidence["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    evidence["additional_artifacts"] = auxiliary
    evidence["residual_test_names"] = residuals
    evidence["audit_metadata_revision"] = 1
    evidence["supersedes"] = {"path": str(original_evidence), "sha256": digest(original_evidence)}
    evidence["metadata_correction"] = {"path": str(CORRECTION), "sha256": digest(CORRECTION)}
    write_new(EVIDENCE, evidence)
    print(json.dumps({"report": {"path": str(REPORT), "sha256": digest(REPORT)},
                      "evidence": {"path": str(EVIDENCE), "sha256": digest(EVIDENCE)},
                      "additional_artifacts": len(auxiliary), "total_methods": evidence["total_methods"],
                      "status": "scoped_audit_pass_parent_validation_required"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
