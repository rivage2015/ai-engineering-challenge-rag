#!/usr/bin/env python3
"""Build a question-independent manifest of visual work items.

The builder expands OCR-deferred PDF pages, enumerates embedded Office and
notebook images, records standalone images, and retains graph-bearing
containers for later renderers.  It never reads competition questions.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import hashlib
import json
import math
import mimetypes
import os
import re
import tempfile
import unicodedata
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


SCHEMA_VERSION = "0.1"
BUILDER = "visual-asset-manifest-builder"
BUILDER_VERSION = "0.2.0"
SELECTION_METHOD = "deterministic-stratified-round-robin-v1"

DEFAULT_MAX_OFFICE_ARCHIVE_ENTRIES = 10_000
DEFAULT_MAX_OFFICE_MEMBER_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_OFFICE_TOTAL_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_OFFICE_COMPRESSION_RATIO = 200.0
OFFICE_MEMBER_READ_CHUNK_BYTES = 1024 * 1024

REQUIRED_INVENTORY_FIELDS = {
    "file_id", "file_path", "file_name", "extension", "file_size",
    "source_sha256", "document_type", "processing_layer", "page_count", "notes",
}
PROCESSING_LAYERS = {"native_text", "ocr_required", "graph_required", "unsupported"}
OFFICE_PREFIXES = {
    "docx": "word/media/",
    "pptx": "ppt/media/",
    "xlsx": "xl/media/",
}
VISUAL_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff",
    ".svg", ".emf", ".wmf",
}
MIME_OVERRIDES = {
    ".emf": "image/emf",
    ".wmf": "image/wmf",
    ".svg": "image/svg+xml",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}
CANDIDATE_LAYER_VALUES = {
    "chart_source_recovery", "text_ocr", "layout_classification",
    "table_structure", "chart_table", "diagram_relations",
    "illustration_description",
}
PRIMARY_STRATA = (
    "scanned_pdf_page",
    "office_embedded_image",
    "standalone_graph",
)
PRIMARY_WEIGHTS = {
    "scanned_pdf_page": 3,
    "office_embedded_image": 3,
    "standalone_graph": 2,
}
ALL_STRATA = PRIMARY_STRATA + (
    "standalone_image",
    "notebook_embedded_image",
    "visual_container",
)
MATERIALIZABLE_ORIGIN_KINDS = {
    "pdf_page", "office_embedded_image", "notebook_embedded_image", "standalone_image",
}
OCR_PAGE_PATTERN = re.compile(r"OCR deferred for pages \[([0-9, ]+)\]")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_RESUME_STATUSES = {
    "pending_materialization", "materialized", "pending_classification",
    "materialization_error", "unsupported_media",
}
DIRECT_RASTER_MEDIA_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(prefix: str, value: Any) -> str:
    digest = digest_bytes(canonical_json(value).encode("utf-8"))
    return f"{prefix}_{digest[:32]}"


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def nfc_path(path: Path | PurePosixPath | str) -> str:
    if isinstance(path, (Path, PurePosixPath)):
        return nfc(path.as_posix())
    return nfc(str(path).replace("\\", "/"))


def media_type_for(name: str) -> str:
    suffix = PurePosixPath(name).suffix.lower()
    return MIME_OVERRIDES.get(suffix) or mimetypes.guess_type(name)[0] or "application/octet-stream"


def utc_timestamp(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"--run-at is not ISO-8601: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError("--run-at must include a timezone")
    return parsed.isoformat()


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return parsed


def office_zip_limits(
    *,
    max_archive_entries: int = DEFAULT_MAX_OFFICE_ARCHIVE_ENTRIES,
    max_member_uncompressed_bytes: int = DEFAULT_MAX_OFFICE_MEMBER_BYTES,
    max_total_uncompressed_bytes: int = DEFAULT_MAX_OFFICE_TOTAL_BYTES,
    max_compression_ratio: float = DEFAULT_MAX_OFFICE_COMPRESSION_RATIO,
) -> dict[str, int | float]:
    integer_limits = {
        "max_archive_entries": max_archive_entries,
        "max_member_uncompressed_bytes": max_member_uncompressed_bytes,
        "max_total_uncompressed_bytes": max_total_uncompressed_bytes,
    }
    for name, value in integer_limits.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if (
        isinstance(max_compression_ratio, bool)
        or not isinstance(max_compression_ratio, (int, float))
        or not math.isfinite(float(max_compression_ratio))
        or float(max_compression_ratio) <= 0
    ):
        raise ValueError("max_compression_ratio must be a finite positive number")
    return {
        **integer_limits,
        "max_compression_ratio": float(max_compression_ratio),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True, type=Path, help="Layer-1 text_inventory.csv")
    parser.add_argument("--root", required=True, type=Path, help="source root used by the inventory")
    parser.add_argument("--out", required=True, type=Path, help="output visual-assets.jsonl")
    parser.add_argument(
        "--batch-out", type=Path,
        help="optional selected-only JSONL ordered by batch rank",
    )
    parser.add_argument(
        "--materializable-out", type=Path,
        help="optional JSONL containing every directly materializable visual asset",
    )
    parser.add_argument(
        "--batch-size", type=nonnegative_int, default=16,
        help="deterministic representative batch size (default: 16)",
    )
    parser.add_argument(
        "--max-office-archive-entries", type=positive_int,
        default=DEFAULT_MAX_OFFICE_ARCHIVE_ENTRIES,
        help="maximum ZIP entries inspected per Office container (default: 10000)",
    )
    parser.add_argument(
        "--max-office-member-bytes", type=positive_int,
        default=DEFAULT_MAX_OFFICE_MEMBER_BYTES,
        help="maximum uncompressed bytes per Office media member (default: 67108864)",
    )
    parser.add_argument(
        "--max-office-total-bytes", type=positive_int,
        default=DEFAULT_MAX_OFFICE_TOTAL_BYTES,
        help="maximum total uncompressed Office media bytes (default: 268435456)",
    )
    parser.add_argument(
        "--max-office-compression-ratio", type=positive_float,
        default=DEFAULT_MAX_OFFICE_COMPRESSION_RATIO,
        help="maximum declared Office media compression ratio (default: 200)",
    )
    parser.add_argument("--run-at", help="ISO-8601 provenance timestamp")
    output_mode = parser.add_mutually_exclusive_group()
    output_mode.add_argument(
        "--resume", action="store_true",
        help="preserve statuses from an unchanged existing manifest",
    )
    output_mode.add_argument(
        "--overwrite", action="store_true",
        help="replace an existing manifest and reset its processing statuses",
    )
    return parser.parse_args()


def read_inventory(path: Path) -> tuple[list[dict[str, str]], str]:
    if not path.is_file():
        raise ValueError(f"inventory does not exist: {path}")
    inventory_sha256 = digest_file(path)
    rows: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    seen_ids: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        missing = REQUIRED_INVENTORY_FIELDS - fields
        if missing:
            raise ValueError(f"inventory is missing fields: {sorted(missing)}")
        for line_number, raw in enumerate(reader, 2):
            row = {key: nfc(value or "") for key, value in raw.items()}
            relative_path = nfc_path(row["file_path"])
            if not relative_path or relative_path.startswith("/") or ".." in PurePosixPath(relative_path).parts:
                raise ValueError(f"inventory:{line_number}: unsafe file_path")
            if relative_path in seen_paths:
                raise ValueError(f"inventory:{line_number}: duplicate file_path: {relative_path}")
            source_sha256 = row["source_sha256"]
            if not SHA256_PATTERN.fullmatch(source_sha256):
                raise ValueError(f"inventory:{line_number}: invalid source_sha256")
            expected_file_id = stable_id(
                "file", {"relative_path": relative_path, "source_sha256": source_sha256}
            )
            if row["file_id"] != expected_file_id:
                raise ValueError(f"inventory:{line_number}: unstable file_id")
            if row["file_id"] in seen_ids:
                raise ValueError(f"inventory:{line_number}: duplicate file_id")
            try:
                size_bytes = int(row["file_size"])
            except ValueError as exc:
                raise ValueError(f"inventory:{line_number}: invalid file_size") from exc
            if size_bytes < 0:
                raise ValueError(f"inventory:{line_number}: file_size must be non-negative")
            layers = tuple(item for item in row["processing_layer"].split(";") if item)
            if not layers or len(layers) != len(set(layers)) or not set(layers) <= PROCESSING_LAYERS:
                raise ValueError(f"inventory:{line_number}: invalid processing_layer")
            row["file_path"] = relative_path
            row["extension"] = row["extension"].lower().lstrip(".")
            row["file_size"] = str(size_bytes)
            row["processing_layer"] = ";".join(layers)
            rows.append(row)
            seen_paths.add(relative_path)
            seen_ids.add(row["file_id"])
    return sorted(rows, key=lambda item: item["file_path"]), inventory_sha256


def source_file_index(root: Path) -> dict[str, Path]:
    if root.is_symlink():
        raise ValueError(f"source root must not be a symlink: {root}")
    if not root.is_dir():
        raise ValueError(f"source root is not a directory: {root}")
    resolved_root = root.resolve(strict=True)
    result: dict[str, Path] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"symlinks are not allowed below source root: {path}")
        if not path.is_file():
            continue
        resolved_path = path.resolve(strict=True)
        try:
            resolved_relative = resolved_path.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError(f"source candidate resolves outside --root: {path}") from exc
        relative_path = nfc_path(resolved_relative)
        previous = result.get(relative_path)
        if previous is not None and previous != resolved_path:
            raise ValueError(f"NFC path collision below source root: {relative_path}")
        result[relative_path] = resolved_path
    return result


def resolve_source(row: dict[str, str], index: dict[str, Path], root: Path) -> Path:
    path = index.get(row["file_path"])
    if path is None:
        raise ValueError(f"inventory source is missing below --root: {row['file_path']}")
    if path.is_symlink():
        raise ValueError(f"inventory source must not be a symlink: {row['file_path']}")
    resolved_root = root.resolve(strict=True)
    resolved_path = path.resolve(strict=True)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"inventory source resolves outside --root: {row['file_path']}") from exc
    if resolved_path.stat().st_size != int(row["file_size"]):
        raise ValueError(f"source size changed since inventory: {row['file_path']}")
    if digest_file(resolved_path) != row["source_sha256"]:
        raise ValueError(f"source hash changed since inventory: {row['file_path']}")
    return resolved_path


def document_id(row: dict[str, str]) -> str:
    return stable_id(
        "doc",
        {"relative_path": row["file_path"], "source_sha256": row["source_sha256"]},
    )


def asset_identity(
    *, row: dict[str, str], origin_kind: str, page_number: int | None,
    member_path: str | None, member_sha256: str | None,
) -> dict[str, Any]:
    return {
        "file_id": row["file_id"],
        "document_id": document_id(row),
        "source_sha256": row["source_sha256"],
        "origin_kind": origin_kind,
        "page_number": page_number,
        "member_path": member_path,
        "member_sha256": member_sha256,
    }


def make_record(
    *, row: dict[str, str], origin_kind: str, page_number: int | None,
    member_path: str | None, member_sha256: str | None,
    member_size_bytes: int | None, media_type: str | None,
    candidate_layers: Iterable[str], selection_stratum: str,
    discovery_method: str, inventory_path: Path, inventory_sha256: str,
    root: Path, generated_at: str,
    office_limits: dict[str, int | float],
) -> dict[str, Any]:
    layers = list(dict.fromkeys(candidate_layers))
    if not layers or not set(layers) <= CANDIDATE_LAYER_VALUES:
        raise ValueError(f"invalid candidate layers for {row['file_path']}: {layers}")
    normalized_member = nfc_path(member_path) if member_path is not None else None
    identity = asset_identity(
        row=row,
        origin_kind=origin_kind,
        page_number=page_number,
        member_path=normalized_member,
        member_sha256=member_sha256,
    )
    processing_layers = [item for item in row["processing_layer"].split(";") if item]
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "visual_asset",
        "asset_id": stable_id("asset", identity),
        "file_id": row["file_id"],
        "document_id": document_id(row),
        "source": {
            "relative_path": row["file_path"],
            "sha256": row["source_sha256"],
            "size_bytes": int(row["file_size"]),
            "extension": row["extension"],
            "document_type": row["document_type"],
            "processing_layers": processing_layers,
        },
        "origin": {
            "kind": origin_kind,
            "page_number": page_number,
            "member_path": normalized_member,
            "member_sha256": member_sha256,
            "member_size_bytes": member_size_bytes,
            "media_type": media_type,
        },
        "candidate_layers": layers,
        "duplicate_of_asset_id": None,
        "selection": {
            "stratum": selection_stratum,
            "selected_for_batch": False,
            "batch_size": 0,
            "batch_rank": None,
            "stratum_rank": None,
            "method": SELECTION_METHOD,
        },
        "status": "pending_materialization",
        "materialized_path": None,
        "materialization": None,
        "error": None,
        "provenance": {
            "builder": BUILDER,
            "builder_version": BUILDER_VERSION,
            "inventory_path": nfc_path(inventory_path.resolve()),
            "inventory_sha256": inventory_sha256,
            "source_root": nfc_path(root.resolve()),
            "discovery_method": discovery_method,
            "generated_at": generated_at,
            "question_independent": True,
            "office_zip_limits": dict(office_limits),
        },
    }


def deferred_pdf_pages(row: dict[str, str]) -> list[int]:
    match = OCR_PAGE_PATTERN.search(row["notes"])
    if match:
        pages = [int(value.strip()) for value in match.group(1).split(",") if value.strip()]
    else:
        try:
            page_count = int(row["page_count"])
        except ValueError as exc:
            raise ValueError(f"OCR PDF has no deferred-page list or page_count: {row['file_path']}") from exc
        pages = list(range(1, page_count + 1))
    if not pages or len(pages) != len(set(pages)) or min(pages) < 1:
        raise ValueError(f"invalid OCR page list: {row['file_path']}")
    if row["page_count"]:
        page_count = int(row["page_count"])
        if max(pages) > page_count:
            raise ValueError(f"OCR page exceeds page_count: {row['file_path']}")
    return sorted(pages)


def safe_member_name(name: str) -> str:
    normalized = nfc_path(name)
    parts = PurePosixPath(normalized).parts
    if not normalized or normalized.startswith("/") or ".." in parts:
        raise ValueError(f"unsafe container member path: {name}")
    return normalized


def office_media(
    path: Path,
    extension: str,
    *,
    max_archive_entries: int = DEFAULT_MAX_OFFICE_ARCHIVE_ENTRIES,
    max_member_uncompressed_bytes: int = DEFAULT_MAX_OFFICE_MEMBER_BYTES,
    max_total_uncompressed_bytes: int = DEFAULT_MAX_OFFICE_TOTAL_BYTES,
    max_compression_ratio: float = DEFAULT_MAX_OFFICE_COMPRESSION_RATIO,
) -> list[tuple[str, str, int]]:
    """Stream-hash bounded Office media without retaining member payloads."""
    limits = office_zip_limits(
        max_archive_entries=max_archive_entries,
        max_member_uncompressed_bytes=max_member_uncompressed_bytes,
        max_total_uncompressed_bytes=max_total_uncompressed_bytes,
        max_compression_ratio=max_compression_ratio,
    )
    prefix = OFFICE_PREFIXES[extension]
    if not zipfile.is_zipfile(path):
        raise ValueError(f"Office source is not a readable ZIP container: {path}")
    result: list[tuple[str, str, int]] = []
    seen_normalized_members: dict[str, str] = {}
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > limits["max_archive_entries"]:
            raise ValueError(
                f"Office ZIP entry count exceeds safety limit: {path} "
                f"({len(infos)} > {limits['max_archive_entries']})"
            )
        media_members: list[tuple[str, zipfile.ZipInfo]] = []
        declared_total = 0
        for info in sorted(infos, key=lambda item: nfc_path(item.filename)):
            if info.is_dir():
                continue
            member_path = safe_member_name(info.filename)
            if not member_path.startswith(prefix):
                continue
            if PurePosixPath(member_path).suffix.lower() not in VISUAL_SUFFIXES:
                continue
            previous_raw = seen_normalized_members.get(member_path)
            if previous_raw is not None:
                raise ValueError(
                    "NFC member collision in Office container: "
                    f"{path}:{previous_raw!r} and {info.filename!r}"
                )
            seen_normalized_members[member_path] = info.filename
            if info.file_size < 1:
                raise ValueError(f"empty Office media member: {path}:{member_path}")
            if info.file_size > limits["max_member_uncompressed_bytes"]:
                raise ValueError(
                    f"Office media member exceeds uncompressed-size safety limit: "
                    f"{path}:{member_path} "
                    f"({info.file_size} > {limits['max_member_uncompressed_bytes']})"
                )
            declared_total += info.file_size
            if declared_total > limits["max_total_uncompressed_bytes"]:
                raise ValueError(
                    f"Office media total exceeds uncompressed-size safety limit: {path} "
                    f"({declared_total} > {limits['max_total_uncompressed_bytes']})"
                )
            if info.compress_size < 1:
                raise ValueError(
                    f"Office media member has invalid compressed size: {path}:{member_path}"
                )
            compression_ratio = info.file_size / info.compress_size
            if compression_ratio > limits["max_compression_ratio"]:
                raise ValueError(
                    f"Office media member exceeds compression-ratio safety limit: "
                    f"{path}:{member_path} "
                    f"({compression_ratio:.6g} > {limits['max_compression_ratio']:.6g})"
                )
            media_members.append((member_path, info))

        expanded_total = 0
        for member_path, info in media_members:
            member_digest = hashlib.sha256()
            expanded_member = 0
            try:
                with archive.open(info, "r") as handle:
                    while True:
                        remaining_member = limits["max_member_uncompressed_bytes"] - expanded_member
                        remaining_total = limits["max_total_uncompressed_bytes"] - expanded_total
                        read_size = min(
                            OFFICE_MEMBER_READ_CHUNK_BYTES,
                            remaining_member + 1,
                            remaining_total + 1,
                        )
                        if read_size < 1:
                            raise ValueError(
                                f"Office media expansion exceeds safety limit: "
                                f"{path}:{member_path}"
                            )
                        chunk = handle.read(read_size)
                        if not chunk:
                            break
                        expanded_member += len(chunk)
                        expanded_total += len(chunk)
                        if expanded_member > limits["max_member_uncompressed_bytes"]:
                            raise ValueError(
                                f"Office media member exceeds streamed-size safety limit: "
                                f"{path}:{member_path}"
                            )
                        if expanded_total > limits["max_total_uncompressed_bytes"]:
                            raise ValueError(
                                f"Office media total exceeds streamed-size safety limit: {path}"
                            )
                        member_digest.update(chunk)
            except (RuntimeError, NotImplementedError) as exc:
                raise ValueError(
                    f"Office media member cannot be safely streamed: {path}:{member_path}"
                ) from exc
            if expanded_member != info.file_size:
                raise ValueError(
                    f"Office media member size differs from ZIP metadata: "
                    f"{path}:{member_path} ({expanded_member} != {info.file_size})"
                )
            result.append((member_path, member_digest.hexdigest(), expanded_member))
    return result


def notebook_media(path: Path) -> list[tuple[str, str, bytes]]:
    try:
        notebook = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"notebook is not valid UTF-8 JSON: {path}") from exc
    result: list[tuple[str, str, bytes]] = []
    seen_normalized_members: set[str] = set()

    def add_payload(member_path: str, media_type: str, payload: Any) -> None:
        if isinstance(payload, list):
            payload = "".join(str(item) for item in payload)
        if not isinstance(payload, str) or not payload:
            raise ValueError(f"invalid notebook image payload: {path}:{member_path}")
        try:
            if media_type == "image/svg+xml":
                data = payload.encode("utf-8")
            else:
                compact_payload = "".join(payload.split())
                data = base64.b64decode(compact_payload.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error) as exc:
            raise ValueError(f"invalid notebook image encoding: {path}:{member_path}") from exc
        if not data:
            raise ValueError(f"empty notebook image payload: {path}:{member_path}")
        normalized_member = nfc_path(member_path)
        if normalized_member in seen_normalized_members:
            raise ValueError(
                f"NFC member collision in notebook: {path}:{normalized_member}"
            )
        seen_normalized_members.add(normalized_member)
        result.append((normalized_member, media_type, data))

    for cell_index, cell in enumerate(notebook.get("cells", [])):
        if not isinstance(cell, dict):
            continue
        for output_index, output in enumerate(cell.get("outputs", [])):
            if not isinstance(output, dict):
                continue
            data = output.get("data", {})
            if not isinstance(data, dict):
                continue
            for media_type in sorted(key for key in data if key.startswith("image/")):
                add_payload(
                    f"cells/{cell_index}/outputs/{output_index}/data/{media_type}",
                    media_type,
                    data[media_type],
                )
        attachments = cell.get("attachments", {})
        if not isinstance(attachments, dict):
            continue
        for attachment_name in sorted(attachments):
            payloads = attachments[attachment_name]
            if not isinstance(payloads, dict):
                continue
            for media_type in sorted(key for key in payloads if key.startswith("image/")):
                add_payload(
                    f"cells/{cell_index}/attachments/{attachment_name}/{media_type}",
                    media_type,
                    payloads[media_type],
                )
    return result


def graph_candidate_layers() -> list[str]:
    return [
        "chart_source_recovery", "chart_table", "text_ocr",
        "layout_classification", "diagram_relations", "illustration_description",
    ]


def mixed_visual_candidate_layers() -> list[str]:
    return [
        "text_ocr", "layout_classification", "table_structure", "chart_table",
        "diagram_relations", "illustration_description",
    ]


def container_candidate_layers(row: dict[str, str]) -> list[str]:
    if row["extension"] == "xlsx":
        return ["chart_source_recovery", "table_structure", "chart_table", "layout_classification"]
    return [
        "chart_source_recovery", "chart_table", "layout_classification",
        "text_ocr", "diagram_relations",
    ]


def discover_records(
    *, inventory: Path, root: Path, generated_at: str,
    max_office_archive_entries: int = DEFAULT_MAX_OFFICE_ARCHIVE_ENTRIES,
    max_office_member_bytes: int = DEFAULT_MAX_OFFICE_MEMBER_BYTES,
    max_office_total_bytes: int = DEFAULT_MAX_OFFICE_TOTAL_BYTES,
    max_office_compression_ratio: float = DEFAULT_MAX_OFFICE_COMPRESSION_RATIO,
) -> list[dict[str, Any]]:
    office_limits = office_zip_limits(
        max_archive_entries=max_office_archive_entries,
        max_member_uncompressed_bytes=max_office_member_bytes,
        max_total_uncompressed_bytes=max_office_total_bytes,
        max_compression_ratio=max_office_compression_ratio,
    )
    rows, inventory_sha256 = read_inventory(inventory)
    source_index = source_file_index(root)
    records: list[dict[str, Any]] = []
    for row in rows:
        processing_layers = set(row["processing_layer"].split(";"))
        is_visual_candidate = bool(processing_layers & {"ocr_required", "graph_required"})
        if not is_visual_candidate:
            continue
        path = resolve_source(row, source_index, root)
        extension = row["extension"]

        if extension == "pdf" and "ocr_required" in processing_layers:
            for page_number in deferred_pdf_pages(row):
                records.append(make_record(
                    row=row,
                    origin_kind="pdf_page",
                    page_number=page_number,
                    member_path=None,
                    member_sha256=None,
                    member_size_bytes=None,
                    media_type="application/pdf",
                    candidate_layers=mixed_visual_candidate_layers(),
                    selection_stratum="scanned_pdf_page",
                    discovery_method="inventory_pdf_page_expansion",
                    inventory_path=inventory,
                    inventory_sha256=inventory_sha256,
                    root=root,
                    generated_at=generated_at,
                    office_limits=office_limits,
                ))

        if extension in OFFICE_PREFIXES:
            for member_path, member_sha256, member_size_bytes in office_media(
                path,
                extension,
                max_archive_entries=max_office_archive_entries,
                max_member_uncompressed_bytes=max_office_member_bytes,
                max_total_uncompressed_bytes=max_office_total_bytes,
                max_compression_ratio=max_office_compression_ratio,
            ):
                records.append(make_record(
                    row=row,
                    origin_kind="office_embedded_image",
                    page_number=None,
                    member_path=member_path,
                    member_sha256=member_sha256,
                    member_size_bytes=member_size_bytes,
                    media_type=media_type_for(member_path),
                    candidate_layers=(
                        graph_candidate_layers()
                        if "graph_required" in processing_layers
                        else mixed_visual_candidate_layers()
                    ),
                    selection_stratum="office_embedded_image",
                    discovery_method="office_zip_media_scan",
                    inventory_path=inventory,
                    inventory_sha256=inventory_sha256,
                    root=root,
                    generated_at=generated_at,
                    office_limits=office_limits,
                ))

        if extension == "ipynb":
            for member_path, media_type, data in notebook_media(path):
                records.append(make_record(
                    row=row,
                    origin_kind="notebook_embedded_image",
                    page_number=None,
                    member_path=member_path,
                    member_sha256=digest_bytes(data),
                    member_size_bytes=len(data),
                    media_type=media_type,
                    candidate_layers=graph_candidate_layers(),
                    selection_stratum="notebook_embedded_image",
                    discovery_method="notebook_embedded_media_scan",
                    inventory_path=inventory,
                    inventory_sha256=inventory_sha256,
                    root=root,
                    generated_at=generated_at,
                    office_limits=office_limits,
                ))

        if row["document_type"] == "image":
            is_graph = "graph_required" in processing_layers
            records.append(make_record(
                row=row,
                origin_kind="standalone_image",
                page_number=None,
                member_path=None,
                member_sha256=None,
                member_size_bytes=None,
                media_type=media_type_for(row["file_path"]),
                candidate_layers=graph_candidate_layers() if is_graph else mixed_visual_candidate_layers(),
                selection_stratum="standalone_graph" if is_graph else "standalone_image",
                discovery_method="inventory_standalone_image",
                inventory_path=inventory,
                inventory_sha256=inventory_sha256,
                root=root,
                generated_at=generated_at,
                office_limits=office_limits,
            ))

        if (
            "graph_required" in processing_layers
            and row["document_type"] != "image"
            and extension in {*OFFICE_PREFIXES, "ipynb"}
        ):
            records.append(make_record(
                row=row,
                origin_kind="visual_container",
                page_number=None,
                member_path=None,
                member_sha256=None,
                member_size_bytes=None,
                media_type=mimetypes.guess_type(row["file_path"])[0] or "application/octet-stream",
                candidate_layers=container_candidate_layers(row),
                selection_stratum="visual_container",
                discovery_method="inventory_visual_container",
                inventory_path=inventory,
                inventory_sha256=inventory_sha256,
                root=root,
                generated_at=generated_at,
                office_limits=office_limits,
            ))

    asset_ids = [record["asset_id"] for record in records]
    if len(asset_ids) != len(set(asset_ids)):
        raise ValueError("visual asset discovery produced duplicate asset_id values")
    return sorted(records, key=record_sort_key)


def record_sort_key(record: dict[str, Any]) -> tuple[Any, ...]:
    origin = record["origin"]
    return (
        ALL_STRATA.index(record["selection"]["stratum"]),
        record["source"]["relative_path"],
        origin["page_number"] or 0,
        origin["member_path"] or "",
        record["asset_id"],
    )


def diversity_key(record: dict[str, Any]) -> str:
    parts = PurePosixPath(record["source"]["relative_path"]).parts
    if len(parts) >= 2 and parts[0] == "プロジェクト":
        return "/".join(parts[:2])
    return parts[0] if parts else record["file_id"]


def diverse_order(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in sorted(records, key=record_sort_key):
        buckets[diversity_key(record)].append(record)
    ordered: list[dict[str, Any]] = []
    keys = sorted(buckets)
    while any(buckets[key] for key in keys):
        for key in keys:
            if buckets[key]:
                ordered.append(buckets[key].pop(0))
    return ordered


def known_content_sha256(record: dict[str, Any]) -> str | None:
    kind = record["origin"]["kind"]
    if kind == "standalone_image":
        return record["source"]["sha256"]
    if kind in {"office_embedded_image", "notebook_embedded_image"}:
        return record["origin"]["member_sha256"]
    return None


def selection_eligible(record: dict[str, Any]) -> bool:
    if record["duplicate_of_asset_id"] is not None:
        return False
    kind = record["origin"]["kind"]
    if kind == "pdf_page":
        return True
    if kind not in {
        "office_embedded_image", "notebook_embedded_image", "standalone_image",
    }:
        return False
    return record["origin"]["media_type"] in DIRECT_RASTER_MEDIA_TYPES


def mark_duplicates(records: list[dict[str, Any]]) -> None:
    canonical_by_hash: dict[str, str] = {}
    for record in records:
        content_sha256 = known_content_sha256(record)
        if content_sha256 is None:
            continue
        canonical_id = canonical_by_hash.get(content_sha256)
        if canonical_id is None:
            canonical_by_hash[content_sha256] = record["asset_id"]
        else:
            record["duplicate_of_asset_id"] = canonical_id


def primary_quotas(records: list[dict[str, Any]], batch_size: int) -> dict[str, int]:
    available = {
        stratum: sum(
            record["selection"]["stratum"] == stratum
            and selection_eligible(record)
            for record in records
        )
        for stratum in PRIMARY_STRATA
    }
    total_weight = sum(PRIMARY_WEIGHTS.values())
    raw = {
        stratum: batch_size * PRIMARY_WEIGHTS[stratum] / total_weight
        for stratum in PRIMARY_STRATA
    }
    desired = {stratum: int(raw[stratum]) for stratum in PRIMARY_STRATA}
    remainder = batch_size - sum(desired.values())
    for stratum in sorted(
        PRIMARY_STRATA,
        key=lambda item: (-(raw[item] - desired[item]), PRIMARY_STRATA.index(item)),
    )[:remainder]:
        desired[stratum] += 1
    quotas = {stratum: min(desired[stratum], available[stratum]) for stratum in PRIMARY_STRATA}
    remaining = min(batch_size, sum(available.values())) - sum(quotas.values())
    while remaining:
        progressed = False
        for stratum in PRIMARY_STRATA:
            if quotas[stratum] < available[stratum]:
                quotas[stratum] += 1
                remaining -= 1
                progressed = True
                if not remaining:
                    break
        if not progressed:
            break
    return quotas


def apply_selection(records: list[dict[str, Any]], batch_size: int) -> None:
    quotas = primary_quotas(records, batch_size)
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    for stratum in PRIMARY_STRATA:
        candidates = diverse_order(
            record for record in records
            if record["selection"]["stratum"] == stratum
            and selection_eligible(record)
        )
        for stratum_rank, record in enumerate(candidates[:quotas[stratum]], 1):
            record["selection"]["stratum_rank"] = stratum_rank
            selected.append(record)
            selected_ids.add(record["asset_id"])
    eligible_count = sum(selection_eligible(record) for record in records)
    remaining = min(batch_size, eligible_count) - len(selected)
    if remaining:
        fallback = diverse_order(
            record for record in records
            if record["asset_id"] not in selected_ids
            and selection_eligible(record)
        )
        fallback_counts: dict[str, int] = defaultdict(int)
        for record in fallback[:remaining]:
            stratum = record["selection"]["stratum"]
            fallback_counts[stratum] += 1
            record["selection"]["stratum_rank"] = fallback_counts[stratum]
            selected.append(record)
            selected_ids.add(record["asset_id"])
    for record in records:
        record["selection"]["batch_size"] = batch_size
    for batch_rank, record in enumerate(selected, 1):
        record["selection"]["selected_for_batch"] = True
        record["selection"]["batch_rank"] = batch_rank


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: record is not an object")
            records.append(value)
    return records


def resume_timestamp(
    path: Path,
    inventory_sha256: str,
    root: Path,
    batch_size: int,
    expected_office_limits: dict[str, int | float],
) -> str:
    existing = read_jsonl(path)
    if not existing:
        raise ValueError(f"cannot resume an empty manifest: {path}")
    first = existing[0]
    provenance = first.get("provenance", {})
    selection = first.get("selection", {})
    if provenance.get("inventory_sha256") != inventory_sha256:
        raise ValueError("cannot resume: inventory changed; use --overwrite for a new manifest")
    if provenance.get("source_root") != nfc_path(root.resolve()):
        raise ValueError("cannot resume: source root changed; use --overwrite")
    if provenance.get("office_zip_limits") != expected_office_limits:
        raise ValueError("cannot resume: Office ZIP safety limits changed; use --overwrite")
    if selection.get("batch_size") != batch_size:
        raise ValueError("cannot resume: batch size changed; use --overwrite")
    generated_at = provenance.get("generated_at")
    if not isinstance(generated_at, str):
        raise ValueError("cannot resume: generated_at is missing")
    return utc_timestamp(generated_at)


def merge_resume(existing: list[dict[str, Any]], current: list[dict[str, Any]]) -> None:
    old_by_id = {record.get("asset_id"): record for record in existing}
    if len(old_by_id) != len(existing) or set(old_by_id) != {record["asset_id"] for record in current}:
        raise ValueError("cannot resume: discovered asset set changed; use --overwrite")
    for record in current:
        old = old_by_id[record["asset_id"]]
        for key in (
            "file_id", "document_id", "source", "origin", "candidate_layers",
            "duplicate_of_asset_id", "selection", "provenance",
        ):
            if old.get(key) != record[key]:
                raise ValueError(f"cannot resume: immutable asset fields changed: {record['asset_id']}")
        status = old.get("status")
        materialized_path = old.get("materialized_path")
        materialization = old.get("materialization")
        error = old.get("error")
        if status not in ALLOWED_RESUME_STATUSES:
            raise ValueError(f"cannot resume: invalid status: {record['asset_id']}")
        if materialized_path is not None and (not isinstance(materialized_path, str) or not materialized_path):
            raise ValueError(f"cannot resume: invalid materialized_path: {record['asset_id']}")
        if status == "pending_materialization" and materialized_path is not None:
            raise ValueError(f"cannot resume: pending asset has materialized_path: {record['asset_id']}")
        if status == "pending_materialization" and materialization is not None:
            raise ValueError(f"cannot resume: pending asset has materialization: {record['asset_id']}")
        if status in {"pending_materialization", "materialized", "pending_classification"} and error is not None:
            raise ValueError(f"cannot resume: non-failure asset has error: {record['asset_id']}")
        if status in {"materialized", "pending_classification"}:
            if not isinstance(materialization, dict) or materialized_path is None:
                raise ValueError(f"cannot resume: materialized asset lacks metadata: {record['asset_id']}")
            if materialization.get("output_path") != materialized_path:
                raise ValueError(f"cannot resume: materialization path mismatch: {record['asset_id']}")
        if status in {"materialization_error", "unsupported_media"}:
            if materialized_path is not None or materialization is not None:
                raise ValueError(f"cannot resume: failed asset has output metadata: {record['asset_id']}")
            if not isinstance(error, str) or not error:
                raise ValueError(f"cannot resume: failed asset lacks error: {record['asset_id']}")
        record["status"] = status
        record["materialized_path"] = materialized_path
        record["materialization"] = materialization
        record["error"] = error


def atomic_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary_name = handle.name
            for record in records:
                handle.write(canonical_json(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def selected_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (record for record in records if record["selection"]["selected_for_batch"]),
        key=lambda record: record["selection"]["batch_rank"],
    )


def materializable_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return every direct visual asset in immutable manifest order.

    Duplicates intentionally remain present: coverage is tracked per asset, while
    immutable image caches still avoid rewriting identical rendered bytes.
    """
    return [
        record for record in records
        if record["origin"]["kind"] in MATERIALIZABLE_ORIGIN_KINDS
    ]


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_origin: dict[str, int] = defaultdict(int)
    selected_by_stratum: dict[str, int] = defaultdict(int)
    for record in records:
        by_origin[record["origin"]["kind"]] += 1
        if record["selection"]["selected_for_batch"]:
            selected_by_stratum[record["selection"]["stratum"]] += 1
    return {
        "records": len(records),
        "materializable": sum(
            record["origin"]["kind"] in MATERIALIZABLE_ORIGIN_KINDS
            for record in records
        ),
        "selected": sum(selected_by_stratum.values()),
        "duplicates": sum(record["duplicate_of_asset_id"] is not None for record in records),
        "by_origin": dict(sorted(by_origin.items())),
        "selected_by_stratum": dict(sorted(selected_by_stratum.items())),
    }


