#!/usr/bin/env python3
"""Build a deterministic, question-independent Data Catalog.

Only Document, SearchUnit, and optional Evidence JSONL inputs are accepted.
Search text, row/cell values, answers, evaluation data, embeddings, and
query-specific metadata are never copied to catalog records.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Mapping, Sequence

from structured_search_units import (
    PROFILE_NAME as STRUCTURED_PROFILE_NAME,
    PROFILE_VERSION as STRUCTURED_PROFILE_VERSION,
    StructuredProfileAccumulator,
    capabilities_for_profile,
)


REPOSITORY = Path(__file__).resolve().parents[1]
SCHEMA_DIRECTORY = REPOSITORY / "schemas"
BUILDER_NAME = "data-catalog-builder"
BUILDER_VERSION = "0.2"
FIXED_EPOCH = "1970-01-01T00:00:00Z"

_FORBIDDEN_SOURCE_COMPONENTS = frozenset(
    {
        "質問回答",
        "question-answer",
        "question-answers",
        "question_answer",
        "question_answers",
    }
)
_FORBIDDEN_SOURCE_NAMES = frozenset(
    {
        "answer.csv",
        "answer.json",
        "answer.jsonl",
        "answers.csv",
        "answers.json",
        "answers.jsonl",
        "ground_truth.csv",
        "ground_truth.json",
        "ground_truth.jsonl",
        "predictions.csv",
        "predictions.json",
        "predictions.jsonl",
        "questions_test.csv",
        "questions_test.json",
        "questions_test.jsonl",
        "questions_valid.csv",
        "questions_valid.json",
        "questions_valid.jsonl",
        "submission.zip",
    }
)
_FORBIDDEN_CATALOG_KEYS = frozenset(
    {
        "alias",
        "answer",
        "answers",
        "content",
        "embedding",
        "filter_value",
        "original_question",
        "primary",
        "question",
        "questions",
        "rank",
        "raw_value",
        "relevance",
        "score",
        "search_text",
        "statistics",
        "text",
    }
)

_MEDIA_TYPES = {
    "csv": "text/csv",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "html": "text/html",
    "ipynb": "application/x-ipynb+json",
    "json": "application/json",
    "jsonl": "application/x-ndjson",
    "md": "text/markdown",
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "py": "text/x-python",
    "txt": "text/plain",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "zip": "application/zip",
}


class CatalogContractError(ValueError):
    """Raised when an input or output violates the Data Catalog contract."""


@dataclass(frozen=True)
class Limits:
    max_record_bytes: int = 16 * 1024 * 1024
    max_depth: int = 64

    def __post_init__(self) -> None:
        if self.max_record_bytes < 1:
            raise ValueError("max_record_bytes must be positive")
        if self.max_depth < 1:
            raise ValueError("max_depth must be positive")


@dataclass(frozen=True)
class InputStats:
    record_type: str
    schema_version: str
    sha256: str
    record_count: int

    def as_snapshot_input(self) -> dict[str, Any]:
        return {
            "record_type": self.record_type,
            "schema_version": self.schema_version,
            "sha256": self.sha256,
            "record_count": self.record_count,
        }


@dataclass(frozen=True)
class CompiledCatalog:
    entries: tuple[dict[str, Any], ...]
    snapshot: dict[str, Any]
    entry_stream_sha256: str
    entry_stream_record_count: int
    generated_at: str
    input_stats: tuple[InputStats, ...]


@dataclass(frozen=True)
class BuildResult:
    entry_count: int
    entry_stream_sha256: str
    snapshot_id: str
    generated_at: str
    written: bool


@dataclass
class _MutableStats:
    record_type: str
    schema_version: str = "0.1"
    record_count: int = 0
    digest: Any = field(default_factory=hashlib.sha256)

    def frozen(self) -> InputStats:
        return InputStats(
            record_type=self.record_type,
            schema_version=self.schema_version,
            sha256=self.digest.hexdigest(),
            record_count=self.record_count,
        )


@dataclass
class _FieldAccumulator:
    surface: str
    normalized: str
    ordinal: int
    source_refs: set[str] = field(default_factory=set)


@dataclass
class _GroupAccumulator:
    document_id: str
    container_kind: str
    container_name: str | None
    container_index: int | None
    source_member: str | None
    container_label_kind: str | None
    unit_ids: set[str] = field(default_factory=set)
    evidence_ids: set[str] = field(default_factory=set)
    fields: dict[tuple[int, str], _FieldAccumulator] = field(default_factory=dict)
    structured_profile: StructuredProfileAccumulator = field(
        default_factory=StructuredProfileAccumulator
    )


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _surface(value: str) -> str:
    return _nfc(value).strip()


def normalize_label(value: str) -> str:
    """Return the sole v0.1 label normalization: trim, NFC, then casefold."""

    surface = _surface(value)
    return _nfc(surface.casefold())


def _nfc_tree(value: Any) -> Any:
    if isinstance(value, str):
        return _nfc(value)
    if isinstance(value, list):
        return [_nfc_tree(item) for item in value]
    if isinstance(value, tuple):
        return [_nfc_tree(item) for item in value]
    if isinstance(value, dict):
        return {_nfc(str(key)): _nfc_tree(item) for key, item in value.items()}
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Encode canonical, finite JSON without a trailing newline."""

    normalized = _nfc_tree(value)
    try:
        text = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CatalogContractError("value is not finite canonical JSON") from exc
    return text.encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}_{_sha256_json(value)[:32]}"


