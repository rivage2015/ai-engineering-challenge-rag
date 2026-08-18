#!/usr/bin/env python3
"""Validate the shadow PDFPageObservation-to-Evidence overlay.

This validator intentionally treats the overlay as a specialised boundary,
not as a complete intermediate directory.  It verifies the published
Evidence schema and identity formulas, binds every output record to both one
PDFPageObservation and one pre-existing page Evidence record, and rejects the
normal intermediate entry points that would make the shadow output
accidentally consumable by the production search builders.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

import build_pdf_page_observations as pdf_observations
import build_pdf_page_evidence as adapter
import validate_intermediate_records as intermediate


DEFAULT_EXPECTED_COUNT = 220
MAX_JSON_BYTES = 512 * 1024 * 1024
MAX_JSONL_RECORDS = 500_000
EVIDENCE_FILE_NAMES = (adapter.EVIDENCE_FILE,)
STATE_FILE_NAMES = (adapter.STATE_FILE,)
FORBIDDEN_NORMAL_OUTPUTS = tuple(sorted(adapter.FORBIDDEN_OUTPUT_NAMES))
REQUIRED_STATE_FLAGS = dict(adapter.STATE_FLAGS)


class PDFPageEvidenceValidationError(ValueError):
    """Raised when the shadow overlay violates its validation contract."""


def _sha256_file(path: Path) -> str:
    return pdf_observations.sha256_file(path)


def _require_regular_file(path: Path, label: str) -> Path:
    try:
        return adapter._regular_file(path, label)
    except (OSError, adapter.PDFPageEvidenceError) as exc:
        raise PDFPageEvidenceValidationError(
            f"{label} violates the physical path boundary: {exc}"
        ) from exc


def _require_directory(path: Path, label: str) -> Path:
    try:
        return adapter._resolve_directory(path, label)
    except (OSError, adapter.PDFPageEvidenceError) as exc:
        raise PDFPageEvidenceValidationError(
            f"{label} violates the physical path boundary: {exc}"
        ) from exc


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], str]:
    resolved = _require_regular_file(path, label)
    try:
        value = intermediate.strict_json_loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PDFPageEvidenceValidationError(
            f"{label} is not strict JSON: {resolved}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise PDFPageEvidenceValidationError(f"{label} root must be an object")
    return value, _sha256_file(resolved)


def _read_jsonl(path: Path, label: str) -> tuple[list[dict[str, Any]], str]:
    resolved = _require_regular_file(path, label)
    records: list[dict[str, Any]] = []
    try:
        with resolved.open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if not raw.strip():
                    raise PDFPageEvidenceValidationError(
                        f"{label}:{line_number}: blank JSONL line"
                    )
                try:
                    value = intermediate.strict_json_loads(raw)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise PDFPageEvidenceValidationError(
                        f"{label}:{line_number}: invalid JSON: {exc}"
                    ) from exc
                if not isinstance(value, dict):
                    raise PDFPageEvidenceValidationError(
                        f"{label}:{line_number}: record must be an object"
                    )
                records.append(value)
                if len(records) > MAX_JSONL_RECORDS:
                    raise PDFPageEvidenceValidationError(
                        f"{label} exceeds {MAX_JSONL_RECORDS} records"
                    )
    except (OSError, UnicodeDecodeError) as exc:
        raise PDFPageEvidenceValidationError(
            f"{label} cannot be read: {resolved}: {exc}"
        ) from exc
    return records, _sha256_file(resolved)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_shard(base: Path, relative_path: object, label: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        raise PDFPageEvidenceValidationError(
            f"{label}.relative_path must be a non-empty string"
        )
    raw = Path(relative_path)
    if raw.is_absolute() or ".." in raw.parts:
        raise PDFPageEvidenceValidationError(
            f"{label}.relative_path must remain beneath base intermediate"
        )
    base_absolute = Path(os.path.abspath(base))
    candidate = Path(os.path.abspath(base_absolute / raw))
    if not _inside(candidate, base_absolute):
        raise PDFPageEvidenceValidationError(
            f"{label}.relative_path escapes base intermediate"
        )
    current = base_absolute
    for component in raw.parts:
        current = current / component
        if current.is_symlink():
            raise PDFPageEvidenceValidationError(
                f"{label}.relative_path contains a symlink component"
            )
    return _require_regular_file(candidate, label)


def _validate_shard_metadata(
    path: Path,
    records: Sequence[Mapping[str, Any]],
    metadata: Mapping[str, Any],
    label: str,
) -> list[str]:
    errors: list[str] = []
    actual_sha256 = _sha256_file(path)
    actual_size = path.stat().st_size
    if metadata.get("sha256") != actual_sha256:
        errors.append(f"{label}: shard sha256 mismatch")
    if metadata.get("size_bytes") != actual_size:
        errors.append(f"{label}: shard size_bytes mismatch")
    if metadata.get("record_count") != len(records):
        errors.append(f"{label}: shard record_count mismatch")
    return errors


def _observation_sort_key(record: Mapping[str, Any]) -> tuple[str, str, int, str]:
    source = record.get("source")
    page = record.get("page")
    relative_path = source.get("relative_path") if isinstance(source, Mapping) else ""
    source_sha = source.get("sha256") if isinstance(source, Mapping) else ""
    page_number = page.get("page_number") if isinstance(page, Mapping) else 0
    observation_id = record.get("observation_id")
    return (
        adapter._normalized(relative_path) if isinstance(relative_path, str) else "",
        source_sha if isinstance(source_sha, str) else "",
        page_number if isinstance(page_number, int) and not isinstance(page_number, bool) else 0,
        observation_id if isinstance(observation_id, str) else "",
    )


def _validate_official_evidence(
    record: Mapping[str, Any],
    label: str,
    schema_validator: Any,
) -> list[str]:
    errors = intermediate.schema_record_errors(
        "evidence", record, label, schema_validator
    )
    errors.extend(intermediate.question_boundary_errors("evidence", dict(record), label))
    if errors:
        return errors
    content = record["content"]
    try:
        expected_content_sha = intermediate.digest_value(
            intermediate.content_hash_payload(content)
        )
    except ValueError as exc:
        errors.append(f"{label}: {exc}")
        return errors
    if content.get("sha256") != expected_content_sha:
        errors.append(f"{label}: content.sha256 does not match the official formula")
    expected_id = intermediate.stable_id(
        "ev",
        {
            "document_id": record.get("document_id"),
            "evidence_type": record.get("evidence_type"),
            "location": record.get("location"),
            "content_sha256": content.get("sha256"),
        },
    )
    if record.get("evidence_id") != expected_id:
        errors.append(f"{label}: evidence_id does not match the official formula")
    if "raw_text" in content:
        expected_normalized = intermediate.normalize_text(content["raw_text"])
        if content.get("normalized_text") != expected_normalized:
            errors.append(f"{label}: normalized_text is missing or inconsistent")
    if "raw_value" in content and content.get("normalized_value") != content["raw_value"]:
        errors.append(f"{label}: normalized_value is missing or inconsistent")
    return errors


def _validate_native_lineage(
    evidence: Mapping[str, Any],
    observation: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
    observations_sha256: str,
    base_state_sha256: str,
    label: str,
) -> list[str]:
    native = evidence.get("native_properties")
    if not isinstance(native, Mapping):
        return [f"{label}: native_properties lineage is required"]
    expected_native = {
        "pdf_page_observation_id": observation["observation_id"],
        "pdf_page_observations_sha256": observations_sha256,
        "pdf_page_observation_input_sha256": observation["hashes"]["input_sha256"],
        "pdf_page_observation_content_sha256": observation["hashes"]["content_sha256"],
        "pdf_page_observation_signature_sha256": observation["hashes"]["signature_sha256"],
        "pdf_page_observation_record_integrity_sha256": observation["hashes"][
            "record_integrity_sha256"
        ],
        "base_intermediate_state_sha256": base_state_sha256,
        "base_page_evidence_id": base_evidence["evidence_id"],
        "base_page_evidence_content_sha256": base_evidence["content"]["sha256"],
        "source_sha256": observation["source"]["sha256"],
        "page_count": observation["page"]["page_count"],
        "coordinate_system": observation["page"]["coordinate_system"],
        "extraction_route": observation["extraction"]["route"],
        "route_reason": observation["extraction"]["route_reason"],
        "observation_status": observation["status"],
        "raw_text_modified": False,
        "shadow_only": True,
        "evidence_connected": True,
        "intermediate_connected": False,
        "search_unit_connected": False,
        "production_index_connected": False,
        "content_arbitrated": False,
    }
    errors: list[str] = []
    if dict(native) != expected_native:
        errors.append(f"{label}.native_properties: exact lineage mismatch")
    if evidence.get("parent_evidence_id") != base_evidence.get("evidence_id"):
        errors.append(
            f"{label}: parent_evidence_id does not uniquely bind the base page Evidence"
        )
    return errors


def _validate_geometry(
    evidence: Mapping[str, Any],
    observation: Mapping[str, Any],
    label: str,
) -> list[str]:
    geometry = evidence.get("geometry")
    if not isinstance(geometry, Mapping):
        return [f"{label}.geometry: required object is missing"]
    page = observation["page"]
    errors: list[str] = []
    expected = {
        "coordinate_space": "page",
        "unit": "pt",
        "x": 0,
        "y": 0,
        "width": page["width_pt"],
        "height": page["height_pt"],
        "rotation_deg": page["rotation_degrees"],
    }
    if dict(geometry) != expected:
        errors.append(f"{label}.geometry: exact page geometry mismatch")
    return errors


def _validate_adapter_shape(
    evidence: Mapping[str, Any],
    observation: Mapping[str, Any],
    run_at: object,
    label: str,
) -> list[str]:
    errors: list[str] = []
    expected_fields = {
        "schema_version",
        "record_type",
        "evidence_id",
        "document_id",
        "evidence_type",
        "location",
        "content",
        "geometry",
        "parent_evidence_id",
        "ordinal",
        "native_properties",
        "provenance",
    }
    if set(evidence) != expected_fields:
        errors.append(f"{label}: fields do not exactly match the shadow adapter")
    expected_location = {
        "page_number": observation["page"]["page_number"],
        "locator_text": adapter.LOCATOR_TEXT,
    }
    if evidence.get("location") != expected_location:
        errors.append(f"{label}.location: exact shadow locator mismatch")
    if evidence.get("ordinal") != observation["page"]["page_number"]:
        errors.append(f"{label}.ordinal: must equal page_number")
    warnings = []
    if observation["status"] == "needs_review":
        warnings.append(
            "PDF page observation requires review; raw readings and unresolved "
            "values are retained without arbitration"
        )
    expected_provenance = {
        "extraction_method": "pdf_page_observation_shadow_adaptation",
        "extractor": adapter.ADAPTER,
        "extractor_version": adapter.ADAPTER_VERSION,
        "extracted_at": run_at,
        "deterministic": True,
        "confidence": 1.0 if observation["status"] == "observed" else 0.0,
        "warnings": warnings,
    }
    if evidence.get("provenance") != expected_provenance:
        errors.append(f"{label}.provenance: exact adapter provenance mismatch")
    return errors


def _validate_lossless_content(
    evidence: Mapping[str, Any],
    observation: Mapping[str, Any],
    label: str,
) -> list[str]:
    """Require a non-textual, lossless copy of the unresolved observation facts."""
    content = evidence.get("content")
    if not isinstance(content, Mapping):
        return [f"{label}.content: must be an object"]
    errors: list[str] = []
    raw_value = content.get("raw_value")
    if not isinstance(raw_value, Mapping):
        errors.append(f"{label}.content.raw_value: observation payload is required")
        return errors
    expected_raw_value = {
        "extraction": observation["extraction"],
        "native": observation["native"],
        "ocr": observation["ocr"],
        "conflicts": observation["conflicts"],
        "unresolved": observation["unresolved"],
        "status": observation["status"],
        "hashes": observation["hashes"],
    }
    if dict(raw_value) != expected_raw_value:
        errors.append(
            f"{label}.content.raw_value: must exactly preserve PDFPageObservation facts"
        )
    expected_content = adapter.content(raw_value=expected_raw_value)
    if dict(content) != expected_content:
        errors.append(f"{label}.content: exact shadow content envelope mismatch")
    return errors


def _sensitive_state_paths(value: object, path: str = "state") -> list[str]:
    errors: list[str] = []
    pending: list[tuple[object, str]] = [(value, path)]
    while pending:
        current, current_path = pending.pop()
        if isinstance(current, Mapping):
            pending.extend(
                (child, f"{current_path}.{key}")
                for key, child in current.items()
            )
        elif isinstance(current, list):
            pending.extend(
                (child, f"{current_path}[{index}]")
                for index, child in enumerate(current)
            )
        elif isinstance(current, str):
            for component in current.replace("\\", "/").split("/"):
                if pdf_observations.FORBIDDEN_DATA_RE.search(component):
                    errors.append(
                        f"{current_path}: forbidden question/answer/prediction/gold reference"
                    )
                    break
    return errors


def _resolve_named_output(
    overlay_dir: Path,
    explicit: Path | None,
    candidates: Sequence[str],
    label: str,
) -> Path:
    if explicit is not None:
        candidate = explicit if explicit.is_absolute() else overlay_dir / explicit
        if ".." in candidate.parts:
            raise PDFPageEvidenceValidationError(
                f"{label} must not contain parent traversal"
            )
        absolute = Path(os.path.abspath(candidate))
        if not _inside(absolute, Path(os.path.abspath(overlay_dir))):
            raise PDFPageEvidenceValidationError(
                f"{label} must remain inside overlay_dir when both are supplied"
            )
        if absolute.name != candidates[0]:
            raise PDFPageEvidenceValidationError(
                f"{label} must use the canonical shadow filename {candidates[0]!r}"
            )
        return absolute
    present = [overlay_dir / name for name in candidates if (overlay_dir / name).exists()]
    if len(present) != 1:
        raise PDFPageEvidenceValidationError(
            f"{label}: expected exactly one of {[str(overlay_dir / name) for name in candidates]}, "
            f"found {len(present)}"
        )
    return present[0]


def _resolve_outputs(
    overlay_dir: Path | None,
    evidence_path: Path | None,
    state_path: Path | None,
) -> tuple[Path, Path, tuple[Path, ...]]:
    if overlay_dir is None:
        if evidence_path is None or state_path is None:
            raise PDFPageEvidenceValidationError(
                "provide overlay_dir or both --evidence and --state"
            )
        evidence = _require_regular_file(evidence_path, "PDF page Evidence output")
        state = _require_regular_file(state_path, "PDF page Evidence state")
        if evidence.name != EVIDENCE_FILE_NAMES[0]:
            raise PDFPageEvidenceValidationError(
                f"evidence must use {EVIDENCE_FILE_NAMES[0]!r}"
            )
        if state.name != STATE_FILE_NAMES[0]:
            raise PDFPageEvidenceValidationError(
                f"state must use {STATE_FILE_NAMES[0]!r}"
            )
        audit_dirs = tuple(dict.fromkeys((evidence.parent, state.parent)))
        return evidence, state, audit_dirs
    overlay = _require_directory(overlay_dir, "overlay_dir")
    evidence = _resolve_named_output(
        overlay, evidence_path, EVIDENCE_FILE_NAMES, "evidence"
    )
    state = _resolve_named_output(overlay, state_path, STATE_FILE_NAMES, "state")
    present_names = {path.name for path in overlay.iterdir()}
    expected_names = {EVIDENCE_FILE_NAMES[0], STATE_FILE_NAMES[0]}
    if present_names != expected_names:
        raise PDFPageEvidenceValidationError(
            "overlay_dir must contain exactly the two canonical shadow files: "
            f"found={sorted(present_names)}"
        )
    return evidence, state, (overlay,)


def _normal_output_errors(audit_dirs: Sequence[Path], state_path: Path) -> list[str]:
    errors: list[str] = []
    if state_path.name == "build-state.json":
        errors.append("state must not use the normal intermediate build-state.json name")
    for directory in audit_dirs:
        for name in FORBIDDEN_NORMAL_OUTPUTS:
            path = directory / name
            if path.exists():
                errors.append(
                    f"shadow overlay emitted forbidden normal intermediate output: {path}"
                )
    return errors


def _load_base_bindings(
    base_intermediate: Path,
    observations: Sequence[Mapping[str, Any]],
) -> tuple[
    dict[tuple[str, int], dict[str, Any]],
    dict[str, dict[str, Any]],
    set[str],
    dict[str, Any],
    str,
    list[str],
]:
    base = _require_directory(base_intermediate, "base intermediate")
    state_path = base / "build-state.json"
    state, state_sha256 = _read_json(state_path, "base build-state")
    errors: list[str] = []
    reserved_paths = intermediate.reserved_query_paths(state, "base_state")
    if reserved_paths:
        errors.append(
            "base build-state contains query-layer metadata: "
            + ", ".join(sorted(set(reserved_paths)))
        )
    if state.get("build_status") != "complete":
        errors.append("base build-state: build_status must be complete")
    if state.get("state_version") != "1":
        errors.append("base build-state: state_version must be '1'")
    if not isinstance(state.get("extractor"), str) or not state["extractor"]:
        errors.append("base build-state: extractor is missing")
    if (
        not isinstance(state.get("extractor_version"), str)
        or not state["extractor_version"]
    ):
        errors.append("base build-state: extractor_version is missing")
    if not isinstance(state.get("run_at"), str) or not state["run_at"]:
        errors.append("base build-state: run_at is missing")
    entries = state.get("entries")
    if not isinstance(entries, Mapping):
        raise PDFPageEvidenceValidationError(
            "base build-state.entries must be an object keyed by relative path"
        )
    input_paths = state.get("input_paths")
    if not isinstance(input_paths, list):
        errors.append("base build-state.input_paths must be an array")
    else:
        if (
            any(not isinstance(item, str) for item in input_paths)
            or len(input_paths) != len(set(input_paths))
            or set(input_paths) != set(entries)
        ):
            errors.append("base build-state.input_paths do not exactly bind entries")
        for index, relative_path in enumerate(input_paths, start=1):
            if not isinstance(relative_path, str):
                continue
            try:
                adapter._forbid_query_source_path(
                    relative_path, f"base input_paths[{index}]"
                )
            except ValueError as exc:
                errors.append(str(exc))
    by_path: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for observation in observations:
        by_path[observation["source"]["relative_path"]].append(observation)
    base_pages: dict[tuple[str, int], dict[str, Any]] = {}
    base_documents: dict[str, dict[str, Any]] = {}
    base_evidence_ids: set[str] = set()
    evidence_validator = intermediate._published_schema_validator("evidence")
    document_validator = intermediate._published_schema_validator("document")
    for relative_path in sorted(by_path):
        grouped = by_path[relative_path]
        first = grouped[0]
        entry = entries.get(relative_path)
        entry_label = f"base entry {relative_path!r}"
        if not isinstance(entry, Mapping):
            errors.append(f"{entry_label}: missing")
            continue
        expected_document_id = first["document_id"]
        expected_source_sha = first["source"]["sha256"]
        if entry.get("relative_path") != relative_path:
            errors.append(f"{entry_label}: relative_path mismatch")
        if entry.get("document_id") != expected_document_id:
            errors.append(f"{entry_label}: document_id mismatch")
        if entry.get("source_sha256") != expected_source_sha:
            errors.append(f"{entry_label}: source_sha256 mismatch")
        if entry.get("status") not in {"success", "partial"}:
            errors.append(f"{entry_label}: status must be success or partial")
        shards = entry.get("shards")
        if not isinstance(shards, Mapping):
            errors.append(f"{entry_label}: shards must be an object")
            continue
        shard_records: dict[str, list[dict[str, Any]]] = {}
        for kind in ("documents", "evidence"):
            metadata = shards.get(kind)
            shard_label = f"{entry_label}.{kind}"
            if not isinstance(metadata, Mapping):
                errors.append(f"{shard_label}: metadata is missing")
                continue
            try:
                shard_path = _resolve_shard(
                    base, metadata.get("relative_path"), shard_label
                )
                records, _ = _read_jsonl(shard_path, shard_label)
            except PDFPageEvidenceValidationError as exc:
                errors.append(str(exc))
                continue
            errors.extend(
                _validate_shard_metadata(shard_path, records, metadata, shard_label)
            )
            shard_records[kind] = records
        documents = shard_records.get("documents", [])
        if len(documents) != 1:
            errors.append(
                f"{entry_label}: documents shard must contain exactly one record"
            )
        matching_documents = [
            record
            for record in documents
            if record.get("document_id") == expected_document_id
        ]
        if len(matching_documents) != 1:
            errors.append(
                f"{entry_label}: expected one matching base Document, found "
                f"{len(matching_documents)}"
            )
        else:
            document = matching_documents[0]
            label = f"{entry_label}.Document"
            errors.extend(
                intermediate.schema_record_errors(
                    "document", document, label, document_validator
                )
            )
            errors.extend(intermediate.question_boundary_errors("document", document, label))
            source = document.get("source")
            if isinstance(source, Mapping):
                if source.get("relative_path") != relative_path:
                    errors.append(f"{label}: source.relative_path mismatch")
                if source.get("sha256") != expected_source_sha:
                    errors.append(f"{label}: source.sha256 mismatch")
                if source.get("size_bytes") != first["source"]["size_bytes"]:
                    errors.append(f"{label}: source.size_bytes mismatch")
                expected_stable_document_id = intermediate.stable_id(
                    "doc",
                    {
                        "relative_path": source.get("relative_path"),
                        "source_sha256": source.get("sha256"),
                    },
                )
                if document.get("document_id") != expected_stable_document_id:
                    errors.append(f"{label}: document_id is not stable")
            extraction = document.get("extraction")
            if not isinstance(extraction, Mapping) or extraction.get("status") != entry.get("status"):
                errors.append(f"{label}: extraction status does not match entry")
            base_documents[expected_document_id] = document
        entry_page_numbers: set[int] = set()
        for record_index, record in enumerate(shard_records.get("evidence", []), 1):
            label = f"{entry_label}.Evidence[{record_index}]"
            errors.extend(
                _validate_official_evidence(record, label, evidence_validator)
            )
            evidence_id = record.get("evidence_id")
            if isinstance(evidence_id, str):
                if evidence_id in base_evidence_ids:
                    errors.append(f"{label}: duplicate base Evidence id")
                base_evidence_ids.add(evidence_id)
            if record.get("document_id") != expected_document_id:
                errors.append(f"{label}: document_id mismatch")
            if record.get("evidence_type") != "page":
                continue
            location = record.get("location")
            page_number = (
                location.get("page_number") if isinstance(location, Mapping) else None
            )
            key = (record.get("document_id"), page_number)
            if (
                record.get("document_id") != expected_document_id
                or isinstance(page_number, bool)
                or not isinstance(page_number, int)
            ):
                errors.append(f"{label}: invalid physical page binding")
                continue
            if key in base_pages:
                errors.append(f"{label}: duplicate base physical page {key}")
            else:
                base_pages[key] = record
                entry_page_numbers.add(page_number)
        expected_page_count = first["page"]["page_count"]
        if entry_page_numbers != set(range(1, expected_page_count + 1)):
            errors.append(
                f"{entry_label}: base page Evidence does not exactly cover "
                f"1..{expected_page_count}"
            )
        for observation in grouped:
            key = (observation["document_id"], observation["page"]["page_number"])
            if key not in base_pages:
                errors.append(
                    f"{entry_label}: no unique base page Evidence for physical page {key}"
                )
    return (
        base_pages,
        base_documents,
        base_evidence_ids,
        state,
        state_sha256,
        errors,
    )


def _validate_state(
    state: Mapping[str, Any],
    observations_sha256: str,
    observation_count: int,
    base_state: Mapping[str, Any],
    base_state_sha256: str,
    evidence_sha256: str,
    evidence_path: Path,
    evidence_count: int,
    document_count: int,
    route_counts: Mapping[str, int],
    status_counts: Mapping[str, int],
    evidence_type_counts: Mapping[str, int],
) -> list[str]:
    errors: list[str] = []
    expected_top_level = {
        "state_version",
        "build_status",
        "deterministic",
        "adapter",
        "adapter_version",
        "run_at",
        "inputs",
        "output",
        "counts",
        "flags",
    }
    if set(state) != expected_top_level:
        errors.append(
            "state: fields must exactly match the published shadow contract; "
            f"found={sorted(state)}"
        )
    expected_scalars = {
        "state_version": "0.1",
        "build_status": "complete",
        "deterministic": True,
        "adapter": adapter.ADAPTER,
        "adapter_version": adapter.ADAPTER_VERSION,
        "run_at": base_state.get("run_at"),
    }
    for field, expected in expected_scalars.items():
        if state.get(field) != expected:
            errors.append(f"state.{field}: expected {expected!r}")
    errors.extend(intermediate.question_boundary_errors("evidence", dict(state), "state"))
    errors.extend(_sensitive_state_paths(state))
    schema_path = Path(adapter.__file__).resolve().parents[1] / "schemas" / "evidence.schema.json"
    expected_inputs = {
        "pdf_page_observations": {
            "sha256": observations_sha256,
            "record_count": observation_count,
        },
        "base_intermediate": {
            "build_state_sha256": base_state_sha256,
            "extractor": base_state.get("extractor"),
            "extractor_version": base_state.get("extractor_version"),
        },
        "evidence_schema": {
            "schema_version": "0.1",
            "sha256": _sha256_file(schema_path),
        },
    }
    if state.get("inputs") != expected_inputs:
        errors.append("state.inputs: exact input lineage mismatch")
    expected_output = {
        "relative_path": adapter.EVIDENCE_FILE,
        "sha256": evidence_sha256,
        "size_bytes": evidence_path.stat().st_size,
        "record_count": evidence_count,
    }
    if state.get("output") != expected_output:
        errors.append("state.output: exact Evidence output binding mismatch")
    expected_counts = {
        "documents": document_count,
        "pages": evidence_count,
        "routes": dict(route_counts),
        "statuses": dict(status_counts),
        "evidence_types": dict(evidence_type_counts),
    }
    if state.get("counts") != expected_counts:
        errors.append("state.counts: exact count summary mismatch")
    if state.get("flags") != REQUIRED_STATE_FLAGS:
        errors.append("state.flags: exact safety flags mismatch")
    return errors


def validate(
    *,
    observations_path: Path,
    base_intermediate: Path,
    overlay_dir: Path | None = None,
    evidence_path: Path | None = None,
    state_path: Path | None = None,
    expected_count: int = DEFAULT_EXPECTED_COUNT,
) -> dict[str, Any]:
    """Validate one complete, ordered shadow Evidence overlay."""
    if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count <= 0:
        raise PDFPageEvidenceValidationError("expected_count must be a positive integer")
    evidence_file, state_file, audit_dirs = _resolve_outputs(
        overlay_dir, evidence_path, state_path
    )
    errors = _normal_output_errors(audit_dirs, state_file)
    try:
        observations, observations_sha256 = pdf_observations.load_jsonl(
            _require_regular_file(observations_path, "PDFPageObservation input"),
            "PDFPageObservation input",
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PDFPageEvidenceValidationError(
            f"cannot load PDFPageObservation input: {exc}"
        ) from exc
    evidence_records, evidence_sha256 = _read_jsonl(
        evidence_file, "PDF page Evidence output"
    )
    state, state_sha256 = _read_json(state_file, "PDF page Evidence state")
    observations.sort(key=_observation_sort_key)
    if len(observations) != expected_count:
        errors.append(
            f"PDFPageObservation count: expected {expected_count}, found {len(observations)}"
        )
    if len(evidence_records) != expected_count:
        errors.append(
            f"Evidence count: expected {expected_count}, found {len(evidence_records)}"
        )
    if len(observations) != len(evidence_records):
        errors.append(
            "PDFPageObservation and Evidence counts are not one-to-one: "
            f"{len(observations)} != {len(evidence_records)}"
        )
    observation_ids: set[str] = set()
    observation_pages: set[tuple[str, int]] = set()
    observation_sources: set[tuple[str, str, int]] = set()
    observation_pages_by_document: dict[str, set[int]] = defaultdict(set)
    observation_page_counts: dict[str, set[int]] = defaultdict(set)
    valid_observations: list[dict[str, Any]] = []
    route_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    for index, observation in enumerate(observations, start=1):
        label = f"observation[{index}]"
        semantic = pdf_observations.validate_observation(observation)
        errors.extend(f"{label}{error}" for error in semantic)
        if semantic:
            continue
        try:
            adapter._forbid_query_source_path(
                observation["source"]["relative_path"],
                f"{label} source.relative_path",
            )
        except ValueError as exc:
            errors.append(str(exc))
        valid_observations.append(observation)
        observation_id = observation["observation_id"]
        if observation_id in observation_ids:
            errors.append(f"{label}: duplicate observation_id {observation_id}")
        observation_ids.add(observation_id)
        page_key = (observation["document_id"], observation["page"]["page_number"])
        source_key = (
            observation["source"]["relative_path"],
            observation["source"]["sha256"],
            observation["page"]["page_number"],
        )
        if page_key in observation_pages:
            errors.append(f"{label}: duplicate physical document page {page_key}")
        if source_key in observation_sources:
            errors.append(f"{label}: duplicate physical source page {source_key}")
        observation_pages.add(page_key)
        observation_sources.add(source_key)
        observation_pages_by_document[observation["document_id"]].add(
            observation["page"]["page_number"]
        )
        observation_page_counts[observation["document_id"]].add(
            observation["page"]["page_count"]
        )
        expected_document_id = intermediate.stable_id(
            "doc",
            {
                "relative_path": observation["source"]["relative_path"],
                "source_sha256": observation["source"]["sha256"],
            },
        )
        if observation["document_id"] != expected_document_id:
            errors.append(f"{label}: document_id is not stable for source path/SHA")
        route_counts[observation["extraction"]["route"]] += 1
        status_counts[observation["status"]] += 1
    for document_id, page_counts in sorted(observation_page_counts.items()):
        if len(page_counts) != 1:
            errors.append(
                f"{document_id}: inconsistent observation page_count values "
                f"{sorted(page_counts)!r}"
            )
            continue
        page_count = next(iter(page_counts))
        expected_pages = set(range(1, page_count + 1))
        actual_pages = observation_pages_by_document[document_id]
        if actual_pages != expected_pages:
            errors.append(
                f"{document_id}: observations do not completely cover pages "
                f"1..{page_count}; found {sorted(actual_pages)!r}"
            )
    if len(valid_observations) != len(observations):
        raise PDFPageEvidenceValidationError(
            "validation failed before lineage traversal:\n- " + "\n- ".join(errors)
        )
    (
        base_pages,
        base_documents,
        base_evidence_ids,
        base_state,
        base_state_sha256,
        base_errors,
    ) = _load_base_bindings(base_intermediate, valid_observations)
    errors.extend(base_errors)
    evidence_validator = intermediate._published_schema_validator("evidence")
    evidence_ids: set[str] = set()
    evidence_pages: set[tuple[str, int]] = set()
    evidence_type_counts: Counter[str] = Counter()
    for index, evidence in enumerate(evidence_records, start=1):
        label = f"evidence[{index}]"
        errors.extend(_validate_official_evidence(evidence, label, evidence_validator))
        evidence_id = evidence.get("evidence_id")
        if isinstance(evidence_id, str):
            if evidence_id in evidence_ids:
                errors.append(f"{label}: duplicate evidence_id {evidence_id}")
            if evidence_id in base_evidence_ids:
                errors.append(f"{label}: evidence_id collides with base Evidence")
            evidence_ids.add(evidence_id)
        evidence_type = evidence.get("evidence_type")
        if isinstance(evidence_type, str):
            evidence_type_counts[evidence_type] += 1
        if evidence_type != "other":
            errors.append(
                f"{label}: evidence_type must remain 'other' for the shadow boundary"
            )
        provenance = evidence.get("provenance")
        if not isinstance(provenance, Mapping) or provenance.get("deterministic") is not True:
            errors.append(f"{label}.provenance.deterministic: expected true")
        if index > len(observations):
            continue
        observation = observations[index - 1]
        page_number = observation["page"]["page_number"]
        document_id = observation["document_id"]
        if evidence.get("document_id") != document_id:
            errors.append(f"{label}: document_id breaks observation order/binding")
        location = evidence.get("location")
        output_page = (
            location.get("page_number") if isinstance(location, Mapping) else None
        )
        if output_page != page_number:
            errors.append(f"{label}: page_number breaks observation order/binding")
        page_key = (evidence.get("document_id"), output_page)
        if page_key in evidence_pages:
            errors.append(f"{label}: duplicate output physical page {page_key}")
        evidence_pages.add(page_key)
        if evidence.get("ordinal") is not None and evidence.get("ordinal") != page_number:
            errors.append(f"{label}: ordinal must equal physical page number")
        errors.extend(
            _validate_adapter_shape(
                evidence,
                observation,
                base_state.get("run_at"),
                label,
            )
        )
        errors.extend(_validate_lossless_content(evidence, observation, label))
        base_evidence = base_pages.get((document_id, page_number))
        if base_evidence is None:
            errors.append(f"{label}: base page Evidence binding is absent")
        else:
            errors.extend(
                _validate_native_lineage(
                    evidence,
                    observation,
                    base_evidence,
                    observations_sha256,
                    base_state_sha256,
                    label,
                )
            )
        errors.extend(_validate_geometry(evidence, observation, label))
    if evidence_pages != observation_pages:
        missing = sorted(observation_pages - evidence_pages)
        extra = sorted(evidence_pages - observation_pages)
        errors.append(
            f"physical page set mismatch: missing={missing[:10]!r}, extra={extra[:10]!r}"
        )
    if len(base_documents) != len({item["document_id"] for item in observations}):
        errors.append(
            "base Document binding count does not match observation document count"
        )
    errors.extend(
        _validate_state(
            state,
            observations_sha256,
            len(observations),
            base_state,
            base_state_sha256,
            evidence_sha256,
            evidence_file,
            len(evidence_records),
            len(base_documents),
            dict(sorted(route_counts.items())),
            dict(sorted(status_counts.items())),
            dict(sorted(evidence_type_counts.items())),
        )
    )
    if errors:
        raise PDFPageEvidenceValidationError(
            "validation failed:\n- " + "\n- ".join(errors)
        )
    return {
        "status": "ok",
        "counts": {
            "observations": len(observations),
            "evidence": len(evidence_records),
            "base_documents": len(base_documents),
            "physical_pages": len(evidence_pages),
            "routes": dict(sorted(route_counts.items())),
            "statuses": dict(sorted(status_counts.items())),
            "evidence_types": dict(sorted(evidence_type_counts.items())),
        },
        "hashes": {
            "pdf_page_observations_sha256": observations_sha256,
            "base_intermediate_state_sha256": base_state_sha256,
            "evidence_sha256": evidence_sha256,
            "state_sha256": state_sha256,
        },
        "paths": {
            "evidence": str(evidence_file),
            "state": str(state_file),
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "overlay_dir",
        nargs="?",
        type=Path,
        help="directory containing the shadow Evidence JSONL and its state",
    )
    parser.add_argument(
        "--observations",
        "--pdf-page-observations",
        dest="observations",
        required=True,
        type=Path,
        help="PDFPageObservation JSONL input",
    )
    parser.add_argument(
        "--base-intermediate",
        required=True,
        type=Path,
        help="complete base intermediate directory containing build-state.json",
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        help="explicit shadow Evidence JSONL path (or path relative to overlay_dir)",
    )
    parser.add_argument(
        "--state",
        type=Path,
        help="explicit overlay state path (or path relative to overlay_dir)",
    )
    parser.add_argument(
        "--expected-count",
        type=int,
        default=DEFAULT_EXPECTED_COUNT,
        help=f"required one-to-one record count (default: {DEFAULT_EXPECTED_COUNT})",
    )
    args = parser.parse_args(argv)
    try:
        summary = validate(
            observations_path=args.observations,
            base_intermediate=args.base_intermediate,
            overlay_dir=args.overlay_dir,
            evidence_path=args.evidence,
            state_path=args.state,
            expected_count=args.expected_count,
        )
    except (OSError, PDFPageEvidenceValidationError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(intermediate.canonical_json(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
