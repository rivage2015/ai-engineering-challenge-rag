#!/usr/bin/env python3
"""Build a non-searchable Evidence shadow from PDFPageObservation records.

The adapter deliberately does not emit a normal intermediate bundle.  Every
record is ``evidence_type=other`` and retains the complete native/OCR facts in
``content.raw_value`` without selecting, joining, or correcting OCR text.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import errno
import json
import os
import re
import shutil
import tempfile
import unicodedata
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

import build_pdf_page_observations as pdf_observations
from probe_intermediate_records import canonical_json, content, digest_file, stable_id
import validate_intermediate_records as intermediate


ADAPTER = "pdf-page-evidence-adapter"
ADAPTER_VERSION = "0.1.0"
EVIDENCE_FILE = "pdf-page-evidence.jsonl"
STATE_FILE = "pdf-page-evidence-state.json"
BASE_STATE_FILE = "build-state.json"
LOCATOR_TEXT = "pdf-page-observation-shadow-v0.1"
MAX_JSON_BYTES = 512 * 1024 * 1024
MAX_JSONL_RECORDS = 500_000
FORBIDDEN_OUTPUT_NAMES = {
    "build-state.json",
    "documents.jsonl",
    "evidence.jsonl",
    "relations.jsonl",
    "shards",
}
STATE_FLAGS = {
    "question_independent": True,
    "question_data_used": False,
    "gold_data_used": False,
    "prediction_data_used": False,
    "answer_data_used": False,
    "shadow_only": True,
    "evidence_connected": True,
    "intermediate_connected": False,
    "search_unit_connected": False,
    "production_index_connected": False,
    "content_arbitrated": False,
}
QUERY_SOURCE_TOKEN_RE = re.compile(
    r"(?:^|[-_.])(questions?|gold|predictions?|answers?)(?:[-_.]|$)",
    re.IGNORECASE,
)
FORBIDDEN_SOURCE_COMPONENTS = {"質問回答", "正解データ", "予測データ", "回答データ"}


class PDFPageEvidenceError(ValueError):
    """Raised when a shadow input or output violates the adapter contract."""


def _forbid_query_source_path(value: str, label: str) -> None:
    parts = [part for part in value.replace("\\", "/").split("/") if part]
    for component in parts:
        if (
            component in FORBIDDEN_SOURCE_COMPONENTS
            or QUERY_SOURCE_TOKEN_RE.search(component)
        ):
            raise PDFPageEvidenceError(
                f"{label} contains a forbidden query-data component: {component}"
            )


def _normalized(value: str | Path) -> str:
    return unicodedata.normalize("NFC", str(value))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _absolute_path(path: Path, label: str) -> Path:
    raw = Path(path)
    if ".." in raw.parts:
        raise PDFPageEvidenceError(f"{label} must not contain parent traversal")
    return Path(os.path.abspath(raw))


def _forbid_physical_query_path(path: Path, label: str) -> None:
    """Reject a physical path whose lexical or resolved components are query data."""
    try:
        pdf_observations._forbid_sensitive_path(path, label)
    except pdf_observations.PDFObservationError as exc:
        raise PDFPageEvidenceError(str(exc)) from exc


def _reject_symlink_components(path: Path, label: str) -> None:
    """Reject symlinks anywhere in an absolute path, including its parents."""
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current = current / component
        if current.is_symlink():
            raise PDFPageEvidenceError(
                f"{label} contains a symlink component: {current}"
            )


def _resolve_directory(path: Path, label: str, *, must_exist: bool = True) -> Path:
    candidate = _absolute_path(path, label)
    _forbid_physical_query_path(candidate, label)
    _reject_symlink_components(candidate, label)
    if must_exist:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise PDFPageEvidenceError(f"{label} cannot be resolved: {exc}") from exc
        _forbid_physical_query_path(resolved, label)
        if not resolved.is_dir():
            raise PDFPageEvidenceError(f"{label} must be a directory")
        return resolved
    try:
        parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise PDFPageEvidenceError(f"{label} parent cannot be resolved: {exc}") from exc
    _forbid_physical_query_path(parent / candidate.name, label)
    if not parent.is_dir():
        raise PDFPageEvidenceError(f"{label} parent must be a directory")
    if os.path.lexists(candidate) and not candidate.is_dir():
        raise PDFPageEvidenceError(f"{label} must be a directory")
    return parent / candidate.name


def _regular_file(path: Path, label: str) -> Path:
    candidate = _absolute_path(path, label)
    _forbid_physical_query_path(candidate, label)
    _reject_symlink_components(candidate, label)
    try:
        resolved = candidate.resolve(strict=True)
        stat = resolved.stat()
    except OSError as exc:
        raise PDFPageEvidenceError(f"{label} cannot be read: {exc}") from exc
    _forbid_physical_query_path(resolved, label)
    if not resolved.is_file():
        raise PDFPageEvidenceError(f"{label} must be a regular file")
    if stat.st_size <= 0 or stat.st_size > MAX_JSON_BYTES:
        raise PDFPageEvidenceError(
            f"{label} size is outside the accepted range: {stat.st_size}"
        )
    return resolved


def _strict_json(path: Path, label: str) -> tuple[dict[str, Any], str]:
    resolved = _regular_file(path, label)
    try:
        value = intermediate.strict_json_loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PDFPageEvidenceError(f"{label} is not strict JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise PDFPageEvidenceError(f"{label} root must be an object")
    return value, digest_file(resolved)


def _strict_jsonl(path: Path, label: str) -> tuple[list[dict[str, Any]], str]:
    resolved = _regular_file(path, label)
    records: list[dict[str, Any]] = []
    try:
        with resolved.open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if not raw.strip():
                    raise PDFPageEvidenceError(
                        f"{label}:{line_number}: blank JSONL line"
                    )
                value = intermediate.strict_json_loads(raw)
                if not isinstance(value, dict):
                    raise PDFPageEvidenceError(
                        f"{label}:{line_number}: record must be an object"
                    )
                records.append(value)
                if len(records) > MAX_JSONL_RECORDS:
                    raise PDFPageEvidenceError(f"{label} has too many records")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, PDFPageEvidenceError):
            raise
        raise PDFPageEvidenceError(f"{label} is not strict JSONL: {exc}") from exc
    return records, digest_file(resolved)


def _resolve_shard(base: Path, relative_path: object, label: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        raise PDFPageEvidenceError(f"{label}.relative_path must be a non-empty string")
    raw = Path(relative_path)
    if raw.is_absolute() or ".." in raw.parts:
        raise PDFPageEvidenceError(f"{label} path must be a safe relative path")
    current = base
    for component in raw.parts:
        current = current / component
        if current.is_symlink():
            raise PDFPageEvidenceError(f"{label} contains a symlink component")
    resolved = current.resolve(strict=True)
    if not _inside(resolved, base) or not resolved.is_file():
        raise PDFPageEvidenceError(f"{label} escapes the base intermediate")
    return resolved


def _load_shard(
    base: Path,
    metadata: object,
    label: str,
) -> list[dict[str, Any]]:
    if not isinstance(metadata, Mapping):
        raise PDFPageEvidenceError(f"{label} metadata must be an object")
    path = _resolve_shard(base, metadata.get("relative_path"), label)
    records, actual_sha = _strict_jsonl(path, label)
    stat = path.stat()
    expected_count = metadata.get("record_count")
    expected_size = metadata.get("size_bytes")
    expected_sha = metadata.get("sha256")
    if expected_count != len(records):
        raise PDFPageEvidenceError(f"{label} record_count mismatch")
    if expected_size != stat.st_size:
        raise PDFPageEvidenceError(f"{label} size_bytes mismatch")
    if expected_sha != actual_sha:
        raise PDFPageEvidenceError(f"{label} sha256 mismatch")
    return records


def _official_evidence_errors(record: dict[str, Any], label: str) -> list[str]:
    errors = intermediate.schema_record_errors("evidence", record, label)
    errors.extend(intermediate.question_boundary_errors("evidence", record, label))
    if errors:
        return errors
    item_content = record["content"]
    try:
        expected_sha = intermediate.digest_value(
            intermediate.content_hash_payload(item_content)
        )
    except ValueError as exc:
        return [f"{label}: {exc}"]
    if item_content.get("sha256") != expected_sha:
        errors.append(f"{label}: content hash mismatch")
    expected_id = stable_id(
        "ev",
        {
            "document_id": record.get("document_id"),
            "evidence_type": record.get("evidence_type"),
            "location": record.get("location"),
            "content_sha256": item_content.get("sha256"),
        },
    )
    if record.get("evidence_id") != expected_id:
        errors.append(f"{label}: unstable Evidence id")
    if "raw_value" in item_content and item_content.get("normalized_value") != item_content["raw_value"]:
        errors.append(f"{label}: normalized_value is missing or inconsistent")
    return errors


def _validated_observations(path: Path) -> tuple[list[dict[str, Any]], str]:
    _forbid_physical_query_path(path, "PDFPageObservation input")
    try:
        records, input_sha = pdf_observations.load_jsonl(
            _regular_file(path, "PDFPageObservation input"),
            "PDFPageObservation input",
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PDFPageEvidenceError(
            f"cannot load PDFPageObservation input: {exc}"
        ) from exc
    if not records:
        raise PDFPageEvidenceError("PDFPageObservation input is empty")
    errors: list[str] = []
    seen_ids: set[str] = set()
    seen_pages: set[tuple[str, int]] = set()
    seen_sources: set[tuple[str, str, int]] = set()
    pages_by_document: dict[str, set[int]] = defaultdict(set)
    page_counts: dict[str, set[int]] = defaultdict(set)
    source_identity: dict[str, tuple[str, str, int]] = {}
    for index, record in enumerate(records, start=1):
        label = f"observation[{index}]"
        validation = pdf_observations.validate_observation(record)
        errors.extend(f"{label}{item}" for item in validation)
        if validation:
            continue
        observation_id = record["observation_id"]
        document_id = record["document_id"]
        page_number = record["page"]["page_number"]
        page_count = record["page"]["page_count"]
        source = record["source"]
        _forbid_query_source_path(
            source["relative_path"], f"{label} source.relative_path"
        )
        page_key = (document_id, page_number)
        source_key = (source["relative_path"], source["sha256"], page_number)
        if observation_id in seen_ids:
            errors.append(f"{label}: duplicate observation_id {observation_id}")
        if page_key in seen_pages:
            errors.append(f"{label}: duplicate document/page {page_key}")
        if source_key in seen_sources:
            errors.append(f"{label}: duplicate source/page {source_key}")
        seen_ids.add(observation_id)
        seen_pages.add(page_key)
        seen_sources.add(source_key)
        pages_by_document[document_id].add(page_number)
        page_counts[document_id].add(page_count)
        identity = (source["relative_path"], source["sha256"], source["size_bytes"])
        prior = source_identity.setdefault(document_id, identity)
        if prior != identity:
            errors.append(f"{label}: inconsistent source identity for document")
        expected_document_id = stable_id(
            "doc",
            {
                "relative_path": source["relative_path"],
                "source_sha256": source["sha256"],
            },
        )
        if document_id != expected_document_id:
            errors.append(f"{label}: unstable document_id")
    for document_id, counts in sorted(page_counts.items()):
        if len(counts) != 1:
            errors.append(f"{document_id}: inconsistent page_count values")
            continue
        page_count = next(iter(counts))
        expected_pages = set(range(1, page_count + 1))
        if pages_by_document[document_id] != expected_pages:
            errors.append(
                f"{document_id}: observations do not cover pages 1..{page_count}"
            )
    if errors:
        raise PDFPageEvidenceError(
            "invalid PDFPageObservation input:\n- " + "\n- ".join(errors)
        )
    records.sort(
        key=lambda record: (
            _normalized(record["source"]["relative_path"]),
            record["source"]["sha256"],
            record["page"]["page_number"],
            record["observation_id"],
        )
    )
    return records, input_sha


def _load_base_bindings(
    base: Path,
    observations: Sequence[dict[str, Any]],
) -> tuple[
    dict[tuple[str, int], dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, Any],
    str,
    set[str],
]:
    state_path = _regular_file(base / BASE_STATE_FILE, "base build-state")
    state, state_sha = _strict_json(state_path, "base build-state")
    reserved_paths = intermediate.reserved_query_paths(state, "base_state")
    if reserved_paths:
        raise PDFPageEvidenceError(
            "base build-state contains query-layer metadata: "
            + ", ".join(sorted(set(reserved_paths)))
        )
    if state.get("build_status") != "complete":
        raise PDFPageEvidenceError("base intermediate must be complete")
    if state.get("state_version") != "1":
        raise PDFPageEvidenceError("base intermediate state_version must be '1'")
    if not isinstance(state.get("extractor"), str) or not state["extractor"]:
        raise PDFPageEvidenceError("base intermediate extractor is missing")
    if (
        not isinstance(state.get("extractor_version"), str)
        or not state["extractor_version"]
    ):
        raise PDFPageEvidenceError("base intermediate extractor_version is missing")
    if not isinstance(state.get("run_at"), str) or not state["run_at"]:
        raise PDFPageEvidenceError("base intermediate must contain run_at")
    entries = state.get("entries")
    input_paths = state.get("input_paths")
    if not isinstance(entries, dict) or not isinstance(input_paths, list):
        raise PDFPageEvidenceError("base state entries/input_paths are invalid")
    if (
        len(input_paths) != len(set(input_paths))
        or set(input_paths) != set(entries)
    ):
        raise PDFPageEvidenceError("base state input_paths do not exactly bind entries")
    for index, relative_path in enumerate(input_paths, start=1):
        if not isinstance(relative_path, str):
            raise PDFPageEvidenceError(
                f"base state input_paths[{index}] must be a string"
            )
        _forbid_query_source_path(
            relative_path, f"base state input_paths[{index}]"
        )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for observation in observations:
        grouped[observation["source"]["relative_path"]].append(observation)
    base_pages: dict[tuple[str, int], dict[str, Any]] = {}
    base_documents: dict[str, dict[str, Any]] = {}
    seen_base_evidence_ids: set[str] = set()
    for relative_path in sorted(grouped, key=_normalized):
        source_observations = grouped[relative_path]
        first = source_observations[0]
        source = first["source"]
        document_id = first["document_id"]
        entry = entries.get(relative_path)
        if not isinstance(entry, Mapping):
            raise PDFPageEvidenceError(f"missing base entry for {relative_path}")
        expected_entry = {
            "relative_path": relative_path,
            "document_id": document_id,
            "source_sha256": source["sha256"],
        }
        for field, expected in expected_entry.items():
            if entry.get(field) != expected:
                raise PDFPageEvidenceError(
                    f"base entry {relative_path}: {field} mismatch"
                )
        if entry.get("status") not in {"success", "partial"}:
            raise PDFPageEvidenceError(
                f"base entry {relative_path}: status must be success or partial"
            )
        shards = entry.get("shards")
        if not isinstance(shards, Mapping):
            raise PDFPageEvidenceError(f"base entry {relative_path}: shards missing")
        documents = _load_shard(
            base, shards.get("documents"), f"base documents shard {relative_path}"
        )
        evidence = _load_shard(
            base, shards.get("evidence"), f"base evidence shard {relative_path}"
        )
        if len(documents) != 1:
            raise PDFPageEvidenceError(
                f"base documents shard {relative_path} must contain one record"
            )
        document = documents[0]
        document_errors = intermediate.schema_record_errors(
            "document", document, f"base document {relative_path}"
        )
        document_errors.extend(
            intermediate.question_boundary_errors(
                "document", document, f"base document {relative_path}"
            )
        )
        if document_errors:
            raise PDFPageEvidenceError("\n- ".join(document_errors))
        document_source = document["source"]
        expected_document_id = stable_id(
            "doc",
            {
                "relative_path": document_source["relative_path"],
                "source_sha256": document_source["sha256"],
            },
        )
        if document.get("document_id") != expected_document_id:
            raise PDFPageEvidenceError(f"base document {relative_path}: unstable id")
        if (
            document.get("document_id") != document_id
            or document_source.get("relative_path") != relative_path
            or document_source.get("sha256") != source["sha256"]
            or document_source.get("size_bytes") != source["size_bytes"]
        ):
            raise PDFPageEvidenceError(
                f"base document {relative_path}: source binding mismatch"
            )
        if document.get("extraction", {}).get("status") != entry.get("status"):
            raise PDFPageEvidenceError(
                f"base document {relative_path}: extraction status mismatch"
            )
        base_documents[document_id] = document
        page_records: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for index, record in enumerate(evidence, start=1):
            label = f"base evidence {relative_path}[{index}]"
            evidence_errors = _official_evidence_errors(record, label)
            if evidence_errors:
                raise PDFPageEvidenceError("\n- ".join(evidence_errors))
            if record.get("document_id") != document_id:
                raise PDFPageEvidenceError(f"{label}: document_id mismatch")
            evidence_id = record["evidence_id"]
            if evidence_id in seen_base_evidence_ids:
                raise PDFPageEvidenceError(f"{label}: duplicate Evidence id")
            seen_base_evidence_ids.add(evidence_id)
            if record.get("evidence_type") == "page":
                page_number = record.get("location", {}).get("page_number")
                if isinstance(page_number, int) and not isinstance(page_number, bool):
                    page_records[page_number].append(record)
        expected_page_count = first["page"]["page_count"]
        if set(page_records) != set(range(1, expected_page_count + 1)):
            raise PDFPageEvidenceError(
                f"base page Evidence {relative_path} does not cover 1..{expected_page_count}"
            )
        for page_number in range(1, expected_page_count + 1):
            matches = page_records[page_number]
            if len(matches) != 1:
                raise PDFPageEvidenceError(
                    f"base page Evidence {relative_path} page {page_number} is not unique"
                )
            base_pages[(document_id, page_number)] = matches[0]
    return base_pages, base_documents, state, state_sha, seen_base_evidence_ids


def _shadow_content(observation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "extraction": observation["extraction"],
        "native": observation["native"],
        "ocr": observation["ocr"],
        "conflicts": observation["conflicts"],
        "unresolved": observation["unresolved"],
        "status": observation["status"],
        "hashes": observation["hashes"],
    }


def make_evidence(
    observation: Mapping[str, Any],
    base_evidence: Mapping[str, Any],
    *,
    observations_sha256: str,
    base_state_sha256: str,
    run_at: str,
) -> dict[str, Any]:
    raw_value = _shadow_content(observation)
    item_content = content(raw_value=raw_value)
    page = observation["page"]
    location = {
        "page_number": page["page_number"],
        "locator_text": LOCATOR_TEXT,
    }
    evidence_id = stable_id(
        "ev",
        {
            "document_id": observation["document_id"],
            "evidence_type": "other",
            "location": location,
            "content_sha256": item_content["sha256"],
        },
    )
    warnings = []
    if observation["status"] == "needs_review":
        warnings.append(
            "PDF page observation requires review; raw readings and unresolved "
            "values are retained without arbitration"
        )
    hashes = observation["hashes"]
    record = {
        "schema_version": "0.1",
        "record_type": "evidence",
        "evidence_id": evidence_id,
        "document_id": observation["document_id"],
        "evidence_type": "other",
        "location": location,
        "content": item_content,
        "geometry": {
            "coordinate_space": "page",
            "unit": "pt",
            "x": 0,
            "y": 0,
            "width": page["width_pt"],
            "height": page["height_pt"],
            "rotation_deg": page["rotation_degrees"],
        },
        "parent_evidence_id": base_evidence["evidence_id"],
        "ordinal": page["page_number"],
        "native_properties": {
            "pdf_page_observation_id": observation["observation_id"],
            "pdf_page_observations_sha256": observations_sha256,
            "pdf_page_observation_input_sha256": hashes["input_sha256"],
            "pdf_page_observation_content_sha256": hashes["content_sha256"],
            "pdf_page_observation_signature_sha256": hashes["signature_sha256"],
            "pdf_page_observation_record_integrity_sha256": hashes[
                "record_integrity_sha256"
            ],
            "base_intermediate_state_sha256": base_state_sha256,
            "base_page_evidence_id": base_evidence["evidence_id"],
            "base_page_evidence_content_sha256": base_evidence["content"]["sha256"],
            "source_sha256": observation["source"]["sha256"],
            "page_count": page["page_count"],
            "coordinate_system": page["coordinate_system"],
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
        },
        "provenance": {
            "extraction_method": "pdf_page_observation_shadow_adaptation",
            "extractor": ADAPTER,
            "extractor_version": ADAPTER_VERSION,
            "extracted_at": run_at,
            "deterministic": True,
            "confidence": 1.0 if observation["status"] == "observed" else 0.0,
            "warnings": warnings,
        },
    }
    errors = _official_evidence_errors(record, observation["observation_id"])
    if errors:
        raise PDFPageEvidenceError(
            "generated Evidence is invalid:\n- " + "\n- ".join(errors)
        )
    return record


def _write_jsonl(path: Path, records: Sequence[dict[str, Any]]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(canonical_json(record) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _prepare_output_path(path: Path) -> Path:
    output = _resolve_directory(path, "output", must_exist=False)
    pdf_observations._forbid_sensitive_path(output, "output")
    if os.path.lexists(output):
        raise PDFPageEvidenceError(
            f"output already exists; refusing to overwrite bundle: {output}"
        )
    return output


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_noreplace(source: Path, target: Path) -> None:
    """Atomically rename a directory while refusing an existing target."""
    library_name = ctypes.util.find_library("c")
    if not library_name:
        raise PDFPageEvidenceError("cannot locate the C library for atomic publish")
    library = ctypes.CDLL(library_name, use_errno=True)
    if sys.platform == "darwin":
        rename = library.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(os.fsencode(source), os.fsencode(target), 0x00000004)
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        rename = library.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    else:
        raise PDFPageEvidenceError(
            "platform does not provide atomic no-replace directory publication"
        )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(f"refusing to overwrite existing output: {target}")
    raise OSError(error_number, os.strerror(error_number), str(target))


def _publish_bundle(staging: Path, output: Path) -> None:
    """Publish a fully-written directory as the single visible commit point."""
    lock = output.with_name(f".{output.name}.lock")
    try:
        lock_descriptor = os.open(
            lock,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise PDFPageEvidenceError(
            f"another publication or stale lock exists: {lock}"
        ) from exc
    try:
        if os.path.lexists(output):
            raise PDFPageEvidenceError(
                f"output appeared during publication; refusing overwrite: {output}"
            )
        _rename_noreplace(staging, output)
        _fsync_directory(output.parent)
    finally:
        os.close(lock_descriptor)
        lock.unlink(missing_ok=True)


def build(
    *,
    observations_path: Path,
    base_intermediate: Path,
    output: Path,
) -> dict[str, Any]:
    """Build and publish one complete Evidence-only shadow bundle."""
    observations_path = _absolute_path(observations_path, "PDFPageObservation input")
    base = _resolve_directory(base_intermediate, "base intermediate")
    pdf_observations._forbid_sensitive_path(base, "base intermediate")
    observations, observations_sha = _validated_observations(observations_path)
    (
        base_pages,
        base_documents,
        base_state,
        base_state_sha,
        base_ids,
    ) = _load_base_bindings(base, observations)
    records: list[dict[str, Any]] = []
    output_ids: set[str] = set()
    route_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    for observation in observations:
        page_key = (
            observation["document_id"],
            observation["page"]["page_number"],
        )
        base_evidence = base_pages.get(page_key)
        if base_evidence is None:
            raise PDFPageEvidenceError(f"missing base page Evidence: {page_key}")
        record = make_evidence(
            observation,
            base_evidence,
            observations_sha256=observations_sha,
            base_state_sha256=base_state_sha,
            run_at=base_state["run_at"],
        )
        if record["evidence_id"] in output_ids:
            raise PDFPageEvidenceError(
                f"duplicate generated Evidence id: {record['evidence_id']}"
            )
        if record["evidence_id"] in base_ids:
            raise PDFPageEvidenceError(
                f"generated Evidence id collides with base: {record['evidence_id']}"
            )
        output_ids.add(record["evidence_id"])
        records.append(record)
        route_counts[observation["extraction"]["route"]] += 1
        status_counts[observation["status"]] += 1
    output_directory = _prepare_output_path(output)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output_directory.name}.",
            dir=output_directory.parent,
        )
    )
    try:
        evidence_staging = staging / EVIDENCE_FILE
        state_staging = staging / STATE_FILE
        _write_jsonl(evidence_staging, records)
        evidence_sha = digest_file(evidence_staging)
        evidence_size = evidence_staging.stat().st_size
        schema_path = Path(__file__).resolve().parents[1] / "schemas" / "evidence.schema.json"
        state = {
            "state_version": "0.1",
            "build_status": "complete",
            "deterministic": True,
            "adapter": ADAPTER,
            "adapter_version": ADAPTER_VERSION,
            "run_at": base_state["run_at"],
            "inputs": {
                "pdf_page_observations": {
                    "sha256": observations_sha,
                    "record_count": len(observations),
                },
                "base_intermediate": {
                    "build_state_sha256": base_state_sha,
                    "extractor": base_state.get("extractor"),
                    "extractor_version": base_state.get("extractor_version"),
                },
                "evidence_schema": {
                    "schema_version": "0.1",
                    "sha256": digest_file(schema_path),
                },
            },
            "output": {
                "relative_path": EVIDENCE_FILE,
                "sha256": evidence_sha,
                "size_bytes": evidence_size,
                "record_count": len(records),
            },
            "counts": {
                "documents": len(base_documents),
                "pages": len(records),
                "routes": dict(sorted(route_counts.items())),
                "statuses": dict(sorted(status_counts.items())),
                "evidence_types": {"other": len(records)},
            },
            "flags": dict(STATE_FLAGS),
        }
        _write_jsonl(state_staging, [state])
        forbidden = sorted(
            name for name in FORBIDDEN_OUTPUT_NAMES if (staging / name).exists()
        )
        if forbidden:
            raise PDFPageEvidenceError(
                f"forbidden normal intermediate outputs exist: {forbidden}"
            )
        _fsync_directory(staging)
        _publish_bundle(staging, output_directory)
        staging = None
    except BaseException:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        raise
    evidence_final = output_directory / EVIDENCE_FILE
    state_final = output_directory / STATE_FILE
    return {
        "build_status": "complete",
        "adapter": ADAPTER,
        "adapter_version": ADAPTER_VERSION,
        "counts": state["counts"],
        "hashes": {
            "pdf_page_observations_sha256": observations_sha,
            "base_intermediate_state_sha256": base_state_sha,
            "evidence_sha256": evidence_sha,
            "state_sha256": digest_file(state_final),
        },
        "paths": {
            "evidence": str(evidence_final),
            "state": str(state_final),
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--observations",
        "--pdf-page-observations",
        dest="observations",
        required=True,
        type=Path,
    )
    parser.add_argument("--base-intermediate", required=True, type=Path)
    parser.add_argument("--out", "--output", dest="output", required=True, type=Path)
    args = parser.parse_args(argv)
    result = build(
        observations_path=args.observations,
        base_intermediate=args.base_intermediate,
        output=args.output,
    )
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
