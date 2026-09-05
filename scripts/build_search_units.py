#!/usr/bin/env python3
"""Build deterministic, question-independent search units from Evidence shards."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
from pathlib import Path
from typing import Any, Callable


BUILDER = "search-unit-builder"
BUILDER_VERSION = "0.6.0"
SCHEMA_VERSION = "0.1"
STATE_FILE = "search-build-state.json"
CELL_PATTERN = re.compile(r"^([A-Z]{1,3})([1-9][0-9]*)$")
SEARCH_LOCATOR_KEYS = {
    "page_number", "slide_number", "sheet_name", "cell", "table_index", "shape_id",
    "row_index", "paragraph_start", "paragraph_end", "notebook_cell_index",
    "code_line_start", "code_line_end", "locator_text", "source_member",
    "object_index", "image_object_index", "series_index",
}
FORMULA_CACHED_VALUE_STATUS = "stored_in_file_not_recalculated"
PROVISIONAL_OCR_MARKER = "[暫定読取]"
PROVISIONAL_VISUAL_METHODS = {
    "local_vlm_unlocated_transcript_provisional",
    "local_vlm_visual_observation_provisional",
}
VERIFIED_CHART_METHODS = {
    "verified_chart_table_adaptation",
    "verified_ooxml_chart_cache",
}
OCR_QUALITY_BY_AGREEMENT = {
    "independent_agreement": "high",
    "same_engine_agreement": "provisional",
    "provisional_single_pass": "provisional",
    "display_transform_unresolved": "provisional",
}
IMAGE_READING_ORDER_METHOD = "geometry_row_bands_v1"
ROW_BAND_CENTER_TOLERANCE = 0.55
OCR_BBOX_COORDINATE_SYSTEMS = {
    "raw_raster_top_left_normalized_1000",
    "display_oriented_top_left_normalized_1000",
    "source_orientation_1_top_left_normalized_1000",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def digest_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}_{digest_value(value)[:32]}"


def display_value(evidence: dict[str, Any]) -> str:
    content = evidence.get("content", {})
    if "normalized_text" in content:
        return str(content["normalized_text"]).strip()
    if "raw_text" in content:
        return str(content["raw_text"]).strip()
    if "normalized_value" in content:
        value = content["normalized_value"]
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return canonical_json(value)
        return str(value).strip()
    if "raw_value" in content:
        value = content["raw_value"]
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return canonical_json(value)
        return str(value).strip()
    return ""


def scalar_display(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return canonical_json(value)
    return str(value).strip()


def cached_value_display(value: Any, available: bool) -> str:
    if not available:
        return "なし"
    rendered = scalar_display(value)
    return rendered if rendered else "空文字"


def column_letters_to_number(letters: str) -> int:
    result = 0
    for character in letters:
        result = result * 26 + ord(character) - ord("A") + 1
    return result


def table_cell_identity(evidence: dict[str, Any]) -> tuple[tuple[Any, ...], int, str, dict[str, Any]] | None:
    location = evidence.get("location", {})
    if "cell" in location and "sheet_name" in location:
        match = CELL_PATTERN.fullmatch(location["cell"])
        if not match:
            return None
        letters, row_text = match.groups()
        row = int(row_text)
        column = column_letters_to_number(letters)
        locator = {"sheet_name": location["sheet_name"], "row_index": row}
        return (("sheet", location["sheet_name"], row), column, letters, locator)
    if "table_index" in location and "row_index" in location:
        table = location["table_index"]
        row = location["row_index"]
        column = location.get("column_index", 1)
        locator = {"table_index": table, "row_index": row}
        if "slide_number" in location:
            locator["slide_number"] = location["slide_number"]
        if "shape_id" in location:
            locator["shape_id"] = location["shape_id"]
        container = ("table", location.get("slide_number"), location.get("shape_id"), table)
        return (container + (row,), column, str(column), locator)
    if "shape_id" in location and "row_index" in location and "slide_number" in location:
        row = location["row_index"]
        column = location.get("column_index", 1)
        locator = {
            "slide_number": location["slide_number"],
            "shape_id": location["shape_id"],
            "row_index": row,
        }
        container = ("slide_table", location["slide_number"], location["shape_id"])
        return (container + (row,), column, str(column), locator)
    return None


def image_row_bands(lines: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Reconstruct deterministic visual rows from normalized OCR geometry.

    OCR engines often enumerate a table down columns.  Keeping that engine
    order turns correct characters into an incorrect document.  The line
    Evidence remains untouched; only the question-independent SearchUnit view
    is ordered into top-to-bottom bands and left-to-right fragments.
    """
    fragments: list[dict[str, Any]] = []
    for line in lines:
        geometry = line.get("geometry")
        if not isinstance(geometry, dict):
            raise ValueError("OCR line lacks geometry for reading-order reconstruction")
        values = [geometry.get(key) for key in ("x", "y", "width", "height")]
        if any(
            not isinstance(value, (int, float)) or isinstance(value, bool)
            for value in values
        ):
            raise ValueError("OCR line geometry is not numeric")
        x, y, width, height = values
        if (
            x < 0 or y < 0 or width <= 0 or height <= 0
            or x + width > 1000 or y + height > 1000
        ):
            raise ValueError("OCR line geometry is outside normalized image bounds")
        fragments.append({
            **line,
            "_x": float(x),
            "_top": float(y),
            "_height": float(height),
            "_center_y": float(y) + float(height) / 2,
        })

    fragments.sort(key=lambda item: (
        item["_center_y"], item["_x"], item["evidence_id"],
    ))
    groups: list[list[dict[str, Any]]] = []
    for item in fragments:
        candidates: list[tuple[float, int]] = []
        for index, group in enumerate(groups):
            group_center = statistics.median(value["_center_y"] for value in group)
            group_height = statistics.median(value["_height"] for value in group)
            distance = abs(item["_center_y"] - group_center)
            if distance <= ROW_BAND_CENTER_TOLERANCE * min(item["_height"], group_height):
                candidates.append((distance, index))
        if candidates:
            groups[min(candidates)[1]].append(item)
        else:
            groups.append([item])

    ordered = sorted(
        groups,
        key=lambda group: (
            min(item["_top"] for item in group),
            min(item["_x"] for item in group),
            min(item["evidence_id"] for item in group),
        ),
    )
    return [
        sorted(group, key=lambda item: (
            item["_x"], item["_center_y"], item["evidence_id"],
        ))
        for group in ordered
    ]


