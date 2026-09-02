#!/usr/bin/env python3
"""Build a fully local SQLite semantic index from content Evidence JSONL."""

from __future__ import annotations

import argparse
import array
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import urllib.request
from datetime import datetime
from pathlib import Path


OLLAMA_EMBED_URL = "http://127.0.0.1:11434/api/embed"
MAX_EMBED_CHARS = 4_000
EMBEDDING_SPACE_PROBE_VERSION = "local-memory-embedding-space-v1"
EMBEDDING_SPACE_PROBE_TEXT = "local memory embedding space integrity probe v1"
INDEX_SCHEMA_VERSION = "0.3"
GRAPH_SCHEMA_VERSION = "0.1"
GRAPH_READY_STATUS = "validated_safe_partition"
GRAPH_SOURCE_SCHEMA_VERSION = "0.1"
GRAPH_NODE_STATUSES = {
    "observed", "proposed", "verified", "ambiguous", "rejected", "unresolved",
}
RELATION_CLASSES = {
    "structural", "version", "spatial", "semantic", "citation", "lineage",
    "comparison", "other",
}
RELATION_STATUSES = {"proposed", "verified", "rejected"}
EXPLICIT_STRUCTURAL_PRODUCERS = {
    ("intermediate-record-extractor", "0.7.0", "native containment"),
    ("intermediate-record-extractor", "0.8.0", "native containment"),
}
# ChartTable containment remains fail-closed until it has its own source-bound
# reconstruction contract; a producer-name allowlist alone is not attestation.
LINEAGE_VALIDATOR = "adaptive-semantic-lineage-validator"
LINEAGE_VALIDATOR_VERSION = "0.1.0"
LINEAGE_CONTRACT = "search-unit-source-lineage-v1"
EXPLICIT_LINEAGE_PRODUCERS = {
    (
        LINEAGE_VALIDATOR,
        LINEAGE_VALIDATOR_VERSION,
        "independent SearchUnit lineage reconstruction",
    ),
}
LINEAGE_PROPERTY_FIELDS = {
    "lineage_contract",
    "source_search_unit_id",
    "source_search_unit_sha256",
    "source_evidence_ordinal",
    "source_evidence_count",
    "fan_in_sha256",
    "derived_evidence_sha256",
}
LINEAGE_CONTEXT_FIELDS = {"output_dir", "source_root", "inventory"}
LINEAGE_STATE_FIELDS = {
    "schema_version", "validator", "validator_version", "status",
    "question_independent", "generated_at", "inputs", "output", "coverage",
}
LINEAGE_STATE_INPUT_FIELDS = {
    "layer1_build_state_sha256", "layer1_documents_sha256",
    "layer1_evidence_sha256", "layer1_relations_sha256",
    "search_build_state_sha256",
    "search_units_sha256", "semantic_documents_sha256",
    "semantic_evidence_sha256", "document_source_set_sha256",
    "evidence_source_set_sha256", "layer_evidence_source_set_sha256",
    "layer_relation_source_set_sha256", "search_unit_source_set_sha256",
}
LINEAGE_STATE_OUTPUT_FIELDS = {
    "path", "sha256", "count", "verified_relation_ids",
    "relation_source_set_sha256",
}
LINEAGE_COVERAGE_FIELDS = {
    "projected_search_unit_count", "source_reference_count",
    "eligible_derived_count", "verified_derived_count",
    "verified_relation_count", "held_derived_count",
    "held_source_reference_count", "held",
}
SECURITY_CONTEXT_FIELDS = {"gate_dir"}
SECURITY_ATTESTATION_FIELDS = {
    "state_sha256", "source_evidence_sha256", "source_documents_sha256",
    "output_sha256",
}
SECURITY_GATE_OUTPUT_FILES = {
    "content-security-classifications.jsonl",
    "content-security-documents.jsonl",
    "safe-answer-evidence.jsonl",
    "prompt-library-evidence.jsonl",
    "quarantine-evidence.jsonl",
    "content-security-exclusions.jsonl",
}
SECURITY_GRAPH_PARTITIONER = "content-security-graph-partitioner"
SECURITY_GRAPH_PARTITIONER_VERSION = "0.1.0"
SECURITY_GRAPH_PARTITION_FIELDS = {
    "schema_version", "partitioner", "partitioner_version", "status",
    "question_independent", "security_policy_version",
    "security_state_sha256", "safe_answer_evidence_sha256",
    "document_source_set_sha256", "evidence_source_set_sha256",
    "source_relation_set_sha256", "projected_relation_set_sha256",
    "promoted_relation_ids", "held_relations", "held_derived_evidence",
    "counts", "partition_sha256",
}
SECURITY_GRAPH_PARTITION_COUNT_FIELDS = {
    "source_relations", "promoted_relations", "held_relations",
    "safe_evidence", "held_derived_evidence",
}
SECURITY_HELD_RELATION_FIELDS = {
    "relation_id", "relation_class", "reason_codes",
    "excluded_evidence_ids", "source_relation_sha256",
}
SECURITY_HELD_DERIVED_FIELDS = {
    "evidence_id", "reason_codes", "excluded_source_evidence_ids",
}
GRAPH_RECORD_ID_PATTERNS = {
    "document": re.compile(r"^doc_[0-9a-f]{16,64}$"),
    "evidence": re.compile(r"^ev_[0-9a-f]{16,64}$"),
    "relation": re.compile(r"^rel_[0-9a-f]{16,64}$"),
}
RELATION_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
RFC3339_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?"
    r"(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
