#!/usr/bin/env python3
"""Validate large SearchUnit outputs with disk-backed Evidence references."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

from lexical_search_common import canonical_json, digest_file


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


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}_{canonical_digest(value)[:32]}"


def records(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                yield line_number, json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc


def image_packet_contract_errors(item: dict[str, Any], label: str) -> list[str]:
    context = item.get("context", {})
    quality_keys = {
        "quality_tier", "agreement_types", "provisional_marker",
        "bbox_coordinate_system", "reading_order_method", "row_band_count",
    }
    if item.get("unit_type") != "image_text_packet":
        return (
            [f"{label}: image quality metadata is only valid on image_text_packet"]
            if quality_keys & context.keys() else []
        )
    errors: list[str] = []
    if context.get("container_kind") != "standalone_image":
        errors.append(f"{label}: image packet container_kind must be standalone_image")
    if context.get("bbox_coordinate_system") not in OCR_BBOX_COORDINATE_SYSTEMS:
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
    if {OCR_QUALITY_BY_AGREEMENT.get(value) for value in agreement_types} != {quality_tier}:
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
    return errors


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=FILE;
        CREATE TABLE documents(id TEXT PRIMARY KEY);
        CREATE TABLE evidence(
            id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            evidence_type TEXT,
            quality_tier TEXT,
            agreement_type TEXT,
            provisional_marker TEXT,
            bbox_coordinate_system TEXT
        );
        CREATE INDEX evidence_document_idx ON evidence(document_id);
        CREATE TABLE units(
            id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            unit_type TEXT,
            quality_tier TEXT,
            agreement_types TEXT,
            bbox_coordinate_system TEXT
        );
        CREATE TABLE refs(unit_id TEXT NOT NULL, unit_document_id TEXT NOT NULL, evidence_id TEXT NOT NULL);
        CREATE INDEX refs_evidence_idx ON refs(evidence_id);
    """)


