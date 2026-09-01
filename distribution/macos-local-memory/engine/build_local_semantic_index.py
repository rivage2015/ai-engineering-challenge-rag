#!/usr/bin/env python3
"""Build a fully local SQLite semantic index from content Evidence JSONL."""

from __future__ import annotations

import argparse
import array
import hashlib
import json
import os
import re
import sqlite3
import urllib.request
from datetime import datetime
from pathlib import Path


OLLAMA_EMBED_URL = "http://127.0.0.1:11434/api/embed"
MAX_EMBED_CHARS = 4_000
INDEX_SCHEMA_VERSION = "0.3"
GRAPH_SCHEMA_VERSION = "0.1"
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
    ("chart-table-intermediate-adapter", "0.1.0", "ChartTable containment"),
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
) -> dict:
    """Project an authorized Evidence universe into the inactive graph tables.

    Only verified structural Relation records become Edges. Other valid
    Relation records are returned explicitly as skipped IDs. Any missing or
    mismatched endpoint fails before publication; no placeholder Node is made.
    This function intentionally does not enable graph retrieval or update the
    capability metadata.
    """
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
            node_row = {
                "node_id": node_id,
                "node_type": node_type,
                "payload": payload,
                "status": _node_status(record),
            }
            node_row["record_sha256"] = record_sha256(node_row)
            node_rows.append(node_row)

    skipped_relations = {
        "not_verified": [], "non_structural": [], "not_explicit": [],
    }
    edge_rows: list[dict] = []
    for relation_id, relation in sorted(relation_by_id.items()):
        _validate_relation_source(relation_id, relation)
        relation_status = relation.get("status")
        relation_class = relation.get("relation_class")
        if relation_status != "verified":
            skipped_relations["not_verified"].append(relation_id)
            continue
        if relation_class != "structural":
            skipped_relations["non_structural"].append(relation_id)
            continue

        provenance = relation["provenance"]
        generated_by = provenance["generated_by"]
        generator_version = provenance["generator_version"]
        basis_rule = provenance.get("rule_or_model")
        if (
            provenance["deterministic"] is not True
            or not isinstance(basis_rule, str)
            or (
                generated_by, generator_version, basis_rule
            ) not in EXPLICIT_STRUCTURAL_PRODUCERS
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
            "answer_generation_allowed": args.index_purpose == "safe_answer",
        }
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
