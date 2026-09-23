#!/usr/bin/env python3
"""Validate local intermediate JSONL records and their Evidence boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from probe_intermediate_records import (
    is_notebook_document, normalize_text, notebook_binding_report,
    notebook_document_binding,
)


REPOSITORY = Path(__file__).resolve().parents[1]
SCHEMA_PATHS = {
    "document": REPOSITORY / "schemas" / "document.schema.json",
    "evidence": REPOSITORY / "schemas" / "evidence.schema.json",
    "relation": REPOSITORY / "schemas" / "relation.schema.json",
}
PATTERNS = {
    "document": re.compile(r"^doc_[0-9a-f]{16,64}$"),
    "evidence": re.compile(r"^ev_[0-9a-f]{16,64}$"),
    "relation": re.compile(r"^rel_[0-9a-f]{16,64}$"),
}
REQUIRED = {
    "document": {"schema_version", "record_type", "document_id", "source", "extraction"},
    "evidence": {"schema_version", "record_type", "evidence_id", "document_id", "evidence_type", "location", "content", "provenance"},
    "relation": {"schema_version", "record_type", "relation_id", "relation_class", "relation_type", "from_ref", "to_ref", "provenance", "status"},
}
ALLOWED = {
    "document": {
        "schema_version", "record_type", "document_id", "source", "classification",
        "extraction",
    },
    "evidence": {
        "schema_version", "record_type", "evidence_id", "document_id", "evidence_type",
        "location", "content", "style", "geometry", "parent_evidence_id", "ordinal",
        "native_properties", "annotations", "provenance",
    },
    "relation": {
        "schema_version", "record_type", "relation_id", "relation_class", "relation_type",
        "from_ref", "to_ref", "properties", "supporting_evidence_ids", "provenance",
        "status",
    },
}
QUERY_LAYER_RESERVED_KEYS = {
    "question_understanding_run_id",
    "question_intent_contract_id",
    "question_intent_contract",
    "query_context_graph",
    "candidate_query_paths",
    "intent_gate",
    "retrieval_plans",
    "retrieval_runs",
    "retrieval_hits",
    "retrieved_evidence_bundles",
    "candidate_evaluations",
    "primary_query_path",
    "proof_obligation",
    "answerability_gate",
    "answer_plan",
    "output_validation",
    "query_run_id",
    "final_answer",
    "forbidden_check_results",
}
QUESTION_TRACE_KEYS = frozenset({"question_id", "original_question"})
QUERY_LAYER_STRUCTURAL_KEYS = QUERY_LAYER_RESERVED_KEYS - {
    "question_understanding_run_id",
    "question_intent_contract_id",
    "query_run_id",
    "final_answer",
}
QUERY_LAYER_KEY_COMBINATIONS = (
    frozenset({"question_understanding_run_id", "stage_statuses"}),
    frozenset(
        {
            "question_understanding_run_id",
            "question_intent_contract",
            "query_context_graph",
            "candidate_query_paths",
            "intent_gate",
        }
    ),
    frozenset(
        {
            "strategy",
            "source_ambiguity_refs",
            "logical_branch_limit",
            "excluded_combinations",
        }
    ),
    frozenset(
        {
            "branch_id",
            "selected_candidates",
            "intent_diffs",
            "candidate_intent",
            "status",
        }
    ),
    frozenset(
        {
            "intent_diff_id",
            "ambiguity_ref",
            "candidate_ref",
            "field_path",
            "before",
            "after",
        }
    ),
    frozenset({"question_intent_contract_id", "requested"}),
    frozenset({"requested", "not_requested", "forbidden", "ambiguity"}),
    frozenset({"query_run_id", "final_answer"}),
    frozenset({"query_run_id", "stage_statuses"}),
    frozenset({"question_id", "final_answer"}),
    frozenset({"question_intent_contract_id", "final_answer"}),
    frozenset({"stage_statuses", "final_status", "runtime_metadata"}),
    frozenset({"retrieval_run_id", "branch_id", "plan", "status"}),
    frozenset(
        {"branch_id", "channel", "rank", "search_unit_id", "source_evidence_ids"}
    ),
    frozenset({"query_branch_id", "evidence_nodes", "evidence_edges"}),
    frozenset(
        {
            "branch_id",
            "disqualifiers",
            "signals",
            "evidence_ids",
            "equivalence_class_id",
        }
    ),
    frozenset(
        {"branch_id", "equivalent_branch_ids", "evidence_ids", "required_qualifiers"}
    ),
    frozenset({"operation_graph_ref", "requirements", "coverage", "overall"}),
    frozenset({"rule_id", "stage", "validator_id", "subject_refs", "action_taken"}),
    frozenset({"output_plans", "allowed_claims", "forbidden_rule_ids"}),
    frozenset({"branch_id", "candidate_intent", "assumptions"}),
)
SOURCE_VALUE_KEYS = {
    "raw_text",
    "raw_value",
    "normalized_text",
    "normalized_value",
    "content_ref",
}
FORBIDDEN_SOURCE_NAMES = {
    "questions_valid.csv",
    "questions_test.csv",
    "predictions.csv",
    "submission.zip",
}
RFC3339_DATETIME = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?"
    r"(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
MAX_JSON_DEPTH = 64
TEXT_FIRST_READING_POLICY = "text_first_v1"
VISUAL_LOCATION_INTEGER_FIELDS = frozenset({
    "page_number", "slide_number", "paragraph_index", "table_index",
    "row_index", "column_index", "object_index", "image_object_index",
    "series_index", "notebook_cell_index", "code_line_start", "code_line_end",
})
VISUAL_LOCATION_STRING_FIELDS = frozenset({
    "sheet_name", "cell", "range", "section", "shape_id", "object_id",
    "source_member", "locator_text",
})


def visual_coverage_shape_errors(document: dict[str, Any], label: str) -> list[str]:
    """Validate opt-in reading coverage even without the JSON Schema runtime."""
    extraction = document.get("extraction")
    if not isinstance(extraction, dict):
        return []  # Existing Document validation owns the base extraction shape.
    has_policy = "reading_policy" in extraction
    has_coverage = "visual_coverage" in extraction
    if not has_policy and not has_coverage:
        return []
    errors: list[str] = []
    prefix = f"{label}: visual_coverage"
    if not has_policy or extraction.get("reading_policy") != TEXT_FIRST_READING_POLICY:
        errors.append(f"{prefix}: reading_policy is missing or unknown")
    coverage = extraction.get("visual_coverage")
    if not has_coverage or not isinstance(coverage, dict):
        return [*errors, f"{prefix}: coverage object is required"]
    if set(coverage) != {"status", "pending"}:
        errors.append(f"{prefix}: coverage fields are invalid")
    status = coverage.get("status")
    pending = coverage.get("pending")
    if status not in ("pending", "none_pending"):
        errors.append(f"{prefix}: status is invalid")
    if not isinstance(pending, list):
        return [*errors, f"{prefix}: pending must be an array"]
    if (status == "pending" and not pending) or (status == "none_pending" and pending):
        errors.append(f"{prefix}: status differs from pending inventory")
    if pending and extraction.get("status") not in ("partial", "failed"):
        errors.append(f"{prefix}: unread visuals require partial or failed extraction")
    seen: set[str] = set()
    for index, item in enumerate(pending):
        item_label = f"{prefix}.pending[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{item_label}: must be an object")
            continue
        kind = item.get("kind")
        fields = {"evidence_id", "source_sha256", "location", "reason", "kind"}
        if kind in ("embedded_image", "standalone_image"):
            fields.add("image_sha256")
        elif kind != "pdf_page":
            errors.append(f"{item_label}: kind is invalid")
        if set(item) != fields:
            errors.append(f"{item_label}: fields are invalid")
        if item.get("reason") != "deferred_by_reading_policy":
            errors.append(f"{item_label}: reason is invalid")
        evidence_id = item.get("evidence_id")
        if not isinstance(evidence_id, str) or not PATTERNS["evidence"].fullmatch(evidence_id):
            errors.append(f"{item_label}: evidence_id is invalid")
        elif evidence_id in seen:
            errors.append(f"{item_label}: duplicate pending evidence_id")
        else:
            seen.add(evidence_id)
        for key in ("source_sha256", "image_sha256"):
            if key == "image_sha256" and kind == "pdf_page":
                continue
            value = item.get(key)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                errors.append(f"{item_label}: {key} is invalid")
        location = item.get("location")
        if not isinstance(location, dict):
            errors.append(f"{item_label}: location must be an object")
            continue
        if set(location) - (VISUAL_LOCATION_INTEGER_FIELDS | VISUAL_LOCATION_STRING_FIELDS):
            errors.append(f"{item_label}: location fields are invalid")
        for key, value in location.items():
            if key in VISUAL_LOCATION_INTEGER_FIELDS and (
                isinstance(value, bool) or not isinstance(value, int) or value < 1
            ):
                errors.append(f"{item_label}: location.{key} is invalid")
            if key in VISUAL_LOCATION_STRING_FIELDS and (
                not isinstance(value, str) or not value
                or (key == "cell" and not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]*", value))
            ):
                errors.append(f"{item_label}: location.{key} is invalid")
    return errors


def visual_coverage_binding_errors(
    document: dict[str, Any], evidence: Iterable[dict[str, Any]], label: str,
) -> list[str]:
    """Bind every pending placement to this original document and its Evidence.

    Pending locations are inventory, not answer Evidence or a statement that an
    image was read. Complete inventory is checked so omitting a pending item
    cannot silently promote a text-first document to fully read.
    """
    extraction = document.get("extraction")
    if not isinstance(extraction, dict) or "reading_policy" not in extraction:
        return []
    if visual_coverage_shape_errors(document, label):
        return []  # Report malformed shapes once, before reference validation.
    source = document.get("source", {})
    document_id = document.get("document_id")
    is_pdf = source.get("extension") == "pdf"
    visuals = {
        item.get("evidence_id"): item for item in evidence
        if item.get("evidence_type") == "image"
        or (is_pdf and item.get("evidence_type") == "page")
    }
    pending = extraction["visual_coverage"]["pending"]
    errors: list[str] = []
    prefix = f"{label}: visual_coverage"
    pending_ids = {item["evidence_id"] for item in pending}
    if pending_ids != set(visuals):
        errors.append(f"{prefix}: pending inventory differs from visual Evidence")
    for item in pending:
        item_label = f"{prefix}:{item['evidence_id']}"
        parent = visuals.get(item["evidence_id"])
        if parent is None:
            errors.append(f"{item_label}: pending Evidence is missing or not a visual")
            continue
        if parent.get("document_id") != document_id:
            errors.append(f"{item_label}: pending Evidence belongs to another document")
        if item["source_sha256"] != source.get("sha256"):
            errors.append(f"{item_label}: source hash mismatch")
        if canonical_json(item["location"]) != canonical_json(parent.get("location")):
            errors.append(f"{item_label}: location mismatch")
        native = parent.get("native_properties")
        native = native if isinstance(native, dict) else {}
        kind = item["kind"]
        if kind == "pdf_page":
            if not is_pdf or parent.get("evidence_type") != "page":
                errors.append(f"{item_label}: pdf_page must reference a PDF page")
        elif parent.get("evidence_type") != "image":
            errors.append(f"{item_label}: image kind must reference image Evidence")
        elif kind == "embedded_image":
            if item["image_sha256"] != native.get("embedded_sha256"):
                errors.append(f"{item_label}: embedded image hash mismatch")
        elif item["image_sha256"] != source.get("sha256"):
            errors.append(f"{item_label}: standalone image hash mismatch")
    return errors


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}_{digest_value(value)[:32]}"


def _reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_non_finite_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number is forbidden: {value}")
    return parsed


def _check_json_depth(value: object, max_depth: int = MAX_JSON_DEPTH) -> None:
    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        if depth > max_depth:
            raise ValueError(
                f"JSON nesting exceeds the configured depth {max_depth}"
            )
        if isinstance(current, dict):
            pending.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            pending.extend((child, depth + 1) for child in current)


def strict_json_loads(value: str) -> object:
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_object_keys,
            parse_constant=_reject_non_finite_constant,
            parse_float=_parse_finite_float,
        )
        _check_json_depth(parsed)
        return parsed
    except (RecursionError, OverflowError) as exc:
        raise ValueError(
            f"JSON parser resource limit exceeded: {type(exc).__name__}"
        ) from exc


def read_jsonl(path: Path) -> list[object]:
    records: list[object] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                records.append(strict_json_loads(line))
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
    return records


def _load_schema(path: Path) -> dict[str, Any]:
    try:
        value = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"cannot load published schema {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"published schema root must be an object: {path}")
    return value


@lru_cache(maxsize=3)
def _published_schema_validator(kind: str) -> Any:
    try:
        import jsonschema
    except ImportError as exc:
        raise ValueError(
            "jsonschema is required for intermediate Draft 2020-12 validation"
        ) from exc
    path = SCHEMA_PATHS.get(kind)
    if path is None:
        raise ValueError(f"unknown intermediate record kind: {kind!r}")
    schema = _load_schema(path)
    jsonschema.Draft202012Validator.check_schema(schema)
    format_checker = jsonschema.FormatChecker()

    @format_checker.checks("date-time", raises=ValueError)
    def _is_rfc3339_datetime(value: object) -> bool:
        if not isinstance(value, str) or RFC3339_DATETIME.fullmatch(value) is None:
            return False
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True

    return jsonschema.Draft202012Validator(
        schema,
        format_checker=format_checker,
    )


def published_schema_validators() -> dict[str, Any]:
    return {kind: _published_schema_validator(kind) for kind in SCHEMA_PATHS}


def schema_record_errors(
    kind: str,
    record: object,
    label: str,
    validator: Any | None = None,
) -> list[str]:
    compiled = validator if validator is not None else _published_schema_validator(kind)
    result: list[str] = []
    for error in sorted(
        compiled.iter_errors(record),
        key=lambda item: (
            tuple(str(component) for component in item.absolute_path),
            item.message,
        ),
    ):
        location = ".".join(str(part) for part in error.absolute_path) or "root"
        result.append(f"{label}: schema {location}: {error.message}")
    return result


def content_hash_payload(item: dict[str, Any]) -> dict[str, Any]:
    for key in ("raw_text", "raw_value", "content_ref"):
        if key in item:
            return {key: item[key]}
    raise ValueError("content has none of raw_text/raw_value/content_ref")


def _query_signature_paths(value: dict[str, Any], path: str) -> list[str]:
    keys = set(value)
    matches = [
        f"{path}.{key}"
        for key in sorted(keys & QUERY_LAYER_STRUCTURAL_KEYS)
    ]
    if value.get("record_type") == "question_intent_contract" and (
        "question_intent_contract_id" in keys or "requested" in keys
    ):
        matches.append(f"{path}<question_intent_contract>")
    if value.get("record_type") == "query_run" and (
        "query_run_id" in keys
        or "question_intent_contract" in keys
        or "stage_statuses" in keys
    ):
        matches.append(f"{path}<query_run>")
    if value.get("record_type") == "question_understanding_run" and (
        "question_understanding_run_id" in keys
        or "question_intent_contract" in keys
        or "query_context_graph" in keys
        or "stage_statuses" in keys
    ):
        matches.append(f"{path}<question_understanding_run>")
    for signature in QUERY_LAYER_KEY_COMBINATIONS:
        if signature <= keys:
            matches.append(f"{path}<{'+'.join(sorted(signature))}>")
    trace_keys = keys & QUESTION_TRACE_KEYS
    # A source table may legitimately have a column literally named
    # ``question_id`` or ``original_question``.  Preserve that narrow case,
    # while rejecting either key everywhere else in persistent metadata and
    # rejecting the complete question envelope even inside a column map.
    is_native_column_map = path.endswith(".columns")
    if trace_keys == QUESTION_TRACE_KEYS or (
        trace_keys and not is_native_column_map
    ):
        matches.extend(f"{path}.{key}" for key in sorted(trace_keys))
    annotation_key = value.get("key")
    if (
        isinstance(annotation_key, str)
        and annotation_key
        in QUERY_LAYER_STRUCTURAL_KEYS | QUESTION_TRACE_KEYS
    ):
        matches.append(f"{path}.key")
    return matches


def reserved_query_paths(value: Any, path: str = "root") -> list[str]:
    """Find query-control structures while leaving source payload values untouched."""
    matches: list[str] = []
    pending: list[tuple[Any, str]] = [(value, path)]
    while pending:
        current, current_path = pending.pop()
        if isinstance(current, dict):
            matches.extend(_query_signature_paths(current, current_path))
            for key, child in current.items():
                if current_path == "root.content" and key in SOURCE_VALUE_KEYS:
                    continue
                pending.append((child, f"{current_path}.{key}"))
        elif isinstance(current, list):
            pending.extend(
                (child, f"{current_path}[{index}]")
                for index, child in enumerate(current)
            )
    return matches


def _is_forbidden_source_path(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parts = [part for part in value.replace("\\", "/").split("/") if part]
    normalized_names = {
        part.split("#", 1)[0].split("?", 1)[0].casefold()
        for part in parts
    }
    return "質問回答".casefold() in normalized_names or bool(
        normalized_names & FORBIDDEN_SOURCE_NAMES
    )


def question_boundary_errors(kind: str, record: dict[str, Any], label: str) -> list[str]:
    errors: list[str] = []
    source_paths: list[tuple[str, object]] = []
    if kind == "document":
        source = record.get("source")
        if isinstance(source, dict):
            source_paths.extend(
                (f"source.{field}", source.get(field))
                for field in ("relative_path", "file_name", "archive_relative_path")
            )
    elif kind == "evidence":
        content = record.get("content")
        location = record.get("location")
        if isinstance(content, dict):
            source_paths.append(("content.content_ref", content.get("content_ref")))
        if isinstance(location, dict):
            source_paths.append(("location.source_member", location.get("source_member")))
    for field, value in source_paths:
        if _is_forbidden_source_path(value):
            errors.append(
                f"{label}: question, answer, or submission source is forbidden in "
                f"{field}: {value!r}"
            )
    matches = sorted(set(reserved_query_paths(record)))
    if matches:
        errors.append(
            f"{label}: question-layer data is forbidden in persistent metadata: "
            f"{matches}"
        )
    return errors


def validate_report(directory: Path, source_root: Path | None = None) -> dict[str, Any]:
    schema_validators = published_schema_validators()
    groups = {
        "document": read_jsonl(directory / "documents.jsonl"),
        "evidence": read_jsonl(directory / "evidence.jsonl"),
        "relation": read_jsonl(directory / "relations.jsonl"),
    }
    errors: list[str] = []
    ids: dict[str, set[str]] = {key: set() for key in groups}
    valid_groups: dict[str, list[dict[str, Any]]] = {key: [] for key in groups}
    for kind, records in groups.items():
        id_key = f"{kind}_id"
        for index, record in enumerate(records, 1):
            label = f"{kind}[{index}]"
            record_schema_errors = schema_record_errors(
                kind,
                record,
                label,
                schema_validators[kind],
            )
            errors.extend(record_schema_errors)
            if not isinstance(record, dict):
                continue
            missing = REQUIRED[kind] - record.keys()
            if missing:
                errors.append(f"{label}: missing {sorted(missing)}")
            extra = record.keys() - ALLOWED[kind]
            if extra:
                errors.append(f"{label}: unexpected fields {sorted(extra)}")
            errors.extend(question_boundary_errors(kind, record, label))
            if kind == "document":
                errors.extend(visual_coverage_shape_errors(record, label))
            if record_schema_errors:
                continue
            valid_groups[kind].append(record)
            if record.get("schema_version") != "0.1" or record.get("record_type") != kind:
                errors.append(f"{label}: schema_version/record_type mismatch")
            record_id = record.get(id_key, "")
            if not PATTERNS[kind].fullmatch(record_id):
                errors.append(f"{label}: malformed {id_key}: {record_id!r}")
            if record_id in ids[kind]:
                errors.append(f"{label}: duplicate id {record_id}")
            ids[kind].add(record_id)

    evidence_by_id = {
        item["evidence_id"]: item for item in valid_groups["evidence"]
    }
    for item in valid_groups["document"]:
        source = item.get("source", {})
        expected = stable_id("doc", {
            "relative_path": source.get("relative_path"),
            "source_sha256": source.get("sha256"),
        })
        if item.get("document_id") != expected:
            errors.append(f"{item.get('document_id', '<missing>')}: unstable document id")
        if source_root is not None and not is_notebook_document(item):
            root = source_root.resolve()
            source_path = (root / source.get("relative_path", "")).resolve()
            try:
                source_path.relative_to(root)
            except ValueError:
                errors.append(f"{item.get('document_id', '<missing>')}: source path escapes root")
            else:
                if not source_path.is_file():
                    errors.append(f"{item.get('document_id', '<missing>')}: source file is missing")
                else:
                    source_bytes = source_path.read_bytes()
                    actual_source_sha = hashlib.sha256(source_bytes).hexdigest()
                    if actual_source_sha != source.get("sha256"):
                        errors.append(f"{item.get('document_id', '<missing>')}: source hash mismatch")
                    if len(source_bytes) != source.get("size_bytes"):
                        errors.append(f"{item.get('document_id', '<missing>')}: source size mismatch")
    for item in valid_groups["evidence"]:
        ev_id = item.get("evidence_id", "<missing>")
        doc_id = item.get("document_id")
        if doc_id not in ids["document"]:
            errors.append(f"{ev_id}: dangling document_id {doc_id}")
        parent_id = item.get("parent_evidence_id")
        if parent_id:
            parent = evidence_by_id.get(parent_id)
            if parent is None:
                errors.append(f"{ev_id}: dangling parent {parent_id}")
            elif parent.get("document_id") != doc_id:
                errors.append(f"{ev_id}: parent belongs to another document")
        item_content = item.get("content", {})
        try:
            actual = digest_value(content_hash_payload(item_content))
            if actual != item_content.get("sha256"):
                errors.append(f"{ev_id}: content hash mismatch")
            expected = stable_id("ev", {
                "document_id": doc_id,
                "evidence_type": item.get("evidence_type"),
                "location": item.get("location"),
                "content_sha256": item_content.get("sha256"),
            })
            if ev_id != expected:
                errors.append(f"{ev_id}: unstable evidence id")
            if "raw_text" in item_content:
                expected_normalized = normalize_text(item_content["raw_text"])
                if item_content.get("normalized_text") != expected_normalized:
                    errors.append(f"{ev_id}: normalized_text is missing or inconsistent")
            if "raw_value" in item_content and item_content.get("normalized_value") != item_content["raw_value"]:
                errors.append(f"{ev_id}: normalized_value is missing or inconsistent")
        except ValueError as exc:
            errors.append(f"{ev_id}: {exc}")

    for relation in valid_groups["relation"]:
        rel_id = relation.get("relation_id", "<missing>")
        for side in ("from_ref", "to_ref"):
            ref = relation.get(side, {})
            kind = ref.get("record_type")
            record_id = ref.get("record_id")
            if kind not in ("document", "evidence"):
                errors.append(f"{rel_id}: invalid {side} type {kind!r}")
            elif record_id not in ids[kind]:
                errors.append(f"{rel_id}: dangling {side} {record_id}")
        for evidence_id in relation.get("supporting_evidence_ids", []):
            if evidence_id not in ids["evidence"]:
                errors.append(f"{rel_id}: dangling supporting evidence {evidence_id}")
        expected = stable_id("rel", {
            "class": relation.get("relation_class"),
            "type": relation.get("relation_type"),
            "from": relation.get("from_ref"),
            "to": relation.get("to_ref"),
            "generator": relation.get("provenance", {}).get("generated_by"),
            "generator_version": relation.get("provenance", {}).get("generator_version"),
        })
        if rel_id != expected:
            errors.append(f"{rel_id}: unstable relation id")

    bindings = []
    evidence_by_document: dict[str, list[dict[str, Any]]] = {}
    for item in valid_groups["evidence"]:
        evidence_by_document.setdefault(item.get("document_id"), []).append(item)
    for document in valid_groups["document"]:
        errors.extend(visual_coverage_binding_errors(
            document, evidence_by_document.get(document["document_id"], []),
            str(document.get("document_id")),
        ))
        try:
            bindings.append(notebook_document_binding(
                document,
                evidence_by_document.get(document["document_id"], []),
                source_root,
                parent_lookup=evidence_by_id.get,
            ))
        except ValueError as exc:
            errors.append(f"{document.get('document_id')}: {exc}")
    if errors:
        raise ValueError("validation failed:\n- " + "\n- ".join(errors))
    return notebook_binding_report({kind: len(records) for kind, records in groups.items()}, bindings)


def validate(directory: Path, source_root: Path | None = None) -> dict[str, int]:
    report = validate_report(directory, source_root)
    if report["status"] != "PASS":
        raise ValueError("notebook_metadata_binding_unverified: " + ",".join(report["notebook_metadata_binding"]["reason_codes"]))
    return report["counts"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--root", type=Path, help="recheck original bytes; required for Notebook metadata binding")
    args = parser.parse_args()
    try:
        report = validate_report(args.directory, args.root)
    except ValueError as exc:
        print(canonical_json({"status": "FAIL", "error": str(exc)[:512]}))
        raise SystemExit(1)
    print(canonical_json(report))
    if report["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
