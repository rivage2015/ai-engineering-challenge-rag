"""Bounded read-only F11a packet generation; stdout only, not audit approval.

No product/test imports, discovery, network, model, subprocess, or file writes.
All selected paths are explicit below or finite expansions of literal lists.
Read limits: 250 files, 2 MiB/file, 24 MiB total, 30 s checked between reads;
output <= 2 MiB. These are generator guards, not OS/RSS guarantees. The caller
saves returned artifact/source_packet through apply_patch in NEW versioned files.
Historical manifests supply provenance only, never current path discovery or
current authority. Current eleven product pins are independent literals here.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
R = "design/local-memory-v1-hardening/runs/"
TASK = "lms-v1-f11a-notebook-metadata-2026-09-09"
MAX_FILES, MAX_FILE, MAX_TOTAL, MAX_OUTPUT = 250, 2097152, 25165824, 2097152
START = time.monotonic()

PRODUCTS = (
    ("PROBE", "probe", "scripts/probe_intermediate_records.py", "1320cda390ab671dc2df82678ed2237e12aa4828b29666e3fb3d5b8a734f456d"),
    ("MANAGED", "managed", "scripts/build_intermediate_records.py", "8d9dd31a7acba7d6b8f3e5a841b86565ea3e6e85d932fa08f77e3f3641ae59dd"),
    ("INATIVE", "intermediate-native", "scripts/validate_intermediate_records.py", "91573a6216a1cf1814c36452e36752c08af46ef6dd393020830f7d82df87ae31"),
    ("ISTREAM", "intermediate-stream", "scripts/validate_intermediate_records_streaming.py", "e7f5a53dc9822f8cccd1f0fa60c3c6873605575b4297e4c888b350dbfe56928a"),
    ("SBUILD", "search-builder", "scripts/build_search_units.py", "55f0284acd6462260ba4962fa77246fb78090aed9b42554146e49723728c20df"),
    ("SNATIVE", "search-native", "scripts/validate_search_units.py", "c1bb29855c8b390c4af8334e9641af0ad069279cc7ea86faf25fddd85c7f500b"),
    ("SSTREAM", "search-stream", "scripts/validate_search_units_streaming.py", "54851bb4500a8eb82a9dada65c5bec7cbcdce212fd70852f430b3f703ac24059"),
    ("ESCHEMA", "evidence-schema", "schemas/evidence.schema.json", "030fea7251578e6a1c57934b595a457294ab7281c38e93779a097d3dca0a4899"),
    ("SSCHEMA", "search-schema", "schemas/search-unit.schema.json", "b0bc29b9e9252bd35ac553ca428e679b8e664d8a353ff6d40d56e90c4375e776"),
    ("AVALIDATOR", "adaptive-validator", "distribution/macos-local-memory/engine/validate_adaptive_semantic_graph.py", "c2587b685d06a1e8d1ec006bb2577be47168a0c14b072a22b3a90878c30563e3"),
    ("INDEX", "index", "distribution/macos-local-memory/engine/build_local_semantic_index.py", "c70f36d98012cca29877af72e4d345c30a555e1fef24e34ebc057b4f823e7229"),
)
IMAGE_IDS = {"PROBE", "INATIVE", "ISTREAM", "SBUILD", "SNATIVE", "SSTREAM"}
FIXED = {
    "CONTRACT": R + "f11a-task-contract.v1.md",
    "ADDENDUM": R + "f11a-task-contract-addendum.v1.md",
    "CONTRACT_GATE": R + "f11a-contract-gate.v1.md",
    "IMPLEMENTATION_GATE": R + "f11a-implementation-gate.v1.md",
    "REGRESSION_GATE": R + "f11a-regression-repair-gate.v1.md",
    "IMAGE_GATE": R + "f11a-image-repair-gate.v1.md",
    "IMAGE_DESIGN": R + "f11a-image-repair-design.v1.md",
    "ROOT_RED_ASSESSMENT": R + "f11a-root-red-assessment.v1.md",
    "EXECUTOR_RED_ASSESSMENT": R + "f11a-executor-red-assessment.v1.md",
    "APP_DIAGNOSIS": R + "f11a-app-regression-diagnosis.v1.md",
    "IMAGE_DIAGNOSIS": R + "f11a-image-regression-diagnosis.v1.md",
    "INITIAL_BEFORE_MANIFEST": R + "f11a-executor-before-manifest.v1.json",
    "INITIAL_MANIFEST": R + "f11a-executor-manifest.v1.json",
    "INITIAL_COHERENT": R + "f11a-executor-coherent.v1.json",
    "INITIAL_SUMMARY": R + "f11a-executor-summary.v1.md",
    "INITIAL_FREEZE": R + "f11a-executor-freeze-result.v1.json",
    "INDEX_PACKET": R + "f11a-index-regression-coherent.v1.json",
    "INDEX_SUMMARY": R + "f11a-index-regression-repair.v1.md",
    "IMAGE_PACKET": R + "f11a-image-repair-coherent.v1.json",
    "IMAGE_SUMMARY": R + "f11a-image-repair-summary.v1.md",
    "IMAGE_MECHANICAL": R + "f11a-image-repair-mechanical-check.v1.json",
    "IMAGE_RUN_SNAPSHOT": R + "f11a-image-repair-coherent-before-runs.v1.json",
    "METADATA_TEST": "tests/test_notebook_metadata_binding.py",
    "METADATA_GOLD1": R + "f11a-executor-gold-test.v1.py",
    "METADATA_GOLD2": R + "f11a-executor-gold-test.v2.py",
    "METADATA_GOLD_DELTA": R + "f11a-executor-gold-additive.v2.diff",
    "METADATA_METHODS": R + "f11a-executor-gold-methods.v2.json",
    "ROOT_BOUNDARY_TEST": "tests/test_notebook_metadata_boundaries.py",
    "APP_TEST": "tests/test_notebook_metadata_application.py",
    "APP_GOLD_ORIGINAL": R + "f11a-root-app-gold.v1.py",
    "APP_GOLD_CORRECTION": R + "f11a-root-app-gold-correction.v1.md",
    "INDEX_TEST": R + "f11a-app-regression-gold.v1.py",
    "IMAGE_TEST": R + "f11a-image-regression-gold.v1.py",
    "IMAGE_ADDITIONAL_TEST": R + "f11a-image-additional-gold.v1.py",
    "LEGACY_IMAGE_TEST": "tests/test_local_embedded_visual_pipeline.py",
    "INITIAL_RUNNER": R + "f11a-executor-run.v1.py",
    "POST_RUNNER": R + "f11a-executor-run.v2.py",
    "FINAL_METADATA_RUNNER": R + "f11a-final-metadata-run.v1.py",
    "ROOT_BOUNDARY_RUNNER": R + "f11a-root-run.v1.py",
    "APP_RUNNER": R + "f11a-root-app-run.v1.py",
    "REGRESSION_RUNNER": R + "f11a-regression-run.v1.py",
    "LEGACY_IMAGE_RUNNER": R + "f11a-root-image-run.v1.py",
    "ADDITIONAL_RUNNER": R + "f11a-image-additional-run.v1.py",
    "GUARD": R + "f04a-executor-run.v1.py",
    "FIXTURE_BUDGET": R + "f05b-fixture-budget.v1.py",
    "SUPERVISOR": "scripts/run_local_memory_hardening_tests.py",
    "HARNESS": "distribution/macos-local-memory/tests/test_versioned_safe_index_e2e.py",
    "BOOTSTRAP": "distribution/macos-local-memory/app/bootstrap.py",
    "READER": "distribution/macos-local-memory/engine/build_adaptive_semantic_graph.py",
    "ADAPTER": "scripts/adapt_layer1_to_local_memory.py",
    "CHUNKING": "scripts/evidence_text_chunking.py",
    "INTEGRITY": "scripts/intermediate_build_integrity.py",
    "LEXICAL": "scripts/lexical_search_common.py",
    "OCR": "scripts/local_image_ocr.py",
    "VLM": "scripts/local_visual_observation.py",
    "DOCUMENT_SCHEMA": "schemas/document.schema.json",
    "RELATION_SCHEMA": "schemas/relation.schema.json",
    "PACKAGE_BUILD_READ_ONLY": "distribution/macos-local-memory/build/build_package.sh",
    "GENERATOR": R + "f11a-final-artifact-generator.v1.py",
}
PINS = {
    "CONTRACT": "20dfd742490462e101ce952e661c6d23a5a40ce0b89b3cf921204b973fc66d15",
    "ADDENDUM": "2542f29b2776127a5d919639c69505d31b4efdf46968d9ff80a0af7d930f6f8f",
    "IMAGE_GATE": "0b9e6252a549c29989f6209e5a2258f3a5edbfe5c10098fe807f919bca475d4b",
    "IMAGE_DESIGN": "b63e7fdc3161aed6cb61466770ecfe720018f8872df429a357dbb9817b9afc48",
    "METADATA_TEST": "ab2256c31b04bc8e8ec9a3c5c294d1e1d98071e38e9969c62bee3718c95bc41e",
    "METADATA_GOLD1": "0b80382980a130cc52a58108ed5d89df5f0bc4249af39a2432d650078a966daa",
    "METADATA_GOLD2": "ab2256c31b04bc8e8ec9a3c5c294d1e1d98071e38e9969c62bee3718c95bc41e",
    "APP_TEST": "b254860584650f376400a50d3890b9cc6074633427a565787b91aae11965496a",
    "INDEX_TEST": "55dc6665d9fbfc9bd6721898ea7be73606560f8a7d0e2fc61e9a05839c222296",
    "IMAGE_TEST": "75573e3ba7ea4a464b20feaf09b6a9e4d4a9133ceabe519399785f9638046913",
    "LEGACY_IMAGE_TEST": "892658aa919cad1dd35a7fdc19aba9c8b0ef1b931e83f3137fc6112b5a150508",
}
# source_id, directory, expected methods, historical/current classification.
RUNS = (
    ("RED_INITIAL", "f11a-executor-red-initial-001", 9, "historical_true_semantic_RED"),
    ("RED_ROOT", "f11a-root-red-001", 4, "historical_true_semantic_RED"),
    ("OLD_INITIAL", "f11a-executor-green-initial-001", 9, "historical_before_image_correction"),
    ("OLD_POST", "f11a-executor-post-green-001", 21, "historical_before_image_correction"),
    ("OLD_RESIDUAL", "f11a-executor-residual-green-001", 3, "historical_residual_witness_not_defense"),
    ("OLD_ROOT", "f11a-root-green-001", 4, "historical_before_corrections"),
    ("OLD_APP_ERROR", "f11a-root-app-001", 4, "historical_setup_regression_not_defense"),
    ("OLD_IMAGE_ERROR", "f11a-root-images-001", 3, "historical_setup_regression_not_defense"),
    ("RED_INDEX", "f11a-regression-app-001", 9, "historical_true_semantic_RED"),
    ("RED_IMAGE", "f11a-regression-image-001", 3, "historical_baseline_mixed_failure_negatives_not_reached"),
    ("GREEN_INDEX", "f11a-regression-app-002", 9, "current_index_only_pure_check"),
    ("OLD_APP_GREEN", "f11a-root-app-002", 4, "historical_after_index_before_image_correction"),
    ("GREEN_IMAGE", "f11a-regression-image-002", 3, "current_image_semantic_controls"),
    ("GREEN_LEGACY_IMAGE", "f11a-root-images-002", 3, "current_legacy_mocked_image_controls"),
    ("FINAL_INITIAL", "f11a-final-initial-001", 9, "current_metadata_acceptance_candidate"),
    ("FINAL_POST", "f11a-final-post-001", 21, "current_metadata_acceptance_candidate"),
    ("FINAL_RESIDUAL", "f11a-final-residual-001", 3, "current_residual_witness_not_defense"),
    ("FINAL_APP", "f11a-root-app-003", 4, "current_app_gate_migration_rebuild_static_package_controls"),
    ("ADDITIONAL_IMAGE", "f11a-image-additional-001", 6, "current_additional_visual_boundary_controls"),
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def strict_json(raw):
    def object_pairs(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, "duplicate JSON key")
            value[key] = item
        return value
    def constant(value):
        raise ValueError("nonfinite JSON constant: " + value)
    return json.loads(raw, object_pairs_hook=object_pairs, parse_constant=constant)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def patch_text(text, patch, inverse=False):
    source, lines, output = text.splitlines(keepends=True), patch.splitlines(keepends=True), []
    cursor, index = 0, 2
    while index < len(lines):
        match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", lines[index])
        require(match is not None, "malformed isolated patch")
        start = int(match.group(2 if inverse else 1)) - 1
        require(start >= cursor, "overlapping patch hunks")
        output.extend(source[cursor:start])
        cursor, index = start, index + 1
        while index < len(lines) and not lines[index].startswith("@@ "):
            line = lines[index]
            index += 1
            prefix, body = line[0], line[1:]
            if inverse:
                prefix = {"+": "-", "-": "+"}.get(prefix, prefix)
            require(prefix in {" ", "+", "-"}, "unexpected patch line")
            if prefix in {" ", "-"}:
                require(cursor < len(source) and source[cursor] == body, "patch input differs")
                cursor += 1
            if prefix in {" ", "+"}:
                output.append(body)
    return "".join(output + source[cursor:])


def literal_assignment(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError("missing literal assignment: " + name)


def main():
    specs = dict(FIXED)
    roles = {key: "selected_dependency_or_evidence" for key in specs}
    deltas = []
    for source_id, short, path, digest in PRODUCTS:
        specs[source_id], roles[source_id], PINS[source_id] = path, "current_product", digest
        if source_id == "INDEX":
            specs["INDEX_BEFORE"] = R + "f11a-app-regression-before-index.v1.py"
            specs["INDEX_AFTER"] = R + "f11a-index-regression-after.v1.py"
            specs["INDEX_DELTA"] = R + "f11a-index-regression-delta.v1.patch"
            deltas.append(("INDEX", "INDEX_BEFORE", "INDEX_DELTA", "integration_correction1_index"))
            continue
        extension = "json" if source_id in {"ESCHEMA", "SSCHEMA"} else "py"
        specs[source_id + "_ORIGINAL"] = R + "f11a-executor-before-" + short + ".v1." + extension
        specs[source_id + "_INITIAL_DELTA"] = R + "f11a-executor-" + short + "-delta.v1.diff"
        initial_after = source_id
        if source_id in IMAGE_IDS:
            initial_after = source_id + "_IMAGE_BEFORE"
            for stage in ("before", "after"):
                specs[source_id + "_IMAGE_" + stage.upper()] = R + "f11a-image-repair-" + stage + "-" + short + ".v1.py"
            specs[source_id + "_IMAGE_DELTA"] = R + "f11a-image-repair-" + short + "-delta.v1.diff"
            deltas.append((source_id, initial_after, source_id + "_IMAGE_DELTA", "integration_correction1_image"))
        deltas.append((initial_after, source_id + "_ORIGINAL", source_id + "_INITIAL_DELTA", "initial_implementation_historical_target"))
    deltas.append(("METADATA_GOLD2", "METADATA_GOLD1", "METADATA_GOLD_DELTA", "preimplementation_additive_gold"))
    for run_id, directory, _, _ in RUNS:
        for suffix, filename in (("RESULT", "result.json"), ("LOG", "unittest.log"), ("START", "started.json")):
            specs[run_id + "_" + suffix] = R + directory + "/" + filename
            roles[run_id + "_" + suffix] = "run_receipt_or_log"
    require(len(specs) <= MAX_FILES and len(set(specs.values())) == len(specs), "source count/duplicate path")
    raw, sources, trees, total = {}, [], {}, 0
    for source_id, relative in specs.items():
        require(time.monotonic() - START < 30, "generator time bound")
        parts = PurePosixPath(relative)
        require(not parts.is_absolute() and ".." not in parts.parts, "path escapes scope")
        path = ROOT / relative
        require(not path.is_symlink() and path.resolve(strict=True).is_relative_to(ROOT), "source path escape/symlink")
        with path.open("rb") as handle:
            value = handle.read(MAX_FILE + 1)
        total += len(value)
        require(len(value) <= MAX_FILE and total <= MAX_TOTAL, "generator byte bound")
        digest = hashlib.sha256(value).hexdigest()
        require(source_id not in PINS or digest == PINS[source_id], "frozen source drift: " + source_id)
        raw[source_id] = value
        if path.suffix == ".py":
            trees[source_id] = ast.parse(value.decode("utf-8"), filename=relative)
        elif path.suffix == ".json":
            strict_json(value)
        sources.append({"id": source_id, "path": str(path), "relative_path": relative,
                        "sha256": digest, "bytes": len(value),
                        "role": roles.get(source_id, "historical_snapshot_or_delta")})
    source_by_id = {item["id"]: item for item in sources}
    for source_id, short, _, _ in PRODUCTS:
        if source_id in IMAGE_IDS:
            require(raw[source_id] == raw[source_id + "_IMAGE_AFTER"], "current/image after mismatch")
    require(raw["INDEX"] == raw["INDEX_AFTER"], "current index after mismatch")
    initial_before = strict_json(raw["INITIAL_BEFORE_MANIFEST"])
    by_path = {item["relative_path"]: item for item in sources}
    for item in initial_before["sources"]:
        require(item["before_path"] in by_path, "unselected historical before")
        require(by_path[item["before_path"]]["sha256"] == item["before_sha256"], "historical before hash mismatch")
    delta_checks = []
    for target, before, patch, phase in deltas:
        a, b, p = (raw[key].decode("utf-8") for key in (before, target, patch))
        require(patch_text(a, p) == b and patch_text(b, p, True) == a, "delta forward/inverse mismatch: " + patch)
        delta_checks.append({"target_id": target, "before_id": before, "delta_id": patch,
                             "phase": phase, "forward_exact": True, "inverse_exact": True})
    require(raw["METADATA_TEST"] == raw["METADATA_GOLD2"], "permanent gold changed")
    # Drop only the predeclared additive class; compare the entire remaining AST.
    current_tree = trees["METADATA_TEST"]
    retained = ast.Module(body=[n for n in current_tree.body if not (
        isinstance(n, ast.ClassDef) and n.name == "NotebookMetadataPostAPITests")], type_ignores=[])
    require(ast.dump(retained, include_attributes=False) == ast.dump(trees["METADATA_GOLD1"], include_attributes=False), "initial complete gold AST changed")
    selections = {
        "initial": literal_assignment(trees["INITIAL_RUNNER"], "METHODS"),
        **literal_assignment(trees["POST_RUNNER"], "METHODS"),
        "app": literal_assignment(trees["APP_RUNNER"], "METHODS"),
        "index": literal_assignment(trees["INDEX_TEST"], "METHOD_NAMES"),
        "image": literal_assignment(trees["IMAGE_TEST"], "METHODS"),
        "legacy_image": literal_assignment(trees["LEGACY_IMAGE_RUNNER"], "METHODS"),
        "additional_image": literal_assignment(trees["IMAGE_ADDITIONAL_TEST"], "METHODS"),
    }
    require([len(selections[k]) for k in ("initial", "post", "residual", "app", "index", "image", "legacy_image", "additional_image")] == [9,21,3,4,9,3,3,6], "selected method counts changed")
    require(not set(selections["initial"]) & set(selections["post"]) and not set(selections["post"]) & set(selections["residual"]), "acceptance/residual overlap")
    run_checks = []
    footer = re.compile(r"^Ran (\d+) tests? in [0-9.]+s\r?\n\r?\n(OK|FAILED)(?: \(([^\r\n]*)\))?\r?\n?\Z", re.MULTILINE)
    for run_id, directory, count, classification in RUNS:
        result, started = strict_json(raw[run_id + "_RESULT"]), strict_json(raw[run_id + "_START"])
        log = raw[run_id + "_LOG"]
        text = log.decode("utf-8")
        terminal = footer.search(text)
        require(terminal is not None and int(terminal.group(1)) == count == result["tests_reported"], "run/footer count mismatch")
        require(result["log_sha256"] == hashlib.sha256(log).hexdigest() and result["log_bytes"] == len(log), "log integrity mismatch")
        require(result["log_path"] == str(ROOT / R / directory / "unittest.log"), "result log path mismatch")
        require(result["command"] == started["command"] and result["started_at"] == started["started_at"], "start/result mismatch")
        require(result["timeout_seconds"] == 30 and result["max_log_bytes"] == 1048576, "unexpected run bounds")
        if classification.startswith("current_"):
            require(result["status"] == "passed" and result["exit_code"] == 0 and terminal.group(2) == "OK", "current check did not pass")
            require(result["skipped_reported"] == 0 and result["expected_failures_reported"] == 0, "skip/expected failure is not current PASS")
        methods = re.findall(r"^(test_[A-Za-z0-9_]+) \(", text, re.MULTILINE)
        require(len(methods) == count, "method log does not match footer")
        run_checks.append({"id": run_id, "run_directory": R + directory, "classification": classification,
                           "basis": [run_id + "_START", run_id + "_RESULT", run_id + "_LOG"],
                           "status": result["status"], "tests": count, "terminal_outcome": terminal.group(2),
                           "terminal_details": terminal.group(3), "methods": methods,
                           "elapsed_seconds": result["elapsed_seconds"], "log_sha256": result["log_sha256"],
                           "limits": {"seconds": 30, "log_bytes": 1048576},
                           "fixture_footer": next((l for l in text.splitlines() if l.startswith("F05b explicit fixture budget:")), None)})
    expected_run_selection = {"FINAL_INITIAL":"initial", "FINAL_POST":"post", "FINAL_RESIDUAL":"residual", "FINAL_APP":"app", "GREEN_INDEX":"index", "GREEN_IMAGE":"image", "GREEN_LEGACY_IMAGE":"legacy_image", "ADDITIONAL_IMAGE":"additional_image"}
    for run in run_checks:
        if run["id"] in expected_run_selection:
            require(run["methods"] == list(selections[expected_run_selection[run["id"]]]), "run selected methods differ from frozen literals")
    ast_index = []
    selected_python = {p[0] for p in PRODUCTS} | {"METADATA_TEST", "APP_TEST", "IMAGE_TEST", "IMAGE_ADDITIONAL_TEST", "INDEX_TEST"}
    names = {"read_notebook_bytes", "parse_notebook_bytes", "validate_notebook_state", "classify_notebook_evidence", "notebook_evidence_state", "notebook_document_binding", "notebook_binding_report", "_visual_origin_errors", "_validate_notebook_visual_text", "notebook_parent_lookup", "notebook_search_unit_contract_errors", "consume", "add_direct_text", "validate", "validate_report", "main", "_is_explicit_verified_structural", "_validate_attested_smartart_connection", "derive_native_structural_relations", "process_file"}
    for source_id in selected_python & trees.keys():
        for node in ast.walk(trees[source_id]):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (node.name in names or node.name.startswith("test_")):
                ast_index.append({"source_id": source_id, "name": node.name, "line": node.lineno,
                                  "end_line": node.end_lineno, "ast_sha256": hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()})
    ast_index.sort(key=lambda item: (item["source_id"], item["line"]))
    artifact = graph(sources)
    known = set(specs) | {n["id"] for n in artifact["nodes"]} | {e["id"] for e in artifact["edges"]}
    for item in artifact["nodes"] + artifact["edges"]:
        require(bool(item["basis"]) and set(item["basis"]) <= set(specs), "invalid node/edge basis")
    for edge in artifact["edges"]:
        require(edge["source"] in known and edge["target"] in known, "invalid edge endpoint")
    for path in artifact["required_paths"]:
        require(set(path["node_ids"] + path["edge_ids"] + path["basis"]) <= known, "invalid required path ref")
    packet = {"schema_version":"1.0", "task_id":TASK, "status":"draft_pending_root_verification_not_audit_dispatch",
              "source_policy":"explicit selected paths; historical manifests do not define current sources",
              "files":sources, "runs":run_checks, "isolated_deltas":delta_checks, "method_selections":selections,
              "current_product_ids":[p[0] for p in PRODUCTS], "ast_index":ast_index,
              "mechanical_checks":{"pins_verified":len(PINS), "files":len(sources), "read_bytes":total,
                                   "all_delta_forward_inverse_exact":True, "initial_gold_complete_ast_unchanged":True,
                                   "permanent_gold2_byte_exact":True, "run_hash_footer_method_checks":True},
              "limits":{"files":MAX_FILES,"bytes_per_file":MAX_FILE,"total_read_bytes":MAX_TOTAL,"seconds":30,"output_bytes":MAX_OUTPUT},
              "not_claimed":["formal audit PASS", "all dependencies transitively discovered", "new product/test execution", "broader collateral not listed", "whole V1/F11 completion"]}
    output = encoded({"artifact": artifact, "source_packet": packet})
    require(len(output) <= MAX_OUTPUT, "generator output bound")
    sys.stdout.buffer.write(output)


def graph(sources):
    def node(identifier, text, basis):
        return {"id":identifier, "text":text, "status":"proposed_for_independent_audit", "basis":basis}
    nodes = [
        node("N_GOAL", "F11a certifies only the declared textual Notebook cell/source/saved-output metadata slice. Body correspondence, complete membership, execution/freshness and F11b downstream presentation remain excluded and open.", ["CONTRACT","ADDENDUM","IMAGE_DESIGN"]),
        node("N_PARSE", "Bounded original-byte snapshot supplies both Document digest/length and parsed metadata; preparse byte/token/depth/number limits and strict malformed-JSON/count handling do not execute Notebook code. Missing counts are not fabricated as null.", ["PROBE","MANAGED","METADATA_TEST","FINAL_INITIAL_LOG","FINAL_POST_LOG"]),
        node("N_BIND", "Native/schema-stream/structural-stream intermediate validation checks canonical role/location/ordinal/state against same-read original metadata. Rootless, limits, incomplete and zero-checked states remain explicitly unverified; known malformed metadata fails.", ["PROBE","INATIVE","ISTREAM","ESCHEMA","METADATA_TEST","FINAL_POST_LOG"]),
        node("N_COPY", "Direct Search units copy exact metadata, and both Search validators bind it to referenced Evidence including same-ID alteration and omission. Search alone is not original-byte or body/membership attestation.", ["SBUILD","SNATIVE","SSTREAM","SSCHEMA","FINAL_INITIAL_LOG","FINAL_POST_LOG","FINAL_RESIDUAL_LOG"]),
        node("N_REPORT_GATE", "PASS/UNVERIFIED/FAIL and counts remain distinct. Actual adaptive run_tool observes intermediate exit2 before Search/adapter; tested failed generation leaves prior CONFIG and generation bytes intact. Existing counts wrapper returns only on PASS.", ["INATIVE","ISTREAM","READER","BOOTSTRAP","APP_TEST","FINAL_APP_LOG","FINAL_POST_LOG"]),
        node("N_VISUAL", "Only producer-shaped provisional visual children with real same-document image parent and strict origin/location contracts are excluded from Notebook metadata counts. Deferred image lookup, unlocated transcript, attachment/dataURI and missing/cross-document parent checks have bounded synthetic controls; visual text stays provisional.", ["PROBE","SBUILD","INATIVE","ISTREAM","SNATIVE","SSTREAM","IMAGE_DESIGN","IMAGE_TEST","IMAGE_ADDITIONAL_TEST","GREEN_IMAGE_LOG","ADDITIONAL_IMAGE_LOG"]),
        node("N_IDENTITY", "The managed0.12.0 identity is recognized at adaptive and projector native/SmartArt pins without removing old versions or loosening support/source comparisons. The original app3 setup errors and independent index semantic RED are retained; current index9 and final app4 pass.", ["MANAGED","AVALIDATOR","INDEX","INDEX_DELTA","INDEX_TEST","RED_INDEX_LOG","GREEN_INDEX_LOG","FINAL_APP_LOG"]),
        node("N_COMPAT", "Synthetic migration/rebuild protects prior generations; static package copy/fingerprint checks cover already shipped Probe/schema paths. Checkout stream parity is not proof the missing packaged streaming Search validator or a built app is accepted.", ["APP_TEST","FINAL_APP_LOG","PACKAGE_BUILD_READ_ONLY","BOOTSTRAP","CONTRACT"]),
        node("N_EVIDENCE", "Current metadata30, app4, image3, legacy-image3 and additional-image6 are distinct functional batches; index9 is a current-index-only pure batch. Historical baselines/RED/errors and all deltas/gold remain identifiable, not relabeled as current. Formal audit is still pending.", ["METADATA_METHODS","FINAL_INITIAL_LOG","FINAL_POST_LOG","FINAL_APP_LOG","GREEN_IMAGE_LOG","GREEN_LEGACY_IMAGE_LOG","ADDITIONAL_IMAGE_LOG","GREEN_INDEX_LOG","INITIAL_MANIFEST","INDEX_PACKET","IMAGE_PACKET"]),
        node("N_RESIDUAL", "Three passing residual witnesses show coherent body replacement, nonzero record removal and Search-only coherent metadata forgery remain possible under the declared slice. They are not defended attacks or three extra acceptance tests; F11b/body/membership work remains mandatory.", ["CONTRACT","METADATA_TEST","FINAL_RESIDUAL_LOG","IMAGE_DESIGN"]),
        node("N_PENDING", "This draft awaits root source/gold/receipt verification, broader coverage selection and separate-context formal audit. Mechanical JSON/AST/hash checks do not establish semantic correctness or self-approval.", ["CONTRACT","IMPLEMENTATION_GATE","REGRESSION_GATE","IMAGE_GATE","GENERATOR"]),
    ]
    edges = [
        {"id":"E_PARSE_BIND","source":"N_PARSE","target":"N_BIND","relation":"provides same-read source facts and digest authority","basis":["PROBE","MANAGED","INATIVE","ISTREAM"]},
        {"id":"E_BIND_COPY","source":"N_BIND","target":"N_COPY","relation":"original attestation requires this intermediate generation before Evidence-only Search validation","basis":["CONTRACT","READER","INATIVE","ISTREAM","SNATIVE","SSTREAM"]},
        {"id":"E_BIND_GATE","source":"N_BIND","target":"N_REPORT_GATE","relation":"unverified metadata yields exit2 and stops subsequent stages","basis":["ISTREAM","READER","APP_TEST","FINAL_APP_LOG"]},
        {"id":"E_VISUAL_BIND","source":"N_VISUAL","target":"N_BIND","relation":"separates valid provisional visual children from saved textual metadata counts without a label-only exemption","basis":["PROBE","IMAGE_TEST","IMAGE_ADDITIONAL_TEST","GREEN_IMAGE_LOG","ADDITIONAL_IMAGE_LOG"]},
        {"id":"E_IDENTITY_GATE","source":"N_IDENTITY","target":"N_REPORT_GATE","relation":"restores exact current-version structural projection so real app gates are reached","basis":["INDEX","AVALIDATOR","INDEX_TEST","FINAL_APP_LOG"]},
        {"id":"E_GATE_COMPAT","source":"N_REPORT_GATE","target":"N_COMPAT","relation":"failed or incompatible new processing preserves existing publication","basis":["BOOTSTRAP","APP_TEST","FINAL_APP_LOG"]},
        {"id":"E_EVIDENCE_RESIDUAL","source":"N_EVIDENCE","target":"N_RESIDUAL","relation":"separates tested limitations from defended obligations","basis":["METADATA_TEST","METADATA_METHODS","FINAL_RESIDUAL_LOG"]},
        {"id":"E_SCOPE_PENDING","source":"N_GOAL","target":"N_PENDING","relation":"limits proposed acceptance and reserves approval to orchestrator/auditor","basis":["CONTRACT","REGRESSION_GATE","IMAGE_GATE"]},
    ]
    required = [
        ("G1_G4_SOURCE", ["N_PARSE","N_BIND"], ["E_PARSE_BIND"], ["CONTRACT","METADATA_TEST","FINAL_INITIAL_LOG","FINAL_POST_LOG"]),
        ("G2_G3_BIND_COPY", ["N_BIND","N_COPY"], ["E_BIND_COPY"], ["METADATA_TEST","FINAL_POST_LOG"]),
        ("G5_REPORT_GATE", ["N_BIND","N_REPORT_GATE"], ["E_BIND_GATE"], ["APP_TEST","FINAL_APP_LOG"]),
        ("G6_COMPAT", ["N_IDENTITY","N_COMPAT"], ["E_IDENTITY_GATE","E_GATE_COMPAT"], ["INDEX_TEST","GREEN_INDEX_LOG","APP_TEST","FINAL_APP_LOG"]),
        ("IMAGE_CORRECTION", ["N_VISUAL","N_BIND"], ["E_VISUAL_BIND"], ["IMAGE_GATE","IMAGE_TEST","IMAGE_ADDITIONAL_TEST","GREEN_IMAGE_LOG","ADDITIONAL_IMAGE_LOG"]),
        ("PROVENANCE_AND_SCOPE", ["N_EVIDENCE","N_RESIDUAL","N_GOAL","N_PENDING"], ["E_EVIDENCE_RESIDUAL","E_SCOPE_PENDING"], ["INITIAL_MANIFEST","INDEX_PACKET","IMAGE_PACKET","GENERATOR"]),
    ]
    return {"schema_version":"1.0","task_id":TASK,"artifact_version":1,
            "status":"draft_pending_root_verification_before_formal_audit","audit_repair_round":0,"integration_correction_round":1,
            "task_contract":{"source_ids":["CONTRACT","ADDENDUM","CONTRACT_GATE","IMPLEMENTATION_GATE","REGRESSION_GATE","IMAGE_GATE","IMAGE_DESIGN"],"maximum_repair_cycles":2,"role_separation":"same_model_separate_context"},
            "goal":"Independently review the closed F11a metadata-only implementation and its integration correction, retaining every declared residual and required follow-on.",
            "nodes":nodes,"edges":edges,"sources":sources,
            "required_paths":[{"id":key,"node_ids":ns,"edge_ids":es,"basis":bs,"status":"submitted_for_review_not_self_approved"} for key,ns,es,bs in required],
            "scope_limits":["metadata-only; no raw body/pointer or complete membership attestation","no freshness/execution history inference","visual lineage shape is not source-image membership or model correctness","F11b app/shard/index/retrieval/answer propagation remains mandatory open","all-format/real-model/GUI/package/RSS guarantees excluded","broader collateral not listed in this draft awaits root review"],
            "proposed_output":{"action":"root_verify_then_dispatch_separate_audit","acceptance_requested":"F11a declared slice only, contingent on formal audit and parent deterministic checks","self_approval":False,"whole_F11_or_V1_complete":False}}


if __name__ == "__main__":
    main()
