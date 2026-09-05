#!/usr/bin/env python3
"""Adapt verified Layer 1 intermediate records to Local Memory Evidence.

This is a one-way, question-independent boundary adapter.  It does not answer
questions, execute document instructions, or bypass the Local Memory content
security gate.  Its outputs must be classified before any answer index is
built.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from evidence_text_chunking import MAX_QUESTION_EVIDENCE_CHARS, exact_text_chunks
from intermediate_build_integrity import validate_managed_build_integrity
from validate_search_units import validate as validate_search_units


ADAPTER = "layer1-to-local-memory-evidence-adapter"
ADAPTER_VERSION = "0.7.0"
SCHEMA_VERSION = "0.1"
QUESTION_SHARD_VERSION = "question-evidence-shard-v1"
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


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:32]}"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_canonical(value: Any) -> str:
    return sha256_text(canonical(value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
    return records


def atomic_write(path: Path, value: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def text_from_content(content: dict[str, Any]) -> tuple[str, str]:
    if isinstance(content.get("raw_text"), str):
        return content["raw_text"], "raw_text"
    if "raw_value" in content:
        return canonical(content["raw_value"]), "canonical_raw_value"
    raise ValueError("Evidence content has neither raw_text nor raw_value")


def _marked_lines(text: str) -> list[str]:
    return [
        line for line in text.splitlines()
        if line.strip() and line.lstrip().startswith(PROVISIONAL_OCR_MARKER + " ")
    ]


def _mark_provisional_text(text: str) -> str:
    return "\n".join(
        line if line.lstrip().startswith(PROVISIONAL_OCR_MARKER + " ")
        else f"{PROVISIONAL_OCR_MARKER} {line}"
        for line in text.splitlines()
        if line.strip() and line.strip() != PROVISIONAL_OCR_MARKER
    )


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


def _question_shard_id(metadata: dict[str, Any]) -> str:
    return stable_id(
        "ev",
        {
            "adapter": ADAPTER,
            "adapter_version": ADAPTER_VERSION,
            "question_shard": metadata,
        },
    )


def validate_question_shard_reconstruction(
    source_projection: dict[str, Any],
    shards: list[dict[str, Any]],
) -> str:
    """Fail closed unless semantic shards reconstruct one projection exactly."""
    source_projection_id = source_projection.get("evidence_id")
    source_text = source_projection.get("observed_text")
    if not isinstance(source_projection_id, str) or not source_projection_id:
        raise ValueError("question shard source projection ID is invalid")
    if not isinstance(source_text, str) or len(source_text) <= MAX_QUESTION_EVIDENCE_CHARS:
        raise ValueError("question shard source text does not require sharding")
    if not shards:
        raise ValueError("question shard set is empty")

    source_projection_sha256 = sha256_canonical(source_projection)
    source_text_sha256 = sha256_text(source_text)
    provisional = source_projection.get("quality_tier") == "provisional"
    canonical_prefix = PROVISIONAL_OCR_MARKER + " "
    reconstructed: list[str] = []
    expected_start = 0
    seen_ids: set[str] = set()

    for expected_index, shard in enumerate(shards, 1):
        metadata = shard.get("adapter", {}).get("question_shard")
        if not isinstance(metadata, dict) or set(metadata) != QUESTION_SHARD_KEYS:
            raise ValueError("question shard metadata is invalid")
        if (
            metadata.get("version") != QUESTION_SHARD_VERSION
            or metadata.get("source_projection_id") != source_projection_id
            or metadata.get("source_projection_sha256") != source_projection_sha256
            or metadata.get("source_text_sha256") != source_text_sha256
            or metadata.get("chunk_index") != expected_index
            or metadata.get("chunk_count") != len(shards)
        ):
            raise ValueError("question shard lineage is inconsistent")
        start = metadata.get("character_start")
        end = metadata.get("character_end")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start != expected_start
            or end <= start
            or end > len(source_text)
        ):
            raise ValueError("question shard offsets are not contiguous")

        expected_payload = source_text[start:end]
        expected_prefix = (
            ""
            if not provisional or expected_payload.startswith(PROVISIONAL_OCR_MARKER)
            else canonical_prefix
        )
        prefix = metadata.get("observed_text_prefix")
        observed_text = shard.get("observed_text")
        if (
            prefix != expected_prefix
            or not isinstance(observed_text, str)
            or not observed_text.startswith(expected_prefix)
            or len(observed_text) > MAX_QUESTION_EVIDENCE_CHARS
        ):
            raise ValueError("question shard visible text is invalid")
        payload = observed_text[len(expected_prefix):]
        if payload != expected_payload or metadata.get("chunk_sha256") != sha256_text(payload):
            raise ValueError("question shard content does not match its source offset")
        if provisional and not observed_text.startswith(PROVISIONAL_OCR_MARKER):
            raise ValueError("provisional question shard is not visibly marked")

        shard_id = shard.get("evidence_id")
        if (
            not isinstance(shard_id, str)
            or shard_id in seen_ids
            or shard_id != _question_shard_id(metadata)
        ):
            raise ValueError("question shard ID is invalid")
        seen_ids.add(shard_id)

        restored = copy.deepcopy(shard)
        restored["evidence_id"] = source_projection_id
        restored["observed_text"] = source_text
        del restored["adapter"]["question_shard"]
        if restored != source_projection:
            raise ValueError("question shard did not preserve source projection metadata")

        reconstructed.append(payload)
        expected_start = end

    result = "".join(reconstructed)
    if expected_start != len(source_text) or result != source_text:
        raise ValueError("question shard reconstruction is incomplete")
    return result


def question_shards(projected: dict[str, Any]) -> list[dict[str, Any]]:
    """Replace one oversized semantic projection with exact question-sized shards."""
    observed_text = projected.get("observed_text")
    if not isinstance(observed_text, str):
        raise ValueError("semantic projection observed_text is invalid")
    if len(observed_text) <= MAX_QUESTION_EVIDENCE_CHARS:
        return [projected]

    source_projection_id = projected.get("evidence_id")
    if not isinstance(source_projection_id, str) or not source_projection_id:
        raise ValueError("semantic projection evidence_id is invalid")
    source_projection_sha256 = sha256_canonical(projected)
    source_text_sha256 = sha256_text(observed_text)
    provisional = projected.get("quality_tier") == "provisional"
    visible_prefix = PROVISIONAL_OCR_MARKER + " "
    payload_limit = (
        MAX_QUESTION_EVIDENCE_CHARS - len(visible_prefix)
        if provisional else MAX_QUESTION_EVIDENCE_CHARS
    )
    chunks = exact_text_chunks(observed_text, max_chars=payload_limit)
    shards: list[dict[str, Any]] = []
    for chunk_index, chunk in enumerate(chunks, 1):
        prefix = (
            ""
            if not provisional or chunk.text.startswith(PROVISIONAL_OCR_MARKER)
            else visible_prefix
        )
        metadata = {
            "version": QUESTION_SHARD_VERSION,
            "source_projection_id": source_projection_id,
            "source_projection_sha256": source_projection_sha256,
            "source_text_sha256": source_text_sha256,
            "character_start": chunk.start,
            "character_end": chunk.end,
            "chunk_index": chunk_index,
            "chunk_count": len(chunks),
            "chunk_sha256": sha256_text(chunk.text),
            "observed_text_prefix": prefix,
        }
        shard = copy.deepcopy(projected)
        shard["evidence_id"] = _question_shard_id(metadata)
        shard["observed_text"] = prefix + chunk.text
        shard["adapter"]["question_shard"] = metadata
        shards.append(shard)
    validate_question_shard_reconstruction(projected, shards)
    return shards


def ocr_evidence_quality(record: dict[str, Any]) -> tuple[str, list[str], str | None]:
    """Return validated quality metadata for one Layer 1 OCR Evidence record."""
    native = record.get("native_properties", {})
    agreement_type = native.get("agreement_type")
    expected_tier = OCR_QUALITY_BY_AGREEMENT.get(agreement_type)
    if expected_tier is None:
        raise ValueError(f"unsupported OCR agreement type: {agreement_type!r}")
    quality_tier = native.get("quality_tier")
    if quality_tier != expected_tier:
        raise ValueError(
            "OCR quality tier disagrees with agreement type: "
            f"{agreement_type!r} cannot be {quality_tier!r}"
        )
    marker = native.get("provisional_marker")
    marker_present = "provisional_marker" in native
    extraction_method = record.get("provenance", {}).get("extraction_method")
    overlap = native.get("spatial_overlap")
    bbox_coordinate_system = native.get("bbox_coordinate_system")
    if bbox_coordinate_system not in OCR_BBOX_COORDINATE_SYSTEMS:
        raise ValueError("OCR Evidence bbox coordinate system is invalid")
    numeric_overlap = isinstance(overlap, (int, float)) and not isinstance(overlap, bool)
    if quality_tier == "high":
        if marker_present:
            raise ValueError("high OCR Evidence must not carry a provisional marker")
        if native.get("independent_engines") is not True:
            raise ValueError("high OCR Evidence requires independent engine groups")
        if extraction_method != "dual_local_ocr_consensus":
            raise ValueError("high OCR Evidence requires dual-engine consensus provenance")
        if not numeric_overlap or overlap < 0.5:
            raise ValueError("high OCR Evidence requires spatial agreement")
        if bbox_coordinate_system != "source_orientation_1_top_left_normalized_1000":
            raise ValueError("high OCR Evidence requires the shared orientation-1 frame")
        return quality_tier, [agreement_type], None
    if marker != PROVISIONAL_OCR_MARKER:
        raise ValueError("provisional OCR Evidence lacks the canonical marker")
    if extraction_method != "adaptive_local_ocr_provisional":
        raise ValueError("provisional OCR Evidence has invalid extraction provenance")
    if agreement_type in {
        "same_engine_agreement",
        "display_transform_unresolved",
    }:
        if not numeric_overlap or overlap < 0.5:
            raise ValueError("multi-pass provisional OCR requires spatial overlap")
    elif overlap != 0:
        raise ValueError("single-pass provisional OCR must have zero overlap")
    return quality_tier, [agreement_type], PROVISIONAL_OCR_MARKER


def provisional_text_evidence_quality(
    record: dict[str, Any],
) -> tuple[str, str] | None:
    """Validate allowlisted provisional VLM text before semantic projection.

    The quality declaration lives in Layer 1 ``native_properties``.  Unknown
    VLM text methods and quality-bearing visual text types fail closed
    instead of silently losing their provisional status at this boundary.
    """
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
    if allowed_types is None:
        raise ValueError(f"unsupported VLM text method: {method!r}")
    if evidence_type not in allowed_types:
        raise ValueError(
            "provisional VLM text method is not allowed for Evidence type: "
            f"{method!r}/{evidence_type!r}"
        )
    if native.get("quality_tier") != "provisional":
        raise ValueError("provisional VLM text requires quality_tier='provisional'")
    if native.get("provisional_marker") != PROVISIONAL_OCR_MARKER:
        raise ValueError("provisional VLM text lacks the canonical marker")
    if native.get("question_independent") is not True:
        raise ValueError("provisional VLM text must be question-independent")
    if method == "local_vlm_unlocated_transcript_provisional":
        if (
            native.get("location_status") != "unlocated"
            or native.get("transcript_type")
            != "whole_image_faithful_transcript"
        ):
            raise ValueError("unlocated VLM transcript provenance is invalid")
        if "geometry" in record:
            raise ValueError("unlocated VLM transcript must not carry geometry")
    return "provisional", PROVISIONAL_OCR_MARKER


def image_packet_quality(unit: dict[str, Any]) -> tuple[str, list[str], str | None]:
    """Return validated quality metadata for a homogeneous image packet."""
    context = unit.get("context", {})
    if context.get("container_kind") not in IMAGE_PACKET_CONTAINER_KINDS:
        raise ValueError("image text packet container kind is invalid")
    if context.get("bbox_coordinate_system") not in OCR_BBOX_COORDINATE_SYSTEMS:
        raise ValueError("image text packet bbox coordinate system is invalid")
    if (
        context.get("reading_order_method") != "geometry_row_bands_v1"
        or not isinstance(context.get("row_band_count"), int)
        or isinstance(context.get("row_band_count"), bool)
        or context.get("row_band_count", 0) < 1
    ):
        raise ValueError("image text packet reading-order metadata is invalid")
    quality_tier = context.get("quality_tier")
    agreement_types = context.get("agreement_types")
    if (
        quality_tier not in {"high", "provisional"}
        or not isinstance(agreement_types, list)
        or not agreement_types
        or len(agreement_types) != len(set(agreement_types))
    ):
        raise ValueError("image text packet quality metadata is invalid")
    expected_tiers = {OCR_QUALITY_BY_AGREEMENT.get(value) for value in agreement_types}
    if expected_tiers != {quality_tier}:
        raise ValueError("image text packet mixes agreement quality tiers")
    marker = context.get("provisional_marker")
    marker_present = "provisional_marker" in context
    text = unit.get("text", {}).get("search_text", "")
    marked_lines = _marked_lines(text)
    packet_lines = text.splitlines()
    if packet_lines and packet_lines[0].startswith("Image file: "):
        packet_lines = packet_lines[1:]
    content_lines = [line for line in packet_lines if line.strip()]
    if context.get("row_band_count") != len(content_lines):
        raise ValueError("image text packet row-band count does not match its text")
    if quality_tier == "high":
        if marker_present or marked_lines:
            raise ValueError("high image text packet must not carry provisional markers")
        return quality_tier, agreement_types, None
    if marker != PROVISIONAL_OCR_MARKER:
        raise ValueError("provisional image text packet lacks the canonical marker")
    if not content_lines or any(
        not line.lstrip().startswith(PROVISIONAL_OCR_MARKER + " ")
        for line in content_lines
    ):
        raise ValueError("every provisional image packet line must be visibly marked")
    return quality_tier, agreement_types, PROVISIONAL_OCR_MARKER


def validate_source_binding(root: Path, source: dict[str, Any]) -> None:
    relative = source.get("relative_path")
    if not isinstance(relative, str) or not relative:
        raise ValueError("Document source has no relative_path")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"source escapes root: {relative}") from exc
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"source is not a regular file: {relative}")
    expected_hash = source.get("sha256")
    if not isinstance(expected_hash, str) or sha256_file(path) != expected_hash:
        raise ValueError(f"source hash mismatch: {relative}")
    if path.stat().st_size != source.get("size_bytes"):
        raise ValueError(f"source size mismatch: {relative}")


def adapt(
    intermediate: Path,
    source_root: Path,
    output: Path,
    search_output: Path | None = None,
) -> dict[str, Any]:
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    state_path = intermediate / "build-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("build_status") not in {"complete", "complete_with_failures"}:
        raise ValueError("Layer 1 intermediate build must have reached a terminal state")
    if Path(state.get("source_root", "")).resolve() != source_root:
        raise ValueError("source root does not match Layer 1 build state")
    validate_managed_build_integrity(intermediate, state)

    layer_documents = read_jsonl(intermediate / "documents.jsonl")
    layer_evidence = read_jsonl(intermediate / "evidence.jsonl")
    search_units: list[dict[str, Any]] = []
    if search_output is not None:
        validate_search_units(search_output, intermediate)
        search_units = read_jsonl(search_output / "search_units.jsonl")
    document_by_id: dict[str, dict[str, Any]] = {}
    for document in layer_documents:
        document_id = document.get("document_id")
        if not isinstance(document_id, str) or not document_id or document_id in document_by_id:
            raise ValueError(f"invalid or duplicate document_id: {document_id!r}")
        validate_source_binding(source_root, document["source"])
        document_by_id[document_id] = document
    state_document_ids = {
        entry.get("document_id") for entry in state.get("entries", {}).values()
    }
    if state_document_ids != set(document_by_id):
        raise ValueError("Document IDs do not match Layer 1 build state")

    evidence_by_document: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_evidence: set[str] = set()
    projections: list[dict[str, Any]] = []
    projection_methods: Counter[str] = Counter()
    question_shard_sources: Counter[str] = Counter()
    question_shard_count = 0
    skipped_binary_evidence = 0
    for record in layer_evidence:
        evidence_id = record.get("evidence_id")
        document_id = record.get("document_id")
        if not isinstance(evidence_id, str) or not evidence_id or evidence_id in seen_evidence:
            raise ValueError(f"invalid or duplicate evidence_id: {evidence_id!r}")
        if document_id not in document_by_id:
            raise ValueError(f"Evidence references missing document: {document_id}")
        seen_evidence.add(evidence_id)
        record_content = record.get("content", {})
        if isinstance(record_content.get("content_ref"), str):
            # Preserve the binary source binding in Layer 1, but do not turn a
            # path or opaque image payload into searchable text. Searchable
            # image content must arrive through separately audited OCR lines.
            skipped_binary_evidence += 1
            continue
        observed_text, projection_method = text_from_content(record_content)
        quality_metadata: tuple[str, list[str], str | None] | None = None
        provisional_text_quality: tuple[str, str] | None = None
        if record.get("evidence_type") == "ocr_line":
            quality_metadata = ocr_evidence_quality(record)
            quality_tier, _agreement_types, marker = quality_metadata
            if quality_tier == "provisional" and observed_text:
                observed_text = _mark_provisional_text(observed_text)
            elif marker is None and any(
                line.lstrip().startswith(PROVISIONAL_OCR_MARKER)
                for line in observed_text.splitlines()
            ):
                raise ValueError("high OCR text collides with the provisional marker")
        else:
            provisional_text_quality = provisional_text_evidence_quality(record)
            if provisional_text_quality is not None:
                observed_text = _mark_provisional_text(observed_text)
                if not observed_text:
                    raise ValueError("provisional VLM text is empty after marker normalization")
        projection_methods[projection_method] += 1
        source = document_by_id[document_id]["source"]
        projected = {
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
            "extraction_method": record.get("provenance", {}).get("extraction_method", "unknown"),
            "status": "observed",
            "adapter": {
                "name": ADAPTER,
                "version": ADAPTER_VERSION,
                "source_record_type": record.get("evidence_type"),
                "text_projection": projection_method,
                "execution_policy": "never_execute",
            },
        }
        if quality_metadata is not None:
            quality_tier, agreement_types, marker = quality_metadata
            projected["quality_tier"] = quality_tier
            projected["agreement_types"] = agreement_types
            projected["bbox_coordinate_system"] = record["native_properties"][
                "bbox_coordinate_system"
            ]
            if marker is not None:
                projected["provisional_marker"] = marker
        elif provisional_text_quality is not None:
            quality_tier, marker = provisional_text_quality
            projected["quality_tier"] = quality_tier
            projected["provisional_marker"] = marker
        geometry = record.get("geometry")
        if isinstance(geometry, dict) and geometry:
            projected["geometry"] = geometry
        projected_shards = question_shards(projected)
        if len(projected_shards) > 1:
            question_shard_sources[str(record.get("evidence_type", "unknown"))] += 1
            question_shard_count += len(projected_shards)
        for shard in projected_shards:
            shard_id = shard["evidence_id"]
            if shard_id != evidence_id:
                if shard_id in seen_evidence:
                    raise ValueError(f"question shard collides with Evidence ID: {shard_id}")
                seen_evidence.add(shard_id)
            evidence_by_document[document_id].append(shard)
            projections.append(shard)

    # SearchUnits are derived, question-independent groupings of verified
    # Evidence. Preserve table rows, native chart groupings, and audited image
    # text packets at this boundary because isolated cells/series/OCR lines
    # lose their relationships.
    # Every referenced Evidence ID has already been validated against the same
    # intermediate build by validate_search_units().
    search_unit_projection_count = 0
    image_quality_counts: Counter[str] = Counter()
    for unit in search_units:
        if unit.get("unit_type") not in {
            "table_row", "chart_summary", "chart_series", "image_text_packet",
        }:
            continue
        document_id = unit["document_id"]
        source_evidence_ids = unit["source_evidence_ids"]
        observed_text = unit["text"]["search_text"]
        image_quality: tuple[str, list[str], str | None] | None = None
        if unit["unit_type"] == "image_text_packet":
            image_quality = image_packet_quality(unit)
        evidence_id = stable_id("ev", {
            "adapter": ADAPTER,
            "adapter_version": ADAPTER_VERSION,
            "source_search_unit_id": unit["search_unit_id"],
            "document_id": document_id,
            "unit_type": unit["unit_type"],
            "source_evidence_ids": source_evidence_ids,
            "locator": unit["locator"],
            "text_sha256": unit["text"]["sha256"],
        })
        if evidence_id in seen_evidence:
            raise ValueError(f"projected SearchUnit collides with Evidence ID: {evidence_id}")
        seen_evidence.add(evidence_id)
        source = document_by_id[document_id]["source"]
        projected = {
            "schema_version": SCHEMA_VERSION,
            "evidence_id": evidence_id,
            "document_id": document_id,
            "ordinal": len(evidence_by_document[document_id]) + 1,
            "locator": unit["locator"],
            "observed_text": observed_text,
            "source": {
                "relative_path": source["relative_path"],
                "sha256": source["sha256"],
            },
            "extraction_method": "verified_search_unit_projection",
            "status": "observed",
            "adapter": {
                "name": ADAPTER,
                "version": ADAPTER_VERSION,
                "source_record_type": "search_unit",
                "source_search_unit_id": unit["search_unit_id"],
                "source_evidence_ids": source_evidence_ids,
                "unit_type": unit["unit_type"],
                "text_projection": "search_unit_text",
                "execution_policy": "never_execute",
            },
        }
        if image_quality is not None:
            quality_tier, agreement_types, marker = image_quality
            image_context = unit["context"]
            projected["quality_tier"] = quality_tier
            projected["agreement_types"] = agreement_types
            projected["bbox_coordinate_system"] = image_context["bbox_coordinate_system"]
            projected["reading_order_method"] = image_context["reading_order_method"]
            projected["row_band_count"] = image_context["row_band_count"]
            if marker is not None:
                projected["provisional_marker"] = marker
            image_quality_counts[quality_tier] += 1
        projected_shards = question_shards(projected)
        if len(projected_shards) > 1:
            question_shard_sources[f"search_unit:{unit['unit_type']}"] += 1
            question_shard_count += len(projected_shards)
        for shard in projected_shards:
            shard_id = shard["evidence_id"]
            if shard_id != evidence_id:
                if shard_id in seen_evidence:
                    raise ValueError(f"question shard collides with Evidence ID: {shard_id}")
                seen_evidence.add(shard_id)
            evidence_by_document[document_id].append(shard)
            projections.append(shard)
        projection_methods["search_unit_text"] += 1
        search_unit_projection_count += 1

    if any(
        len(item.get("observed_text", "")) > MAX_QUESTION_EVIDENCE_CHARS
        for item in projections
    ):
        raise RuntimeError("semantic question evidence exceeds the configured character cap")

    documents: list[dict[str, Any]] = []
    statuses: Counter[str] = Counter()
    for source_document in layer_documents:
        document_id = source_document["document_id"]
        source = source_document["source"]
        status = source_document.get("extraction", {}).get("status", "unknown")
        statuses[status] += 1
        extraction_failed = status == "failed"
        documents.append({
            "schema_version": SCHEMA_VERSION,
            "document_id": document_id,
            "source": {
                "relative_path": source["relative_path"],
                "absolute_path": str(source_root / source["relative_path"]),
                "sha256": source["sha256"],
                "size_bytes": source["size_bytes"],
                "file_type": source.get("extension") or "no_extension",
            },
            "classification": "unresolved" if extraction_failed else "extractable",
            "classification_reason": (
                "layer1_extraction_failed" if extraction_failed
                else "verified_layer1_intermediate_record"
            ),
            "project_id": None,
            "extraction_method": source_document.get("extraction", {}).get("parser", "unknown"),
            "status": (
                "extraction_failed" if extraction_failed
                else "extracted" if evidence_by_document.get(document_id)
                else "empty_after_extraction"
            ),
            "evidence_ids": [item["evidence_id"] for item in evidence_by_document.get(document_id, [])],
            "extraction_metadata": {
                "layer1_status": status,
                "layer1_parser_version": source_document.get("extraction", {}).get("parser_version"),
                "adapter": ADAPTER,
                "adapter_version": ADAPTER_VERSION,
            },
            "error": (
                "; ".join(str(item) for item in source_document.get("extraction", {}).get("errors", []))
                if extraction_failed else None
            ),
        })

    document_bytes = "".join(canonical(item) + "\n" for item in documents).encode("utf-8")
    evidence_bytes = "".join(canonical(item) + "\n" for item in projections).encode("utf-8")
    documents_path = output / "semantic-documents.jsonl"
    evidence_path = output / "semantic-evidence.jsonl"
    atomic_write(documents_path, document_bytes)
    atomic_write(evidence_path, evidence_bytes)
    result = {
        "schema_version": SCHEMA_VERSION,
        "adapter": ADAPTER,
        "adapter_version": ADAPTER_VERSION,
        "question_independent": True,
        "execution_policy": "never_execute",
        "requires_content_security_gate": True,
        "source_root": str(source_root),
        "source_state": {"path": str(state_path), "sha256": sha256_file(state_path)},
        "inputs": {
            "documents_sha256": sha256_file(intermediate / "documents.jsonl"),
            "evidence_sha256": sha256_file(intermediate / "evidence.jsonl"),
        },
        "outputs": {
            "documents": {"path": documents_path.name, "sha256": sha256_file(documents_path), "count": len(documents)},
            "evidence": {"path": evidence_path.name, "sha256": sha256_file(evidence_path), "count": len(projections)},
        },
        "layer1_status_counts": dict(sorted(statuses.items())),
        "text_projection_counts": dict(sorted(projection_methods.items())),
        "question_sharding": {
            "version": QUESTION_SHARD_VERSION,
            "max_observed_text_chars": MAX_QUESTION_EVIDENCE_CHARS,
            "source_projection_count": sum(question_shard_sources.values()),
            "shard_count": question_shard_count,
            "source_record_type_counts": dict(sorted(question_shard_sources.items())),
        },
        "search_unit_projection": {
            "enabled": search_output is not None,
            "included_unit_types": [
                "chart_series", "chart_summary", "image_text_packet", "table_row",
            ] if search_output is not None else [],
            "count": search_unit_projection_count,
            "search_state": ({
                "path": str(search_output / "search-build-state.json"),
                "sha256": sha256_file(search_output / "search-build-state.json"),
            } if search_output is not None else None),
            "search_units_sha256": (
                sha256_file(search_output / "search_units.jsonl") if search_output is not None else None
            ),
            "image_quality_counts": dict(sorted(image_quality_counts.items())),
        },
        "skipped_binary_evidence": skipped_binary_evidence,
    }
    atomic_write(
        output / "layer1-adapter-state.json",
        (json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intermediate", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--search-output", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    result = adapt(
        args.intermediate.resolve(strict=True),
        args.source_root.resolve(strict=True),
        args.out.resolve(),
        args.search_output.resolve(strict=True) if args.search_output is not None else None,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
