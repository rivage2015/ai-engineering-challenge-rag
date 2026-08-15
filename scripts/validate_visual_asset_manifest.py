#!/usr/bin/env python3
"""Validate visual-asset discovery, selection, deduplication, and provenance."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import build_visual_asset_manifest as manifest_builder
from materialize_visual_assets import materialization_signature


SHA256 = re.compile(r"^[0-9a-f]{64}$")
ID_PATTERNS = {
    "asset_id": re.compile(r"^asset_[0-9a-f]{32}$"),
    "file_id": re.compile(r"^file_[0-9a-f]{32}$"),
    "document_id": re.compile(r"^doc_[0-9a-f]{32}$"),
}
TOP_LEVEL_KEYS = {
    "schema_version", "record_type", "asset_id", "file_id", "document_id",
    "source", "origin", "candidate_layers", "duplicate_of_asset_id", "selection",
    "status", "materialized_path", "materialization", "error", "provenance",
}
SOURCE_KEYS = {
    "relative_path", "sha256", "size_bytes", "extension", "document_type",
    "processing_layers",
}
ORIGIN_KEYS = {
    "kind", "page_number", "member_path", "member_sha256", "member_size_bytes",
    "media_type",
}
SELECTION_KEYS = {
    "stratum", "selected_for_batch", "batch_size", "batch_rank", "stratum_rank",
    "method",
}
PROVENANCE_KEYS = {
    "builder", "builder_version", "inventory_path", "inventory_sha256", "source_root",
    "discovery_method", "generated_at", "question_independent", "office_zip_limits",
}
OFFICE_ZIP_LIMIT_KEYS = {
    "max_archive_entries", "max_member_uncompressed_bytes",
    "max_total_uncompressed_bytes", "max_compression_ratio",
}
MATERIALIZATION_KEYS = {
    "output_path", "sha256", "size_bytes", "mime_type", "width_px", "height_px",
    "renderer", "renderer_version", "dpi", "signature", "cache_hit", "generated_at",
}
ORIGIN_KINDS = {
    "pdf_page", "office_embedded_image", "notebook_embedded_image",
    "standalone_image", "visual_container",
}
STATUSES = manifest_builder.ALLOWED_RESUME_STATUSES
DISCOVERY_METHODS = {
    "inventory_pdf_page_expansion", "office_zip_media_scan",
    "notebook_embedded_media_scan", "inventory_standalone_image",
    "inventory_visual_container",
}
FORBIDDEN_KEYS = {"question", "query", "answer", "question_id", "answer_id"}
MAX_MATERIALIZED_PIXELS = 50_000_000
PIL_FORMAT_MIME_TYPES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
    "TIFF": "image/tiff",
}


def parse_args() -> argparse.Namespace:
    repository = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path, help="visual-assets.jsonl")
    parser.add_argument(
        "--batch", type=Path,
        help="optional selected-only JSONL to compare with the full manifest",
    )
    parser.add_argument(
        "--materialized-batch", type=Path,
        help="optional materialized selected-only JSONL to verify against the full manifest",
    )
    parser.add_argument(
        "--materializable-batch", type=Path,
        help="optional all-direct-visual JSONL to compare with the full manifest",
    )
    parser.add_argument(
        "--materialized-full-batch", type=Path,
        help="optional materialized all-direct-visual JSONL to verify against the full manifest",
    )
    parser.add_argument("--inventory", required=True, type=Path, help="Layer-1 text_inventory.csv")
    parser.add_argument("--root", required=True, type=Path, help="source root used by the inventory")
    parser.add_argument(
        "--schema", type=Path, default=repository / "schemas" / "visual-asset.schema.json",
        help="VisualAsset JSON Schema",
    )
    parser.add_argument(
        "--batch-size", type=manifest_builder.nonnegative_int,
        help="expected representative batch size; otherwise infer it from the manifest",
    )
    parser.add_argument(
        "--max-office-archive-entries", type=manifest_builder.positive_int,
        default=manifest_builder.DEFAULT_MAX_OFFICE_ARCHIVE_ENTRIES,
        help="expected Office ZIP entry-count safety limit (default: 10000)",
    )
    parser.add_argument(
        "--max-office-member-bytes", type=manifest_builder.positive_int,
        default=manifest_builder.DEFAULT_MAX_OFFICE_MEMBER_BYTES,
        help="expected Office member uncompressed-size limit (default: 67108864)",
    )
    parser.add_argument(
        "--max-office-total-bytes", type=manifest_builder.positive_int,
        default=manifest_builder.DEFAULT_MAX_OFFICE_TOTAL_BYTES,
        help="expected Office media total uncompressed-size limit (default: 268435456)",
    )
    parser.add_argument(
        "--max-office-compression-ratio", type=manifest_builder.positive_float,
        default=manifest_builder.DEFAULT_MAX_OFFICE_COMPRESSION_RATIO,
        help="expected Office media compression-ratio limit (default: 200)",
    )
    return parser.parse_args()


def timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return value


def exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{label} keys mismatch: missing={sorted(expected - actual)} "
            f"extra={sorted(actual - expected)}"
        )
    return value


def positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def nullable_positive_integer(value: Any, label: str) -> int | None:
    if value is None:
        return None
    return positive_integer(value, label)


def nonnegative_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def validate_schema(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"schema does not exist: {path}")
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"schema is invalid JSON: {path}") from exc
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise ValueError("visual asset schema is not draft 2020-12")
    if schema.get("additionalProperties") is not False:
        raise ValueError("visual asset schema must reject additional properties")
    if set(schema.get("required", [])) != TOP_LEVEL_KEYS:
        raise ValueError("visual asset schema required fields do not match the validator contract")
    if set(schema.get("properties", {})) != TOP_LEVEL_KEYS:
        raise ValueError("visual asset schema properties do not match the validator contract")
    return schema


def compile_published_schema(schema: dict[str, Any]) -> tuple[Any | None, str]:
    """Use jsonschema when installed; manual validation remains the strict fallback."""
    try:
        import jsonschema
    except ImportError:
        return None, "strict_manual_fallback"
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
        validator = jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()
        )
    except Exception as exc:
        raise ValueError(f"published VisualAsset schema cannot be compiled: {exc}") from exc
    return validator, "jsonschema_draft202012_format"


def apply_published_schema(record: dict[str, Any], index: int, validator: Any | None) -> None:
    if validator is None:
        return
    errors = list(validator.iter_errors(record))
    if not errors:
        return
    error = min(errors, key=lambda item: (list(map(str, item.absolute_path)), item.message))
    location = ".".join(str(part) for part in error.absolute_path) or "<root>"
    raise ValueError(f"manifest record {index}: schema violation at {location}: {error.message}")


def read_manifest(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"manifest does not exist: {path}")
    records = manifest_builder.read_jsonl(path)
    if not records:
        raise ValueError("manifest is empty")
    return records


def walk_keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_keys(child)


def decoded_image_metadata(path: Path, label: str) -> tuple[str, int, int]:
    try:
        from PIL import Image
    except ImportError as exc:
        raise ValueError(
            "Pillow is required for strict materialized-image validation"
        ) from exc
    try:
        image = Image.open(path)
    except Exception as exc:
        raise ValueError(f"{label}: materialized output is not a decodable image") from exc
    with image:
        image_format = image.format
        width, height = image.size
        if (
            not isinstance(width, int) or not isinstance(height, int)
            or width < 1 or height < 1
        ):
            raise ValueError(f"{label}: decoded image has invalid dimensions")
        pixels = width * height
        if pixels > MAX_MATERIALIZED_PIXELS:
            raise ValueError(
                f"{label}: decoded image exceeds maximum pixel count "
                f"({pixels} > {MAX_MATERIALIZED_PIXELS})"
            )
        try:
            image.load()
        except Exception as exc:
            raise ValueError(f"{label}: materialized output cannot be fully decoded") from exc
    mime_type = PIL_FORMAT_MIME_TYPES.get(str(image_format))
    if mime_type is None:
        raise ValueError(f"{label}: unsupported decoded image format: {image_format!r}")
    return mime_type, width, height


def validate_materialization(record: dict[str, Any], label: str, manifest_path: Path) -> None:
    status = record["status"]
    materialized_path = record["materialized_path"]
    materialization = record["materialization"]
    error = record["error"]
    if error is not None and (not isinstance(error, str) or not error):
        raise ValueError(f"{label}.error must be null or a non-empty string")
    if materialized_path is not None and (not isinstance(materialized_path, str) or not materialized_path):
        raise ValueError(f"{label}.materialized_path must be null or a non-empty string")
    if status == "pending_materialization":
        if materialized_path is not None or materialization is not None or error is not None:
            raise ValueError(f"{label}: pending materialization must not have output metadata")
        return
    if status in {"materialization_error", "unsupported_media"}:
        if materialized_path is not None or materialization is not None:
            raise ValueError(f"{label}: failed materialization must not have output metadata")
        if error is None:
            raise ValueError(f"{label}: failed materialization requires an error")
        return
    if status in {"materialized", "pending_classification"} and not isinstance(materialization, dict):
        raise ValueError(f"{label}: {status} requires materialization metadata")
    if status in {"materialized", "pending_classification"} and error is not None:
        raise ValueError(f"{label}: {status} must have error=null")
    if status not in {"materialization_error", "unsupported_media"} and error is not None:
        raise ValueError(f"{label}: only failure statuses may contain error")
    if materialization is None:
        return
    value = exact_keys(materialization, MATERIALIZATION_KEYS, f"{label}.materialization")
    output_path = value["output_path"]
    if not isinstance(output_path, str) or not output_path:
        raise ValueError(f"{label}.materialization.output_path must be non-empty")
    if materialized_path != output_path:
        raise ValueError(f"{label}: materialized_path must equal materialization.output_path")
    if not SHA256.fullmatch(str(value["sha256"])) or not SHA256.fullmatch(str(value["signature"])):
        raise ValueError(f"{label}: invalid materialization hash or signature")
    positive_integer(value["size_bytes"], f"{label}.materialization.size_bytes")
    positive_integer(value["width_px"], f"{label}.materialization.width_px")
    positive_integer(value["height_px"], f"{label}.materialization.height_px")
    if not isinstance(value["mime_type"], str) or not value["mime_type"].startswith("image/"):
        raise ValueError(f"{label}.materialization.mime_type must be image/*")
    for key in ("renderer", "renderer_version"):
        if not isinstance(value[key], str) or not value[key]:
            raise ValueError(f"{label}.materialization.{key} must be non-empty")
    dpi = value["dpi"]
    if record["origin"]["kind"] == "pdf_page":
        positive_integer(dpi, f"{label}.materialization.dpi")
        signature_dpi = dpi
    else:
        if dpi is not None:
            raise ValueError(f"{label}.materialization.dpi must be null for non-PDF assets")
        signature_dpi = 0
    if not isinstance(value["cache_hit"], bool):
        raise ValueError(f"{label}.materialization.cache_hit must be boolean")
    timestamp(value["generated_at"], f"{label}.materialization.generated_at")
    output = Path(output_path)
    if not output.is_absolute():
        output = manifest_path.parent / output
    if not output.is_file():
        raise ValueError(f"{label}: materialized output is missing: {output}")
    if output.stat().st_size != value["size_bytes"]:
        raise ValueError(f"{label}: materialized output size mismatch")
    if manifest_builder.digest_file(output) != value["sha256"]:
        raise ValueError(f"{label}: materialized output hash mismatch")
    actual_mime_type, actual_width, actual_height = decoded_image_metadata(output, label)
    if value["mime_type"] != actual_mime_type:
        raise ValueError(
            f"{label}: materialized MIME mismatch "
            f"({value['mime_type']} != {actual_mime_type})"
        )
    if value["width_px"] != actual_width or value["height_px"] != actual_height:
        raise ValueError(
            f"{label}: materialized dimensions mismatch "
            f"({value['width_px']}x{value['height_px']} != {actual_width}x{actual_height})"
        )
    expected_signature = materialization_signature(
        record["source"]["sha256"],
        record["origin"],
        signature_dpi,
        value["renderer"],
        value["renderer_version"],
    )
    if value["signature"] != expected_signature:
        raise ValueError(f"{label}: materialization signature mismatch")


def validate_record(
    record: dict[str, Any],
    index: int,
    manifest_path: Path,
    expected_office_limits: dict[str, int | float],
) -> None:
    label = f"manifest record {index}"
    exact_keys(record, TOP_LEVEL_KEYS, label)
    if record["schema_version"] != manifest_builder.SCHEMA_VERSION:
        raise ValueError(f"{label}: unsupported schema_version")
    if record["record_type"] != "visual_asset":
        raise ValueError(f"{label}: invalid record_type")
    for key, pattern in ID_PATTERNS.items():
        if not pattern.fullmatch(str(record[key])):
            raise ValueError(f"{label}: invalid {key}")
    forbidden = FORBIDDEN_KEYS & set(walk_keys(record))
    if forbidden:
        raise ValueError(f"{label}: question-dependent keys are forbidden: {sorted(forbidden)}")

    source = exact_keys(record["source"], SOURCE_KEYS, f"{label}.source")
    if not isinstance(source["relative_path"], str) or not source["relative_path"]:
        raise ValueError(f"{label}: empty source path")
    if manifest_builder.nfc(source["relative_path"]) != source["relative_path"]:
        raise ValueError(f"{label}: source path is not NFC")
    if not SHA256.fullmatch(str(source["sha256"])):
        raise ValueError(f"{label}: invalid source hash")
    nonnegative_integer(source["size_bytes"], f"{label}.source.size_bytes")
    if not isinstance(source["extension"], str) or not source["extension"]:
        raise ValueError(f"{label}: invalid source extension")
    if not isinstance(source["document_type"], str) or not source["document_type"]:
        raise ValueError(f"{label}: invalid document_type")
    layers = source["processing_layers"]
    if (
        not isinstance(layers, list) or not layers or len(layers) != len(set(layers))
        or not set(layers) <= manifest_builder.PROCESSING_LAYERS
    ):
        raise ValueError(f"{label}: invalid processing layers")

    origin = exact_keys(record["origin"], ORIGIN_KEYS, f"{label}.origin")
    kind = origin["kind"]
    if kind not in ORIGIN_KINDS:
        raise ValueError(f"{label}: invalid origin kind")
    page_number = nullable_positive_integer(origin["page_number"], f"{label}.origin.page_number")
    member_path = origin["member_path"]
    member_sha256 = origin["member_sha256"]
    member_size = nullable_positive_integer(
        origin["member_size_bytes"], f"{label}.origin.member_size_bytes"
    )
    if member_path is not None:
        if not isinstance(member_path, str) or not member_path or manifest_builder.nfc(member_path) != member_path:
            raise ValueError(f"{label}: invalid or non-NFC member_path")
    if member_sha256 is not None and not SHA256.fullmatch(str(member_sha256)):
        raise ValueError(f"{label}: invalid member_sha256")
    if not isinstance(origin["media_type"], str) or not origin["media_type"]:
        raise ValueError(f"{label}: invalid origin media_type")
    if kind == "pdf_page":
        if page_number is None or any(value is not None for value in (member_path, member_sha256, member_size)):
            raise ValueError(f"{label}: invalid PDF-page origin")
    elif kind in {"office_embedded_image", "notebook_embedded_image"}:
        if page_number is not None or None in (member_path, member_sha256, member_size):
            raise ValueError(f"{label}: invalid embedded-image origin")
    elif page_number is not None or any(value is not None for value in (member_path, member_sha256, member_size)):
        raise ValueError(f"{label}: invalid standalone/container origin")

    candidate_layers = record["candidate_layers"]
    if (
        not isinstance(candidate_layers, list) or not candidate_layers
        or len(candidate_layers) != len(set(candidate_layers))
        or not set(candidate_layers) <= manifest_builder.CANDIDATE_LAYER_VALUES
    ):
        raise ValueError(f"{label}: invalid candidate_layers")
    duplicate = record["duplicate_of_asset_id"]
    if duplicate is not None and not ID_PATTERNS["asset_id"].fullmatch(str(duplicate)):
        raise ValueError(f"{label}: invalid duplicate_of_asset_id")

    selection = exact_keys(record["selection"], SELECTION_KEYS, f"{label}.selection")
    if selection["stratum"] not in manifest_builder.ALL_STRATA:
        raise ValueError(f"{label}: invalid selection stratum")
    if not isinstance(selection["selected_for_batch"], bool):
        raise ValueError(f"{label}: selected_for_batch must be boolean")
    if isinstance(selection["batch_size"], bool) or not isinstance(selection["batch_size"], int) or selection["batch_size"] < 0:
        raise ValueError(f"{label}: invalid batch_size")
    batch_rank = nullable_positive_integer(selection["batch_rank"], f"{label}.selection.batch_rank")
    stratum_rank = nullable_positive_integer(
        selection["stratum_rank"], f"{label}.selection.stratum_rank"
    )
    if selection["method"] != manifest_builder.SELECTION_METHOD:
        raise ValueError(f"{label}: invalid selection method")
    if selection["selected_for_batch"] != (batch_rank is not None and stratum_rank is not None):
        raise ValueError(f"{label}: selected flag and ranks disagree")
    if duplicate is not None and selection["selected_for_batch"]:
        raise ValueError(f"{label}: duplicate asset must not enter representative batch")

    if record["status"] not in STATUSES:
        raise ValueError(f"{label}: invalid status")
    validate_materialization(record, label, manifest_path)

    provenance = exact_keys(record["provenance"], PROVENANCE_KEYS, f"{label}.provenance")
    if provenance["builder"] != manifest_builder.BUILDER:
        raise ValueError(f"{label}: invalid provenance builder")
    if provenance["builder_version"] != manifest_builder.BUILDER_VERSION:
        raise ValueError(f"{label}: invalid provenance builder version")
    if not SHA256.fullmatch(str(provenance["inventory_sha256"])):
        raise ValueError(f"{label}: invalid inventory hash")
    if provenance["discovery_method"] not in DISCOVERY_METHODS:
        raise ValueError(f"{label}: invalid discovery method")
    if provenance["question_independent"] is not True:
        raise ValueError(f"{label}: question_independent must be true")
    timestamp(provenance["generated_at"], f"{label}.provenance.generated_at")
    limits = exact_keys(
        provenance["office_zip_limits"],
        OFFICE_ZIP_LIMIT_KEYS,
        f"{label}.provenance.office_zip_limits",
    )
    try:
        normalized_limits = manifest_builder.office_zip_limits(
            max_archive_entries=limits["max_archive_entries"],
            max_member_uncompressed_bytes=limits["max_member_uncompressed_bytes"],
            max_total_uncompressed_bytes=limits["max_total_uncompressed_bytes"],
            max_compression_ratio=limits["max_compression_ratio"],
        )
    except ValueError as exc:
        raise ValueError(f"{label}: invalid Office ZIP safety limits: {exc}") from exc
    if normalized_limits != expected_office_limits:
        raise ValueError(
            f"{label}: Office ZIP safety limits do not match validator configuration"
        )

    row = {
        "file_id": record["file_id"],
        "file_path": source["relative_path"],
        "source_sha256": source["sha256"],
    }
    expected_document_id = manifest_builder.stable_id(
        "doc", {"relative_path": source["relative_path"], "source_sha256": source["sha256"]}
    )
    if record["document_id"] != expected_document_id:
        raise ValueError(f"{label}: unstable document_id")
    expected_asset_id = manifest_builder.stable_id(
        "asset",
        manifest_builder.asset_identity(
            row=row,
            origin_kind=kind,
            page_number=page_number,
            member_path=member_path,
            member_sha256=member_sha256,
        ),
    )
    if record["asset_id"] != expected_asset_id:
        raise ValueError(f"{label}: unstable asset_id")


def immutable_view(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: record[key]
        for key in (
            "schema_version", "record_type", "asset_id", "file_id", "document_id",
            "source", "origin", "candidate_layers", "duplicate_of_asset_id", "selection",
            "provenance",
        )
    }


def validate(
    manifest: Path, inventory: Path, root: Path, schema: Path | None = None,
    batch_size: int | None = None, batch: Path | None = None,
    materialized_batch: Path | None = None,
    materializable_batch: Path | None = None,
    materialized_full_batch: Path | None = None,
    max_office_archive_entries: int = manifest_builder.DEFAULT_MAX_OFFICE_ARCHIVE_ENTRIES,
    max_office_member_bytes: int = manifest_builder.DEFAULT_MAX_OFFICE_MEMBER_BYTES,
    max_office_total_bytes: int = manifest_builder.DEFAULT_MAX_OFFICE_TOTAL_BYTES,
    max_office_compression_ratio: float = manifest_builder.DEFAULT_MAX_OFFICE_COMPRESSION_RATIO,
) -> dict[str, Any]:
    manifest = manifest.resolve()
    inventory = inventory.resolve()
    if root.is_symlink():
        raise ValueError(f"source root must not be a symlink: {root}")
    root = root.resolve()
    schema = schema or Path(__file__).resolve().parents[1] / "schemas" / "visual-asset.schema.json"
    schema_document = validate_schema(schema)
    schema_validator, schema_validation = compile_published_schema(schema_document)
    office_limits = manifest_builder.office_zip_limits(
        max_archive_entries=max_office_archive_entries,
        max_member_uncompressed_bytes=max_office_member_bytes,
        max_total_uncompressed_bytes=max_office_total_bytes,
        max_compression_ratio=max_office_compression_ratio,
    )
    records = read_manifest(manifest)
    for index, record in enumerate(records, 1):
        apply_published_schema(record, index, schema_validator)
        validate_record(record, index, manifest, office_limits)
    if records != sorted(records, key=manifest_builder.record_sort_key):
        raise ValueError("manifest records are not in deterministic order")
    asset_ids = [record["asset_id"] for record in records]
    if len(asset_ids) != len(set(asset_ids)):
        raise ValueError("manifest contains duplicate asset_id values")
    batch_sizes = {record["selection"]["batch_size"] for record in records}
    if len(batch_sizes) != 1:
        raise ValueError("manifest contains inconsistent batch_size values")
    actual_batch_size = next(iter(batch_sizes))
    if batch_size is not None and actual_batch_size != batch_size:
        raise ValueError(f"manifest batch_size is {actual_batch_size}, expected {batch_size}")

    generated_values = {record["provenance"]["generated_at"] for record in records}
    if len(generated_values) != 1:
        raise ValueError("manifest contains inconsistent generated_at values")
    generated_at = next(iter(generated_values))
    expected = manifest_builder.discover_records(
        inventory=inventory,
        root=root,
        generated_at=generated_at,
        max_office_archive_entries=max_office_archive_entries,
        max_office_member_bytes=max_office_member_bytes,
        max_office_total_bytes=max_office_total_bytes,
        max_office_compression_ratio=max_office_compression_ratio,
    )
    manifest_builder.mark_duplicates(expected)
    manifest_builder.apply_selection(expected, actual_batch_size)
    if [immutable_view(record) for record in records] != [immutable_view(record) for record in expected]:
        raise ValueError("manifest discovery or deterministic selection does not match current inputs")

    seen: dict[str, dict[str, Any]] = {}
    canonical_by_hash: dict[str, str] = {}
    for record in records:
        duplicate = record["duplicate_of_asset_id"]
        content_sha256 = manifest_builder.known_content_sha256(record)
        if content_sha256 is not None:
            expected_duplicate = canonical_by_hash.get(content_sha256)
            if expected_duplicate is None:
                canonical_by_hash[content_sha256] = record["asset_id"]
            elif duplicate != expected_duplicate:
                raise ValueError(f"duplicate reference is not the first matching asset: {record['asset_id']}")
        if duplicate is not None:
            canonical = seen.get(duplicate)
            if canonical is None:
                raise ValueError(f"duplicate reference is not earlier in the manifest: {record['asset_id']}")
            if content_sha256 is None or manifest_builder.known_content_sha256(canonical) != content_sha256:
                raise ValueError(f"duplicate reference content hash differs: {record['asset_id']}")
        seen[record["asset_id"]] = record

    selected = [record for record in records if record["selection"]["selected_for_batch"]]
    if any(not manifest_builder.selection_eligible(record) for record in selected):
        raise ValueError("representative batch contains a duplicate or unsupported media type")
    ranks = sorted(record["selection"]["batch_rank"] for record in selected)
    if ranks != list(range(1, len(selected) + 1)):
        raise ValueError("representative batch ranks are not contiguous")
    if batch is not None:
        batch_records = read_manifest(batch.resolve())
        expected_batch = manifest_builder.selected_records(records)
        if batch_records != expected_batch:
            raise ValueError("selected-only batch does not exactly match the full manifest")
        batch_ranks = [record["selection"]["batch_rank"] for record in batch_records]
        if batch_ranks != list(range(1, len(batch_records) + 1)):
            raise ValueError("selected-only batch is not ordered by batch_rank")

    expected_materializable = manifest_builder.materializable_records(records)
    if materializable_batch is not None:
        materializable_records = read_manifest(materializable_batch.resolve())
        if materializable_records != expected_materializable:
            raise ValueError(
                "materializable batch does not exactly match direct visual assets in manifest order"
            )
    materialized_selected = 0
    if materialized_batch is not None:
        materialized_records = read_manifest(materialized_batch.resolve())
        expected_selected = manifest_builder.selected_records(records)
        expected_by_id = {record["asset_id"]: record for record in expected_selected}
        materialized_ids = [record.get("asset_id") for record in materialized_records]
        if len(materialized_ids) != len(set(materialized_ids)):
            raise ValueError("materialized batch contains duplicate asset_id values")
        if set(materialized_ids) != set(expected_by_id):
            raise ValueError("materialized batch asset set differs from the selected full manifest")
        for index, record in enumerate(materialized_records, 1):
            apply_published_schema(record, index, schema_validator)
            validate_record(
                record, index, materialized_batch.resolve(), office_limits
            )
            expected_record = expected_by_id[record["asset_id"]]
            if immutable_view(record) != immutable_view(expected_record):
                raise ValueError(
                    f"materialized batch changed immutable discovery fields: {record['asset_id']}"
                )
            if record["status"] != "pending_classification":
                raise ValueError(
                    f"materialized batch record is not pending_classification: {record['asset_id']}"
                )
        materialized_ranks = [
            record["selection"]["batch_rank"] for record in materialized_records
        ]
        if materialized_ranks != list(range(1, len(materialized_records) + 1)):
            raise ValueError("materialized batch is not ordered by batch_rank")
        materialized_selected = len(materialized_records)

    materialized_full = 0
    if materialized_full_batch is not None:
        materialized_records = read_manifest(materialized_full_batch.resolve())
        expected_by_id = {record["asset_id"]: record for record in expected_materializable}
        materialized_ids = [record.get("asset_id") for record in materialized_records]
        if materialized_ids != list(expected_by_id):
            raise ValueError(
                "materialized full batch order or asset set differs from direct visual assets"
            )
        for index, record in enumerate(materialized_records, 1):
            apply_published_schema(record, index, schema_validator)
            validate_record(record, index, materialized_full_batch.resolve(), office_limits)
            expected_record = expected_by_id[record["asset_id"]]
            if immutable_view(record) != immutable_view(expected_record):
                raise ValueError(
                    f"materialized full batch changed immutable fields: {record['asset_id']}"
                )
            if record["status"] != "pending_classification":
                raise ValueError(
                    f"materialized full batch record is not pending_classification: "
                    f"{record['asset_id']}"
                )
        materialized_full = len(materialized_records)
    counts_by_origin = Counter(record["origin"]["kind"] for record in records)
    selected_by_stratum = Counter(record["selection"]["stratum"] for record in selected)
    result = {
        "records": len(records),
        "selected": len(selected),
        "materializable": len(expected_materializable),
        "duplicates": sum(record["duplicate_of_asset_id"] is not None for record in records),
        "counts_by_origin": dict(sorted(counts_by_origin.items())),
        "selected_by_stratum": dict(sorted(selected_by_stratum.items())),
        "schema_validation": schema_validation,
    }
    if materialized_batch is not None:
        result["materialized_selected"] = materialized_selected
    if materialized_full_batch is not None:
        result["materialized_full"] = materialized_full
    return result


def main() -> int:
    args = parse_args()
    try:
        result = validate(
            args.manifest, args.inventory, args.root, args.schema, args.batch_size,
            args.batch, args.materialized_batch,
            args.materializable_batch, args.materialized_full_batch,
            args.max_office_archive_entries,
            args.max_office_member_bytes,
            args.max_office_total_bytes,
            args.max_office_compression_ratio,
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(f"error: {exc}") from exc
    print(manifest_builder.canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