def _path_parts(value: str) -> tuple[str, ...]:
    return tuple(
        part.split("#", 1)[0].split("?", 1)[0].casefold()
        for part in _nfc(value).replace("\\", "/").split("/")
        if part
    )


def _is_forbidden_source_path(value: str) -> bool:
    parts = _path_parts(value)
    return bool(
        set(parts).intersection(_FORBIDDEN_SOURCE_COMPONENTS)
        or set(parts).intersection(_FORBIDDEN_SOURCE_NAMES)
    )


def assert_safe_path(path: str | Path, *, role: str, must_exist: bool) -> Path:
    """Reject paths that could be question, answer, or evaluation inputs."""

    candidate = Path(path).expanduser()
    inspected = [str(candidate)]
    try:
        inspected.append(str(candidate.resolve(strict=must_exist)))
    except FileNotFoundError as exc:
        raise CatalogContractError(f"{role} path does not exist") from exc
    for text in inspected:
        if _is_forbidden_source_path(text):
            raise CatalogContractError(f"{role} path is forbidden by source-only policy")
    if must_exist and (not candidate.is_file() or candidate.is_symlink()):
        raise CatalogContractError(f"{role} must be a regular non-symlink file")
    return candidate


def _safe_source_relative_path(value: str, *, role: str) -> str:
    normalized = _surface(value).replace("\\", "/")
    pure = PurePosixPath(normalized)
    if not normalized or pure.is_absolute() or ".." in pure.parts:
        raise CatalogContractError(f"{role} contains an unsafe source path")
    if _is_forbidden_source_path(normalized):
        raise CatalogContractError(f"{role} contains a forbidden source path")
    return pure.as_posix()


def _safe_archive_member(value: str, *, role: str) -> str:
    """Canonicalize an archive-root member without treating it as a host path."""

    normalized = _surface(value).replace("\\", "/").lstrip("/")
    pure = PurePosixPath(normalized)
    if not normalized or pure.is_absolute() or ".." in pure.parts:
        raise CatalogContractError(f"{role} contains an unsafe archive member")
    if _is_forbidden_source_path(normalized):
        raise CatalogContractError(f"{role} contains a forbidden source path")
    return pure.as_posix()


def _reject_constant(value: str) -> None:
    raise CatalogContractError("non-finite JSON number is forbidden")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogContractError("duplicate JSON object key is forbidden")
        result[key] = value
    return result


def _assert_json_tree(value: Any, *, max_depth: int) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > max_depth:
            raise CatalogContractError("JSON nesting depth exceeds the configured limit")
        if isinstance(item, float) and not math.isfinite(item):
            raise CatalogContractError("non-finite JSON number is forbidden")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)


def _loads_strict(raw: bytes, *, source: str, limits: Limits) -> Any:
    if len(raw) > limits.max_record_bytes:
        raise CatalogContractError(f"{source}: JSON record exceeds the size limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CatalogContractError(f"{source}: input is not strict UTF-8") from exc
    if not text.strip():
        raise CatalogContractError(f"{source}: blank JSONL records are forbidden")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except CatalogContractError:
        raise
    except (json.JSONDecodeError, UnicodeError, ValueError, RecursionError, OverflowError) as exc:
        raise CatalogContractError(f"{source}: invalid JSON") from exc
    _assert_json_tree(value, max_depth=limits.max_depth)
    return value


def _jsonschema_module() -> Any:
    try:
        import jsonschema
    except ImportError as exc:
        raise CatalogContractError(
            "jsonschema is required for Draft 2020-12 validation"
        ) from exc
    return jsonschema


def _load_schema(name: str) -> dict[str, Any]:
    path = SCHEMA_DIRECTORY / name
    raw = path.read_bytes()
    value = _loads_strict(
        raw,
        source=f"schema:{name}",
        limits=Limits(max_record_bytes=4 * 1024 * 1024, max_depth=128),
    )
    if not isinstance(value, dict):
        raise CatalogContractError(f"schema:{name}: schema root must be an object")
    return value


def _validator(name: str) -> Any:
    jsonschema = _jsonschema_module()
    schema = _load_schema(name)
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.FormatChecker(),
    )