def image_fragment_text(value: str, quality_tier: str) -> str:
    fragments = []
    for line in value.splitlines():
        item = line.strip()
        if quality_tier == "provisional" and item.startswith(PROVISIONAL_OCR_MARKER):
            item = item[len(PROVISIONAL_OCR_MARKER):].lstrip()
        if item:
            fragments.append(item)
    return " ".join(fragments)


def make_unit(
    document_id: str,
    unit_type: str,
    evidence_ids: list[str],
    locator: dict[str, Any],
    search_text: str,
    generated_at: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text_sha = hashlib.sha256(search_text.encode("utf-8")).hexdigest()
    identity = {
        "document_id": document_id,
        "unit_type": unit_type,
        "source_evidence_ids": evidence_ids,
        "locator": locator,
        "text_sha256": text_sha,
        "builder": BUILDER,
        "builder_version": BUILDER_VERSION,
    }
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "search_unit",
        "search_unit_id": stable_id("su", identity),
        "document_id": document_id,
        "unit_type": unit_type,
        "source_evidence_ids": evidence_ids,
        "locator": locator,
        "text": {"search_text": search_text, "sha256": text_sha, "char_count": len(search_text)},
        "provenance": {
            "builder": BUILDER,
            "builder_version": BUILDER_VERSION,
            "generated_at": generated_at,
            "deterministic": True,
        },
    }
    if context:
        record["context"] = context
    return record


