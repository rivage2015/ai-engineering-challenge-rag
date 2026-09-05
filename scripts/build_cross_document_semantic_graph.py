#!/usr/bin/env python3
"""Build a question-independent, cross-document semantic graph snapshot.

Only Layer 1 adapter ``semantic-documents.jsonl`` and Content Security Gate
``safe-answer-evidence.jsonl`` records are accepted.  The builder deliberately
has no question, fixture, expected-answer, or gold input.  It promotes only
facts that are explicit in labelled fields, structured rows, approved identity
registers, or explicit version links.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "0.1"
BUILDER_NAME = "cross-document-semantic-graph-builder"
BUILDER_VERSION = "0.3.0"
PROVISIONAL_MARKER = "[暫定読取]"
VISUAL_QUALITY_SOURCE_TYPES = frozenset({"ocr_line", "visual_observation"})
VISUAL_QUALITY_UNIT_TYPES = frozenset({"image_text_packet"})
VISUAL_QUALITY_METHODS = frozenset({
    "adaptive_local_ocr_provisional",
    "dual_local_ocr_consensus",
    "local_vlm_unlocated_transcript_provisional",
    "local_vlm_visual_observation_provisional",
})
NODE_TYPES = {
    "Project", "ProjectAlias", "Work", "WorkName", "Employee", "Person",
    "Claim", "Reason",
}
RELATION_TYPES = {
    "HAS_ALIAS", "CONTAINS_WORK", "HAS_NAME", "ASSIGNED_TO",
    "IDENTIFIES_PERSON", "HAS_CLAIM", "HAS_CURRENT_CLAIM",
    "CLAIMS_ASSIGNEE", "SUPERSEDES", "CONTRADICTS", "HAS_CHANGE_REASON",
}


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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", str(value)).replace("\x00", "")
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _compact(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return re.sub(r"[\s_\-:/\uff0f・.]+", "", text)


def _identity_key(value: object) -> str:
    """Normalize identity comparisons without deleting meaningful punctuation."""
    return unicodedata.normalize("NFKC", str(value)).casefold().strip()


def _read_jsonl(path: Path, input_kind: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid_jsonl:{path}:{line_number}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"invalid_{input_kind}_record:{path}:{line_number}")
            required = (
                {"document_id", "source", "evidence_ids", "status"}
                if input_kind == "document"
                else {"evidence_id", "document_id", "source", "locator", "observed_text", "adapter", "status"}
            )
            if not required <= set(record):
                raise ValueError(f"invalid_{input_kind}_record:{path}:{line_number}")
            records.append(record)
    if not records:
        raise ValueError(f"empty_{input_kind}_input:{path}")
    return records


HEADER_ALIASES: dict[str, frozenset[str]] = {
    "record_id": frozenset({"recordid", "assignmentid", "レコードid", "割当id"}),
    "project_id": frozenset({"projectid", "プロジェクトid", "案件id"}),
    "project_name": frozenset({"正式名称", "projectname", "案件名", "プロジェクト名"}),
    "work_id": frozenset({"workid", "taskid", "業務id", "作業id"}),
    "work_name": frozenset({"workname", "taskname", "業務名", "作業名"}),
    "role": frozenset({"role", "役割", "担当区分"}),
    "assignee_id": frozenset({"assigneeid", "assignedemployeeid", "担当者id", "担当社員id"}),
    "employee_id": frozenset({"employeeid", "staffid", "社員id", "職員id"}),
    "person_name": frozenset({"name", "fullname", "personname", "氏名", "社員名", "職員名"}),
    "valid_from": frozenset({"validfrom", "startdate", "有効開始日", "担当開始日"}),
    "valid_to": frozenset({"validto", "enddate", "有効終了日", "担当終了日"}),
    "effective_from": frozenset({"effectivefrom", "effectivedate", "適用開始日", "発効日"}),
    "status": frozenset({"status", "state", "状態", "ステータス", "登録状態"}),
    "version": frozenset({"version", "版", "バージョン"}),
    "supersedes": frozenset({"supersedes", "replaces", "置換対象", "後継元", "旧版"}),
    "change_reason": frozenset({"changereason", "reasonforchange", "変更理由", "改定理由"}),
    "register_version": frozenset({"registerversion", "registryversion", "名簿版", "台帳版"}),
    "document_status": frozenset({"documentstatus", "文書状態", "署名状態"}),
}


def _header_key(value: object) -> str | None:
    compact = _compact(value)
    if re.fullmatch(r"alias\d*", compact) or re.fullmatch(r"(?:別名|別表記)\d*", compact):
        return "project_alias"
    for field_name, aliases in HEADER_ALIASES.items():
        if compact in aliases:
            return field_name
    return None


def _normalized_date(value: object) -> str | None:
    text = unicodedata.normalize("NFKC", str(value)).strip()
    match = re.search(r"(?<!\d)(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?!\d)", text)
    if match is None:
        match = re.search(r"(?<!\d)(20\d{2})年(\d{1,2})月(\d{1,2})日", text)
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return None


def _normalized_version(value: object) -> str | None:
    text = unicodedata.normalize("NFKC", str(value))
    matches = re.findall(r"(?<![A-Za-z0-9])v\s*(\d+(?:\.\d+)*)(?![A-Za-z0-9])", text, re.I)
    if not matches:
        return None
    return "v" + matches[-1]


def _status_text(value: object) -> str:
    return re.sub(r"\s*/\s*", "/", _clean_text(value))


def _is_current_status(value: object) -> bool:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    if any(marker in text for marker in ("draft", "not approved", "unapproved", "未承認", "下書き")):
        return False
    return any(marker in text for marker in ("approved", "final", "finalized", "承認済", "確定", "最終", "署名済"))


def _is_draft_status(value: object) -> bool:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return any(marker in text for marker in ("draft", "not approved", "unapproved", "未承認", "下書き"))


def _is_active_row_status(value: object) -> bool:
    text = unicodedata.normalize("NFKC", str(value)).casefold().strip()
    if any(marker in text for marker in (
        "invalid", "inactive", "rejected", "draft", "not approved",
        "unapproved", "無効", "失効", "却下", "未承認",
    )):
        return False
    return any(marker in text for marker in ("valid", "active", "approved", "final", "有効", "承認済", "確定"))


IDENTITY_ROW_STATUS_PHRASES = frozenset({
    "approved", "not approved", "unapproved", "active", "inactive",
    "valid", "invalid", "final", "finalized", "rejected", "draft",
    "承認済", "未承認", "有効", "無効", "失効", "却下", "確定", "最終",
    "署名済", "下書き",
})


def _normalized_identity_status(value: object) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", str(value)).casefold().split()
    )


def _identity_status_state(value: object) -> str | None:
    normalized = _normalized_identity_status(value)
    if normalized not in IDENTITY_ROW_STATUS_PHRASES:
        return None
    return "active" if _is_active_row_status(value) else "inactive"


def _whitespace_identity_values(
    fields: Sequence[str], data_line: str
) -> list[str] | None:
    """Recover a text row only when its column boundaries remain provable.

    A single space cannot distinguish a multi-word name/status from adjacent
    columns.  Surplus tokens therefore require preserved delimiters (a tab or
    two-or-more spaces); an allowlisted suffix alone is not enough evidence.
    """
    tokens = data_line.split()
    if len(tokens) < len(fields):
        return None
    if len(tokens) == len(fields):
        values = list(tokens)
    else:
        values = [
            value.strip()
            for value in re.split(r"(?: {2,}|\t+)", data_line.strip())
            if value.strip()
        ]
        if len(values) != len(fields):
            return None
    if "status" in fields:
        status = values[list(fields).index("status")]
        if _normalized_identity_status(status) not in IDENTITY_ROW_STATUS_PHRASES:
            return None
    if any(not value for value in values):
        return None
    return values


def _role_key(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold().strip()
    if "主担当" in text or re.search(r"\b(?:main|primary)\b", text):
        return "main-assignee"
    slug = re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", "-", text).strip("-")
    return slug or "assignee"


def _looks_identifier(value: object) -> bool:
    text = _clean_text(value)
    return bool(
        text
        and len(text) <= 120
        and re.search(r"[0-9]", text)
        and re.fullmatch(r"[A-Za-z0-9_.:/\-]+", unicodedata.normalize("NFKC", text))
    )


def _looks_person_name(value: object) -> bool:
    text = _clean_text(value)
    return bool(
        text
        and len(text) <= 100
        and ":" not in text
        and "：" not in text
        and not _looks_identifier(text)
        and _header_key(text) is None
        and any(character.isalpha() or "\u3040" <= character <= "\u9fff" for character in text)
    )


@dataclass(frozen=True)
class EvidenceView:
    evidence_id: str
    document_id: str
    relative_path: str
    source_sha256: str
    evidence_type: str
    location: dict[str, Any]
    observed_text: str
    observed_sha256: str
    text: str
    ordinal: int
    geometry: dict[str, Any] | None
    quality_disposition: str = "eligible_native"


@dataclass(frozen=True)
class FieldValue:
    value: str
    evidence_ids: tuple[str, ...]


@dataclass
class DocumentFacts:
    document_id: str
    relative_path: str
    extension: str
    fields: dict[str, list[FieldValue]] = field(default_factory=lambda: defaultdict(list))

    def add(self, field_name: str, value: object, evidence_ids: Iterable[str]) -> None:
        rendered = _clean_text(value)
        if field_name in {"status", "document_status", "claim_status_marker"}:
            rendered = _status_text(rendered)
        ids = tuple(sorted(set(evidence_ids)))
        if not rendered or not ids:
            return
        candidate = FieldValue(rendered, ids)
        if candidate not in self.fields[field_name]:
            self.fields[field_name].append(candidate)

    def unique(self, field_name: str) -> FieldValue | None:
        values = self.fields.get(field_name, [])
        by_value: dict[str, set[str]] = defaultdict(set)
        rendered: dict[str, str] = {}
        for item in values:
            key = unicodedata.normalize("NFKC", item.value).casefold().strip()
            by_value[key].update(item.evidence_ids)
            rendered.setdefault(key, item.value)
        if len(by_value) != 1:
            return None
        key = next(iter(by_value))
        return FieldValue(rendered[key], tuple(sorted(by_value[key])))


@dataclass(frozen=True)
class StructuredRow:
    document_id: str
    container_key: tuple[Any, ...]
    row_index: int
    fields: Mapping[str, FieldValue]

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(sorted({item for value in self.fields.values() for item in value.evidence_ids}))


@dataclass
class ClaimFact:
    document_id: str
    project_id: str
    work_id: str
    role: str
    role_key: str
    assignee_id: str
    effective_from: str
    status: str
    version: str
    current: bool
    canonical_key: str
    row_evidence_ids: tuple[str, ...]
    assignee_evidence_ids: tuple[str, ...]
    status_evidence_ids: tuple[str, ...]


class GraphAccumulator:
    def __init__(self) -> None:
        self.nodes: dict[tuple[str, str], dict[str, Any]] = {}
        self.edges: dict[tuple[Any, ...], dict[str, Any]] = {}

    def add_node(self, node_type: str, canonical_key: object, properties: Mapping[str, Any] | None = None) -> None:
        key = _clean_text(canonical_key)
        if node_type not in NODE_TYPES or not key:
            raise ValueError(f"invalid_node:{node_type}:{key!r}")
        props = dict(properties or {})
        identity = (node_type, key)
        existing = self.nodes.get(identity)
        if existing is not None and existing != props:
            merged = dict(existing)
            for name, value in props.items():
                if name in merged and merged[name] != value:
                    raise ValueError(f"node_property_conflict:{node_type}:{key}:{name}")
                merged[name] = value
            props = merged
        self.nodes[identity] = props

    def add_edge(
        self,
        from_type: str,
        from_key: object,
        relation_type: str,
        to_type: str,
        to_key: object,
        *,
        basis_kind: str,
        basis_rule: str,
        properties: Mapping[str, Any] | None,
        evidence_ids: Iterable[str],
    ) -> None:
        if relation_type not in RELATION_TYPES:
            raise ValueError(f"invalid_relation_type:{relation_type}")
        source = _clean_text(from_key)
        target = _clean_text(to_key)
        supports = set(evidence_ids)
        if not source or not target or not supports:
            return
        self.add_node(from_type, source)
        self.add_node(to_type, target)
        props = dict(properties or {})
        identity = (
            from_type, source, relation_type, to_type, target,
            basis_kind, basis_rule, canonical_json(props),
        )
        if identity not in self.edges:
            self.edges[identity] = {
                "from_type": from_type,
                "from_key": source,
                "relation_type": relation_type,
                "to_type": to_type,
                "to_key": target,
                "basis_kind": basis_kind,
                "basis_rule": basis_rule,
                "properties": props,
                "evidence_ids": set(),
            }
        self.edges[identity]["evidence_ids"].update(supports)


def _evidence_text(record: Mapping[str, Any]) -> str:
    observed = record.get("observed_text")
    if isinstance(observed, str):
        adapter = record.get("adapter")
        projection = adapter.get("text_projection", "") if isinstance(adapter, Mapping) else ""
        if projection.startswith("canonical_"):
            try:
                decoded = json.loads(observed)
            except json.JSONDecodeError:
                decoded = observed
            if isinstance(decoded, (str, int, float)) and not isinstance(decoded, bool):
                return _clean_text(decoded)
        return _clean_text(observed)
    content = record.get("content")
    if not isinstance(content, Mapping):
        return ""
    for key in ("normalized_text", "raw_text"):
        value = content.get(key)
        if isinstance(value, str):
            return _clean_text(value)
    for key in ("normalized_value", "raw_value"):
        value = content.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            return _clean_text(value)
    return ""


def _has_visible_provisional_marker(text: str) -> bool:
    # Fail closed even if a malformed upstream record embeds the marker in the
    # middle of a line instead of using the canonical line prefix.
    return PROVISIONAL_MARKER in text


def _quality_disposition(record: Mapping[str, Any], text: str) -> str:
    """Classify whether one safe Evidence record may support verified facts.

    Content-security eligibility and evidence quality are distinct gates.  A
    native record has no visual-quality declaration.  Visual-derived records
    must declare a valid tier; only ``high`` can support a verified graph.
    """
    adapter = record.get("adapter")
    adapter = adapter if isinstance(adapter, Mapping) else {}
    source_record_type = adapter.get("source_record_type")
    unit_type = adapter.get("unit_type")
    extraction_method = record.get("extraction_method")
    quality_present = "quality_tier" in record
    quality_tier = record.get("quality_tier")
    marker_present = "provisional_marker" in record
    marker = record.get("provisional_marker")
    visible_marker = _has_visible_provisional_marker(text)
    visual_method_like = (
        isinstance(extraction_method, str)
        and extraction_method.startswith("local_vlm_")
    )
    provisional_method_like = (
        isinstance(extraction_method, str)
        and extraction_method.endswith("_provisional")
    )
    quality_required = (
        source_record_type in VISUAL_QUALITY_SOURCE_TYPES
        or unit_type in VISUAL_QUALITY_UNIT_TYPES
        or extraction_method in VISUAL_QUALITY_METHODS
        or visual_method_like
        or provisional_method_like
    )

    if quality_present and (
        not isinstance(quality_tier, str)
        or quality_tier not in {"high", "provisional"}
    ):
        return "excluded_invalid_quality"
    if quality_tier == "provisional":
        if marker != PROVISIONAL_MARKER or not visible_marker:
            return "excluded_invalid_quality"
        return "excluded_provisional"
    if quality_tier == "high":
        if not quality_required or provisional_method_like:
            return "excluded_invalid_quality"
        if marker_present or visible_marker:
            return "excluded_marker"
        return "eligible_high"
    if quality_required or quality_present:
        return "excluded_invalid_quality"
    if marker_present:
        return (
            "excluded_marker"
            if marker == PROVISIONAL_MARKER
            else "excluded_invalid_quality"
        )
    if visible_marker:
        return "excluded_marker"
    return "eligible_native"


def _prepare_inputs(
    document_records: Sequence[dict[str, Any]], evidence_records: Sequence[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[EvidenceView]]:
    documents: dict[str, dict[str, Any]] = {}
    for record in document_records:
        document_id = record.get("document_id")
        source = record.get("source")
        if not isinstance(document_id, str) or not document_id or document_id in documents:
            raise ValueError(f"invalid_or_duplicate_document_id:{document_id!r}")
        if not isinstance(source, Mapping):
            raise ValueError(f"document_source_missing:{document_id}")
        relative_path = source.get("relative_path")
        source_sha256 = source.get("sha256")
        if not isinstance(relative_path, str) or not relative_path or not isinstance(source_sha256, str):
            raise ValueError(f"document_source_invalid:{document_id}")
        if record.get("status") != "extracted":
            raise ValueError(f"document_not_extracted:{document_id}")
        authorized = record.get("evidence_ids")
        if (
            not isinstance(authorized, list)
            or any(not isinstance(value, str) or not value for value in authorized)
            or len(authorized) != len(set(authorized))
        ):
            raise ValueError(f"document_evidence_ids_invalid:{document_id}")
        documents[document_id] = record

    seen: set[str] = set()
    evidence: list[EvidenceView] = []
    for record in evidence_records:
        evidence_id = record.get("evidence_id")
        document_id = record.get("document_id")
        if not isinstance(evidence_id, str) or not evidence_id or evidence_id in seen:
            raise ValueError(f"invalid_or_duplicate_evidence_id:{evidence_id!r}")
        seen.add(evidence_id)
        if document_id not in documents:
            raise ValueError(f"evidence_document_missing:{evidence_id}:{document_id}")
        if record.get("status") != "observed":
            raise ValueError(f"unsafe_or_unobserved_evidence:{evidence_id}")
        adapter = record.get("adapter")
        if not isinstance(adapter, Mapping) or adapter.get("execution_policy") != "never_execute":
            raise ValueError(f"evidence_execution_policy_invalid:{evidence_id}")
        source_record_type = adapter.get("source_record_type")
        if not isinstance(source_record_type, str) or not source_record_type:
            raise ValueError(f"evidence_source_record_type_invalid:{evidence_id}")
        observed_text = record.get("observed_text")
        if not isinstance(observed_text, str):
            raise ValueError(f"evidence_observed_text_invalid:{evidence_id}")
        text = _evidence_text(record)
        quality_disposition = _quality_disposition(record, text)
        document = documents[document_id]
        source = document["source"]
        source_record = record.get("source")
        if (
            not isinstance(source_record, Mapping)
            or source_record.get("relative_path") != source["relative_path"]
            or source_record.get("sha256") != source["sha256"]
            or evidence_id not in document.get("evidence_ids", [])
        ):
            raise ValueError(f"safe_evidence_document_binding_invalid:{evidence_id}")
        location = record.get("locator")
        if not isinstance(location, Mapping):
            raise ValueError(f"evidence_locator_invalid:{evidence_id}")
        geometry = record.get("geometry")
        evidence.append(EvidenceView(
            evidence_id=evidence_id,
            document_id=document_id,
            relative_path=source["relative_path"],
            source_sha256=source["sha256"],
            evidence_type=source_record_type,
            location=dict(location),
            observed_text=observed_text,
            observed_sha256=hashlib.sha256(observed_text.encode("utf-8")).hexdigest(),
            text=text,
            ordinal=int(record.get("ordinal", 0) or 0),
            geometry=dict(geometry) if isinstance(geometry, Mapping) else None,
            quality_disposition=quality_disposition,
        ))
    evidence.sort(key=lambda item: (item.relative_path, item.ordinal, item.evidence_id))
    return documents, evidence


def _column_number(cell: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"([A-Za-z]{1,3})([1-9][0-9]*)", cell)
    if match is None:
        return None
    column = 0
    for character in match.group(1).upper():
        column = column * 26 + ord(character) - 64
    return int(match.group(2)), column


def _cell_position(item: EvidenceView) -> tuple[tuple[Any, ...], int, int] | None:
    if item.evidence_type != "table_cell":
        return None
    location = item.location
    if isinstance(location.get("cell"), str) and isinstance(location.get("sheet_name"), str):
        position = _column_number(location["cell"])
        if position is not None:
            return (("sheet", item.document_id, location["sheet_name"]), position[0], position[1])
    row = location.get("row_index")
    column = location.get("column_index")
    if not isinstance(row, int) or not isinstance(column, int):
        return None
    if "table_index" in location:
        container = ("table", item.document_id, location.get("table_index"))
    elif "shape_id" in location and "slide_number" in location:
        container = (
            "slide_table", item.document_id,
            location.get("slide_number"), str(location.get("shape_id")),
        )
    else:
        return None
    return container, row, column


def _structured_tables(evidence: Sequence[EvidenceView]) -> dict[tuple[Any, ...], dict[int, dict[int, EvidenceView]]]:
    tables: dict[tuple[Any, ...], dict[int, dict[int, EvidenceView]]] = defaultdict(lambda: defaultdict(dict))
    for item in evidence:
        position = _cell_position(item)
        if position is None:
            continue
        container, row, column = position
        if column in tables[container][row]:
            raise ValueError(f"duplicate_table_coordinate:{container}:{row}:{column}")
        tables[container][row][column] = item
    return tables


def _parse_structured_rows(
    tables: Mapping[tuple[Any, ...], Mapping[int, Mapping[int, EvidenceView]]],
) -> list[StructuredRow]:
    output: list[StructuredRow] = []
    for container, rows in sorted(tables.items(), key=lambda item: canonical_json(item[0])):
        header_row = None
        header_fields: dict[int, str] = {}
        for row_index in sorted(rows):
            candidates = {
                column: field_name
                for column, cell in rows[row_index].items()
                if (field_name := _header_key(cell.text)) is not None
            }
            if len(candidates) >= 2:
                header_row = row_index
                header_fields = candidates
                break
        if header_row is None:
            continue
        for row_index in sorted(index for index in rows if index > header_row):
            values: dict[str, FieldValue] = {}
            for column, field_name in header_fields.items():
                cell = rows[row_index].get(column)
                if cell is not None and cell.text:
                    values[field_name] = FieldValue(cell.text, (cell.evidence_id,))
            if values:
                output.append(StructuredRow(container[1], container, row_index, values))
    return output


def _parse_key_value_tables(
    facts: Mapping[str, DocumentFacts],
    tables: Mapping[tuple[Any, ...], Mapping[int, Mapping[int, EvidenceView]]],
) -> None:
    for container, rows in tables.items():
        document = facts[container[1]]
        for row_index in sorted(rows):
            # A key/value table row is exactly one label and one value.  Wider
            # tabular headers must be handled by the header-semantic row parser.
            if len(rows[row_index]) != 2:
                continue
            label = rows[row_index].get(min(rows[row_index]))
            value = rows[row_index].get(max(rows[row_index]))
            if label is None or value is None or label is value:
                continue
            field_name = _header_key(label.text)
            if field_name is not None:
                document.add(field_name, value.text, (label.evidence_id, value.evidence_id))


def _labelled_segments(text: str) -> list[tuple[str, str]]:
    output: list[tuple[str, str]] = []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        segments = re.split(r"\s+/\s+(?=[^/\n]{1,60}[:：])", line)
        for segment in segments:
            match = re.match(r"^([^:：]{1,80})[:：]\s*(.*)$", segment)
            if match is None:
                continue
            label, value = match.group(1).strip(), match.group(2).strip()
            if not value and index + 1 < len(lines):
                value = lines[index + 1]
            output.append((label, value))
        if _header_key(line) == "change_reason" and index + 1 < len(lines):
            output.append((line, lines[index + 1]))
    return output


def _document_facts(
    documents: Mapping[str, dict[str, Any]], evidence: Sequence[EvidenceView],
    tables: Mapping[tuple[Any, ...], Mapping[int, Mapping[int, EvidenceView]]],
) -> dict[str, DocumentFacts]:
    facts: dict[str, DocumentFacts] = {}
    by_document: dict[str, list[EvidenceView]] = defaultdict(list)
    for item in evidence:
        by_document[item.document_id].append(item)
    for document_id, record in documents.items():
        source = record["source"]
        facts[document_id] = DocumentFacts(
            document_id=document_id,
            relative_path=source["relative_path"],
            extension=str(source.get("extension", Path(source["relative_path"]).suffix.lstrip("."))).casefold(),
        )
    _parse_key_value_tables(facts, tables)
    for document_id, items in by_document.items():
        document = facts[document_id]
        for item in items:
            labelled = _labelled_segments(item.text)
            labelled_fields: set[str] = set()
            for label, value in labelled:
                field_name = _header_key(label)
                if field_name is not None:
                    labelled_fields.add(field_name)
                    document.add(field_name, value, (item.evidence_id,))
            version = _normalized_version(item.text)
            # Labelled Version fields were already captured above.  Do not take
            # the last v-number from a compound block that also names the
            # superseded version.
            if (
                version
                and "version" not in labelled_fields
                and (
                    "version" in item.text.casefold()
                    or re.search(r"(?:^|\s)v\d", item.text, re.I)
                )
            ):
                document.add("version", version, (item.evidence_id,))
            if _is_draft_status(item.text):
                document.add("claim_status_marker", "DRAFT", (item.evidence_id,))
            elif _is_current_status(item.text):
                document.add("claim_status_marker", "APPROVED", (item.evidence_id,))
    return facts


ALIAS_STATEMENT = re.compile(
    r"(?:案件|プロジェク)?別表記\s*[:：]\s*(?P<aliases>.+?)"
    r"は[、,]?\s*(?:Project\s*ID|プロジェクトID|案件ID)\s*[:：]?\s*"
    r"(?P<project>[^\s、。,]+)の別表記",
    re.I,
)
WORK_STATEMENT = re.compile(
    r"(?P<work>[A-Za-z0-9_.:/\-]+)は[、,]?\s*(?P<project>[A-Za-z0-9_.:/\-]+)"
    r"に属する(?:業務|作業|タスク)[「『“\"](?P<name>[^」』”\"]+)[」』”\"]"
    r"の正式ID",
    re.I,
)


def _split_aliases(value: str) -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in re.split(r"\s*(?:と|、|,|;)　*\s*", value)
        if item.strip()
    )


def _add_identity_definitions(
    graph: GraphAccumulator,
    facts: Mapping[str, DocumentFacts],
    evidence: Sequence[EvidenceView],
) -> None:
    by_document: dict[str, list[EvidenceView]] = defaultdict(list)
    for item in evidence:
        by_document[item.document_id].append(item)
    for document_id, document in facts.items():
        explicit_aliases: set[tuple[str, str, str]] = set()
        explicit_work: set[tuple[str, str, str, str]] = set()
        for item in by_document[document_id]:
            for match in ALIAS_STATEMENT.finditer(item.text):
                project = match.group("project").strip()
                for alias in _split_aliases(match.group("aliases")):
                    explicit_aliases.add((project, alias, item.evidence_id))
            for match in WORK_STATEMENT.finditer(item.text):
                explicit_work.add((
                    match.group("project").strip(), match.group("work").strip(),
                    match.group("name").strip(), item.evidence_id,
                ))
        for project, alias, evidence_id in sorted(explicit_aliases):
            graph.add_edge(
                "Project", project, "HAS_ALIAS", "ProjectAlias", alias,
                basis_kind="explicit_source_statement",
                basis_rule="explicit_project_alias_statement_or_field",
                properties={}, evidence_ids=(evidence_id,),
            )
        for project, work, work_name, evidence_id in sorted(explicit_work):
            graph.add_edge(
                "Project", project, "CONTAINS_WORK", "Work", work,
                basis_kind="explicit_source_statement",
                basis_rule="explicit_project_work_identity",
                properties={}, evidence_ids=(evidence_id,),
            )
            graph.add_edge(
                "Work", work, "HAS_NAME", "WorkName", work_name,
                basis_kind="explicit_source_statement",
                basis_rule="explicit_project_work_identity",
                properties={}, evidence_ids=(evidence_id,),
            )

        project = document.unique("project_id")
        work = document.unique("work_id")
        work_name = document.unique("work_name")
        if project and work:
            support = (*project.evidence_ids, *work.evidence_ids)
            graph.add_edge(
                "Project", project.value, "CONTAINS_WORK", "Work", work.value,
                basis_kind="explicit_source_statement",
                basis_rule="explicit_project_work_identity",
                properties={}, evidence_ids=support,
            )
        if work and work_name:
            support = (*work.evidence_ids, *work_name.evidence_ids)
            graph.add_edge(
                "Work", work.value, "HAS_NAME", "WorkName", work_name.value,
                basis_kind="explicit_source_statement",
                basis_rule="explicit_project_work_identity",
                properties={}, evidence_ids=support,
            )
        if project:
            for field_name in ("project_name", "project_alias"):
                for alias in document.fields.get(field_name, []):
                    matching = [
                        item.evidence_id for item in by_document[document_id]
                        if project.value in item.text and alias.value in item.text and "別表記" in item.text
                    ]
                    graph.add_edge(
                        "Project", project.value, "HAS_ALIAS", "ProjectAlias", alias.value,
                        basis_kind="explicit_source_statement",
                        basis_rule="explicit_project_alias_statement_or_field",
                        properties={},
                        evidence_ids=(*project.evidence_ids, *alias.evidence_ids, *matching),
                    )


def _field(row: StructuredRow, name: str) -> FieldValue | None:
    return row.fields.get(name)


def _add_assignments(graph: GraphAccumulator, rows: Sequence[StructuredRow]) -> None:
    for row in rows:
        project = _field(row, "project_id")
        work = _field(row, "work_id")
        role = _field(row, "role")
        assignee = _field(row, "assignee_id")
        valid_from = _field(row, "valid_from")
        valid_to = _field(row, "valid_to")
        status = _field(row, "status")
        if not all((project, work, role, assignee, valid_from, status)):
            continue
        start = _normalized_date(valid_from.value)
        end = _normalized_date(valid_to.value) if valid_to else None
        if start is None or (valid_to is not None and end is None) or not _is_current_status(status.value):
            continue
        record = _field(row, "record_id")
        properties = {
            "record_id": record.value if record else None,
            "role": role.value,
            "source_status": _status_text(status.value),
            "valid_from": start,
            "valid_from_inclusive": True,
            "valid_to": end,
        }
        if end is not None:
            properties["valid_to_inclusive"] = True
        graph.add_node("Project", project.value)
        graph.add_node("Work", work.value)
        graph.add_node("Employee", assignee.value)
        graph.add_edge(
            "Work", work.value, "ASSIGNED_TO", "Employee", assignee.value,
            basis_kind="explicit_table_row", basis_rule="final_assignment_row",
            properties=properties, evidence_ids=row.evidence_ids,
        )


def _pdf_page_number(item: EvidenceView) -> int | None:
    page_number = item.location.get("page_number")
    if (
        isinstance(page_number, int)
        and not isinstance(page_number, bool)
        and page_number >= 1
    ):
        return page_number
    return None


def _page_groups(
    items: Sequence[EvidenceView],
) -> list[list[list[EvidenceView]]]:
    """Build visual rows inside one page and one coordinate frame only."""
    partitions: dict[tuple[int, str, str, str], list[EvidenceView]] = defaultdict(list)
    for item in items:
        geometry = item.geometry
        page_number = _pdf_page_number(item)
        if (
            geometry is None
            or page_number is None
            or not isinstance(geometry.get("x"), (int, float))
            or isinstance(geometry.get("x"), bool)
            or not isinstance(geometry.get("y"), (int, float))
            or isinstance(geometry.get("y"), bool)
            or not isinstance(geometry.get("coordinate_space"), str)
            or not geometry.get("coordinate_space")
            or not isinstance(geometry.get("unit"), str)
            or not geometry.get("unit")
        ):
            continue
        coordinate_origin = geometry.get("coordinate_origin", "")
        if not isinstance(coordinate_origin, str):
            continue
        key = (
            page_number,
            geometry["coordinate_space"],
            geometry["unit"],
            coordinate_origin,
        )
        partitions[key].append(item)

    grouped_partitions: list[list[list[EvidenceView]]] = []
    for key in sorted(partitions):
        positioned = partitions[key]
        positioned.sort(
            key=lambda item: (
                float(item.geometry["y"]),
                float(item.geometry["x"]),
                item.ordinal,
            )
        )
        groups: list[list[EvidenceView]] = []
        for item in positioned:
            y = float(item.geometry["y"])
            if not groups:
                groups.append([item])
                continue
            prior_y = sum(
                float(value.geometry["y"]) for value in groups[-1]
            ) / len(groups[-1])
            heights = [
                float(value.geometry.get("height", 10.0))
                for value in groups[-1]
            ]
            tolerance = max(
                2.0,
                min(8.0, sum(heights) / len(heights) * 0.35),
            )
            if abs(y - prior_y) <= tolerance:
                groups[-1].append(item)
            else:
                groups.append([item])
        for group in groups:
            group.sort(key=lambda item: float(item.geometry["x"]))
        grouped_partitions.append(groups)
    return grouped_partitions


def _coordinate_identity_rows(items: Sequence[EvidenceView]) -> list[dict[str, FieldValue]]:
    result: list[dict[str, FieldValue]] = []
    for groups in _page_groups(items):
        for header_index, group in enumerate(groups):
            headers = {
                _header_key(item.text): (float(item.geometry["x"]), item)
                for item in group
                if _header_key(item.text) is not None
            }
            if not {"employee_id", "person_name"} <= set(headers):
                continue
            column_fields = {
                field_name: x_item[0]
                for field_name, x_item in headers.items()
                if field_name in {"employee_id", "person_name", "status"}
            }
            x_values = sorted(column_fields.values())
            minimum_gap = min(
                (right - left for left, right in zip(x_values, x_values[1:])),
                default=100.0,
            )
            maximum_distance = max(18.0, minimum_gap * 0.45)
            partition_result: list[dict[str, FieldValue]] = []
            for data_group in groups[header_index + 1:]:
                row: dict[str, FieldValue] = {}
                for item in data_group:
                    x = float(item.geometry["x"])
                    field_name, distance = min(
                        (
                            (name, abs(x - header_x))
                            for name, header_x in column_fields.items()
                        ),
                        key=lambda pair: pair[1],
                    )
                    if distance <= maximum_distance and field_name not in row:
                        row[field_name] = FieldValue(item.text, (item.evidence_id,))
                employee = row.get("employee_id")
                person = row.get("person_name")
                if (
                    employee
                    and person
                    and _looks_identifier(employee.value)
                    and _looks_person_name(person.value)
                ):
                    partition_result.append(row)
                elif partition_result:
                    break
            if partition_result:
                result.extend(partition_result)
                break
    return result


def _ordered_identity_rows_for_page(
    items: Sequence[EvidenceView],
) -> list[dict[str, FieldValue]]:
    # PDFKit commonly returns one native string per page.  Preserve its line
    # boundaries and recognize an explicit whitespace-delimited table before
    # trying the older one-cell-per-Evidence ordering fallback.
    for item in items:
        if item.evidence_type not in {"page", "paragraph", "text_block"}:
            continue
        lines = [line.strip() for line in item.text.splitlines() if line.strip()]
        for header_index, line in enumerate(lines):
            header_fields = [_header_key(token) for token in line.split()]
            if (
                any(field is None for field in header_fields)
                or not {"employee_id", "person_name"} <= set(header_fields)
                or len(header_fields) > 8
            ):
                continue
            fields = [str(field) for field in header_fields]
            result: list[dict[str, FieldValue]] = []
            for data_line in lines[header_index + 1:]:
                values = _whitespace_identity_values(fields, data_line)
                if values is None:
                    if result:
                        break
                    continue
                row = {
                    field_name: FieldValue(values[index], (item.evidence_id,))
                    for index, field_name in enumerate(fields)
                }
                employee = row.get("employee_id")
                person = row.get("person_name")
                if (
                    employee
                    and person
                    and _looks_identifier(employee.value)
                    and _looks_person_name(person.value)
                ):
                    result.append(row)
                elif result:
                    break
            if result:
                return result
    individual = [
        item for item in items
        if item.evidence_type not in {"page", "table", "slide", "worksheet", "style_span"}
    ]
    sequences: list[list[tuple[str, str]]] = []
    if individual:
        individual.sort(key=lambda item: (item.ordinal, item.evidence_id))
        sequences.append([(item.text, item.evidence_id) for item in individual])
    for item in items:
        if item.evidence_type in {"page", "paragraph", "text_block"} and "\n" in item.text:
            sequences.append([(line.strip(), item.evidence_id) for line in item.text.splitlines() if line.strip()])
    for sequence in sequences:
        for start in range(len(sequence)):
            header_fields: list[str] = []
            cursor = start
            while cursor < len(sequence) and len(header_fields) < 8:
                field_name = _header_key(sequence[cursor][0])
                if field_name not in {"employee_id", "person_name", "status"}:
                    break
                header_fields.append(field_name)
                cursor += 1
            if not {"employee_id", "person_name"} <= set(header_fields):
                continue
            result: list[dict[str, FieldValue]] = []
            width = len(header_fields)
            while cursor + width <= len(sequence):
                chunk = sequence[cursor:cursor + width]
                row = {
                    field_name: FieldValue(chunk[index][0], (chunk[index][1],))
                    for index, field_name in enumerate(header_fields)
                }
                employee = row.get("employee_id")
                person = row.get("person_name")
                if not employee or not person or not _looks_identifier(employee.value) or not _looks_person_name(person.value):
                    break
                result.append(row)
                cursor += width
            if result:
                return result
    return []


def _ordered_identity_rows(items: Sequence[EvidenceView]) -> list[dict[str, FieldValue]]:
    """Apply the text-order fallback independently to each PDF page."""
    by_page: dict[int, list[EvidenceView]] = defaultdict(list)
    for item in items:
        page_number = _pdf_page_number(item)
        if page_number is not None:
            by_page[page_number].append(item)
    rows: list[dict[str, FieldValue]] = []
    for page_number in sorted(by_page):
        rows.extend(_ordered_identity_rows_for_page(by_page[page_number]))
    return rows


def _add_employee_identities(
    graph: GraphAccumulator,
    facts: Mapping[str, DocumentFacts],
    evidence: Sequence[EvidenceView],
    diagnostics: dict[str, int],
) -> None:
    by_document: dict[str, list[EvidenceView]] = defaultdict(list)
    for item in evidence:
        by_document[item.document_id].append(item)
    for document_id, document in facts.items():
        if document.extension != "pdf":
            continue
        status = document.unique("document_status")
        register_version = document.unique("register_version")
        if status is None or not _is_current_status(status.value):
            continue
        eligible_items = by_document[document_id]
        rows: list[tuple[str, dict[str, FieldValue]]] = []
        for disposition in ("eligible_native", "eligible_high"):
            source_items = [
                item for item in eligible_items
                if item.quality_disposition == disposition
            ]
            source_rows = _coordinate_identity_rows(source_items)
            route = "pdf_coordinate_rows"
            if not source_rows:
                source_rows = _ordered_identity_rows(source_items)
                route = "pdf_order_fallback"
            if source_rows:
                diagnostics[route] = diagnostics.get(route, 0) + len(source_rows)
                rows.extend((disposition, row) for row in source_rows)
        if not rows:
            continue
        person_values_by_employee: dict[str, set[str]] = defaultdict(set)
        dispositions_by_employee: dict[str, set[str]] = defaultdict(set)
        status_states_by_employee: dict[str, list[str | None]] = defaultdict(list)
        for disposition, row in rows:
            employee = row["employee_id"]
            person = row["person_name"]
            employee_key = _identity_key(employee.value)
            person_values_by_employee[employee_key].add(
                _identity_key(person.value)
            )
            dispositions_by_employee[employee_key].add(disposition)
            row_status = row.get("status")
            status_states_by_employee[employee_key].append(
                _identity_status_state(row_status.value)
                if row_status is not None else None
            )
        status_conflicts = {
            employee_key
            for employee_key, dispositions in dispositions_by_employee.items()
            if len(dispositions) > 1
            and (
                any(
                    state is None
                    for state in status_states_by_employee[employee_key]
                )
                or len(set(status_states_by_employee[employee_key])) != 1
            )
        }
        for _, row in rows:
            employee = row["employee_id"]
            person = row["person_name"]
            employee_key = _identity_key(employee.value)
            if len(person_values_by_employee[employee_key]) != 1:
                diagnostics["pdf_identity_conflicts_excluded"] = (
                    diagnostics.get("pdf_identity_conflicts_excluded", 0) + 1
                )
                continue
            if employee_key in status_conflicts:
                diagnostics["pdf_identity_status_conflicts_excluded"] = (
                    diagnostics.get(
                        "pdf_identity_status_conflicts_excluded", 0
                    ) + 1
                )
                continue
            row_status = row.get("status")
            if row_status is not None and not _is_active_row_status(row_status.value):
                continue
            properties = {
                "register_version": _normalized_date(register_version.value) if register_version else None,
                "source_status": _status_text(status.value),
            }
            support = [*employee.evidence_ids, *person.evidence_ids, *status.evidence_ids]
            if register_version:
                support.extend(register_version.evidence_ids)
            if row_status:
                support.extend(row_status.evidence_ids)
            graph.add_edge(
                "Employee", employee.value, "IDENTIFIES_PERSON", "Person", person.value,
                basis_kind="explicit_table_row", basis_rule="approved_employee_identity_row",
                properties=properties, evidence_ids=support,
            )


def _matching_text_evidence(
    items: Sequence[EvidenceView], required: Iterable[str], markers: Iterable[str] = (),
) -> tuple[str, ...]:
    required_values = [value for value in required if value]
    marker_values = [value.casefold() for value in markers if value]
    return tuple(sorted(
        item.evidence_id
        for item in items
        if all(value in item.text for value in required_values)
        and (not marker_values or any(value in item.text.casefold() for value in marker_values))
    ))


def _add_claims(
    graph: GraphAccumulator,
    facts: Mapping[str, DocumentFacts],
    evidence: Sequence[EvidenceView],
    rows: Sequence[StructuredRow],
) -> list[ClaimFact]:
    by_document: dict[str, list[EvidenceView]] = defaultdict(list)
    for item in evidence:
        by_document[item.document_id].append(item)
    claims: list[ClaimFact] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for row in rows:
        document = facts[row.document_id]
        if document.extension != "pptx":
            continue
        project = _field(row, "project_id")
        work = _field(row, "work_id")
        role = _field(row, "role")
        assignee = _field(row, "assignee_id")
        effective = _field(row, "effective_from")
        status = _field(row, "status")
        version = document.unique("version")
        if not all((project, work, role, assignee, effective, status, version)):
            continue
        effective_date = _normalized_date(effective.value)
        normalized_version = _normalized_version(version.value)
        if effective_date is None or normalized_version is None:
            continue
        current = _is_current_status(status.value)
        if not current and not _is_draft_status(status.value):
            continue
        role_key = _role_key(role.value)
        claim_key = f"claim:{project.value}:{work.value}:{role_key}:{normalized_version}"
        dedupe = (project.value, work.value, role_key, normalized_version, assignee.value, effective_date)
        if dedupe in seen:
            continue
        seen.add(dedupe)
        status_value = "APPROVED" if current else "DRAFT"
        item_list = by_document[row.document_id]
        assignee_support = set(row.evidence_ids)
        assignee_support.update(_matching_text_evidence(item_list, (assignee.value, effective_date)))
        status_support = set(version.evidence_ids)
        status_support.update(_matching_text_evidence(
            item_list,
            (normalized_version,),
            ("approved", "final", "承認済") if current else ("draft", "not approved", "未承認"),
        ))
        if not status_support:
            status_support.update(row.evidence_ids)
        claim_properties = {
            "claim_status": status_value,
            "current": current,
            "effective_from": effective_date,
            "project_id": project.value,
            "work_id": work.value,
            "role": role.value,
            "version": normalized_version,
        }
        graph.add_node("Claim", claim_key, claim_properties)
        graph.add_edge(
            "Work", work.value,
            "HAS_CURRENT_CLAIM" if current else "HAS_CLAIM",
            "Claim", claim_key,
            basis_kind="explicit_document_claim",
            basis_rule="explicit_versioned_assignment_claim",
            properties={
                "claim_status": status_value,
                "current": current,
                "effective_from": effective_date,
            },
            evidence_ids=status_support,
        )
        graph.add_edge(
            "Claim", claim_key, "CLAIMS_ASSIGNEE", "Employee", assignee.value,
            basis_kind="explicit_document_claim",
            basis_rule="explicit_versioned_assignment_claim",
            properties={
                "claim_status": status_value,
                "current": current,
                "effective_from": effective_date,
                "role": role.value,
            },
            evidence_ids=assignee_support,
        )
        claims.append(ClaimFact(
            document_id=row.document_id,
            project_id=project.value,
            work_id=work.value,
            role=role.value,
            role_key=role_key,
            assignee_id=assignee.value,
            effective_from=effective_date,
            status=status_value,
            version=normalized_version,
            current=current,
            canonical_key=claim_key,
            row_evidence_ids=row.evidence_ids,
            assignee_evidence_ids=tuple(sorted(assignee_support)),
            status_evidence_ids=tuple(sorted(status_support)),
        ))
    return claims


def _add_version_relations(
    graph: GraphAccumulator,
    facts: Mapping[str, DocumentFacts],
    claims: Sequence[ClaimFact],
) -> None:
    claims_by_document: dict[str, list[ClaimFact]] = defaultdict(list)
    for claim in claims:
        claims_by_document[claim.document_id].append(claim)
    basename_to_document = {
        Path(document.relative_path).name.casefold(): document_id
        for document_id, document in facts.items()
    }
    for document_id, document in facts.items():
        supersedes_values = document.fields.get("supersedes", [])
        current_claims = [claim for claim in claims_by_document.get(document_id, []) if claim.current]
        for source in supersedes_values:
            target_document = basename_to_document.get(Path(source.value.strip()).name.casefold())
            target_version = _normalized_version(source.value)
            for current in current_claims:
                candidates = [
                    claim for claim in claims
                    if claim.document_id != document_id
                    and claim.project_id == current.project_id
                    and claim.work_id == current.work_id
                    and claim.role_key == current.role_key
                    and (target_document is None or claim.document_id == target_document)
                    and (target_version is None or claim.version == target_version)
                ]
                if target_document is None and target_version is None:
                    continue
                if len(candidates) != 1:
                    continue
                previous = candidates[0]
                graph.add_edge(
                    "Claim", current.canonical_key, "SUPERSEDES", "Claim", previous.canonical_key,
                    basis_kind="explicit_source_statement",
                    basis_rule="explicit_supersedes_reference",
                    properties={}, evidence_ids=source.evidence_ids,
                )
                if (
                    previous.work_id == current.work_id
                    and previous.role_key == current.role_key
                    and previous.effective_from == current.effective_from
                    and previous.assignee_id != current.assignee_id
                ):
                    graph.add_edge(
                        "Claim", previous.canonical_key, "CONTRADICTS", "Claim", current.canonical_key,
                        basis_kind="verified_comparison",
                        basis_rule=(
                            "same_work_role_effective_date_different_assignee_"
                            "under_explicit_supersession"
                        ),
                        properties={"comparison_dimensions": ["work", "role", "effective_from", "assignee"]},
                        evidence_ids=(*previous.assignee_evidence_ids, *current.assignee_evidence_ids),
                    )


def _add_change_reasons(
    graph: GraphAccumulator,
    facts: Mapping[str, DocumentFacts],
    claims: Sequence[ClaimFact],
) -> None:
    claims_by_document: dict[str, list[ClaimFact]] = defaultdict(list)
    for claim in claims:
        claims_by_document[claim.document_id].append(claim)
    for document_id, document in facts.items():
        current = [claim for claim in claims_by_document.get(document_id, []) if claim.current]
        reasons = document.fields.get("change_reason", [])
        if len(current) != 1:
            continue
        for reason in reasons:
            graph.add_edge(
                "Claim", current[0].canonical_key, "HAS_CHANGE_REASON", "Reason", reason.value,
                basis_kind="explicit_source_statement", basis_rule="explicit_change_reason",
                properties={}, evidence_ids=reason.evidence_ids,
            )


def _source_evidence_record(item: EvidenceView) -> dict[str, Any]:
    core = {
        "evidence_id": item.evidence_id,
        "document_id": item.document_id,
        "relative_path": item.relative_path,
        "source_sha256": item.source_sha256,
        "locator": item.location,
        "observed_text": item.observed_text,
        "observed_sha256": item.observed_sha256,
    }
    return {**core, "record_sha256": sha256_json(core)}


def _materialize_graph(
    graph: GraphAccumulator,
    evidence: Sequence[EvidenceView],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, str]:
    evidence_records = [_source_evidence_record(item) for item in evidence]
    known_evidence_ids = {item["evidence_id"] for item in evidence_records}
    node_ids: dict[tuple[str, str], str] = {}
    nodes: list[dict[str, Any]] = []
    for (node_type, canonical_key), properties in sorted(
        graph.nodes.items(), key=lambda item: (item[0][0], item[0][1]),
    ):
        node_id = "node_" + sha256_json({"node_type": node_type, "canonical_key": canonical_key})[:32]
        node_ids[(node_type, canonical_key)] = node_id
        core = {
            "node_id": node_id,
            "node_type": node_type,
            "canonical_key": canonical_key,
            "status": "verified",
            "properties": properties,
        }
        nodes.append({**core, "record_sha256": sha256_json(core)})

    edges: list[dict[str, Any]] = []
    for value in graph.edges.values():
        supporting = sorted(value["evidence_ids"])
        unknown = sorted(set(supporting) - known_evidence_ids)
        if unknown:
            raise ValueError(f"edge_support_missing:{unknown[:4]}")
        from_node_id = node_ids[(value["from_type"], value["from_key"])]
        to_node_id = node_ids[(value["to_type"], value["to_key"])]
        edge_identity = {
            "from_node_id": from_node_id,
            "relation_type": value["relation_type"],
            "to_node_id": to_node_id,
            "relation_class": "semantic",
            "status": "verified",
            "basis_kind": value["basis_kind"],
            "basis_rule": value["basis_rule"],
            "properties": value["properties"],
            "supporting_evidence_ids": supporting,
        }
        edge_id = "edge_" + sha256_json(edge_identity)[:32]
        core = {"edge_id": edge_id, **edge_identity}
        edges.append({**core, "record_sha256": sha256_json(core)})
    edges.sort(key=lambda item: item["edge_id"])
    logical = {
        "evidence_record_sha256": sorted(item["record_sha256"] for item in evidence_records),
        "node_record_sha256": sorted(node["record_sha256"] for node in nodes),
        "edge_record_sha256": sorted(edge["record_sha256"] for edge in edges),
    }
    logical_sha256 = sha256_json(logical)
    return nodes, edges, "xkgs_" + logical_sha256[:32], logical_sha256


def _write_database(
    path: Path,
    metadata: Mapping[str, Any],
    evidence: Sequence[EvidenceView],
    nodes: Sequence[dict[str, Any]],
    edges: Sequence[dict[str, Any]],
) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
            PRAGMA page_size=4096;
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE source_evidence (
                evidence_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                locator_json TEXT NOT NULL,
                observed_text TEXT NOT NULL,
                observed_sha256 TEXT NOT NULL,
                record_sha256 TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE nodes (
                node_id TEXT PRIMARY KEY,
                node_type TEXT NOT NULL,
                canonical_key TEXT NOT NULL,
                status TEXT NOT NULL,
                properties_json TEXT NOT NULL,
                record_sha256 TEXT NOT NULL,
                UNIQUE(node_type, canonical_key)
            );
            CREATE TABLE edges (
                edge_id TEXT PRIMARY KEY,
                from_node_id TEXT NOT NULL REFERENCES nodes(node_id),
                relation_type TEXT NOT NULL,
                to_node_id TEXT NOT NULL REFERENCES nodes(node_id),
                relation_class TEXT NOT NULL,
                status TEXT NOT NULL,
                basis_kind TEXT NOT NULL,
                basis_rule TEXT NOT NULL,
                properties_json TEXT NOT NULL,
                record_sha256 TEXT NOT NULL
            );
            CREATE TABLE edge_evidence (
                edge_id TEXT NOT NULL REFERENCES edges(edge_id),
                evidence_id TEXT NOT NULL REFERENCES source_evidence(evidence_id),
                PRIMARY KEY(edge_id, evidence_id)
            ) WITHOUT ROWID;
            CREATE INDEX nodes_type_key_idx ON nodes(node_type, canonical_key);
            CREATE INDEX edges_from_type_idx ON edges(from_node_id, relation_type);
            CREATE INDEX edges_to_type_idx ON edges(to_node_id, relation_type);
            CREATE INDEX edge_evidence_evidence_idx ON edge_evidence(evidence_id);
            """
        )
        connection.executemany(
            "INSERT INTO metadata VALUES (?, ?)",
            [(key, canonical_json(value)) for key, value in sorted(metadata.items())],
        )
        connection.executemany(
            "INSERT INTO source_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(
                record["evidence_id"], record["document_id"], record["relative_path"],
                record["source_sha256"], canonical_json(record["locator"]),
                record["observed_text"], record["observed_sha256"], record["record_sha256"],
            ) for record in (
                _source_evidence_record(item)
                for item in sorted(evidence, key=lambda value: value.evidence_id)
            )],
        )
        connection.executemany(
            "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?)",
            [(
                item["node_id"], item["node_type"], item["canonical_key"],
                item["status"], canonical_json(item["properties"]), item["record_sha256"],
            ) for item in nodes],
        )
        connection.executemany(
            "INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(
                item["edge_id"], item["from_node_id"], item["relation_type"],
                item["to_node_id"], item["relation_class"], item["status"],
                item["basis_kind"], item["basis_rule"],
                canonical_json(item["properties"]), item["record_sha256"],
            ) for item in edges],
        )
        connection.executemany(
            "INSERT INTO edge_evidence VALUES (?, ?)",
            [
                (edge["edge_id"], evidence_id)
                for edge in edges
                for evidence_id in edge["supporting_evidence_ids"]
            ],
        )
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("sqlite_foreign_key_check_failed")
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()


