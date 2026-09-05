#!/usr/bin/env python3
"""Validate search units and their traceability to intermediate Evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any


ID_PATTERN = re.compile(r"^su_[0-9a-f]{16,64}$")
DOCUMENT_PATTERN = re.compile(r"^doc_[0-9a-f]{16,64}$")
EVIDENCE_PATTERN = re.compile(r"^ev_[0-9a-f]{16,64}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
UNIT_TYPES = {
    "paragraph_chunk", "table_row", "slide_text", "page_text",
    "text_chunk", "code_chunk", "notebook_cell", "chart_summary", "chart_series",
    "image_text_packet",
}
REQUIRED = {
    "schema_version", "record_type", "search_unit_id", "document_id", "unit_type",
    "source_evidence_ids", "locator", "text", "provenance",
}
ALLOWED = REQUIRED | {"context"}
LOCATOR_KEYS = {
    "page_number", "slide_number", "sheet_name", "cell", "table_index", "shape_id",
    "row_index", "paragraph_start", "paragraph_end", "notebook_cell_index",
    "code_line_start", "code_line_end", "locator_text", "source_member",
    "object_index", "image_object_index", "series_index",
}
CONTEXT_KEYS = {
    "heading_text", "header_labels", "header_evidence_ids", "header_method",
    "is_header_candidate", "container_kind", "container_heading_text",
    "container_heading_evidence_ids", "quality_tier", "agreement_types",
    "provisional_marker", "bbox_coordinate_system", "reading_order_method",
    "row_band_count",
}
PROVISIONAL_OCR_MARKER = "[暫定読取]"
PROVISIONAL_VISUAL_METHODS = {
    "local_vlm_unlocated_transcript_provisional",
    "local_vlm_visual_observation_provisional",
}
OCR_QUALITY_BY_AGREEMENT = {
    "independent_agreement": "high",
    "same_engine_agreement": "provisional",
    "provisional_single_pass": "provisional",
    "display_transform_unresolved": "provisional",
}
OCR_BBOX_COORDINATE_SYSTEMS = {
    "raw_raster_top_left_normalized_1000",
    "display_oriented_top_left_normalized_1000",
    "source_orientation_1_top_left_normalized_1000",
}
IMAGE_CONTAINER_KINDS = {
    "standalone_image", "pdf_page_image", "office_embedded_image",
    "notebook_embedded_image",
}
NATIVE_CHART_UNIT_TYPES = {
    "chart_summary": "chart",
    "chart_series": "chart_series",
}
VERIFIED_CHART_SEARCH_METHODS = {
    "verified_chart_table_adaptation",
    "verified_ooxml_chart_cache",
}
DOCUMENT_VISUAL_CONTAINER_BY_SUFFIX = {
    ".pdf": "pdf_page_image",
    ".docx": "office_embedded_image",
    ".docm": "office_embedded_image",
    ".xlsx": "office_embedded_image",
    ".xlsm": "office_embedded_image",
    ".pptx": "office_embedded_image",
    ".pptm": "office_embedded_image",
    ".ipynb": "notebook_embedded_image",
    ".png": "standalone_image",
    ".jpg": "standalone_image",
    ".jpeg": "standalone_image",
    ".gif": "standalone_image",
    ".bmp": "standalone_image",
    ".tif": "standalone_image",
    ".tiff": "standalone_image",
    ".webp": "standalone_image",
}
ROW_BAND_CENTER_TOLERANCE = 0.55
SEARCH_UNIT_BUILDER = "search-unit-builder"
SEARCH_UNIT_BUILDER_VERSION = "0.6.0"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def is_rfc3339_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return "T" in value and (
        value.endswith("Z") or re.search(r"[+-][0-9]{2}:[0-9]{2}$", value) is not None
    )


def digest_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}_{digest_value(value)[:32]}"


def digest_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def load_intermediate_ids(
    intermediate: Path | list[Path],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    intermediates = [intermediate] if isinstance(intermediate, Path) else intermediate
    documents: dict[str, dict[str, Any]] = {}
    evidence: dict[str, dict[str, Any]] = {}
    for directory in intermediates:
        with (directory / "documents.jsonl").open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    document = json.loads(line)
                    document_id = document["document_id"]
                    if document_id in documents:
                        raise ValueError(f"duplicate intermediate document: {document_id}")
                    documents[document_id] = document
        with (directory / "evidence.jsonl").open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    item = json.loads(line)
                    if item["evidence_id"] in evidence:
                        raise ValueError(f"duplicate intermediate Evidence: {item['evidence_id']}")
                    evidence[item["evidence_id"]] = item
    return documents, evidence


def _evidence_text(record: dict[str, Any]) -> str:
    content = record.get("content", {})
    for key in ("normalized_text", "raw_text"):
        if isinstance(content.get(key), str):
            return content[key].strip()
    return ""


def _evidence_display_value(record: dict[str, Any]) -> str:
    """Reproduce the SearchUnit builder's direct Evidence display value."""
    content = record.get("content", {})
    if not isinstance(content, dict):
        return ""
    for key in ("normalized_text", "raw_text"):
        if key in content:
            return str(content[key]).strip()
    for key in ("normalized_value", "raw_value"):
        if key not in content:
            continue
        value = content[key]
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return canonical_json(value)
        return str(value).strip()
    return ""