class DocumentDeriver:
    def __init__(
        self,
        document_id: str,
        generated_at: str,
        emit: Callable[[dict[str, Any]], None],
        target_chars: int,
    ) -> None:
        self.document_id = document_id
        self.generated_at = generated_at
        self.emit = emit
        self.target_chars = target_chars
        self.paragraphs: list[dict[str, Any]] = []
        self.heading: dict[str, Any] | None = None
        self.current_row_key: tuple[Any, ...] | None = None
        self.current_row_locator: dict[str, Any] = {}
        self.current_row_cells: list[dict[str, Any]] = []
        self.headers: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        self.table_contexts: dict[tuple[Any, ...], dict[str, Any]] = {}
        self.current_slide: int | None = None
        self.slide_shapes: list[tuple[str, str]] = []
        self.image_lines: list[dict[str, Any]] = []
        self.image_evidence_id: str | None = None
        self.image_source_label: str | None = None
        self.image_container_kind = "standalone_image"
        self.image_locator: dict[str, Any] = {"object_index": 1}
        self.counts: dict[str, int] = {}

    def write(self, record: dict[str, Any]) -> None:
        self.emit(record)
        unit_type = record["unit_type"]
        self.counts[unit_type] = self.counts.get(unit_type, 0) + 1

    def flush_paragraphs(self) -> None:
        if not self.paragraphs:
            return
        text = "\n\n".join(item["text"] for item in self.paragraphs).strip()
        if text:
            ids = [item["id"] for item in self.paragraphs]
            indices = [item["index"] for item in self.paragraphs]
            context = {}
            if self.heading is not None:
                context["heading_text"] = self.heading["text"]
            self.write(make_unit(
                self.document_id,
                "paragraph_chunk",
                ids,
                {"paragraph_start": min(indices), "paragraph_end": max(indices)},
                text,
                self.generated_at,
                context,
            ))
        self.paragraphs = []

    def add_paragraph(self, evidence: dict[str, Any]) -> None:
        value = display_value(evidence)
        if not value:
            return
        is_heading = evidence["evidence_type"] == "heading"
        if is_heading:
            self.flush_paragraphs()
            self.heading = {
                "id": evidence["evidence_id"],
                "text": value,
                "index": evidence["location"]["paragraph_index"],
            }
        candidate = {"id": evidence["evidence_id"], "text": value, "index": evidence["location"]["paragraph_index"]}
        prospective = sum(len(item["text"]) for item in self.paragraphs) + len(value) + 2 * len(self.paragraphs)
        if self.paragraphs and prospective > self.target_chars:
            self.flush_paragraphs()
            if not is_heading and self.heading is not None:
                self.paragraphs.append(self.heading)
        self.paragraphs.append(candidate)

    def flush_row(self) -> None:
        if self.current_row_key is None:
            return
        cells = sorted(self.current_row_cells, key=lambda item: item["column"])
        populated = [
            cell for cell in cells
            if cell["value"] or cell.get("formula")
        ]
        if populated:
            container = self.current_row_key[:-1]
            header = self.headers.get(container)
            context: dict[str, Any] = dict(self.table_contexts.get(container, {}))
            if header is None:
                self.headers[container] = cells
                context.update({"header_method": "first_non_empty_row_candidate", "is_header_candidate": True})
                labels: dict[int, str] = {}
            else:
                labels = {
                    cell["column"]: cell["value"]
                    for cell in header if cell["value"]
                }
                header_ids = [
                    evidence_id
                    for cell in header if cell["value"]
                    for evidence_id in (
                        cell.get("evidence_id"), cell.get("formula_evidence_id")
                    )
                    if evidence_id
                ]
                context.update({
                    "header_method": "first_non_empty_row_candidate",
                    "is_header_candidate": False,
                    "header_labels": [
                        labels.get(cell["column"], cell["label"])
                        for cell in cells
                    ],
                    "header_evidence_ids": header_ids,
                })
            lines: list[str] = []
            for cell in populated:
                label = labels.get(cell["column"], cell["label"])
                value = cell["value"]
                formula = cell.get("formula")
                if formula:
                    saved = cached_value_display(
                        cell.get("cached_value"),
                        bool(cell.get("cached_value_available")),
                    )
                    # Keep ``Label: =FORMULA`` verbatim for relationship
                    # questions, while exposing the saved workbook value in
                    # the same bounded row field.  Repeating the whole label
                    # on a second line distorts BM25 length/ranking elsewhere.
                    lines.append(
                        f"{label}: {formula} "
                        f"[保存値・ファイル保存時・未再計算: {saved}]"
                    )
                else:
                    lines.append(f"{label}: {value}")
            heading_text = context.get("container_heading_text")
            if heading_text:
                lines.insert(0, f"セクション: {heading_text}")
            row_evidence_ids = [
                evidence_id
                for cell in populated
                for evidence_id in (
                    cell.get("evidence_id"), cell.get("formula_evidence_id")
                )
                if evidence_id
            ]
            evidence_ids = list(dict.fromkeys(
                context.get("container_heading_evidence_ids", [])
                + context.get("header_evidence_ids", [])
                + row_evidence_ids
            ))
            self.write(make_unit(
                self.document_id,
                "table_row",
                evidence_ids,
                self.current_row_locator,
                "\n".join(lines),
                self.generated_at,
                context,
            ))
        self.current_row_key = None
        self.current_row_locator = {}
        self.current_row_cells = []

    def add_cell(self, evidence: dict[str, Any]) -> None:
        identity = table_cell_identity(evidence)
        if identity is None:
            return
        row_key, column, label, locator = identity
        if self.current_row_key != row_key:
            self.flush_row()
            self.current_row_key = row_key
            self.current_row_locator = locator
        self.current_row_cells.append({
            "column": column,
            "label": label,
            "value": display_value(evidence),
            "evidence_id": evidence["evidence_id"],
            "formula": None,
            "formula_evidence_id": None,
            "cached_value": None,
            "cached_value_available": False,
        })

    def add_formula(self, evidence: dict[str, Any]) -> None:
        identity = table_cell_identity(evidence)
        if identity is None:
            return
        row_key, column, label, locator = identity
        if self.current_row_key != row_key:
            self.flush_row()
            self.current_row_key = row_key
            self.current_row_locator = {
                key: value for key, value in locator.items() if key != "cell"
            }
        cell = next(
            (item for item in self.current_row_cells if item["column"] == column),
            None,
        )
        if cell is None:
            cell = {
                "column": column,
                "label": label,
                "value": "",
                "evidence_id": evidence.get("parent_evidence_id"),
                "formula": None,
                "formula_evidence_id": None,
                "cached_value": None,
                "cached_value_available": False,
            }
            self.current_row_cells.append(cell)
        formula = display_value(evidence)
        if not formula:
            return
        native = evidence.get("native_properties", {})
        if native.get("cached_value_status") != FORMULA_CACHED_VALUE_STATUS:
            raise ValueError("formula Evidence lacks stored-value semantics")
        cached_available = native.get("cached_value_available")
        if not isinstance(cached_available, bool):
            raise ValueError("formula Evidence lacks cached-value availability")
        cell["formula"] = formula
        cell["formula_evidence_id"] = evidence["evidence_id"]
        cell["cached_value"] = native.get("cached_value")
        cell["cached_value_available"] = cached_available

        cached_line = cached_value_display(
            native.get("cached_value"), cached_available
        )
        source_evidence_ids = list(dict.fromkeys(
            [
                evidence_id
                for evidence_id in (
                    evidence.get("parent_evidence_id"), evidence["evidence_id"]
                )
                if evidence_id
            ]
        ))
        location = evidence.get("location", {})
        exact_locator = {
            key: location[key] for key in SEARCH_LOCATOR_KEYS if key in location
        }
        self.write(make_unit(
            self.document_id,
            "text_chunk",
            source_evidence_ids,
            exact_locator,
            "\n".join([
                f"セル: {location.get('cell', label)}",
                f"保存値（ファイル保存時・未再計算）: {cached_line}",
                f"式: {formula}",
            ]),
            self.generated_at,
        ))

    def add_table(self, evidence: dict[str, Any]) -> None:
        location = evidence.get("location", {})
        if "table_index" in location:
            container = ("table", location.get("slide_number"), location.get("shape_id"), location["table_index"])
        elif "slide_number" in location and "shape_id" in location:
            container = ("slide_table", location["slide_number"], location["shape_id"])
        else:
            return
        native = evidence.get("native_properties", {})
        context: dict[str, Any] = {"container_kind": "table"}
        heading_text = native.get("preceding_heading_text")
        heading_evidence_id = native.get("preceding_heading_evidence_id")
        if heading_text and heading_evidence_id:
            context["container_heading_text"] = heading_text
            context["container_heading_evidence_ids"] = [heading_evidence_id]
        self.table_contexts[container] = context

    def flush_slide(self) -> None:
        if self.current_slide is not None and self.slide_shapes:
            body = "\n\n".join(value for _, value in self.slide_shapes if value).strip()
            text = f"コンテナ種別: スライド\n{body}" if body else ""
            if text:
                self.write(make_unit(
                    self.document_id,
                    "slide_text",
                    [evidence_id for evidence_id, value in self.slide_shapes if value],
                    {"slide_number": self.current_slide},
                    text,
                    self.generated_at,
                ))
        self.current_slide = None
        self.slide_shapes = []

    def add_shape(self, evidence: dict[str, Any]) -> None:
        slide = evidence.get("location", {}).get("slide_number")
        value = str(evidence.get("content", {}).get("raw_text", "")).strip()
        if slide is None or not value:
            return
        if self.current_slide != slide:
            self.flush_slide()
            self.current_slide = slide
        self.slide_shapes.append((evidence["evidence_id"], value))

    def add_page(self, evidence: dict[str, Any]) -> None:
        value = display_value(evidence)
        page = evidence.get("location", {}).get("page_number")
        if value and page is not None:
            self.write(make_unit(
                self.document_id,
                "page_text",
                [evidence["evidence_id"]],
                {"page_number": page},
                value,
                self.generated_at,
            ))

    def flush_image(self) -> None:
        if not self.image_lines:
            return
        # A packet is the retrieval/answer boundary. Keep both quality tiers
        # and coordinate frames separate before reconstructing visual rows;
        # EXIF-rotated Vision and raw-raster Tesseract boxes are not comparable.
        for quality_tier in ("high", "provisional"):
            coordinate_systems = sorted({
                line["bbox_coordinate_system"] for line in self.image_lines
                if line["quality_tier"] == quality_tier
            })
            for bbox_coordinate_system in coordinate_systems:
                packet_lines = [
                    line for line in self.image_lines
                    if line["quality_tier"] == quality_tier
                    and line["bbox_coordinate_system"] == bbox_coordinate_system
                ]
                row_bands = image_row_bands(packet_lines)
                lines: list[dict[str, Any]] = []
                rows: list[str] = []
                for band in row_bands:
                    fragments = [
                        image_fragment_text(line["text"], quality_tier)
                        for line in band
                    ]
                    row = " ".join(
                        fragment for fragment in fragments if fragment
                    ).strip()
                    if not row:
                        continue
                    if quality_tier == "provisional":
                        row = f"{PROVISIONAL_OCR_MARKER} {row}"
                    rows.append(row)
                    lines.extend(band)
                if not lines:
                    continue
                body = "\n".join(rows).strip()
                text = (
                    f"Image file: {self.image_source_label}\n{body}"
                    if self.image_source_label and body else body
                )
                if not text:
                    continue
                evidence_ids = [line["evidence_id"] for line in lines]
                if self.image_evidence_id:
                    evidence_ids.insert(0, self.image_evidence_id)
                agreement_types = sorted({line["agreement_type"] for line in lines})
                context: dict[str, Any] = {
                    "container_kind": self.image_container_kind,
                    "quality_tier": quality_tier,
                    "agreement_types": agreement_types,
                    "bbox_coordinate_system": bbox_coordinate_system,
                    "reading_order_method": IMAGE_READING_ORDER_METHOD,
                    "row_band_count": len(rows),
                }
                if quality_tier == "provisional":
                    context["provisional_marker"] = PROVISIONAL_OCR_MARKER
                self.write(make_unit(
                    self.document_id,
                    "image_text_packet",
                    evidence_ids,
                    {
                        **self.image_locator,
                        "locator_text": (
                            f"container_kind={self.image_container_kind};"
                            f"quality_tier={quality_tier};"
                            f"bbox_coordinate_system={bbox_coordinate_system}"
                        ),
                    },
                    text,
                    self.generated_at,
                    context,
                ))
        self.image_lines = []
        self.image_evidence_id = None
        self.image_source_label = None
        self.image_container_kind = "standalone_image"
        self.image_locator = {"object_index": 1}

    def start_image(self, evidence: dict[str, Any]) -> None:
        content_ref = evidence.get("content", {}).get("content_ref")
        if not isinstance(content_ref, str) or not content_ref:
            return
        native = evidence.get("native_properties", {})
        visual_origin = native.get("visual_origin") if isinstance(native, dict) else None
        origin_kind = visual_origin.get("kind") if isinstance(visual_origin, dict) else None
        allowed = {
            "standalone_image", "pdf_page_image", "office_embedded_image",
            "notebook_embedded_image",
        }
        self.image_container_kind = origin_kind if origin_kind in allowed else "standalone_image"
        location = evidence.get("location", {})
        self.image_locator = {
            key: location[key] for key in SEARCH_LOCATOR_KEYS if key in location
        }
        if not self.image_locator:
            self.image_locator = {"object_index": 1}
        self.image_evidence_id = evidence["evidence_id"]
        source_name = content_ref.split("::", 1)[0].split("#", 1)[0]
        self.image_source_label = Path(source_name).name

    def add_ocr_line(self, evidence: dict[str, Any]) -> None:
        value = display_value(evidence)
        if value:
            native = evidence.get("native_properties", {})
            agreement_type = native.get("agreement_type")
            expected_quality_tier = OCR_QUALITY_BY_AGREEMENT.get(agreement_type)
            quality_tier = native.get("quality_tier")
            if expected_quality_tier is None:
                raise ValueError(
                    f"unsupported OCR agreement type: {agreement_type!r}"
                )
            if quality_tier != expected_quality_tier:
                raise ValueError(
                    "OCR quality tier disagrees with agreement type: "
                    f"{agreement_type!r} cannot be {quality_tier!r}"
                )
            marker = native.get("provisional_marker")
            bbox_coordinate_system = native.get("bbox_coordinate_system")
            if bbox_coordinate_system not in OCR_BBOX_COORDINATE_SYSTEMS:
                raise ValueError("OCR Evidence lacks a supported bbox coordinate system")
            if (
                quality_tier == "high"
                and bbox_coordinate_system
                != "source_orientation_1_top_left_normalized_1000"
            ):
                raise ValueError("high OCR Evidence requires the shared orientation-1 frame")
            if quality_tier == "provisional":
                if marker != PROVISIONAL_OCR_MARKER:
                    raise ValueError("provisional OCR Evidence lacks the canonical marker")
                value = "\n".join(
                    line if line.lstrip().startswith(PROVISIONAL_OCR_MARKER + " ")
                    else f"{PROVISIONAL_OCR_MARKER} {line}"
                    for line in value.splitlines()
                    if line.strip()
                )
            elif marker is not None or any(
                line.lstrip().startswith(PROVISIONAL_OCR_MARKER)
                for line in value.splitlines()
            ):
                raise ValueError("high OCR Evidence must not carry a provisional marker")
            self.image_lines.append({
                "evidence_id": evidence["evidence_id"],
                "text": value,
                "quality_tier": quality_tier,
                "agreement_type": agreement_type,
                "bbox_coordinate_system": bbox_coordinate_system,
                "geometry": evidence.get("geometry"),
            })

    def add_direct_table_row(self, evidence: dict[str, Any]) -> None:
        value = display_value(evidence)
        if not value:
            return
        location = evidence.get("location", {})
        locator = {key: location[key] for key in SEARCH_LOCATOR_KEYS if key in location}
        native = evidence.get("native_properties", {})
        headers = [str(item) for item in native.get("headers", [])]
        context = {
            "header_labels": headers,
            "header_method": "source_header_row",
            "is_header_candidate": False,
        } if headers else None
        self.write(make_unit(
            self.document_id,
            "table_row",
            [evidence["evidence_id"]],
            locator,
            value,
            self.generated_at,
            context,
        ))

    def add_direct_text(
        self,
        evidence: dict[str, Any],
        unit_type: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        value = display_value(evidence)
        if not value:
            return
        location = evidence.get("location", {})
        locator = {key: location[key] for key in SEARCH_LOCATOR_KEYS if key in location}
        evidence_type = evidence.get("evidence_type")
        locator_text = location.get("locator_text")
        if evidence_type in {"field", "defined_name", "filter", "data_validation", "metadata"} and locator_text:
            value = f"階層: {locator_text}\n値: {value}"
        if evidence_type == "style_span":
            style = canonical_json(evidence.get("style", {}))
            value = f"書式対象: {value}\n書式: {style}"
        method = evidence.get("provenance", {}).get("extraction_method")
        if method in PROVISIONAL_VISUAL_METHODS:
            native = evidence.get("native_properties", {})
            if (
                native.get("quality_tier") != "provisional"
                or native.get("provisional_marker") != PROVISIONAL_OCR_MARKER
            ):
                raise ValueError("provisional local visual Evidence lacks its quality contract")
            visible_lines: list[str] = []
            for line in value.splitlines():
                item = line.strip()
                if not item or item == PROVISIONAL_OCR_MARKER:
                    continue
                if not item.startswith(PROVISIONAL_OCR_MARKER + " "):
                    item = f"{PROVISIONAL_OCR_MARKER} {item}"
                visible_lines.append(item)
            if not visible_lines:
                raise ValueError("provisional local visual Evidence has no searchable text")
            value = "\n".join(visible_lines)
            visual_origin = native.get("visual_origin", {})
            origin_kind = (
                visual_origin.get("kind")
                if isinstance(visual_origin, dict) else None
            )
            allowed_kinds = {
                "standalone_image", "pdf_page_image", "office_embedded_image",
                "notebook_embedded_image",
            }
            provisional_context = {
                "container_kind": (
                    origin_kind if origin_kind in allowed_kinds else "standalone_image"
                ),
                "quality_tier": "provisional",
                "provisional_marker": PROVISIONAL_OCR_MARKER,
            }
            context = {**(context or {}), **provisional_context}
        self.write(make_unit(
            self.document_id,
            unit_type,
            [evidence["evidence_id"]],
            locator,
            value,
            self.generated_at,
            context,
        ))

    def consume(self, evidence: dict[str, Any]) -> None:
        evidence_type = evidence.get("evidence_type")
        if evidence_type != "ocr_line":
            self.flush_image()
        if evidence_type in {"heading", "paragraph"}:
            self.flush_row()
            self.add_paragraph(evidence)
        elif evidence_type == "table":
            self.flush_paragraphs()
            self.add_table(evidence)
        elif evidence_type == "table_cell":
            self.flush_paragraphs()
            self.add_cell(evidence)
        elif evidence_type == "formula":
            self.flush_paragraphs()
            self.add_formula(evidence)
        elif evidence_type == "table_row":
            self.flush_paragraphs()
            self.flush_row()
            self.add_direct_table_row(evidence)
        elif evidence_type == "shape":
            self.flush_row()
            self.add_shape(evidence)
        elif evidence_type == "page":
            self.flush_paragraphs()
            self.flush_row()
            self.flush_slide()
            self.add_page(evidence)
        elif evidence_type == "code_block":
            self.flush_paragraphs()
            self.flush_row()
            self.flush_slide()
            self.add_direct_text(evidence, "code_chunk")
        elif evidence_type == "notebook_cell":
            self.flush_paragraphs()
            self.flush_row()
            self.flush_slide()
            self.add_direct_text(evidence, "notebook_cell")
        elif evidence_type == "ocr_line":
            self.flush_paragraphs()
            self.flush_row()
            self.flush_slide()
            self.add_ocr_line(evidence)
        elif evidence_type == "image":
            self.start_image(evidence)
        elif (
            evidence_type == "chart"
            and evidence.get("provenance", {}).get("extraction_method")
            in VERIFIED_CHART_METHODS
        ):
            self.flush_paragraphs()
            self.flush_row()
            self.flush_slide()
            self.add_direct_text(evidence, "chart_summary", {"container_kind": "chart"})
        elif (
            evidence_type == "chart_series"
            and evidence.get("provenance", {}).get("extraction_method")
            in VERIFIED_CHART_METHODS
        ):
            self.flush_paragraphs()
            self.flush_row()
            self.flush_slide()
            self.add_direct_text(evidence, "chart_series", {"container_kind": "chart"})
        elif evidence_type in {
            "text_block", "field", "header", "footer", "speaker_note", "comment",
            "style_span", "defined_name", "filter", "data_validation",
        }:
            self.flush_paragraphs()
            self.flush_row()
            self.flush_slide()
            self.add_direct_text(evidence, "text_chunk")

    def finish(self) -> dict[str, int]:
        self.flush_paragraphs()
        self.flush_row()
        self.flush_slide()
        self.flush_image()
        return self.counts


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON: {path}: {exc}") from exc


def prepare_output(output: Path) -> None:
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)


