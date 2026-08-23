#!/usr/bin/env python3
"""Build a closed, question-independent table observation from Docling output.

This adapter does not run OCR and does not correct recognized text.  It binds a
validated Docling structural run back to the original visual asset, verifies
the source DOCX member and materialized image bytes, and emits raw cells plus a
deterministic coverage decision.  The output remains a shadow artifact: it is
not Evidence and is not connected to SearchUnit or the production index.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import math
import os
import re
import sys
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator, FormatChecker
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_docling_poc as docling_contract  # noqa: E402
import build_visual_asset_manifest as visual_manifest_builder  # noqa: E402
import validate_visual_asset_manifest as visual_manifest_validator  # noqa: E402


SCHEMA_PATH = ROOT / "schemas" / "visual-table-observation.schema.json"
RUNNER_VERSION = "0.1"
MAX_JSONL_BYTES = 128 * 1024 * 1024
MAX_JSONL_RECORDS = 100_000
MAX_ZIP_ENTRIES = 10_000
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_IMAGE_BYTES = 128 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 256 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200.0
MAX_GRID_SLOTS = 1_000_000
MAX_TABLE_CELLS = 100_000
FORBIDDEN_DATA_RE = re.compile(
    r"(?:(?:^|[-_.])(questions?|gold|predictions?|answers?)(?:[-_.]|$)|質問|正解|予測|回答)",
    re.IGNORECASE,
)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"duplicate JSON key: {key}")
        output[key] = value
    return output


def _reject_nonfinite_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _forbid_sensitive_path(path: Path, label: str) -> None:
    for component in path.parts:
        if FORBIDDEN_DATA_RE.search(component):
            raise ValueError(f"{label} contains a forbidden data component: {component}")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _has_symlink_component(path: Path, trusted_root: Path) -> bool:
    current = trusted_root
    for component in path.relative_to(trusted_root).parts:
        current = current / component
        if current.is_symlink():
            return True
    return False


def resolve_trusted_root(raw: Path, label: str) -> Path:
    candidate = raw if raw.is_absolute() else Path.cwd() / raw
    candidate = Path(os.path.abspath(candidate))
    current = Path(candidate.anchor)
    for component in candidate.parts[1:]:
        current = current / component
        if current.is_symlink():
            raise ValueError(f"{label} contains a symlink component")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(f"{label} is not a directory")
    return resolved


def resolve_repo_input(root: Path, raw: Path, label: str) -> Path:
    if ".." in raw.parts:
        raise ValueError(f"{label} contains a parent traversal")
    candidate = raw if raw.is_absolute() else root / raw
    candidate = Path(os.path.abspath(candidate))
    trusted_root = root.resolve(strict=True)
    if not _inside(candidate, trusted_root):
        raise ValueError(f"{label} must be inside repository root")
    if _has_symlink_component(candidate, trusted_root):
        raise ValueError(f"{label} contains a symlink component")
    _forbid_sensitive_path(candidate.relative_to(trusted_root), label)
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"{label} is not a regular file")
    return resolved


def resolve_repo_output(root: Path, raw: Path) -> Path:
    if ".." in raw.parts:
        raise ValueError("output contains a parent traversal")
    candidate = raw if raw.is_absolute() else root / raw
    candidate = Path(os.path.abspath(candidate))
    trusted_root = root.resolve(strict=True)
    if not _inside(candidate, trusted_root):
        raise ValueError("output must be inside repository root")
    current = trusted_root
    for component in candidate.relative_to(trusted_root).parts[:-1]:
        current = current / component
        if current.exists() and current.is_symlink():
            raise ValueError("output parent contains a symlink component")
    if candidate.is_symlink():
        raise ValueError("output must not be a symlink")
    return candidate


def resolve_regular_file(root: Path, relative: str, label: str) -> Path:
    raw = Path(relative)
    if raw.is_absolute() or ".." in raw.parts:
        raise ValueError(f"{label} must be a safe relative path")
    _forbid_sensitive_path(raw, label)
    trusted_root = root.resolve(strict=True)
    candidate = trusted_root.joinpath(*raw.parts)
    if not candidate.exists():
        # macOS usually resolves NFC/NFD spellings transparently.  This narrow
        # fallback keeps the record portable without searching outside root.
        normalized = Path(*(unicodedata.normalize("NFD", part) for part in raw.parts))
        candidate = trusted_root.joinpath(*normalized.parts)
    resolved = candidate.resolve(strict=True)
    if not _inside(resolved, trusted_root):
        raise ValueError(f"{label} escapes its trusted root")
    if _has_symlink_component(candidate, trusted_root):
        raise ValueError(f"{label} contains a symlink component")
    if not resolved.is_file():
        raise ValueError(f"{label} is not a regular file")
    return resolved


def load_jsonl(path: Path, *, label: str) -> list[dict[str, Any]]:
    _forbid_sensitive_path(path, label)
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink")
    size = path.stat().st_size
    if size <= 0 or size > MAX_JSONL_BYTES:
        raise ValueError(f"{label} size is outside the accepted range")
    output: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                raise ValueError(f"{label}:{line_number}: blank JSONL line")
            value = json.loads(
                raw,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite_constant,
            )
            if not isinstance(value, dict):
                raise ValueError(f"{label}:{line_number}: record must be an object")
            output.append(value)
            if len(output) > MAX_JSONL_RECORDS:
                raise ValueError(f"{label} has too many records")
    return output


def _schema_validator(path: Path) -> Draft202012Validator:
    schema = json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
    )
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _validate_schema(record: Mapping[str, Any], path: Path, label: str) -> None:
    errors = sorted(_schema_validator(path).iter_errors(record), key=lambda e: list(e.path))
    if errors:
        details = []
        for error in errors[:20]:
            location = "/" + "/".join(str(value) for value in error.path)
            details.append(f"{location}: {error.message}")
        raise ValueError(f"{label} failed schema validation: {'; '.join(details)}")


def _single(values: Sequence[dict[str, Any]], predicate: Any, label: str) -> dict[str, Any]:
    matches = [value for value in values if predicate(value)]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {label}, found {len(matches)}")
    return matches[0]


def select_visual_asset(
    records: Sequence[dict[str, Any]], asset_id: str, manifest_path: Path
) -> dict[str, Any]:
    indexed = [
        (index, value)
        for index, value in enumerate(records, start=1)
        if value.get("asset_id") == asset_id
    ]
    if len(indexed) != 1:
        raise ValueError(f"expected exactly one visual asset, found {len(indexed)}")
    index, record = indexed[0]
    _validate_schema(record, ROOT / "schemas" / "visual-asset.schema.json", "visual asset")
    expected_limits = visual_manifest_builder.office_zip_limits(
        max_archive_entries=visual_manifest_builder.DEFAULT_MAX_OFFICE_ARCHIVE_ENTRIES,
        max_member_uncompressed_bytes=visual_manifest_builder.DEFAULT_MAX_OFFICE_MEMBER_BYTES,
        max_total_uncompressed_bytes=visual_manifest_builder.DEFAULT_MAX_OFFICE_TOTAL_BYTES,
        max_compression_ratio=visual_manifest_builder.DEFAULT_MAX_OFFICE_COMPRESSION_RATIO,
    )
    visual_manifest_validator.validate_record(
        record, index, manifest_path, expected_limits
    )
    return record


def select_docling_run(
    records: Sequence[dict[str, Any]], *, sample_id: str | None, image_sha256: str
) -> dict[str, Any]:
    def matches(value: Mapping[str, Any]) -> bool:
        record_sample = value.get("input", {}).get("sample_id")
        record_image = value.get("input", {}).get("image_sha256")
        return record_image == image_sha256 and (sample_id is None or record_sample == sample_id)

    record = _single(records, matches, "Docling run")
    problems = docling_contract.validate_record(record)
    if problems:
        raise ValueError("Docling run is invalid: " + "; ".join(problems))
    if record["status"] != "completed" or record["errors"]:
        raise ValueError("Docling run must be completed without errors")
    provenance = record["provenance"]
    for key in ("question_data_used", "gold_data_used", "prediction_data_used", "answer_data_used"):
        if provenance.get(key) is not False:
            raise ValueError(f"Docling provenance must set {key}=false")
    return record


def verify_source_and_member(asset: Mapping[str, Any], source_root: Path) -> None:
    source_root = resolve_trusted_root(source_root, "source root")
    source = asset["source"]
    source_path = resolve_regular_file(source_root, source["relative_path"], "source path")
    if source_path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("source file exceeds the accepted size")
    source_bytes = source_path.read_bytes()
    if sha256_bytes(source_bytes) != source["sha256"]:
        raise ValueError("source SHA-256 mismatch")
    origin = asset["origin"]
    if origin["kind"] != "office_embedded_image":
        raise ValueError("this PoC accepts only Office embedded images")
    member_path = PurePosixPath(origin["member_path"])
    if member_path.is_absolute() or ".." in member_path.parts:
        raise ValueError("unsafe Office member path")
    _forbid_sensitive_path(Path(*member_path.parts), "Office member path")
    with zipfile.ZipFile(io.BytesIO(source_bytes)) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ZIP_ENTRIES:
            raise ValueError("Office archive has too many members")
        total_size = sum(info.file_size for info in entries)
        if total_size > MAX_ZIP_TOTAL_BYTES:
            raise ValueError("Office archive expands beyond the accepted total size")
        for entry in entries:
            if entry.file_size and entry.compress_size == 0:
                raise ValueError("Office archive has an invalid compression ratio")
            if entry.compress_size and entry.file_size / entry.compress_size > MAX_COMPRESSION_RATIO:
                raise ValueError("Office archive exceeds the accepted compression ratio")
        matches = [info for info in entries if info.filename == member_path.as_posix()]
        if len(matches) != 1:
            raise ValueError("Office member is missing or duplicated")
        info = matches[0]
        if info.is_dir() or info.file_size > MAX_MEMBER_BYTES:
            raise ValueError("Office member is not a bounded regular payload")
        if info.file_size != origin["member_size_bytes"]:
            raise ValueError("Office member size mismatch")
        member_bytes = archive.read(info)
    if sha256_bytes(member_bytes) != origin["member_sha256"]:
        raise ValueError("Office member SHA-256 mismatch")


def verify_image_and_lineage(
    asset: Mapping[str, Any], run: Mapping[str, Any], repository_root: Path
) -> Path:
    source = asset["source"]
    materialization = asset["materialization"]
    run_input = run["input"]
    if run_input["source_relative_path"] != source["relative_path"]:
        raise ValueError("Docling source path does not match visual asset")
    if run_input["source_sha256"] != source["sha256"]:
        raise ValueError("Docling source SHA-256 does not match visual asset")
    if run_input["origin_kind"] != asset["origin"]["kind"]:
        raise ValueError("Docling origin kind does not match visual asset")
    if run_input["page_number"] != asset["origin"]["page_number"]:
        raise ValueError("Docling source page does not match visual asset")
    if run_input["image_sha256"] != materialization["sha256"]:
        raise ValueError("Docling image SHA-256 does not match materialization")
    dimensions = {
        "width_px": materialization["width_px"],
        "height_px": materialization["height_px"],
    }
    if run_input["dimensions"] != dimensions:
        raise ValueError("Docling dimensions do not match materialization")
    image_path = resolve_regular_file(
        repository_root, run_input["materialized_path"], "materialized image"
    )
    if image_path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError("materialized image exceeds the accepted size")
    image_bytes = image_path.read_bytes()
    if sha256_bytes(image_bytes) != run_input["image_sha256"]:
        raise ValueError("materialized image SHA-256 mismatch")
    if run["hashes"]["input_sha256"] != run_input["image_sha256"]:
        raise ValueError("Docling input hash does not match its input image")
    with Image.open(io.BytesIO(image_bytes)) as image:
        image.load()
        actual_dimensions = {"width_px": image.width, "height_px": image.height}
    if actual_dimensions != dimensions:
        raise ValueError("materialized image dimensions mismatch")
    return image_path


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    output = float(value)
    if not math.isfinite(output):
        raise ValueError(f"{label} must be finite")
    return output


def _strict_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _raw_bbox(value: Mapping[str, Any], *, width: int, height: int, label: str) -> dict[str, Any]:
    required = ("l", "t", "r", "b", "coord_origin")
    if set(value) != set(required):
        raise ValueError(f"{label} bbox fields are not closed")
    left = _finite_number(value["l"], f"{label}.l")
    top = _finite_number(value["t"], f"{label}.t")
    right = _finite_number(value["r"], f"{label}.r")
    bottom = _finite_number(value["b"], f"{label}.b")
    origin = value["coord_origin"]
    if origin != "TOPLEFT":
        raise ValueError(f"{label} cell bbox must retain TOPLEFT origin")
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise ValueError(f"{label} bbox is outside the image or inverted")
    return {"l": left, "t": top, "r": right, "b": bottom, "coord_origin": origin}


def normalize_table(
    table: Mapping[str, Any], *, observation_seed: str, dimensions: Mapping[str, int]
) -> tuple[dict[str, Any], list[str]]:
    rows = _strict_int(table.get("rows", 0), "table rows")
    columns = _strict_int(table.get("columns", 0), "table columns")
    if rows <= 0 or columns <= 0:
        raise ValueError("table rows and columns must be positive")
    if rows * columns > MAX_GRID_SLOTS:
        raise ValueError("table grid exceeds the accepted resource limit")
    raw_cells = table.get("cells", [])
    if not isinstance(raw_cells, list) or len(raw_cells) > MAX_TABLE_CELLS:
        raise ValueError("table cell count exceeds the accepted resource limit")
    if not raw_cells:
        raise ValueError("table has no cells")
    width = dimensions["width_px"]
    height = dimensions["height_px"]
    coverage: dict[tuple[int, int], list[str]] = {
        (row, column): [] for row in range(rows) for column in range(columns)
    }
    normalized_cells: list[dict[str, Any]] = []
    coordinate_keys: set[tuple[int, int, int, int]] = set()
    for index, raw in enumerate(raw_cells):
        row_start = _strict_int(raw["row_start"], f"cell {index}.row_start")
        row_end = _strict_int(raw["row_end"], f"cell {index}.row_end")
        column_start = _strict_int(
            raw["column_start"], f"cell {index}.column_start"
        )
        column_end = _strict_int(raw["column_end"], f"cell {index}.column_end")
        row_span = _strict_int(raw["row_span"], f"cell {index}.row_span")
        column_span = _strict_int(
            raw["column_span"], f"cell {index}.column_span"
        )
        if not (0 <= row_start < row_end <= rows):
            raise ValueError(f"cell {index} row offsets are invalid")
        if not (0 <= column_start < column_end <= columns):
            raise ValueError(f"cell {index} column offsets are invalid")
        if row_span != row_end - row_start or column_span != column_end - column_start:
            raise ValueError(f"cell {index} span does not match offsets")
        coordinate_key = (row_start, row_end, column_start, column_end)
        if coordinate_key in coordinate_keys:
            raise ValueError(f"cell {index} duplicates a logical cell")
        coordinate_keys.add(coordinate_key)
        raw_text = raw["text"]
        if not isinstance(raw_text, str):
            raise ValueError(f"cell {index} raw text must be a string")
        bbox = _raw_bbox(raw["bbox"], width=width, height=height, label=f"cell {index}")
        cell_signature = sha256_json(
            {
                "seed": observation_seed,
                "row_start": row_start,
                "row_end": row_end,
                "column_start": column_start,
                "column_end": column_end,
                "raw_text": raw_text,
                "bbox": bbox,
            }
        )
        cell_id = "vtcell_" + cell_signature[:24]
        for row in range(row_start, row_end):
            for column in range(column_start, column_end):
                coverage[(row, column)].append(cell_id)
        normalized_cells.append(
            {
                "cell_id": cell_id,
                "row_start": row_start,
                "row_end": row_end,
                "column_start": column_start,
                "column_end": column_end,
                "row_span": row_span,
                "column_span": column_span,
                "roles": {
                    "column_header": bool(raw["column_header"]),
                    "row_header": bool(raw["row_header"]),
                    "row_section": bool(raw["row_section"]),
                },
                "raw_text": raw_text,
                "text_status": "observed",
                "bbox": bbox,
            }
        )
    normalized_cells.sort(
        key=lambda cell: (
            cell["row_start"],
            cell["column_start"],
            cell["row_end"],
            cell["column_end"],
            cell["cell_id"],
        )
    )
    missing = [
        {"row": row, "column": column}
        for (row, column), ids in sorted(coverage.items())
        if not ids
    ]
    overlapping = [
        {"row": row, "column": column, "cell_ids": sorted(ids)}
        for (row, column), ids in sorted(coverage.items())
        if len(ids) > 1
    ]
    observed_slots = sum(1 for ids in coverage.values() if len(ids) == 1)
    nonempty_cells = sum(1 for cell in normalized_cells if cell["raw_text"].strip())
    bbox_cells = len(normalized_cells)
    reasons: list[str] = []
    if missing:
        reasons.append("missing_grid_slots")
    if overlapping:
        reasons.append("overlapping_grid_slots")
    if nonempty_cells != len(normalized_cells):
        reasons.append("blank_cell_text")
    structure_status = "pass" if not reasons else "hold"
    coverage_summary = {
        "expected_slots": rows * columns,
        "observed_slots": observed_slots,
        "coverage_ratio": observed_slots / (rows * columns),
        "cell_count": len(normalized_cells),
        "nonempty_cell_count": nonempty_cells,
        "bbox_cell_count": bbox_cells,
        "missing_slots": missing,
        "overlapping_slots": overlapping,
    }
    bbox = {
        "l": min(cell["bbox"]["l"] for cell in normalized_cells),
        "t": min(cell["bbox"]["t"] for cell in normalized_cells),
        "r": max(cell["bbox"]["r"] for cell in normalized_cells),
        "b": max(cell["bbox"]["b"] for cell in normalized_cells),
        "coord_origin": "TOPLEFT",
        "derivation": "cell_bbox_union",
    }
    normalized = {
        "table_id": "vtable_" + sha256_json(
            {"seed": observation_seed, "table_index": table["table_index"]}
        )[:24],
        "table_index": _strict_int(table["table_index"], "table index"),
        "upstream_self_ref": table["self_ref"],
        "label": table["label"],
        "rows": rows,
        "columns": columns,
        "bbox": bbox,
        "raw_provenance": copy.deepcopy(table.get("provenance", [])),
        "structure_status": structure_status,
        "coverage": coverage_summary,
        "cells": normalized_cells,
    }
    return normalized, reasons


def _signature_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": record["schema_version"],
        "record_type": record["record_type"],
        "runner_version": record["runner_version"],
        "source": record["source"],
        "upstream": record["upstream"],
        "status": record["status"],
        "reasons": record["reasons"],
        "table_sha256": record["hashes"]["table_sha256"],
        "input_bundle_sha256": record["hashes"]["input_bundle_sha256"],
        "provenance": record["provenance"],
    }


def record_integrity_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(dict(record))
    payload["hashes"].pop("record_integrity_sha256", None)
    return payload


def build_observation(
    *, asset: Mapping[str, Any], run: Mapping[str, Any]
) -> dict[str, Any]:
    pages = run["document"]["pages"]
    if len(pages) != 1:
        raise ValueError(f"expected exactly one engine-local page, found {len(pages)}")
    page = pages[0]
    expected_dimensions = run["input"]["dimensions"]
    if page["page_number"] != 1:
        raise ValueError("image input must use engine-local page number 1")
    if page["width"] != expected_dimensions["width_px"] or page["height"] != expected_dimensions["height_px"]:
        raise ValueError("engine-local page dimensions do not match input image")
    tables = run["document"]["tables"]
    if len(tables) != 1:
        raise ValueError(f"expected exactly one table, found {len(tables)}")
    for item in tables[0].get("provenance", []):
        if item.get("page_number") != page["page_number"]:
            raise ValueError("table provenance references a different engine-local page")
    materialization = asset["materialization"]
    input_bundle = {
        "visual_asset_record_sha256": sha256_json(asset),
        "docling_record_integrity_sha256": run["hashes"]["record_integrity_sha256"],
        "source_sha256": asset["source"]["sha256"],
        "member_sha256": asset["origin"]["member_sha256"],
        "image_sha256": materialization["sha256"],
    }
    input_bundle_sha256 = sha256_json(input_bundle)
    table, structure_reasons = normalize_table(
        tables[0],
        observation_seed=input_bundle_sha256,
        dimensions={
            "width_px": materialization["width_px"],
            "height_px": materialization["height_px"],
        },
    )
    reasons = list(structure_reasons)
    reasons.append("cell_text_not_fully_verified")
    status = "hold"
    configuration = run["configuration"]
    record: dict[str, Any] = {
        "schema_version": "0.1",
        "record_type": "visual_table_observation",
        "observation_id": "vtobs_" + "0" * 24,
        "runner_version": RUNNER_VERSION,
        "source": {
            "asset_id": asset["asset_id"],
            "document_id": asset["document_id"],
            "file_id": asset["file_id"],
            "source_relative_path": asset["source"]["relative_path"],
            "source_sha256": asset["source"]["sha256"],
            "origin_kind": asset["origin"]["kind"],
            "member_path": asset["origin"]["member_path"],
            "member_sha256": asset["origin"]["member_sha256"],
            "source_page_number": asset["origin"]["page_number"],
            "materialized_path": run["input"]["materialized_path"],
            "image_sha256": materialization["sha256"],
            "dimensions": {
                "width_px": materialization["width_px"],
                "height_px": materialization["height_px"],
            },
        },
        "upstream": {
            "run_id": run["run_id"],
            "schema_version": run["schema_version"],
            "sample_id": run["input"]["sample_id"],
            "engine_page_number": run["document"]["pages"][0]["page_number"],
            "ocr_engine": configuration["pipeline"]["ocr"]["engine"],
            "ocr_engine_fingerprint_sha256": configuration["ocr_engine_fingerprint"][
                "fingerprint_sha256"
            ],
            "package_fingerprint_sha256": configuration[
                "package_fingerprint_sha256"
            ],
            "layout_model_sha256": configuration["models"]["layout"]["sha256"],
            "table_model_sha256": configuration["models"]["tableformer"]["sha256"],
            "output_sha256": run["hashes"]["output_sha256"],
            "record_integrity_sha256": run["hashes"]["record_integrity_sha256"],
        },
        "status": status,
        "reasons": sorted(set(reasons)),
        "table": table,
        "hashes": {
            "visual_asset_record_sha256": input_bundle[
                "visual_asset_record_sha256"
            ],
            "input_bundle_sha256": input_bundle_sha256,
            "table_sha256": sha256_json(table),
            "signature_sha256": "0" * 64,
            "record_integrity_sha256": "0" * 64,
        },
        "provenance": {
            "selection_method": "asset-id-and-validated-docling-image-sha-v0.1",
            "question_independent": True,
            "question_data_used": False,
            "gold_data_used": False,
            "prediction_data_used": False,
            "answer_data_used": False,
            "source_data_used": True,
            "raw_text_modified": False,
            "structure_connected": False,
            "evidence_connected": False,
            "search_unit_connected": False,
            "production_index_connected": False,
        },
    }
    signature = sha256_json(_signature_payload(record))
    record["hashes"]["signature_sha256"] = signature
    record["observation_id"] = "vtobs_" + signature[:24]
    record["hashes"]["record_integrity_sha256"] = sha256_json(
        record_integrity_payload(record)
    )
    return record


def _semantic_observation_errors(record: Mapping[str, Any]) -> list[str]:
    """Recompute the v0.1 structural contract without trusting stored summaries."""
    errors: list[str] = []
    source = record["source"]
    upstream = record["upstream"]
    table = record["table"]
    width = source["dimensions"]["width_px"]
    height = source["dimensions"]["height_px"]

    if source["origin_kind"] != "office_embedded_image":
        errors.append("/source/origin_kind: v0.1 accepts only Office embedded images")
    if source["source_page_number"] is not None:
        errors.append("/source/source_page_number: Office image source page must be null")
    if upstream["engine_page_number"] != 1:
        errors.append("/upstream/engine_page_number: image-local page must be 1")

    expected_input_bundle_sha256 = sha256_json(
        {
            "visual_asset_record_sha256": record["hashes"][
                "visual_asset_record_sha256"
            ],
            "docling_record_integrity_sha256": upstream[
                "record_integrity_sha256"
            ],
            "source_sha256": source["source_sha256"],
            "member_sha256": source["member_sha256"],
            "image_sha256": source["image_sha256"],
        }
    )
    if record["hashes"]["input_bundle_sha256"] != expected_input_bundle_sha256:
        errors.append("/hashes/input_bundle_sha256: does not match input lineage")

    provenance = table["raw_provenance"]
    if not provenance:
        errors.append("/table/raw_provenance: at least one upstream item is required")
    for index, item in enumerate(provenance):
        bbox = item["bbox"]
        if item["page_number"] != upstream["engine_page_number"]:
            errors.append(
                f"/table/raw_provenance/{index}/page_number: does not match engine page"
            )
        if bbox["coord_origin"] != "BOTTOMLEFT":
            errors.append(
                f"/table/raw_provenance/{index}/bbox/coord_origin: must remain BOTTOMLEFT"
            )
        if not (
            0 <= bbox["l"] < bbox["r"] <= width
            and 0 <= bbox["b"] < bbox["t"] <= height
        ):
            errors.append(
                f"/table/raw_provenance/{index}/bbox: outside image or inverted"
            )
        if item["charspan"][0] > item["charspan"][1]:
            errors.append(
                f"/table/raw_provenance/{index}/charspan: start exceeds end"
            )

    rows = table["rows"]
    columns = table["columns"]
    slots: dict[tuple[int, int], list[str]] = {
        (row, column): [] for row in range(rows) for column in range(columns)
    }
    logical_cells: set[tuple[int, int, int, int]] = set()
    cell_ids: set[str] = set()
    structure_reasons: list[str] = []
    for index, cell in enumerate(table["cells"]):
        row_start = cell["row_start"]
        row_end = cell["row_end"]
        column_start = cell["column_start"]
        column_end = cell["column_end"]
        if not (0 <= row_start < row_end <= rows):
            errors.append(f"/table/cells/{index}: row offsets are invalid")
            continue
        if not (0 <= column_start < column_end <= columns):
            errors.append(f"/table/cells/{index}: column offsets are invalid")
            continue
        if cell["row_span"] != row_end - row_start:
            errors.append(f"/table/cells/{index}/row_span: does not match offsets")
        if cell["column_span"] != column_end - column_start:
            errors.append(f"/table/cells/{index}/column_span: does not match offsets")
        logical_key = (row_start, row_end, column_start, column_end)
        if logical_key in logical_cells:
            errors.append(f"/table/cells/{index}: duplicate logical cell")
        logical_cells.add(logical_key)
        if cell["cell_id"] in cell_ids:
            errors.append(f"/table/cells/{index}/cell_id: duplicate identifier")
        cell_ids.add(cell["cell_id"])
        bbox = cell["bbox"]
        if bbox["coord_origin"] != "TOPLEFT" or not (
            0 <= bbox["l"] < bbox["r"] <= width
            and 0 <= bbox["t"] < bbox["b"] <= height
        ):
            errors.append(f"/table/cells/{index}/bbox: outside image or inverted")
        expected_cell_id = "vtcell_" + sha256_json(
            {
                "seed": record["hashes"]["input_bundle_sha256"],
                "row_start": row_start,
                "row_end": row_end,
                "column_start": column_start,
                "column_end": column_end,
                "raw_text": cell["raw_text"],
                "bbox": bbox,
            }
        )[:24]
        if cell["cell_id"] != expected_cell_id:
            errors.append(f"/table/cells/{index}/cell_id: does not match raw cell")
        for row in range(row_start, row_end):
            for column in range(column_start, column_end):
                slots[(row, column)].append(cell["cell_id"])

    missing = [
        {"row": row, "column": column}
        for (row, column), ids in sorted(slots.items())
        if not ids
    ]
    overlapping = [
        {"row": row, "column": column, "cell_ids": sorted(ids)}
        for (row, column), ids in sorted(slots.items())
        if len(ids) > 1
    ]
    if missing:
        structure_reasons.append("missing_grid_slots")
    if overlapping:
        structure_reasons.append("overlapping_grid_slots")
    nonempty_cells = sum(1 for cell in table["cells"] if cell["raw_text"].strip())
    if nonempty_cells != len(table["cells"]):
        structure_reasons.append("blank_cell_text")
    expected_coverage = {
        "expected_slots": rows * columns,
        "observed_slots": sum(1 for ids in slots.values() if len(ids) == 1),
        "coverage_ratio": sum(1 for ids in slots.values() if len(ids) == 1)
        / (rows * columns),
        "cell_count": len(table["cells"]),
        "nonempty_cell_count": nonempty_cells,
        "bbox_cell_count": len(table["cells"]),
        "missing_slots": missing,
        "overlapping_slots": overlapping,
    }
    if table["coverage"] != expected_coverage:
        errors.append("/table/coverage: does not match recomputed cell coverage")
    expected_structure_status = "pass" if not structure_reasons else "hold"
    if table["structure_status"] != expected_structure_status:
        errors.append("/table/structure_status: does not match recomputed structure")

    if table["cells"]:
        expected_bbox = {
            "l": min(cell["bbox"]["l"] for cell in table["cells"]),
            "t": min(cell["bbox"]["t"] for cell in table["cells"]),
            "r": max(cell["bbox"]["r"] for cell in table["cells"]),
            "b": max(cell["bbox"]["b"] for cell in table["cells"]),
            "coord_origin": "TOPLEFT",
            "derivation": "cell_bbox_union",
        }
        if table["bbox"] != expected_bbox:
            errors.append("/table/bbox: does not match cell bbox union")

    expected_table_id = "vtable_" + sha256_json(
        {
            "seed": record["hashes"]["input_bundle_sha256"],
            "table_index": table["table_index"],
        }
    )[:24]
    if table["table_id"] != expected_table_id:
        errors.append("/table/table_id: does not match table identity")

    expected_reasons = sorted(set(structure_reasons + ["cell_text_not_fully_verified"]))
    if record["status"] != "hold":
        errors.append("/status: v0.1 raw text must remain on hold")
    if record["reasons"] != expected_reasons:
        errors.append("/reasons: do not match recomputed v0.1 reasons")
    return errors


def validate_observation(record: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if not SCHEMA_PATH.exists():
        return [f"schema is missing: {SCHEMA_PATH}"]
    for error in sorted(
        _schema_validator(SCHEMA_PATH).iter_errors(record), key=lambda e: list(e.path)
    ):
        location = "/" + "/".join(str(value) for value in error.path)
        errors.append(f"{location}: {error.message}")
    if errors:
        return errors
    errors.extend(_semantic_observation_errors(record))
    if record["hashes"]["table_sha256"] != sha256_json(record["table"]):
        errors.append("/hashes/table_sha256: does not match table")
    signature = sha256_json(_signature_payload(record))
    if record["hashes"]["signature_sha256"] != signature:
        errors.append("/hashes/signature_sha256: does not match signature payload")
    if record["observation_id"] != "vtobs_" + signature[:24]:
        errors.append("/observation_id: does not match signature")
    integrity = sha256_json(record_integrity_payload(record))
    if record["hashes"]["record_integrity_sha256"] != integrity:
        errors.append("/hashes/record_integrity_sha256: does not match complete record")
    return errors


def write_json(path: Path, record: Mapping[str, Any], *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"output already exists: {path}")
    if path.is_symlink():
        raise ValueError("output must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (canonical_json(record) + "\n").encode("utf-8")
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists() or temporary.is_symlink():
        raise FileExistsError(f"temporary output already exists: {temporary}")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists() and not overwrite:
            raise FileExistsError(f"output already exists: {path}")
        if overwrite and path.exists():
            path.unlink()
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--repository-root", type=Path, default=ROOT)
    value.add_argument("--source-root", type=Path, required=True)
    value.add_argument("--visual-assets", type=Path, required=True)
    value.add_argument("--docling-runs", type=Path, required=True)
    value.add_argument("--asset-id", required=True)
    value.add_argument("--sample-id")
    value.add_argument("--output", type=Path, required=True)
    value.add_argument("--overwrite", action="store_true")
    return value


def main() -> int:
    args = parser().parse_args()
    repository_root = resolve_trusted_root(args.repository_root, "repository root")
    visual_assets_path = resolve_repo_input(
        repository_root, args.visual_assets, "visual asset manifest"
    )
    docling_runs_path = resolve_repo_input(
        repository_root, args.docling_runs, "Docling runs"
    )
    output_path = resolve_repo_output(repository_root, args.output)
    visual_records = load_jsonl(visual_assets_path, label="visual asset manifest")
    asset = select_visual_asset(visual_records, args.asset_id, visual_assets_path)
    docling_records = load_jsonl(docling_runs_path, label="Docling runs")
    run = select_docling_run(
        docling_records,
        sample_id=args.sample_id,
        image_sha256=asset["materialization"]["sha256"],
    )
    verify_source_and_member(asset, args.source_root)
    verify_image_and_lineage(asset, run, repository_root)
    observation = build_observation(asset=asset, run=run)
    problems = validate_observation(observation)
    if problems:
        raise ValueError("generated observation is invalid: " + "; ".join(problems))
    write_json(output_path, observation, overwrite=args.overwrite)
    print(
        canonical_json(
            {
                "observation_id": observation["observation_id"],
                "status": observation["status"],
                "structure_status": observation["table"]["structure_status"],
                "rows": observation["table"]["rows"],
                "columns": observation["table"]["columns"],
                "cell_count": observation["table"]["coverage"]["cell_count"],
                "output": str(output_path),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