def _schema_validate(validator: Any, value: Any, *, source: str) -> None:
    errors = sorted(
        validator.iter_errors(value),
        key=lambda error: tuple(str(component) for component in error.absolute_path),
    )
    if not errors:
        return
    error = errors[0]
    location = "/" + "/".join(str(part) for part in error.absolute_path)
    raise CatalogContractError(
        f"{source}: schema {error.validator!s} violation at {location}"
    )


def _iter_jsonl(
    path: Path,
    *,
    role: str,
    record_type: str,
    validator: Any,
    limits: Limits,
) -> tuple[Iterator[tuple[int, dict[str, Any]]], _MutableStats]:
    stats = _MutableStats(record_type=record_type)

    def generate() -> Iterator[tuple[int, dict[str, Any]]]:
        try:
            handle = path.open("rb")
        except OSError as exc:
            raise CatalogContractError(f"cannot open {role}") from exc
        with handle:
            for line_number, raw in enumerate(handle, start=1):
                stats.digest.update(raw)
                value = _loads_strict(
                    raw,
                    source=f"{role}:{line_number}",
                    limits=limits,
                )
                if not isinstance(value, dict):
                    raise CatalogContractError(
                        f"{role}:{line_number}: JSONL record must be an object"
                    )
                _schema_validate(
                    validator,
                    value,
                    source=f"{role}:{line_number}",
                )
                if value.get("record_type") != record_type:
                    raise CatalogContractError(
                        f"{role}:{line_number}: record_type does not match the input role"
                    )
                stats.record_count += 1
                yield line_number, value
        if stats.record_count == 0:
            raise CatalogContractError(f"{role}: empty JSONL input is forbidden")

    return generate(), stats


def _normalize_datetime(value: str) -> tuple[datetime, str]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError as exc:
        raise CatalogContractError("generated_at must be an RFC 3339 date-time") from exc
    if parsed.tzinfo is None:
        raise CatalogContractError("generated_at must include a timezone")
    utc = parsed.astimezone(timezone.utc)
    if utc.microsecond:
        rendered = utc.isoformat(timespec="microseconds").replace("+00:00", "Z")
    else:
        rendered = utc.isoformat(timespec="seconds").replace("+00:00", "Z")
    return utc, rendered


def _deterministic_generated_at(
    timestamps: Iterable[str], explicit: str | None
) -> str:
    if explicit is not None:
        return _normalize_datetime(explicit)[1]
    parsed = [_normalize_datetime(value) for value in timestamps]
    if not parsed:
        return FIXED_EPOCH
    return max(parsed, key=lambda pair: pair[0])[1]


def _source_identity(document: Mapping[str, Any]) -> dict[str, Any]:
    source = document["source"]
    relative_path = _safe_source_relative_path(
        source["relative_path"], role="Document.source.relative_path"
    )
    file_name = _surface(source["file_name"])
    if not file_name or PurePosixPath(relative_path).name != file_name:
        raise CatalogContractError(
            "Document source file_name must equal the relative_path basename"
        )
    _safe_source_relative_path(file_name, role="Document.source.file_name")
    extension = _surface(source["extension"]).casefold()
    media_type = _surface(source.get("media_type", ""))
    if not media_type:
        media_type = _MEDIA_TYPES.get(extension, "application/octet-stream")
    return {
        "relative_path": relative_path,
        "file_name": file_name,
        "extension": extension,
        "media_type": media_type,
        "sha256": source["sha256"],
    }


def _container_for_unit(
    unit: Mapping[str, Any],
) -> tuple[str, str | None, int | None, str | None, str | None]:
    locator = unit["locator"]
    context = unit.get("context") or {}
    source_member_raw = locator.get("source_member")
    source_member = (
        _safe_archive_member(source_member_raw, role="SearchUnit.locator.source_member")
        if isinstance(source_member_raw, str)
        else None
    )
    sheet_raw = locator.get("sheet_name")
    sheet = _surface(sheet_raw) if isinstance(sheet_raw, str) else None
    if "table_index" in locator:
        return "table", sheet, locator["table_index"], source_member, (
            "worksheet_name" if sheet else None
        )
    if sheet:
        if unit["unit_type"] == "table_row" or context.get("container_kind") == "table":
            return "table", sheet, None, source_member, "worksheet_name"
        return "worksheet", sheet, None, source_member, "worksheet_name"
    if "slide_number" in locator:
        return "slide", None, locator["slide_number"], source_member, None
    if "page_number" in locator:
        return "page", None, locator["page_number"], source_member, None
    if source_member:
        return "archive_member", source_member, None, source_member, "path_component"
    return "document", None, None, None, None


