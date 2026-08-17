#!/usr/bin/env python3
"""Deterministically resolve a validated question intent against a Data Catalog.

The resolver never changes the question intent, reads row values, ranks source
candidates, or selects one of several equivalent matches.  It binds explicit
scope and field names by exact or exact-normalized equality and verifies that
the selected catalog entry declares every required execution capability.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_data_catalog import (  # noqa: E402
    CatalogContractError,
    Limits,
    _deterministic_generated_at,
    _schema_validate,
    _sha256_json,
    _stable_id,
    _validator,
    canonical_json_bytes,
    normalize_label,
)
from build_question_understanding import validate_understanding_run  # noqa: E402
from validate_data_catalog import (  # noqa: E402
    _read_entries,
    _read_snapshot,
    validate_data_catalog,
)
from validate_question_clause_ir import (  # noqa: E402
    canonical_json,
    load_json_records,
    validate_question_clause_ir,
)


RESOLVER = "catalog-resolver"
RESOLVER_VERSION = "0.1"
STRUCTURED_OPERATORS = frozenset(
    {
        "filter",
        "project",
        "count",
        "list",
        "compare",
        "calculate",
        "sum",
        "mean",
        "min",
        "max",
        "absolute_distance",
        "argmin_all",
        "argmax_all",
        "sort",
        "deduplicate",
        "group",
        "boolean_test",
    }
)


class ResolutionError(ValueError):
    """Raised when one of the closed resolver inputs is invalid."""


def _pointer_get(value: Any, pointer: str) -> tuple[bool, Any]:
    if not pointer.startswith("/"):
        return False, None
    current = value
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return False, None
            current = current[token]
        elif isinstance(current, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token):
                return False, None
            index = int(token)
            if index >= len(current):
                return False, None
            current = current[index]
        else:
            return False, None
    return True, current


def _clause_refs_for_path(clause_ir: Mapping[str, Any], path: str) -> list[str]:
    return sorted(
        clause["clause_id"]
        for clause in clause_ir["clauses"]
        if path in clause["qic_paths"]
    )


def _match_mode(qic_mode: str) -> str | None:
    if qic_mode == "exact":
        return "exact"
    if qic_mode == "exact_normalized":
        return "exact_normalized"
    return None


def _match_surface(query: str, surface: str, normalized: str, mode: str) -> bool:
    if mode == "exact":
        return query == surface
    return normalize_label(query) == normalized


def _matching_labels(
    entry: Mapping[str, Any], role: str, query: str, mode: str
) -> list[dict[str, Any]]:
    return [
        label
        for label in entry["scope_labels"]
        if label["role"] == role
        and _match_surface(query, label["surface"], label["normalized"], mode)
    ]


def _matching_fields(
    entry: Mapping[str, Any], query: str, mode: str
) -> list[dict[str, Any]]:
    return [
        field
        for field in entry["fields"]
        if _match_surface(query, field["surface"], field["normalized"], mode)
    ]


def _required_field_paths(intent: Mapping[str, Any]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    scope = intent["scope"]
    for index, predicate in enumerate(scope["filters"]):
        result.append((f"/requested/scope/filters/{index}/field", predicate["field"]))
    graph = intent["operation_graph"]
    for index, node in enumerate(graph["nodes"]):
        predicate = node.get("predicate")
        if isinstance(predicate, dict):
            result.append(
                (
                    f"/requested/operation_graph/nodes/{index}/predicate/field",
                    predicate["field"],
                )
            )
        for field_index, field_name in enumerate(node.get("fields") or []):
            result.append(
                (
                    f"/requested/operation_graph/nodes/{index}/fields/{field_index}",
                    field_name,
                )
            )
        if isinstance(node.get("field"), str):
            result.append(
                (f"/requested/operation_graph/nodes/{index}/field", node["field"])
            )
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str]] = []
    for item in result:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def _target_matches(
    entry: Mapping[str, Any], intent: Mapping[str, Any], mode: str
) -> list[tuple[str, str]]:
    target = intent["target"]
    surface = target.get("surface") or target.get("instance")
    if not isinstance(surface, str) or not surface:
        return []
    field_matches = _matching_fields(entry, surface, mode)
    if field_matches:
        return [("field", value["field_id"]) for value in field_matches]
    # A record/document-like target denotes the selected addressable catalog
    # entry itself.  This is a typed fallback, not fuzzy label matching.
    if target.get("canonical_type") in {
        "record",
        "document",
        "row",
        "table",
        "worksheet",
    }:
        return [("catalog_entry", entry["data_catalog_entry_id"])]
    return []


def _capability_checks(
    entry: Mapping[str, Any], intent: Mapping[str, Any], mode: str
) -> list[dict[str, Any]]:
    declared = entry["capabilities"]
    nodes = intent["operation_graph"]["nodes"]
    retrieval = "structured" if any(
        node["operator"] in STRUCTURED_OPERATORS for node in nodes
    ) else "lexical"
    checks: list[dict[str, Any]] = [
        {
            "operation_ref": None,
            "capability_kind": "retrieval_channel",
            "required_capability": retrieval,
            "status": "pass" if retrieval in declared["retrieval_channels"] else "fail",
            "reason_code": (
                "declared_supported"
                if retrieval in declared["retrieval_channels"]
                else "not_declared"
            ),
        }
    ]
    nodes_by_output = {
        node["output_ref"]: node
        for node in nodes
        if isinstance(node.get("output_ref"), str)
    }

    def numeric_field_is_proven(field_name: str) -> bool:
        matches = _matching_fields(entry, field_name, mode)
        return len(matches) == 1 and matches[0]["data_type"] in {"integer", "number"}

    numeric_graph_operators = {
        "calculate",
        "sum",
        "mean",
        "min",
        "max",
        "absolute_distance",
        "argmin_all",
        "argmax_all",
    }
    for node in nodes:
        operator = node["operator"]
        numeric_fields: list[str] = []
        if isinstance(node.get("field"), str):
            numeric_fields.append(node["field"])
        if operator in numeric_graph_operators:
            numeric_fields.extend(node.get("fields") or [])
            for input_ref in node.get("input_refs") or []:
                parent = nodes_by_output.get(input_ref)
                if isinstance(parent, dict):
                    numeric_fields.extend(parent.get("fields") or [])
                    if isinstance(parent.get("field"), str):
                        numeric_fields.append(parent["field"])
        type_safe = (
            operator not in numeric_graph_operators
            or (
                bool(numeric_fields)
                and all(numeric_field_is_proven(name) for name in numeric_fields)
            )
        )
        graph_declared = operator in declared["graph_operators"]
        checks.append(
            {
                "operation_ref": node["operation_id"],
                "capability_kind": "graph_operator",
                "required_capability": operator,
                "status": "pass" if graph_declared and type_safe else "fail",
                "reason_code": (
                    "declared_supported"
                    if graph_declared and type_safe
                    else ("operation_unresolved" if graph_declared else "not_declared")
                ),
            }
        )
        predicate = node.get("predicate")
        if isinstance(predicate, dict):
            predicate_operator = predicate["operator"]
            predicate_declared = predicate_operator in declared["predicate_operators"]
            predicate_type_safe = (
                predicate_operator not in {"gt", "gte", "lt", "lte", "between"}
                or numeric_field_is_proven(predicate["field"])
            )
            checks.append(
                {
                    "operation_ref": node["operation_id"],
                    "capability_kind": "predicate_operator",
                    "required_capability": predicate_operator,
                    "status": (
                        "pass"
                        if predicate_declared and predicate_type_safe
                        else "fail"
                    ),
                    "reason_code": (
                        "declared_supported"
                        if predicate_declared and predicate_type_safe
                        else (
                            "operation_unresolved"
                            if predicate_declared
                            else "not_declared"
                        )
                    ),
                }
            )
    for output in intent["requested_outputs"]:
        mode_value = output["cardinality"]["mode"]
        required = (
            "count"
            if output["return_field"] == "count"
            else ("list" if mode_value in {"multiple", "all", "mixed"} else None)
        )
        if required is None:
            continue
        declared_output = required in declared["graph_operators"]
        checks.append(
            {
                "operation_ref": output["source_operation_ref"],
                "capability_kind": "graph_operator",
                "required_capability": required,
                "status": "pass" if declared_output else "fail",
                "reason_code": "declared_supported" if declared_output else "not_declared",
            }
        )
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for check in checks:
        signature = canonical_json(check)
        if signature not in seen:
            seen.add(signature)
            unique.append(check)
    return unique


def _binding_id(binding: Mapping[str, Any]) -> str:
    core = {key: value for key, value in binding.items() if key != "binding_id"}
    return _stable_id("crb", core)


def _candidate_binding(
    entry: Mapping[str, Any],
    intent: Mapping[str, Any],
    clause_ir: Mapping[str, Any],
    mode: str,
) -> tuple[dict[str, Any] | None, str]:
    scope_bindings: list[dict[str, Any]] = []
    basis: set[str] = {entry["data_catalog_entry_id"]}
    for field_name, role in (("location", "location"), ("container", "container")):
        query = intent["scope"].get(field_name)
        if query is None:
            continue
        path = f"/requested/scope/{field_name}"
        matches = _matching_labels(entry, role, query, mode)
        if not matches:
            return None, "scope_missing"
        if len(matches) != 1:
            return None, "scope_ambiguous"
        label = matches[0]
        scope_bindings.append(
            {
                "qic_path": path,
                "label_ref": label["label_id"],
                "match_mode": mode,
                "status": "matched",
            }
        )
        basis.add(label["label_id"])
        basis.update(_clause_refs_for_path(clause_ir, path))

    target_matches = _target_matches(entry, intent, mode)
    if not target_matches:
        return None, "field_missing"
    if len(target_matches) != 1:
        return None, "field_ambiguous"
    target_kind, target_ref = target_matches[0]
    target_path = "/requested/target/surface"
    target_bindings = [
        {
            "qic_path": target_path,
            "target_kind": target_kind,
            "target_ref": target_ref,
            "match_mode": mode,
            "status": "matched",
        }
    ]
    basis.add(target_ref)
    basis.update(_clause_refs_for_path(clause_ir, target_path))

    field_bindings: list[dict[str, Any]] = []
    for path, field_name in _required_field_paths(intent):
        matches = _matching_fields(entry, field_name, mode)
        if not matches:
            return None, "field_missing"
        if len(matches) != 1:
            return None, "field_ambiguous"
        field = matches[0]
        field_bindings.append(
            {
                "qic_path": path,
                "field_ref": field["field_id"],
                "match_mode": mode,
                "status": "matched",
            }
        )
        basis.add(field["field_id"])
        basis.update(_clause_refs_for_path(clause_ir, path))

    checks = _capability_checks(entry, intent, mode)
    resolved = all(check["status"] == "pass" for check in checks)
    binding: dict[str, Any] = {
        "binding_id": "crb_" + "0" * 32,
        "catalog_entry_ref": entry["data_catalog_entry_id"],
        "target_bindings": target_bindings,
        "scope_bindings": scope_bindings,
        "field_bindings": field_bindings,
        "capability_checks": checks,
        "basis_refs": sorted(basis),
        "status": "resolved" if resolved else "partial",
    }
    binding["binding_id"] = _binding_id(binding)
    return binding, "resolved" if resolved else "capability_unsupported"


def _branch_resolution(
    branch: Mapping[str, Any],
    clause_ir: Mapping[str, Any],
    entries: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], set[str]]:
    intent = branch["candidate_intent"]
    if intent["scope"].get("time_or_version") is not None:
        return {
            "branch_id": branch["branch_id"],
            "candidate_bindings": [],
            "status": "unsupported",
        }, {"capability_unsupported"}
    mode = _match_mode(intent["scope"]["match_mode"])
    if mode is None:
        return {
            "branch_id": branch["branch_id"],
            "candidate_bindings": [],
            "status": "conflict",
        }, {"binding_conflict"}
    bindings: list[dict[str, Any]] = []
    reasons: set[str] = set()
    for entry in entries:
        if not entry["availability"]["searchable"]:
            continue
        binding, reason = _candidate_binding(entry, intent, clause_ir, mode)
        if binding is not None:
            bindings.append(binding)
        else:
            reasons.add(reason)
    bindings.sort(key=lambda item: item["binding_id"])
    if not bindings:
        reason = "field_missing" if "field_missing" in reasons else (
            "scope_ambiguous" if "scope_ambiguous" in reasons else "scope_missing"
        )
        return {
            "branch_id": branch["branch_id"],
            "candidate_bindings": [],
            "status": "ambiguous" if reason.endswith("ambiguous") else "missing",
        }, {reason}
    if len(bindings) > 1:
        return {
            "branch_id": branch["branch_id"],
            "candidate_bindings": bindings,
            "status": "ambiguous",
        }, {"field_ambiguous"}
    binding = bindings[0]
    if binding["status"] != "resolved":
        return {
            "branch_id": branch["branch_id"],
            "candidate_bindings": bindings,
            "status": "unsupported",
        }, {"capability_unsupported"}
    return {
        "branch_id": branch["branch_id"],
        "candidate_bindings": bindings,
        "status": "resolved",
    }, set()


def _resolution_identity(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": record["schema_version"],
        "record_type": record["record_type"],
        "question_understanding_run_id": record["question_understanding_run_id"],
        "question_intent_contract_id": record["question_intent_contract_id"],
        "question_clause_ir_id": record["question_clause_ir_id"],
        "data_catalog_snapshot_id": record["data_catalog_snapshot_id"],
        "branch_resolutions": record["branch_resolutions"],
        "final_status": record["final_status"],
        "reason_codes": record["reason_codes"],
        "errors": record["errors"],
        "resolver": record["provenance"]["resolver"],
        "resolver_version": record["provenance"]["resolver_version"],
        "input_qur_sha256": record["provenance"]["input_qur_sha256"],
        "input_catalog_sha256": record["provenance"]["input_catalog_sha256"],
    }


def deterministic_resolution_id(record: Mapping[str, Any]) -> str:
    return _stable_id("crr", _resolution_identity(record))


def validate_resolution_record_local(record: Any) -> list[str]:
    errors: list[str] = []
    try:
        _schema_validate(_validator("catalog-resolution-run.schema.json"), record, source="resolution")
    except (CatalogContractError, KeyError, TypeError) as exc:
        return [str(exc)]
    if record["catalog_resolution_run_id"] != deterministic_resolution_id(record):
        errors.append("catalog_resolution_run_id is inconsistent")
    seen_bindings: set[str] = set()
    for branch in record["branch_resolutions"]:
        for binding in branch["candidate_bindings"]:
            if binding["binding_id"] != _binding_id(binding):
                errors.append("candidate binding ID is inconsistent")
            if binding["binding_id"] in seen_bindings:
                errors.append("candidate binding ID is duplicated")
            seen_bindings.add(binding["binding_id"])
    return sorted(set(errors))


def resolve_catalog(
    qur: dict[str, Any],
    clause_ir: dict[str, Any],
    entries: Sequence[dict[str, Any]],
    snapshot: dict[str, Any],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Resolve one validated QUR/ClauseIR pair against one catalog snapshot."""

    qur_errors = validate_understanding_run(qur)
    if qur_errors:
        raise ResolutionError("invalid QuestionUnderstandingRun: " + "; ".join(qur_errors[:8]))
    qic = qur["question_intent_contract"]
    clause_errors = validate_question_clause_ir(clause_ir, qic)
    if clause_errors:
        raise ResolutionError("invalid QuestionClauseIR: " + "; ".join(clause_errors[:8]))
    try:
        entry_validator = _validator("data-catalog-entry.schema.json")
        snapshot_validator = _validator("data-catalog-snapshot.schema.json")
        for index, entry in enumerate(entries):
            _schema_validate(entry_validator, entry, source=f"entry:{index}")
        _schema_validate(snapshot_validator, snapshot, source="snapshot")
    except CatalogContractError as exc:
        raise ResolutionError(f"invalid Data Catalog: {exc}") from exc
    entry_ids = [entry["data_catalog_entry_id"] for entry in entries]
    if entry_ids != sorted(entry_ids) or len(entry_ids) != len(set(entry_ids)):
        raise ResolutionError("Data Catalog entries must be uniquely ID-sorted")
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(canonical_json_bytes(entry))
        digest.update(b"\n")
    if snapshot["entry_stream"]["record_count"] != len(entries):
        raise ResolutionError("Data Catalog snapshot record_count mismatch")
    if snapshot["entry_stream"]["sha256"] != digest.hexdigest():
        raise ResolutionError("Data Catalog snapshot stream digest mismatch")
    if qic["question_id"] != clause_ir["question_id"] or qic["original_question"] != clause_ir["original_question"]:
        raise ResolutionError("QUR/QIC and ClauseIR question identity mismatch")

    timestamps = [
        qur["provenance"]["generated_at"],
        clause_ir["provenance"]["generated_at"],
        snapshot["provenance"]["generated_at"],
    ]
    resolved_at = _deterministic_generated_at(timestamps, generated_at)
    reasons: set[str] = set()
    branch_resolutions: list[dict[str, Any]] = []
    intent_ready = (
        qur["final_status"] == "ready_for_retrieval"
        and qur["intent_gate"]["status"] == "pass"
    )
    clause_complete = clause_ir["coverage"]["status"] == "complete"
    if not intent_ready:
        reasons.add("intent_not_ready")
    if not clause_complete:
        reasons.add("clause_coverage_incomplete")
    if intent_ready and clause_complete:
        for branch in qur["candidate_query_paths"]:
            resolution, branch_reasons = _branch_resolution(branch, clause_ir, entries)
            branch_resolutions.append(resolution)
            reasons.update(branch_reasons)

    if not intent_ready or not clause_complete:
        final_status = "clarification_required"
    elif (
        len(branch_resolutions) == 1
        and branch_resolutions[0]["status"] == "resolved"
    ):
        final_status = "resolved"
        reasons.clear()
    elif any(item["status"] == "unsupported" for item in branch_resolutions):
        final_status = "abstained"
    elif any(item["status"] == "conflict" for item in branch_resolutions):
        final_status = "abstained"
    else:
        final_status = "clarification_required"
    if not branch_resolutions and intent_ready and clause_complete:
        reasons.add("catalog_missing")
        final_status = "clarification_required"

    record: dict[str, Any] = {
        "schema_version": "0.1",
        "record_type": "catalog_resolution_run",
        "catalog_resolution_run_id": "crr_" + "0" * 32,
        "question_understanding_run_id": qur["question_understanding_run_id"],
        "question_intent_contract_id": qic["question_intent_contract_id"],
        "question_clause_ir_id": clause_ir["question_clause_ir_id"],
        "data_catalog_snapshot_id": snapshot["data_catalog_snapshot_id"],
        "branch_resolutions": branch_resolutions,
        "final_status": final_status,
        "reason_codes": sorted(reasons),
        "errors": [],
        "provenance": {
            "resolver": RESOLVER,
            "resolver_version": RESOLVER_VERSION,
            "generated_at": resolved_at,
            "deterministic": True,
            "model_used": False,
            "question_independent": False,
            "question_data_used": True,
            "source_data_used": True,
            "answer_data_used": False,
            "past_answers_used": False,
            "input_qur_sha256": _sha256_json(qur),
            "input_catalog_sha256": _sha256_json(snapshot),
        },
    }
    record["catalog_resolution_run_id"] = deterministic_resolution_id(record)
    errors = validate_resolution_record_local(record)
    if errors:
        raise ResolutionError("compiled CatalogResolutionRun is invalid: " + "; ".join(errors[:8]))
    return record


