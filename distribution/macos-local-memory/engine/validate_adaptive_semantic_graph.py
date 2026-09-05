#!/usr/bin/env python3
"""Validate the Layer 1 bridge boundary before content-security classification."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import statistics
import tempfile
import unicodedata
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any


PROVISIONAL_OCR_MARKER = "[暫定読取]"
PROVISIONAL_TEXT_METHOD_TYPES = {
    "local_vlm_unlocated_transcript_provisional": frozenset({"text_block"}),
    "local_vlm_visual_observation_provisional": frozenset({
        "text_block", "visual_observation",
    }),
}
PROVISIONAL_TEXT_EVIDENCE_TYPES = frozenset(
    evidence_type
    for evidence_types in PROVISIONAL_TEXT_METHOD_TYPES.values()
    for evidence_type in evidence_types
)
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
IMAGE_PACKET_CONTAINER_KINDS = {
    "standalone_image",
    "pdf_page_image",
    "office_embedded_image",
    "notebook_embedded_image",
}
IMAGE_PACKET_LOCATOR_KEYS = {
    "page_number", "slide_number", "sheet_name", "cell", "table_index",
    "shape_id", "row_index", "paragraph_start", "paragraph_end",
    "notebook_cell_index", "code_line_start", "code_line_end", "locator_text",
    "source_member", "object_index", "image_object_index", "series_index",
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
PROJECTED_SEARCH_UNIT_TYPES = frozenset({
    "table_row", "image_text_packet", *NATIVE_CHART_UNIT_TYPES,
})
IMAGE_ROW_BAND_CENTER_TOLERANCE = 0.55
OCR_ENGINE_BY_PASS = {
    "apple_vision_primary": "apple_vision",
    "apple_vision_literal": "apple_vision",
    "apple_vision_fast_sparse": "apple_vision",
    "paddleocr_primary": "paddleocr",
    "tesseract_psm3": "tesseract",
    "tesseract_psm6": "tesseract",
    "tesseract_psm11": "tesseract",
}
ADAPTER_NAME = "layer1-to-local-memory-evidence-adapter"
ADAPTER_VERSION = "0.7.0"
SEARCH_UNIT_BUILDER = "search-unit-builder"
SEARCH_UNIT_BUILDER_VERSION = "0.6.0"
SCHEMA_VERSION = "0.1"
QUESTION_SHARD_VERSION = "question-evidence-shard-v1"
MAX_QUESTION_EVIDENCE_CHARS = 1_600
LINEAGE_VALIDATOR = "adaptive-semantic-lineage-validator"
LINEAGE_VALIDATOR_VERSION = "0.1.0"
LINEAGE_CONTRACT_VERSION = "search-unit-source-lineage-v1"
LINEAGE_RULE = "independent SearchUnit lineage reconstruction"
LINEAGE_RELATIONS_FILE = "semantic-lineage-relations.jsonl"
LINEAGE_VALIDATION_FILE = "semantic-lineage-validation.json"
NATIVE_STRUCTURAL_PRODUCERS = {
    ("intermediate-record-extractor", "0.7.0"),
    ("intermediate-record-extractor", "0.8.0"),
    ("intermediate-record-extractor", "0.10.1"),
    ("intermediate-record-extractor", "0.11.0"),
}
NATIVE_STRUCTURAL_RULE = "native containment"
NATIVE_SMARTART_CONNECTION_RULE = "native SmartArt srcId/destId connection"
NATIVE_SMARTART_MARKER = "SmartArt（ファイル内の明示構造）"
QUESTION_SHARD_KEYS = {
    "version",
    "source_projection_id",
    "source_projection_sha256",
    "source_text_sha256",
    "character_start",
    "character_end",
    "chunk_index",
    "chunk_count",
    "chunk_sha256",
    "observed_text_prefix",
}
LOCAL_LLM_RUNNERS = {"ollama_loopback_chat"}
IMAGE_QUALITY_KEYS = {
    "quality_tier",
    "agreement_types",
    "provisional_marker",
    "bbox_coordinate_system",
    "reading_order_method",
    "row_band_count",
}
RFC3339_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?"
    r"(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def is_rfc3339_timestamp(value: object) -> bool:
    if not isinstance(value, str) or RFC3339_PATTERN.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}_{hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()[:32]}"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def exact_text_chunks(value: str, *, max_chars: int) -> list[tuple[int, int, str]]:
    """Independent reconstruction of the adapter's exact shard boundaries."""
    chunks: list[tuple[int, int, str]] = []
    start = 0
    while start < len(value):
        hard_end = min(start + max_chars, len(value))
        end = hard_end
        if hard_end < len(value):
            newline = value.rfind("\n", start + max_chars // 2, hard_end)
            if newline >= 0:
                end = newline + 1
        fail(end <= start or end - start > max_chars, "expected_question_shard_boundary_invalid")
        chunks.append((start, end, value[start:end]))
        start = end
    fail("".join(text for _, _, text in chunks) != value, "expected_question_shard_reconstruction_failed")
    return chunks


def question_shard_id(metadata: dict[str, Any]) -> str:
    return stable_id(
        "ev",
        {
            "adapter": ADAPTER_NAME,
            "adapter_version": ADAPTER_VERSION,
            "question_shard": metadata,
        },
    )


def expected_question_shards(projected: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive exact answer-sized projections without trusting adapter output."""
    observed_text = projected.get("observed_text")
    fail(not isinstance(observed_text, str), "expected_projection_text_invalid")
    if len(observed_text) <= MAX_QUESTION_EVIDENCE_CHARS:
        return [projected]
    source_projection_id = projected.get("evidence_id")
    fail(
        not isinstance(source_projection_id, str) or not source_projection_id,
        "expected_question_shard_source_id_invalid",
    )
    source_projection_sha256 = sha256_text(canonical(projected))
    source_text_sha256 = sha256_text(observed_text)
    provisional = projected.get("quality_tier") == "provisional"
    visible_prefix = PROVISIONAL_OCR_MARKER + " "
    payload_limit = (
        MAX_QUESTION_EVIDENCE_CHARS - len(visible_prefix)
        if provisional else MAX_QUESTION_EVIDENCE_CHARS
    )
    chunks = exact_text_chunks(observed_text, max_chars=payload_limit)
    shards: list[dict[str, Any]] = []
    for chunk_index, (start, end, payload) in enumerate(chunks, 1):
        prefix = (
            ""
            if not provisional or payload.startswith(PROVISIONAL_OCR_MARKER)
            else visible_prefix
        )
        metadata = {
            "version": QUESTION_SHARD_VERSION,
            "source_projection_id": source_projection_id,
            "source_projection_sha256": source_projection_sha256,
            "source_text_sha256": source_text_sha256,
            "character_start": start,
            "character_end": end,
            "chunk_index": chunk_index,
            "chunk_count": len(chunks),
            "chunk_sha256": sha256_text(payload),
            "observed_text_prefix": prefix,
        }
        shard = copy.deepcopy(projected)
        shard["evidence_id"] = question_shard_id(metadata)
        shard["observed_text"] = prefix + payload
        shard["adapter"]["question_shard"] = metadata
        fail(
            len(shard["observed_text"]) > MAX_QUESTION_EVIDENCE_CHARS,
            "expected_question_shard_too_large",
        )
        fail(
            provisional and not shard["observed_text"].startswith(PROVISIONAL_OCR_MARKER),
            "expected_provisional_question_shard_unmarked",
        )
        shards.append(shard)
    return shards


def projected_text(content: dict[str, Any]) -> tuple[str, str]:
    if isinstance(content.get("raw_text"), str):
        return content["raw_text"], "raw_text"
    if "raw_value" in content:
        return canonical(content["raw_value"]), "canonical_raw_value"
    raise ValueError("layer_evidence_content_not_projectable")


def mark_provisional_text(text: str) -> str:
    return "\n".join(
        line if line.lstrip().startswith(PROVISIONAL_OCR_MARKER + " ")
        else f"{PROVISIONAL_OCR_MARKER} {line}"
        for line in text.splitlines()
        if line.strip() and line.strip() != PROVISIONAL_OCR_MARKER
    )


def expected_semantic_evidence(
    layer_documents: list[dict[str, Any]],
    layer_evidence: list[dict[str, Any]],
    search_units: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rebuild the adapter projection from immutable Layer 1 inputs.

    Validation must derive a projection's type and content from its source,
    never from output-side adapter labels that an altered record can rewrite.
    """
    documents = {item["document_id"]: item for item in layer_documents}
    layer_evidence_by_id = {
        item["evidence_id"]: item for item in layer_evidence
    }
    projected_by_document: dict[str, list[dict[str, Any]]] = defaultdict(list)
    projected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    emitted_ids: set[str] = set()

    for record in layer_evidence:
        evidence_id = record.get("evidence_id")
        document_id = record.get("document_id")
        fail(
            not isinstance(evidence_id, str)
            or evidence_id in seen_ids
            or document_id not in documents,
            "expected_layer_evidence_lineage_invalid",
        )
        seen_ids.add(evidence_id)
        record_content = record.get("content", {})
        if isinstance(record_content.get("content_ref"), str):
            continue
        observed_text, projection_method = projected_text(record_content)
        source_record_type = record.get("evidence_type")
        quality: tuple[str, list[str], str | None] | None = None
        provisional_text_quality: tuple[str, str] | None = None
        if source_record_type == "ocr_line":
            quality = layer_ocr_quality(record)
            validate_layer_visual_source_binding(
                record, layer_evidence_by_id, documents
            )
            if quality[0] == "provisional" and observed_text:
                observed_text = mark_provisional_text(observed_text)
        else:
            provisional_text_quality = layer_provisional_text_quality(record)
            if provisional_text_quality is not None:
                validate_layer_visual_source_binding(
                    record, layer_evidence_by_id, documents
                )
                observed_text = mark_provisional_text(observed_text)
                fail(
                    not observed_text,
                    "layer_provisional_vlm_text_empty_after_marker_normalization",
                )
        source = documents[document_id]["source"]
        item: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "evidence_id": evidence_id,
            "document_id": document_id,
            "ordinal": record.get("ordinal"),
            "locator": record.get("location", {}),
            "observed_text": observed_text,
            "source": {
                "relative_path": source["relative_path"],
                "sha256": source["sha256"],
            },
            "extraction_method": record.get("provenance", {}).get(
                "extraction_method", "unknown"
            ),
            "status": "observed",
            "adapter": {
                "name": ADAPTER_NAME,
                "version": ADAPTER_VERSION,
                "source_record_type": source_record_type,
                "text_projection": projection_method,
                "execution_policy": "never_execute",
            },
        }
        if quality is not None:
            tier, agreements, marker = quality
            item["quality_tier"] = tier
            item["agreement_types"] = agreements
            item["bbox_coordinate_system"] = record["native_properties"][
                "bbox_coordinate_system"
            ]
            if marker is not None:
                item["provisional_marker"] = marker
        elif provisional_text_quality is not None:
            tier, marker = provisional_text_quality
            item["quality_tier"] = tier
            item["provisional_marker"] = marker
        geometry = record.get("geometry")
        if isinstance(geometry, dict) and geometry:
            item["geometry"] = geometry
        for output_item in expected_question_shards(item):
            output_id = output_item["evidence_id"]
            fail(output_id in emitted_ids, "expected_semantic_evidence_id_collision")
            emitted_ids.add(output_id)
            projected_by_document[document_id].append(output_item)
            projected.append(output_item)

    for unit in search_units:
        unit_type = unit.get("unit_type")
        if unit_type not in PROJECTED_SEARCH_UNIT_TYPES:
            continue
        document_id = unit.get("document_id")
        fail(document_id not in documents, "expected_search_unit_document_missing")
        source_evidence_ids = unit.get("source_evidence_ids")
        fail(
            not isinstance(source_evidence_ids, list) or not source_evidence_ids,
            "expected_search_unit_sources_invalid",
        )
        quality = (
            layer_image_packet_quality(
                unit, layer_evidence_by_id, documents
            )
            if unit_type == "image_text_packet" else None
        )
        if unit_type in NATIVE_CHART_UNIT_TYPES:
            layer_native_chart_search_unit(unit, layer_evidence_by_id)
        evidence_id = stable_id("ev", {
            "adapter": ADAPTER_NAME,
            "adapter_version": ADAPTER_VERSION,
            "source_search_unit_id": unit["search_unit_id"],
            "document_id": document_id,
            "unit_type": unit_type,
            "source_evidence_ids": source_evidence_ids,
            "locator": unit["locator"],
            "text_sha256": unit["text"]["sha256"],
        })
        fail(evidence_id in seen_ids, "expected_search_projection_id_collision")
        seen_ids.add(evidence_id)
        source = documents[document_id]["source"]
        item = {
            "schema_version": SCHEMA_VERSION,
            "evidence_id": evidence_id,
            "document_id": document_id,
            "ordinal": len(projected_by_document[document_id]) + 1,
            "locator": unit["locator"],
            "observed_text": unit["text"]["search_text"],
            "source": {
                "relative_path": source["relative_path"],
                "sha256": source["sha256"],
            },
            "extraction_method": "verified_search_unit_projection",
            "status": "observed",
            "adapter": {
                "name": ADAPTER_NAME,
                "version": ADAPTER_VERSION,
                "source_record_type": "search_unit",
                "source_search_unit_id": unit["search_unit_id"],
                "source_evidence_ids": source_evidence_ids,
                "unit_type": unit_type,
                "text_projection": "search_unit_text",
                "execution_policy": "never_execute",
            },
        }
        if quality is not None:
            tier, agreements, marker = quality
            context = unit["context"]
            item["quality_tier"] = tier
            item["agreement_types"] = agreements
            item["bbox_coordinate_system"] = context["bbox_coordinate_system"]
            item["reading_order_method"] = context["reading_order_method"]
            item["row_band_count"] = context["row_band_count"]
            if marker is not None:
                item["provisional_marker"] = marker
        for output_item in expected_question_shards(item):
            output_id = output_item["evidence_id"]
            fail(output_id in emitted_ids, "expected_semantic_evidence_id_collision")
            emitted_ids.add(output_id)
            projected_by_document[document_id].append(output_item)
            projected.append(output_item)
    return projected


def validate_exact_projection(
    actual: list[dict[str, Any]], expected: list[dict[str, Any]]
) -> None:
    fail(actual != expected, "semantic_evidence_projection_mismatch")


def derive_question_sharding_state(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    source_types: dict[str, str] = {}
    shard_count = 0
    for item in evidence:
        adapter = item.get("adapter", {})
        metadata = adapter.get("question_shard")
        if metadata is None:
            continue
        fail(
            not isinstance(metadata, dict) or set(metadata) != QUESTION_SHARD_KEYS,
            "question_shard_metadata_invalid",
        )
        source_projection_id = metadata.get("source_projection_id")
        fail(
            not isinstance(source_projection_id, str) or not source_projection_id,
            "question_shard_source_projection_id_invalid",
        )
        source_type = str(adapter.get("source_record_type", "unknown"))
        if source_type == "search_unit":
            source_type = f"search_unit:{adapter.get('unit_type', 'unknown')}"
        previous = source_types.setdefault(source_projection_id, source_type)
        fail(previous != source_type, "question_shard_source_type_mismatch")
        shard_count += 1
    counts = Counter(source_types.values())
    return {
        "version": QUESTION_SHARD_VERSION,
        "max_observed_text_chars": MAX_QUESTION_EVIDENCE_CHARS,
        "source_projection_count": len(source_types),
        "shard_count": shard_count,
        "source_record_type_counts": dict(sorted(counts.items())),
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    return sha256_text(canonical(value))


def record_source_set_sha256(
    records: list[dict[str, Any]], id_key: str,
) -> str:
    indexed: dict[str, dict[str, Any]] = {}
    for record in records:
        record_id = record.get(id_key)
        fail(
            not isinstance(record_id, str)
            or not record_id
            or record_id in indexed,
            f"lineage_{id_key}_source_set_invalid",
        )
        indexed[record_id] = record
    return canonical_sha256([indexed[value] for value in sorted(indexed)])


def _expected_search_unit_id(unit: dict[str, Any]) -> str:
    provenance = unit.get("provenance", {})
    text = unit.get("text", {})
    return stable_id("su", {
        "document_id": unit.get("document_id"),
        "unit_type": unit.get("unit_type"),
        "source_evidence_ids": unit.get("source_evidence_ids"),
        "locator": unit.get("locator"),
        "text_sha256": text.get("sha256"),
        "builder": provenance.get("builder"),
        "builder_version": provenance.get("builder_version"),
    })


def _expected_search_unit_projection_id(unit: dict[str, Any]) -> str:
    return stable_id("ev", {
        "adapter": ADAPTER_NAME,
        "adapter_version": ADAPTER_VERSION,
        "source_search_unit_id": unit.get("search_unit_id"),
        "document_id": unit.get("document_id"),
        "unit_type": unit.get("unit_type"),
        "source_evidence_ids": unit.get("source_evidence_ids"),
        "locator": unit.get("locator"),
        "text_sha256": unit.get("text", {}).get("sha256"),
    })


def _layer_search_display_value(record: dict[str, Any]) -> str:
    """Reproduce the SearchUnit builder's direct Evidence display value."""
    content = record.get("content", {})
    fail(not isinstance(content, dict), "chart_search_unit_source_content_invalid")
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
            return canonical(value)
        return str(value).strip()
    return ""


def layer_native_chart_search_unit(
    unit: dict[str, Any],
    evidence_by_id: dict[str, dict[str, Any]],
) -> None:
    """Independently rebuild one native chart SearchUnit from its Evidence."""
    unit_type = unit.get("unit_type")
    expected_evidence_type = NATIVE_CHART_UNIT_TYPES.get(unit_type)
    fail(expected_evidence_type is None, "chart_search_unit_type_invalid")
    source_ids = unit.get("source_evidence_ids")
    fail(
        not isinstance(source_ids, list)
        or len(source_ids) != 1
        or not isinstance(source_ids[0], str),
        "chart_search_unit_sources_invalid",
    )
    source = evidence_by_id.get(source_ids[0])
    fail(source is None, "chart_search_unit_source_missing")
    fail(
        source.get("evidence_type") != expected_evidence_type,
        "chart_search_unit_source_type_mismatch",
    )
    source_provenance = source.get("provenance", {})
    fail(
        not isinstance(source_provenance, dict)
        or source_provenance.get("extraction_method")
        not in VERIFIED_CHART_SEARCH_METHODS,
        "chart_search_unit_source_method_invalid",
    )
    fail(
        source.get("document_id") != unit.get("document_id"),
        "chart_search_unit_source_document_mismatch",
    )
    location = source.get("location", {})
    fail(not isinstance(location, dict), "chart_search_unit_source_locator_invalid")
    expected_locator = {
        key: location[key]
        for key in IMAGE_PACKET_LOCATOR_KEYS if key in location
    }
    fail(
        unit.get("locator") != expected_locator,
        "chart_search_unit_locator_mismatch",
    )
    expected_text = _layer_search_display_value(source)
    fail(not expected_text, "chart_search_unit_source_text_empty")
    fail(
        unit.get("text") != {
            "search_text": expected_text,
            "sha256": sha256_text(expected_text),
            "char_count": len(expected_text),
        },
        "chart_search_unit_text_mismatch",
    )
    fail(
        unit.get("context") != {"container_kind": "chart"},
        "chart_search_unit_context_mismatch",
    )
    fail(
        unit.get("search_unit_id") != _expected_search_unit_id(unit),
        "chart_search_unit_id_unstable",
    )


def _stable_lineage_relation_id(
    from_ref: dict[str, str], to_ref: dict[str, str],
) -> str:
    return stable_id("rel", {
        "class": "lineage",
        "type": "derived_from",
        "from": from_ref,
        "to": to_ref,
        "generator": LINEAGE_VALIDATOR,
        "generator_version": LINEAGE_VALIDATOR_VERSION,
    })


def _native_structural_relation(
    *,
    relation_type: str,
    from_ref: dict[str, str],
    to_ref: dict[str, str],
    extractor: str,
    extractor_version: str,
    generated_at: str,
) -> dict[str, Any]:
    identity = {
        "class": "structural",
        "type": relation_type,
        "from": from_ref,
        "to": to_ref,
        "generator": extractor,
        "generator_version": extractor_version,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "relation",
        "relation_id": stable_id("rel", identity),
        "relation_class": "structural",
        "relation_type": relation_type,
        "from_ref": from_ref,
        "to_ref": to_ref,
        "properties": {},
        "supporting_evidence_ids": [],
        "provenance": {
            "generated_by": extractor,
            "generator_version": extractor_version,
            "generated_at": generated_at,
            "deterministic": True,
            "confidence": 1.0,
            "rule_or_model": NATIVE_STRUCTURAL_RULE,
            "warnings": [],
        },
        "status": "verified",
    }


def _native_smartart_connection_relation(
    *,
    from_ref: dict[str, str],
    to_ref: dict[str, str],
    properties: dict[str, Any],
    supporting_evidence_ids: list[str],
    extractor: str,
    extractor_version: str,
    generated_at: str,
) -> dict[str, Any]:
    identity = {
        "class": "structural",
        "type": "diagram_connection",
        "from": from_ref,
        "to": to_ref,
        "generator": extractor,
        "generator_version": extractor_version,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "relation",
        "relation_id": stable_id("rel", identity),
        "relation_class": "structural",
        "relation_type": "diagram_connection",
        "from_ref": from_ref,
        "to_ref": to_ref,
        "properties": properties,
        "supporting_evidence_ids": supporting_evidence_ids,
        "provenance": {
            "generated_by": extractor,
            "generator_version": extractor_version,
            "generated_at": generated_at,
            "deterministic": True,
            "confidence": 1.0,
            "rule_or_model": NATIVE_SMARTART_CONNECTION_RULE,
            "warnings": [],
        },
        "status": "verified",
    }


def _native_parser_provenance(
    record: dict[str, Any],
    *,
    extractor: str,
    extractor_version: str,
    generated_at: str,
    error: str,
) -> None:
    fail(
        record.get("provenance") != {
            "extraction_method": "native_parser",
            "extractor": extractor,
            "extractor_version": extractor_version,
            "extracted_at": generated_at,
            "deterministic": True,
            "confidence": 1.0,
            "warnings": [],
        },
        error,
    )


def _safe_smartart_member(value: object, prefix: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    member = PurePosixPath(value)
    return (
        not member.is_absolute()
        and all(part not in {"", ".", ".."} for part in member.parts)
        and value.startswith(prefix)
        and value.casefold().endswith(".xml")
    )


def _derive_native_smartart_relations(
    *,
    documents: dict[str, dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
    extractor: str,
    extractor_version: str,
    generated_at: str,
) -> list[dict[str, Any]]:
    """Rebuild explicit SmartArt edges from their native Evidence records.

    The relation is intentionally not inferred from visible text.  It is
    accepted only when every endpoint and raw connection is bound to one
    native SmartArt data part on one slide, and its supporting Evidence can be
    reproduced without consulting the claimed Relation record.
    """
    children_by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    connection_parents: set[str] = set()
    for record in evidence_by_id.values():
        parent_id = record.get("parent_evidence_id")
        if isinstance(parent_id, str):
            children_by_parent[parent_id].append(record)
        native = record.get("native_properties", {})
        if isinstance(native, dict) and "smartart_connection" in native:
            fail(
                not isinstance(parent_id, str),
                f"native_smartart_connection_parent_invalid:{record.get('evidence_id')}",
            )
            connection_parents.add(parent_id)

    expected: list[dict[str, Any]] = []
    for parent_id in sorted(connection_parents):
        diagram = evidence_by_id.get(parent_id)
        fail(diagram is None, f"native_smartart_diagram_missing:{parent_id}")
        diagram_id = str(diagram.get("evidence_id"))
        document_id = diagram.get("document_id")
        document = documents.get(document_id) if isinstance(document_id, str) else None
        fail(
            document is None
            or PurePosixPath(
                str(document.get("source", {}).get("relative_path", ""))
            ).suffix.casefold() != ".pptx",
            f"native_smartart_document_invalid:{diagram_id}",
        )
        fail(
            diagram.get("evidence_type") != "shape"
            or diagram.get("parent_evidence_id") is not None
            or diagram.get("content", {}).get("raw_text") != NATIVE_SMARTART_MARKER,
            f"native_smartart_diagram_invalid:{diagram_id}",
        )
        _native_parser_provenance(
            diagram,
            extractor=extractor,
            extractor_version=extractor_version,
            generated_at=generated_at,
            error=f"native_smartart_diagram_provenance_invalid:{diagram_id}",
        )
        diagram_location = diagram.get("location", {})
        diagram_native = diagram.get("native_properties", {})
        relationship = (
            diagram_native.get("ooxml_relationship")
            if isinstance(diagram_native, dict) else None
        )
        fail(
            not isinstance(diagram_location, dict)
            or not isinstance(diagram_native, dict)
            or set(diagram_native) != {
                "ooxml_part", "xml_sha256", "point_count",
                "connection_count", "ooxml_relationship",
            }
            or not isinstance(relationship, dict)
            or set(relationship) != {
                "source_part", "relationship_id", "relationship_occurrence",
            },
            f"native_smartart_diagram_metadata_invalid:{diagram_id}",
        )
        slide_number = diagram_location.get("slide_number")
        source_member = diagram_location.get("source_member")
        diagram_index = diagram_location.get("object_index")
        relationship_id = relationship.get("relationship_id")
        relationship_occurrence = relationship.get("relationship_occurrence")
        source_part = relationship.get("source_part")
        point_count = diagram_native.get("point_count")
        connection_count = diagram_native.get("connection_count")
        xml_sha256 = diagram_native.get("xml_sha256")
        expected_diagram_locator = (
            f"slide={slide_number};smartart={source_member};"
            f"relationship={relationship_id};occurrence={relationship_occurrence}"
        )
        fail(
            isinstance(slide_number, bool)
            or not isinstance(slide_number, int)
            or slide_number < 1
            or isinstance(diagram_index, bool)
            or not isinstance(diagram_index, int)
            or diagram_index < 1
            or diagram.get("ordinal") != diagram_index
            or not _safe_smartart_member(source_member, "ppt/diagrams/")
            or diagram_native.get("ooxml_part") != source_member
            or not isinstance(xml_sha256, str)
            or SHA256_PATTERN.fullmatch(xml_sha256) is None
            or not _safe_smartart_member(source_part, "ppt/slides/")
            or not isinstance(relationship_id, str)
            or not relationship_id
            or isinstance(relationship_occurrence, bool)
            or not isinstance(relationship_occurrence, int)
            or relationship_occurrence < 1
            or isinstance(point_count, bool)
            or not isinstance(point_count, int)
            or point_count < 1
            or isinstance(connection_count, bool)
            or not isinstance(connection_count, int)
            or connection_count < 1
            or diagram_location != {
                "slide_number": slide_number,
                "source_member": source_member,
                "object_index": diagram_index,
                "locator_text": expected_diagram_locator,
            },
            f"native_smartart_diagram_binding_invalid:{diagram_id}",
        )

        points_by_model_id: dict[str, dict[str, Any]] = {}
        point_ordinals: set[int] = set()
        connections: list[dict[str, Any]] = []
        connection_ordinals: set[int] = set()
        for child in children_by_parent.get(parent_id, []):
            native = child.get("native_properties", {})
            if not isinstance(native, dict):
                continue
            child_id = str(child.get("evidence_id"))
            if "smartart_model_id" in native:
                model_id = native.get("smartart_model_id")
                ordinal = child.get("ordinal")
                expected_location = {
                    "slide_number": slide_number,
                    "source_member": source_member,
                    "object_index": ordinal,
                    "object_id": model_id,
                    "locator_text": (
                        f"{expected_diagram_locator};point="
                        f"{urllib.parse.quote(str(model_id), safe='-._~')}"
                    ),
                }
                fail(
                    child.get("document_id") != document_id
                    or child.get("evidence_type") != "text_block"
                    or set(native) != {
                        "smartart_model_id", "smartart_point_type",
                        "ooxml_part", "xml_sha256",
                    }
                    or not isinstance(model_id, str)
                    or not model_id
                    or model_id in points_by_model_id
                    or isinstance(ordinal, bool)
                    or not isinstance(ordinal, int)
                    or ordinal < 1
                    or ordinal in point_ordinals
                    or ordinal > point_count
                    or native.get("ooxml_part") != source_member
                    or native.get("xml_sha256") != xml_sha256
                    or child.get("location") != expected_location
                    or not isinstance(child.get("content", {}).get("raw_text"), str)
                    or not child["content"]["raw_text"],
                    f"native_smartart_point_invalid:{child_id}",
                )
                _native_parser_provenance(
                    child,
                    extractor=extractor,
                    extractor_version=extractor_version,
                    generated_at=generated_at,
                    error=f"native_smartart_point_provenance_invalid:{child_id}",
                )
                point_ordinals.add(ordinal)
                points_by_model_id[model_id] = child
            if "smartart_connection" in native:
                raw_connection = native.get("smartart_connection")
                ordinal = child.get("ordinal")
                fail(
                    child.get("document_id") != document_id
                    or child.get("evidence_type") != "text_block"
                    or set(native) != {
                        "smartart_connection", "ooxml_part", "xml_sha256",
                        "semantic_interpretation_performed",
                    }
                    or not isinstance(raw_connection, dict)
                    or not set(raw_connection) <= {
                        "modelId", "srcId", "destId", "type",
                    }
                    or any(not isinstance(value, str) for value in raw_connection.values())
                    or native.get("semantic_interpretation_performed") is not False
                    or native.get("ooxml_part") != source_member
                    or native.get("xml_sha256") != xml_sha256
                    or isinstance(ordinal, bool)
                    or not isinstance(ordinal, int)
                    or ordinal < 1
                    or ordinal in connection_ordinals
                    or ordinal > connection_count
                    or child.get("location") != {
                        "slide_number": slide_number,
                        "source_member": source_member,
                        "object_index": ordinal,
                        "locator_text": f"{expected_diagram_locator};connection={ordinal}",
                    },
                    f"native_smartart_connection_invalid:{child_id}",
                )
                _native_parser_provenance(
                    child,
                    extractor=extractor,
                    extractor_version=extractor_version,
                    generated_at=generated_at,
                    error=f"native_smartart_connection_provenance_invalid:{child_id}",
                )
                connection_ordinals.add(ordinal)
                connections.append(child)

        fail(
            not connections,
            f"native_smartart_connection_set_empty:{diagram_id}",
        )
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for connection in sorted(connections, key=lambda item: item["ordinal"]):
            connection_id = str(connection.get("evidence_id"))
            raw_connection = connection["native_properties"]["smartart_connection"]
            source_id = raw_connection.get("srcId")
            target_id = raw_connection.get("destId")
            source = points_by_model_id.get(source_id)
            target = points_by_model_id.get(target_id)
            connection_type = str(raw_connection.get("type") or "unspecified")
            fail(
                source is None
                or target is None
                or connection.get("content", {}).get("raw_text") != (
                    "SmartArtの明示接続: "
                    f"{source['content']['raw_text']} -> "
                    f"{target['content']['raw_text']} "
                    f"(原形式type={connection_type})"
                ),
                f"native_smartart_connection_endpoint_invalid:{connection_id}",
            )
            grouped[(source_id, target_id)].append(connection)

        for (source_id, target_id), grouped_connections in sorted(grouped.items()):
            source = points_by_model_id[source_id]
            target = points_by_model_id[target_id]
            expected.append(_native_smartart_connection_relation(
                from_ref={
                    "record_type": "evidence",
                    "record_id": source["evidence_id"],
                },
                to_ref={
                    "record_type": "evidence",
                    "record_id": target["evidence_id"],
                },
                properties={
                    "raw_connections": [
                        item["native_properties"]["smartart_connection"]
                        for item in grouped_connections
                    ],
                    "source_member": source_member,
                    "slide_number": slide_number,
                    "semantic_interpretation_performed": False,
                },
                supporting_evidence_ids=[
                    diagram_id,
                    *[item["evidence_id"] for item in grouped_connections],
                ],
                extractor=extractor,
                extractor_version=extractor_version,
                generated_at=generated_at,
            ))
    return expected


def derive_native_structural_relations(
    layer_documents: list[dict[str, Any]],
    layer_evidence: list[dict[str, Any]],
    intermediate_state: dict[str, Any],
) -> list[dict[str, Any]]:
    """Rebuild native containment from Layer 1 records, not Relation claims."""
    extractor = intermediate_state.get("extractor")
    extractor_version = intermediate_state.get("extractor_version")
    generated_at = intermediate_state.get("run_at")
    fail(
        (extractor, extractor_version) not in NATIVE_STRUCTURAL_PRODUCERS
        or not is_rfc3339_timestamp(generated_at),
        "native_structural_producer_invalid",
    )

    documents: dict[str, dict[str, Any]] = {}
    for record in layer_documents:
        document_id = record.get("document_id")
        fail(
            not isinstance(document_id, str)
            or not document_id
            or document_id in documents,
            "native_structural_document_id_invalid",
        )
        extraction = record.get("extraction", {})
        fail(
            not isinstance(extraction, dict)
            or extraction.get("extracted_at") != generated_at,
            f"native_structural_document_run_mismatch:{document_id}",
        )
        documents[document_id] = record

    evidence_by_id: dict[str, dict[str, Any]] = {}
    for record in layer_evidence:
        evidence_id = record.get("evidence_id")
        fail(
            not isinstance(evidence_id, str)
            or not evidence_id
            or evidence_id in evidence_by_id,
            "native_structural_evidence_id_invalid",
        )
        provenance = record.get("provenance", {})
        fail(
            not isinstance(provenance, dict)
            or provenance.get("extractor") != extractor
            or provenance.get("extractor_version") != extractor_version
            or provenance.get("extracted_at") != generated_at,
            f"native_structural_evidence_run_mismatch:{evidence_id}",
        )
        evidence_by_id[evidence_id] = record

    for evidence_id in sorted(evidence_by_id):
        ancestry: set[str] = set()
        current_id: str | None = evidence_id
        while current_id is not None:
            fail(
                current_id in ancestry,
                f"native_structural_parent_cycle:{evidence_id}",
            )
            ancestry.add(current_id)
            parent_id = evidence_by_id[current_id].get("parent_evidence_id")
            fail(
                parent_id is not None
                and (
                    not isinstance(parent_id, str)
                    or parent_id not in evidence_by_id
                ),
                f"native_structural_parent_invalid:{current_id}",
            )
            current_id = parent_id

    relations: dict[str, dict[str, Any]] = {}

    def add(
        relation_type: str,
        from_ref: dict[str, str],
        to_ref: dict[str, str],
    ) -> None:
        relation = _native_structural_relation(
            relation_type=relation_type,
            from_ref=from_ref,
            to_ref=to_ref,
            extractor=str(extractor),
            extractor_version=str(extractor_version),
            generated_at=generated_at,
        )
        relation_id = relation["relation_id"]
        fail(
            relation_id in relations,
            f"native_structural_relation_duplicate:{relation_id}",
        )
        relations[relation_id] = relation

    for evidence_id, record in sorted(evidence_by_id.items()):
        document_id = record.get("document_id")
        fail(
            not isinstance(document_id, str) or document_id not in documents,
            f"native_structural_document_missing:{evidence_id}",
        )
        parent_id = record.get("parent_evidence_id")
        if parent_id is None:
            from_ref = {"record_type": "document", "record_id": document_id}
        else:
            fail(
                not isinstance(parent_id, str)
                or parent_id == evidence_id
                or parent_id not in evidence_by_id
                or evidence_by_id[parent_id].get("document_id") != document_id,
                f"native_structural_parent_invalid:{evidence_id}",
            )
            from_ref = {"record_type": "evidence", "record_id": parent_id}
        to_ref = {"record_type": "evidence", "record_id": evidence_id}
        add("contains", from_ref, to_ref)

        native_properties = record.get("native_properties", {})
        fail(
            not isinstance(native_properties, dict),
            f"native_structural_properties_invalid:{evidence_id}",
        )
        heading_id = native_properties.get("preceding_heading_evidence_id")
        if heading_id is None:
            continue
        fail(
            not isinstance(heading_id, str)
            or heading_id == evidence_id
            or heading_id not in evidence_by_id
            or evidence_by_id[heading_id].get("document_id") != document_id,
            f"native_structural_heading_invalid:{evidence_id}",
        )
        heading = evidence_by_id[heading_id]
        fail(
            record.get("evidence_type") != "table"
            or heading.get("evidence_type") != "heading"
            or native_properties.get("preceding_heading_text")
            != heading.get("content", {}).get("raw_text"),
            f"native_structural_heading_binding_mismatch:{evidence_id}",
        )
        add(
            "section_contains",
            {"record_type": "evidence", "record_id": heading_id},
            to_ref,
        )

    for relation in _derive_native_smartart_relations(
        documents=documents,
        evidence_by_id=evidence_by_id,
        extractor=str(extractor),
        extractor_version=str(extractor_version),
        generated_at=generated_at,
    ):
        relation_id = relation["relation_id"]
        fail(
            relation_id in relations,
            f"native_structural_relation_duplicate:{relation_id}",
        )
        relations[relation_id] = relation

    return [relations[relation_id] for relation_id in sorted(relations)]


def _assert_acyclic_lineage(relations: list[dict[str, Any]]) -> None:
    adjacency: dict[str, list[str]] = defaultdict(list)
    for relation in relations:
        source = relation["from_ref"]["record_id"]
        target = relation["to_ref"]["record_id"]
        fail(source == target, "lineage_self_loop")
        adjacency[source].append(target)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        fail(node_id in visiting, "lineage_cycle")
        if node_id in visited:
            return
        visiting.add(node_id)
        for target_id in adjacency.get(node_id, []):
            visit(target_id)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in sorted(adjacency):
        visit(node_id)


def derive_verified_lineage_relations(
    search_units: list[dict[str, Any]],
    semantic_evidence: list[dict[str, Any]],
    layer_evidence: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Independently derive complete SearchUnit lineage over final Evidence.

    A derived Evidence fan-in is promoted only when every source endpoint is a
    final semantic Evidence in the same document. Known binary omissions and
    projection shards are held explicitly; unexplained gaps fail closed.
    """
    semantic_by_id: dict[str, dict[str, Any]] = {}
    derived_by_unit: dict[str, list[dict[str, Any]]] = defaultdict(list)
    source_projection_shards: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in semantic_evidence:
        evidence_id = record.get("evidence_id")
        fail(
            not isinstance(evidence_id, str)
            or not evidence_id
            or evidence_id in semantic_by_id,
            "lineage_semantic_evidence_id_invalid",
        )
        semantic_by_id[evidence_id] = record
        adapter = record.get("adapter", {})
        if isinstance(adapter, dict):
            source_unit_id = adapter.get("source_search_unit_id")
            if isinstance(source_unit_id, str) and source_unit_id:
                derived_by_unit[source_unit_id].append(record)
            shard = adapter.get("question_shard")
            if isinstance(shard, dict):
                source_projection_id = shard.get("source_projection_id")
                if isinstance(source_projection_id, str) and source_projection_id:
                    source_projection_shards[source_projection_id].append(record)

    layer_by_id: dict[str, dict[str, Any]] = {}
    for record in layer_evidence:
        evidence_id = record.get("evidence_id")
        fail(
            not isinstance(evidence_id, str)
            or not evidence_id
            or evidence_id in layer_by_id,
            "lineage_layer_evidence_id_invalid",
        )
        layer_by_id[evidence_id] = record

    relation_by_id: dict[str, dict[str, Any]] = {}
    held: list[dict[str, Any]] = []
    eligible_derived_count = 0
    source_reference_count = 0
    held_source_reference_count = 0
    seen_units: set[str] = set()
    projected_units = [
        unit for unit in search_units
        if unit.get("unit_type") in PROJECTED_SEARCH_UNIT_TYPES
    ]
    for unit in projected_units:
        search_unit_id = unit.get("search_unit_id")
        fail(
            not isinstance(search_unit_id, str)
            or not search_unit_id
            or search_unit_id in seen_units,
            "lineage_search_unit_id_invalid",
        )
        seen_units.add(search_unit_id)
        fail(
            search_unit_id != _expected_search_unit_id(unit),
            f"lineage_search_unit_id_unstable:{search_unit_id}",
        )
        document_id = unit.get("document_id")
        source_ids = unit.get("source_evidence_ids")
        fail(
            not isinstance(document_id, str)
            or not document_id
            or not isinstance(source_ids, list)
            or not source_ids
            or any(not isinstance(value, str) or not value for value in source_ids)
            or len(source_ids) != len(set(source_ids)),
            f"lineage_search_unit_sources_invalid:{search_unit_id}",
        )
        source_reference_count += len(source_ids)
        provenance = unit.get("provenance", {})
        generated_at = provenance.get("generated_at")
        fail(
            provenance.get("builder") != "search-unit-builder"
            or provenance.get("builder_version") != SEARCH_UNIT_BUILDER_VERSION
            or provenance.get("deterministic") is not True
            or not is_rfc3339_timestamp(generated_at),
            f"lineage_search_unit_provenance_invalid:{search_unit_id}",
        )
        if unit.get("unit_type") in NATIVE_CHART_UNIT_TYPES:
            layer_native_chart_search_unit(unit, layer_by_id)

        expected_projection_id = _expected_search_unit_projection_id(unit)
        derived_records = sorted(
            derived_by_unit.get(search_unit_id, []),
            key=lambda item: str(item.get("evidence_id", "")),
        )
        fail(
            not derived_records,
            f"lineage_derived_projection_missing:{search_unit_id}",
        )
        for derived in derived_records:
            adapter = derived.get("adapter", {})
            fail(
                not isinstance(adapter, dict)
                or adapter.get("name") != ADAPTER_NAME
                or adapter.get("version") != ADAPTER_VERSION
                or adapter.get("source_record_type") != "search_unit"
                or adapter.get("source_search_unit_id") != search_unit_id
                or adapter.get("source_evidence_ids") != source_ids
                or adapter.get("unit_type") != unit.get("unit_type"),
                f"lineage_derived_projection_binding_invalid:{search_unit_id}",
            )

        derived_is_sharded = any(
            isinstance(item.get("adapter", {}).get("question_shard"), dict)
            for item in derived_records
        )
        if derived_is_sharded:
            fail(
                any(
                    item["adapter"]["question_shard"].get("source_projection_id")
                    != expected_projection_id
                    for item in derived_records
                ),
                f"lineage_derived_shard_anchor_invalid:{search_unit_id}",
            )
        else:
            fail(
                len(derived_records) != 1
                or derived_records[0].get("evidence_id") != expected_projection_id,
                f"lineage_derived_projection_id_invalid:{search_unit_id}",
            )

        hold_reasons: set[str] = set()
        unresolved_source_ids: list[str] = []
        if derived_is_sharded:
            hold_reasons.add("requires_projection_anchor")
        resolved_sources: list[dict[str, Any]] = []
        for source_id in source_ids:
            layer_source = layer_by_id.get(source_id)
            fail(
                layer_source is None,
                f"lineage_source_unexplained_missing:{search_unit_id}:{source_id}",
            )
            fail(
                layer_source.get("document_id") != document_id,
                f"lineage_source_cross_document:{search_unit_id}:{source_id}",
            )
            semantic_source = semantic_by_id.get(source_id)
            if semantic_source is not None:
                fail(
                    semantic_source.get("document_id") != document_id,
                    f"lineage_semantic_source_cross_document:{search_unit_id}:{source_id}",
                )
                resolved_sources.append(semantic_source)
                continue
            content = layer_source.get("content", {})
            if isinstance(content, dict) and isinstance(content.get("content_ref"), str):
                hold_reasons.add("non_projected_binary_source")
                unresolved_source_ids.append(source_id)
            elif source_projection_shards.get(source_id):
                hold_reasons.add("requires_projection_anchor")
                unresolved_source_ids.append(source_id)
            else:
                raise ValueError(
                    f"lineage_source_unexplained_missing:{search_unit_id}:{source_id}"
                )

        if hold_reasons:
            held_source_reference_count += len(source_ids)
            held.append({
                "source_search_unit_id": search_unit_id,
                "derived_evidence_ids": [
                    str(item["evidence_id"]) for item in derived_records
                ],
                "reasons": sorted(hold_reasons),
                "unresolved_source_evidence_ids": sorted(unresolved_source_ids),
            })
            continue

        fail(
            len(resolved_sources) != len(source_ids),
            f"lineage_partial_fan_in:{search_unit_id}",
        )
        eligible_derived_count += 1
        derived = derived_records[0]
        derived_id = str(derived["evidence_id"])
        fan_in_sha256 = canonical_sha256(source_ids)
        for ordinal, source in enumerate(resolved_sources, 1):
            source_id = str(source["evidence_id"])
            from_ref = {"record_type": "evidence", "record_id": derived_id}
            to_ref = {"record_type": "evidence", "record_id": source_id}
            relation_id = _stable_lineage_relation_id(from_ref, to_ref)
            fail(
                relation_id in relation_by_id,
                f"lineage_relation_id_duplicate:{relation_id}",
            )
            relation = {
                "schema_version": SCHEMA_VERSION,
                "record_type": "relation",
                "relation_id": relation_id,
                "relation_class": "lineage",
                "relation_type": "derived_from",
                "from_ref": from_ref,
                "to_ref": to_ref,
                "properties": {
                    "lineage_contract": LINEAGE_CONTRACT_VERSION,
                    "source_search_unit_id": search_unit_id,
                    "source_search_unit_sha256": canonical_sha256(unit),
                    "source_evidence_ordinal": ordinal,
                    "source_evidence_count": len(source_ids),
                    "fan_in_sha256": fan_in_sha256,
                    "derived_evidence_sha256": canonical_sha256(derived),
                },
                "supporting_evidence_ids": [source_id],
                "provenance": {
                    "generated_by": LINEAGE_VALIDATOR,
                    "generator_version": LINEAGE_VALIDATOR_VERSION,
                    "generated_at": generated_at,
                    "deterministic": True,
                    "confidence": 1.0,
                    "rule_or_model": LINEAGE_RULE,
                    "warnings": [],
                },
                "status": "verified",
            }
            relation_by_id[relation_id] = relation

    relations = [relation_by_id[value] for value in sorted(relation_by_id)]
    _assert_acyclic_lineage(relations)
    coverage = {
        "projected_search_unit_count": len(projected_units),
        "source_reference_count": source_reference_count,
        "eligible_derived_count": eligible_derived_count,
        "verified_derived_count": eligible_derived_count,
        "verified_relation_count": len(relations),
        "held_derived_count": len(held),
        "held_source_reference_count": held_source_reference_count,
        "held": held,
    }
    return relations, coverage


def _clear_lineage_artifacts(output: Path) -> None:
    for name in (LINEAGE_RELATIONS_FILE, LINEAGE_VALIDATION_FILE):
        (output / name).unlink(missing_ok=True)


def _publish_lineage_artifacts(
    output: Path,
    relations: list[dict[str, Any]],
    validation_state: dict[str, Any],
) -> None:
    relation_bytes = "".join(
        canonical(record) + "\n" for record in relations
    ).encode("utf-8")
    state_bytes = (canonical(validation_state) + "\n").encode("utf-8")
    destinations = (
        (output / LINEAGE_RELATIONS_FILE, relation_bytes),
        (output / LINEAGE_VALIDATION_FILE, state_bytes),
    )
    temporary_paths: list[Path] = []
    try:
        for destination, payload in destinations:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".tmp", dir=output,
            )
            temporary = Path(temporary_name)
            temporary_paths.append(temporary)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        for temporary, (destination, _payload) in zip(
            temporary_paths, destinations,
        ):
            os.replace(temporary, destination)
        temporary_paths.clear()
    except BaseException:
        _clear_lineage_artifacts(output)
        raise
    finally:
        for temporary in temporary_paths:
            temporary.unlink(missing_ok=True)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def derive_llm_extraction(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Independently derive extraction-model use from Layer 1 provenance."""
    methods: Counter[str] = Counter()
    for record in records:
        provenance = record.get("provenance", {})
        native = record.get("native_properties", {})
        method = provenance.get("extraction_method") if isinstance(provenance, dict) else None
        runner = native.get("runner") if isinstance(native, dict) else None
        uses_llm = (
            isinstance(method, str) and method.startswith("local_vlm_")
        ) or runner in LOCAL_LLM_RUNNERS
        if uses_llm:
            methods[method if isinstance(method, str) and method else "unknown"] += 1
    return {
        "used": bool(methods),
        "evidence_count": sum(methods.values()),
        "methods": dict(sorted(methods.items())),
    }


def fail(condition: bool, message: str) -> None:
    if condition:
        raise ValueError(message)


def content_lines(text: str, *, packet: bool) -> list[str]:
    return [
        line for line in text.splitlines()
        if line.strip() and not (packet and line.startswith("Image file: "))
    ]


def validate_quality_projection(
    item: dict[str, Any],
    *,
    expected_tier: str,
    expected_agreements: list[str],
    expected_marker: str | None,
    packet: bool,
) -> None:
    """Reject tier promotion, marker loss, and agreement rewriting."""
    fail(item.get("quality_tier") != expected_tier, "image_quality_tier_lineage_mismatch")
    fail(
        item.get("agreement_types") != expected_agreements,
        "image_agreement_lineage_mismatch",
    )
    marker_present = "provisional_marker" in item
    if expected_marker is None:
        fail(marker_present, "high_image_projection_has_provisional_marker")
    else:
        fail(
            not marker_present or item.get("provisional_marker") != expected_marker,
            "provisional_image_projection_marker_mismatch",
        )
    lines = content_lines(str(item.get("observed_text", "")), packet=packet)
    fail(not lines, "image_projection_text_empty")
    if expected_tier == "high":
        fail(
            any(line.lstrip().startswith(PROVISIONAL_OCR_MARKER) for line in lines),
            "high_image_projection_text_marked_provisional",
        )
    else:
        fail(
            any(
                not line.lstrip().startswith(PROVISIONAL_OCR_MARKER + " ")
                for line in lines
            ),
            "provisional_image_projection_text_unmarked",
        )


def validate_provisional_text_projection(item: dict[str, Any]) -> None:
    """Reject marker loss or accidental OCR-specific promotion metadata."""
    fail(
        item.get("quality_tier") != "provisional",
        "provisional_vlm_projection_quality_mismatch",
    )
    fail(
        item.get("provisional_marker") != PROVISIONAL_OCR_MARKER,
        "provisional_vlm_projection_marker_mismatch",
    )
    fail(
        bool(
            {
                "agreement_types",
                "bbox_coordinate_system",
                "reading_order_method",
                "row_band_count",
            }
            & item.keys()
        ),
        "provisional_vlm_projection_has_ocr_metadata",
    )
    lines = content_lines(str(item.get("observed_text", "")), packet=False)
    fail(not lines, "provisional_vlm_projection_text_empty")
    fail(
        any(
            not line.lstrip().startswith(PROVISIONAL_OCR_MARKER + " ")
            for line in lines
        ),
        "provisional_vlm_projection_text_unmarked",
    )


def _layer_ocr_match_text(value: object) -> str:
    fail(not isinstance(value, str) or not value.strip(), "layer_ocr_supporter_text_missing")
    return unicodedata.normalize("NFC", value).strip()


def _layer_ocr_bbox(value: object) -> list[int]:
    fail(
        not isinstance(value, list)
        or len(value) != 4
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
        or value[0] < 0
        or value[1] < 0
        or value[2] <= 0
        or value[3] <= 0
        or value[0] + value[2] > 1000
        or value[1] + value[3] > 1000,
        "layer_ocr_supporter_bbox_invalid",
    )
    return list(value)


def _layer_ocr_overlap(first: list[int], second: list[int]) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[0] + first[2], second[0] + second[2])
    bottom = min(first[1] + first[3], second[1] + second[3])
    if right <= left or bottom <= top:
        return 0.0
    intersection = (right - left) * (bottom - top)
    smaller = min(first[2] * first[3], second[2] * second[3])
    return intersection / smaller if smaller else 0.0


def validate_layer_ocr_supporters(record: dict[str, Any]) -> None:
    """Recompute OCR consensus from the raw per-engine supporter records."""
    native = record.get("native_properties", {})
    observation = native.get("observation_provenance")
    fail(not isinstance(observation, dict), "layer_ocr_observation_provenance_missing")
    agreement = native.get("agreement_type")
    primary_pass = observation.get("primary_pass")
    primary_engine = OCR_ENGINE_BY_PASS.get(primary_pass)
    fail(primary_engine is None, "layer_ocr_primary_pass_invalid")
    primary_group = primary_engine
    fail(
        observation.get("primary_engine") != primary_engine
        or observation.get("primary_independence_group") != primary_group,
        "layer_ocr_primary_supporter_identity_invalid",
    )
    audit_pass = observation.get("audit_pass")
    audit_engine = OCR_ENGINE_BY_PASS.get(audit_pass) if audit_pass is not None else None
    audit_group = audit_engine
    fail(
        audit_pass is not None
        and (
            audit_engine is None
            or observation.get("audit_engine") != audit_engine
            or observation.get("audit_independence_group") != audit_group
        ),
        "layer_ocr_audit_supporter_identity_invalid",
    )
    fail(
        agreement in {"independent_agreement", "display_transform_unresolved"}
        and (audit_group is None or primary_group == audit_group),
        "layer_high_ocr_supporters_not_independent",
    )
    fail(
        agreement == "same_engine_agreement"
        and (audit_group is None or primary_group != audit_group),
        "layer_same_engine_ocr_supporters_not_same_group",
    )
    fail(
        agreement == "provisional_single_pass" and audit_group is not None,
        "layer_single_pass_ocr_has_audit_supporter",
    )

    coordinate_system = native.get("bbox_coordinate_system")
    expected = [(
        primary_pass,
        primary_engine,
        primary_group,
        observation.get("primary_line_id"),
        observation.get("primary_bbox_coordinate_system"),
        native.get("primary_confidence"),
    )]
    if audit_pass is not None:
        expected.append((
            audit_pass,
            audit_engine,
            audit_group,
            observation.get("audit_line_id"),
            observation.get("audit_bbox_coordinate_system"),
            native.get("audit_confidence"),
        ))
    fail(
        any(contract[4] != coordinate_system for contract in expected),
        "layer_ocr_supporter_coordinate_frame_mismatch",
    )
    fail(
        len(expected) == 2
        and observation.get("comparison_coordinate_system") != coordinate_system,
        "layer_ocr_comparison_coordinate_frame_mismatch",
    )
    supporters = observation.get("supporters")
    fail(
        not isinstance(supporters, list) or len(supporters) != len(expected),
        "layer_ocr_supporters_missing",
    )
    content = record.get("content", {})
    line_text = _layer_ocr_match_text(content.get("raw_text"))
    boxes: list[list[int]] = []
    for supporter, contract in zip(supporters, expected):
        fail(not isinstance(supporter, dict), "layer_ocr_supporter_invalid")
        pass_name, engine, group, line_id, frame, confidence = contract
        fail(
            supporter.get("pass") != pass_name
            or supporter.get("engine") != engine
            or supporter.get("independence_group") != group
            or supporter.get("line_id") != line_id
            or supporter.get("bbox_coordinate_system") != frame,
            "layer_ocr_supporter_identity_mismatch",
        )
        fail(
            _layer_ocr_match_text(supporter.get("raw_text")) != line_text,
            "layer_ocr_supporter_text_mismatch",
        )
        actual_confidence = supporter.get("confidence")
        fail(
            isinstance(actual_confidence, bool)
            or not isinstance(actual_confidence, (int, float))
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or float(actual_confidence) != float(confidence),
            "layer_ocr_supporter_confidence_mismatch",
        )
        boxes.append(_layer_ocr_bbox(supporter.get("bbox")))

    geometry = record.get("geometry")
    fail(not isinstance(geometry, dict), "layer_ocr_consensus_geometry_missing")
    result_bbox = _layer_ocr_bbox([
        geometry.get("x"), geometry.get("y"),
        geometry.get("width"), geometry.get("height"),
    ])
    union = [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[0] + box[2] for box in boxes),
        max(box[1] + box[3] for box in boxes),
    ]
    union[2] -= union[0]
    union[3] -= union[1]
    fail(result_bbox != union, "layer_ocr_supporter_union_mismatch")
    claimed_overlap = native.get("spatial_overlap")
    if len(boxes) == 1:
        fail(
            claimed_overlap != 0 or agreement != "provisional_single_pass",
            "layer_single_pass_ocr_supporter_contract_invalid",
        )
        return
    recomputed_overlap = _layer_ocr_overlap(boxes[0], boxes[1])
    fail(
        isinstance(claimed_overlap, bool)
        or not isinstance(claimed_overlap, (int, float))
        or abs(float(claimed_overlap) - round(recomputed_overlap, 6)) > 0.000001
        or recomputed_overlap < 0.5,
        "layer_ocr_supporter_overlap_mismatch",
    )


def layer_ocr_quality(record: dict[str, Any]) -> tuple[str, list[str], str | None]:
    native = record.get("native_properties", {})
    agreement = native.get("agreement_type")
    expected_tier = OCR_QUALITY_BY_AGREEMENT.get(agreement)
    fail(expected_tier is None, "layer_ocr_agreement_invalid")
    fail(native.get("quality_tier") != expected_tier, "layer_ocr_tier_invalid")
    marker_present = "provisional_marker" in native
    marker = native.get("provisional_marker")
    overlap = native.get("spatial_overlap")
    bbox_coordinate_system = native.get("bbox_coordinate_system")
    fail(
        bbox_coordinate_system not in OCR_BBOX_COORDINATE_SYSTEMS,
        "layer_ocr_bbox_coordinate_system_invalid",
    )
    numeric_overlap = isinstance(overlap, (int, float)) and not isinstance(overlap, bool)
    method = record.get("provenance", {}).get("extraction_method")
    validate_layer_ocr_supporters(record)
    origin = native.get("visual_origin")
    materialization = (
        origin.get("materialization") if isinstance(origin, dict) else None
    )
    if isinstance(origin, dict) and origin.get("kind") in {
        "office_embedded_image", "notebook_embedded_image",
    }:
        fail(
            expected_tier != "provisional"
            or marker != PROVISIONAL_OCR_MARKER
            or method != "adaptive_local_ocr_provisional"
            or native.get("display_transform_resolved") is not False
            or not isinstance(materialization, dict)
            or materialization.get("display_transform_resolved") is not False
            or materialization.get("display_transform_status") != "unresolved",
            "layer_unresolved_embedded_ocr_not_provisional",
        )
    if expected_tier == "high":
        fail(marker_present, "layer_high_ocr_has_provisional_marker")
        fail(native.get("independent_engines") is not True, "layer_high_ocr_not_independent")
        fail(method != "dual_local_ocr_consensus", "layer_high_ocr_method_invalid")
        fail(not numeric_overlap or overlap < 0.5, "layer_high_ocr_overlap_invalid")
        fail(
            bbox_coordinate_system != "source_orientation_1_top_left_normalized_1000",
            "layer_high_ocr_coordinate_frame_invalid",
        )
        return expected_tier, [agreement], None
    fail(marker != PROVISIONAL_OCR_MARKER, "layer_provisional_ocr_marker_invalid")
    fail(method != "adaptive_local_ocr_provisional", "layer_provisional_ocr_method_invalid")
    if agreement in {
        "same_engine_agreement", "display_transform_unresolved",
    }:
        fail(not numeric_overlap or overlap < 0.5, "layer_same_engine_overlap_invalid")
        if agreement == "display_transform_unresolved":
            fail(
                native.get("independent_engines") is not True,
                "layer_display_transform_ocr_not_independent",
            )
    else:
        fail(overlap != 0, "layer_single_pass_overlap_invalid")
    if agreement == "display_transform_unresolved":
        fail(
            native.get("display_transform_resolved") is not False
            or native.get("embedded_source_agreement_type")
            != "independent_agreement",
            "layer_display_transform_ocr_source_contract_invalid",
        )
        fail(
            not isinstance(origin, dict)
            or origin.get("kind") not in {
                "office_embedded_image", "notebook_embedded_image",
            }
            or not isinstance(materialization, dict)
            or materialization.get("display_transform_resolved") is not False
            or materialization.get("display_transform_status") != "unresolved",
            "layer_display_transform_ocr_origin_contract_invalid",
        )
    return expected_tier, [agreement], PROVISIONAL_OCR_MARKER


def layer_provisional_text_quality(
    record: dict[str, Any],
) -> tuple[str, str] | None:
    """Independently reconstruct the allowlisted provisional VLM contract."""
    evidence_type = record.get("evidence_type")
    provenance = record.get("provenance", {})
    method = provenance.get("extraction_method") if isinstance(provenance, dict) else None
    native = record.get("native_properties", {})
    if not isinstance(native, dict):
        native = {}
    declares_quality = any(
        key in native for key in ("quality_tier", "provisional_marker")
    )
    local_vlm_method_like = (
        isinstance(method, str)
        and method.startswith("local_vlm_")
    )
    if not (
        method in PROVISIONAL_TEXT_METHOD_TYPES
        or local_vlm_method_like
        or (
            evidence_type in PROVISIONAL_TEXT_EVIDENCE_TYPES
            and declares_quality
        )
    ):
        return None
    allowed_types = PROVISIONAL_TEXT_METHOD_TYPES.get(method)
    fail(allowed_types is None, "layer_provisional_vlm_method_invalid")
    fail(
        evidence_type not in allowed_types,
        "layer_provisional_vlm_evidence_type_invalid",
    )
    fail(
        native.get("quality_tier") != "provisional",
        "layer_provisional_vlm_quality_invalid",
    )
    fail(
        native.get("provisional_marker") != PROVISIONAL_OCR_MARKER,
        "layer_provisional_vlm_marker_invalid",
    )
    fail(
        native.get("question_independent") is not True,
        "layer_provisional_vlm_question_dependency_invalid",
    )
    if method == "local_vlm_unlocated_transcript_provisional":
        fail(
            native.get("location_status") != "unlocated"
            or native.get("transcript_type")
            != "whole_image_faithful_transcript",
            "layer_unlocated_vlm_transcript_provenance_invalid",
        )
        fail(
            "geometry" in record,
            "layer_unlocated_vlm_transcript_has_geometry",
        )
    return "provisional", PROVISIONAL_OCR_MARKER


def _layer_evidence_text(record: dict[str, Any]) -> str:
    content = record.get("content", {})
    for key in ("normalized_text", "raw_text"):
        if isinstance(content.get(key), str):
            return content[key].strip()
    return ""


def _layer_provisional_visual_search_text(value: str) -> str:
    """Reproduce SearchUnit builder normalization, including line markers."""
    lines: list[str] = []
    for line in value.splitlines():
        item = line.strip()
        if not item or item == PROVISIONAL_OCR_MARKER:
            continue
        if not item.startswith(PROVISIONAL_OCR_MARKER + " "):
            item = f"{PROVISIONAL_OCR_MARKER} {item}"
        lines.append(item)
    return "\n".join(lines)


def layer_provisional_visual_search_unit_quality(
    unit: dict[str, Any],
    evidence_by_id: dict[str, dict[str, Any]],
    documents_by_id: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Independently rebuild a provisional VLM text SearchUnit."""
    fail(
        unit.get("unit_type") != "text_chunk",
        "provisional_visual_search_unit_type_invalid",
    )
    source_ids = unit.get("source_evidence_ids")
    fail(
        not isinstance(source_ids, list) or len(source_ids) != 1,
        "provisional_visual_search_unit_sources_invalid",
    )
    source = evidence_by_id.get(source_ids[0])
    fail(source is None, "provisional_visual_search_unit_source_missing")
    fail(
        source.get("evidence_type") != "text_block",
        "provisional_visual_search_unit_source_type_invalid",
    )
    provenance = source.get("provenance", {})
    method = (
        provenance.get("extraction_method")
        if isinstance(provenance, dict) else None
    )
    fail(
        method not in PROVISIONAL_TEXT_METHOD_TYPES,
        "provisional_visual_search_unit_source_method_invalid",
    )
    # This also validates the source-side quality, marker, question independence,
    # and the stricter unlocated-transcript contract.
    fail(
        layer_provisional_text_quality(source) is None,
        "provisional_visual_search_unit_source_quality_missing",
    )

    expected_text = _layer_provisional_visual_search_text(
        _layer_evidence_text(source)
    )
    fail(
        not expected_text,
        "provisional_visual_search_unit_source_text_empty",
    )
    expected_text_record = {
        "search_text": expected_text,
        "sha256": sha256_text(expected_text),
        "char_count": len(expected_text),
    }
    fail(
        unit.get("text") != expected_text_record,
        "provisional_visual_search_unit_text_mismatch",
    )

    location = source.get("location", {})
    fail(
        not isinstance(location, dict),
        "provisional_visual_search_unit_source_locator_invalid",
    )
    expected_locator = {
        key: location[key]
        for key in IMAGE_PACKET_LOCATOR_KEYS if key in location
    }
    fail(
        unit.get("locator") != expected_locator,
        "provisional_visual_search_unit_locator_mismatch",
    )

    native = source.get("native_properties", {})
    origin = native.get("visual_origin") if isinstance(native, dict) else None
    fail(
        not isinstance(origin, dict)
        or origin.get("kind") not in IMAGE_PACKET_CONTAINER_KINDS,
        "provisional_visual_search_unit_origin_invalid",
    )
    parent_id = source.get("parent_evidence_id")
    parent = evidence_by_id.get(parent_id) if isinstance(parent_id, str) else None
    fail(
        parent is None or parent.get("evidence_type") != "image",
        "provisional_visual_search_unit_parent_image_missing",
    )
    document = (
        documents_by_id.get(unit.get("document_id"))
        if documents_by_id is not None else None
    )
    _validate_layer_visual_origins(
        parent, [source], str(origin.get("kind")), document
    )

    origin_kind = origin.get("kind") if isinstance(origin, dict) else None
    expected_container = (
        origin_kind
        if origin_kind in IMAGE_PACKET_CONTAINER_KINDS
        else "standalone_image"
    )
    fail(
        unit.get("context") != {
            "container_kind": expected_container,
            "quality_tier": "provisional",
            "provisional_marker": PROVISIONAL_OCR_MARKER,
        },
        "provisional_visual_search_unit_context_mismatch",
    )
    fail(
        unit.get("search_unit_id") != _expected_search_unit_id(unit),
        "provisional_visual_search_unit_id_unstable",
    )


def _layer_image_fragment_text(value: str, quality_tier: str) -> str:
    fragments: list[str] = []
    for line in value.splitlines():
        fragment = line.strip()
        if quality_tier == "provisional" and fragment.startswith(PROVISIONAL_OCR_MARKER):
            fragment = fragment[len(PROVISIONAL_OCR_MARKER):].lstrip()
        if fragment:
            fragments.append(fragment)
    return " ".join(fragments)


def _layer_image_row_bands(
    lines: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    fragments: list[dict[str, Any]] = []
    for line in lines:
        geometry = line.get("geometry")
        fail(not isinstance(geometry, dict), "image_packet_source_geometry_missing")
        values = [geometry.get(key) for key in ("x", "y", "width", "height")]
        fail(
            any(
                not isinstance(value, (int, float)) or isinstance(value, bool)
                for value in values
            ),
            "image_packet_source_geometry_not_numeric",
        )
        x, y, width, height = values
        fail(
            x < 0 or y < 0 or width <= 0 or height <= 0
            or x + width > 1000 or y + height > 1000,
            "image_packet_source_geometry_out_of_bounds",
        )
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
            if distance <= IMAGE_ROW_BAND_CENTER_TOLERANCE * min(
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


def _validate_layer_visual_origins(
    parent: dict[str, Any],
    visual_sources: list[dict[str, Any]],
    expected_container: str,
    document: dict[str, Any] | None = None,
) -> None:
    """Validate the canonical image origin and every child copy fail-closed."""
    parent_location = parent.get("location", {})
    parent_native = parent.get("native_properties", {})
    fail(not isinstance(parent_location, dict), "image_packet_parent_location_invalid")
    fail(not isinstance(parent_native, dict), "image_packet_parent_native_invalid")
    parent_origin = (
        parent_native.get("visual_origin") if isinstance(parent_native, dict) else None
    )
    fail(
        not isinstance(parent_origin, dict),
        "image_packet_parent_visual_origin_invalid",
    )
    fail(
        not {
            "kind", "source_relative_path", "source_sha256",
            "source_location", "materialization",
        }.issubset(parent_origin),
        "image_packet_parent_visual_origin_incomplete",
    )
    fail(
        parent_origin.get("kind") != expected_container,
        "image_packet_parent_visual_origin_kind_invalid",
    )
    fail(
        parent_origin.get("source_location") != parent_location,
        "image_packet_parent_visual_origin_location_mismatch",
    )
    source_relative_path = parent_origin.get("source_relative_path")
    source_sha256 = parent_origin.get("source_sha256")
    fail(
        not isinstance(source_relative_path, str) or not source_relative_path,
        "image_packet_parent_visual_origin_source_path_invalid",
    )
    fail(
        not isinstance(source_sha256, str)
        or SHA256_PATTERN.fullmatch(source_sha256) is None,
        "image_packet_parent_visual_origin_source_hash_invalid",
    )
    if document is not None:
        document_source = document.get("source", {})
        fail(
            not isinstance(document_source, dict)
            or source_relative_path != document_source.get("relative_path")
            or source_sha256 != document_source.get("sha256"),
            "image_packet_parent_visual_origin_document_mismatch",
        )
        document_path = (
            document_source.get("relative_path")
            if isinstance(document_source, dict) else None
        )
        expected_document_container = (
            DOCUMENT_VISUAL_CONTAINER_BY_SUFFIX.get(
                PurePosixPath(document_path).suffix.casefold()
            )
            if isinstance(document_path, str) else None
        )
        fail(
            expected_document_container is not None
            and expected_document_container != expected_container,
            "image_packet_parent_visual_origin_document_kind_mismatch",
        )

    materialization = parent_origin.get("materialization")
    fail(
        not isinstance(materialization, dict),
        "image_packet_parent_visual_materialization_invalid",
    )
    rendered_sha256 = materialization.get("rendered_sha256")
    fail(
        not isinstance(rendered_sha256, str)
        or SHA256_PATTERN.fullmatch(rendered_sha256) is None,
        "image_packet_parent_rendered_hash_invalid",
    )
    fail(
        materialization.get("source_sha256") != source_sha256,
        "image_packet_parent_materialization_source_hash_mismatch",
    )
    fail(
        materialization.get("external_network_used") is not False,
        "image_packet_parent_materialization_network_invalid",
    )
    if expected_container in {
        "office_embedded_image", "notebook_embedded_image",
    }:
        embedded_sha256 = materialization.get("embedded_sha256")
        fail(
            not isinstance(embedded_sha256, str)
            or SHA256_PATTERN.fullmatch(embedded_sha256) is None
            or embedded_sha256 != rendered_sha256
            or parent_native.get("embedded_sha256") != embedded_sha256,
            "image_packet_parent_embedded_digest_mismatch",
        )
    else:
        fail(
            parent_native.get("source_sha256") != rendered_sha256,
            "image_packet_parent_rendered_digest_mismatch",
        )

    for source in visual_sources:
        native = source.get("native_properties", {})
        origin = native.get("visual_origin") if isinstance(native, dict) else None
        fail(
            not isinstance(origin, dict),
            "image_packet_child_visual_origin_missing",
        )
        fail(
            origin != parent_origin,
            "image_packet_child_visual_origin_parent_mismatch",
        )


def validate_layer_visual_source_binding(
    source: dict[str, Any],
    evidence_by_id: dict[str, dict[str, Any]],
    documents_by_id: dict[str, dict[str, Any]],
) -> None:
    """Bind an individually projected visual Evidence item to its source image."""
    document_id = source.get("document_id")
    document = documents_by_id.get(document_id)
    fail(document is None, "visual_source_document_missing")
    parent_id = source.get("parent_evidence_id")
    parent = evidence_by_id.get(parent_id) if isinstance(parent_id, str) else None
    fail(
        parent is None or parent.get("evidence_type") != "image",
        "visual_source_parent_image_missing",
    )
    fail(
        parent.get("document_id") != document_id,
        "visual_source_parent_document_mismatch",
    )
    native = source.get("native_properties", {})
    origin = native.get("visual_origin") if isinstance(native, dict) else None
    fail(
        not isinstance(origin, dict)
        or origin.get("kind") not in IMAGE_PACKET_CONTAINER_KINDS,
        "visual_source_origin_invalid",
    )
    _validate_layer_visual_origins(
        parent, [source], str(origin.get("kind")), document
    )


def _reconstruct_layer_image_packet(
    unit: dict[str, Any],
    evidence_by_id: dict[str, dict[str, Any]],
    documents_by_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    source_ids = unit.get("source_evidence_ids")
    fail(
        not isinstance(source_ids, list) or not source_ids,
        "image_packet_source_ids_invalid",
    )
    source_records = [evidence_by_id.get(evidence_id) for evidence_id in source_ids]
    fail(any(record is None for record in source_records), "image_packet_source_missing")
    image_sources = [
        record for record in source_records
        if isinstance(record, dict) and record.get("evidence_type") == "image"
    ]
    fail(
        len(image_sources) != 1,
        "image_packet_parent_image_source_count_invalid",
    )
    parent = image_sources[0]
    parent_id = parent["evidence_id"]
    fail(source_ids[0] != parent_id, "image_packet_parent_image_not_first")
    fail(
        any(
            record.get("evidence_type") not in {"image", "ocr_line"}
            for record in source_records if isinstance(record, dict)
        ),
        "image_packet_non_ocr_source_present",
    )
    context = unit.get("context", {})
    tier = context.get("quality_tier")
    frame = context.get("bbox_coordinate_system")
    parent_native = parent.get("native_properties", {})
    parent_origin = (
        parent_native.get("visual_origin") if isinstance(parent_native, dict) else None
    )
    fail(not isinstance(parent_origin, dict), "image_packet_parent_visual_origin_invalid")
    expected_container = parent_origin.get("kind")
    fail(
        expected_container not in IMAGE_PACKET_CONTAINER_KINDS,
        "image_packet_parent_visual_origin_kind_invalid",
    )

    ocr_sources = [
        record for record in evidence_by_id.values()
        if record.get("evidence_type") == "ocr_line"
        and record.get("parent_evidence_id") == parent_id
        and record.get("native_properties", {}).get("quality_tier") == tier
        and record.get("native_properties", {}).get("bbox_coordinate_system") == frame
        and _layer_evidence_text(record)
    ]
    fail(not ocr_sources, "image_packet_matching_child_ocr_missing")
    for source in ocr_sources:
        layer_ocr_quality(source)
    document = (
        documents_by_id.get(unit.get("document_id"))
        if documents_by_id is not None else None
    )
    _validate_layer_visual_origins(
        parent, ocr_sources, expected_container, document
    )

    rows: list[str] = []
    ordered_sources: list[dict[str, Any]] = []
    for band in _layer_image_row_bands(ocr_sources):
        row = " ".join(
            fragment
            for fragment in (
                _layer_image_fragment_text(_layer_evidence_text(source), str(tier))
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
    fail(not rows, "image_packet_reconstructed_text_empty")

    content_ref = parent.get("content", {}).get("content_ref")
    fail(
        not isinstance(content_ref, str) or not content_ref,
        "image_packet_parent_content_ref_missing",
    )
    source_name = Path(content_ref.split("::", 1)[0].split("#", 1)[0]).name
    body = "\n".join(rows)
    search_text = f"Image file: {source_name}\n{body}" if source_name else body
    parent_location = parent.get("location", {})
    locator = {
        key: parent_location[key]
        for key in IMAGE_PACKET_LOCATOR_KEYS if key in parent_location
    }
    if not locator:
        locator = {"object_index": 1}
    locator["locator_text"] = (
        f"container_kind={expected_container};quality_tier={tier};"
        f"bbox_coordinate_system={frame}"
    )
    agreement_types = {
        source.get("native_properties", {}).get("agreement_type")
        for source in ordered_sources
    }
    fail(
        not agreement_types or any(not isinstance(value, str) for value in agreement_types),
        "image_packet_reconstructed_agreement_invalid",
    )
    return {
        "source_evidence_ids": [parent_id] + [
            source["evidence_id"] for source in ordered_sources
        ],
        "locator": locator,
        "search_text": search_text,
        "container_kind": expected_container,
        "agreement_types": sorted(agreement_types),
        "row_band_count": len(rows),
    }


def layer_image_packet_quality(
    unit: dict[str, Any],
    evidence_by_id: dict[str, dict[str, Any]],
    documents_by_id: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, list[str], str | None]:
    context = unit.get("context", {})
    fail(
        context.get("container_kind") not in IMAGE_PACKET_CONTAINER_KINDS,
        "image_packet_container_invalid",
    )
    bbox_coordinate_system = context.get("bbox_coordinate_system")
    fail(
        bbox_coordinate_system not in OCR_BBOX_COORDINATE_SYSTEMS,
        "image_packet_bbox_coordinate_system_invalid",
    )
    fail(
        context.get("reading_order_method") != "geometry_row_bands_v1"
        or not isinstance(context.get("row_band_count"), int)
        or isinstance(context.get("row_band_count"), bool)
        or context.get("row_band_count", 0) < 1,
        "image_packet_reading_order_invalid",
    )
    tier = context.get("quality_tier")
    agreements = context.get("agreement_types")
    fail(
        tier not in {"high", "provisional"}
        or not isinstance(agreements, list)
        or not agreements
        or len(agreements) != len(set(agreements)),
        "image_packet_quality_metadata_invalid",
    )
    fail(
        {OCR_QUALITY_BY_AGREEMENT.get(value) for value in agreements} != {tier},
        "image_packet_mixed_agreement_tiers",
    )
    marker_present = "provisional_marker" in context
    marker = context.get("provisional_marker")
    if tier == "high":
        fail(marker_present, "high_image_packet_has_provisional_marker")
        expected_marker = None
    else:
        fail(marker != PROVISIONAL_OCR_MARKER, "provisional_image_packet_marker_invalid")
        expected_marker = PROVISIONAL_OCR_MARKER
    source_records = [
        evidence_by_id.get(evidence_id)
        for evidence_id in unit.get("source_evidence_ids", [])
    ]
    ocr_sources = [
        record for record in source_records
        if isinstance(record, dict) and record.get("evidence_type") == "ocr_line"
    ]
    fail(not ocr_sources, "image_packet_ocr_source_missing")
    source_contracts = [layer_ocr_quality(record) for record in ocr_sources]
    fail({value[0] for value in source_contracts} != {tier}, "image_packet_source_tier_mixed")
    fail(
        {value for contract in source_contracts for value in contract[1]} != set(agreements),
        "image_packet_source_agreement_mismatch",
    )
    fail(
        {
            record.get("native_properties", {}).get("bbox_coordinate_system")
            for record in ocr_sources
        } != {bbox_coordinate_system},
        "image_packet_source_coordinate_frame_mixed",
    )
    search_text = unit.get("text", {}).get("search_text", "")
    packet_lines = [
        line for line in str(search_text).splitlines() if line.strip()
    ]
    lines = (
        packet_lines[1:]
        if packet_lines and packet_lines[0].startswith("Image file: ")
        else packet_lines
    )
    fail(not lines, "image_packet_text_empty")
    fail(context.get("row_band_count") != len(lines), "image_packet_row_band_count_mismatch")
    if tier == "high":
        fail(
            any(line.lstrip().startswith(PROVISIONAL_OCR_MARKER) for line in lines),
            "high_image_packet_text_marked_provisional",
        )
    else:
        fail(
            any(
                not line.lstrip().startswith(PROVISIONAL_OCR_MARKER + " ")
                for line in lines
            ),
            "provisional_image_packet_text_unmarked",
        )
    expected = _reconstruct_layer_image_packet(
        unit, evidence_by_id, documents_by_id
    )
    fail(
        unit.get("source_evidence_ids") != expected["source_evidence_ids"],
        "image_packet_source_ids_or_order_mismatch",
    )
    fail(
        unit.get("locator") != expected["locator"],
        "image_packet_locator_mismatch",
    )
    fail(
        search_text != expected["search_text"],
        "image_packet_text_reconstruction_mismatch",
    )
    fail(
        context.get("container_kind") != expected["container_kind"],
        "image_packet_container_origin_mismatch",
    )
    fail(
        agreements != expected["agreement_types"],
        "image_packet_agreement_order_mismatch",
    )
    fail(
        context.get("row_band_count") != expected["row_band_count"],
        "image_packet_row_order_mismatch",
    )
    return tier, agreements, expected_marker


def bound_source(root: Path, relative_path: str) -> Path:
    relative = PurePosixPath(relative_path)
    fail(relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts), "unsafe_source_path")
    path = root.joinpath(*relative.parts)
    fail(path.is_symlink() or not path.is_file(), "source_not_regular_file")
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("source_path_escape") from exc
    return resolved


def validate(output: Path, source_root: Path, inventory: Path) -> dict[str, Any]:
    output = output.resolve(strict=True)
    source_root = source_root.resolve(strict=True)
    inventory = inventory.resolve(strict=True)
    # A failed revalidation must never leave a stale PASS artifact available
    # to a later graph projection.
    _clear_lineage_artifacts(output)
    state = json.loads((output / "adaptive-reader-state.json").read_text(encoding="utf-8"))
    fail(state.get("builder") != "adaptive-layer1-semantic-bridge", "builder_invalid")
    fail(state.get("status") not in {"complete", "complete_with_limits"}, "build_status_invalid")
    fail(state.get("question_independent") is not True, "question_independent_invalid")
    fail(state.get("execution_policy") != "never_execute", "execution_policy_invalid")
    fail(state.get("external_network_used") is not False, "external_network_policy_invalid")
    fail(
        not isinstance(state.get("llm_used_for_extraction"), bool),
        "llm_extraction_flag_invalid",
    )
    fail(state.get("requires_content_security_gate") is not True, "security_gate_requirement_invalid")
    fail(Path(state.get("source_root", "")).resolve() != source_root, "source_root_mismatch")
    fail(state.get("source_inventory", {}).get("sha256") != sha256_file(inventory), "source_inventory_hash_mismatch")

    for stage in state.get("stages", {}).values():
        stage_path = output / stage.get("path", "")
        fail(not stage_path.is_file(), "stage_file_missing")
        fail(stage.get("sha256") != sha256_file(stage_path), "stage_hash_mismatch")
    for output_record in state.get("outputs", {}).values():
        path = output / output_record.get("path", "")
        fail(not path.is_file(), "semantic_output_missing")
        fail(output_record.get("sha256") != sha256_file(path), "semantic_output_hash_mismatch")

    manifest = json.loads((output / "layer1-input-manifest.json").read_text(encoding="utf-8"))
    fail(manifest.get("schema_version") != "0.1", "manifest_schema_invalid")
    fail(Path(manifest.get("source_root", "")).resolve() != source_root, "manifest_source_root_mismatch")
    paths = manifest.get("paths", [])
    fail(not isinstance(paths, list) or not paths or len(paths) != len(set(paths)), "manifest_paths_invalid")
    fail(len(paths) != state.get("selected_file_count"), "selected_file_count_mismatch")
    inventory_records = read_jsonl(inventory)
    inventory_files = {
        item["relative_path"]: item
        for item in inventory_records
        if item.get("kind") == "file" and isinstance(item.get("relative_path"), str)
    }
    fail(not set(paths) <= set(inventory_files), "manifest_path_missing_from_inventory")

    intermediate_state = json.loads((output / "layer1-intermediate" / "build-state.json").read_text(encoding="utf-8"))
    fail(
        intermediate_state.get("build_status") not in {"complete", "complete_with_failures"},
        "intermediate_build_not_terminal",
    )
    fail(intermediate_state.get("input_paths") != paths, "intermediate_input_manifest_mismatch")
    validation_state = json.loads((output / "layer1-validation-state.json").read_text(encoding="utf-8"))
    fail(validation_state.get("status") != "pass", "intermediate_validation_status_invalid")
    fail(
        validation_state.get("schema_validation") not in {"draft202012", "structural_contract_only"},
        "intermediate_schema_validation_mode_invalid",
    )
    fail(
        validation_state.get("intermediate_state_sha256")
        != sha256_file(output / "layer1-intermediate" / "build-state.json"),
        "intermediate_validation_source_hash_mismatch",
    )
    adapter_state = json.loads((output / "layer1-adapter" / "layer1-adapter-state.json").read_text(encoding="utf-8"))
    fail(adapter_state.get("requires_content_security_gate") is not True, "adapter_security_gate_requirement_invalid")
    fail(adapter_state.get("adapter") != ADAPTER_NAME, "adapter_name_invalid")
    fail(adapter_state.get("adapter_version") != ADAPTER_VERSION, "adapter_version_invalid")

    layer_documents_path = output / "layer1-intermediate" / "documents.jsonl"
    layer_evidence_path = output / "layer1-intermediate" / "evidence.jsonl"
    layer_relations_path = output / "layer1-intermediate" / "relations.jsonl"
    search_state_path = output / "layer1-search" / "search-build-state.json"
    search_units_path = output / "layer1-search" / "search_units.jsonl"
    fail(
        adapter_state.get("inputs", {}).get("documents_sha256")
        != sha256_file(layer_documents_path),
        "adapter_intermediate_documents_hash_mismatch",
    )
    fail(
        adapter_state.get("inputs", {}).get("evidence_sha256")
        != sha256_file(layer_evidence_path),
        "adapter_intermediate_evidence_hash_mismatch",
    )
    fail(
        adapter_state.get("search_unit_projection", {}).get("search_units_sha256")
        != sha256_file(search_units_path),
        "adapter_search_units_hash_mismatch",
    )

    layer_documents = read_jsonl(layer_documents_path)
    layer_evidence = read_jsonl(layer_evidence_path)
    layer_relations = read_jsonl(layer_relations_path)
    layer_documents_by_id = {
        item["document_id"]: item for item in layer_documents
    }
    expected_layer_relations = derive_native_structural_relations(
        layer_documents, layer_evidence, intermediate_state,
    )
    layer_relation_by_id = {
        record.get("relation_id"): record for record in layer_relations
    }
    expected_layer_relation_by_id = {
        record["relation_id"]: record for record in expected_layer_relations
    }
    fail(
        len(layer_relation_by_id) != len(layer_relations)
        or layer_relation_by_id != expected_layer_relation_by_id,
        "native_structural_relations_mismatch",
    )
    fail(
        intermediate_state.get("totals", {}).get("relations")
        != len(expected_layer_relations),
        "native_structural_relation_count_mismatch",
    )
    derived_llm_extraction = derive_llm_extraction(layer_evidence)
    fail(
        state.get("llm_used_for_extraction") is not derived_llm_extraction["used"],
        "llm_extraction_flag_lineage_mismatch",
    )
    fail(
        state.get("llm_extraction") != derived_llm_extraction,
        "llm_extraction_summary_lineage_mismatch",
    )
    layer_evidence_ids = [item.get("evidence_id") for item in layer_evidence]
    fail(
        any(not isinstance(value, str) or not value for value in layer_evidence_ids)
        or len(layer_evidence_ids) != len(set(layer_evidence_ids)),
        "layer_evidence_ids_invalid",
    )
    layer_evidence_by_id = {item["evidence_id"]: item for item in layer_evidence}
    search_state = json.loads(search_state_path.read_text(encoding="utf-8"))
    search_generated_at = search_state.get("generated_at")
    intermediate_run_at = intermediate_state.get("run_at")
    fail(
        search_state.get("builder") != SEARCH_UNIT_BUILDER
        or search_state.get("builder_version") != SEARCH_UNIT_BUILDER_VERSION
        or search_state.get("deterministic") is not True,
        "lineage_search_builder_provenance_invalid",
    )
    fail(
        not is_rfc3339_timestamp(search_generated_at)
        or search_generated_at != intermediate_run_at,
        "lineage_search_run_mismatch",
    )
    search_units = read_jsonl(search_units_path)
    search_unit_ids = [item.get("search_unit_id") for item in search_units]
    fail(
        any(not isinstance(value, str) or not value for value in search_unit_ids)
        or len(search_unit_ids) != len(set(search_unit_ids)),
        "search_unit_ids_invalid",
    )
    search_units_by_id = {item["search_unit_id"]: item for item in search_units}
    for item in search_units:
        provenance = item.get("provenance", {})
        fail(
            provenance != {
                "builder": SEARCH_UNIT_BUILDER,
                "builder_version": SEARCH_UNIT_BUILDER_VERSION,
                "generated_at": search_generated_at,
                "deterministic": True,
            },
            f"lineage_search_unit_run_mismatch:{item['search_unit_id']}",
        )
        if item.get("unit_type") == "image_text_packet":
            continue
        if item.get("unit_type") in NATIVE_CHART_UNIT_TYPES:
            layer_native_chart_search_unit(item, layer_evidence_by_id)
            continue
        context = item.get("context", {})
        fail(
            not isinstance(context, dict),
            f"lineage_search_unit_context_invalid:{item['search_unit_id']}",
        )
        source_records = [
            layer_evidence_by_id.get(evidence_id)
            for evidence_id in item.get("source_evidence_ids", [])
            if isinstance(evidence_id, str)
        ]
        source_methods = {
            source.get("provenance", {}).get("extraction_method")
            for source in source_records if isinstance(source, dict)
        }
        provisional_source = any(
            method in PROVISIONAL_TEXT_METHOD_TYPES
            or (isinstance(method, str) and method.startswith("local_vlm_"))
            for method in source_methods
        )
        if provisional_source or IMAGE_QUALITY_KEYS & context.keys():
            layer_provisional_visual_search_unit_quality(
                item, layer_evidence_by_id, layer_documents_by_id
            )

    documents = read_jsonl(output / "semantic-documents.jsonl")
    evidence = read_jsonl(output / "semantic-evidence.jsonl")
    fail(len(documents) != state["outputs"]["documents"]["count"], "document_count_mismatch")
    fail(len(evidence) != state["outputs"]["evidence"]["count"], "evidence_count_mismatch")
    document_ids = [item.get("document_id") for item in documents]
    evidence_ids = [item.get("evidence_id") for item in evidence]
    fail(any(not isinstance(value, str) or not value for value in document_ids), "document_id_invalid")
    fail(any(not isinstance(value, str) or not value for value in evidence_ids), "evidence_id_invalid")
    fail(len(document_ids) != len(set(document_ids)), "duplicate_document_id")
    fail(len(evidence_ids) != len(set(evidence_ids)), "duplicate_evidence_id")
    fail(
        any(
            not isinstance(item.get("observed_text"), str)
            or len(item["observed_text"]) > MAX_QUESTION_EVIDENCE_CHARS
            for item in evidence
        ),
        "semantic_question_evidence_size_invalid",
    )
    validate_exact_projection(
        evidence,
        expected_semantic_evidence(
            layer_documents, layer_evidence, search_units
        ),
    )
    fail(
        adapter_state.get("question_sharding")
        != derive_question_sharding_state(evidence),
        "adapter_question_sharding_state_mismatch",
    )
    documents_by_id = {item["document_id"]: item for item in documents}
    by_document: dict[str, list[str]] = defaultdict(list)
    image_quality_counts: Counter[str] = Counter()
    for item in evidence:
        document = documents_by_id.get(item.get("document_id"))
        fail(document is None, "evidence_document_missing")
        fail(not isinstance(item.get("observed_text"), str), "evidence_text_invalid")
        fail(item.get("status") != "observed", "evidence_status_invalid")
        fail(item.get("adapter", {}).get("execution_policy") != "never_execute", "evidence_execution_policy_invalid")
        fail(item.get("source") != {
            "relative_path": document["source"]["relative_path"],
            "sha256": document["source"]["sha256"],
        }, "evidence_source_lineage_mismatch")
        adapter = item.get("adapter", {})
        source_record_type = adapter.get("source_record_type")
        if source_record_type == "ocr_line":
            shard_metadata = adapter.get("question_shard")
            source_projection_id = (
                shard_metadata.get("source_projection_id")
                if isinstance(shard_metadata, dict)
                else item.get("evidence_id")
            )
            source_record = layer_evidence_by_id.get(source_projection_id)
            fail(
                source_record is None or source_record.get("evidence_type") != "ocr_line",
                "projected_ocr_source_missing",
            )
            tier, agreements, marker = layer_ocr_quality(source_record)
            validate_quality_projection(
                item,
                expected_tier=tier,
                expected_agreements=agreements,
                expected_marker=marker,
                packet=False,
            )
            fail(
                item.get("bbox_coordinate_system")
                != source_record.get("native_properties", {}).get("bbox_coordinate_system"),
                "projected_ocr_coordinate_frame_mismatch",
            )
            fail(
                "reading_order_method" in item or "row_band_count" in item,
                "projected_ocr_line_has_packet_order_metadata",
            )
            image_quality_counts[tier] += 1
        elif source_record_type in PROVISIONAL_TEXT_EVIDENCE_TYPES:
            shard_metadata = adapter.get("question_shard")
            source_projection_id = (
                shard_metadata.get("source_projection_id")
                if isinstance(shard_metadata, dict)
                else item.get("evidence_id")
            )
            source_record = layer_evidence_by_id.get(source_projection_id)
            fail(
                source_record is None
                or source_record.get("evidence_type") != source_record_type,
                "projected_provisional_vlm_source_missing",
            )
            provisional_text_quality = layer_provisional_text_quality(
                source_record
            )
            if provisional_text_quality is None:
                fail(
                    bool(IMAGE_QUALITY_KEYS & item.keys()),
                    "native_text_projection_has_image_quality_metadata",
                )
            else:
                validate_provisional_text_projection(item)
                image_quality_counts[provisional_text_quality[0]] += 1
        elif (
            source_record_type == "search_unit"
            and adapter.get("unit_type") == "image_text_packet"
        ):
            source_unit = search_units_by_id.get(adapter.get("source_search_unit_id"))
            fail(
                source_unit is None or source_unit.get("unit_type") != "image_text_packet",
                "projected_image_packet_source_missing",
            )
            fail(
                adapter.get("source_evidence_ids") != source_unit.get("source_evidence_ids"),
                "projected_image_packet_evidence_lineage_mismatch",
            )
            tier, agreements, marker = layer_image_packet_quality(
                source_unit, layer_evidence_by_id, layer_documents_by_id
            )
            validate_quality_projection(
                item,
                expected_tier=tier,
                expected_agreements=agreements,
                expected_marker=marker,
                packet=True,
            )
            source_context = source_unit.get("context", {})
            fail(
                item.get("bbox_coordinate_system")
                != source_context.get("bbox_coordinate_system"),
                "projected_image_packet_coordinate_frame_mismatch",
            )
            fail(
                item.get("reading_order_method")
                != source_context.get("reading_order_method")
                or item.get("row_band_count") != source_context.get("row_band_count"),
                "projected_image_packet_reading_order_mismatch",
            )
            image_quality_counts[tier] += 1
        elif (
            source_record_type == "search_unit"
            and adapter.get("unit_type") in NATIVE_CHART_UNIT_TYPES
        ):
            source_unit = search_units_by_id.get(
                adapter.get("source_search_unit_id")
            )
            fail(
                source_unit is None
                or source_unit.get("unit_type") != adapter.get("unit_type"),
                "projected_chart_search_unit_source_missing",
            )
            fail(
                adapter.get("source_evidence_ids")
                != source_unit.get("source_evidence_ids"),
                "projected_chart_search_unit_evidence_lineage_mismatch",
            )
            layer_native_chart_search_unit(source_unit, layer_evidence_by_id)
            fail(
                bool(IMAGE_QUALITY_KEYS & item.keys()),
                "projected_chart_search_unit_has_image_quality_metadata",
            )
        else:
            fail(
                bool(IMAGE_QUALITY_KEYS & item.keys()),
                "non_image_projection_has_image_quality_metadata",
            )
        by_document[item["document_id"]].append(item["evidence_id"])

    state_entries = intermediate_state.get("entries", {})
    fail(set(state_entries) != set(paths), "intermediate_entry_coverage_mismatch")
    fail({item["source"]["relative_path"] for item in documents} != set(paths), "document_path_coverage_mismatch")
    for document in documents:
        relative = document["source"]["relative_path"]
        entry = state_entries.get(relative, {})
        inventory_item = inventory_files[relative]
        fail(document["document_id"] != entry.get("document_id"), "document_id_lineage_mismatch")
        fail(document["source"].get("sha256") != inventory_item.get("sha256"), "document_inventory_hash_mismatch")
        fail(document["source"].get("size_bytes") != inventory_item.get("size_bytes"), "document_inventory_size_mismatch")
        path = bound_source(source_root, relative)
        fail(path.stat().st_size != document["source"].get("size_bytes"), "source_size_mismatch")
        fail(sha256_file(path) != document["source"].get("sha256"), "source_hash_mismatch")
        fail(document.get("evidence_ids", []) != by_document.get(document["document_id"], []), "document_evidence_order_mismatch")

    limitations = state.get("limitations", {})
    has_limits = any(isinstance(value, int) and value > 0 for value in limitations.values())
    fail((state["status"] == "complete_with_limits") != has_limits, "limitation_status_mismatch")
    relations, lineage_coverage = derive_verified_lineage_relations(
        search_units, evidence, layer_evidence,
    )
    relation_payload = "".join(
        canonical(record) + "\n" for record in relations
    ).encode("utf-8")
    generated_at = search_generated_at
    fail(
        not is_rfc3339_timestamp(generated_at),
        "lineage_validation_generated_at_invalid",
    )
    validation_state = {
        "schema_version": SCHEMA_VERSION,
        "validator": LINEAGE_VALIDATOR,
        "validator_version": LINEAGE_VALIDATOR_VERSION,
        "status": "pass",
        "question_independent": True,
        "generated_at": generated_at,
        "inputs": {
            "layer1_build_state_sha256": sha256_file(
                output / "layer1-intermediate" / "build-state.json"
            ),
            "layer1_documents_sha256": sha256_file(layer_documents_path),
            "layer1_evidence_sha256": sha256_file(layer_evidence_path),
            "layer1_relations_sha256": sha256_file(layer_relations_path),
            "search_build_state_sha256": sha256_file(search_state_path),
            "search_units_sha256": sha256_file(search_units_path),
            "semantic_documents_sha256": sha256_file(
                output / "semantic-documents.jsonl"
            ),
            "semantic_evidence_sha256": sha256_file(
                output / "semantic-evidence.jsonl"
            ),
            "document_source_set_sha256": record_source_set_sha256(
                documents, "document_id"
            ),
            "evidence_source_set_sha256": record_source_set_sha256(
                evidence, "evidence_id"
            ),
            "layer_evidence_source_set_sha256": record_source_set_sha256(
                layer_evidence, "evidence_id"
            ),
            "layer_relation_source_set_sha256": record_source_set_sha256(
                layer_relations, "relation_id"
            ),
            "search_unit_source_set_sha256": record_source_set_sha256(
                search_units, "search_unit_id"
            ),
        },
        "output": {
            "path": LINEAGE_RELATIONS_FILE,
            "sha256": hashlib.sha256(relation_payload).hexdigest(),
            "count": len(relations),
            "verified_relation_ids": [
                relation["relation_id"] for relation in relations
            ],
            "relation_source_set_sha256": record_source_set_sha256(
                relations, "relation_id"
            ),
        },
        "coverage": lineage_coverage,
    }
    _publish_lineage_artifacts(output, relations, validation_state)
    return {
        "status": "PASS",
        "build_status": state["status"],
        "documents": len(documents),
        "evidence": len(evidence),
        "selected_files": len(paths),
        "limitations": limitations,
        "image_quality_counts": dict(sorted(image_quality_counts.items())),
        "lineage_relations": len(relations),
        "structural_relations": len(expected_layer_relations),
        "lineage_held_derived": lineage_coverage["held_derived_count"],
        "lineage_artifacts": {
            "relations": LINEAGE_RELATIONS_FILE,
            "validation": LINEAGE_VALIDATION_FILE,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--inventory", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(validate(args.output_dir, args.source_root, args.inventory), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