def _group_key(
    document_id: str,
    container: tuple[str, str | None, int | None, str | None, str | None],
) -> tuple[Any, ...]:
    kind, name, index, source_member, _ = container
    return document_id, kind, name, index, source_member


def _add_header_fields(
    group: _GroupAccumulator,
    unit: Mapping[str, Any],
    *,
    evidence_refs_verified: bool,
) -> None:
    context = unit.get("context") or {}
    labels = context.get("header_labels")
    evidence_ids = context.get("header_evidence_ids")
    if not isinstance(labels, list) or not isinstance(evidence_ids, list):
        return
    if not labels or not evidence_ids:
        return
    # When no Evidence stream is supplied, keep the provenance closed over the
    # actual inputs by citing the SearchUnit that declares the header metadata.
    # Evidence IDs are retained only after their existence and document
    # membership have been verified against the optional Evidence input.
    all_refs = (
        {str(item) for item in evidence_ids}
        if evidence_refs_verified
        else {str(unit["search_unit_id"])}
    )
    for ordinal, raw_label in enumerate(labels):
        if not isinstance(raw_label, str):
            raise CatalogContractError("SearchUnit header label must be a string")
        surface = _surface(raw_label)
        if not surface:
            continue
        normalized = normalize_label(surface)
        refs = (
            {str(evidence_ids[ordinal])}
            if evidence_refs_verified and len(evidence_ids) == len(labels)
            else set(all_refs)
        )
        # One exact, deterministic source is sufficient to prove the field
        # declaration.  Repeating every row-level SearchUnit here can turn a
        # small schema catalog into hundreds of megabytes; full assignment
        # coverage remains bound separately by assigned_search_units_sha256.
        refs = {min(refs)}
        key = (ordinal, normalized)
        current = group.fields.get(key)
        if current is None:
            group.fields[key] = _FieldAccumulator(
                surface=surface,
                normalized=normalized,
                ordinal=ordinal,
                source_refs=refs,
            )
        else:
            if canonical_json_bytes(surface) < canonical_json_bytes(current.surface):
                current.surface = surface
            current.source_refs = {min(current.source_refs.union(refs))}


def _availability(extraction_status: str, lexical: bool) -> dict[str, Any]:
    if lexical:
        if extraction_status not in {"success", "partial"}:
            raise CatalogContractError(
                "SearchUnit exists for a Document whose extraction is unavailable"
            )
        return {
            "extraction_status": extraction_status,
            "searchable": True,
            "reason_codes": ["extraction_partial"] if extraction_status == "partial" else [],
        }
    reason_by_status = {
        "pending": "extraction_pending",
        "partial": "extraction_partial",
        "deferred": "extraction_deferred",
        "failed": "extraction_failed",
        "success": "no_searchable_content",
    }
    return {
        "extraction_status": extraction_status,
        "searchable": False,
        "reason_codes": [reason_by_status[extraction_status]],
    }


def _label_cores(
    document: Mapping[str, Any],
    group: _GroupAccumulator,
) -> list[dict[str, Any]]:
    identity = _source_identity(document)
    relative = PurePosixPath(identity["relative_path"])
    cores: list[dict[str, Any]] = []

    def add(role: str, surface: str, source_kind: str, refs: Iterable[str]) -> None:
        clean = _surface(surface)
        if not clean:
            return
        core = {
            "role": role,
            "surface": clean,
            "normalized": normalize_label(clean),
            "source_kind": source_kind,
            "source_refs": sorted(set(refs)),
        }
        if core not in cores:
            cores.append(core)

    for component in relative.parts[:-1]:
        if component not in {"", "."}:
            add("location", component, "path_component", [document["document_id"]])
    add("container", identity["file_name"], "file_name", [document["document_id"]])
    stem = PurePosixPath(identity["file_name"]).stem
    if stem:
        add("container", stem, "file_stem", [document["document_id"]])
    if group.container_kind != "document" and group.container_name and group.container_label_kind:
        refs: list[str] = [min(group.unit_ids)] if group.unit_ids else [document["document_id"]]
        add("container", group.container_name, group.container_label_kind, refs)
    return sorted(
        cores,
        key=lambda item: (
            item["role"],
            item["normalized"],
            item["source_kind"],
            item["surface"],
        ),
    )


