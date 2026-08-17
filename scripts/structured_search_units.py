#!/usr/bin/env python3
"""Deterministic structured-row profile and executor for SearchUnit records.

The module never persists source values.  It recognizes only the exact
``header: value`` row serialization emitted by ``build_search_units.py``.
Any ambiguous header, missing cell, multiline value, schema drift, or unknown
operation fails closed instead of being guessed.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence


PROFILE_NAME = "search-unit-structured-row"
PROFILE_VERSION = "0.1"

_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")
_NUMBER = re.compile(
    r"-?(?:(?:0|[1-9][0-9]*)\.[0-9]+|(?:0|[1-9][0-9]*)[eE][+-]?[0-9]+|"
    r"(?:0|[1-9][0-9]*)\.[0-9]+[eE][+-]?[0-9]+)\Z"
)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_DATETIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z"
)


class StructuredRowError(ValueError):
    """Raised when a row or operation cannot be interpreted exactly."""


@dataclass(frozen=True)
class DecodedTableRow:
    search_unit_id: str
    document_id: str
    headers: tuple[str, ...]
    values: tuple[str, ...]

    def mapping(self) -> dict[str, str]:
        if len(self.headers) != len(self.values):
            raise StructuredRowError("decoded row header/value arity differs")
        return dict(zip(self.headers, self.values))


@dataclass(frozen=True)
class StructuredTableProfile:
    headers: tuple[str, ...]
    data_types: tuple[str, ...]
    row_count: int


@dataclass
class StructuredProfileAccumulator:
    """Incrementally certify that every non-header row has one stable schema."""

    headers: tuple[str, ...] | None = None
    observed_types: list[set[str]] = field(default_factory=list)
    row_count: int = 0
    invalid_reason: str | None = None

    def observe(self, unit: Mapping[str, Any]) -> None:
        if self.invalid_reason is not None or unit.get("unit_type") != "table_row":
            return
        context = unit.get("context")
        if isinstance(context, Mapping) and context.get("is_header_candidate") is True:
            return
        try:
            decoded = decode_table_row(unit)
        except StructuredRowError as exc:
            self.invalid_reason = str(exc)
            return
        if self.headers is None:
            self.headers = decoded.headers
            self.observed_types = [set() for _ in self.headers]
        elif decoded.headers != self.headers:
            self.invalid_reason = "table header schema changes between rows"
            return
        for index, value in enumerate(decoded.values):
            self.observed_types[index].add(classify_scalar(value))
        self.row_count += 1

    def finish(self) -> StructuredTableProfile | None:
        if (
            self.invalid_reason is not None
            or self.headers is None
            or self.row_count == 0
            or len(self.observed_types) != len(self.headers)
        ):
            return None
        return StructuredTableProfile(
            self.headers,
            tuple(merge_data_types(values) for values in self.observed_types),
            self.row_count,
        )


@dataclass(frozen=True)
class StructuredExecution:
    operation_values: Mapping[str, Any]
    requested_outputs: tuple[dict[str, Any], ...]
    source_search_unit_ids: tuple[str, ...]


def _surface(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def decode_table_row(unit: Mapping[str, Any]) -> DecodedTableRow:
    """Decode one exact SearchUnit row or raise without guessing."""

    if unit.get("unit_type") != "table_row":
        raise StructuredRowError("SearchUnit is not a table_row")
    context = unit.get("context")
    if not isinstance(context, Mapping):
        raise StructuredRowError("table row has no closed header context")
    if context.get("is_header_candidate") is True:
        raise StructuredRowError("header candidate is not a data row")
    raw_headers = context.get("header_labels")
    if not isinstance(raw_headers, list) or not raw_headers:
        raise StructuredRowError("table row has no header labels")
    if not all(isinstance(value, str) and _surface(value) for value in raw_headers):
        raise StructuredRowError("table row contains an invalid header label")
    headers = tuple(_surface(value) for value in raw_headers)
    if len(headers) != len(set(headers)):
        raise StructuredRowError("table row contains duplicate exact headers")
    normalized = tuple(unicodedata.normalize("NFC", value.casefold()) for value in headers)
    if len(normalized) != len(set(normalized)):
        raise StructuredRowError("table row contains normalized header collisions")

    text_record = unit.get("text")
    body = text_record.get("search_text") if isinstance(text_record, Mapping) else None
    if not isinstance(body, str) or not body:
        raise StructuredRowError("table row has no serialized text")
    heading = context.get("container_heading_text")
    if isinstance(heading, str) and heading:
        prefix = f"セクション: {heading}\n"
        if not body.startswith(prefix):
            raise StructuredRowError("table row heading prefix is inconsistent")
        body = body[len(prefix) :]
    if "\r" in body:
        raise StructuredRowError("table row uses a non-canonical line ending")
    lines = body.split("\n")
    if len(lines) != len(headers):
        raise StructuredRowError("table row is incomplete or contains multiline values")
    values: list[str] = []
    for expected_header, line in zip(headers, lines):
        prefix = f"{expected_header}: "
        if not line.startswith(prefix):
            raise StructuredRowError("table row header order or label is inconsistent")
        value = line[len(prefix) :]
        if not value:
            raise StructuredRowError("table row contains an empty structured value")
        values.append(value)
    reconstructed = "\n".join(
        f"{header}: {value}" for header, value in zip(headers, values)
    )
    if reconstructed != body:
        raise StructuredRowError("table row cannot be reconstructed exactly")
    return DecodedTableRow(
        str(unit["search_unit_id"]),
        str(unit["document_id"]),
        headers,
        tuple(values),
    )


def classify_scalar(value: str) -> str:
    if _INTEGER.fullmatch(value):
        return "integer"
    if _NUMBER.fullmatch(value):
        return "number"
    if value.casefold() in {"true", "false"}:
        return "boolean"
    if _DATETIME.fullmatch(value):
        return "datetime"
    if _DATE.fullmatch(value):
        return "date"
    return "string"


def merge_data_types(values: set[str]) -> str:
    if not values:
        return "unknown"
    if values <= {"integer"}:
        return "integer"
    if values <= {"integer", "number"}:
        return "number"
    if len(values) == 1:
        return next(iter(values))
    return "string"


def capabilities_for_profile(
    profile: StructuredTableProfile | None,
    *,
    lexical: bool,
) -> dict[str, list[str]]:
    retrieval = ["lexical"] if lexical else []
    if profile is None:
        return {
            "retrieval_channels": retrieval,
            "predicate_operators": [],
            "graph_operators": [],
        }
    retrieval.append("structured")
    predicates = ["eq", "ne"]
    graph = ["filter", "list", "project"]
    if any(value in {"integer", "number"} for value in profile.data_types):
        predicates.extend(["gt", "gte", "lt", "lte"])
        graph.extend(["argmin_all", "mean"])
    return {
        "retrieval_channels": retrieval,
        "predicate_operators": predicates,
        "graph_operators": graph,
    }


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise StructuredRowError("boolean is not a numeric scalar")
    try:
        rendered = str(value)
        if not (_INTEGER.fullmatch(rendered) or _NUMBER.fullmatch(rendered)):
            raise StructuredRowError("numeric operation received a non-numeric value")
        return Decimal(rendered)
    except (InvalidOperation, ValueError) as exc:
        raise StructuredRowError("numeric scalar is invalid") from exc


def _predicate_matches(raw: str, operator: str, expected: Any) -> bool:
    if operator in {"eq", "ne"}:
        if isinstance(expected, (int, float, Decimal)) and not isinstance(expected, bool):
            result = _decimal(raw) == _decimal(expected)
        else:
            result = raw == str(expected)
        return result if operator == "eq" else not result
    left = _decimal(raw)
    right = _decimal(expected)
    comparisons = {
        "gt": left > right,
        "gte": left >= right,
        "lt": left < right,
        "lte": left <= right,
    }
    if operator not in comparisons:
        raise StructuredRowError(f"unsupported predicate operator: {operator}")
    return comparisons[operator]


def execute_operation_graph(
    requested: Mapping[str, Any],
    rows: Sequence[DecodedTableRow],
) -> StructuredExecution:
    """Execute the certified v0.1 operation subset in exact DAG order."""

    if not rows:
        raise StructuredRowError("structured execution requires at least one row")
    headers = rows[0].headers
    if any(row.headers != headers for row in rows):
        raise StructuredRowError("structured rows do not share one schema")
    values: dict[str, Any] = {
        item["input_ref"]: tuple(rows)
        for item in requested["operation_graph"]["external_inputs"]
        if item["source"] == "scope" and item["input_type"] == "record_set"
    }
    operation_outputs: dict[str, Any] = {}
    for node in requested["operation_graph"]["nodes"]:
        inputs = [values[reference] for reference in node["input_refs"]]
        operator = node["operator"]
        if operator == "filter" and len(inputs) == 1:
            predicate = node["predicate"]
            field_name = predicate["field"]
            if field_name not in headers:
                raise StructuredRowError("filter field is absent from the row schema")
            output = tuple(
                row
                for row in inputs[0]
                if _predicate_matches(
                    row.mapping()[field_name],
                    predicate["operator"],
                    predicate["value"],
                )
            )
        elif operator == "project" and len(inputs) == 1:
            fields = node.get("fields") or []
            if len(fields) != 1 or fields[0] not in headers:
                raise StructuredRowError("v0.1 project requires one known field")
            output = tuple(row.mapping()[fields[0]] for row in inputs[0])
        elif operator == "mean" and len(inputs) == 1:
            projected = tuple(_decimal(value) for value in inputs[0])
            if not projected:
                raise StructuredRowError("mean is undefined for an empty set")
            output = sum(projected, Decimal(0)) / Decimal(len(projected))
        elif operator == "argmin_all" and len(inputs) == 2:
            candidates = values.get(node.get("candidate_set_ref"))
            scalar = next(
                (value for value in inputs if isinstance(value, Decimal)), None
            )
            if not isinstance(candidates, tuple) or scalar is None:
                raise StructuredRowError("argmin_all inputs are inconsistent")
            field_name = node.get("field")
            if field_name not in headers or node.get("tie_policy") != "all":
                raise StructuredRowError("argmin_all field or tie policy is unsupported")
            distances = [
                abs(_decimal(row.mapping()[field_name]) - scalar)
                for row in candidates
            ]
            if not distances:
                raise StructuredRowError("argmin_all is undefined for an empty set")
            minimum = min(distances)
            output = tuple(
                row
                for row, distance in zip(candidates, distances)
                if distance == minimum
            )
        else:
            raise StructuredRowError(f"unsupported graph operation: {operator}")
        values[node["output_ref"]] = output
        operation_outputs[node["operation_id"]] = output

    requested_outputs: list[dict[str, Any]] = []
    for output_contract in requested["requested_outputs"]:
        value = operation_outputs[output_contract["source_operation_ref"]]
        requested_outputs.append(
            {
                "output_id": output_contract["output_id"],
                "value": value,
            }
        )
    return StructuredExecution(
        operation_values=operation_outputs,
        requested_outputs=tuple(requested_outputs),
        source_search_unit_ids=tuple(row.search_unit_id for row in rows),
    )