def build(intermediate: Path | list[Path], output: Path, target_chars: int) -> dict[str, Any]:
    if target_chars < 100:
        raise ValueError("--target-chars must be at least 100")
    intermediates = [intermediate] if isinstance(intermediate, Path) else intermediate
    if not intermediates:
        raise ValueError("at least one intermediate input is required")
    loaded: list[tuple[Path, Path, dict[str, Any]]] = []
    for directory in intermediates:
        state_path = directory / "build-state.json"
        state = load_json(state_path)
        if state.get("build_status") not in {"complete", "complete_with_failures"}:
            raise ValueError(f"intermediate build must have reached a terminal state: {directory}")
        loaded.append((directory, state_path, state))
    run_at_values = {state["run_at"] for _, _, state in loaded}
    if len(run_at_values) != 1:
        raise ValueError("all intermediate inputs must use the same run_at timestamp")
    run_at = next(iter(run_at_values))
    prepare_output(output)
    final_path = output / "search_units.jsonl"
    temporary_path = output / ".search_units.jsonl.tmp"
    counts: dict[str, int] = {}
    document_counts: dict[str, int] = {}
    seen_document_ids: set[str] = set()
    with temporary_path.open("w", encoding="utf-8", newline="\n") as destination:
        for directory, _, state in loaded:
            for relative_path in state.get("input_paths", []):
                entry = state["entries"].get(relative_path)
                if entry is None:
                    raise ValueError(f"missing intermediate entry: {relative_path}")
                document_id = entry["document_id"]
                if document_id in seen_document_ids:
                    raise ValueError(f"duplicate document across intermediate inputs: {document_id}")
                seen_document_ids.add(document_id)
                shard = entry.get("shards", {}).get("evidence", {})
                evidence_path = directory / shard.get("relative_path", "")
                if not evidence_path.is_file() or digest_file(evidence_path) != shard.get("sha256"):
                    raise ValueError(f"invalid evidence shard: {evidence_path}")
                emitted = 0

                def emit(record: dict[str, Any]) -> None:
                    nonlocal emitted
                    destination.write(canonical_json(record) + "\n")
                    emitted += 1

                deriver = DocumentDeriver(document_id, run_at, emit, target_chars)
                with evidence_path.open(encoding="utf-8") as source:
                    for line_number, line in enumerate(source, 1):
                        if not line.strip():
                            continue
                        evidence = json.loads(line)
                        if evidence.get("document_id") != document_id:
                            raise ValueError(f"{evidence_path}:{line_number}: document_id mismatch")
                        deriver.consume(evidence)
                per_type = deriver.finish()
                document_counts[document_id] = emitted
                for unit_type, count in per_type.items():
                    counts[unit_type] = counts.get(unit_type, 0) + count
        destination.flush()
        os.fsync(destination.fileno())
    os.replace(temporary_path, final_path)
    source_state = {
        "intermediate_states": [
            {
                "sha256": digest_file(state_path),
                "extractor": state.get("extractor"),
                "extractor_version": state.get("extractor_version"),
            }
            for _, state_path, state in loaded
        ],
    }
    if len(loaded) == 1:
        _, only_state_path, only_state = loaded[0]
        source_state.update({
            "intermediate_state_sha256": digest_file(only_state_path),
            "extractor": only_state.get("extractor"),
            "extractor_version": only_state.get("extractor_version"),
        })
    result = {
        "state_version": "1",
        "build_status": "complete",
        "builder": BUILDER,
        "builder_version": BUILDER_VERSION,
        "generated_at": run_at,
        "deterministic": True,
        "target_chars": target_chars,
        "source": source_state,
        "output": {
            "relative_path": final_path.name,
            "sha256": digest_file(final_path),
            "size_bytes": final_path.stat().st_size,
            "record_count": sum(counts.values()),
        },
        "counts_by_type": dict(sorted(counts.items())),
        "counts_by_document": document_counts,
    }
    state_output = output / STATE_FILE
    temporary_state = output / f".{STATE_FILE}.tmp"
    with temporary_state.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(result) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_state, state_output)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intermediate", required=True, type=Path, nargs="+")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--target-chars", type=int, default=1200)
    args = parser.parse_args()
    print(canonical_json(build(
        [path.resolve() for path in args.intermediate],
        args.out.resolve(),
        args.target_chars,
    )))


if __name__ == "__main__":
    main()