def build(
    documents_path: Path,
    evidence_path: Path,
    output_path: Path,
    state_path: Path,
) -> dict[str, Any]:
    """Build and atomically publish one deterministic SQLite graph snapshot."""
    documents_path = Path(documents_path).resolve(strict=True)
    evidence_path = Path(evidence_path).resolve(strict=True)
    output_path = Path(output_path).resolve(strict=False)
    state_path = Path(state_path).resolve(strict=False)
    if documents_path == evidence_path:
        raise ValueError("documents_and_evidence_inputs_must_differ")
    if documents_path.name != "semantic-documents.jsonl":
        raise ValueError("documents_input_must_be_semantic_documents_jsonl")
    if evidence_path.name != "safe-answer-evidence.jsonl":
        raise ValueError("evidence_input_must_be_safe_answer_evidence_jsonl")
    if output_path == state_path:
        raise ValueError("database_and_state_outputs_must_differ")
    if output_path in {documents_path, evidence_path} or state_path in {documents_path, evidence_path}:
        raise ValueError("outputs_must_not_overwrite_inputs")
    document_records = _read_jsonl(documents_path, "document")
    evidence_records = _read_jsonl(evidence_path, "evidence")
    documents, evidence = _prepare_inputs(document_records, evidence_records)
    quality_counts = {
        disposition: sum(
            item.quality_disposition == disposition for item in evidence
        )
        for disposition in (
            "eligible_native",
            "eligible_high",
            "excluded_provisional",
            "excluded_marker",
            "excluded_invalid_quality",
        )
    }
    graph_evidence = [
        item for item in evidence
        if item.quality_disposition in {"eligible_native", "eligible_high"}
    ]
    excluded_evidence_count = len(evidence) - len(graph_evidence)
    # The document manifest can include a document whose Evidence was entirely
    # quarantined.  Keep input coverage separate from safe graph membership.
    tables = _structured_tables(graph_evidence)
    rows = _parse_structured_rows(tables)
    facts = _document_facts(documents, graph_evidence, tables)

    graph = GraphAccumulator()
    diagnostics: dict[str, int] = {
        "quality_gate_eligible_native": quality_counts["eligible_native"],
        "quality_gate_eligible_high": quality_counts["eligible_high"],
        "quality_gate_excluded_provisional": quality_counts[
            "excluded_provisional"
        ],
        "quality_gate_excluded_marker": quality_counts["excluded_marker"],
        "quality_gate_excluded_invalid_quality": quality_counts[
            "excluded_invalid_quality"
        ],
        "quality_gate_excluded_total": excluded_evidence_count,
    }
    _add_identity_definitions(graph, facts, graph_evidence)
    _add_assignments(graph, rows)
    _add_employee_identities(graph, facts, graph_evidence, diagnostics)
    claims = _add_claims(graph, facts, graph_evidence, rows)
    _add_version_relations(graph, facts, claims)
    _add_change_reasons(graph, facts, claims)
    nodes, edges, graph_snapshot_id, logical_sha256 = _materialize_graph(
        graph, evidence,
    )
    if not nodes or not edges:
        raise ValueError("no_verified_semantic_graph_records")
    document_by_evidence_id = {
        item.evidence_id: item.document_id for item in evidence
    }
    graph_document_count = len({
        document_by_evidence_id[evidence_id]
        for edge in edges
        for evidence_id in edge["supporting_evidence_ids"]
    })

    metadata = {
        "schema_version": SCHEMA_VERSION,
        "builder": BUILDER_NAME,
        "builder_version": BUILDER_VERSION,
        "status": "complete",
        "question_independent": True,
        "external_network_used": False,
        "graph_snapshot_id": graph_snapshot_id,
        "logical_snapshot_sha256": logical_sha256,
        "documents_input_sha256": sha256_file(documents_path),
        "evidence_input_sha256": sha256_file(evidence_path),
        "document_count": graph_document_count,
        "source_evidence_count": len(evidence),
        "node_count": len(nodes),
        "edge_count": len(edges),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=output_path.name + ".", suffix=".building", dir=output_path.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink()
    try:
        _write_database(temporary, metadata, evidence, nodes, edges)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        sqlite_sha256 = sha256_file(temporary)
        state = {
            **metadata,
            "record_type": "cross_document_semantic_graph_state",
            "sqlite_sha256": sqlite_sha256,
            "counts": {
                "input_documents": len(documents),
                "documents": graph_document_count,
                "source_evidence": len(evidence),
                "nodes": len(nodes),
                "edges": len(edges),
                "edge_evidence": sum(len(item["supporting_evidence_ids"]) for item in edges),
                **dict(sorted(diagnostics.items())),
            },
            "relation_type_counts": dict(sorted(
                (relation_type, sum(edge["relation_type"] == relation_type for edge in edges))
                for relation_type in {edge["relation_type"] for edge in edges}
            )),
            "output": {"sqlite_file": output_path.name, "state_file": state_path.name},
        }
        # Prepare the state completely before either public path is replaced.
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_descriptor, state_temporary_name = tempfile.mkstemp(
            prefix=state_path.name + ".", suffix=".tmp", dir=state_path.parent,
        )
        state_temporary = Path(state_temporary_name)
        try:
            with os.fdopen(state_descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, output_path)
            os.replace(state_temporary, state_path)
        finally:
            state_temporary.unlink(missing_ok=True)
        return state
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a deterministic cross-document semantic graph from Layer 1 records only."
    )
    parser.add_argument("--documents", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="SQLite snapshot path")
    parser.add_argument("--state", type=Path, required=True, help="Atomic state JSON path")
    args = parser.parse_args()
    state = build(args.documents, args.evidence, args.output, args.state)
    print(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
