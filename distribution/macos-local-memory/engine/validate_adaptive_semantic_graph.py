#!/usr/bin/env python3
"""Validate the Layer 1 bridge boundary before content-security classification."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any


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
ADAPTER_NAME = "layer1-to-local-memory-evidence-adapter"
ADAPTER_VERSION = "0.6.0"
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
}
NATIVE_STRUCTURAL_RULE = "native containment"
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
        if line.strip()
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
        if source_record_type == "ocr_line":
            quality = layer_ocr_quality(record)
            if quality[0] == "provisional" and observed_text:
                observed_text = mark_provisional_text(observed_text)
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
        if unit_type not in {"table_row", "image_text_packet"}:
            continue
        document_id = unit.get("document_id")
        fail(document_id not in documents, "expected_search_unit_document_missing")
        source_evidence_ids = unit.get("source_evidence_ids")
        fail(
            not isinstance(source_evidence_ids, list) or not source_evidence_ids,
            "expected_search_unit_sources_invalid",
        )
        quality = (
            layer_image_packet_quality(unit, layer_evidence_by_id)
            if unit_type == "image_text_packet" else None
        )
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
            or heading.get("evidence_type") != "paragraph"
            or native_properties.get("preceding_heading_text")
            != heading.get("content", {}).get("raw_text"),
            f"native_structural_heading_binding_mismatch:{evidence_id}",
        )
        add(
            "section_contains",
            {"record_type": "evidence", "record_id": heading_id},
            to_ref,
        )

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
        if unit.get("unit_type") in {"table_row", "image_text_packet"}
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
            or provenance.get("deterministic") is not True
            or not is_rfc3339_timestamp(generated_at),
            f"lineage_search_unit_provenance_invalid:{search_unit_id}",
        )

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
    if agreement == "same_engine_agreement":
        fail(not numeric_overlap or overlap < 0.5, "layer_same_engine_overlap_invalid")
    else:
        fail(overlap != 0, "layer_single_pass_overlap_invalid")
    return expected_tier, [agreement], PROVISIONAL_OCR_MARKER


def layer_image_packet_quality(
    unit: dict[str, Any],
    evidence_by_id: dict[str, dict[str, Any]],
) -> tuple[str, list[str], str | None]:
    context = unit.get("context", {})
    fail(context.get("container_kind") != "standalone_image", "image_packet_container_invalid")
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
    lines = content_lines(str(search_text), packet=True)
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
        fail(
            item.get("provenance", {}).get("generated_at")
            != search_generated_at,
            f"lineage_search_unit_run_mismatch:{item['search_unit_id']}",
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
                source_unit, layer_evidence_by_id
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