def _field_cores(group: _GroupAccumulator) -> list[dict[str, Any]]:
    profile = group.structured_profile.finish()
    inferred_types = (
        {
            (index, normalize_label(header)): profile.data_types[index]
            for index, header in enumerate(profile.headers)
        }
        if profile is not None
        else {}
    )
    return [
        {
            "surface": item.surface,
            "normalized": item.normalized,
            "ordinal": item.ordinal,
            "data_type": inferred_types.get(
                (item.ordinal, item.normalized), "unknown"
            ),
            "unit": None,
            "source_refs": sorted(item.source_refs),
        }
        for item in sorted(
            group.fields.values(),
            key=lambda value: (value.ordinal, value.normalized, value.surface),
        )
    ]


def _build_entry(
    document: Mapping[str, Any],
    group: _GroupAccumulator,
    *,
    generated_at: str,
    parent_entry_ref: str | None,
) -> dict[str, Any]:
    identity = _source_identity(document)
    lexical = bool(group.unit_ids)
    structured_profile = group.structured_profile.finish()
    capabilities = capabilities_for_profile(structured_profile, lexical=lexical)
    availability = _availability(document["extraction"]["status"], lexical)
    address = {
        "container_kind": group.container_kind,
        "container_name": (
            identity["file_name"]
            if group.container_kind == "document"
            else group.container_name
        ),
        "container_index": group.container_index,
        "source_member": group.source_member,
        "parent_entry_ref": parent_entry_ref,
    }
    label_cores = _label_cores(document, group)
    field_cores = _field_cores(group)
    compact_input_refs = {document["document_id"]}
    if group.unit_ids:
        compact_input_refs.add(min(group.unit_ids))
    for field_core in field_cores:
        compact_input_refs.update(field_core["source_refs"])
    unit_set_sha256 = _sha256_json(sorted(group.unit_ids))
    entry_identity = {
        "schema_version": "0.1",
        "builder": BUILDER_NAME,
        "builder_version": BUILDER_VERSION,
        "document_id": document["document_id"],
        "source_identity": identity,
        "address": address,
        "scope_labels": label_cores,
        "fields": field_cores,
        "capabilities": capabilities,
        "availability": availability,
        "input_refs": sorted(compact_input_refs),
        "assigned_search_units_sha256": unit_set_sha256,
    }
    entry_id = _stable_id("dce", entry_identity)
    labels = [
        {
            "label_id": _stable_id(
                "dcl", {"data_catalog_entry_id": entry_id, **core}
            ),
            **core,
        }
        for core in label_cores
    ]
    fields = [
        {
            "field_id": _stable_id(
                "dcf", {"data_catalog_entry_id": entry_id, **core}
            ),
            **core,
        }
        for core in field_cores
    ]
    return {
        "schema_version": "0.1",
        "record_type": "data_catalog_entry",
        "data_catalog_entry_id": entry_id,
        "document_id": document["document_id"],
        "source_identity": identity,
        "address": address,
        "scope_labels": labels,
        "fields": fields,
        "capabilities": capabilities,
        "availability": availability,
        "provenance": {
            "builder": BUILDER_NAME,
            "builder_version": BUILDER_VERSION,
            "generated_at": generated_at,
            "deterministic": True,
            "question_independent": True,
            "source_data_used": True,
            "question_data_used": False,
            "answer_data_used": False,
            "past_answers_used": False,
            "raw_values_embedded": False,
            "input_refs": sorted(compact_input_refs),
        },
    }


def _entry_stream_metadata(entries: Sequence[Mapping[str, Any]]) -> tuple[str, int]:
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(canonical_json_bytes(entry))
        digest.update(b"\n")
    return digest.hexdigest(), len(entries)


def _entry_stream_relative_path(entries_path: Path, snapshot_path: Path) -> str:
    try:
        relative = os.path.relpath(
            entries_path.absolute(),
            start=snapshot_path.parent.absolute(),
        )
    except ValueError as exc:
        raise CatalogContractError("cannot derive the entry stream relative path") from exc
    return _nfc(relative.replace(os.sep, "/"))


def _build_config_sha256() -> str:
    return _sha256_json(
        {
            "builder": BUILDER_NAME,
            "builder_version": BUILDER_VERSION,
            "capability_policy": "lexical_and_certified_structured_rows_v0_1",
            "structured_profile": {
                "name": STRUCTURED_PROFILE_NAME,
                "version": STRUCTURED_PROFILE_VERSION,
                "policy": "exact_complete_header_value_rows_only",
            },
            "field_policy": "header_labels_with_header_evidence_only",
            "grouping_policy": "table_index_then_sheet_then_slide_then_page_then_member_v0_1",
            "normalization": "trim_nfc_casefold",
            "raw_values_embedded": False,
        }
    )


