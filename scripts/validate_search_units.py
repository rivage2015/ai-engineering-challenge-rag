#!/usr/bin/env python3
"""Validate search units and their traceability to intermediate Evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


ID_PATTERN = re.compile(r"^su_[0-9a-f]{16,64}$")
DOCUMENT_PATTERN = re.compile(r"^doc_[0-9a-f]{16,64}$")
EVIDENCE_PATTERN = re.compile(r"^ev_[0-9a-f]{16,64}$")
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
    "object_index", "series_index",
}
CONTEXT_KEYS = {
    "heading_text", "header_labels", "header_evidence_ids", "header_method",
    "is_header_candidate", "container_kind", "container_heading_text",
    "container_heading_evidence_ids", "quality_tier", "agreement_types",
    "provisional_marker", "bbox_coordinate_system", "reading_order_method",
    "row_band_count",
}
PROVISIONAL_OCR_MARKER = "[暫定読取]"
OCR_QUALITY_BY_AGREEMENT = {
    "independent_agreement": "high",
    "same_engine_agreement": "provisional",
    "provisional_single_pass": "provisional",
}
OCR_BBOX_COORDINATE_SYSTEMS = {
    "raw_raster_top_left_normalized_1000",
    "display_oriented_top_left_normalized_1000",
    "source_orientation_1_top_left_normalized_1000",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
) -> tuple[set[str], dict[str, dict[str, Any]]]:
    intermediates = [intermediate] if isinstance(intermediate, Path) else intermediate
    documents: set[str] = set()
    evidence: dict[str, dict[str, Any]] = {}
    for directory in intermediates:
        with (directory / "documents.jsonl").open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    document_id = json.loads(line)["document_id"]
                    if document_id in documents:
                        raise ValueError(f"duplicate intermediate document: {document_id}")
                    documents.add(document_id)
        with (directory / "evidence.jsonl").open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    item = json.loads(line)
                    if item["evidence_id"] in evidence:
                        raise ValueError(f"duplicate intermediate Evidence: {item['evidence_id']}")
                    evidence[item["evidence_id"]] = item
    return documents, evidence


def image_packet_contract_errors(
    item: dict[str, Any],
    label: str,
    evidence: dict[str, dict[str, Any]],
) -> list[str]:
    """Check packet-level and referenced-line OCR quality invariants."""
    context = item.get("context", {})
    quality_keys = {
        "quality_tier", "agreement_types", "provisional_marker",
        "bbox_coordinate_system", "reading_order_method", "row_band_count",
    }
    if item.get("unit_type") != "image_text_packet":
        if quality_keys & context.keys():
            return [f"{label}: image quality metadata is only valid on image_text_packet"]
        return []
    errors: list[str] = []
    if context.get("container_kind") != "standalone_image":
        errors.append(f"{label}: image packet container_kind must be standalone_image")
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
    content_lines = [
        line for line in search_text.splitlines()
        if line.strip() and not line.startswith("Image file: ")
    ]
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
            if provenance.get("builder") != "search-unit-builder" or provenance.get("deterministic") is not True:
                errors.append(f"{label}: invalid provenance")
            errors.extend(image_packet_contract_errors(item, label, evidence))
            counts[unit_type] = counts.get(unit_type, 0) + 1
    if errors:
        preview = errors[:100]
        suffix = f"\n- ... {len(errors) - 100} more" if len(errors) > 100 else ""
        raise ValueError("validation failed:\n- " + "\n- ".join(preview) + suffix)
    output_state = search_state.get("output", {})
    state_errors = []
    if search_state.get("build_status") != "complete":
        state_errors.append("search build state is not complete")
    if search_state.get("builder") != "search-unit-builder" or search_state.get("deterministic") is not True:
        state_errors.append("search build state provenance is invalid")
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
