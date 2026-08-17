#!/usr/bin/env python3
"""Validate deterministic, question-only QuestionClauseIR records.

The certified v0.1 grammar reads only ``question_id`` and
``original_question``.  It never reads a corpus, catalog, retrieval result, or
answer.  Schema validation closes the record shape; the semantic checks below
re-derive the grammar profile and clause partition, recompute content IDs and
hashes, and optionally bind every declared QIC pointer to a supplied
QuestionIntentContract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"
CLAUSE_IR_SCHEMA_PATH = SCHEMAS / "question-clause-ir.schema.json"
QIC_SCHEMA_PATH = SCHEMAS / "question-intent-contract.schema.json"

MAX_JSON_BYTES = 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 100_000
MAX_JSON_DEPTH = 64

PARSER = "question-clause-parser"
PARSER_VERSION = "0.1"
RULE_VERSION = "v0.1"
SUPPORTED_PROFILES = (
    "list_eq_id_all_v0_1",
    "list_suffix_eq_id_all_v0_1",
    "compound_eq_gt_mean_nearest_id_all_v0_1",
)
UNSUPPORTED_PROFILE = "unsupported_v0_1"

sys.path.insert(0, str(ROOT / "scripts"))
from question_language_registry import (  # noqa: E402
    ALTERNATIVE_CONNECTORS,
    CANONICAL_TARGET_TYPE_LEXEMES,
    LANGUAGE_REGISTRY_SHA256,
    REGISTRY_NAME,
    REGISTRY_VERSION,
    SUPPORTED_FILTER_SUFFIXES,
    SUPPORTED_LANE_NEGATIVE_MARKERS,
    SUPPORTED_METRIC_DESCRIPTOR_ALIASES,
    registry_digest,
)


QUESTION_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["question_id", "original_question"],
    "properties": {
        "question_id": {"type": ["string", "null"], "minLength": 1},
        "original_question": {"type": "string", "minLength": 1},
    },
}


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number is forbidden: {value}")
    return parsed


def _check_json_depth(value: Any, max_depth: int) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > max_depth:
            raise ValueError(f"JSON nesting exceeds the configured depth {max_depth}")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)


def load_strict_json(
    text: str,
    *,
    max_bytes: int = MAX_JSON_BYTES,
    max_depth: int = MAX_JSON_DEPTH,
) -> Any:
    """Load one complete JSON value with bounded bytes and nesting.

    Duplicate object keys, NaN/Infinity, parser recursion overflow, and UTF-8
    payloads larger than ``max_bytes`` are rejected.
    """

    if not isinstance(text, str):
        raise TypeError("JSON input must be text")
    if not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if not isinstance(max_depth, int) or max_depth < 1:
        raise ValueError("max_depth must be a positive integer")
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError(f"JSON input exceeds {max_bytes} bytes")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
            parse_float=_parse_finite_float,
        )
        _check_json_depth(value, max_depth)
        return value
    except (RecursionError, OverflowError) as exc:
        raise ValueError(
            f"JSON parser resource limit exceeded: {type(exc).__name__}"
        ) from exc


def _read_regular_text(path: Path, max_file_bytes: int) -> str:
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"input must be a regular non-symlink file: {path}")
    if metadata.st_size > max_file_bytes:
        raise ValueError(f"input exceeds {max_file_bytes} bytes: {path}")
    return path.read_text(encoding="utf-8")


def load_json_records(
    path: Path,
    *,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_records: int = MAX_RECORDS,
) -> list[dict[str, Any]]:
    """Load a single-object JSON file or an object-per-line JSONL file."""

    if not isinstance(max_file_bytes, int) or max_file_bytes < 1:
        raise ValueError("max_file_bytes must be a positive integer")
    if not isinstance(max_records, int) or max_records < 1:
        raise ValueError("max_records must be a positive integer")
    text = _read_regular_text(path, max_file_bytes)
    suffix = path.suffix.casefold()
    if suffix == ".json":
        value = load_strict_json(text, max_bytes=max_file_bytes)
        if not isinstance(value, dict):
            raise ValueError(f"{path}: JSON root must be an object")
        return [value]
    if suffix != ".jsonl":
        raise ValueError(f"{path}: suffix must be .json or .jsonl")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if len(records) >= max_records:
            raise ValueError(f"record count exceeds {max_records}")
        value = load_strict_json(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: record must be an object")
        records.append(value)
    if not records:
        raise ValueError(f"{path}: contains no records")
    return records


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def question_sha256(question: str) -> str:
    return hashlib.sha256(question.encode("utf-8")).hexdigest()


def _content_id(prefix: str, value: Any, length: int = 20) -> str:
    return f"{prefix}_{sha256_json(value)[:length]}"


def clause_identity_core(
    question_hash: str,
    grammar_profile: str,
    clause: dict[str, Any],
) -> dict[str, Any]:
    return {
        "question_sha256": question_hash,
        "grammar_profile": grammar_profile,
        "span": clause["span"],
        "role": clause["role"],
        "normalized_value": clause["normalized_value"],
        "polarity": clause["polarity"],
        "qic_paths": clause["qic_paths"],
        "disposition": clause["disposition"],
    }


def deterministic_clause_id(
    question_hash: str,
    grammar_profile: str,
    clause: dict[str, Any],
) -> str:
    return _content_id(
        "qcl", clause_identity_core(question_hash, grammar_profile, clause)
    )


def ir_identity_core(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": record["schema_version"],
        "record_type": record["record_type"],
        "question_id": record["question_id"],
        "input_question_sha256": record["provenance"]["input_question_sha256"],
        "grammar_profile": record["grammar_profile"],
        "clauses": record["clauses"],
        "coverage": record["coverage"],
        "parser": record["provenance"]["parser"],
        "parser_version": record["provenance"]["parser_version"],
        "registry_name": record["provenance"]["registry_name"],
        "registry_version": record["provenance"]["registry_version"],
        "registry_sha256": record["provenance"]["registry_sha256"],
        "rule_version": record["provenance"]["rule_version"],
    }


def deterministic_ir_id(record: dict[str, Any]) -> str:
    return _content_id("qcir", ir_identity_core(record))


def _load_schema(path: Path) -> dict[str, Any]:
    value = load_strict_json(
        path.read_text(encoding="utf-8"), max_bytes=MAX_FILE_BYTES
    )
    if not isinstance(value, dict):
        raise ValueError(f"schema root must be an object: {path}")
    return value


def _schema_errors(validator: Any, value: Any) -> list[str]:
    result: list[str] = []
    for error in sorted(
        validator.iter_errors(value),
        key=lambda item: (
            tuple(str(component) for component in item.absolute_path),
            item.message,
        ),
    ):
        location = ".".join(str(item) for item in error.absolute_path) or "root"
        result.append(f"{location}: {error.message}")
    return result


@lru_cache(maxsize=1)
def _question_input_validator() -> Any:
    import jsonschema

    jsonschema.Draft202012Validator.check_schema(QUESTION_INPUT_SCHEMA)
    return jsonschema.Draft202012Validator(QUESTION_INPUT_SCHEMA)


@lru_cache(maxsize=1)
def _clause_ir_validator() -> Any:
    import jsonschema

    schema = _load_schema(CLAUSE_IR_SCHEMA_PATH)
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )


@lru_cache(maxsize=1)
def _qic_validator() -> Any:
    import jsonschema

    schema = _load_schema(QIC_SCHEMA_PATH)
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )


def validate_question_input(value: Any) -> list[str]:
    return _schema_errors(_question_input_validator(), value)


def validate_question_clause_ir_schema(value: Any) -> list[str]:
    return _schema_errors(_clause_ir_validator(), value)


_FILTER_SUFFIX_ALTERNATION = "|".join(
    re.escape(value)
    for value in sorted(SUPPORTED_FILTER_SUFFIXES, key=lambda value: (-len(value), value))
)
_IDENTIFIER_TOKEN = (
    r"(?:[A-Za-z][A-Za-z0-9_.-]*[Ii][Dd][A-Za-z0-9_.-]*|"
    r"(?<![A-Za-z0-9_])[Ii][Dd](?![A-Za-z0-9_])|"
    r"(?:タスク|行|従業員|社員|プロジェクト|イベント|"
    r"文書|レコード|組織)[Ii][Dd])"
)
_SCOPE_PREFIX = (
    r"(?P<location>[^,、。\n]{1,96}?)の"
    r"(?P<container>[^,、。\n]{1,96}?)において[ \t]*、[ \t]*"
)
_LIST_OUTPUT = (
    rf"(?P<identifier>{_IDENTIFIER_TOKEN})を"
    r"(?P<cardinality>すべて|全て|全部)"
    r"(?P<request_operation>挙げて|答えて|教えて|列挙して)ください[.。]?"
)
_LIST_STANDARD_PATTERN = re.compile(
    rf"\A{_SCOPE_PREFIX}"
    r"(?P<field>[^,、。\n]{1,64}?)が"
    r"(?P<value>[^,、。\n]{1,96}?)に(?P<operator>一致)"
    r"(?P<filter_operation>する)"
    rf"{_LIST_OUTPUT}\Z"
)
_LIST_SUFFIX_PATTERN = re.compile(
    rf"\A{_SCOPE_PREFIX}"
    r"(?P<value>[^,、。\n]{1,96}?)"
    rf"(?P<field>{_FILTER_SUFFIX_ALTERNATION})"
    r"に(?P<operator>一致)(?P<filter_operation>する)"
    rf"{_LIST_OUTPUT}\Z"
)
_NUMBER_TOKEN = r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?"
_COMPOUND_PATTERN = re.compile(
    rf"\A{_SCOPE_PREFIX}"
    r"(?P<equality_field>[^,、。\n]{1,64}?)が"
    r"(?P<equality_value>[^,、。\n]{1,96}?)"
    r"(?:であり[,、]?)?(?P<boolean_connector>かつ)"
    r"(?P<threshold_field>[^,、。\n]{1,64}?)が"
    rf"(?P<threshold>{_NUMBER_TOKEN})(?P<gt_operator>より大きい)"
    r"(?P<target>データ)を(?P<extract_operation>抽出)し[,、]"
    r"(?P<metric>[^,、。\n]{1,64}?)の(?P<mean_operation>平均値)"
    r"を計算してください。その平均値に(?P<nearest_operation>最も近い)"
    r"(?P<nearest_descriptor>[^,、。\n]{1,64}?)の"
    rf"{_LIST_OUTPUT}\Z"
)
_IDENTIFIER_OUTPUT_PATTERN = re.compile(
    rf"(?P<identifier>{_IDENTIFIER_TOKEN})を"
    r"(?:すべて|全て|全部)?(?:挙げ|答え|教え|列挙)"
)


def _lexical_token_count(text: str, token: str) -> int:
    if token and all(
        character.isascii() and (character.isalnum() or character == "_")
        for character in token
    ):
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])"
        return len(re.findall(pattern, text, flags=re.IGNORECASE))
    return text.casefold().count(token.casefold())


def _contains_lexical_token(text: str, token: str) -> bool:
    return _lexical_token_count(text, token) > 0


def _target_type_matches(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    compact = re.sub(r"[\s_\-./:：]+", "", normalized)
    matches: list[tuple[int, str]] = []
    for canonical_type, lexemes in CANONICAL_TARGET_TYPE_LEXEMES.items():
        for lexeme in lexemes:
            normalized_lexeme = unicodedata.normalize("NFKC", lexeme).casefold()
            compact_lexeme = re.sub(r"[\s_\-./:：]+", "", normalized_lexeme)
            if not compact_lexeme:
                continue
            if compact_lexeme.endswith("id") and len(compact_lexeme) > 2:
                matched = compact.startswith(compact_lexeme) or compact.endswith(
                    compact_lexeme
                )
            elif all(character.isascii() for character in compact_lexeme):
                matched = _contains_lexical_token(normalized, normalized_lexeme)
            else:
                matched = (
                    compact == compact_lexeme
                    if len(compact_lexeme) == 1
                    else compact_lexeme in compact
                )
            if matched:
                matches.append((len(compact_lexeme), canonical_type))
    if not matches:
        return set()
    longest = max(length for length, _ in matches)
    return {kind for length, kind in matches if length == longest}


def _capture_has_inline_choice(value: str) -> bool:
    return re.search(
        r"(?<=[^\s,\u3001。\n])(?:か(?!つ)|と)(?=[^\s,\u3001。\n])",
        value,
    ) is not None


def _capture_has_clear_connector(
    value: str,
    *,
    allow_middle_dot: bool = False,
    allow_path_slash: bool = False,
) -> bool:
    return (
        any(connector in value for connector in ALTERNATIVE_CONNECTORS)
        or any(_contains_lexical_token(value, token) for token in ("or", "and"))
        or _capture_has_inline_choice(value)
        or (not allow_middle_dot and "・" in value)
        or (not allow_path_slash and any(token in value for token in ("/", "／")))
    )


def _grammar_match(question: str) -> tuple[str, re.Match[str]] | None:
    matches: list[tuple[str, re.Match[str]]] = []
    for profile, pattern in (
        ("list_eq_id_all_v0_1", _LIST_STANDARD_PATTERN),
        ("list_suffix_eq_id_all_v0_1", _LIST_SUFFIX_PATTERN),
        ("compound_eq_gt_mean_nearest_id_all_v0_1", _COMPOUND_PATTERN),
    ):
        match = pattern.fullmatch(question)
        if match is not None:
            matches.append((profile, match))
    if len(matches) != 1:
        return None
    profile, match = matches[0]
    folded = question.casefold()
    if any(marker.casefold() in folded for marker in SUPPORTED_LANE_NEGATIVE_MARKERS):
        return None
    if any(connector in question for connector in ALTERNATIVE_CONNECTORS):
        return None
    if any(_contains_lexical_token(question, token) for token in ("or", "and")):
        return None
    for group, value in match.groupdict().items():
        if value is None or not value or value != value.strip() or "\t" in value:
            return None
        if _capture_has_clear_connector(
            value,
            allow_middle_dot=group in {"value", "equality_value"},
            allow_path_slash=group == "container",
        ):
            return None
    outputs = list(_IDENTIFIER_OUTPUT_PATTERN.finditer(question))
    if (
        len(outputs) != 1
        or outputs[0].span("identifier") != match.span("identifier")
    ):
        return None
    if len(_target_type_matches(match.group("identifier"))) != 1:
        return None
    if profile.startswith("list_"):
        if question.count("一致") != 1:
            return None
        if "が" in match.group("field") or "が" in match.group("value"):
            return None
        if profile == "list_suffix_eq_id_all_v0_1":
            if match.group("field") not in SUPPORTED_FILTER_SUFFIXES:
                return None
        return profile, match
    for group in (
        "equality_field",
        "equality_value",
        "threshold_field",
        "metric",
        "nearest_descriptor",
    ):
        value = match.group(group)
        if "が" in value or any(
            token in value
            for token in ("かつ", "であり", "より大きい", "に一致", "抽出")
        ):
            return None
    if question.count("より大きい") != 1 or question.count("最も近い") != 1:
        return None
    metric = match.group("metric")
    descriptor = match.group("nearest_descriptor")
    if metric != descriptor and (metric, descriptor) not in SUPPORTED_METRIC_DESCRIPTOR_ALIASES:
        return None
    try:
        threshold = load_strict_json(match.group("threshold"), max_bytes=256)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        return None
    if _target_type_matches(match.group("target")) != {"record"}:
        return None
    return profile, match


def _unique_scalars(values: Iterable[Any]) -> Any:
    result: list[Any] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result[0] if len(result) == 1 else result


def _semantic_clause(
    match: re.Match[str],
    group: str,
    role: str,
    normalized_value: Any,
    qic_paths: list[str],
    *,
    polarity: str = "positive",
) -> dict[str, Any]:
    start, end = match.span(group)
    return {
        "span": {"start": start, "end": end, "text": match.string[start:end]},
        "role": role,
        "normalized_value": normalized_value,
        "polarity": polarity,
        "qic_paths": qic_paths,
        "disposition": "mapped",
    }


def _list_semantic_clauses(match: re.Match[str]) -> list[dict[str, Any]]:
    identifier = match.group("identifier")
    return [
        _semantic_clause(
            match,
            "location",
            "scope_location",
            match.group("location"),
            ["/requested/scope/location"],
        ),
        _semantic_clause(
            match,
            "container",
            "scope_container",
            match.group("container"),
            ["/requested/scope/container"],
        ),
        _semantic_clause(
            match,
            "field",
            "filter_field",
            match.group("field"),
            [
                "/requested/scope/filters/0/field",
                "/requested/operation_graph/nodes/0/predicate/field",
            ],
        ),
        _semantic_clause(
            match,
            "value",
            "filter_value",
            match.group("value"),
            [
                "/requested/scope/filters/0/value",
                "/requested/operation_graph/nodes/0/predicate/value",
            ],
        ),
        _semantic_clause(
            match,
            "operator",
            "filter_operator",
            "eq",
            [
                "/requested/scope/filters/0/operator",
                "/requested/operation_graph/nodes/0/predicate/operator",
            ],
        ),
        _semantic_clause(
            match,
            "filter_operation",
            "operation",
            "filter",
            ["/requested/operation_graph/nodes/0/operator"],
        ),
        _semantic_clause(
            match,
            "identifier",
            "target_surface",
            _unique_scalars((identifier, "identifier")),
            [
                "/requested/target/surface",
                "/requested/operation_graph/nodes/1/fields/0",
                "/requested/requested_outputs/0/return_field",
            ],
        ),
        _semantic_clause(
            match,
            "cardinality",
            "cardinality",
            "all",
            ["/requested/requested_outputs/0/cardinality/mode"],
        ),
        _semantic_clause(
            match,
            "request_operation",
            "operation",
            "project",
            ["/requested/operation_graph/nodes/1/operator"],
        ),
    ]


def _compound_semantic_clauses(match: re.Match[str]) -> list[dict[str, Any]]:
    threshold = load_strict_json(match.group("threshold"), max_bytes=256)
    metric = match.group("metric")
    identifier = match.group("identifier")
    equality_separator_start = match.end("equality_field")
    equality_separator_end = match.start("equality_value")
    if match.string[equality_separator_start:equality_separator_end] != "が":
        raise ValueError("certified equality separator is inconsistent")
    equality_operator = {
        "span": {
            "start": equality_separator_start,
            "end": equality_separator_end,
            "text": "が",
        },
        "role": "filter_operator",
        "normalized_value": "eq",
        "polarity": "positive",
        "qic_paths": [
            "/requested/scope/filters/0/operator",
            "/requested/operation_graph/nodes/0/predicate/operator",
        ],
        "disposition": "mapped",
    }
    return [
        _semantic_clause(match, "location", "scope_location", match.group("location"), ["/requested/scope/location"]),
        _semantic_clause(match, "container", "scope_container", match.group("container"), ["/requested/scope/container"]),
        _semantic_clause(match, "equality_field", "filter_field", match.group("equality_field"), ["/requested/scope/filters/0/field", "/requested/operation_graph/nodes/0/predicate/field"]),
        equality_operator,
        _semantic_clause(match, "equality_value", "filter_value", match.group("equality_value"), ["/requested/scope/filters/0/value", "/requested/operation_graph/nodes/0/predicate/value"]),
        _semantic_clause(
            match,
            "boolean_connector",
            "boolean_connector",
            "and",
            ["/requested/operation_graph/edges"],
        ),
        _semantic_clause(match, "threshold_field", "filter_field", match.group("threshold_field"), ["/requested/scope/filters/1/field", "/requested/operation_graph/nodes/1/predicate/field"]),
        _semantic_clause(match, "threshold", "filter_value", threshold, ["/requested/scope/filters/1/value", "/requested/operation_graph/nodes/1/predicate/value"]),
        _semantic_clause(match, "gt_operator", "filter_operator", "gt", ["/requested/scope/filters/1/operator", "/requested/operation_graph/nodes/1/predicate/operator"]),
        _semantic_clause(match, "target", "target_surface", match.group("target"), ["/requested/target/surface"]),
        _semantic_clause(match, "extract_operation", "operation", "filter", ["/requested/operation_graph/nodes/0/operator", "/requested/operation_graph/nodes/1/operator"]),
        _semantic_clause(match, "metric", "filter_field", _unique_scalars((metric, "value")), ["/requested/operation_graph/nodes/2/fields/0", "/requested/operation_graph/nodes/4/field", "/requested/requested_outputs/0/return_field"]),
        _semantic_clause(match, "mean_operation", "operation", "mean", ["/requested/operation_graph/nodes/3/operator"]),
        _semantic_clause(match, "nearest_operation", "operation", "argmin_all", ["/requested/operation_graph/nodes/4/operator"]),
        _semantic_clause(match, "nearest_descriptor", "filter_field", metric, ["/requested/operation_graph/nodes/4/field"]),
        _semantic_clause(
            match,
            "identifier",
            "return_field",
            "identifier",
            [
                "/requested/operation_graph/nodes/5/fields/0",
                "/requested/requested_outputs/1/return_field",
            ],
        ),
        _semantic_clause(match, "cardinality", "cardinality", "all", ["/requested/requested_outputs/1/cardinality/mode"]),
        _semantic_clause(match, "request_operation", "operation", "project", ["/requested/operation_graph/nodes/5/operator"]),
    ]


def _partition_with_syntax(
    question: str,
    semantic_clauses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    semantic = sorted(
        semantic_clauses,
        key=lambda item: (item["span"]["start"], item["span"]["end"]),
    )
    result: list[dict[str, Any]] = []
    cursor = 0
    for clause in semantic:
        start = clause["span"]["start"]
        end = clause["span"]["end"]
        if start < cursor or end <= start or end > len(question):
            raise ValueError("certified semantic spans overlap or are out of bounds")
        if start > cursor:
            result.append(
                {
                    "span": {
                        "start": cursor,
                        "end": start,
                        "text": question[cursor:start],
                    },
                    "role": "syntax",
                    "normalized_value": None,
                    "polarity": "not_applicable",
                    "qic_paths": [],
                    "disposition": "syntax",
                }
            )
        if clause["span"]["text"] != question[start:end]:
            raise ValueError("certified semantic span text is inconsistent")
        result.append(clause)
        cursor = end
    if cursor < len(question):
        result.append(
            {
                "span": {"start": cursor, "end": len(question), "text": question[cursor:]},
                "role": "syntax",
                "normalized_value": None,
                "polarity": "not_applicable",
                "qic_paths": [],
                "disposition": "syntax",
            }
        )
    if not result:
        raise ValueError("a non-empty question must produce at least one clause")
    return result


def parse_certified_question(question: str) -> tuple[str, list[dict[str, Any]]]:
    """Return one certified profile and a complete ID-free clause partition.

    Unsupported or ambiguous text is represented by one unresolved full-span
    clause; it is never guessed into a supported profile.
    """

    if not isinstance(question, str) or not question:
        raise ValueError("original_question must be a non-empty string")
    matched = _grammar_match(question)
    if matched is None:
        return UNSUPPORTED_PROFILE, [
            {
                "span": {"start": 0, "end": len(question), "text": question},
                "role": "unresolved",
                "normalized_value": None,
                "polarity": "not_applicable",
                "qic_paths": [],
                "disposition": "unresolved",
            }
        ]
    profile, match = matched
    semantic = (
        _compound_semantic_clauses(match)
        if profile == "compound_eq_gt_mean_nearest_id_all_v0_1"
        else _list_semantic_clauses(match)
    )
    return profile, _partition_with_syntax(question, semantic)


def _pointer_get(value: Any, pointer: str) -> tuple[bool, Any]:
    if not pointer.startswith("/"):
        return False, None
    current = value
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
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


def _normalized_binds(
    normalized: Any, actual: Any, *, role: str, surface: str
) -> bool:
    # In QIC v0.1 a conjunction is encoded structurally by the first filter
    # feeding the second filter; there is no free-form connector field.  The
    # ClauseIR still preserves the explicit connector and binds it to the
    # typed edge list, whose exact topology is checked below.
    if role == "boolean_connector":
        return normalized == "and" and isinstance(actual, list)
    if role == "return_field" and normalized == "identifier":
        return actual in {"identifier", surface}
    if isinstance(normalized, list):
        return any(actual == candidate for candidate in normalized)
    return actual == normalized


def _expected_qic_paths(
    profile: str,
    match: re.Match[str],
) -> dict[str, Any]:
    identifier = match.group("identifier")
    target_type = next(iter(_target_type_matches(identifier)))
    common: dict[str, Any] = {
        "/requested/scope/location": match.group("location"),
        "/requested/scope/container": match.group("container"),
        "/requested/scope/time_or_version": None,
        "/requested/scope/source": "explicit",
        "/requested/scope/match_mode": "exact_normalized",
    }
    if profile.startswith("list_"):
        common.update(
            {
                "/requested/target/surface": identifier,
                "/requested/target/canonical_type": target_type,
                "/requested/target/instance": None,
                "/requested/scope/filters/0/field": match.group("field"),
                "/requested/scope/filters/0/operator": "eq",
                "/requested/scope/filters/0/value": match.group("value"),
                "/requested/operation_graph/nodes/0/operator": "filter",
                "/requested/operation_graph/nodes/0/predicate/field": match.group("field"),
                "/requested/operation_graph/nodes/0/predicate/operator": "eq",
                "/requested/operation_graph/nodes/0/predicate/value": match.group("value"),
                "/requested/operation_graph/nodes/1/operator": "project",
                "/requested/operation_graph/nodes/1/fields/0": identifier,
                "/requested/requested_outputs/0/return_field": "identifier",
                "/requested/requested_outputs/0/cardinality/mode": "all",
                "/requested/requested_outputs/0/cardinality/expected_count": None,
                "/requested/requested_outputs/0/answer_shape/container": "list",
                "/requested/requested_outputs/0/answer_shape/value_type": "identifier",
                "/requested/requested_outputs/0/answer_shape/unit": None,
                "/requested/requested_outputs/0/answer_shape/precision": "exact",
                "/requested/requested_outputs/0/display_precision": None,
            }
        )
        return common
    threshold = load_strict_json(match.group("threshold"), max_bytes=256)
    metric = match.group("metric")
    common.update(
        {
            "/requested/target/surface": match.group("target"),
            "/requested/target/canonical_type": "record",
            "/requested/target/instance": None,
            "/requested/scope/filters/0/field": match.group("equality_field"),
            "/requested/scope/filters/0/operator": "eq",
            "/requested/scope/filters/0/value": match.group("equality_value"),
            "/requested/scope/filters/1/field": match.group("threshold_field"),
            "/requested/scope/filters/1/operator": "gt",
            "/requested/scope/filters/1/value": threshold,
            "/requested/operation_graph/nodes/0/operator": "filter",
            "/requested/operation_graph/nodes/0/predicate/field": match.group("equality_field"),
            "/requested/operation_graph/nodes/0/predicate/operator": "eq",
            "/requested/operation_graph/nodes/0/predicate/value": match.group("equality_value"),
            "/requested/operation_graph/nodes/1/operator": "filter",
            "/requested/operation_graph/nodes/1/predicate/field": match.group("threshold_field"),
            "/requested/operation_graph/nodes/1/predicate/operator": "gt",
            "/requested/operation_graph/nodes/1/predicate/value": threshold,
            "/requested/operation_graph/nodes/2/operator": "project",
            "/requested/operation_graph/nodes/2/fields/0": metric,
            "/requested/operation_graph/nodes/3/operator": "mean",
            "/requested/operation_graph/nodes/3/calculation_precision": "exact_unrounded",
            "/requested/operation_graph/nodes/4/operator": "argmin_all",
            "/requested/operation_graph/nodes/4/distance": "absolute",
            "/requested/operation_graph/nodes/4/field": metric,
            "/requested/operation_graph/nodes/4/tie_policy": "all",
            "/requested/operation_graph/nodes/5/operator": "project",
            "/requested/operation_graph/nodes/5/fields/0": identifier,
            "/requested/requested_outputs/0/return_field": "value",
            "/requested/requested_outputs/0/cardinality/mode": "single",
            "/requested/requested_outputs/0/cardinality/expected_count": 1,
            "/requested/requested_outputs/0/answer_shape/container": "scalar",
            "/requested/requested_outputs/0/answer_shape/value_type": "number",
            "/requested/requested_outputs/0/answer_shape/unit": None,
            "/requested/requested_outputs/0/answer_shape/precision": "exact",
            "/requested/requested_outputs/0/display_precision": None,
            "/requested/requested_outputs/1/return_field": "identifier",
            "/requested/requested_outputs/1/cardinality/mode": "all",
            "/requested/requested_outputs/1/cardinality/expected_count": None,
            "/requested/requested_outputs/1/answer_shape/container": "list",
            "/requested/requested_outputs/1/answer_shape/value_type": "identifier",
            "/requested/requested_outputs/1/answer_shape/unit": None,
            "/requested/requested_outputs/1/answer_shape/precision": "exact",
            "/requested/requested_outputs/1/display_precision": None,
        }
    )
    return common


def _validate_qic_binding(
    record: dict[str, Any],
    qic: Any,
    profile_match: tuple[str, re.Match[str]] | None,
) -> list[str]:
    errors = [f"qic.{item}" for item in _schema_errors(_qic_validator(), qic)]
    if errors or not isinstance(qic, dict):
        return errors
    if qic.get("question_id") != record["question_id"]:
        errors.append("qic.question_id does not match QuestionClauseIR")
    if qic.get("original_question") != record["original_question"]:
        errors.append("qic.original_question does not match QuestionClauseIR")
    for clause in record["clauses"]:
        if clause["disposition"] != "mapped":
            continue
        for pointer in clause["qic_paths"]:
            found, actual = _pointer_get(qic, pointer)
            if not found:
                errors.append(f"qic path is missing: {pointer}")
            elif not _normalized_binds(
                clause["normalized_value"],
                actual,
                role=clause["role"],
                surface=clause["span"]["text"],
            ):
                errors.append(f"qic path value does not bind normalized_value: {pointer}")
    if profile_match is not None:
        profile, match = profile_match
        requested = qic.get("requested")
        if isinstance(requested, dict):
            expected_counts = {
                "/requested/scope/filters": 2 if profile.startswith("compound_") else 1,
                "/requested/operation_graph/nodes": 6 if profile.startswith("compound_") else 2,
                "/requested/requested_outputs": 2 if profile.startswith("compound_") else 1,
            }
            for pointer, expected_count in expected_counts.items():
                found, value = _pointer_get(qic, pointer)
                if not found or not isinstance(value, list) or len(value) != expected_count:
                    errors.append(f"qic collection cardinality mismatch: {pointer}")
            for pointer, expected in _expected_qic_paths(profile, match).items():
                found, actual = _pointer_get(qic, pointer)
                if not found:
                    errors.append(f"qic expected binding is missing: {pointer}")
                elif actual != expected:
                    errors.append(f"qic expected binding mismatch: {pointer}")
            if profile.startswith("compound_"):
                found, nodes = _pointer_get(qic, "/requested/operation_graph/nodes")
                found_edges, edges = _pointer_get(qic, "/requested/operation_graph/edges")
                if not found or not found_edges or not isinstance(nodes, list) or not isinstance(edges, list):
                    errors.append("qic conjunction topology is missing")
                elif len(nodes) < 2:
                    errors.append("qic conjunction topology has too few nodes")
                else:
                    first_id = nodes[0].get("operation_id") if isinstance(nodes[0], dict) else None
                    second_id = nodes[1].get("operation_id") if isinstance(nodes[1], dict) else None
                    if not isinstance(first_id, str) or not isinstance(second_id, str):
                        errors.append("qic conjunction topology identifiers are invalid")
                    elif not any(
                        isinstance(edge, dict)
                        and edge.get("from") == first_id
                        and edge.get("to") == second_id
                        for edge in edges
                    ):
                        errors.append("qic conjunction topology does not preserve and")
    return sorted(set(errors))


def validate_question_clause_ir(
    record: Any,
    qic: dict[str, Any] | None = None,
) -> list[str]:
    """Return schema and deterministic semantic errors for one ClauseIR."""

    errors = validate_question_clause_ir_schema(record)
    if errors or not isinstance(record, dict):
        return errors
    question = record["original_question"]
    question_hash = question_sha256(question)
    provenance = record["provenance"]
    expected_provenance = {
        "parser": PARSER,
        "parser_version": PARSER_VERSION,
        "registry_name": REGISTRY_NAME,
        "registry_version": REGISTRY_VERSION,
        "registry_sha256": registry_digest(),
        "rule_version": RULE_VERSION,
        "input_question_sha256": question_hash,
        "deterministic": True,
        "question_only": True,
        "catalog_used": False,
        "answer_data_used": False,
        "past_answers_used": False,
    }
    for key, expected in expected_provenance.items():
        if provenance.get(key) != expected:
            errors.append(f"provenance.{key} is inconsistent")
    if LANGUAGE_REGISTRY_SHA256 != registry_digest():
        errors.append("loaded language registry digest constant is inconsistent")

    profile, expected_without_ids = parse_certified_question(question)
    if record["grammar_profile"] != profile:
        errors.append("grammar_profile does not match the certified raw grammar")
    clauses = record["clauses"]
    cursor = 0
    seen_ids: set[str] = set()
    for index, clause in enumerate(clauses):
        span = clause["span"]
        start, end = span["start"], span["end"]
        if start != cursor:
            errors.append(f"clauses.{index}.span does not continue the exact partition")
        if end <= start or end > len(question):
            errors.append(f"clauses.{index}.span is out of bounds")
        elif span["text"] != question[start:end]:
            errors.append(f"clauses.{index}.span.text does not equal question slice")
        cursor = end
        expected_id = deterministic_clause_id(question_hash, profile, clause)
        if clause["clause_id"] != expected_id:
            errors.append(f"clauses.{index}.clause_id is inconsistent")
        if clause["clause_id"] in seen_ids:
            errors.append(f"clauses.{index}.clause_id is duplicated")
        seen_ids.add(clause["clause_id"])
    if cursor != len(question):
        errors.append("clauses do not partition the complete question")

    actual_without_ids = [
        {key: value for key, value in clause.items() if key != "clause_id"}
        for clause in clauses
    ]
    if actual_without_ids != expected_without_ids:
        errors.append("clauses do not equal the deterministic certified partition")

    unresolved = [
        clause["clause_id"]
        for clause in clauses
        if clause["disposition"] == "unresolved"
    ]
    conflicts = [
        clause["clause_id"]
        for clause in clauses
        if clause["disposition"] == "conflict"
    ]
    covered = sum(
        clause["span"]["end"] - clause["span"]["start"]
        for clause in clauses
        if clause["disposition"] in {"mapped", "syntax"}
    )
    coverage = record["coverage"]
    expected_status = "conflict" if conflicts else ("incomplete" if unresolved or coverage["unbound_qic_paths"] else "complete")
    if coverage["total_codepoints"] != len(question):
        errors.append("coverage.total_codepoints is inconsistent")
    if coverage["covered_codepoints"] != covered:
        errors.append("coverage.covered_codepoints is inconsistent")
    if coverage["unresolved_clause_refs"] != unresolved:
        errors.append("coverage.unresolved_clause_refs is inconsistent")
    if coverage["conflict_clause_refs"] != conflicts:
        errors.append("coverage.conflict_clause_refs is inconsistent")
    if coverage["status"] != expected_status:
        errors.append("coverage.status is inconsistent")
    known_paths = {
        pointer
        for clause in clauses
        for pointer in clause["qic_paths"]
    }
    if any(pointer in known_paths for pointer in coverage["unbound_qic_paths"]):
        errors.append("coverage.unbound_qic_paths contains a bound clause path")
    if record["question_clause_ir_id"] != deterministic_ir_id(record):
        errors.append("question_clause_ir_id is inconsistent")

    profile_match = _grammar_match(question)
    if qic is not None:
        errors.extend(_validate_qic_binding(record, qic, profile_match))
    return sorted(set(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="QuestionClauseIR JSON or JSONL")
    parser.add_argument("--qic", type=Path, help="optional aligned QIC JSON or JSONL")
    args = parser.parse_args()
    try:
        records = load_json_records(args.input)
        qics = load_json_records(args.qic) if args.qic is not None else None
        if qics is not None and len(qics) != len(records):
            raise ValueError("QIC record count must equal ClauseIR record count")
        failures = 0
        for index, record in enumerate(records):
            qic = qics[index] if qics is not None else None
            errors = validate_question_clause_ir(record, qic)
            if errors:
                failures += 1
                for error in errors:
                    print(f"record[{index}]: {error}", file=sys.stderr)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(canonical_json({"records": len(records), "invalid": failures}))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