def display_transform_unresolved_contract_errors(
    record: dict[str, Any], label: str
) -> list[str]:
    """Validate the fail-closed quality contract for raw embedded-image OCR."""
    native = record.get("native_properties", {})
    if not isinstance(native, dict):
        return []
    errors: list[str] = []
    provenance = record.get("provenance", {})
    if not isinstance(provenance, dict):
        provenance = {}
    agreement = native.get("agreement_type")
    origin = native.get("visual_origin")
    materialization = origin.get("materialization") if isinstance(origin, dict) else None
    is_embedded = isinstance(origin, dict) and origin.get("kind") in {
        "office_embedded_image", "notebook_embedded_image",
    }
    if is_embedded and (
        native.get("quality_tier") != "provisional"
        or native.get("provisional_marker") != PROVISIONAL_OCR_MARKER
        or provenance.get("extraction_method") != "adaptive_local_ocr_provisional"
        or native.get("display_transform_resolved") is not False
        or not isinstance(materialization, dict)
        or materialization.get("display_transform_resolved") is not False
        or materialization.get("display_transform_status") != "unresolved"
    ):
        errors.append(f"{label}: unresolved embedded-image OCR must remain provisional")
    if agreement != "display_transform_unresolved":
        return errors
    if (
        native.get("quality_tier") != "provisional"
        or native.get("provisional_marker") != PROVISIONAL_OCR_MARKER
        or native.get("independent_engines") is not True
        or native.get("display_transform_resolved") is not False
        or native.get("embedded_source_agreement_type") != "independent_agreement"
        or provenance.get("extraction_method") != "adaptive_local_ocr_provisional"
    ):
        errors.append(f"{label}: unresolved display-transform OCR quality is invalid")
    observation = native.get("observation_provenance")
    supporters = observation.get("supporters") if isinstance(observation, dict) else None
    if (
        not isinstance(observation, dict)
        or not isinstance(supporters, list)
        or len(supporters) != 2
        or observation.get("primary_independence_group")
        == observation.get("audit_independence_group")
        or not isinstance(observation.get("audit_independence_group"), str)
    ):
        errors.append(f"{label}: unresolved display-transform OCR supporters are invalid")
    if (
        not isinstance(origin, dict)
        or origin.get("kind") not in {
            "office_embedded_image", "notebook_embedded_image",
        }
        or not isinstance(materialization, dict)
        or materialization.get("display_transform_resolved") is not False
        or materialization.get("display_transform_status") != "unresolved"
    ):
        errors.append(f"{label}: unresolved display-transform origin is invalid")
    return errors


def chart_search_unit_contract_errors(
    item: dict[str, Any],
    label: str,
    evidence: dict[str, dict[str, Any]],
) -> list[str]:
    """Independently rebuild one verified native-chart SearchUnit."""
    expected_evidence_type = NATIVE_CHART_UNIT_TYPES.get(item.get("unit_type"))
    if expected_evidence_type is None:
        return []
    errors: list[str] = []
    source_ids = item.get("source_evidence_ids")
    if (
        not isinstance(source_ids, list)
        or len(source_ids) != 1
        or not isinstance(source_ids[0], str)
    ):
        return [f"{label}: native chart unit must have exactly one source Evidence"]
    source = evidence.get(source_ids[0])
    if not isinstance(source, dict):
        return [f"{label}: native chart source Evidence is missing"]
    if source.get("evidence_type") != expected_evidence_type:
        errors.append(f"{label}: native chart source Evidence type is invalid")
    provenance = source.get("provenance", {})
    if (
        not isinstance(provenance, dict)
        or provenance.get("extraction_method")
        not in VERIFIED_CHART_SEARCH_METHODS
    ):
        errors.append(f"{label}: native chart source method is not verified")
    if source.get("document_id") != item.get("document_id"):
        errors.append(f"{label}: native chart source document differs from unit")
    location = source.get("location", {})
    if not isinstance(location, dict):
        errors.append(f"{label}: native chart source location is invalid")
        expected_locator: dict[str, Any] = {}
    else:
        expected_locator = {
            key: location[key] for key in LOCATOR_KEYS if key in location
        }
    if item.get("locator") != expected_locator:
        errors.append(f"{label}: native chart locator differs from source Evidence")
    expected_text = _evidence_display_value(source)
    expected_text_record = {
        "search_text": expected_text,
        "sha256": hashlib.sha256(expected_text.encode("utf-8")).hexdigest(),
        "char_count": len(expected_text),
    }
    if not expected_text:
        errors.append(f"{label}: native chart source text is empty")
    if item.get("text") != expected_text_record:
        errors.append(f"{label}: native chart text differs from source Evidence")
    if item.get("context") != {"container_kind": "chart"}:
        errors.append(f"{label}: native chart context is invalid")
    return errors