def resolve_catalog_files(
    qur_path: str | Path,
    clause_ir_path: str | Path,
    entries_path: str | Path,
    snapshot_path: str | Path,
    *,
    generated_at: str | None = None,
    limits: Limits = Limits(),
) -> dict[str, Any]:
    qurs = load_json_records(Path(qur_path))
    clauses = load_json_records(Path(clause_ir_path))
    if len(qurs) != 1 or len(clauses) != 1:
        raise ResolutionError("resolver v0.1 accepts exactly one QUR and one ClauseIR")
    catalog_errors = validate_data_catalog(entries_path, snapshot_path, limits=limits)
    if catalog_errors:
        raise ResolutionError("invalid Data Catalog: " + "; ".join(catalog_errors[:8]))
    entries, _ = _read_entries(Path(entries_path), limits)
    snapshot = _read_snapshot(Path(snapshot_path), limits)
    return resolve_catalog(
        qurs[0], clauses[0], entries, snapshot, generated_at=generated_at
    )


def _atomic_write(path: Path, record: Mapping[str, Any]) -> None:
    from build_question_clause_ir import _atomic_write_jsonl

    _atomic_write_jsonl(path, [dict(record)])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qur", required=True, type=Path)
    parser.add_argument("--clause-ir", required=True, type=Path)
    parser.add_argument("--entries", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--generated-at")
    args = parser.parse_args(argv)
    try:
        record = resolve_catalog_files(
            args.qur,
            args.clause_ir,
            args.entries,
            args.snapshot,
            generated_at=args.generated_at,
        )
        _atomic_write(args.output, record)
    except (CatalogContractError, ResolutionError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(canonical_json({"final_status": record["final_status"], "resolution_id": record["catalog_resolution_run_id"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