def _assert_no_catalog_leakage(value: Any) -> None:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            forbidden = _FORBIDDEN_CATALOG_KEYS.intersection(item)
            if forbidden:
                raise CatalogContractError("forbidden question/value metadata reached catalog output")
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def _validate_entry_semantics(
    entries: Sequence[dict[str, Any]],
    *,
    documents: Mapping[str, Mapping[str, Any]],
    source_refs: set[str],
) -> None:
    entry_ids: set[str] = set()
    label_ids: set[str] = set()
    field_ids: set[str] = set()
    for entry in entries:
        _assert_no_catalog_leakage(entry)
        entry_id = entry["data_catalog_entry_id"]
        if entry_id in entry_ids:
            raise CatalogContractError("duplicate DataCatalogEntry ID")
        entry_ids.add(entry_id)
        document = documents.get(entry["document_id"])
        if document is None or entry["source_identity"] != _source_identity(document):
            raise CatalogContractError("DataCatalogEntry source identity mismatch")
        for ref in entry["provenance"]["input_refs"]:
            if ref not in source_refs:
                raise CatalogContractError("DataCatalogEntry provenance reference is missing")
        for label in entry["scope_labels"]:
            if label["label_id"] in label_ids:
                raise CatalogContractError("duplicate DataCatalog label ID")
            label_ids.add(label["label_id"])
            if label["normalized"] != normalize_label(label["surface"]):
                raise CatalogContractError("DataCatalog label normalization mismatch")
            if not set(label["source_refs"]).issubset(source_refs):
                raise CatalogContractError("DataCatalog label reference is missing")
        for catalog_field in entry["fields"]:
            if catalog_field["field_id"] in field_ids:
                raise CatalogContractError("duplicate DataCatalog field ID")
            field_ids.add(catalog_field["field_id"])
            if catalog_field["normalized"] != normalize_label(catalog_field["surface"]):
                raise CatalogContractError("DataCatalog field normalization mismatch")
            if not set(catalog_field["source_refs"]).issubset(source_refs):
                raise CatalogContractError("DataCatalog field reference is missing")
    for entry in entries:
        parent = entry["address"]["parent_entry_ref"]
        if parent is not None and parent not in entry_ids:
            raise CatalogContractError("DataCatalog parent entry reference is missing")


def _load_source_records(
    documents_path: Path,
    search_units_path: Path,
    evidence_path: Path | None,
    *,
    limits: Limits,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[tuple[Any, ...], _GroupAccumulator],
    set[str],
    tuple[InputStats, ...],
    list[str],
]:
    document_validator = _validator("document.schema.json")
    search_validator = _validator("search-unit.schema.json")
    evidence_validator = _validator("evidence.schema.json") if evidence_path else None

    documents: dict[str, dict[str, Any]] = {}
    timestamps: list[str] = []
    document_iter, document_stats = _iter_jsonl(
        documents_path,
        role="documents",
        record_type="document",
        validator=document_validator,
        limits=limits,
    )
    for _, document in document_iter:
        document_id = document["document_id"]
        if document_id in documents:
            raise CatalogContractError("duplicate Document ID")
        _source_identity(document)
        archive_path = document["source"].get("archive_relative_path")
        if archive_path is not None:
            _safe_source_relative_path(
                archive_path,
                role="Document.source.archive_relative_path",
            )
        documents[document_id] = document
        timestamps.append(document["extraction"]["extracted_at"])

    evidence_ids: set[str] = set()
    evidence_documents: dict[str, str] = {}
    input_stats: list[InputStats] = [document_stats.frozen()]
    if evidence_path is not None and evidence_validator is not None:
        evidence_iter, evidence_stats = _iter_jsonl(
            evidence_path,
            role="evidence",
            record_type="evidence",
            validator=evidence_validator,
            limits=limits,
        )
        for _, evidence in evidence_iter:
            evidence_id = evidence["evidence_id"]
            if evidence_id in evidence_ids:
                raise CatalogContractError("duplicate Evidence ID")
            if evidence["document_id"] not in documents:
                raise CatalogContractError("Evidence references an unknown Document")
            evidence_ids.add(evidence_id)
            evidence_documents[evidence_id] = evidence["document_id"]
        input_stats.append(evidence_stats.frozen())

    groups: dict[tuple[Any, ...], _GroupAccumulator] = {}
    for document in documents.values():
        identity = _source_identity(document)
        group = _GroupAccumulator(
            document_id=document["document_id"],
            container_kind="document",
            container_name=identity["file_name"],
            container_index=None,
            source_member=None,
            container_label_kind="file_name",
        )
        groups[_group_key(document["document_id"], ("document", None, None, None, None))] = group

    search_ids: set[str] = set()
    search_iter, search_stats = _iter_jsonl(
        search_units_path,
        role="search_units",
        record_type="search_unit",
        validator=search_validator,
        limits=limits,
    )
    for _, unit in search_iter:
        unit_id = unit["search_unit_id"]
        if unit_id in search_ids:
            raise CatalogContractError("duplicate SearchUnit ID")
        search_ids.add(unit_id)
        document_id = unit["document_id"]
        if document_id not in documents:
            raise CatalogContractError("SearchUnit references an unknown Document")
        text = unit["text"]
        if len(text["search_text"]) != text["char_count"]:
            raise CatalogContractError("SearchUnit char_count does not match search_text")
        if hashlib.sha256(text["search_text"].encode("utf-8")).hexdigest() != text["sha256"]:
            raise CatalogContractError("SearchUnit text SHA-256 mismatch")
        timestamps.append(unit["provenance"]["generated_at"])
        all_evidence_refs = set(unit["source_evidence_ids"])
        context = unit.get("context") or {}
        all_evidence_refs.update(context.get("header_evidence_ids") or [])
        all_evidence_refs.update(context.get("container_heading_evidence_ids") or [])
        if evidence_path is not None:
            missing = all_evidence_refs.difference(evidence_ids)
            if missing:
                raise CatalogContractError("SearchUnit references missing Evidence")
            if any(evidence_documents[ref] != document_id for ref in all_evidence_refs):
                raise CatalogContractError("SearchUnit crosses Document/Evidence boundaries")
        container = _container_for_unit(unit)
        key = _group_key(document_id, container)
        group = groups.get(key)
        if group is None:
            group = _GroupAccumulator(
                document_id=document_id,
                container_kind=container[0],
                container_name=container[1],
                container_index=container[2],
                source_member=container[3],
                container_label_kind=container[4],
            )
            groups[key] = group
        group.unit_ids.add(unit_id)
        group.evidence_ids.update(unit["source_evidence_ids"])
        group.structured_profile.observe(unit)
        _add_header_fields(
            group,
            unit,
            evidence_refs_verified=evidence_path is not None,
        )
    input_stats.append(search_stats.frozen())

    known_refs = set(documents).union(search_ids).union(evidence_ids)
    return (
        documents,
        groups,
        known_refs,
        tuple(sorted(input_stats, key=lambda item: item.record_type)),
        timestamps,
    )