def _image_fragment_text(value: str, quality_tier: str) -> str:
    fragments: list[str] = []
    for line in value.splitlines():
        fragment = line.strip()
        if quality_tier == "provisional" and fragment.startswith(PROVISIONAL_OCR_MARKER):
            fragment = fragment[len(PROVISIONAL_OCR_MARKER):].lstrip()
        if fragment:
            fragments.append(fragment)
    return " ".join(fragments)


def _image_row_bands(lines: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Independently reconstruct the builder's geometry-based reading order."""
    fragments: list[dict[str, Any]] = []
    for line in lines:
        geometry = line.get("geometry")
        if not isinstance(geometry, dict):
            raise ValueError("source OCR Evidence lacks geometry")
        values = [geometry.get(key) for key in ("x", "y", "width", "height")]
        if any(
            not isinstance(value, (int, float)) or isinstance(value, bool)
            for value in values
        ):
            raise ValueError("source OCR Evidence geometry is not numeric")
        x, y, width, height = values
        if (
            x < 0 or y < 0 or width <= 0 or height <= 0
            or x + width > 1000 or y + height > 1000
        ):
            raise ValueError("source OCR Evidence geometry is outside normalized bounds")
        fragments.append({
            **line,
            "_x": float(x),
            "_top": float(y),
            "_height": float(height),
            "_center_y": float(y) + float(height) / 2,
        })
    fragments.sort(key=lambda value: (
        value["_center_y"], value["_x"], value["evidence_id"],
    ))
    groups: list[list[dict[str, Any]]] = []
    for fragment in fragments:
        candidates: list[tuple[float, int]] = []
        for index, group in enumerate(groups):
            group_center = statistics.median(value["_center_y"] for value in group)
            group_height = statistics.median(value["_height"] for value in group)
            distance = abs(fragment["_center_y"] - group_center)
            if distance <= ROW_BAND_CENTER_TOLERANCE * min(
                fragment["_height"], group_height
            ):
                candidates.append((distance, index))
        if candidates:
            groups[min(candidates)[1]].append(fragment)
        else:
            groups.append([fragment])
    groups.sort(key=lambda group: (
        min(value["_top"] for value in group),
        min(value["_x"] for value in group),
        min(value["evidence_id"] for value in group),
    ))
    return [
        sorted(group, key=lambda value: (
            value["_x"], value["_center_y"], value["evidence_id"],
        ))
        for group in groups
    ]


def _visual_origin_errors(
    parent: dict[str, Any],
    visual_sources: list[dict[str, Any]],
    expected_container: str,
    document: dict[str, Any] | None = None,
) -> list[str]:
    """Validate one canonical, source-bound origin shared by an image family."""
    errors: list[str] = []
    parent_location = parent.get("location", {})
    parent_native = parent.get("native_properties", {})
    if not isinstance(parent_location, dict):
        return ["parent image location is not an object"]
    if not isinstance(parent_native, dict):
        return ["parent image native properties are not an object"]
    parent_origin = (
        parent_native.get("visual_origin") if isinstance(parent_native, dict) else None
    )
    if not isinstance(parent_origin, dict):
        return ["parent image visual origin is missing or invalid"]
    required_origin = {
        "kind", "source_relative_path", "source_sha256",
        "source_location", "materialization",
    }
    if not required_origin.issubset(parent_origin):
        errors.append("parent image visual origin is incomplete")
    if parent_origin.get("kind") != expected_container:
        errors.append("parent image visual origin kind is invalid")
    source_location = parent_origin.get("source_location")
    if source_location != parent_location:
        errors.append("parent image visual origin location is inconsistent")

    source_relative_path = parent_origin.get("source_relative_path")
    source_sha256 = parent_origin.get("source_sha256")
    if not isinstance(source_relative_path, str) or not source_relative_path:
        errors.append("parent image visual origin source path is invalid")
    if not isinstance(source_sha256, str) or not SHA256_PATTERN.fullmatch(source_sha256):
        errors.append("parent image visual origin source hash is invalid")
    if document is not None:
        document_source = document.get("source", {})
        if not isinstance(document_source, dict) or (
            source_relative_path != document_source.get("relative_path")
            or source_sha256 != document_source.get("sha256")
        ):
            errors.append("parent image visual origin differs from Document source")
        if isinstance(document_source, dict):
            document_path = document_source.get("relative_path")
            expected_document_container = (
                DOCUMENT_VISUAL_CONTAINER_BY_SUFFIX.get(
                    PurePosixPath(document_path).suffix.casefold()
                )
                if isinstance(document_path, str) else None
            )
            if (
                expected_document_container is not None
                and expected_document_container != expected_container
            ):
                errors.append(
                    "parent image visual origin kind differs from Document format"
                )

    materialization = parent_origin.get("materialization")
    if not isinstance(materialization, dict):
        errors.append("parent image visual materialization is missing or invalid")
    else:
        rendered_sha256 = materialization.get("rendered_sha256")
        if (
            not isinstance(rendered_sha256, str)
            or not SHA256_PATTERN.fullmatch(rendered_sha256)
        ):
            errors.append("parent image rendered hash is invalid")
        if materialization.get("source_sha256") != source_sha256:
            errors.append("parent image materialization source hash is inconsistent")
        if materialization.get("external_network_used") is not False:
            errors.append("parent image materialization is not offline")
        if expected_container in {
            "office_embedded_image", "notebook_embedded_image",
        }:
            embedded_sha256 = materialization.get("embedded_sha256")
            if (
                not isinstance(embedded_sha256, str)
                or not SHA256_PATTERN.fullmatch(embedded_sha256)
                or embedded_sha256 != rendered_sha256
                or parent_native.get("embedded_sha256") != embedded_sha256
            ):
                errors.append("parent embedded image digest binding is invalid")
        else:
            if parent_native.get("source_sha256") != rendered_sha256:
                errors.append("parent rendered image digest binding is invalid")

    for source in visual_sources:
        native = source.get("native_properties", {})
        origin = native.get("visual_origin") if isinstance(native, dict) else None
        if not isinstance(origin, dict):
            errors.append("child visual Evidence origin is missing or invalid")
            continue
        if origin != parent_origin:
            errors.append("child visual Evidence origin differs from parent image")
    return errors


def reconstruct_image_packet(
    item: dict[str, Any],
    evidence: dict[str, dict[str, Any]],
    document: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive an image packet solely from its parent image and child OCR Evidence."""
    source_ids = item.get("source_evidence_ids")
    if not isinstance(source_ids, list) or not source_ids:
        raise ValueError("image packet source Evidence IDs are invalid")
    source_records = [evidence.get(evidence_id) for evidence_id in source_ids]
    if any(record is None for record in source_records):
        raise ValueError("image packet has missing source Evidence")
    image_sources = [
        record for record in source_records
        if isinstance(record, dict) and record.get("evidence_type") == "image"
    ]
    if len(image_sources) != 1:
        raise ValueError("image packet must reference exactly one parent image Evidence")
    parent = image_sources[0]
    parent_id = parent["evidence_id"]
    if source_ids[0] != parent_id:
        raise ValueError("parent image Evidence must be the first source ID")
    if any(
        record.get("evidence_type") not in {"image", "ocr_line"}
        for record in source_records if isinstance(record, dict)
    ):
        raise ValueError("image packet contains a non-image/non-OCR source")

    context = item.get("context", {})
    tier = context.get("quality_tier")
    frame = context.get("bbox_coordinate_system")
    parent_native = parent.get("native_properties", {})
    parent_origin = (
        parent_native.get("visual_origin") if isinstance(parent_native, dict) else None
    )
    if isinstance(parent_origin, dict) and parent_origin.get("kind") in IMAGE_CONTAINER_KINDS:
        expected_container = parent_origin["kind"]
    else:
        raise ValueError("parent image visual origin is missing or invalid")

    ocr_sources = [
        record for record in evidence.values()
        if record.get("evidence_type") == "ocr_line"
        and record.get("parent_evidence_id") == parent_id
        and record.get("native_properties", {}).get("quality_tier") == tier
        and record.get("native_properties", {}).get("bbox_coordinate_system") == frame
        and _evidence_text(record)
    ]
    if not ocr_sources:
        raise ValueError("image packet has no matching child OCR Evidence")
    display_errors = [
        error
        for source in ocr_sources
        for error in display_transform_unresolved_contract_errors(
            source, str(source.get("evidence_id", "OCR Evidence"))
        )
    ]
    if display_errors:
        raise ValueError("; ".join(display_errors))
    origin_errors = _visual_origin_errors(
        parent, ocr_sources, expected_container, document
    )
    if origin_errors:
        raise ValueError("; ".join(origin_errors))

    rows: list[str] = []
    ordered_sources: list[dict[str, Any]] = []
    for band in _image_row_bands(ocr_sources):
        row = " ".join(
            fragment
            for fragment in (
                _image_fragment_text(_evidence_text(source), str(tier))
                for source in band
            )
            if fragment
        ).strip()
        if not row:
            continue
        if tier == "provisional":
            row = f"{PROVISIONAL_OCR_MARKER} {row}"
        rows.append(row)
        ordered_sources.extend(band)
    if not rows:
        raise ValueError("image packet reconstructs to empty text")

    content_ref = parent.get("content", {}).get("content_ref")
    if not isinstance(content_ref, str) or not content_ref:
        raise ValueError("parent image content reference is missing")
    source_name = Path(content_ref.split("::", 1)[0].split("#", 1)[0]).name
    body = "\n".join(rows)
    search_text = f"Image file: {source_name}\n{body}" if source_name else body
    parent_location = parent.get("location", {})
    locator = {
        key: parent_location[key]
        for key in LOCATOR_KEYS if key in parent_location
    }
    if not locator:
        locator = {"object_index": 1}
    locator["locator_text"] = (
        f"container_kind={expected_container};quality_tier={tier};"
        f"bbox_coordinate_system={frame}"
    )
    return {
        "source_evidence_ids": [parent_id] + [
            source["evidence_id"] for source in ordered_sources
        ],
        "locator": locator,
        "search_text": search_text,
        "container_kind": expected_container,
        "agreement_types": sorted({
            source.get("native_properties", {}).get("agreement_type")
            for source in ordered_sources
        }),
        "row_band_count": len(rows),
    }


def _provisional_visual_text(value: str) -> str:
    """Reproduce the builder's visible provisional-line normalization."""
    lines: list[str] = []
    for line in value.splitlines():
        item = line.strip()
        if not item or item == PROVISIONAL_OCR_MARKER:
            continue
        if not item.startswith(PROVISIONAL_OCR_MARKER + " "):
            item = f"{PROVISIONAL_OCR_MARKER} {item}"
        lines.append(item)
    return "\n".join(lines)


def provisional_visual_text_contract_errors(
    item: dict[str, Any],
    label: str,
    evidence: dict[str, dict[str, Any]],
    document: dict[str, Any] | None = None,
) -> list[str]:
    """Rebuild a provisional VLM text chunk from its sole source Evidence."""
    errors: list[str] = []
    source_ids = item.get("source_evidence_ids")
    if not isinstance(source_ids, list) or len(source_ids) != 1:
        return [f"{label}: provisional visual text must have exactly one source Evidence"]
    source = evidence.get(source_ids[0])
    if not isinstance(source, dict):
        return [f"{label}: provisional visual source Evidence is missing"]

    provenance = source.get("provenance", {})
    method = (
        provenance.get("extraction_method")
        if isinstance(provenance, dict) else None
    )
    native = source.get("native_properties", {})
    if not isinstance(native, dict):
        native = {}
    if item.get("unit_type") != "text_chunk":
        errors.append(f"{label}: provisional visual Evidence must produce text_chunk")
    if (
        source.get("evidence_type") != "text_block"
        or method not in PROVISIONAL_VISUAL_METHODS
    ):
        errors.append(f"{label}: provisional visual source method/type is invalid")
    if (
        native.get("quality_tier") != "provisional"
        or native.get("provisional_marker") != PROVISIONAL_OCR_MARKER
        or native.get("question_independent") is not True
    ):
        errors.append(f"{label}: provisional visual source quality is invalid")
    if method == "local_vlm_unlocated_transcript_provisional" and (
        native.get("location_status") != "unlocated"
        or native.get("transcript_type") != "whole_image_faithful_transcript"
        or "geometry" in source
    ):
        errors.append(f"{label}: unlocated provisional visual source is invalid")

    expected_text = _provisional_visual_text(_evidence_text(source))
    if not expected_text:
        errors.append(f"{label}: provisional visual source text is empty")
    if item.get("text", {}).get("search_text") != expected_text:
        errors.append(f"{label}: provisional visual text differs from source Evidence")

    location = source.get("location", {})
    expected_locator = {
        key: location[key] for key in LOCATOR_KEYS if key in location
    } if isinstance(location, dict) else {}
    if item.get("locator") != expected_locator:
        errors.append(f"{label}: provisional visual locator differs from source Evidence")

    origin = native.get("visual_origin")
    origin_kind = origin.get("kind") if isinstance(origin, dict) else None
    if not isinstance(origin, dict) or origin_kind not in IMAGE_CONTAINER_KINDS:
        errors.append(f"{label}: provisional visual source origin is invalid")
    parent_id = source.get("parent_evidence_id")
    parent = evidence.get(parent_id) if isinstance(parent_id, str) else None
    if not isinstance(parent, dict) or parent.get("evidence_type") != "image":
        errors.append(f"{label}: provisional visual parent image is missing")
    elif origin_kind in IMAGE_CONTAINER_KINDS:
        errors.extend(
            f"{label}: {error}"
            for error in _visual_origin_errors(
                parent, [source], origin_kind, document
            )
        )
    expected_container = (
        origin_kind if origin_kind in IMAGE_CONTAINER_KINDS else "standalone_image"
    )
    expected_context = {
        "container_kind": expected_container,
        "quality_tier": "provisional",
        "provisional_marker": PROVISIONAL_OCR_MARKER,
    }
    if item.get("context", {}) != expected_context:
        errors.append(f"{label}: provisional visual context differs from source Evidence")
    return errors


def image_packet_contract_errors(
    item: dict[str, Any],
    label: str,
    evidence: dict[str, dict[str, Any]],
    documents: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    """Check packet-level and referenced-line OCR quality invariants."""
    if item.get("unit_type") in NATIVE_CHART_UNIT_TYPES:
        return chart_search_unit_contract_errors(item, label, evidence)
    context = item.get("context", {})
    quality_keys = {
        "quality_tier", "agreement_types", "provisional_marker",
        "bbox_coordinate_system", "reading_order_method", "row_band_count",
    }
    if item.get("unit_type") != "image_text_packet":
        source_records = [
            evidence.get(evidence_id)
            for evidence_id in item.get("source_evidence_ids", [])
            if isinstance(evidence_id, str)
        ]
        source_methods = {
            source.get("provenance", {}).get("extraction_method")
            for source in source_records if isinstance(source, dict)
        }
        provisional_source = any(
            method in PROVISIONAL_VISUAL_METHODS
            or (isinstance(method, str) and method.startswith("local_vlm_"))
            for method in source_methods
        )
        if provisional_source or quality_keys & context.keys():
            return provisional_visual_text_contract_errors(
                item,
                label,
                evidence,
                documents.get(item.get("document_id")) if documents else None,
            )
        return []
    errors: list[str] = []
    if context.get("container_kind") not in IMAGE_CONTAINER_KINDS:
        errors.append(f"{label}: image packet container_kind is invalid")
    bbox_coordinate_system = context.get("bbox_coordinate_system")
    if bbox_coordinate_system not in OCR_BBOX_COORDINATE_SYSTEMS:
        errors.append(f"{label}: image packet bbox coordinate system is invalid")
    if (
        context.get("reading_order_method") != "geometry_row_bands_v1"
        or not isinstance(context.get("row_band_count"), int)
        or isinstance(context.get("row_band_count"), bool)
        or context.get("row_band_count", 0) < 1
    ):
        errors.append(f"{label}: image packet reading-order metadata is invalid")
    quality_tier = context.get("quality_tier")
    agreement_types = context.get("agreement_types")
    if (
        quality_tier not in {"high", "provisional"}
        or not isinstance(agreement_types, list)
        or not agreement_types
        or len(agreement_types) != len(set(agreement_types))
    ):
        errors.append(f"{label}: image packet quality metadata is invalid")
        return errors
    expected_tiers = {OCR_QUALITY_BY_AGREEMENT.get(value) for value in agreement_types}
    if expected_tiers != {quality_tier}:
        errors.append(f"{label}: image packet agreement types mix quality tiers")
    marker_present = "provisional_marker" in context
    marker = context.get("provisional_marker")
    search_text = item.get("text", {}).get("search_text", "")
    packet_lines = [line for line in search_text.splitlines() if line.strip()]
    content_lines = (
        packet_lines[1:]
        if packet_lines and packet_lines[0].startswith("Image file: ")
        else packet_lines
    )
    if context.get("row_band_count") != len(content_lines):
        errors.append(f"{label}: image packet row-band count does not match its text")
    if quality_tier == "high":
        if marker_present or any(
            line.lstrip().startswith(PROVISIONAL_OCR_MARKER) for line in content_lines
        ):
            errors.append(f"{label}: high image packet carries provisional markers")
    else:
        if marker != PROVISIONAL_OCR_MARKER:
            errors.append(f"{label}: provisional image packet lacks the canonical marker")
        if not content_lines or any(
            not line.lstrip().startswith(PROVISIONAL_OCR_MARKER + " ")
            for line in content_lines
        ):
            errors.append(f"{label}: every provisional image packet line must be marked")

    source_records = [
        evidence[evidence_id]
        for evidence_id in item.get("source_evidence_ids", [])
        if evidence_id in evidence
    ]
    ocr_sources = [
        source for source in source_records if source.get("evidence_type") == "ocr_line"
    ]
    if not ocr_sources:
        errors.append(f"{label}: image packet has no OCR-line source Evidence")
        return errors
    source_tiers = {
        source.get("native_properties", {}).get("quality_tier")
        for source in ocr_sources
    }
    source_agreements = {
        source.get("native_properties", {}).get("agreement_type")
        for source in ocr_sources
    }
    source_coordinate_systems = {
        source.get("native_properties", {}).get("bbox_coordinate_system")
        for source in ocr_sources
    }
    if source_tiers != {quality_tier}:
        errors.append(f"{label}: image packet mixes source Evidence quality tiers")
    if source_agreements != set(agreement_types):
        errors.append(f"{label}: image packet agreement metadata differs from its sources")
    if source_coordinate_systems != {bbox_coordinate_system}:
        errors.append(f"{label}: image packet mixes source bbox coordinate systems")
    try:
        expected = reconstruct_image_packet(
            item,
            evidence,
            documents.get(item.get("document_id")) if documents else None,
        )
    except ValueError as exc:
        errors.append(f"{label}: independent image packet reconstruction failed: {exc}")
        return errors
    if item.get("source_evidence_ids") != expected["source_evidence_ids"]:
        errors.append(f"{label}: image packet source IDs/order differ from OCR Evidence")
    if item.get("locator") != expected["locator"]:
        errors.append(f"{label}: image packet locator differs from parent image Evidence")
    if item.get("text", {}).get("search_text") != expected["search_text"]:
        errors.append(f"{label}: image packet text differs from OCR Evidence")
    if context.get("container_kind") != expected["container_kind"]:
        errors.append(f"{label}: image packet container differs from visual origin")
    if context.get("agreement_types") != expected["agreement_types"]:
        errors.append(f"{label}: image packet agreement order differs from OCR Evidence")
    if context.get("row_band_count") != expected["row_band_count"]:
        errors.append(f"{label}: image packet row order differs from OCR geometry")
    return errors


def validate(search_output: Path, intermediate: Path | list[Path]) -> dict[str, Any]:
    intermediates = [intermediate] if isinstance(intermediate, Path) else intermediate
    documents, evidence = load_intermediate_ids(intermediate)
    search_state = json.loads((search_output / "search-build-state.json").read_text(encoding="utf-8"))
    source_states = [
        (directory / "build-state.json", json.loads((directory / "build-state.json").read_text(encoding="utf-8")))
        for directory in intermediates
    ]
    errors: list[str] = []
    seen: set[str] = set()
    counts: dict[str, int] = {}
    path = search_output / "search_units.jsonl"
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            label = f"search_unit[{line_number}]"
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"{label}: invalid JSON: {exc}")
                continue
            missing = REQUIRED - item.keys()
            if missing:
                errors.append(f"{label}: missing {sorted(missing)}")
            extra = item.keys() - ALLOWED
            if extra:
                errors.append(f"{label}: unexpected fields {sorted(extra)}")
            if item.get("schema_version") != "0.1" or item.get("record_type") != "search_unit":
                errors.append(f"{label}: schema_version/record_type mismatch")
            unit_id = item.get("search_unit_id", "")
            if not ID_PATTERN.fullmatch(unit_id):
                errors.append(f"{label}: malformed search_unit_id")
            if unit_id in seen:
                errors.append(f"{label}: duplicate id {unit_id}")
            seen.add(unit_id)
            document_id = item.get("document_id", "")
            if not DOCUMENT_PATTERN.fullmatch(document_id) or document_id not in documents:
                errors.append(f"{label}: dangling document_id {document_id}")
            unit_type = item.get("unit_type")
            if unit_type not in UNIT_TYPES:
                errors.append(f"{label}: invalid unit_type {unit_type!r}")
            source_ids = item.get("source_evidence_ids", [])
            if not source_ids or len(source_ids) != len(set(source_ids)):
                errors.append(f"{label}: source_evidence_ids must be nonempty and unique")
            for evidence_id in source_ids:
                source_record = evidence.get(evidence_id)
                if (
                    not EVIDENCE_PATTERN.fullmatch(evidence_id)
                    or source_record is None
                    or source_record.get("document_id") != document_id
                ):
                    errors.append(f"{label}: dangling or cross-document Evidence {evidence_id}")
            context = item.get("context", {})
            if context.keys() - CONTEXT_KEYS:
                errors.append(f"{label}: unexpected context fields {sorted(context.keys() - CONTEXT_KEYS)}")
            for evidence_id in context.get("header_evidence_ids", []):
                if evidence_id not in source_ids:
                    errors.append(f"{label}: header Evidence is absent from source_evidence_ids: {evidence_id}")
            for evidence_id in context.get("container_heading_evidence_ids", []):
                if evidence_id not in source_ids:
                    errors.append(f"{label}: container heading Evidence is absent from source_evidence_ids: {evidence_id}")
            text = item.get("text", {})
            if set(text) != {"search_text", "sha256", "char_count"}:
                errors.append(f"{label}: invalid text fields")
            search_text = text.get("search_text", "")
            text_sha = hashlib.sha256(search_text.encode("utf-8")).hexdigest()
            if not search_text or text.get("sha256") != text_sha or text.get("char_count") != len(search_text):
                errors.append(f"{label}: text content/hash/count mismatch")
            provenance = item.get("provenance", {})
            if set(provenance) != {"builder", "builder_version", "generated_at", "deterministic"}:
                errors.append(f"{label}: invalid provenance fields")
            locator = item.get("locator", {})
            if locator.keys() - LOCATOR_KEYS:
                errors.append(f"{label}: unexpected locator fields {sorted(locator.keys() - LOCATOR_KEYS)}")
            expected = stable_id("su", {
                "document_id": document_id,
                "unit_type": unit_type,
                "source_evidence_ids": source_ids,
                "locator": item.get("locator", {}),
                "text_sha256": text_sha,
                "builder": provenance.get("builder"),
                "builder_version": provenance.get("builder_version"),
            })
            if unit_id != expected:
                errors.append(f"{label}: unstable search unit id")
            expected_provenance = {
                "builder": SEARCH_UNIT_BUILDER,
                "builder_version": SEARCH_UNIT_BUILDER_VERSION,
                "generated_at": search_state.get("generated_at"),
                "deterministic": True,
            }
            if provenance != expected_provenance:
                errors.append(f"{label}: provenance differs from search build state")
            errors.extend(
                image_packet_contract_errors(item, label, evidence, documents)
            )
            counts[unit_type] = counts.get(unit_type, 0) + 1
    if errors:
        preview = errors[:100]
        suffix = f"\n- ... {len(errors) - 100} more" if len(errors) > 100 else ""
        raise ValueError("validation failed:\n- " + "\n- ".join(preview) + suffix)
    output_state = search_state.get("output", {})
    state_errors = []
    if search_state.get("build_status") != "complete":
        state_errors.append("search build state is not complete")
    if (
        search_state.get("builder") != SEARCH_UNIT_BUILDER
        or search_state.get("builder_version") != SEARCH_UNIT_BUILDER_VERSION
        or search_state.get("deterministic") is not True
    ):
        state_errors.append("search build state provenance is invalid")
    generated_at = search_state.get("generated_at")
    source_run_at_values = {source_state.get("run_at") for _, source_state in source_states}
    if (
        not is_rfc3339_timestamp(generated_at)
        or len(source_run_at_values) != 1
        or generated_at not in source_run_at_values
    ):
        state_errors.append("search build state generated_at does not match its inputs")
    if output_state.get("relative_path") != path.name:
        state_errors.append("search build state output path mismatch")
    if output_state.get("record_count") != len(seen):
        state_errors.append("search build state record count mismatch")
    if output_state.get("size_bytes") != path.stat().st_size:
        state_errors.append("search build state output size mismatch")
    if output_state.get("sha256") != digest_file(path):
        state_errors.append("search build state output hash mismatch")
    if search_state.get("counts_by_type") != dict(sorted(counts.items())):
        state_errors.append("search build state type counts mismatch")
    expected_sources = [
        {
            "sha256": digest_file(state_path),
            "extractor": source_state.get("extractor"),
            "extractor_version": source_state.get("extractor_version"),
        }
        for state_path, source_state in source_states
    ]
    if search_state.get("source", {}).get("intermediate_states") != expected_sources:
        state_errors.append("intermediate build state list mismatch")
    if len(source_states) == 1 and search_state.get("source", {}).get("intermediate_state_sha256") != expected_sources[0]["sha256"]:
        state_errors.append("intermediate build state hash mismatch")
    if state_errors:
        raise ValueError("validation failed:\n- " + "\n- ".join(state_errors))
    return {"records": len(seen), "counts_by_type": dict(sorted(counts.items()))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("search_output", type=Path)
    parser.add_argument("--intermediate", required=True, type=Path, nargs="+")
    args = parser.parse_args()
    print(canonical_json({"status": "ok", **validate(args.search_output, args.intermediate)}))


if __name__ == "__main__":
    main()
