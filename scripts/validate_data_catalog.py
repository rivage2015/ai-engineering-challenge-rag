#!/usr/bin/env python3
"""Validate DataCatalogEntry JSONL and its DataCatalogSnapshot.

With ``--documents`` and ``--search-units`` the validator deterministically
rebuilds the catalog and compares every entry, ID, reference, input digest, and
snapshot field.  Without source inputs it still validates the closed schemas,
canonical byte stream, stream digest/count/order, local IDs, provenance, and
question/answer leakage boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_data_catalog import (
    BUILDER_NAME,
    BUILDER_VERSION,
    CatalogContractError,
    Limits,
    _assert_no_catalog_leakage,
    _load_schema,
    _loads_strict,
    _sha256_json,
    _stable_id,
    _validator,
    canonical_json_bytes,
    compile_data_catalog,
    normalize_label,
)


def _regular_bytes(path: Path, *, role: str) -> bytes:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise CatalogContractError(f"cannot stat {role}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise CatalogContractError(f"{role} must be a regular non-symlink file")
    return path.read_bytes()


def _schema_errors(validator: Any, value: Any, prefix: str) -> list[str]:
    errors: list[str] = []
    for error in sorted(
        validator.iter_errors(value),
        key=lambda item: (
            tuple(str(component) for component in item.absolute_path),
            item.message,
        ),
    ):
        location = "/".join(str(item) for item in error.absolute_path) or "root"
        errors.append(f"{prefix}:{location}: {error.message}")
    return errors


def _read_entries(path: Path, limits: Limits) -> tuple[list[dict[str, Any]], bytes]:
    raw = _regular_bytes(path, role="entries")
    if not raw:
        raise CatalogContractError("entries JSONL is empty")
    records: list[dict[str, Any]] = []
    canonical_chunks: list[bytes] = []
    validator = _validator("data-catalog-entry.schema.json")
    for line_number, line in enumerate(raw.splitlines(keepends=True), start=1):
        if not line.endswith(b"\n"):
            raise CatalogContractError("each entry JSONL record must end with LF")
        payload = line[:-1]
        value = _loads_strict(
            payload,
            source=f"entries:{line_number}",
            limits=limits,
        )
        if not isinstance(value, dict):
            raise CatalogContractError("entry JSONL record must be an object")
        schema_errors = _schema_errors(validator, value, f"entries:{line_number}")
        if schema_errors:
            raise CatalogContractError(schema_errors[0])
        canonical = canonical_json_bytes(value) + b"\n"
        if line != canonical:
            raise CatalogContractError(
                f"entries:{line_number}: record is not canonical NFC JSON"
            )
        records.append(value)
        canonical_chunks.append(canonical)
    return records, b"".join(canonical_chunks)


def _read_snapshot(path: Path, limits: Limits) -> dict[str, Any]:
    raw = _regular_bytes(path, role="snapshot")
    if raw.endswith(b"\n"):
        payload = raw[:-1]
    else:
        payload = raw
    if b"\n" in payload or b"\r" in payload:
        raise CatalogContractError("snapshot must contain one canonical JSON object")
    value = _loads_strict(payload, source="snapshot", limits=limits)
    if not isinstance(value, dict):
        raise CatalogContractError("snapshot root must be an object")
    validator = _validator("data-catalog-snapshot.schema.json")
    errors = _schema_errors(validator, value, "snapshot")
    if errors:
        raise CatalogContractError(errors[0])
    if payload != canonical_json_bytes(value):
        raise CatalogContractError("snapshot is not canonical NFC JSON")
    return value


def _snapshot_identity(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": snapshot["schema_version"],
        "entry_schema_version": snapshot["entry_schema_version"],
        "entry_stream": {
            key: value
            for key, value in snapshot["entry_stream"].items()
            if key != "relative_path"
        },
        "inputs": snapshot["inputs"],
        "build_config_sha256": snapshot["build_config_sha256"],
        "builder": snapshot["provenance"]["builder"],
        "builder_version": snapshot["provenance"]["builder_version"],
    }


def _local_semantic_errors(
    entries: list[dict[str, Any]],
    entry_bytes: bytes,
    snapshot: dict[str, Any],
    entries_path: Path,
    snapshot_path: Path,
) -> list[str]:
    errors: list[str] = []
    ids = [entry["data_catalog_entry_id"] for entry in entries]
    if ids != sorted(ids):
        errors.append("entries are not sorted by data_catalog_entry_id")
    if len(ids) != len(set(ids)):
        errors.append("entry IDs are duplicated")
    label_ids: set[str] = set()
    field_ids: set[str] = set()
    entry_id_set = set(ids)
    generated_at = snapshot["provenance"]["generated_at"]
    for index, entry in enumerate(entries):
        try:
            _assert_no_catalog_leakage(entry)
        except CatalogContractError as exc:
            errors.append(f"entry[{index}] leakage: {exc}")
        if entry["provenance"]["builder"] != BUILDER_NAME or entry["provenance"]["builder_version"] != BUILDER_VERSION:
            errors.append(f"entry[{index}] builder metadata mismatch")
        if entry["provenance"]["generated_at"] != generated_at:
            errors.append(f"entry[{index}] generated_at differs from snapshot")
        parent = entry["address"]["parent_entry_ref"]
        if parent is not None and parent not in entry_id_set:
            errors.append(f"entry[{index}] parent reference is missing")
        for label in entry["scope_labels"]:
            expected = _stable_id(
                "dcl",
                {
                    "data_catalog_entry_id": entry["data_catalog_entry_id"],
                    **{key: value for key, value in label.items() if key != "label_id"},
                },
            )
            if label["label_id"] != expected:
                errors.append(f"entry[{index}] label ID is inconsistent")
            if label["label_id"] in label_ids:
                errors.append("catalog label IDs are duplicated")
            label_ids.add(label["label_id"])
            if label["normalized"] != normalize_label(label["surface"]):
                errors.append(f"entry[{index}] label normalization is inconsistent")
        for field in entry["fields"]:
            expected = _stable_id(
                "dcf",
                {
                    "data_catalog_entry_id": entry["data_catalog_entry_id"],
                    **{key: value for key, value in field.items() if key != "field_id"},
                },
            )
            if field["field_id"] != expected:
                errors.append(f"entry[{index}] field ID is inconsistent")
            if field["field_id"] in field_ids:
                errors.append("catalog field IDs are duplicated")
            field_ids.add(field["field_id"])
            if field["normalized"] != normalize_label(field["surface"]):
                errors.append(f"entry[{index}] field normalization is inconsistent")

    stream = snapshot["entry_stream"]
    if stream["record_count"] != len(entries):
        errors.append("snapshot entry record_count is inconsistent")
    digest = hashlib.sha256(entry_bytes).hexdigest()
    if stream["sha256"] != digest:
        errors.append("snapshot entry stream SHA-256 is inconsistent")
    declared_entries = (snapshot_path.parent / stream["relative_path"]).resolve(strict=False)
    if declared_entries != entries_path.resolve(strict=False):
        errors.append("snapshot entry_stream.relative_path does not locate the entry file")
    input_types = [item["record_type"] for item in snapshot["inputs"]]
    if input_types != sorted(input_types) or len(input_types) != len(set(input_types)):
        errors.append("snapshot inputs are not unique and sorted by record_type")
    expected_snapshot_id = _stable_id("dcs", _snapshot_identity(snapshot))
    if snapshot["data_catalog_snapshot_id"] != expected_snapshot_id:
        errors.append("snapshot ID is inconsistent")
    try:
        _assert_no_catalog_leakage(snapshot)
    except CatalogContractError as exc:
        errors.append(f"snapshot leakage: {exc}")
    return sorted(set(errors))


def validate_data_catalog(
    entries_path: str | Path,
    snapshot_path: str | Path,
    *,
    documents_path: str | Path | None = None,
    search_units_path: str | Path | None = None,
    evidence_path: str | Path | None = None,
    limits: Limits = Limits(),
) -> list[str]:
    """Return deterministic catalog validation errors."""

    try:
        entries_file = Path(entries_path)
        snapshot_file = Path(snapshot_path)
        entries, entry_bytes = _read_entries(entries_file, limits)
        snapshot = _read_snapshot(snapshot_file, limits)
        errors = _local_semantic_errors(
            entries, entry_bytes, snapshot, entries_file, snapshot_file
        )
        source_flags = (documents_path is not None, search_units_path is not None)
        if source_flags[0] != source_flags[1]:
            errors.append("documents and search_units must be supplied together")
        elif all(source_flags):
            regenerated = compile_data_catalog(
                documents_path,
                search_units_path,
                entries_file,
                snapshot_file,
                evidence_path=evidence_path,
                generated_at=snapshot["provenance"]["generated_at"],
                limits=limits,
            )
            if list(regenerated.entries) != entries:
                errors.append("entries differ from deterministic source rebuild")
            if regenerated.snapshot != snapshot:
                errors.append("snapshot differs from deterministic source rebuild")
        elif evidence_path is not None:
            errors.append("evidence cannot be supplied without source inputs")
        return sorted(set(errors))
    except (CatalogContractError, OSError, UnicodeDecodeError, ValueError) as exc:
        return [str(exc)]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entries", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--documents", type=Path)
    parser.add_argument("--search-units", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--max-record-bytes", type=int, default=Limits().max_record_bytes)
    parser.add_argument("--max-depth", type=int, default=Limits().max_depth)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    errors = validate_data_catalog(
        args.entries,
        args.snapshot,
        documents_path=args.documents,
        search_units_path=args.search_units,
        evidence_path=args.evidence,
        limits=Limits(args.max_record_bytes, args.max_depth),
    )
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok"}, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