def compile_data_catalog(
    documents_path: str | Path,
    search_units_path: str | Path,
    entries_path: str | Path,
    snapshot_path: str | Path,
    *,
    evidence_path: str | Path | None = None,
    generated_at: str | None = None,
    limits: Limits = Limits(),
) -> CompiledCatalog:
    """Compile and validate catalog records without writing output files."""

    documents_input = assert_safe_path(
        documents_path, role="documents", must_exist=True
    )
    search_input = assert_safe_path(
        search_units_path, role="search_units", must_exist=True
    )
    evidence_input = (
        assert_safe_path(evidence_path, role="evidence", must_exist=True)
        if evidence_path is not None
        else None
    )
    entries_output = assert_safe_path(
        entries_path, role="entries output", must_exist=False
    )
    snapshot_output = assert_safe_path(
        snapshot_path, role="snapshot output", must_exist=False
    )
    resolved_inputs = {
        path.resolve() for path in (documents_input, search_input, evidence_input) if path
    }
    if entries_output.absolute() == snapshot_output.absolute():
        raise CatalogContractError("entries and snapshot outputs must be different files")
    for output in (entries_output, snapshot_output):
        if output.resolve(strict=False) in resolved_inputs:
            raise CatalogContractError("output path must not overwrite a source input")

    documents, groups, known_refs, input_stats, timestamps = _load_source_records(
        documents_input,
        search_input,
        evidence_input,
        limits=limits,
    )
    deterministic_time = _deterministic_generated_at(timestamps, generated_at)

    document_entry_ids: dict[str, str] = {}
    entries: list[dict[str, Any]] = []
    for document_id in sorted(documents):
        document_group = groups[
            _group_key(document_id, ("document", None, None, None, None))
        ]
        entry = _build_entry(
            documents[document_id],
            document_group,
            generated_at=deterministic_time,
            parent_entry_ref=None,
        )
        document_entry_ids[document_id] = entry["data_catalog_entry_id"]
        entries.append(entry)

    child_groups = [
        group for group in groups.values() if group.container_kind != "document"
    ]
    for group in sorted(
        child_groups,
        key=lambda item: (
            item.document_id,
            item.container_kind,
            item.container_name or "",
            item.container_index if item.container_index is not None else -1,
            item.source_member or "",
        ),
    ):
        entries.append(
            _build_entry(
                documents[group.document_id],
                group,
                generated_at=deterministic_time,
                parent_entry_ref=document_entry_ids[group.document_id],
            )
        )
    entries.sort(key=lambda item: item["data_catalog_entry_id"])

    entry_validator = _validator("data-catalog-entry.schema.json")
    for index, entry in enumerate(entries):
        _schema_validate(entry_validator, entry, source=f"entry:{index + 1}")
    _validate_entry_semantics(entries, documents=documents, source_refs=known_refs)
    entry_sha256, entry_count = _entry_stream_metadata(entries)

    entry_stream = {
        "format": "jsonl",
        "relative_path": _entry_stream_relative_path(entries_output, snapshot_output),
        "sha256": entry_sha256,
        "record_count": entry_count,
        "sort_key": "data_catalog_entry_id",
        "canonicalization": "utf8_nfc_canonical_json_per_line_lf",
    }
    snapshot_inputs = [stats.as_snapshot_input() for stats in input_stats]
    build_config_sha256 = _build_config_sha256()
    snapshot_identity = {
        "schema_version": "0.1",
        "entry_schema_version": "0.1",
        "entry_stream": {
            key: value for key, value in entry_stream.items() if key != "relative_path"
        },
        "inputs": snapshot_inputs,
        "build_config_sha256": build_config_sha256,
        "builder": BUILDER_NAME,
        "builder_version": BUILDER_VERSION,
    }
    snapshot = {
        "schema_version": "0.1",
        "record_type": "data_catalog_snapshot",
        "data_catalog_snapshot_id": _stable_id("dcs", snapshot_identity),
        "entry_schema_version": "0.1",
        "entry_stream": entry_stream,
        "inputs": snapshot_inputs,
        "build_config_sha256": build_config_sha256,
        "provenance": {
            "builder": BUILDER_NAME,
            "builder_version": BUILDER_VERSION,
            "generated_at": deterministic_time,
            "deterministic": True,
            "question_independent": True,
            "source_data_used": True,
            "question_data_used": False,
            "answer_data_used": False,
            "past_answers_used": False,
            "raw_values_embedded": False,
        },
    }
    _schema_validate(
        _validator("data-catalog-snapshot.schema.json"),
        snapshot,
        source="snapshot",
    )
    _assert_no_catalog_leakage(snapshot)
    return CompiledCatalog(
        entries=tuple(entries),
        snapshot=snapshot,
        entry_stream_sha256=entry_sha256,
        entry_stream_record_count=entry_count,
        generated_at=deterministic_time,
        input_stats=input_stats,
    )