def build(
    *, inventory: Path, root: Path, output: Path, batch_size: int = 16,
    run_at: str | None = None, resume: bool = False, overwrite: bool = False,
    batch_output: Path | None = None,
    materializable_output: Path | None = None,
    max_office_archive_entries: int = DEFAULT_MAX_OFFICE_ARCHIVE_ENTRIES,
    max_office_member_bytes: int = DEFAULT_MAX_OFFICE_MEMBER_BYTES,
    max_office_total_bytes: int = DEFAULT_MAX_OFFICE_TOTAL_BYTES,
    max_office_compression_ratio: float = DEFAULT_MAX_OFFICE_COMPRESSION_RATIO,
) -> dict[str, Any]:
    if resume and overwrite:
        raise ValueError("--resume and --overwrite are mutually exclusive")
    if batch_size < 0:
        raise ValueError("batch_size must be zero or greater")
    limits = office_zip_limits(
        max_archive_entries=max_office_archive_entries,
        max_member_uncompressed_bytes=max_office_member_bytes,
        max_total_uncompressed_bytes=max_office_total_bytes,
        max_compression_ratio=max_office_compression_ratio,
    )
    inventory = inventory.resolve()
    if root.is_symlink():
        raise ValueError(f"source root must not be a symlink: {root}")
    root = root.resolve()
    output = output.resolve()
    batch_output = batch_output.resolve() if batch_output is not None else None
    materializable_output = (
        materializable_output.resolve() if materializable_output is not None else None
    )
    outputs = [path for path in (output, batch_output, materializable_output) if path is not None]
    if len(outputs) != len(set(outputs)):
        raise ValueError("--out, --batch-out, and --materializable-out must differ")
    if output.exists() and not (resume or overwrite):
        raise ValueError(f"refusing to overwrite existing manifest: {output}")
    if batch_output is not None and batch_output.exists() and not (resume or overwrite):
        raise ValueError(f"refusing to overwrite existing representative batch: {batch_output}")
    if (
        materializable_output is not None
        and materializable_output.exists()
        and not (resume or overwrite)
    ):
        raise ValueError(
            f"refusing to overwrite existing materializable batch: {materializable_output}"
        )
    if resume and not output.is_file():
        raise ValueError(f"cannot resume without an existing manifest: {output}")
    _, inventory_sha256 = read_inventory(inventory)
    if resume:
        generated_at = resume_timestamp(
            output, inventory_sha256, root, batch_size, limits
        )
    else:
        generated_at = utc_timestamp(run_at)
    records = discover_records(
        inventory=inventory,
        root=root,
        generated_at=generated_at,
        max_office_archive_entries=max_office_archive_entries,
        max_office_member_bytes=max_office_member_bytes,
        max_office_total_bytes=max_office_total_bytes,
        max_office_compression_ratio=max_office_compression_ratio,
    )
    mark_duplicates(records)
    apply_selection(records, batch_size)
    if resume:
        merge_resume(read_jsonl(output), records)
    atomic_jsonl(output, records)
    if batch_output is not None:
        atomic_jsonl(batch_output, selected_records(records))
    if materializable_output is not None:
        atomic_jsonl(materializable_output, materializable_records(records))
    return summarize(records)


def main() -> int:
    args = parse_args()
    try:
        summary = build(
            inventory=args.inventory,
            root=args.root,
            output=args.out,
            batch_size=args.batch_size,
            run_at=args.run_at,
            resume=args.resume,
            overwrite=args.overwrite,
            batch_output=args.batch_out,
            materializable_output=args.materializable_out,
            max_office_archive_entries=args.max_office_archive_entries,
            max_office_member_bytes=args.max_office_member_bytes,
            max_office_total_bytes=args.max_office_total_bytes,
            max_office_compression_ratio=args.max_office_compression_ratio,
        )
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise SystemExit(f"error: {exc}") from exc
    print(canonical_json(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