def validate(search_output: Path, intermediate: Path | list[Path]) -> dict[str, Any]:
    errors: list[str] = []
    counts: Counter[str] = Counter()
    state = json.loads((search_output / "search-build-state.json").read_text(encoding="utf-8"))
    intermediates = [intermediate] if isinstance(intermediate, Path) else intermediate
    if not intermediates:
        raise ValueError("at least one intermediate input is required")
    source_states = [
        (directory / "build-state.json", json.loads((directory / "build-state.json").read_text(encoding="utf-8")))
        for directory in intermediates
    ]
    units_path = search_output / "search_units.jsonl"
    with tempfile.TemporaryDirectory(prefix="aiec-search-validation-") as temporary:
        connection = sqlite3.connect(Path(temporary) / "ids.sqlite3")
        initialize(connection)
        for directory in intermediates:
            for _, item in records(directory / "documents.jsonl"):
                try:
                    connection.execute("INSERT INTO documents VALUES (?)", (item["document_id"],))
                except sqlite3.IntegrityError:
                    errors.append(f"duplicate intermediate document: {item['document_id']}")
        connection.commit()
        evidence_count = 0
        for directory in intermediates:
            for _, item in records(directory / "evidence.jsonl"):
                native = item.get("native_properties", {})
                try:
                    connection.execute(
                        "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            item["evidence_id"], item["document_id"],
                            item.get("evidence_type"), native.get("quality_tier"),
                            native.get("agreement_type"), native.get("provisional_marker"),
                            native.get("bbox_coordinate_system"),
                        ),
                    )
                except sqlite3.IntegrityError:
                    errors.append(f"duplicate intermediate Evidence: {item['evidence_id']}")
                evidence_count += 1
                if evidence_count % 100000 == 0:
                    connection.commit()
        connection.commit()

        unit_count = 0
        for line_number, item in records(units_path):
            unit_count += 1
            label = f"search_unit[{line_number}]"
            missing = REQUIRED - item.keys()
            if missing:
                errors.append(f"{label}: missing {sorted(missing)}")
            extra = item.keys() - ALLOWED
            if extra:
                errors.append(f"{label}: unexpected fields {sorted(extra)}")
            if item.get("schema_version") != "0.1" or item.get("record_type") != "search_unit":
                errors.append(f"{label}: schema_version/record_type mismatch")
            unit_id = item.get("search_unit_id", "")
            document_id = item.get("document_id", "")
            unit_type = item.get("unit_type")
            if not ID_PATTERN.fullmatch(unit_id):
                errors.append(f"{label}: malformed search_unit_id")
            if not DOCUMENT_PATTERN.fullmatch(document_id):
                errors.append(f"{label}: malformed document_id")
            if unit_type not in UNIT_TYPES:
                errors.append(f"{label}: invalid unit_type {unit_type!r}")
            source_ids = item.get("source_evidence_ids", [])
            if not source_ids or len(source_ids) != len(set(source_ids)):
                errors.append(f"{label}: source_evidence_ids must be nonempty and unique")
            if any(not EVIDENCE_PATTERN.fullmatch(evidence_id) for evidence_id in source_ids):
                errors.append(f"{label}: malformed source Evidence ID")
            context = item.get("context", {})
            if context.keys() - CONTEXT_KEYS:
                errors.append(f"{label}: unexpected context fields")
            for key in ("header_evidence_ids", "container_heading_evidence_ids"):
                if any(evidence_id not in source_ids for evidence_id in context.get(key, [])):
                    errors.append(f"{label}: context Evidence absent from source_evidence_ids")
            locator = item.get("locator", {})
            if locator.keys() - LOCATOR_KEYS:
                errors.append(f"{label}: unexpected locator fields")
            text = item.get("text", {})
            search_text = text.get("search_text", "")
            text_sha = hashlib.sha256(search_text.encode("utf-8")).hexdigest()
            if set(text) != {"search_text", "sha256", "char_count"} or not search_text:
                errors.append(f"{label}: invalid text fields")
            elif text.get("sha256") != text_sha or text.get("char_count") != len(search_text):
                errors.append(f"{label}: text hash/count mismatch")
            provenance = item.get("provenance", {})
            if set(provenance) != {"builder", "builder_version", "generated_at", "deterministic"}:
                errors.append(f"{label}: invalid provenance fields")
            expected = stable_id("su", {
                "document_id": document_id,
                "unit_type": unit_type,
                "source_evidence_ids": source_ids,
                "locator": locator,
                "text_sha256": text_sha,
                "builder": provenance.get("builder"),
                "builder_version": provenance.get("builder_version"),
            })
            if unit_id != expected:
                errors.append(f"{label}: unstable search unit id")
            errors.extend(image_packet_contract_errors(item, label))
            try:
                connection.execute(
                    "INSERT INTO units VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        unit_id, document_id, unit_type,
                        context.get("quality_tier"),
                        canonical_json(context.get("agreement_types", [])),
                        context.get("bbox_coordinate_system"),
                    ),
                )
            except sqlite3.IntegrityError:
                errors.append(f"{label}: duplicate search unit id {unit_id}")
            connection.executemany(
                "INSERT INTO refs VALUES (?, ?, ?)",
                ((unit_id, document_id, evidence_id) for evidence_id in source_ids),
            )
            counts[str(unit_type)] += 1
            if unit_count % 100000 == 0:
                connection.commit()
        connection.commit()

        for unit_id, document_id in connection.execute(
            "SELECT u.id,u.document_id FROM units u LEFT JOIN documents d ON d.id=u.document_id "
            "WHERE d.id IS NULL LIMIT 100"
        ):
            errors.append(f"{unit_id}: dangling document_id {document_id}")
        for unit_id, evidence_id in connection.execute(
            "SELECT r.unit_id,r.evidence_id FROM refs r LEFT JOIN evidence e ON e.id=r.evidence_id "
            "WHERE e.id IS NULL LIMIT 100"
        ):
            errors.append(f"{unit_id}: dangling Evidence {evidence_id}")
        for unit_id, evidence_id in connection.execute(
            "SELECT r.unit_id,r.evidence_id FROM refs r JOIN evidence e ON e.id=r.evidence_id "
            "WHERE e.document_id != r.unit_document_id LIMIT 100"
        ):
            errors.append(f"{unit_id}: cross-document Evidence {evidence_id}")
        for unit_id, quality_tier, agreement_types_json, bbox_coordinate_system in connection.execute(
            "SELECT id,quality_tier,agreement_types,bbox_coordinate_system FROM units "
            "WHERE unit_type='image_text_packet' ORDER BY id"
        ):
            source_quality = list(connection.execute(
                "SELECT e.quality_tier,e.agreement_type,e.provisional_marker,e.bbox_coordinate_system "
                "FROM refs r JOIN evidence e ON e.id=r.evidence_id "
                "WHERE r.unit_id=? AND e.evidence_type='ocr_line'",
                (unit_id,),
            ))
            if not source_quality:
                errors.append(f"{unit_id}: image packet has no OCR-line source Evidence")
                continue
            source_tiers = {row[0] for row in source_quality}
            source_agreements = {row[1] for row in source_quality}
            try:
                agreement_types = set(json.loads(agreement_types_json))
            except (TypeError, json.JSONDecodeError):
                agreement_types = set()
            if source_tiers != {quality_tier}:
                errors.append(f"{unit_id}: image packet mixes source Evidence quality tiers")
            if source_agreements != agreement_types:
                errors.append(f"{unit_id}: image packet agreement metadata differs from its sources")
            if {row[3] for row in source_quality} != {bbox_coordinate_system}:
                errors.append(f"{unit_id}: image packet mixes source bbox coordinate systems")
            if quality_tier == "high" and any(row[2] is not None for row in source_quality):
                errors.append(f"{unit_id}: high image packet references provisionally marked Evidence")
            if quality_tier == "provisional" and any(
                row[2] != PROVISIONAL_OCR_MARKER for row in source_quality
            ):
                errors.append(f"{unit_id}: provisional image packet source marker mismatch")
        connection.close()

    output = state.get("output", {})
    if state.get("build_status") != "complete":
        errors.append("search build state is not complete")
    if output.get("relative_path") != units_path.name:
        errors.append("search build output path mismatch")
    if output.get("record_count") != unit_count or output.get("size_bytes") != units_path.stat().st_size:
        errors.append("search build count or size mismatch")
    if output.get("sha256") != digest_file(units_path):
        errors.append("search build output hash mismatch")
    if state.get("counts_by_type") != dict(sorted(counts.items())):
        errors.append("search build type counts mismatch")
    expected_sources = [
        {
            "sha256": digest_file(state_path),
            "extractor": source_state.get("extractor"),
            "extractor_version": source_state.get("extractor_version"),
        }
        for state_path, source_state in source_states
    ]
    actual_source = state.get("source", {})
    actual_sources = actual_source.get("intermediate_states")
    if actual_sources is None and len(source_states) == 1:
        legacy_source = {
            "sha256": actual_source.get("intermediate_state_sha256"),
            "extractor": actual_source.get("extractor"),
            "extractor_version": actual_source.get("extractor_version"),
        }
        if legacy_source != expected_sources[0]:
            errors.append("legacy intermediate state provenance mismatch")
    elif actual_sources != expected_sources:
        errors.append("intermediate state list mismatch")
    if (
        len(source_states) == 1
        and actual_source.get("intermediate_state_sha256") is not None
        and actual_source.get("intermediate_state_sha256") != expected_sources[0]["sha256"]
    ):
        errors.append("intermediate state hash mismatch")
    if errors:
        preview = errors[:100]
        suffix = f"\n- ... {len(errors) - 100} more" if len(errors) > 100 else ""
        raise ValueError("validation failed:\n- " + "\n- ".join(preview) + suffix)
    return {"records": unit_count, "counts_by_type": dict(sorted(counts.items()))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("search_output", type=Path)
    parser.add_argument("--intermediate", required=True, type=Path, nargs="+")
    args = parser.parse_args()
    print(canonical_json({
        "status": "ok",
        **validate(args.search_output.resolve(), [path.resolve() for path in args.intermediate]),
    }))


if __name__ == "__main__":
    main()