def _atomic_write(path: Path, chunks: Iterable[bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            for chunk in chunks:
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


def build_data_catalog(
    documents_path: str | Path,
    search_units_path: str | Path,
    entries_path: str | Path,
    snapshot_path: str | Path,
    *,
    evidence_path: str | Path | None = None,
    generated_at: str | None = None,
    write: bool = True,
    limits: Limits = Limits(),
) -> BuildResult:
    """Compile, fully validate, and atomically write a Data Catalog."""

    compiled = compile_data_catalog(
        documents_path,
        search_units_path,
        entries_path,
        snapshot_path,
        evidence_path=evidence_path,
        generated_at=generated_at,
        limits=limits,
    )
    if write:
        entries_output = Path(entries_path).expanduser()
        snapshot_output = Path(snapshot_path).expanduser()
        _atomic_write(
            entries_output,
            (
                canonical_json_bytes(entry) + b"\n"
                for entry in compiled.entries
            ),
        )
        # The snapshot is the commit marker and is replaced only after entries.
        _atomic_write(
            snapshot_output,
            [canonical_json_bytes(compiled.snapshot) + b"\n"],
        )
    return BuildResult(
        entry_count=compiled.entry_stream_record_count,
        entry_stream_sha256=compiled.entry_stream_sha256,
        snapshot_id=compiled.snapshot["data_catalog_snapshot_id"],
        generated_at=compiled.generated_at,
        written=write,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build deterministic source-only DataCatalogEntry JSONL and Snapshot JSON."
    )
    parser.add_argument("--documents", required=True, type=Path)
    parser.add_argument("--search-units", required=True, type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--entries-out", required=True, type=Path)
    parser.add_argument("--snapshot-out", required=True, type=Path)
    parser.add_argument("--generated-at")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-record-bytes", type=int, default=Limits().max_record_bytes)
    parser.add_argument("--max-depth", type=int, default=Limits().max_depth)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = build_data_catalog(
            arguments.documents,
            arguments.search_units,
            arguments.entries_out,
            arguments.snapshot_out,
            evidence_path=arguments.evidence,
            generated_at=arguments.generated_at,
            write=not arguments.dry_run,
            limits=Limits(
                max_record_bytes=arguments.max_record_bytes,
                max_depth=arguments.max_depth,
            ),
        )
    except (CatalogContractError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "entry_count": result.entry_count,
                "entry_stream_sha256": result.entry_stream_sha256,
                "snapshot_id": result.snapshot_id,
                "generated_at": result.generated_at,
                "written": result.written,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
