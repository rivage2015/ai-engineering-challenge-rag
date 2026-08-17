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
from typing import Any

from probe_intermediate_records import normalize_text


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


def validate(directory: Path, source_root: Path | None = None) -> dict[str, int]:
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
        if source_root is not None:
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

    if errors:
        raise ValueError("validation failed:\n- " + "\n- ".join(errors))
    return {kind: len(records) for kind, records in groups.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--root", type=Path, help="optionally recheck source file size and SHA-256")
    args = parser.parse_args()
    print(canonical_json({"status": "ok", "counts": validate(args.directory, args.root)}))


if __name__ == "__main__":
    main()