RELATION_REQUIRED_FIELDS = {
    "schema_version", "record_type", "relation_id", "relation_class",
    "relation_type", "from_ref", "to_ref", "provenance", "status",
}
RELATION_ALLOWED_FIELDS = RELATION_REQUIRED_FIELDS | {
    "properties", "supporting_evidence_ids",
}
RELATION_PROVENANCE_REQUIRED_FIELDS = {
    "generated_by", "generator_version", "generated_at", "deterministic",
    "confidence",
}
RELATION_PROVENANCE_ALLOWED_FIELDS = RELATION_PROVENANCE_REQUIRED_FIELDS | {
    "rule_or_model", "warnings",
}
SEMANTIC_DOCUMENT_REQUIRED_FIELDS = {
    "schema_version", "document_id", "source", "classification",
    "classification_reason", "project_id", "extraction_method", "status",
    "evidence_ids", "extraction_metadata", "error",
}
SEMANTIC_DOCUMENT_ALLOWED_FIELDS = SEMANTIC_DOCUMENT_REQUIRED_FIELDS
SEMANTIC_DOCUMENT_SOURCE_FIELDS = {
    "relative_path", "absolute_path", "sha256", "size_bytes", "file_type",
}
SEMANTIC_DOCUMENT_STATUSES = {
    "extracted", "empty_after_extraction", "extraction_failed", "source_changed",
}
SEMANTIC_EVIDENCE_REQUIRED_FIELDS = {
    "schema_version", "evidence_id", "document_id", "ordinal", "locator",
    "observed_text", "source", "extraction_method", "status",
}
SEMANTIC_EVIDENCE_ALLOWED_FIELDS = SEMANTIC_EVIDENCE_REQUIRED_FIELDS | {
    "adapter", "quality_tier", "agreement_types", "bbox_coordinate_system",
    "provisional_marker", "geometry", "reading_order_method", "row_band_count",
}
SEMANTIC_EVIDENCE_SOURCE_FIELDS = {"relative_path", "sha256"}
SEMANTIC_EVIDENCE_STATUSES = {"observed"}
SEMANTIC_EVIDENCE_ADAPTER_FIELDS = {
    "name", "version", "source_record_type", "text_projection",
    "execution_policy", "source_search_unit_id", "source_evidence_ids",
    "unit_type", "question_shard",
}
SEMANTIC_LOCATOR_FIELDS = {
    "page_number", "slide_number", "sheet_name", "cell", "range", "section",
    "paragraph_index", "table_index", "row_index", "column_index", "shape_id",
    "object_id", "object_index", "series_index", "notebook_cell_index",
    "code_line_start", "code_line_end", "source_member", "locator_text",
    "paragraph_start", "paragraph_end", "page", "slide", "chunk",
}
SEMANTIC_GEOMETRY_FIELDS = {
    "coordinate_space", "coordinate_origin", "unit", "x", "y", "width",
    "height", "rotation_deg",
}
QUESTION_SHARD_FIELDS = {
    "version", "source_projection_id", "source_projection_sha256",
    "source_text_sha256", "character_start", "character_end", "chunk_index",
    "chunk_count", "chunk_sha256", "observed_text_prefix",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def record_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def embed(model: str, texts: list[str], timeout: int) -> list[list[float]]:
    payload = json.dumps({"model": model, "input": texts}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        OLLAMA_EMBED_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        value = json.loads(response.read().decode("utf-8"))
    vectors = value.get("embeddings")
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise ValueError("embedding_count_mismatch")
    if not vectors or not all(isinstance(vector, list) and vector for vector in vectors):
        raise ValueError("empty_embedding")
    dimension = len(vectors[0])
    if any(len(vector) != dimension for vector in vectors):
        raise ValueError("embedding_dimension_mismatch")
    return vectors


def embedding_text(record: dict, relative_path: str) -> tuple[str, bool]:
    locator = record.get("locator", {})
    observed = str(record.get("observed_text", ""))
    prefix = (
        f"ファイル: {relative_path}\n"
        f"場所: {json.dumps(locator, ensure_ascii=False, sort_keys=True)}\n"
        "内容:\n"
    )
    remaining = max(0, MAX_EMBED_CHARS - len(prefix))
    return prefix + observed[:remaining], len(observed) > remaining


def require_complete_embedding_inputs(prepared: list[tuple]) -> None:
    """Refuse an index whose embeddings omit part of searchable Evidence."""
    truncated_count = sum(bool(item[2]) for item in prepared)
    if truncated_count:
        raise ValueError(
            "embedding_input_truncation_forbidden: semantic Evidence must be "
            f"question-sharded before indexing ({truncated_count} oversized record(s))"
        )


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=FULL;
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE evidence (
            evidence_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            locator_json TEXT NOT NULL,
            observed_text TEXT NOT NULL,
            embedding_text TEXT NOT NULL,
            embedding_input_truncated INTEGER NOT NULL CHECK (embedding_input_truncated IN (0, 1)),
            observed_sha256 TEXT NOT NULL
        );
        CREATE TABLE embeddings (
            evidence_id TEXT PRIMARY KEY REFERENCES evidence(evidence_id),
            dimension INTEGER NOT NULL,
            vector_f32 BLOB NOT NULL
        );
        CREATE TABLE graph_nodes (
            node_id TEXT PRIMARY KEY CHECK (length(trim(node_id)) > 0),
            node_type TEXT NOT NULL CHECK (length(trim(node_type)) > 0),
            payload_json TEXT NOT NULL CHECK (
                CASE WHEN json_valid(payload_json)
                    THEN json_type(payload_json) = 'object'
                    ELSE 0
                END
            ),
            status TEXT NOT NULL CHECK (
                status IN ('observed', 'proposed', 'verified', 'ambiguous', 'rejected', 'unresolved')
            ),
            record_sha256 TEXT NOT NULL CHECK (
                length(record_sha256) = 64
                AND record_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        );
        CREATE TABLE graph_edges (
            relation_id TEXT PRIMARY KEY CHECK (length(trim(relation_id)) > 0),
            from_node_id TEXT NOT NULL REFERENCES graph_nodes(node_id),
            relation_type TEXT NOT NULL CHECK (length(trim(relation_type)) > 0),
            to_node_id TEXT NOT NULL REFERENCES graph_nodes(node_id),
            relation_class TEXT NOT NULL CHECK (length(trim(relation_class)) > 0),
            basis_kind TEXT NOT NULL CHECK (
                basis_kind IN ('explicit', 'inference', 'hypothesis')
            ),
            basis_rule TEXT NOT NULL CHECK (length(trim(basis_rule)) > 0),
            basis_json TEXT NOT NULL CHECK (
                CASE WHEN json_valid(basis_json)
                    THEN json_type(basis_json) = 'object'
                    ELSE 0
                END
            ),
            properties_json TEXT NOT NULL CHECK (
                CASE WHEN json_valid(properties_json)
                    THEN json_type(properties_json) = 'object'
                    ELSE 0
                END
            ),
            status TEXT NOT NULL CHECK (
                status IN ('proposed', 'verified', 'rejected')
            ),
            record_sha256 TEXT NOT NULL CHECK (
                length(record_sha256) = 64
                AND record_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        );
        CREATE INDEX evidence_document_id_idx ON evidence(document_id);
        CREATE INDEX evidence_relative_path_idx ON evidence(relative_path);
        CREATE INDEX graph_nodes_type_status_idx
            ON graph_nodes(node_type, status);
        CREATE INDEX graph_edges_from_type_status_idx
            ON graph_edges(from_node_id, relation_type, status);
        CREATE INDEX graph_edges_to_type_status_idx
            ON graph_edges(to_node_id, relation_type, status);
        """
    )


def _records_by_id(
    records: list[dict], id_key: str, label: str,
) -> dict[str, dict]:
    indexed: dict[str, dict] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError(f"graph_{label}_record_invalid")
        record_id = record.get(id_key)
        if (
            not isinstance(record_id, str)
            or GRAPH_RECORD_ID_PATTERNS[label].fullmatch(record_id) is None
        ):
            raise ValueError(f"graph_{label}_id_invalid:{record_id!r}")
        if record_id in indexed:
            raise ValueError(f"graph_{label}_id_duplicate:{record_id}")
        indexed[record_id] = record
    return indexed


def _stable_relation_id(relation: dict) -> str:
    identity = {
        "class": relation.get("relation_class"),
        "type": relation.get("relation_type"),
        "from": relation.get("from_ref"),
        "to": relation.get("to_ref"),
        "generator": relation.get("provenance", {}).get("generated_by"),
        "generator_version": relation.get("provenance", {}).get("generator_version"),
    }
    return f"rel_{record_sha256(identity)[:32]}"


def _validate_relation_source(relation_id: str, relation: dict) -> None:
    missing_fields = sorted(RELATION_REQUIRED_FIELDS - relation.keys())
    extra_fields = sorted(relation.keys() - RELATION_ALLOWED_FIELDS)
    if missing_fields:
        raise ValueError(f"graph_relation_fields_missing:{relation_id}:{missing_fields}")
    if extra_fields:
        raise ValueError(f"graph_relation_fields_unexpected:{relation_id}:{extra_fields}")
    if relation.get("record_type") != "relation":
        raise ValueError(f"graph_relation_record_type_invalid:{relation_id}")
    if relation.get("schema_version") != GRAPH_SOURCE_SCHEMA_VERSION:
        raise ValueError(
            f"graph_relation_schema_version_invalid:{relation_id}:"
            f"{relation.get('schema_version')}"
        )

    relation_status = relation.get("status")
    relation_class = relation.get("relation_class")
    relation_type = relation.get("relation_type")
    if relation_status not in RELATION_STATUSES:
        raise ValueError(f"graph_relation_status_invalid:{relation_id}:{relation_status}")
    if relation_class not in RELATION_CLASSES:
        raise ValueError(f"graph_relation_class_invalid:{relation_id}:{relation_class}")
    if (
        not isinstance(relation_type, str)
        or RELATION_TYPE_PATTERN.fullmatch(relation_type) is None
    ):
        raise ValueError(f"graph_relation_type_invalid:{relation_id}:{relation_type}")

    for endpoint_name in ("from_ref", "to_ref"):
        reference = relation.get(endpoint_name)
        if not isinstance(reference, dict) or set(reference) != {"record_type", "record_id"}:
            raise ValueError(
                f"graph_relation_reference_invalid:{relation_id}:{endpoint_name}"
            )
        record_type = reference.get("record_type")
        record_id = reference.get("record_id")
        if (
            record_type not in {"document", "evidence"}
            or not isinstance(record_id, str)
            or GRAPH_RECORD_ID_PATTERNS[record_type].fullmatch(record_id) is None
        ):
            raise ValueError(
                f"graph_relation_reference_invalid:{relation_id}:{endpoint_name}"
            )

    properties = relation.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError(f"graph_relation_properties_invalid:{relation_id}")
    supporting_ids = relation.get("supporting_evidence_ids", [])
    if (
        not isinstance(supporting_ids, list)
        or any(
            not isinstance(value, str)
            or GRAPH_RECORD_ID_PATTERNS["evidence"].fullmatch(value) is None
            for value in supporting_ids
        )
        or len(supporting_ids) != len(set(supporting_ids))
    ):
        raise ValueError(f"graph_relation_support_invalid:{relation_id}")

    provenance = relation.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError(f"graph_relation_provenance_invalid:{relation_id}")
    missing_provenance = sorted(
        RELATION_PROVENANCE_REQUIRED_FIELDS - provenance.keys()
    )
    extra_provenance = sorted(
        provenance.keys() - RELATION_PROVENANCE_ALLOWED_FIELDS
    )
    if missing_provenance or extra_provenance:
        raise ValueError(
            f"graph_relation_provenance_fields_invalid:{relation_id}:"
            f"missing={missing_provenance}:extra={extra_provenance}"
        )
    if any(
        not isinstance(provenance.get(field), str)
        or not provenance[field].strip()
        for field in ("generated_by", "generator_version", "generated_at")
    ):
        raise ValueError(f"graph_relation_provenance_invalid:{relation_id}")
    generated_at = provenance["generated_at"]
    if RFC3339_PATTERN.fullmatch(generated_at) is None:
        raise ValueError(f"graph_relation_generated_at_invalid:{relation_id}")
    try:
        datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"graph_relation_generated_at_invalid:{relation_id}") from exc
    if not isinstance(provenance.get("deterministic"), bool):
        raise ValueError(f"graph_relation_provenance_invalid:{relation_id}")
    confidence = provenance.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        raise ValueError(f"graph_relation_confidence_invalid:{relation_id}")
    if "rule_or_model" in provenance and (
        not isinstance(provenance["rule_or_model"], str)
        or not provenance["rule_or_model"].strip()
    ):
        raise ValueError(f"graph_relation_rule_invalid:{relation_id}")
    warnings = provenance.get("warnings", [])
    if (
        not isinstance(warnings, list)
        or any(not isinstance(value, str) for value in warnings)
        or len(warnings) != len(set(warnings))
    ):
        raise ValueError(f"graph_relation_warnings_invalid:{relation_id}")

    expected_relation_id = _stable_relation_id(relation)
    if relation_id != expected_relation_id:
        raise ValueError(
            f"graph_relation_id_unstable:{relation_id}:{expected_relation_id}"
        )


def _record_source_set_sha256(records_by_id: dict[str, dict]) -> str:
    return record_sha256([
        records_by_id[record_id] for record_id in sorted(records_by_id)
    ])


def _read_jsonl_records(path: Path) -> list[dict]:
    records: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"graph_lineage_artifact_json_invalid:{path.name}:{line_number}"
                ) from exc
            if not isinstance(record, dict):
                raise ValueError(
                    f"graph_lineage_artifact_record_invalid:{path.name}:{line_number}"
                )
            records.append(record)
    return records


def _read_jsonl_snapshot(path: Path) -> tuple[bytes, list[dict]]:
    """Read one immutable byte snapshot and decode records from that snapshot."""
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"graph_security_artifact_utf8_invalid:{path.name}") from exc
    records: list[dict] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"graph_security_artifact_json_invalid:{path.name}:{line_number}"
            ) from exc
        if not isinstance(record, dict):
            raise ValueError(
                f"graph_security_artifact_record_invalid:{path.name}:{line_number}"
            )
        records.append(record)
    return data, records


def _load_lineage_validator():
    validator_path = Path(__file__).resolve().with_name(
        "validate_adaptive_semantic_graph.py"
    )
    specification = importlib.util.spec_from_file_location(
        "local_memory_lineage_attestation", validator_path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("graph_lineage_validator_unavailable")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _load_content_security_validator():
    validator_path = Path(__file__).resolve().with_name(
        "validate_content_security_gate.py"
    )
    specification = importlib.util.spec_from_file_location(
        "local_memory_content_security_attestation", validator_path,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("graph_content_security_validator_unavailable")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _is_explicit_verified_structural(relation: dict) -> bool:
    if (
        relation.get("relation_class") != "structural"
        or relation.get("status") != "verified"
    ):
        return False
    provenance = relation.get("provenance")
    if not isinstance(provenance, dict):
        return False
    producer = (
        provenance.get("generated_by"),
        provenance.get("generator_version"),
        provenance.get("rule_or_model"),
    )
    return (
        provenance.get("deterministic") is True
        and producer in EXPLICIT_STRUCTURAL_PRODUCERS
    )


def _is_document_containment_bound_by_evidence(
    relation: dict,
    evidence_by_id: dict[str, dict],
) -> bool:
    """Recognize the one structural edge derivable without Layer 1 context."""
    if not _is_document_containment_shape(relation):
        return False
    from_ref = relation.get("from_ref")
    to_ref = relation.get("to_ref")
    evidence = evidence_by_id.get(to_ref.get("record_id"))
    return (
        isinstance(evidence, dict)
        and evidence.get("document_id") == from_ref.get("record_id")
    )


def _is_document_containment_shape(relation: dict) -> bool:
    from_ref = relation.get("from_ref")
    to_ref = relation.get("to_ref")
    return (
        relation.get("relation_type") == "contains"
        and isinstance(from_ref, dict)
        and isinstance(to_ref, dict)
        and from_ref.get("record_type") == "document"
        and to_ref.get("record_type") == "evidence"
    )


def _document_containment_metadata_is_safe(relation: dict) -> bool:
    provenance = relation.get("provenance", {})
    return (
        relation.get("properties", {}) == {}
        and relation.get("supporting_evidence_ids", []) == []
        and isinstance(provenance, dict)
        and provenance.get("confidence") == 1.0
        and provenance.get("warnings", []) == []
    )


def _attest_lineage_context(
    documents: list[dict],
    evidence_records: list[dict],
    relations: list[dict],
    context: dict | None,
) -> dict:
    """Re-run the source-bound validator; never trust a caller-issued PASS JSON."""
    if not isinstance(context, dict) or set(context) != LINEAGE_CONTEXT_FIELDS:
        raise ValueError("graph_lineage_validation_context_required")
    try:
        output_dir = Path(context["output_dir"]).resolve(strict=True)
        source_root = Path(context["source_root"]).resolve(strict=True)
        inventory = Path(context["inventory"]).resolve(strict=True)
    except (TypeError, OSError) as exc:
        raise ValueError("graph_lineage_validation_context_invalid") from exc
    if not output_dir.is_dir() or output_dir.is_symlink():
        raise ValueError("graph_lineage_output_directory_invalid")

    validator = _load_lineage_validator()
    report = validator.validate(output_dir, source_root, inventory)
    if report.get("status") != "PASS":
        raise ValueError("graph_lineage_independent_validation_failed")

    state_path = output_dir / validator.LINEAGE_VALIDATION_FILE
    relation_path = output_dir / validator.LINEAGE_RELATIONS_FILE
    state = json.loads(state_path.read_text(encoding="utf-8"))
    attested_relations = _read_jsonl_records(relation_path)
    attested_documents = _read_jsonl_records(output_dir / "semantic-documents.jsonl")
    attested_evidence = _read_jsonl_records(output_dir / "semantic-evidence.jsonl")
    supplied_documents = _records_by_id(documents, "document_id", "document")
    supplied_evidence = _records_by_id(
        evidence_records, "evidence_id", "evidence",
    )
    attested_document_by_id = _records_by_id(
        attested_documents, "document_id", "document",
    )
    attested_evidence_by_id = _records_by_id(
        attested_evidence, "evidence_id", "evidence",
    )
    if supplied_documents != attested_document_by_id:
        raise ValueError("graph_lineage_attested_documents_mismatch")
    if supplied_evidence != attested_evidence_by_id:
        raise ValueError("graph_lineage_attested_evidence_mismatch")

    supplied_verified_lineage = {
        relation["relation_id"]: relation
        for relation in relations
        if relation.get("relation_class") == "lineage"
        and relation.get("status") == "verified"
    }
    attested_relation_by_id = _records_by_id(
        attested_relations, "relation_id", "relation",
    )
    if supplied_verified_lineage != attested_relation_by_id:
        raise ValueError("graph_lineage_attested_relations_mismatch")

    layer_documents = _read_jsonl_records(
        output_dir / "layer1-intermediate" / "documents.jsonl"
    )
    layer_evidence = _read_jsonl_records(
        output_dir / "layer1-intermediate" / "evidence.jsonl"
    )
    intermediate_state = json.loads(
        (output_dir / "layer1-intermediate" / "build-state.json").read_text(
            encoding="utf-8"
        )
    )
    reconstructed_structural = validator.derive_native_structural_relations(
        layer_documents, layer_evidence, intermediate_state,
    )
    attested_structural = {
        relation["relation_id"]: relation
        for relation in reconstructed_structural
    }
    supplied_verified_structural = {
        relation["relation_id"]: relation
        for relation in relations
        if _is_explicit_verified_structural(relation)
    }
    if supplied_verified_structural != attested_structural:
        raise ValueError("graph_structural_attested_relations_mismatch")

    expected_input_files = {
        "layer1_build_state_sha256": output_dir / "layer1-intermediate" / "build-state.json",
        "layer1_documents_sha256": output_dir / "layer1-intermediate" / "documents.jsonl",
        "layer1_evidence_sha256": output_dir / "layer1-intermediate" / "evidence.jsonl",
        "layer1_relations_sha256": output_dir / "layer1-intermediate" / "relations.jsonl",
        "search_build_state_sha256": output_dir / "layer1-search" / "search-build-state.json",
        "search_units_sha256": output_dir / "layer1-search" / "search_units.jsonl",
        "semantic_documents_sha256": output_dir / "semantic-documents.jsonl",
        "semantic_evidence_sha256": output_dir / "semantic-evidence.jsonl",
    }
    inputs = state.get("inputs", {})
    for field, path in expected_input_files.items():
        if inputs.get(field) != sha256_file(path):
            raise ValueError(f"graph_lineage_attestation_input_hash_mismatch:{field}")
    search_units = _read_jsonl_records(
        output_dir / "layer1-search" / "search_units.jsonl"
    )
    if inputs.get("layer_evidence_source_set_sha256") != validator.record_source_set_sha256(
        layer_evidence, "evidence_id",
    ):
        raise ValueError("graph_lineage_attestation_layer_evidence_set_mismatch")
    layer_relations = _read_jsonl_records(
        output_dir / "layer1-intermediate" / "relations.jsonl"
    )
    if inputs.get("layer_relation_source_set_sha256") != validator.record_source_set_sha256(
        layer_relations, "relation_id",
    ):
        raise ValueError("graph_lineage_attestation_layer_relation_set_mismatch")
    if inputs.get("search_unit_source_set_sha256") != validator.record_source_set_sha256(
        search_units, "search_unit_id",
    ):
        raise ValueError("graph_lineage_attestation_search_unit_set_mismatch")
    output = state.get("output", {})
    if output.get("path") != validator.LINEAGE_RELATIONS_FILE:
        raise ValueError("graph_lineage_attestation_output_path_invalid")
    if output.get("sha256") != sha256_file(relation_path):
        raise ValueError("graph_lineage_attestation_output_hash_mismatch")
    return state


def _partition_security_graph(
    documents: list[dict],
    evidence_records: list[dict],
    safe_evidence_records: list[dict],
    relations: list[dict],
    lineage_state: dict,
    security_state: dict,
    gate_dir: Path,
    *,
    security_state_sha256: str | None = None,
    safe_answer_evidence_sha256: str | None = None,
) -> tuple[list[dict], list[dict], dict]:
    """Hold only unsafe explicit branches while preserving complete fan-ins."""
    document_by_id = _records_by_id(documents, "document_id", "document")
    evidence_by_id = _records_by_id(
        evidence_records, "evidence_id", "evidence",
    )
    safe_evidence_by_id = _records_by_id(
        safe_evidence_records, "evidence_id", "evidence",
    )
    relation_by_id = _records_by_id(relations, "relation_id", "relation")
    for relation_id, relation in sorted(relation_by_id.items()):
        _validate_relation_source(relation_id, relation)
    for evidence_id, record in safe_evidence_by_id.items():
        if evidence_by_id.get(evidence_id) != record:
            raise ValueError(
                f"graph_security_safe_evidence_source_mismatch:{evidence_id}"
            )

    lineage_by_id = {
        relation_id: relation
        for relation_id, relation in relation_by_id.items()
        if relation.get("relation_class") == "lineage"
        and relation.get("status") == "verified"
    }
    _validate_lineage_validation_state(
        lineage_state, document_by_id, evidence_by_id, lineage_by_id,
    )
    if lineage_by_id:
        _validate_explicit_lineage_relations(lineage_by_id, evidence_by_id)

    lineage_groups: dict[str, list[dict]] = {}
    for relation in lineage_by_id.values():
        derived_id = relation["from_ref"]["record_id"]
        lineage_groups.setdefault(derived_id, []).append(relation)
    for group in lineage_groups.values():
        group.sort(key=lambda item: item["relation_id"])

    safe_ids = set(safe_evidence_by_id)
    held_reasons: dict[str, set[str]] = {}
    held_excluded_sources: dict[str, set[str]] = {}
    for entry in lineage_state["coverage"]["held"]:
        if not isinstance(entry, dict):
            raise ValueError("graph_security_upstream_hold_invalid")
        derived_ids = entry.get("derived_evidence_ids")
        reasons = entry.get("reasons")
        unresolved_ids = entry.get("unresolved_source_evidence_ids")
        if (
            not isinstance(derived_ids, list)
            or not derived_ids
            or any(value not in evidence_by_id for value in derived_ids)
            or len(derived_ids) != len(set(derived_ids))
            or not isinstance(reasons, list)
            or not reasons
            or any(not isinstance(value, str) or not value for value in reasons)
            or not isinstance(unresolved_ids, list)
            or any(not isinstance(value, str) or not value for value in unresolved_ids)
        ):
            raise ValueError("graph_security_upstream_hold_invalid")
        for derived_id in derived_ids:
            if derived_id in lineage_groups:
                raise ValueError(
                    f"graph_security_lineage_promoted_and_upstream_held:{derived_id}"
                )
            held_reasons.setdefault(derived_id, set()).add(
                "upstream_semantic_lineage_held"
            )
            held_excluded_sources.setdefault(derived_id, set()).update(
                unresolved_ids
            )

    for derived_id, group in lineage_groups.items():
        source_ids = {
            relation["to_ref"]["record_id"] for relation in group
        }
        excluded_source_ids = source_ids - safe_ids
        if derived_id not in safe_ids:
            held_reasons.setdefault(derived_id, set()).add(
                "derived_not_answer_eligible"
            )
        if excluded_source_ids:
            held_reasons.setdefault(derived_id, set()).add(
                "source_not_answer_eligible"
            )
            held_excluded_sources.setdefault(derived_id, set()).update(
                excluded_source_ids
            )

    changed = True
    while changed:
        changed = False
        held_ids = set(held_reasons)
        for derived_id, group in lineage_groups.items():
            upstream_ids = {
                relation["to_ref"]["record_id"] for relation in group
            } & held_ids
            if not upstream_ids:
                continue
            reasons = held_reasons.setdefault(derived_id, set())
            before = len(reasons)
            reasons.add("upstream_lineage_held")
            held_excluded_sources.setdefault(derived_id, set()).update(
                upstream_ids
            )
            if len(reasons) != before:
                changed = True

    held_derived_ids = set(held_reasons)
    retained_document_ids = {
        record["document_id"] for record in safe_evidence_by_id.values()
    }
    missing_documents = retained_document_ids - set(document_by_id)
    if missing_documents:
        raise ValueError(
            "graph_security_safe_evidence_document_missing:"
            f"{sorted(missing_documents)[:8]}"
        )
    filtered_documents = [
        document_by_id[document_id]
        for document_id in sorted(retained_document_ids)
    ]

    projected_relation_by_id: dict[str, dict] = {}
    promoted_relation_ids: list[str] = []
    held_relations: list[dict] = []
    source_explicit_ids: set[str] = set()
    for relation_id, relation in sorted(relation_by_id.items()):
        is_lineage = (
            relation.get("relation_class") == "lineage"
            and relation.get("status") == "verified"
        )
        is_structural = _is_explicit_verified_structural(relation)
        if not (is_lineage or is_structural):
            projected_relation_by_id[relation_id] = relation
            continue
        source_explicit_ids.add(relation_id)

        reason_codes: set[str] = set()
        referenced_evidence_ids = {
            reference["record_id"]
            for reference in (relation["from_ref"], relation["to_ref"])
            if reference["record_type"] == "evidence"
        }
        referenced_evidence_ids.update(
            relation.get("supporting_evidence_ids", [])
        )
        excluded_ids = referenced_evidence_ids - safe_ids
        held_endpoint_ids = referenced_evidence_ids & held_derived_ids
        if is_lineage:
            derived_id = relation["from_ref"]["record_id"]
            if derived_id in held_derived_ids:
                reason_codes.update(held_reasons[derived_id])
                excluded_ids.update(
                    held_excluded_sources.get(derived_id, set())
                )
                excluded_ids.update(held_endpoint_ids)
        else:
            if excluded_ids:
                reason_codes.add("evidence_not_answer_eligible")
            if held_endpoint_ids:
                reason_codes.add("incident_to_held_derived")
                excluded_ids.update(held_endpoint_ids)
            referenced_document_ids = {
                reference["record_id"]
                for reference in (relation["from_ref"], relation["to_ref"])
                if reference["record_type"] == "document"
            }
            if not referenced_document_ids <= retained_document_ids:
                reason_codes.add("document_without_safe_evidence")

        if reason_codes:
            held_relations.append({
                "relation_id": relation_id,
                "relation_class": relation["relation_class"],
                "reason_codes": sorted(reason_codes),
                "excluded_evidence_ids": sorted(excluded_ids),
                "source_relation_sha256": record_sha256(relation),
            })
        else:
            projected_relation_by_id[relation_id] = relation
            promoted_relation_ids.append(relation_id)

    held_relation_ids = {item["relation_id"] for item in held_relations}
    if (
        held_relation_ids & set(promoted_relation_ids)
        or source_explicit_ids
        != held_relation_ids | set(promoted_relation_ids)
    ):
        raise ValueError("graph_security_relation_partition_incomplete")

    held_derived = [
        {
            "evidence_id": evidence_id,
            "reason_codes": sorted(held_reasons[evidence_id]),
            "excluded_source_evidence_ids": sorted(
                held_excluded_sources.get(evidence_id, set())
            ),
        }
        for evidence_id in sorted(held_derived_ids)
    ]
    filtered_document_by_id = {
        item["document_id"]: item for item in filtered_documents
    }
    resolved_security_state_sha256 = (
        security_state_sha256
        if security_state_sha256 is not None
        else sha256_file(gate_dir / "content-security-state.json")
    )
    resolved_safe_evidence_sha256 = (
        safe_answer_evidence_sha256
        if safe_answer_evidence_sha256 is not None
        else sha256_file(gate_dir / "safe-answer-evidence.jsonl")
    )
    for field, value in {
        "security_state_sha256": resolved_security_state_sha256,
        "safe_answer_evidence_sha256": resolved_safe_evidence_sha256,
    }.items():
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError(f"graph_security_attestation_hash_invalid:{field}")
    partition = {
        "schema_version": GRAPH_SOURCE_SCHEMA_VERSION,
        "partitioner": SECURITY_GRAPH_PARTITIONER,
        "partitioner_version": SECURITY_GRAPH_PARTITIONER_VERSION,
        "status": "pass",
        "question_independent": True,
        "security_policy_version": security_state.get("policy_version"),
        "security_state_sha256": resolved_security_state_sha256,
        "safe_answer_evidence_sha256": resolved_safe_evidence_sha256,
        "document_source_set_sha256": _record_source_set_sha256(
            filtered_document_by_id
        ),
        "evidence_source_set_sha256": _record_source_set_sha256(
            safe_evidence_by_id
        ),
        "source_relation_set_sha256": _record_source_set_sha256(
            relation_by_id
        ),
        "projected_relation_set_sha256": _record_source_set_sha256(
            projected_relation_by_id
        ),
        "promoted_relation_ids": sorted(promoted_relation_ids),
        "held_relations": held_relations,
        "held_derived_evidence": held_derived,
        "counts": {
            "source_relations": len(relation_by_id),
            "promoted_relations": len(promoted_relation_ids),
            "held_relations": len(held_relations),
            "safe_evidence": len(safe_evidence_by_id),
            "held_derived_evidence": len(held_derived),
        },
    }
    partition["partition_sha256"] = record_sha256(partition)
    return (
        filtered_documents,
        [projected_relation_by_id[value] for value in sorted(projected_relation_by_id)],
        partition,
    )


def _attest_security_context(
    documents: list[dict],
    evidence_records: list[dict],
    relations: list[dict],
    lineage_context: dict | None,
    security_context: dict | None,
) -> dict:
    """Re-run semantic and security validators before safe graph partitioning."""
    if (
        not isinstance(security_context, dict)
        or set(security_context) != SECURITY_CONTEXT_FIELDS
    ):
        raise ValueError("graph_security_validation_context_required")
    if not isinstance(lineage_context, dict) or set(lineage_context) != LINEAGE_CONTEXT_FIELDS:
        raise ValueError("graph_security_lineage_context_required")
    try:
        output_dir = Path(lineage_context["output_dir"]).resolve(strict=True)
        gate_dir = Path(security_context["gate_dir"]).resolve(strict=True)
    except (TypeError, OSError) as exc:
        raise ValueError("graph_security_validation_context_invalid") from exc
    if not gate_dir.is_dir():
        raise ValueError("graph_security_gate_directory_invalid")

    full_documents = _read_jsonl_records(
        output_dir / "semantic-documents.jsonl"
    )
    full_evidence = _read_jsonl_records(
        output_dir / "semantic-evidence.jsonl"
    )
    lineage_state = _attest_lineage_context(
        full_documents, full_evidence, relations, lineage_context,
    )
    supplied_documents = _records_by_id(documents, "document_id", "document")
    full_document_by_id = _records_by_id(
        full_documents, "document_id", "document",
    )
    if supplied_documents != full_document_by_id:
        raise ValueError("graph_security_attested_documents_mismatch")

    security_validator = _load_content_security_validator()
    security_report = security_validator.validate(
        output_dir / "semantic-evidence.jsonl",
        output_dir / "semantic-documents.jsonl",
        gate_dir,
    )
    if security_report.get("status") != "PASS":
        raise ValueError("graph_security_independent_validation_failed")
    security_attestation = security_report.get("attestation")
    if (
        not isinstance(security_attestation, dict)
        or set(security_attestation) != SECURITY_ATTESTATION_FIELDS
        or not isinstance(security_attestation.get("output_sha256"), dict)
    ):
        raise ValueError("graph_security_attestation_invalid")
    for field in SECURITY_ATTESTATION_FIELDS - {"output_sha256"}:
        value = security_attestation.get(field)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError(f"graph_security_attestation_hash_invalid:{field}")
    output_sha256 = security_attestation["output_sha256"]
    if (
        set(output_sha256) != SECURITY_GATE_OUTPUT_FILES
        or any(
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in output_sha256.values()
        )
    ):
        raise ValueError("graph_security_output_attestation_invalid")

    security_state_bytes = (
        gate_dir / "content-security-state.json"
    ).read_bytes()
    if hashlib.sha256(security_state_bytes).hexdigest() != security_attestation[
        "state_sha256"
    ]:
        raise ValueError("graph_security_state_changed_after_validation")
    security_state = json.loads(security_state_bytes.decode("utf-8"))
    safe_evidence_bytes, safe_evidence = _read_jsonl_snapshot(
        gate_dir / "safe-answer-evidence.jsonl"
    )
    safe_evidence_sha256 = hashlib.sha256(safe_evidence_bytes).hexdigest()
    if safe_evidence_sha256 != output_sha256["safe-answer-evidence.jsonl"]:
        raise ValueError("graph_security_safe_evidence_changed_after_validation")
    supplied_evidence = _records_by_id(
        evidence_records, "evidence_id", "evidence",
    )
    safe_evidence_by_id = _records_by_id(
        safe_evidence, "evidence_id", "evidence",
    )
    if supplied_evidence != safe_evidence_by_id:
        raise ValueError("graph_security_attested_evidence_mismatch")

    filtered_documents, filtered_relations, partition = _partition_security_graph(
        full_documents,
        full_evidence,
        safe_evidence,
        relations,
        lineage_state,
        security_state,
        gate_dir,
        security_state_sha256=security_attestation["state_sha256"],
        safe_answer_evidence_sha256=safe_evidence_sha256,
    )
    return {
        "documents": filtered_documents,
        "evidence_records": safe_evidence,
        "relations": filtered_relations,
        "source_relations": relations,
        "lineage_validation": lineage_state,
        "partition": partition,
    }


def _validate_security_graph_partition(
    partition: dict | None,
    document_by_id: dict[str, dict],
    evidence_by_id: dict[str, dict],
    relation_by_id: dict[str, dict],
    source_relation_by_id: dict[str, dict] | None,
) -> None:
    """Bind the internally attested partition to the exact projected subset."""
    if not isinstance(partition, dict) or set(partition) != SECURITY_GRAPH_PARTITION_FIELDS:
        raise ValueError("graph_security_partition_invalid")
    if source_relation_by_id is None:
        raise ValueError("graph_security_source_relations_required")
    for relation_id, relation in sorted(source_relation_by_id.items()):
        _validate_relation_source(relation_id, relation)
    if (
        partition.get("schema_version") != GRAPH_SOURCE_SCHEMA_VERSION
        or partition.get("partitioner") != SECURITY_GRAPH_PARTITIONER
        or partition.get("partitioner_version") != SECURITY_GRAPH_PARTITIONER_VERSION
        or partition.get("status") != "pass"
        or partition.get("question_independent") is not True
        or not isinstance(partition.get("security_policy_version"), str)
        or not partition["security_policy_version"]
    ):
        raise ValueError("graph_security_partition_identity_invalid")
    for field in {
        "security_state_sha256", "safe_answer_evidence_sha256",
        "document_source_set_sha256", "evidence_source_set_sha256",
        "source_relation_set_sha256", "projected_relation_set_sha256",
        "partition_sha256",
    }:
        value = partition.get(field)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError(f"graph_security_partition_hash_invalid:{field}")
    hash_input = dict(partition)
    supplied_partition_hash = hash_input.pop("partition_sha256")
    if record_sha256(hash_input) != supplied_partition_hash:
        raise ValueError("graph_security_partition_self_hash_mismatch")
    if partition["document_source_set_sha256"] != _record_source_set_sha256(
        document_by_id
    ):
        raise ValueError("graph_security_partition_document_set_mismatch")
    if partition["evidence_source_set_sha256"] != _record_source_set_sha256(
        evidence_by_id
    ):
        raise ValueError("graph_security_partition_evidence_set_mismatch")
    if partition["projected_relation_set_sha256"] != _record_source_set_sha256(
        relation_by_id
    ):
        raise ValueError("graph_security_partition_relation_set_mismatch")
    if partition["source_relation_set_sha256"] != _record_source_set_sha256(
        source_relation_by_id
    ):
        raise ValueError("graph_security_partition_source_relation_set_mismatch")

    promoted_ids = partition.get("promoted_relation_ids")
    held_relations = partition.get("held_relations")
    held_derived = partition.get("held_derived_evidence")
    counts = partition.get("counts")
    if (
        not isinstance(promoted_ids, list)
        or promoted_ids != sorted(set(promoted_ids))
        or not isinstance(held_relations, list)
        or not isinstance(held_derived, list)
        or not isinstance(counts, dict)
        or set(counts) != SECURITY_GRAPH_PARTITION_COUNT_FIELDS
        or any(
            not isinstance(counts[field], int)
            or isinstance(counts[field], bool)
            or counts[field] < 0
            for field in SECURITY_GRAPH_PARTITION_COUNT_FIELDS
        )
    ):
        raise ValueError("graph_security_partition_counts_invalid")

    held_relation_ids: list[str] = []
    for entry in held_relations:
        if (
            not isinstance(entry, dict)
            or set(entry) != SECURITY_HELD_RELATION_FIELDS
            or entry.get("relation_class") not in {"structural", "lineage"}
            or not isinstance(entry.get("relation_id"), str)
            or not isinstance(entry.get("reason_codes"), list)
            or entry["reason_codes"] != sorted(set(entry["reason_codes"]))
            or not entry["reason_codes"]
            or not isinstance(entry.get("excluded_evidence_ids"), list)
            or entry["excluded_evidence_ids"]
            != sorted(set(entry["excluded_evidence_ids"]))
            or re.fullmatch(
                r"[0-9a-f]{64}", str(entry.get("source_relation_sha256", ""))
            ) is None
        ):
            raise ValueError("graph_security_held_relation_invalid")
        held_relation_ids.append(entry["relation_id"])
    if held_relation_ids != sorted(set(held_relation_ids)):
        raise ValueError("graph_security_held_relation_ids_invalid")
    if set(source_relation_by_id) != set(relation_by_id) | set(held_relation_ids):
        raise ValueError("graph_security_source_relation_partition_incomplete")
    for relation_id, relation in relation_by_id.items():
        if source_relation_by_id.get(relation_id) != relation:
            raise ValueError(
                f"graph_security_projected_relation_source_mismatch:{relation_id}"
            )
    held_relation_by_id = {
        entry["relation_id"]: entry for entry in held_relations
    }
    for relation_id, entry in held_relation_by_id.items():
        source_relation = source_relation_by_id.get(relation_id)
        if (
            source_relation is None
            or source_relation.get("relation_class") != entry["relation_class"]
            or record_sha256(source_relation) != entry["source_relation_sha256"]
        ):
            raise ValueError(
                f"graph_security_held_relation_source_mismatch:{relation_id}"
            )

    held_derived_ids: list[str] = []
    for entry in held_derived:
        if (
            not isinstance(entry, dict)
            or set(entry) != SECURITY_HELD_DERIVED_FIELDS
            or not isinstance(entry.get("evidence_id"), str)
            or not isinstance(entry.get("reason_codes"), list)
            or entry["reason_codes"] != sorted(set(entry["reason_codes"]))
            or not entry["reason_codes"]
            or not isinstance(entry.get("excluded_source_evidence_ids"), list)
            or entry["excluded_source_evidence_ids"]
            != sorted(set(entry["excluded_source_evidence_ids"]))
        ):
            raise ValueError("graph_security_held_derived_invalid")
        held_derived_ids.append(entry["evidence_id"])
    if held_derived_ids != sorted(set(held_derived_ids)):
        raise ValueError("graph_security_held_derived_ids_invalid")

    actual_promoted_ids = sorted(
        relation_id
        for relation_id, relation in relation_by_id.items()
        if _is_explicit_verified_structural(relation)
        or (
            relation.get("relation_class") == "lineage"
            and relation.get("status") == "verified"
        )
    )
    if promoted_ids != actual_promoted_ids:
        raise ValueError("graph_security_promoted_relation_ids_mismatch")
    if set(promoted_ids) & set(held_relation_ids):
        raise ValueError("graph_security_promoted_held_overlap")
    expected_counts = {
        "source_relations": len(source_relation_by_id),
        "promoted_relations": len(promoted_ids),
        "held_relations": len(held_relation_ids),
        "safe_evidence": len(evidence_by_id),
        "held_derived_evidence": len(held_derived_ids),
    }
    if counts != expected_counts:
        raise ValueError("graph_security_partition_counts_mismatch")


def _validate_lineage_validation_state(
    state: dict | None,
    document_by_id: dict[str, dict],
    evidence_by_id: dict[str, dict],
    lineage_by_id: dict[str, dict],
) -> None:
    """Bind independently reconstructed lineage to this exact Node universe."""
    if state is None:
        raise ValueError("graph_lineage_validation_required")
    if not isinstance(state, dict) or set(state) != LINEAGE_STATE_FIELDS:
        raise ValueError("graph_lineage_validation_invalid")
    if (
        state.get("schema_version") != GRAPH_SOURCE_SCHEMA_VERSION
        or state.get("validator") != LINEAGE_VALIDATOR
        or state.get("validator_version") != LINEAGE_VALIDATOR_VERSION
        or state.get("status") != "pass"
        or state.get("question_independent") is not True
    ):
        raise ValueError("graph_lineage_validation_identity_invalid")
    inputs = state.get("inputs")
    output = state.get("output")
    if (
        not isinstance(inputs, dict)
        or set(inputs) != LINEAGE_STATE_INPUT_FIELDS
        or not isinstance(output, dict)
        or set(output) != LINEAGE_STATE_OUTPUT_FIELDS
        or not isinstance(state.get("coverage"), dict)
        or set(state["coverage"]) != LINEAGE_COVERAGE_FIELDS
    ):
        raise ValueError("graph_lineage_validation_binding_invalid")
    generated_at = state.get("generated_at")
    if (
        not isinstance(generated_at, str)
        or RFC3339_PATTERN.fullmatch(generated_at) is None
    ):
        raise ValueError("graph_lineage_validation_timestamp_invalid")
    for field in LINEAGE_STATE_INPUT_FIELDS | {
        "sha256", "relation_source_set_sha256",
    }:
        value = inputs.get(field) if field in LINEAGE_STATE_INPUT_FIELDS else output.get(field)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError(f"graph_lineage_validation_hash_invalid:{field}")
    if output.get("path") != "semantic-lineage-relations.jsonl":
        raise ValueError("graph_lineage_validation_output_path_invalid")
    if inputs.get("document_source_set_sha256") != _record_source_set_sha256(
        document_by_id
    ):
        raise ValueError("graph_lineage_document_set_hash_mismatch")
    if inputs.get("evidence_source_set_sha256") != _record_source_set_sha256(
        evidence_by_id
    ):
        raise ValueError("graph_lineage_evidence_set_hash_mismatch")
    if output.get("relation_source_set_sha256") != _record_source_set_sha256(
        lineage_by_id
    ):
        raise ValueError("graph_lineage_relation_set_hash_mismatch")
    if output.get("count") != len(lineage_by_id):
        raise ValueError("graph_lineage_relation_count_mismatch")
    if output.get("verified_relation_ids") != sorted(lineage_by_id):
        raise ValueError("graph_lineage_relation_ids_mismatch")
    coverage = state["coverage"]
    count_fields = LINEAGE_COVERAGE_FIELDS - {"held"}
    if any(
        not isinstance(coverage.get(field), int)
        or isinstance(coverage.get(field), bool)
        or coverage[field] < 0
        for field in count_fields
    ) or not isinstance(coverage.get("held"), list):
        raise ValueError("graph_lineage_coverage_invalid")
    verified_derived_ids = {
        relation["from_ref"]["record_id"] for relation in lineage_by_id.values()
    }
    if (
        coverage["verified_relation_count"] != len(lineage_by_id)
        or coverage["verified_derived_count"] != len(verified_derived_ids)
        or coverage["eligible_derived_count"] != len(verified_derived_ids)
        or coverage["held_derived_count"] != len(coverage["held"])
        or coverage["projected_search_unit_count"]
        != coverage["verified_derived_count"] + coverage["held_derived_count"]
        or coverage["source_reference_count"]
        != len(lineage_by_id) + coverage["held_source_reference_count"]
    ):
        raise ValueError("graph_lineage_coverage_mismatch")


def _validate_explicit_lineage_relations(
    lineage_by_id: dict[str, dict],
    evidence_by_id: dict[str, dict],
) -> None:
    """Validate complete, acyclic SearchUnit fan-in groups fail-closed."""
    groups: dict[str, list[tuple[str, dict]]] = {}
    for relation_id, relation in sorted(lineage_by_id.items()):
        if (
            relation.get("status") != "verified"
            or relation.get("relation_type") != "derived_from"
        ):
            raise ValueError(f"graph_lineage_relation_contract_invalid:{relation_id}")
        provenance = relation["provenance"]
        producer = (
            provenance["generated_by"],
            provenance["generator_version"],
            provenance.get("rule_or_model"),
        )
        if (
            provenance.get("deterministic") is not True
            or provenance.get("confidence") != 1.0
            or producer not in EXPLICIT_LINEAGE_PRODUCERS
        ):
            raise ValueError(f"graph_lineage_provenance_invalid:{relation_id}")
        from_ref = relation["from_ref"]
        to_ref = relation["to_ref"]
        if (
            from_ref.get("record_type") != "evidence"
            or to_ref.get("record_type") != "evidence"
            or from_ref.get("record_id") == to_ref.get("record_id")
        ):
            raise ValueError(f"graph_lineage_endpoint_invalid:{relation_id}")
        derived_id = from_ref["record_id"]
        source_id = to_ref["record_id"]
        if derived_id not in evidence_by_id or source_id not in evidence_by_id:
            raise ValueError(
                f"graph_lineage_endpoint_outside_validated_universe:{relation_id}"
            )
        properties = relation.get("properties")
        if not isinstance(properties, dict) or set(properties) != LINEAGE_PROPERTY_FIELDS:
            raise ValueError(f"graph_lineage_properties_invalid:{relation_id}")
        if properties.get("lineage_contract") != LINEAGE_CONTRACT:
            raise ValueError(f"graph_lineage_contract_version_invalid:{relation_id}")
        source_count = properties.get("source_evidence_count")
        source_ordinal = properties.get("source_evidence_ordinal")
        if (
            not isinstance(source_count, int)
            or isinstance(source_count, bool)
            or source_count < 1
            or not isinstance(source_ordinal, int)
            or isinstance(source_ordinal, bool)
            or not 1 <= source_ordinal <= source_count
        ):
            raise ValueError(f"graph_lineage_fan_in_position_invalid:{relation_id}")
        for field in (
            "source_search_unit_sha256", "fan_in_sha256", "derived_evidence_sha256",
        ):
            value = properties.get(field)
            if (
                not isinstance(value, str)
                or re.fullmatch(r"[0-9a-f]{64}", value) is None
            ):
                raise ValueError(f"graph_lineage_hash_invalid:{relation_id}:{field}")
        if properties["derived_evidence_sha256"] != record_sha256(
            evidence_by_id[derived_id]
        ):
            raise ValueError(f"graph_lineage_derived_hash_mismatch:{relation_id}")
        source_search_unit_id = properties.get("source_search_unit_id")
        if not isinstance(source_search_unit_id, str) or not source_search_unit_id:
            raise ValueError(f"graph_lineage_search_unit_id_invalid:{relation_id}")
        if relation.get("supporting_evidence_ids") != [source_id]:
            raise ValueError(f"graph_lineage_support_invalid:{relation_id}")
        groups.setdefault(derived_id, []).append((relation_id, relation))

    for derived_id, group in sorted(groups.items()):
        first_properties = group[0][1]["properties"]
        expected_count = first_properties["source_evidence_count"]
        shared_fields = {
            "lineage_contract", "source_search_unit_id", "source_search_unit_sha256",
            "source_evidence_count", "fan_in_sha256", "derived_evidence_sha256",
        }
        if len(group) != expected_count:
            raise ValueError(f"graph_lineage_fan_in_incomplete:{derived_id}")
        if any(
            any(
                relation["properties"].get(field) != first_properties.get(field)
                for field in shared_fields
            )
            for _relation_id, relation in group
        ):
            raise ValueError(f"graph_lineage_fan_in_metadata_mismatch:{derived_id}")
        ordinals = sorted(
            relation["properties"]["source_evidence_ordinal"]
            for _relation_id, relation in group
        )
        sources = [
            relation["to_ref"]["record_id"] for _relation_id, relation in group
        ]
        if ordinals != list(range(1, expected_count + 1)) or len(sources) != len(set(sources)):
            raise ValueError(f"graph_lineage_fan_in_members_invalid:{derived_id}")
        ordered_sources = [
            relation["to_ref"]["record_id"]
            for _relation_id, relation in sorted(
                group,
                key=lambda item: item[1]["properties"]["source_evidence_ordinal"],
            )
        ]
        if first_properties["fan_in_sha256"] != record_sha256(ordered_sources):
            raise ValueError(f"graph_lineage_fan_in_hash_mismatch:{derived_id}")

    derived_ids = set(groups)
    if any(
        relation["to_ref"]["record_id"] in derived_ids
        for relation in lineage_by_id.values()
    ):
        raise ValueError("graph_lineage_cycle_or_projection_chain_forbidden")


def _validate_semantic_document(document_id: str, record: dict) -> None:
    missing = sorted(SEMANTIC_DOCUMENT_REQUIRED_FIELDS - record.keys())
    extra = sorted(record.keys() - SEMANTIC_DOCUMENT_ALLOWED_FIELDS)
    if missing or extra:
        raise ValueError(
            f"graph_document_fields_invalid:{document_id}:"
            f"missing={missing}:extra={extra}"
        )
    source = record.get("source")
    if not isinstance(source, dict) or set(source) != SEMANTIC_DOCUMENT_SOURCE_FIELDS:
        raise ValueError(f"graph_document_source_invalid:{document_id}")
    if (
        not isinstance(source.get("relative_path"), str)
        or not source["relative_path"]
        or not isinstance(source.get("absolute_path"), str)
        or not source["absolute_path"]
        or not isinstance(source.get("file_type"), str)
        or not source["file_type"]
        or isinstance(source.get("size_bytes"), bool)
        or not isinstance(source.get("size_bytes"), int)
        or source["size_bytes"] < 0
        or not isinstance(source.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is None
    ):
        raise ValueError(f"graph_document_source_invalid:{document_id}")
    expected_document_id = (
        "doc_"
        + record_sha256({
            "relative_path": source["relative_path"],
            "source_sha256": source["sha256"],
        })[:32]
    )
    if document_id != expected_document_id:
        raise ValueError(
            f"graph_document_id_source_mismatch:{document_id}:{expected_document_id}"
        )
    evidence_ids = record.get("evidence_ids")
    if (
        not isinstance(evidence_ids, list)
        or any(
            not isinstance(value, str)
            or GRAPH_RECORD_ID_PATTERNS["evidence"].fullmatch(value) is None
            for value in evidence_ids
        )
        or len(evidence_ids) != len(set(evidence_ids))
    ):
        raise ValueError(f"graph_document_evidence_ids_invalid:{document_id}")
    if not isinstance(record.get("extraction_metadata"), dict):
        raise ValueError(f"graph_document_extraction_metadata_invalid:{document_id}")
    if record.get("status") not in SEMANTIC_DOCUMENT_STATUSES:
        raise ValueError(
            f"graph_document_status_invalid:{document_id}:{record.get('status')}"
        )


def _validate_semantic_evidence(evidence_id: str, record: dict) -> None:
    missing = sorted(SEMANTIC_EVIDENCE_REQUIRED_FIELDS - record.keys())
    extra = sorted(record.keys() - SEMANTIC_EVIDENCE_ALLOWED_FIELDS)
    if missing or extra:
        raise ValueError(
            f"graph_evidence_fields_invalid:{evidence_id}:"
            f"missing={missing}:extra={extra}"
        )
    source = record.get("source")
    if not isinstance(source, dict) or set(source) != SEMANTIC_EVIDENCE_SOURCE_FIELDS:
        raise ValueError(f"graph_evidence_source_invalid:{evidence_id}")
    if (
        not isinstance(source.get("relative_path"), str)
        or not source["relative_path"]
        or not isinstance(source.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is None
    ):
        raise ValueError(f"graph_evidence_source_invalid:{evidence_id}")
    document_id = record.get("document_id")
    if (
        not isinstance(document_id, str)
        or GRAPH_RECORD_ID_PATTERNS["document"].fullmatch(document_id) is None
    ):
        raise ValueError(f"graph_evidence_document_id_invalid:{evidence_id}")
    if not isinstance(record.get("observed_text"), str):
        raise ValueError(f"graph_evidence_text_invalid:{evidence_id}")
    if record.get("status") not in SEMANTIC_EVIDENCE_STATUSES:
        raise ValueError(
            f"graph_evidence_status_invalid:{evidence_id}:{record.get('status')}"
        )
    locator = record.get("locator")
    if not isinstance(locator, dict) or set(locator) - SEMANTIC_LOCATOR_FIELDS:
        raise ValueError(f"graph_evidence_locator_invalid:{evidence_id}")
    ordinal = record.get("ordinal")
    if ordinal is not None and (
        isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 1
    ):
        raise ValueError(f"graph_evidence_ordinal_invalid:{evidence_id}")
    adapter = record.get("adapter")
    if adapter is not None:
        if not isinstance(adapter, dict) or set(adapter) - SEMANTIC_EVIDENCE_ADAPTER_FIELDS:
            raise ValueError(f"graph_evidence_adapter_invalid:{evidence_id}")
        source_evidence_ids = adapter.get("source_evidence_ids")
        if source_evidence_ids is not None and (
            not isinstance(source_evidence_ids, list)
            or any(
                not isinstance(value, str)
                or GRAPH_RECORD_ID_PATTERNS["evidence"].fullmatch(value) is None
                for value in source_evidence_ids
            )
            or len(source_evidence_ids) != len(set(source_evidence_ids))
        ):
            raise ValueError(f"graph_evidence_adapter_invalid:{evidence_id}")
        question_shard = adapter.get("question_shard")
        if question_shard is not None and (
            not isinstance(question_shard, dict)
            or set(question_shard) != QUESTION_SHARD_FIELDS
        ):
            raise ValueError(f"graph_evidence_question_shard_invalid:{evidence_id}")
    geometry = record.get("geometry")
    if geometry is not None and (
        not isinstance(geometry, dict)
        or set(geometry) - SEMANTIC_GEOMETRY_FIELDS
    ):
        raise ValueError(f"graph_evidence_geometry_invalid:{evidence_id}")


def _node_status(record: dict) -> str:
    source_status = record.get("status")
    if source_status in GRAPH_NODE_STATUSES:
        return str(source_status)
    if source_status == "extraction_failed" or record.get("classification") == "unresolved":
        return "unresolved"
    return "observed"


def _node_payload(record: dict, node_type: str, node_id: str) -> dict:
    source_record_type = record.get("record_type")
    if source_record_type is not None and source_record_type != node_type:
        raise ValueError(
            f"graph_node_record_type_mismatch:{node_id}:{source_record_type}"
        )
    source_record_id = record.get("record_id")
    if source_record_id is not None and source_record_id != node_id:
        raise ValueError(f"graph_node_record_id_mismatch:{node_id}:{source_record_id}")

    # Evidence text remains in the first-class evidence table. Keep the source
    # record nested so projection metadata cannot overwrite a source field and
    # field presence remains auditable.
    source_record = dict(record)
    if node_type == "evidence":
        source_record.pop("observed_text", None)
    payload = {
        "record_type": node_type,
        "record_id": node_id,
        "source_record": source_record,
        "source_record_sha256": record_sha256(record),
        "graph_projection_source": (
            "authorized_semantic_evidence"
            if node_type == "evidence"
            else "authorized_semantic_document"
        ),
    }
    if node_type == "evidence":
        observed_text = record.get("observed_text")
        if not isinstance(observed_text, str):
            raise ValueError(f"graph_evidence_text_invalid:{node_id}")
        observed_hash = hashlib.sha256(observed_text.encode("utf-8")).hexdigest()
        declared_hash = record.get("observed_sha256")
        if declared_hash is not None and declared_hash != observed_hash:
            raise ValueError(f"graph_evidence_declared_hash_mismatch:{node_id}")
        payload["observed_sha256"] = observed_hash
    return payload


def _read_projected_graph(connection: sqlite3.Connection) -> dict:
    nodes: list[dict] = []
    for (
        node_id, node_type, payload_json, status, stored_hash,
    ) in connection.execute(
        "SELECT node_id, node_type, payload_json, status, record_sha256 "
        "FROM graph_nodes "
        "ORDER BY CASE node_type WHEN 'document' THEN 0 ELSE 1 END, node_id"
    ):
        node = {
            "node_id": node_id,
            "node_type": node_type,
            "payload": json.loads(payload_json),
            "status": status,
        }
        if record_sha256(node) != stored_hash:
            raise ValueError(f"graph_node_record_hash_mismatch:{node_id}")
        node["record_sha256"] = stored_hash
        nodes.append(node)

    edges: list[dict] = []
    for (
        relation_id, from_node_id, relation_type, to_node_id, relation_class,
        basis_kind, basis_rule, basis_json, properties_json, status, stored_hash,
    ) in connection.execute(
        "SELECT relation_id, from_node_id, relation_type, to_node_id, "
        "relation_class, basis_kind, basis_rule, basis_json, properties_json, "
        "status, record_sha256 FROM graph_edges ORDER BY relation_id"
    ):
        edge = {
            "relation_id": relation_id,
            "from_node_id": from_node_id,
            "relation_type": relation_type,
            "to_node_id": to_node_id,
            "relation_class": relation_class,
            "basis_kind": basis_kind,
            "basis_rule": basis_rule,
            "basis": json.loads(basis_json),
            "properties": json.loads(properties_json),
            "status": status,
        }
        if record_sha256(edge) != stored_hash:
            raise ValueError(f"graph_edge_record_hash_mismatch:{relation_id}")
        edge["record_sha256"] = stored_hash
        edges.append(edge)

    return {
        "graph_schema_version": GRAPH_SCHEMA_VERSION,
        "nodes": nodes,
        "edges": edges,
    }


def _indexed_evidence_snapshot(connection: sqlite3.Connection) -> dict[str, tuple]:
    return {
        evidence_id: (
            document_id, relative_path, locator_json, observed_text, observed_hash,
            embedding_text, embedding_input_truncated,
        )
        for (
            evidence_id, document_id, relative_path, locator_json, observed_text,
            observed_hash, embedding_text, embedding_input_truncated,
        ) in connection.execute(
            "SELECT evidence_id, document_id, relative_path, locator_json, "
            "observed_text, observed_sha256, embedding_text, "
            "embedding_input_truncated FROM evidence"
        )
    }


def _indexed_embeddings_snapshot(connection: sqlite3.Connection) -> dict[str, tuple]:
    return {
        evidence_id: (dimension, bytes(vector_f32))
        for evidence_id, dimension, vector_f32 in connection.execute(
            "SELECT evidence_id, dimension, vector_f32 FROM embeddings"
        )
    }


def project_verified_structural_graph(
    connection: sqlite3.Connection,
    documents: list[dict],
    evidence_records: list[dict],
    relations: list[dict],
    *,
    lineage_context: dict | None = None,
    security_context: dict | None = None,
) -> dict:
    """Project an authorized Evidence universe into the inactive graph tables.

    Verified structural Relations use the native-producer allowlist. A simple
    Document ``contains`` Evidence edge is independently bound by the
    Evidence's ``document_id``. Every other verified structural edge, and every
    verified lineage edge, requires this projector to re-run the source-bound
    semantic validator from ``lineage_context`` and compare the complete
    reconstructed Relation sets. With ``security_context``, it additionally
    replays the deterministic Content Security Gate, then atomically promotes
    or holds complete lineage fan-ins and affected structural Relations. A
    safe derived Evidence whose lineage is held remains an ``unresolved`` Node.
    Caller-issued PASS metadata is never trusted. Other valid Relation records
    are returned as skipped IDs. Any missing or mismatched endpoint fails
    before publication; no placeholder Node is made. This function
    intentionally does not enable graph retrieval or update capability
    metadata.
    """
    lineage_validation = None
    security_partition = None
    security_source_relations = None
    if security_context is not None:
        attested = _attest_security_context(
            documents,
            evidence_records,
            relations,
            lineage_context,
            security_context,
        )
        documents = attested["documents"]
        evidence_records = attested["evidence_records"]
        relations = attested["relations"]
        security_source_relations = attested["source_relations"]
        lineage_validation = attested["lineage_validation"]
        security_partition = attested["partition"]
    else:
        has_verified_lineage = any(
            relation.get("relation_class") == "lineage"
            and relation.get("status") == "verified"
            for relation in relations
        )
        has_context_bound_structural = any(
            _is_explicit_verified_structural(relation)
            and not _is_document_containment_shape(relation)
            for relation in relations
        )
        if (
            has_verified_lineage
            or has_context_bound_structural
            or lineage_context is not None
        ):
            lineage_validation = _attest_lineage_context(
                documents, evidence_records, relations, lineage_context,
            )
    connection.execute("PRAGMA foreign_keys=ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ValueError("graph_projection_foreign_keys_disabled")

    owns_transaction = not connection.in_transaction
    savepoint = "project_verified_structural_graph"
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE")
    else:
        connection.execute(f"SAVEPOINT {savepoint}")
    try:
        report = _project_verified_structural_graph_in_transaction(
            connection, documents, evidence_records, relations,
            lineage_validation=lineage_validation,
            security_partition=security_partition,
            security_source_relations=security_source_relations,
        )
        if owns_transaction:
            connection.commit()
        else:
            connection.execute(f"RELEASE {savepoint}")
    except BaseException:
        if owns_transaction:
            if connection.in_transaction:
                connection.rollback()
        else:
            try:
                connection.execute(f"ROLLBACK TO {savepoint}")
            finally:
                connection.execute(f"RELEASE {savepoint}")
        raise
    return report


def _project_verified_structural_graph_in_transaction(
    connection: sqlite3.Connection,
    documents: list[dict],
    evidence_records: list[dict],
    relations: list[dict],
    *,
    lineage_validation: dict | None = None,
    security_partition: dict | None = None,
    security_source_relations: list[dict] | None = None,
) -> dict:
    if not connection.in_transaction:
        raise RuntimeError("graph_projection_transaction_missing")
    # BEGIN IMMEDIATE already owns a write reservation. If the caller supplied
    # an outer deferred transaction, this harmless write acquires the same lock
    # before any Evidence is read, closing the read/insert race window.
    connection.execute("UPDATE graph_nodes SET node_id = node_id WHERE 0")

    document_by_id = _records_by_id(documents, "document_id", "document")
    evidence_by_id = _records_by_id(evidence_records, "evidence_id", "evidence")
    relation_by_id = _records_by_id(relations, "relation_id", "relation")
    for relation_id, relation in sorted(relation_by_id.items()):
        _validate_relation_source(relation_id, relation)
    lineage_by_id = {
        relation_id: relation
        for relation_id, relation in relation_by_id.items()
        if relation.get("relation_class") == "lineage"
        and relation.get("status") == "verified"
    }
    if security_partition is not None:
        source_relation_by_id = (
            _records_by_id(
                security_source_relations, "relation_id", "relation",
            )
            if security_source_relations is not None else None
        )
        _validate_security_graph_partition(
            security_partition,
            document_by_id,
            evidence_by_id,
            relation_by_id,
            source_relation_by_id,
        )
        if lineage_by_id:
            _validate_explicit_lineage_relations(lineage_by_id, evidence_by_id)
    elif lineage_by_id or lineage_validation is not None:
        _validate_lineage_validation_state(
            lineage_validation, document_by_id, evidence_by_id, lineage_by_id,
        )
        _validate_explicit_lineage_relations(lineage_by_id, evidence_by_id)

    node_id_overlap = sorted(set(document_by_id) & set(evidence_by_id))
    if node_id_overlap:
        raise ValueError(f"graph_node_id_collision:{node_id_overlap[:8]}")

    indexed_evidence = _indexed_evidence_snapshot(connection)
    indexed_embeddings = _indexed_embeddings_snapshot(connection)
    provided_ids = set(evidence_by_id)
    indexed_ids = set(indexed_evidence)
    if provided_ids != indexed_ids:
        raise ValueError(
            "graph_evidence_universe_mismatch:"
            f"not_indexed={sorted(provided_ids - indexed_ids)[:8]}:"
            f"not_provided={sorted(indexed_ids - provided_ids)[:8]}"
        )

    authorized_document_ids: set[str] = set()
    authorized_evidence_ids_by_document: dict[str, list[str]] = {}
    for evidence_id, record in evidence_by_id.items():
        document_id = record.get("document_id")
        if not isinstance(document_id, str) or not document_id.strip():
            raise ValueError(
                f"graph_evidence_document_id_invalid:{evidence_id}:{document_id!r}"
            )
        authorized_document_ids.add(document_id)
        authorized_evidence_ids_by_document.setdefault(document_id, []).append(evidence_id)
    if set(document_by_id) != authorized_document_ids:
        raise ValueError(
            "graph_document_universe_mismatch:"
            f"without_authorized_evidence="
            f"{sorted(set(document_by_id) - authorized_document_ids)[:8]}:"
            f"not_provided="
            f"{sorted(authorized_document_ids - set(document_by_id))[:8]}"
        )

    node_rows: list[dict] = []
    node_types: dict[str, str] = {}
    security_held_by_id = {
        item["evidence_id"]: item
        for item in (
            security_partition["held_derived_evidence"]
            if security_partition is not None else []
        )
    }
    for node_type, records_by_id in (
        ("document", document_by_id),
        ("evidence", evidence_by_id),
    ):
        for node_id, record in sorted(records_by_id.items()):
            if record.get("schema_version") != GRAPH_SOURCE_SCHEMA_VERSION:
                raise ValueError(
                    f"graph_{node_type}_schema_version_invalid:{node_id}:"
                    f"{record.get('schema_version')}"
                )
            if node_type == "document":
                _validate_semantic_document(node_id, record)
            else:
                _validate_semantic_evidence(node_id, record)
            payload = _node_payload(record, node_type, node_id)
            if node_type == "document":
                source_record = payload["source_record"]
                if "evidence_ids" in source_record:
                    source_evidence_ids = source_record.pop("evidence_ids")
                    if (
                        not isinstance(source_evidence_ids, list)
                        or any(not isinstance(value, str) for value in source_evidence_ids)
                    ):
                        raise ValueError(
                            f"graph_document_evidence_ids_invalid:{node_id}"
                        )
                    payload["source_record_omitted_fields"] = ["evidence_ids"]
                payload["authorized_evidence_ids"] = sorted(
                    authorized_evidence_ids_by_document[node_id]
                )
            if node_type == "evidence":
                document_id = record.get("document_id")
                if document_id not in document_by_id:
                    raise ValueError(
                        f"graph_evidence_document_missing:{node_id}:{document_id}"
                    )
                (
                    db_document_id, db_relative_path, db_locator_json, db_text,
                    db_hash, _db_embedding_text, _db_embedding_input_truncated,
                ) = indexed_evidence[node_id]
                observed_text = record["observed_text"]
                observed_hash = payload["observed_sha256"]
                source = record.get("source")
                relative_path = source.get("relative_path") if isinstance(source, dict) else None
                locator = record.get("locator", {})
                if (
                    not isinstance(relative_path, str)
                    or not relative_path
                    or not isinstance(locator, dict)
                ):
                    raise ValueError(f"graph_evidence_source_binding_invalid:{node_id}")
                document_source = document_by_id[document_id]["source"]
                if (
                    relative_path != document_source["relative_path"]
                    or source.get("sha256") != document_source["sha256"]
                ):
                    raise ValueError(f"graph_evidence_document_binding_mismatch:{node_id}")
                try:
                    db_locator = json.loads(db_locator_json)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError(f"graph_indexed_locator_invalid:{node_id}") from exc
                if (
                    db_document_id != document_id
                    or db_relative_path != relative_path
                    or db_locator != locator
                    or db_text != observed_text
                    or db_hash != observed_hash
                ):
                    raise ValueError(f"graph_evidence_binding_mismatch:{node_id}")
            node_types[node_id] = node_type
            node_status = _node_status(record)
            if node_type == "evidence":
                held = security_held_by_id.get(node_id)
                if held is not None:
                    node_status = "unresolved"
                    payload["security_graph_hold"] = {
                        "reason_codes": held["reason_codes"],
                        "excluded_source_evidence_ids": (
                            held["excluded_source_evidence_ids"]
                        ),
                        "partition_sha256": security_partition[
                            "partition_sha256"
                        ],
                    }
            node_row = {
                "node_id": node_id,
                "node_type": node_type,
                "payload": payload,
                "status": node_status,
            }
            node_row["record_sha256"] = record_sha256(node_row)
            node_rows.append(node_row)

    skipped_relations = {
        "not_verified": [], "non_structural": [], "not_explicit": [],
    }
    edge_rows: list[dict] = []
    for relation_id, relation in sorted(relation_by_id.items()):
        relation_status = relation.get("status")
        relation_class = relation.get("relation_class")
        if relation_status != "verified":
            skipped_relations["not_verified"].append(relation_id)
            continue
        if relation_class not in {"structural", "lineage"}:
            skipped_relations["non_structural"].append(relation_id)
            continue

        provenance = relation["provenance"]
        generated_by = provenance["generated_by"]
        generator_version = provenance["generator_version"]
        basis_rule = provenance.get("rule_or_model")
        producer = (generated_by, generator_version, basis_rule)
        allowed_producers = (
            EXPLICIT_STRUCTURAL_PRODUCERS
            if relation_class == "structural" else EXPLICIT_LINEAGE_PRODUCERS
        )
        if (
            provenance["deterministic"] is not True
            or not isinstance(basis_rule, str)
            or producer not in allowed_producers
        ):
            skipped_relations["not_explicit"].append(relation_id)
            continue

        resolved_endpoints: list[str] = []
        for endpoint_name in ("from_ref", "to_ref"):
            reference = relation.get(endpoint_name)
            record_type = reference.get("record_type")
            record_id = reference.get("record_id")
            actual_type = node_types.get(record_id)
            if actual_type is None:
                raise ValueError(
                    "graph_relation_endpoint_outside_authorized_universe:"
                    f"{relation_id}:{endpoint_name}:{record_type}:{record_id}"
                )
            if actual_type != record_type:
                raise ValueError(
                    f"graph_relation_endpoint_type_mismatch:{relation_id}:"
                    f"{endpoint_name}:{record_type}:{actual_type}"
                )
            resolved_endpoints.append(record_id)

        supporting_ids = relation.get("supporting_evidence_ids", [])
        missing_support = sorted(set(supporting_ids) - provided_ids)
        if missing_support:
            raise ValueError(
                f"graph_relation_support_outside_authorized_universe:"
                f"{relation_id}:{missing_support[:8]}"
            )
        if (
            relation_class == "structural"
            and lineage_validation is None
            and security_partition is None
        ):
            if not _is_document_containment_bound_by_evidence(
                relation, evidence_by_id,
            ):
                raise ValueError(
                    f"graph_structural_independent_binding_mismatch:{relation_id}"
                )
            if not _document_containment_metadata_is_safe(relation):
                raise ValueError(
                    f"graph_structural_independent_metadata_invalid:{relation_id}"
                )

        relation_type = relation["relation_type"]
        properties = relation.get("properties", {})
        source_relation_sha256 = record_sha256(relation)
        basis = {
            "source_schema_version": relation.get("schema_version"),
            "source_record_type": relation["record_type"],
            "from_ref": relation["from_ref"],
            "to_ref": relation["to_ref"],
            "supporting_evidence_ids": supporting_ids,
            "provenance": provenance,
            "optional_fields_present": {
                "properties": "properties" in relation,
                "supporting_evidence_ids": "supporting_evidence_ids" in relation,
            },
            "source_relation_sha256": source_relation_sha256,
        }
        edge_row = {
            "relation_id": relation_id,
            "from_node_id": resolved_endpoints[0],
            "relation_type": relation_type,
            "to_node_id": resolved_endpoints[1],
            "relation_class": relation_class,
            "basis_kind": "explicit",
            "basis_rule": basis_rule,
            "basis": basis,
            "properties": properties,
            "status": "verified",
        }
        edge_row["record_sha256"] = record_sha256(edge_row)
        edge_rows.append(edge_row)

    connected_node_ids = {
        endpoint
        for row in edge_rows
        for endpoint in (row["from_node_id"], row["to_node_id"])
    }
    graph_content = {
        "graph_schema_version": GRAPH_SCHEMA_VERSION,
        "nodes": node_rows,
        "edges": edge_rows,
    }
    report = {
        "graph_schema_version": GRAPH_SCHEMA_VERSION,
        "document_node_count": len(document_by_id),
        "evidence_node_count": len(evidence_by_id),
        "node_count": len(node_rows),
        "edge_count": len(edge_rows),
        "isolated_node_count": len(node_types.keys() - connected_node_ids),
        "relation_input_count": len(relation_by_id),
        "skipped_relations": skipped_relations,
        "skipped_edge_count_by_reason": {
            reason: len(relation_ids)
            for reason, relation_ids in skipped_relations.items()
        },
        "document_source_set_sha256": record_sha256([
            document_by_id[value] for value in sorted(document_by_id)
        ]),
        "evidence_source_set_sha256": record_sha256([
            evidence_by_id[value] for value in sorted(evidence_by_id)
        ]),
        "relation_source_set_sha256": record_sha256([
            relation_by_id[value] for value in sorted(relation_by_id)
        ]),
        "graph_sha256": record_sha256(graph_content),
    }
    if security_partition is not None:
        report["source_relation_input_count"] = security_partition["counts"][
            "source_relations"
        ]
        report["security_partition"] = security_partition

    existing_node_count = connection.execute(
        "SELECT COUNT(*) FROM graph_nodes"
    ).fetchone()[0]
    existing_edge_count = connection.execute(
        "SELECT COUNT(*) FROM graph_edges"
    ).fetchone()[0]
    if existing_node_count or existing_edge_count:
        raise ValueError(
            f"graph_projection_target_not_empty:{existing_node_count}:{existing_edge_count}"
        )

    connection.executemany(
        "INSERT INTO graph_nodes VALUES (?, ?, ?, ?, ?)",
        [(
            row["node_id"], row["node_type"], canonical_json(row["payload"]),
            row["status"], row["record_sha256"],
        ) for row in node_rows],
    )
    connection.executemany(
        "INSERT INTO graph_edges VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(
            row["relation_id"], row["from_node_id"], row["relation_type"],
            row["to_node_id"], row["relation_class"], row["basis_kind"],
            row["basis_rule"], canonical_json(row["basis"]),
            canonical_json(row["properties"]), row["status"],
            row["record_sha256"],
        ) for row in edge_rows],
    )
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise ValueError(f"sqlite_foreign_key_check:{foreign_key_errors[:8]}")
    if _indexed_evidence_snapshot(connection) != indexed_evidence:
        raise ValueError("graph_evidence_changed_during_projection")
    if _indexed_embeddings_snapshot(connection) != indexed_embeddings:
        raise ValueError("graph_embeddings_changed_during_projection")
    persisted_graph = _read_projected_graph(connection)
    if persisted_graph != graph_content:
        raise ValueError("graph_projection_readback_mismatch")
    if record_sha256(persisted_graph) != report["graph_sha256"]:
        raise ValueError("graph_projection_readback_hash_mismatch")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--documents", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--security-state", required=True)
    parser.add_argument("--index-purpose", required=True, choices=("safe_answer", "prompt_library"))
    parser.add_argument("--source-root")
    parser.add_argument("--source-inventory")
    parser.add_argument("--model", default="embeddinggemma:latest")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    if args.batch_size < 1 or args.batch_size > 64:
        raise SystemExit("batch-size must be between 1 and 64")

    evidence_path = Path(args.evidence).resolve(strict=True)
    documents_path = Path(args.documents).resolve(strict=True)
    security_state_path = Path(args.security_state).resolve(strict=True)
    security_state = json.loads(security_state_path.read_text(encoding="utf-8"))
    if security_state.get("classifier") != "deterministic_content_security_gate":
        raise SystemExit("security_state_classifier_invalid")
    if security_state.get("execution_policy") != "never_execute":
        raise SystemExit("security_state_execution_policy_invalid")
    if security_state.get("question_independent") is not True:
        raise SystemExit("security_state_question_independent_invalid")
    evidence_output_name = {
        "safe_answer": "safe-answer-evidence.jsonl",
        "prompt_library": "prompt-library-evidence.jsonl",
    }[args.index_purpose]
    if evidence_path.name != evidence_output_name:
        raise SystemExit(f"security_evidence_filename_mismatch:{evidence_path.name}")
    expected_output = security_state.get("outputs", {}).get(evidence_output_name)
    if not isinstance(expected_output, dict) or expected_output.get("sha256") != sha256_file(evidence_path):
        raise SystemExit("security_evidence_sha256_mismatch")
    if args.index_purpose == "safe_answer" and security_state.get("safe_answer_index_allowed") is not True:
        raise SystemExit("safe_answer_index_not_allowed")
    if args.index_purpose == "prompt_library" and security_state.get("prompt_library_requires_explicit_mode") is not True:
        raise SystemExit("prompt_library_policy_invalid")
    output_path = Path(args.output).resolve(strict=False)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(output_path.name + ".building")
    if temporary_path.exists():
        temporary_path.unlink()

    records = [json.loads(line) for line in evidence_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    documents = [json.loads(line) for line in documents_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    graph_context = None
    if args.index_purpose == "safe_answer":
        if not args.source_root or not args.source_inventory:
            raise SystemExit("safe_answer_graph_context_required")
        if documents_path.name != "semantic-documents.jsonl":
            raise SystemExit(
                f"semantic_documents_filename_mismatch:{documents_path.name}"
            )
        if security_state_path.name != "content-security-state.json":
            raise SystemExit(
                f"security_state_filename_mismatch:{security_state_path.name}"
            )
        if evidence_path.parent != security_state_path.parent:
            raise SystemExit("security_generation_directory_mismatch")
        semantic_dir = documents_path.parent
        security_dir = security_state_path.parent
        source_root = Path(args.source_root).resolve(strict=True)
        source_inventory = Path(args.source_inventory).resolve(strict=True)
        structural_relations = _read_jsonl_records(
            semantic_dir / "layer1-intermediate" / "relations.jsonl"
        )
        lineage_relations = _read_jsonl_records(
            semantic_dir / "semantic-lineage-relations.jsonl"
        )
        graph_context = {
            "relations": [*structural_relations, *lineage_relations],
            "lineage": {
                "output_dir": semantic_dir,
                "source_root": source_root,
                "inventory": source_inventory,
            },
            "security": {"gate_dir": security_dir},
        }
    document_paths = {item["document_id"]: item["source"]["relative_path"] for item in documents}
    if len(document_paths) != len(documents):
        raise SystemExit("Document IDs must be unique")
    ids = [record.get("evidence_id") for record in records]
    if len(ids) != len(set(ids)) or any(not isinstance(value, str) or not value for value in ids):
        raise SystemExit("Evidence IDs must be non-empty and unique")

    connection = sqlite3.connect(temporary_path)
    try:
        initialize(connection)
        prepared = []
        for record in records:
            document_id = record.get("document_id")
            if document_id not in document_paths:
                raise ValueError(f"evidence_document_missing:{document_id}")
            relative_path = document_paths[document_id]
            text, truncated = embedding_text(record, relative_path)
            observed = str(record.get("observed_text", ""))
            prepared.append((record, text, truncated, observed, relative_path))
        require_complete_embedding_inputs(prepared)

        dimension = None
        for offset in range(0, len(prepared), args.batch_size):
            batch = prepared[offset : offset + args.batch_size]
            vectors = embed(args.model, [item[1] for item in batch], args.timeout)
            for (record, text, truncated, observed, relative_path), vector in zip(batch, vectors, strict=True):
                current_dimension = len(vector)
                if dimension is None:
                    dimension = current_dimension
                elif current_dimension != dimension:
                    raise ValueError("global_embedding_dimension_mismatch")
                evidence_id = record["evidence_id"]
                connection.execute(
                    "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        evidence_id,
                        record["document_id"],
                        relative_path,
                        json.dumps(record.get("locator", {}), ensure_ascii=False, sort_keys=True),
                        observed,
                        text,
                        int(truncated),
                        hashlib.sha256(observed.encode("utf-8")).hexdigest(),
                    ),
                )
                packed = array.array("f", (float(value) for value in vector)).tobytes()
                connection.execute(
                    "INSERT INTO embeddings VALUES (?, ?, ?)",
                    (evidence_id, current_dimension, packed),
                )
            connection.commit()
            print(f"embedded {min(offset + len(batch), len(prepared))}/{len(prepared)}", flush=True)

        graph_report = None
        if graph_context is not None:
            graph_report = project_verified_structural_graph(
                connection,
                documents,
                records,
                graph_context["relations"],
                lineage_context=graph_context["lineage"],
                security_context=graph_context["security"],
            )
            security_partition = graph_report.get("security_partition")
            if not isinstance(security_partition, dict):
                raise ValueError("safe_answer_graph_security_partition_missing")
            retrievable_evidence_rows = [
                {
                    "evidence_id": evidence_id,
                    "status": status,
                    "record_sha256": node_sha256,
                }
                for evidence_id, status, node_sha256 in connection.execute(
                    "SELECT node_id, status, record_sha256 FROM graph_nodes "
                    "WHERE node_type = 'evidence' "
                    "AND status IN ('observed', 'verified') ORDER BY node_id"
                )
            ]
            retrievable_evidence_count = len(retrievable_evidence_rows)
            unresolved_evidence_count = connection.execute(
                "SELECT COUNT(*) FROM graph_nodes "
                "WHERE node_type = 'evidence' AND status = 'unresolved'"
            ).fetchone()[0]
            embedding_binding_rows = [
                {
                    "evidence_id": evidence_id,
                    "dimension": embedding_dimension,
                    "vector_f32_sha256": hashlib.sha256(
                        bytes(vector_f32)
                    ).hexdigest(),
                }
                for evidence_id, embedding_dimension, vector_f32
                in connection.execute(
                    "SELECT evidence_id, dimension, vector_f32 "
                    "FROM embeddings ORDER BY evidence_id"
                )
            ]
            probe_vectors = embed(
                args.model, [EMBEDDING_SPACE_PROBE_TEXT], args.timeout
            )
            if len(probe_vectors) != 1 or not probe_vectors[0]:
                raise ValueError("embedding_space_probe_missing")
            probe_vector = [float(value) for value in probe_vectors[0]]
            if len(probe_vector) != dimension:
                raise ValueError("embedding_space_probe_dimension_mismatch")
            probe_vector_f32 = array.array("f", probe_vector).tobytes()
            embedding_probe_binding = {
                "version": EMBEDDING_SPACE_PROBE_VERSION,
                "text_sha256": hashlib.sha256(
                    EMBEDDING_SPACE_PROBE_TEXT.encode("utf-8")
                ).hexdigest(),
                "dimension": len(probe_vector),
                "vector_f32_sha256": hashlib.sha256(
                    probe_vector_f32
                ).hexdigest(),
            }

        metadata = {
            "schema_version": INDEX_SCHEMA_VERSION,
            "graph_schema_version": GRAPH_SCHEMA_VERSION,
            "graph_status": "schema_only",
            "graph_retrieval_enabled": False,
            "graph_node_count": 0,
            "graph_edge_count": 0,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "model": args.model,
            "runtime": "ollama-localhost",
            "evidence_path": str(evidence_path),
            "evidence_sha256": sha256_file(evidence_path),
            "documents_path": str(documents_path),
            "documents_sha256": sha256_file(documents_path),
            "evidence_count": len(records),
            "embedding_dimension": dimension or 0,
            "max_embedding_characters": MAX_EMBED_CHARS,
            "truncated_count": sum(item[2] for item in prepared),
            "external_network_required": False,
            "content_security_gate": True,
            "content_security_policy_version": security_state["policy_version"],
            "content_security_state_path": str(security_state_path),
            "content_security_state_sha256": sha256_file(security_state_path),
            "content_security_execution_policy": "never_execute",
            "index_purpose": args.index_purpose,
            "answer_generation_allowed": (
                args.index_purpose == "safe_answer" and graph_report is not None
            ),
        }
        if graph_report is not None:
            metadata.update({
                "graph_status": GRAPH_READY_STATUS,
                "graph_retrieval_enabled": True,
                "graph_node_count": graph_report["node_count"],
                "graph_edge_count": graph_report["edge_count"],
                "graph_sha256": graph_report["graph_sha256"],
                "graph_document_node_count": graph_report["document_node_count"],
                "graph_evidence_node_count": graph_report["evidence_node_count"],
                "graph_retrievable_evidence_count": retrievable_evidence_count,
                "graph_retrievable_evidence_set_sha256": record_sha256(
                    retrievable_evidence_rows
                ),
                "graph_embeddings_sha256": record_sha256({
                    "model": args.model,
                    "probe": embedding_probe_binding,
                    "records": embedding_binding_rows,
                }),
                "embedding_space_probe_version": embedding_probe_binding[
                    "version"
                ],
                "embedding_space_probe_text_sha256": embedding_probe_binding[
                    "text_sha256"
                ],
                "embedding_space_probe_dimension": embedding_probe_binding[
                    "dimension"
                ],
                "embedding_space_probe_vector_f32_sha256": (
                    embedding_probe_binding["vector_f32_sha256"]
                ),
                "graph_unresolved_evidence_count": unresolved_evidence_count,
                "graph_held_derived_evidence_count": graph_report[
                    "security_partition"
                ]["counts"]["held_derived_evidence"],
                "graph_nonindexed_held_derived_evidence_count": (
                    graph_report["security_partition"]["counts"][
                        "held_derived_evidence"
                    ] - unresolved_evidence_count
                ),
                "graph_security_partition": graph_report["security_partition"],
                "graph_security_partition_sha256": graph_report[
                    "security_partition"
                ]["partition_sha256"],
                "graph_relation_input_count": graph_report["relation_input_count"],
                "graph_source_relation_input_count": graph_report[
                    "source_relation_input_count"
                ],
                "graph_isolated_node_count": graph_report["isolated_node_count"],
                "graph_skipped_edge_count_by_reason": graph_report[
                    "skipped_edge_count_by_reason"
                ],
            })
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            [(key, json.dumps(value, ensure_ascii=False, sort_keys=True)) for key, value in metadata.items()],
        )
        connection.commit()
        connection.execute("PRAGMA optimize")
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise ValueError(f"sqlite_foreign_key_check:{foreign_key_errors[:8]}")
        check = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise ValueError(f"sqlite_integrity_check:{check}")
    finally:
        connection.close()

    os.replace(temporary_path, output_path)
    print(json.dumps(metadata, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
