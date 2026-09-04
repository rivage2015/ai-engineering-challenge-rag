#!/usr/bin/env python3
"""Independently audit one Step 3 semantic-graph query candidate.

This is deliberately a separate implementation from both the Step 3 runtime
adapter and the frozen graph query script.  It reopens the registered SQLite
projection read-only, reconstructs the bounded answer from Nodes, Edges, and
Evidence, and compares every deterministic candidate field.  The resulting
record is an audit sibling; it never mutates or activates the candidate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "0.1"
AUDITOR = "cross-document-semantic-graph-independent-edge-audit"
AUDITOR_VERSION = "0.1.0"
RECORD_TYPE = "cross_document_semantic_graph_independent_edge_audit"
CANDIDATE_RECORD_TYPE = "cross_document_semantic_graph_query_candidate"
CANDIDATE_ADAPTER = "cross-document-semantic-graph-runtime"
CANDIDATE_ADAPTER_VERSION = "0.1.0"
CANDIDATE_PRE_AUDIT_MARKER = "not_implemented_step4"
INDEX_DIRECTORY = "05-semantic-answer-index"
INDEX_FILENAME = "safe-answer-index.sqlite3"
STATE_FILENAME = "semantic-answer-index-state.json"
METADATA_PREFIX = "cross_document_semantic_graph_"
GENERATION_PATTERN = re.compile(r"generation-[0-9a-f]{32}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
GRAPH_SNAPSHOT_PREFIX = "xkgs_"
RUN_PREFIX = "xkgr_"
SUPPORTED_OPERATIONS = frozenset({
    "owner", "assignment_change", "version_change",
})
NODE_TYPES = frozenset({
    "Project", "ProjectAlias", "Work", "WorkName", "Employee", "Person",
    "Claim", "Reason",
})
RELATION_ENDPOINT_TYPES = {
    "HAS_ALIAS": ("Project", "ProjectAlias"),
    "CONTAINS_WORK": ("Project", "Work"),
    "HAS_NAME": ("Work", "WorkName"),
    "ASSIGNED_TO": ("Work", "Employee"),
    "IDENTIFIES_PERSON": ("Employee", "Person"),
    "HAS_CLAIM": ("Work", "Claim"),
    "HAS_CURRENT_CLAIM": ("Work", "Claim"),
    "CLAIMS_ASSIGNEE": ("Claim", "Employee"),
    "SUPERSEDES": ("Claim", "Claim"),
    "CONTRADICTS": ("Claim", "Claim"),
    "HAS_CHANGE_REASON": ("Claim", "Reason"),
}
REGISTRATION_FIELDS = frozenset({
    "schema_version", "status", "generation", "database_path",
    "database_sha256", "state_path", "state_sha256", "base_index_path",
    "base_index_sha256", "graph_snapshot_id", "logical_snapshot_sha256",
    "counts", "retrieval_enabled", "used_for_answers",
})
CANDIDATE_FIELDS = frozenset({
    "schema_version", "record_type", "adapter", "adapter_version", "status",
    "decision", "reason_code", "diagnostic_code", "operation", "answer_text",
    "asserted_facts", "asserted_relations", "trace", "runtime_attestation",
    "used_for_answers", "independent_edge_audit_status",
})
DETERMINISTIC_CANDIDATE_FIELDS = CANDIDATE_FIELDS
AUDIT_FIELDS = frozenset({
    "schema_version", "record_type", "auditor", "auditor_version", "status",
    "verdict", "reason_code", "diagnostic_code", "operation",
    "candidate_sha256", "registration_sha256", "question_sha256",
    "question_reference_date", "graph_snapshot_id",
    "reconstructed_semantics_sha256", "checks", "audit_attestation",
    "used_for_answers", "allows_answer_activation",
})
AUDIT_CHECK_FIELDS = frozenset({
    "candidate_contract", "question_classification",
    "registered_storage_integrity", "independent_graph_reconstruction",
    "candidate_semantics",
})
AUDIT_ATTESTATION_FIELDS = frozenset({
    "read_only", "read_snapshot", "database_opened", "generation",
    "index_sha256", "graph_snapshot_id", "logical_snapshot_sha256",
    "projection_sha256", "node_count", "edge_count", "edge_evidence_count",
    "eligible_evidence_count", "outbound_network_attempt_count",
})
TRACE_BASE_FIELDS = frozenset({
    "graph_snapshot_id", "question_reference_date", "visited_node_ids",
    "visited_node_hashes", "visited_edge_ids", "visited_edge_hashes",
    "used_semantic_edge_ids", "used_semantic_edge_count",
    "used_edge_statuses", "visited_document_paths",
    "resolved_source_references", "disabled_edge_ids", "decision",
    "outbound_network_attempt_count", "database_opened",
})
TRACE_DATABASE_FIELDS = TRACE_BASE_FIELDS | {
    "run_id", "question_hash", "elapsed_ms", "peak_rss_bytes",
}
RUNTIME_ATTESTATION_FIELDS = frozenset({
    "adapter", "adapter_version", "read_only", "read_snapshot", "generation",
    "build_id", "index_sha256", "graph_snapshot_id",
    "logical_snapshot_sha256", "projection_sha256", "node_count", "edge_count",
    "edge_evidence_count", "eligible_evidence_count",
    "outbound_network_attempt_count",
})
REQUEST_FIELDS = frozenset({
    "schema_version", "question", "index_path", "registration",
    "question_reference_date",
})
SEMANTIC_TABLES = frozenset({
    "semantic_graph_nodes", "semantic_graph_edges",
    "semantic_graph_edge_evidence",
})
SEMANTIC_INDEXES = frozenset({
    "semantic_graph_nodes_type_key_idx", "semantic_graph_edges_from_type_idx",
    "semantic_graph_edges_to_type_idx",
    "semantic_graph_edge_evidence_evidence_idx",
})
EXPECTED_COLUMNS = {
    "semantic_graph_nodes": (
        "node_id", "node_type", "canonical_key", "status", "properties_json",
        "record_sha256",
    ),
    "semantic_graph_edges": (
        "edge_id", "from_node_id", "relation_type", "to_node_id",
        "relation_class", "status", "basis_kind", "basis_rule",
        "properties_json", "record_sha256",
    ),
    "semantic_graph_edge_evidence": ("edge_id", "evidence_id"),
    "evidence": (
        "evidence_id", "document_id", "relative_path", "locator_json",
        "observed_text", "embedding_text", "embedding_input_truncated",
        "observed_sha256",
    ),
}
SEMANTIC_METADATA_KEYS = frozenset({
    METADATA_PREFIX + suffix
    for suffix in (
        "storage_schema_version", "storage_status", "retrieval_enabled",
        "used_for_answers", "question_independent", "external_network_used",
        "snapshot_id", "logical_snapshot_sha256", "source_sqlite_sha256",
        "builder_state_sha256", "validation_state_sha256",
        "shadow_run_state_sha256", "documents_input_sha256",
        "source_evidence_input_sha256", "evidence_input_sha256",
        "content_security_state_sha256", "content_security_outputs_sha256",
        "node_count", "edge_count", "edge_evidence_count", "projection_sha256",
        "base_logical_snapshot_sha256",
    )
})


class AuditContractError(ValueError):
    """An audit input or persisted artifact failed a fail-closed contract."""

    def __init__(
        self,
        code: str,
        partial_attestation: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.partial_attestation = partial_attestation


class OutboundNetworkDenied(PermissionError):
    """Raised by the process-wide, non-removable Python audit hook."""


class DenyNetworkBoundary:
    """Deny and count every Python socket or DNS audit event."""

    def __init__(self) -> None:
        self.attempt_count = 0

    def audit_hook(self, event: str, _arguments: tuple[Any, ...]) -> None:
        if event.startswith("socket."):
            self.attempt_count += 1
            raise OutboundNetworkDenied(
                f"outbound network denied by independent audit: {event}"
            )


def install_deny_network_boundary() -> DenyNetworkBoundary:
    """Install a process-lifetime deny-network boundary and return its counter."""
    boundary = DenyNetworkBoundary()
    sys.addaudithook(boundary.audit_hook)
    return boundary


class ResolutionError(ValueError):
    """A bounded question could not be resolved to one complete graph path."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int


@dataclass(frozen=True)
class Node:
    node_id: str
    node_type: str
    canonical_key: str
    status: str
    properties: dict[str, Any]
    record_sha256: str


@dataclass(frozen=True)
class Edge:
    edge_id: str
    from_node_id: str
    relation_type: str
    to_node_id: str
    relation_class: str
    status: str
    basis_kind: str
    basis_rule: str
    properties: dict[str, Any]
    supporting_evidence_ids: tuple[str, ...]
    record_sha256: str


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    document_id: str
    relative_path: str
    source_sha256: str
    locator: dict[str, Any]
    observed_text: str
    observed_sha256: str
    record_sha256: str


@dataclass(frozen=True)
class Snapshot:
    graph_snapshot_id: str
    nodes: dict[str, Node]
    edges: dict[str, Edge]
    evidence: dict[str, Evidence]


@dataclass(frozen=True)
class LoadedGraph:
    snapshot: Snapshot
    candidate_attestation: dict[str, Any]
    audit_attestation: dict[str, Any]


def _fail(code: str) -> None:
    raise AuditContractError(code)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None


def _strict_json(value: str | bytes, label: str) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                _fail(f"{label}_duplicate_key")
            result[key] = item
        return result

    def reject_constant(_value: str) -> None:
        _fail(f"{label}_non_finite_number")

    try:
        return json.loads(
            value,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise AuditContractError(f"{label}_invalid_json") from exc


def _strict_object(value: str | bytes, label: str) -> dict[str, Any]:
    parsed = _strict_json(value, label)
    if not isinstance(parsed, dict):
        _fail(f"{label}_object_required")
    return parsed


def _strict_reference_date(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("reference_date must be strict ISO YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            "reference_date must be strict ISO YYYY-MM-DD"
        ) from exc
    if parsed.isoformat() != value:
        raise ValueError("reference_date must be strict ISO YYYY-MM-DD")
    return value


def _regular_file_bytes(
    path: Path,
    label: str,
    *,
    required_mode: int | None = None,
) -> tuple[bytes, FileIdentity]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AuditContractError(f"{label}_unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            _fail(f"{label}_not_single_regular_file")
        if (
            required_mode is not None
            and stat.S_IMODE(before.st_mode) != required_mode
        ):
            _fail(f"{label}_mode_invalid")
        blocks: list[bytes] = []
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            blocks.append(block)
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_mode != after.st_mode
            or after.st_nlink != 1
            or (
                required_mode is not None
                and stat.S_IMODE(after.st_mode) != required_mode
            )
        ):
            _fail(f"{label}_changed_while_reading")
        current = os.stat(path, follow_symlinks=False)
        if (
            current.st_dev != after.st_dev
            or current.st_ino != after.st_ino
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or current.st_mode != after.st_mode
        ):
            _fail(f"{label}_path_changed")
        return b"".join(blocks), FileIdentity(
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        )
    except OSError as exc:
        raise AuditContractError(f"{label}_path_changed") from exc
    finally:
        os.close(descriptor)


def _private_canonical_object_file(
    path: Path, label: str
) -> dict[str, Any]:
    payload, _identity = _regular_file_bytes(
        path, label, required_mode=0o600
    )
    value = _strict_object(payload, label)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AuditContractError(f"{label}_invalid_utf8") from exc
    if text != canonical_json(value):
        _fail(f"{label}_not_canonical_json")
    return value


def _sha256_regular_file(path: Path, label: str) -> tuple[str, FileIdentity]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AuditContractError(f"{label}_unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            _fail(f"{label}_not_single_regular_file")
        digest = hashlib.sha256()
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or after.st_nlink != 1
        ):
            _fail(f"{label}_changed_while_reading")
        current = os.stat(path, follow_symlinks=False)
        if (
            current.st_dev != after.st_dev
            or current.st_ino != after.st_ino
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
        ):
            _fail(f"{label}_path_changed")
        return digest.hexdigest(), FileIdentity(
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        )
    except OSError as exc:
        raise AuditContractError(f"{label}_path_changed") from exc
    finally:
        os.close(descriptor)


def _validate_paths(index_path: Path, generation: str) -> tuple[Path, Path, Path]:
    index = Path(index_path)
    if index.name != INDEX_FILENAME or index.is_symlink() or not index.is_file():
        _fail("edge_audit_index_path_invalid")
    storage = index.parent
    generation_path = storage.parent
    if (
        storage.name != INDEX_DIRECTORY
        or storage.is_symlink()
        or not storage.is_dir()
        or generation_path.is_symlink()
        or not generation_path.is_dir()
        or GENERATION_PATTERN.fullmatch(generation_path.name) is None
        or generation_path.name != generation
    ):
        _fail("edge_audit_generation_layout_invalid")
    state = storage / STATE_FILENAME
    if state.is_symlink() or not state.is_file():
        _fail("edge_audit_projection_state_invalid")
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = index.with_name(index.name + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            _fail("edge_audit_index_sidecar_present")
    try:
        generation_root = generation_path.resolve(strict=True)
        resolved_index = index.resolve(strict=True)
        resolved_state = state.resolve(strict=True)
        resolved_index.relative_to(generation_root)
        resolved_state.relative_to(generation_root)
    except (OSError, ValueError) as exc:
        raise AuditContractError("edge_audit_index_boundary_invalid") from exc
    if resolved_index != generation_root / INDEX_DIRECTORY / INDEX_FILENAME:
        _fail("edge_audit_index_layout_invalid")
    if resolved_state != generation_root / INDEX_DIRECTORY / STATE_FILENAME:
        _fail("edge_audit_state_layout_invalid")
    return resolved_index, resolved_state, generation_root / INDEX_FILENAME


def _validate_projection_state(
    state: dict[str, Any], generation: str, index_sha256: str
) -> None:
    required = {
        "schema_version": SCHEMA_VERSION,
        "record_type": (
            "cross_document_semantic_graph_answer_index_projection_state"
        ),
        "projector": "cross-document-semantic-graph-answer-index-projector",
        "projector_version": "0.1.0",
        "status": "complete",
        "question_independent": True,
        "external_network_used": False,
        "storage_only": True,
        "retrieval_enabled": False,
        "used_for_answers": False,
        "answer_behavior_changed": False,
        "generation": generation,
    }
    if any(state.get(key) != value for key, value in required.items()):
        _fail("edge_audit_projection_state_contract_invalid")
    if set(state) != set(required) | {
        "base", "shadow", "inputs", "counts", "projection_sha256", "output",
    }:
        _fail("edge_audit_projection_state_fields_invalid")
    if state.get("output") != {
        "sqlite_file": INDEX_FILENAME,
        "state_file": STATE_FILENAME,
        "sqlite_sha256": index_sha256,
    }:
        _fail("edge_audit_projection_output_binding_invalid")
    base = state.get("base")
    if (
        not isinstance(base, dict)
        or set(base) != {
            "sqlite_file", "sqlite_sha256", "logical_snapshot_sha256",
            "answer_graph_sha256", "answer_partition_sha256",
        }
        or base.get("sqlite_file") != INDEX_FILENAME
        or any(
            not _is_sha256(base.get(key))
            for key in (
                "sqlite_sha256", "logical_snapshot_sha256",
                "answer_graph_sha256", "answer_partition_sha256",
            )
        )
    ):
        _fail("edge_audit_projection_base_binding_invalid")
    shadow = state.get("shadow")
    if (
        not isinstance(shadow, dict)
        or set(shadow) != {
            "directory", "build_id", "graph_snapshot_id",
            "logical_snapshot_sha256", "sqlite_sha256", "builder_state_sha256",
            "validation_state_sha256", "run_state_sha256",
        }
        or shadow.get("directory") != "04-semantic-graph-shadow"
        or not isinstance(shadow.get("build_id"), str)
        or not shadow["build_id"].strip()
        or not isinstance(shadow.get("graph_snapshot_id"), str)
        or not shadow["graph_snapshot_id"].startswith(GRAPH_SNAPSHOT_PREFIX)
        or any(
            not _is_sha256(shadow.get(key))
            for key in (
                "logical_snapshot_sha256", "sqlite_sha256",
                "builder_state_sha256", "validation_state_sha256",
                "run_state_sha256",
            )
        )
    ):
        _fail("edge_audit_projection_shadow_binding_invalid")
    inputs = state.get("inputs")
    if (
        not isinstance(inputs, dict)
        or set(inputs) != {
            "documents_input_sha256", "source_evidence_input_sha256",
            "evidence_input_sha256", "content_security_state_sha256",
        }
        or any(not _is_sha256(value) for value in inputs.values())
    ):
        _fail("edge_audit_projection_inputs_invalid")
    counts = state.get("counts")
    if (
        not isinstance(counts, dict)
        or set(counts) != {"nodes", "edges", "edge_evidence"}
        or any(type(value) is not int or value < 1 for value in counts.values())
        or not _is_sha256(state.get("projection_sha256"))
    ):
        _fail("edge_audit_projection_counts_invalid")


def _validate_registration(
    registration: dict[str, Any],
    *,
    index: Path,
    state_path: Path,
    base_index: Path,
    generation: str,
    index_sha256: str,
    state_bytes: bytes,
    state: dict[str, Any],
) -> None:
    if set(registration) != REGISTRATION_FIELDS:
        _fail("edge_audit_registration_fields_invalid")
    required = {
        "schema_version": SCHEMA_VERSION,
        "status": "validated_storage_only",
        "generation": generation,
        "database_path": str(index),
        "database_sha256": index_sha256,
        "state_path": str(state_path),
        "state_sha256": hashlib.sha256(state_bytes).hexdigest(),
        "base_index_path": str(base_index),
        "retrieval_enabled": False,
        "used_for_answers": False,
    }
    if any(registration.get(key) != value for key, value in required.items()):
        _fail("edge_audit_registration_binding_invalid")
    logical = registration.get("logical_snapshot_sha256")
    snapshot_id = registration.get("graph_snapshot_id")
    counts = registration.get("counts")
    if (
        not _is_sha256(registration.get("base_index_sha256"))
        or not _is_sha256(logical)
        or snapshot_id != GRAPH_SNAPSHOT_PREFIX + logical[:32]
        or snapshot_id != state["shadow"]["graph_snapshot_id"]
        or logical != state["shadow"]["logical_snapshot_sha256"]
        or counts != state["counts"]
        or not isinstance(counts, dict)
        or set(counts) != {"nodes", "edges", "edge_evidence"}
        or any(type(value) is not int or value < 1 for value in counts.values())
        or registration["base_index_sha256"] != state["base"]["sqlite_sha256"]
    ):
        _fail("edge_audit_registration_contract_invalid")
    base_sha256, _identity = _sha256_regular_file(
        base_index, "edge_audit_registered_base_index"
    )
    if base_sha256 != registration["base_index_sha256"]:
        _fail("edge_audit_registered_base_index_mismatch")


def _read_metadata(connection: sqlite3.Connection) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for row in connection.execute("SELECT key, value FROM metadata ORDER BY key"):
        key = row["key"]
        if not isinstance(key, str) or key in metadata:
            _fail("edge_audit_metadata_key_invalid")
        metadata[key] = _strict_json(row["value"], "edge_audit_metadata_value")
    semantic_keys = frozenset(
        key for key in metadata if key.startswith(METADATA_PREFIX)
    )
    if semantic_keys != SEMANTIC_METADATA_KEYS:
        _fail("edge_audit_metadata_fields_invalid")
    return metadata


def _validate_metadata(metadata: dict[str, Any], state: dict[str, Any]) -> None:
    required = {
        METADATA_PREFIX + "storage_schema_version": SCHEMA_VERSION,
        METADATA_PREFIX + "storage_status": "validated_storage_only",
        METADATA_PREFIX + "retrieval_enabled": False,
        METADATA_PREFIX + "used_for_answers": False,
        METADATA_PREFIX + "question_independent": True,
        METADATA_PREFIX + "external_network_used": False,
    }
    if any(metadata.get(key) != value for key, value in required.items()):
        _fail("edge_audit_metadata_status_invalid")
    hash_suffixes = (
        "logical_snapshot_sha256", "source_sqlite_sha256",
        "builder_state_sha256", "validation_state_sha256",
        "shadow_run_state_sha256", "documents_input_sha256",
        "source_evidence_input_sha256", "evidence_input_sha256",
        "content_security_state_sha256", "content_security_outputs_sha256",
        "projection_sha256", "base_logical_snapshot_sha256",
    )
    if any(
        not _is_sha256(metadata.get(METADATA_PREFIX + suffix))
        for suffix in hash_suffixes
    ):
        _fail("edge_audit_metadata_hash_invalid")
    logical = metadata[METADATA_PREFIX + "logical_snapshot_sha256"]
    if metadata.get(METADATA_PREFIX + "snapshot_id") != (
        GRAPH_SNAPSHOT_PREFIX + logical[:32]
    ):
        _fail("edge_audit_snapshot_identity_invalid")
    for suffix in ("node_count", "edge_count", "edge_evidence_count"):
        value = metadata.get(METADATA_PREFIX + suffix)
        if type(value) is not int or value < 1:
            _fail("edge_audit_metadata_count_invalid")
    bindings = {
        "snapshot_id": state["shadow"]["graph_snapshot_id"],
        "logical_snapshot_sha256": state["shadow"]["logical_snapshot_sha256"],
        "source_sqlite_sha256": state["shadow"]["sqlite_sha256"],
        "builder_state_sha256": state["shadow"]["builder_state_sha256"],
        "validation_state_sha256": state["shadow"]["validation_state_sha256"],
        "shadow_run_state_sha256": state["shadow"]["run_state_sha256"],
        "documents_input_sha256": state["inputs"]["documents_input_sha256"],
        "source_evidence_input_sha256": state["inputs"][
            "source_evidence_input_sha256"
        ],
        "evidence_input_sha256": state["inputs"]["evidence_input_sha256"],
        "content_security_state_sha256": state["inputs"][
            "content_security_state_sha256"
        ],
        "node_count": state["counts"]["nodes"],
        "edge_count": state["counts"]["edges"],
        "edge_evidence_count": state["counts"]["edge_evidence"],
        "projection_sha256": state["projection_sha256"],
        "base_logical_snapshot_sha256": state["base"][
            "logical_snapshot_sha256"
        ],
    }
    if any(
        metadata.get(METADATA_PREFIX + key) != value
        for key, value in bindings.items()
    ):
        _fail("edge_audit_metadata_state_binding_mismatch")


def _validate_schema(connection: sqlite3.Connection) -> None:
    objects = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'semantic_graph_%'"
        )
    }
    if objects != SEMANTIC_TABLES | SEMANTIC_INDEXES:
        _fail("edge_audit_schema_objects_invalid")
    for table, expected in EXPECTED_COLUMNS.items():
        columns = tuple(
            row["name"]
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        )
        if columns != expected:
            _fail(f"edge_audit_schema_columns_invalid:{table}")


def _source_sha256(payload: dict[str, Any], evidence_id: str) -> str:
    source_record = payload.get("source_record")
    source = source_record.get("source") if isinstance(source_record, dict) else None
    value = source.get("sha256") if isinstance(source, dict) else None
    if not _is_sha256(value):
        _fail(f"edge_audit_evidence_source_hash_missing:{evidence_id}")
    return value


def _load_snapshot(
    connection: sqlite3.Connection, metadata: dict[str, Any]
) -> tuple[Snapshot, int]:
    graph_node_rows: list[dict[str, Any]] = []
    evidence_payloads: dict[str, dict[str, Any]] = {}
    evidence_statuses: dict[str, str] = {}
    for row in connection.execute(
        "SELECT node_id, node_type, payload_json, status, record_sha256 "
        "FROM graph_nodes ORDER BY CASE node_type WHEN 'document' THEN 0 ELSE 1 END, node_id"
    ):
        payload = _strict_object(
            row["payload_json"], f"edge_audit_graph_node:{row['node_id']}"
        )
        item = {
            "node_id": row["node_id"],
            "node_type": row["node_type"],
            "payload": payload,
            "status": row["status"],
        }
        if sha256_value(item) != row["record_sha256"]:
            _fail(f"edge_audit_graph_node_hash_mismatch:{row['node_id']}")
        item["record_sha256"] = row["record_sha256"]
        graph_node_rows.append(item)
        if row["node_type"] == "evidence":
            if row["node_id"] in evidence_payloads:
                _fail("edge_audit_graph_evidence_duplicate")
            if row["status"] not in {"observed", "verified", "unresolved"}:
                _fail("edge_audit_graph_evidence_status_invalid")
            evidence_payloads[row["node_id"]] = payload
            evidence_statuses[row["node_id"]] = row["status"]
        elif row["node_type"] != "document":
            _fail("edge_audit_base_graph_node_type_invalid")

    graph_edge_rows: list[dict[str, Any]] = []
    for row in connection.execute(
        "SELECT relation_id, from_node_id, relation_type, to_node_id, "
        "relation_class, basis_kind, basis_rule, basis_json, properties_json, "
        "status, record_sha256 FROM graph_edges ORDER BY relation_id"
    ):
        item = {
            "relation_id": row["relation_id"],
            "from_node_id": row["from_node_id"],
            "relation_type": row["relation_type"],
            "to_node_id": row["to_node_id"],
            "relation_class": row["relation_class"],
            "basis_kind": row["basis_kind"],
            "basis_rule": row["basis_rule"],
            "basis": _strict_json(
                row["basis_json"], f"edge_audit_graph_edge_basis:{row['relation_id']}"
            ),
            "properties": _strict_json(
                row["properties_json"],
                f"edge_audit_graph_edge_properties:{row['relation_id']}",
            ),
            "status": row["status"],
        }
        if sha256_value(item) != row["record_sha256"]:
            _fail(f"edge_audit_graph_edge_hash_mismatch:{row['relation_id']}")
        item["record_sha256"] = row["record_sha256"]
        graph_edge_rows.append(item)
    base_graph = {
        "graph_schema_version": "0.1",
        "nodes": graph_node_rows,
        "edges": graph_edge_rows,
    }
    if sha256_value(base_graph) != metadata.get("graph_sha256"):
        _fail("edge_audit_base_graph_hash_mismatch")

    eligible_rows = [
        {
            "evidence_id": item["node_id"],
            "status": item["status"],
            "record_sha256": item["record_sha256"],
        }
        for item in graph_node_rows
        if item["node_type"] == "evidence"
        and item["status"] in {"observed", "verified"}
    ]
    eligible_ids = frozenset(item["evidence_id"] for item in eligible_rows)
    if (
        metadata.get("graph_retrievable_evidence_count") != len(eligible_rows)
        or metadata.get("graph_retrievable_evidence_set_sha256")
        != sha256_value(eligible_rows)
    ):
        _fail("edge_audit_retrievable_evidence_set_invalid")

    nodes: dict[str, Node] = {}
    for row in connection.execute(
        "SELECT node_id, node_type, canonical_key, status, properties_json, "
        "record_sha256 FROM semantic_graph_nodes ORDER BY node_id"
    ):
        node_id = row["node_id"]
        properties = _strict_object(
            row["properties_json"], f"edge_audit_node_properties:{node_id}"
        )
        if row["properties_json"] != canonical_json(properties):
            _fail(f"edge_audit_node_json_not_canonical:{node_id}")
        core = {
            "node_id": node_id,
            "node_type": row["node_type"],
            "canonical_key": row["canonical_key"],
            "status": row["status"],
            "properties": properties,
        }
        if (
            not isinstance(node_id, str)
            or not node_id
            or node_id in nodes
            or row["node_type"] not in NODE_TYPES
            or row["status"] != "verified"
            or not isinstance(row["canonical_key"], str)
            or not row["canonical_key"].strip()
            or not _is_sha256(row["record_sha256"])
            or sha256_value(core) != row["record_sha256"]
        ):
            _fail(f"edge_audit_node_contract_invalid:{node_id}")
        nodes[node_id] = Node(
            node_id,
            row["node_type"],
            row["canonical_key"],
            row["status"],
            properties,
            row["record_sha256"],
        )

    evidence: dict[str, Evidence] = {}
    for row in connection.execute(
        "SELECT evidence_id, document_id, relative_path, locator_json, "
        "observed_text, observed_sha256 FROM evidence ORDER BY evidence_id"
    ):
        evidence_id = row["evidence_id"]
        locator = _strict_object(
            row["locator_json"], f"edge_audit_evidence_locator:{evidence_id}"
        )
        payload = evidence_payloads.get(evidence_id)
        if payload is None:
            _fail(f"edge_audit_evidence_node_missing:{evidence_id}")
        source_record = payload.get("source_record")
        source = source_record.get("source") if isinstance(source_record, dict) else None
        observed_sha256 = sha256_text(row["observed_text"])
        if (
            not isinstance(evidence_id, str)
            or not evidence_id
            or evidence_id in evidence
            or not isinstance(row["document_id"], str)
            or not row["document_id"]
            or not isinstance(row["relative_path"], str)
            or not row["relative_path"]
            or not isinstance(row["observed_text"], str)
            or row["observed_sha256"] != observed_sha256
            or not isinstance(source_record, dict)
            or source_record.get("document_id") != row["document_id"]
            or source_record.get("locator") != locator
            or not isinstance(source, dict)
            or source.get("relative_path") != row["relative_path"]
            or payload.get("observed_sha256") != observed_sha256
        ):
            _fail(f"edge_audit_evidence_contract_invalid:{evidence_id}")
        source_hash = _source_sha256(payload, evidence_id)
        core = {
            "evidence_id": evidence_id,
            "document_id": row["document_id"],
            "relative_path": row["relative_path"],
            "source_sha256": source_hash,
            "locator": locator,
            "observed_text": row["observed_text"],
            "observed_sha256": observed_sha256,
        }
        evidence[evidence_id] = Evidence(
            evidence_id,
            row["document_id"],
            row["relative_path"],
            source_hash,
            locator,
            row["observed_text"],
            observed_sha256,
            sha256_value(core),
        )
    if set(evidence) != set(evidence_payloads):
        _fail("edge_audit_evidence_universe_mismatch")

    support: dict[str, list[str]] = {}
    support_pairs: set[tuple[str, str]] = set()
    for row in connection.execute(
        "SELECT edge_id, evidence_id FROM semantic_graph_edge_evidence "
        "ORDER BY edge_id, evidence_id"
    ):
        pair = (row["edge_id"], row["evidence_id"])
        if pair in support_pairs:
            _fail("edge_audit_support_duplicate")
        support_pairs.add(pair)
        evidence_id = row["evidence_id"]
        if evidence_id not in evidence:
            _fail(f"edge_audit_support_evidence_missing:{evidence_id}")
        if (
            evidence_id not in eligible_ids
            or evidence_statuses[evidence_id] not in {"observed", "verified"}
            or evidence_payloads[evidence_id].get("security_graph_hold") is not None
        ):
            _fail(f"edge_audit_support_not_verified:{evidence_id}")
        support.setdefault(row["edge_id"], []).append(evidence_id)

    edges: dict[str, Edge] = {}
    for row in connection.execute(
        "SELECT edge_id, from_node_id, relation_type, to_node_id, "
        "relation_class, status, basis_kind, basis_rule, properties_json, "
        "record_sha256 FROM semantic_graph_edges ORDER BY edge_id"
    ):
        edge_id = row["edge_id"]
        properties = _strict_object(
            row["properties_json"], f"edge_audit_edge_properties:{edge_id}"
        )
        if row["properties_json"] != canonical_json(properties):
            _fail(f"edge_audit_edge_json_not_canonical:{edge_id}")
        evidence_ids = tuple(sorted(support.get(edge_id, ())))
        core = {
            "edge_id": edge_id,
            "from_node_id": row["from_node_id"],
            "relation_type": row["relation_type"],
            "to_node_id": row["to_node_id"],
            "relation_class": row["relation_class"],
            "status": row["status"],
            "basis_kind": row["basis_kind"],
            "basis_rule": row["basis_rule"],
            "properties": properties,
            "supporting_evidence_ids": list(evidence_ids),
        }
        endpoints = RELATION_ENDPOINT_TYPES.get(row["relation_type"])
        actual = (
            nodes[row["from_node_id"]].node_type
            if row["from_node_id"] in nodes else None,
            nodes[row["to_node_id"]].node_type
            if row["to_node_id"] in nodes else None,
        )
        if (
            not isinstance(edge_id, str)
            or not edge_id
            or edge_id in edges
            or not evidence_ids
            or endpoints is None
            or actual != endpoints
            or row["relation_class"] != "semantic"
            or row["status"] != "verified"
            or not isinstance(row["basis_kind"], str)
            or not row["basis_kind"].strip()
            or not isinstance(row["basis_rule"], str)
            or not row["basis_rule"].strip()
            or not _is_sha256(row["record_sha256"])
            or sha256_value(core) != row["record_sha256"]
        ):
            _fail(f"edge_audit_edge_contract_invalid:{edge_id}")
        edges[edge_id] = Edge(
            edge_id,
            row["from_node_id"],
            row["relation_type"],
            row["to_node_id"],
            row["relation_class"],
            row["status"],
            row["basis_kind"],
            row["basis_rule"],
            properties,
            evidence_ids,
            row["record_sha256"],
        )
    if set(support) - set(edges):
        _fail("edge_audit_support_edge_missing")
    counts = {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "edge_evidence_count": len(support_pairs),
    }
    if any(
        metadata.get(METADATA_PREFIX + key) != value
        for key, value in counts.items()
    ):
        _fail("edge_audit_counts_mismatch")
    snapshot_core = {
        "evidence_record_sha256": sorted(item.record_sha256 for item in evidence.values()),
        "node_record_sha256": sorted(item.record_sha256 for item in nodes.values()),
        "edge_record_sha256": sorted(item.record_sha256 for item in edges.values()),
    }
    logical = sha256_value(snapshot_core)
    snapshot_id = GRAPH_SNAPSHOT_PREFIX + logical[:32]
    if (
        logical != metadata[METADATA_PREFIX + "logical_snapshot_sha256"]
        or snapshot_id != metadata[METADATA_PREFIX + "snapshot_id"]
    ):
        _fail("edge_audit_logical_snapshot_mismatch")
    projection = {
        "graph_snapshot_id": snapshot_id,
        "node_record_sha256": sorted(item.record_sha256 for item in nodes.values()),
        "edge_record_sha256": sorted(item.record_sha256 for item in edges.values()),
        "edge_evidence": sorted([left, right] for left, right in support_pairs),
    }
    if sha256_value(projection) != metadata[METADATA_PREFIX + "projection_sha256"]:
        _fail("edge_audit_projection_hash_mismatch")
    return Snapshot(snapshot_id, nodes, edges, evidence), len(eligible_ids)


def load_registered_graph(
    index_path: Path, registration: dict[str, Any]
) -> LoadedGraph:
    generation = registration.get("generation")
    if (
        not isinstance(generation, str)
        or GENERATION_PATTERN.fullmatch(generation) is None
    ):
        _fail("edge_audit_registration_generation_invalid")
    index, state_path, base_index = _validate_paths(index_path, generation)
    state_bytes, state_identity = _regular_file_bytes(
        state_path, "edge_audit_projection_state"
    )
    state = _strict_object(state_bytes, "edge_audit_projection_state")
    index_sha256, index_identity = _sha256_regular_file(
        index, "edge_audit_index"
    )
    _validate_projection_state(state, generation, index_sha256)
    _validate_registration(
        registration,
        index=index,
        state_path=state_path,
        base_index=base_index,
        generation=generation,
        index_sha256=index_sha256,
        state_bytes=state_bytes,
        state=state,
    )
    partial_attestation = _empty_audit_attestation()
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            index.as_uri() + "?mode=ro&immutable=1", uri=True
        )
        partial_attestation.update({
            "database_opened": True,
            "read_snapshot": "connection_opened_no_transaction",
            "generation": generation,
            "index_sha256": index_sha256,
        })
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN")
        partial_attestation["read_snapshot"] = "single_sqlite_transaction"
        quick_check = connection.execute("PRAGMA quick_check").fetchall()
        if len(quick_check) != 1 or quick_check[0][0] != "ok":
            _fail("edge_audit_sqlite_integrity_invalid")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            _fail("edge_audit_foreign_key_check_failed")
        metadata = _read_metadata(connection)
        _validate_metadata(metadata, state)
        _validate_schema(connection)
        snapshot, eligible_count = _load_snapshot(connection, metadata)
        partial_attestation.update({
            "graph_snapshot_id": snapshot.graph_snapshot_id,
            "logical_snapshot_sha256": metadata[
                METADATA_PREFIX + "logical_snapshot_sha256"
            ],
            "projection_sha256": metadata[
                METADATA_PREFIX + "projection_sha256"
            ],
            "node_count": len(snapshot.nodes),
            "edge_count": len(snapshot.edges),
            "edge_evidence_count": sum(
                len(edge.supporting_evidence_ids)
                for edge in snapshot.edges.values()
            ),
            "eligible_evidence_count": eligible_count,
        })
        connection.commit()
    except AuditContractError as exc:
        if exc.partial_attestation is None:
            exc.partial_attestation = dict(partial_attestation)
        raise
    except sqlite3.Error as exc:
        raise AuditContractError(
            "edge_audit_sqlite_read_failed",
            dict(partial_attestation),
        ) from exc
    finally:
        if connection is not None:
            connection.close()
    try:
        final_index_sha256, final_index_identity = _sha256_regular_file(
            index, "edge_audit_index"
        )
        final_state_bytes, final_state_identity = _regular_file_bytes(
            state_path, "edge_audit_projection_state"
        )
        if (
            final_index_sha256 != index_sha256
            or final_index_identity != index_identity
            or final_state_bytes != state_bytes
            or final_state_identity != state_identity
        ):
            _fail("edge_audit_artifact_changed_during_read")
    except AuditContractError as exc:
        if exc.partial_attestation is None:
            exc.partial_attestation = dict(partial_attestation)
        raise
    candidate_attestation = {
        "adapter": CANDIDATE_ADAPTER,
        "adapter_version": CANDIDATE_ADAPTER_VERSION,
        "read_only": True,
        "read_snapshot": "single_sqlite_transaction",
        "generation": generation,
        "build_id": state["shadow"]["build_id"],
        "index_sha256": index_sha256,
        "graph_snapshot_id": snapshot.graph_snapshot_id,
        "logical_snapshot_sha256": metadata[
            METADATA_PREFIX + "logical_snapshot_sha256"
        ],
        "projection_sha256": metadata[METADATA_PREFIX + "projection_sha256"],
        "node_count": len(snapshot.nodes),
        "edge_count": len(snapshot.edges),
        "edge_evidence_count": sum(
            len(edge.supporting_evidence_ids) for edge in snapshot.edges.values()
        ),
        "eligible_evidence_count": eligible_count,
        "outbound_network_attempt_count": 0,
    }
    audit_attestation = {
        "read_only": True,
        "read_snapshot": "single_sqlite_transaction",
        "database_opened": True,
        "generation": generation,
        "index_sha256": index_sha256,
        "graph_snapshot_id": snapshot.graph_snapshot_id,
        "logical_snapshot_sha256": candidate_attestation[
            "logical_snapshot_sha256"
        ],
        "projection_sha256": candidate_attestation["projection_sha256"],
        "node_count": len(snapshot.nodes),
        "edge_count": len(snapshot.edges),
        "edge_evidence_count": candidate_attestation["edge_evidence_count"],
        "eligible_evidence_count": eligible_count,
        "outbound_network_attempt_count": 0,
    }
    return LoadedGraph(snapshot, candidate_attestation, audit_attestation)


# This parser is intentionally local to the auditor.  It shares the published
# bounded language, not executable code, with the producer being audited.
DATE_JA = re.compile(
    r"(?<!\d)(?P<year>[12]\d{3})\s*年\s*"
    r"(?P<month>1[0-2]|0?[1-9])\s*月\s*"
    r"(?P<day>3[01]|[12]\d|0?[1-9])\s*日"
)
DATE_ISO = re.compile(
    r"(?<!\d)(?P<year>[12]\d{3})[-/.]"
    r"(?P<month>1[0-2]|0?[1-9])[-/.]"
    r"(?P<day>3[01]|[12]\d|0?[1-9])(?!\d)"
)
RELATIVE_YEARS_AGO = re.compile(
    r"(?<![0-9.+\-−〇零一二三四五六七八九十])"
    r"(?P<years>[0-9〇零一二三四五六七八九十]+)\s*年前"
)
RELATIVE_YEAR_LIKE = re.compile(
    r"(?:[0-9]+|[〇零一二三四五六七八九十]+)\s*年[^。?？!！\n]{0,12}?前"
)
RELATIVE_YEAR_COUNT = r"(?:[0-9]+|[〇零一二三四五六七八九十]+)"
OTHER_TEMPORAL_CONTEXT = re.compile(
    r"(?:一昨日|昨日|今日|明日|明後日|先日|当日|前日|翌日|"
    r"先々週|先週|今週|来週|再来週|先々月|先月|今月|来月|再来月|"
    r"一昨年度|昨年度|今年度|来年度|再来年度|"
    r"一昨年|昨年|去年|今年|来年|再来年|"
    r"現在|現時点|当時|その時|将来|今後|昔|過去)"
    rf"|(?:{RELATIVE_YEAR_COUNT}|数|半)\s*"
    r"(?:か月|ヶ月|ヵ月|ケ月|月|週間?|日間?|時間|分|秒)\s*"
    r"(?:前|まえ|後|あと)"
    r"|(?:数|半)\s*年(?:間)?"
    r"|(?<!\d)[12]\d{3}\s*年"
    rf"|(?:令和|平成|昭和)\s*(?:元|{RELATIVE_YEAR_COUNT})\s*年"
)
QUESTION_TIME_SCOPE_SIGNAL = re.compile(
    rf"(?:前年|翌年|次年|前月|翌月|次月|前週|翌週|次週|"
    rf"前日|翌日|前々日|翌々日|前営業日|翌営業日|同日|その日|"
    rf"春|夏|秋|冬|上期|下期|上半期|下半期|"
    rf"今期|前期|当期|次期|今四半期|期首|期末|年初|年末|月末|年度末|"
    rf"上旬|中旬|下旬|初日|最終日|午前|午後|正午|朝|昼|夕方|夜|深夜|"
    rf"未明|夕刻|ゴールデンウィーク|盆|連休|"
    rf"(?:月|火|水|木|金|土|日)曜日|[Qq][1-4]|"
    rf"[12]\d{{3}}\s*[Qq][1-4]|"
    rf"[12]\d{{3}}[-/.](?:1[0-2]|0?[1-9])(?![-/.]\d)|"
    rf"(?<!\d)(?:1[0-2]|0?[1-9])[/.-](?:3[01]|[12]\d|0?[1-9])(?!\d)|"
    rf"第?\s*{RELATIVE_YEAR_COUNT}\s*四半期|"
    rf"{RELATIVE_YEAR_COUNT}\s*(?:月|年度|四半期|週))\s*"
    r"(?:に|の|は|で|頃|ごろ|時点)?"
)
PROJECT_TO_WORK_BRIDGE = re.compile(
    r"^\s*(?:の|で|における|内の)\s*[「『\"（(\[]*\s*$"
)
WORK_TO_TIME_BRIDGE = re.compile(
    r"^\s*[」』\"）)\]]*\s*(?:は|で|について|の)?\s*(?:、|,)?\s*$"
)
EXACT_TIME_ALLOWED_SUFFIX = re.compile(
    r"^\s*(?:"
    r"(?:の\s*)?時点\s*(?:で(?:は|の)?|は|に(?:おける)?)|"
    r"(?:現在|付)\s*(?:で(?:は|の)?|は|に)|"
    r"を\s*(?:基準|起点)\s*(?:に|として)|"
    r"で(?:は|の)?|は|に|の"
    r")?\s*(?:、|,)?\s*"
    r"(?=(?:誰|どなた|主担当|担当者?|受け持))"
)
OWNER_TIME_QUESTION_TAIL = re.compile(
    r"^\s*(?:"
    r"(?:誰|どなた)が\s*(?:この業務(?:を|の)?\s*)?"
    r"(?:主担当|担当者?|担当|受け持ち)"
    r"(?:でした|だった|です|なのか|していました|しています|していた|している)?"
    r"(?:か|でしょうか)?|"
    r"(?:主担当|担当者?|担当|受け持ち)(?:は|が)?\s*(?:誰|どなた)"
    r"(?:でした|だった|です|なのか)?(?:か|でしょうか)?"
    r")\s*[。.?？!！]*\s*$"
)
ASSIGNMENT_CHANGE_QUESTION_TAIL = re.compile(
    r"^\s*[」』\"）)\]]*\s*(?:"
    r"で\s*(?:、|,)?\s*主担当が(?:切り替わった|交代した)日と\s*"
    r"(?:、|,)?\s*(?:(?:変更前\s*(?:・|と)\s*変更後)|"
    r"(?:前任\s*(?:・|と)\s*後任))(?:の担当者)?を"
    r"(?:答えて|教えて)ください|"
    r"の\s*主担当がいつ(?:変わった|変更された)か\s*(?:、|,)?\s*"
    r"変更前後の担当者を(?:答えて|教えて)ください"
    r")\s*[。.?？!！]*\s*$"
)
VERSION_CHANGE_QUESTION_TAIL = re.compile(
    r"^\s*[」』\"）)\]]*\s*(?:"
    r"について\s*(?:、|,)?\s*承認済みの担当変更理由と\s*"
    r"(?:、|,)?\s*旧案から何が変わったかを(?:答えて|教えて)ください|"
    r"で\s*(?:、|,)?\s*旧版から何が変わったのか\s*"
    r"(?:、|,)?\s*担当変更の背景も(?:答えて|教えて)ください"
    r")\s*[。.?？!！]*\s*$"
)
APPROXIMATE_TIME_SIGNAL = re.compile(
    r"(?:約|およそ|だいたい|大体|おおむね|概ね|ほぼ|ざっと|"
    r"頃|ごろ|前後|くらい|ぐらい|ほど|程度|以降|以前|"
    r"から|まで|以外|除く|かもしれない|現在|現時点|今日|"
    r"昨日|明日|今年|去年|来年)"
)


def _normalize_surface(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def _normalize_for_match(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def classify_question(question: str) -> dict[str, Any]:
    """Classify without consulting, opening, resolving, or stating the index."""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")
    normalized = _normalize_for_match(question)
    old_version = any(token in normalized for token in ("旧案", "旧版", "前版"))
    version_comparison = any(
        token in normalized
        for token in ("変わ", "変更", "違", "差分", "背景", "経緯", "理由")
    )
    if re.search(r"変更.{0,4}理由", normalized) or (
        old_version and version_comparison
    ):
        operation = "version_change"
    elif (
        "切り替" in normalized
        or "交代" in normalized
        or "変更日" in normalized
        or (
            "いつ" in normalized
            and ("変わ" in normalized or "変更" in normalized)
            and ("担当" in normalized or "前後" in normalized)
        )
        or ("変更前" in normalized and "変更後" in normalized)
        or ("前任" in normalized and "後任" in normalized)
    ):
        operation = "assignment_change"
    elif ("担当" in normalized or "受け持" in normalized) and any(
        token in normalized for token in ("誰", "どなた", "教えて")
    ):
        operation = "owner"
    else:
        return {
            "applicable": False,
            "operation": None,
            "reason_code": "question_operation_unsupported",
        }
    return {"applicable": True, "operation": operation, "reason_code": None}


class Traversal:
    def __init__(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        self.used_edge_ids: list[str] = []
        self.visited_node_ids: set[str] = set()

    def candidates(
        self,
        relation_type: str,
        *,
        from_node_id: str | None = None,
        to_node_id: str | None = None,
    ) -> list[Edge]:
        return sorted(
            (
                edge for edge in self.snapshot.edges.values()
                if edge.relation_type == relation_type
                and (from_node_id is None or edge.from_node_id == from_node_id)
                and (to_node_id is None or edge.to_node_id == to_node_id)
            ),
            key=lambda edge: edge.edge_id,
        )

    def use(self, edge: Edge) -> Edge:
        if edge.edge_id not in self.used_edge_ids:
            self.used_edge_ids.append(edge.edge_id)
        self.visited_node_ids.update((edge.from_node_id, edge.to_node_id))
        return edge

    def one(
        self,
        relation_type: str,
        *,
        from_node_id: str | None = None,
        to_node_id: str | None = None,
        reason_code: str,
    ) -> Edge:
        choices = self.candidates(
            relation_type,
            from_node_id=from_node_id,
            to_node_id=to_node_id,
        )
        if len(choices) != 1:
            raise ResolutionError(
                reason_code,
                f"{relation_type} must resolve to one verified Edge; got {len(choices)}",
            )
        return self.use(choices[0])

    def node(self, node_id: str) -> Node:
        try:
            return self.snapshot.nodes[node_id]
        except KeyError as exc:
            raise AuditContractError(f"edge_audit_unknown_node:{node_id}") from exc


def _resolve_subject(
    traversal: Traversal, question: str
) -> tuple[Node, Node]:
    normalized = _normalize_for_match(question)
    project_candidates: list[tuple[Node, Edge | None]] = []
    for edge in traversal.candidates("HAS_ALIAS"):
        alias = traversal.node(edge.to_node_id)
        if _normalize_for_match(alias.canonical_key) in normalized:
            project_candidates.append((traversal.node(edge.from_node_id), edge))
    for node in traversal.snapshot.nodes.values():
        if (
            node.node_type == "Project"
            and _normalize_for_match(node.canonical_key) in normalized
        ):
            project_candidates.append((node, None))
    project_ids = {node.node_id for node, _edge in project_candidates}
    if len(project_ids) != 1:
        raise ResolutionError(
            "project_identity_not_unique",
            f"question must identify one Project; got {len(project_ids)}",
        )
    project = traversal.node(next(iter(project_ids)))
    alias_edges = sorted(
        (
            edge for node, edge in project_candidates
            if node.node_id == project.node_id and edge is not None
        ),
        key=lambda edge: edge.edge_id,
    )
    if alias_edges:
        traversal.use(alias_edges[0])

    work_candidates: list[tuple[Node, Edge | None]] = []
    for edge in traversal.candidates("HAS_NAME"):
        name = traversal.node(edge.to_node_id)
        if _normalize_for_match(name.canonical_key) in normalized:
            work_candidates.append((traversal.node(edge.from_node_id), edge))
    for node in traversal.snapshot.nodes.values():
        if (
            node.node_type == "Work"
            and _normalize_for_match(node.canonical_key) in normalized
        ):
            work_candidates.append((node, None))
    work_ids = {node.node_id for node, _edge in work_candidates}
    if len(work_ids) != 1:
        raise ResolutionError(
            "work_identity_not_unique",
            f"question must identify one Work; got {len(work_ids)}",
        )
    work = traversal.node(next(iter(work_ids)))
    name_edges = sorted(
        (
            edge for node, edge in work_candidates
            if node.node_id == work.node_id and edge is not None
        ),
        key=lambda edge: edge.edge_id,
    )
    if name_edges:
        traversal.use(name_edges[0])
    traversal.one(
        "CONTAINS_WORK",
        from_node_id=project.node_id,
        to_node_id=work.node_id,
        reason_code="project_work_path_missing",
    )
    return project, work


def _subject_surfaces(
    traversal: Traversal, project: Node, work: Node
) -> tuple[set[str], set[str]]:
    projects = {project.canonical_key}
    projects.update(
        traversal.node(edge.to_node_id).canonical_key
        for edge in traversal.candidates("HAS_ALIAS", from_node_id=project.node_id)
    )
    works = {work.canonical_key}
    works.update(
        traversal.node(edge.to_node_id).canonical_key
        for edge in traversal.candidates("HAS_NAME", from_node_id=work.node_id)
    )
    return projects, works


def _non_owner_form_supported(
    question: str,
    operation: str,
    project_surfaces: Iterable[str],
    work_surfaces: Iterable[str],
) -> bool:
    normalized = _normalize_for_match(question)
    tail = {
        "assignment_change": ASSIGNMENT_CHANGE_QUESTION_TAIL,
        "version_change": VERSION_CHANGE_QUESTION_TAIL,
    }.get(operation)
    if tail is None:
        return False
    matches: set[tuple[int, int, int, int]] = set()
    for project_surface in project_surfaces:
        project = _normalize_for_match(project_surface)
        if not project or not normalized.startswith(project):
            continue
        project_end = len(project)
        for work_surface in work_surfaces:
            work = _normalize_for_match(work_surface)
            work_start = normalized.find(work, project_end) if work else -1
            if work_start < 0:
                continue
            work_end = work_start + len(work)
            if PROJECT_TO_WORK_BRIDGE.fullmatch(
                normalized[project_end:work_start]
            ) is None:
                continue
            if tail.fullmatch(normalized[work_end:]) is not None:
                matches.add((0, project_end, work_start, work_end))
    return len(matches) == 1


def _japanese_integer(value: str) -> int | None:
    normalized = unicodedata.normalize("NFKC", value)
    if normalized.isdecimal():
        return int(normalized)
    digits = {
        "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
        "七": 7, "八": 8, "九": 9, "〇": 0, "零": 0,
    }
    if "十" in normalized:
        if normalized.count("十") != 1:
            return None
        tens_text, ones_text = normalized.split("十")
        if len(tens_text) > 1 or len(ones_text) > 1:
            return None
        tens = digits.get(tens_text, 1) if tens_text else 1
        ones = digits.get(ones_text, 0) if ones_text else 0
        if tens is None or ones is None or tens == 0:
            return None
        return tens * 10 + ones
    if not normalized or any(character not in digits for character in normalized):
        return None
    return int("".join(str(digits[character]) for character in normalized))


def _subtract_calendar_years(reference: date, years: int) -> date:
    target_year = reference.year - years
    if target_year < 1:
        raise ResolutionError("reference_time_invalid", "relative date is invalid")
    try:
        return reference.replace(year=target_year)
    except ValueError:
        if reference.month == 2 and reference.day == 29:
            return date(target_year, 2, 28)
        raise ResolutionError("reference_time_invalid", "relative date is invalid")


def _owner_layout_supported(
    question: str,
    time_start: int,
    time_end: int,
    *,
    relative: bool,
    project_surfaces: Iterable[str],
    work_surfaces: Iterable[str],
) -> bool:
    normalized = _normalize_for_match(question)
    left = normalized[:time_start]
    right = normalized[time_end:]
    if relative:
        prefix = re.search(
            r"(?:(?:今|現在|現時点)\s*(?:から\s*(?:(?:数えて|遡って|さかのぼって)\s*)?|(?:を)?(?:基準|起点)(?:に|として)\s*))?(?:ちょうど|まさに)?\s*$",
            left,
        )
    else:
        prefix = re.search(r"(?:ちょうど|まさに)?\s*$", left)
    if prefix is None:
        return False
    subject_end = prefix.start()
    layouts: set[tuple[int, int, int, int]] = set()
    for work_surface in work_surfaces:
        work = _normalize_for_match(work_surface)
        work_start = normalized.rfind(work, 0, subject_end) if work else -1
        if work_start < 0:
            continue
        work_end = work_start + len(work)
        if WORK_TO_TIME_BRIDGE.fullmatch(
            normalized[work_end:subject_end]
        ) is None:
            continue
        for project_surface in project_surfaces:
            project = _normalize_for_match(project_surface)
            project_start = normalized.rfind(project, 0, work_start) if project else -1
            if project_start != 0:
                continue
            project_end = project_start + len(project)
            if PROJECT_TO_WORK_BRIDGE.fullmatch(
                normalized[project_end:work_start]
            ) is not None:
                layouts.add((project_start, project_end, work_start, work_end))
    suffix = EXACT_TIME_ALLOWED_SUFFIX.match(right)
    return (
        len(layouts) == 1
        and suffix is not None
        and OWNER_TIME_QUESTION_TAIL.fullmatch(right[suffix.end():]) is not None
    )


def _parse_question_date(
    question: str,
    reference_date: str | None,
    project_surfaces: Iterable[str],
    work_surfaces: Iterable[str],
) -> str | None:
    normalized = _normalize_for_match(question)
    explicit: list[tuple[re.Match[str], str]] = []
    for pattern in (DATE_JA, DATE_ISO):
        for match in pattern.finditer(normalized):
            try:
                value = date(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                ).isoformat()
            except ValueError as exc:
                raise ResolutionError(
                    "reference_time_invalid", "question contains an invalid date"
                ) from exc
            explicit.append((match, value))
    relative = list(RELATIVE_YEARS_AGO.finditer(normalized))
    if len(explicit) > 1 or len(relative) > 1 or (explicit and relative):
        raise ResolutionError(
            "reference_time_ambiguous", "question contains multiple reference dates"
        )
    if explicit:
        match, value = explicit[0]
        remainder = normalized[:match.start()] + " " + normalized[match.end():]
        if (
            not _owner_layout_supported(
                normalized,
                match.start(),
                match.end(),
                relative=False,
                project_surfaces=project_surfaces,
                work_surfaces=work_surfaces,
            )
            or APPROXIMATE_TIME_SIGNAL.search(remainder)
        ):
            raise ResolutionError(
                "reference_time_ambiguous", "non-exact date is unsupported"
            )
        return value
    if relative:
        match = relative[0]
        remainder = normalized[:match.start()] + " " + normalized[match.end():]
        cleaned_remainder = re.sub(
            r"(?:今|現在|現時点)\s*(?:から|を基準に|を起点に)",
            " ",
            remainder,
        )
        if (
            not _owner_layout_supported(
                normalized,
                match.start(),
                match.end(),
                relative=True,
                project_surfaces=project_surfaces,
                work_surfaces=work_surfaces,
            )
            or APPROXIMATE_TIME_SIGNAL.search(cleaned_remainder)
        ):
            raise ResolutionError(
                "reference_time_ambiguous", "non-exact relative date is unsupported"
            )
        years = _japanese_integer(match.group("years"))
        if years is None or not 1 <= years <= 99:
            raise ResolutionError(
                "reference_time_invalid", "relative year offset is invalid"
            )
        if reference_date is None:
            raise ResolutionError(
                "reference_time_required",
                "相対日付を解決する実行基準日を確認できません。",
            )
        return _subtract_calendar_years(
            date.fromisoformat(reference_date), years
        ).isoformat()
    return None


def _has_question_time(question: str) -> bool:
    normalized = _normalize_for_match(question)
    return bool(
        DATE_JA.search(normalized)
        or DATE_ISO.search(normalized)
        or RELATIVE_YEAR_LIKE.search(normalized)
        or OTHER_TEMPORAL_CONTEXT.search(normalized)
        or QUESTION_TIME_SCOPE_SIGNAL.search(normalized)
    )


def _date_property(properties: dict[str, Any], key: str) -> date | None:
    value = properties.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or re.fullmatch(r"[12]\d{3}-\d{2}-\d{2}", value) is None:
        raise ResolutionError("graph_date_invalid", f"{key} must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ResolutionError("graph_date_invalid", f"{key} is invalid") from exc


def _inclusive(properties: dict[str, Any], key: str) -> bool:
    value = properties.get(key)
    if not isinstance(value, bool):
        raise ResolutionError("assignment_period_incomplete", f"{key} is missing")
    return value


def _effective_start(edge: Edge) -> date | None:
    value = _date_property(edge.properties, "valid_from")
    if value is None:
        return None
    return value if _inclusive(edge.properties, "valid_from_inclusive") else value + timedelta(days=1)


def _effective_end(edge: Edge) -> date | None:
    value = _date_property(edge.properties, "valid_to")
    if value is None:
        return None
    return value if _inclusive(edge.properties, "valid_to_inclusive") else value - timedelta(days=1)


def _validate_assignments(assignments: list[Edge]) -> None:
    if not assignments:
        raise ResolutionError("assignment_path_missing", "no assignment Edge")
    approved = {
        "active", "approved", "current", "effective", "final", "signed",
        "有効", "承認済み", "署名済み/承認済み",
    }
    for edge in assignments:
        role = edge.properties.get("role")
        source_status = edge.properties.get("source_status")
        if not isinstance(role, str) or not role.strip():
            raise ResolutionError(
                "assignment_semantics_inconsistent", "assignment role is missing"
            )
        if (
            not isinstance(source_status, str)
            or _normalize_for_match(source_status) not in approved
        ):
            raise ResolutionError(
                "assignment_semantics_inconsistent", "assignment is not approved"
            )
        start = _effective_start(edge)
        if start is None:
            raise ResolutionError(
                "assignment_period_incomplete", "assignment valid_from is missing"
            )
        end = _effective_end(edge)
        if end is not None and end < start:
            raise ResolutionError(
                "assignment_semantics_inconsistent", "assignment period is invalid"
            )


def _role_for_question(question: str, assignments: list[Edge]) -> str:
    roles = {
        str(edge.properties.get("role"))
        for edge in assignments
        if isinstance(edge.properties.get("role"), str)
        and str(edge.properties.get("role")).strip()
    }
    mentioned = sorted(role for role in roles if role in question)
    if len(mentioned) == 1:
        return mentioned[0]
    if not mentioned and len(roles) == 1:
        return next(iter(roles))
    raise ResolutionError("assignment_role_not_unique", "assignment role is ambiguous")


def _person_for_employee(
    traversal: Traversal, employee_node_id: str
) -> tuple[Node, Edge]:
    identity = traversal.one(
        "IDENTIFIES_PERSON",
        from_node_id=employee_node_id,
        reason_code="employee_person_identity_missing",
    )
    person = traversal.node(identity.to_node_id)
    if person.node_type != "Person":
        raise ResolutionError(
            "employee_person_identity_invalid", "identity is not a Person"
        )
    return person, identity


def _fact(field: str, value: str, *proof_edges: Edge) -> dict[str, Any]:
    return {
        "field": field,
        "value": value,
        "proof_edge_ids": sorted({edge.edge_id for edge in proof_edges}),
    }


def _answer_owner(
    traversal: Traversal,
    question: str,
    project: Node,
    work: Node,
    reference_date: str | None,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    assignments = traversal.candidates("ASSIGNED_TO", from_node_id=work.node_id)
    _validate_assignments(assignments)
    role = _role_for_question(question, assignments)
    assignments = [
        edge for edge in assignments if edge.properties.get("role") == role
    ]
    project_surfaces, work_surfaces = _subject_surfaces(
        traversal, project, work
    )
    reference_time = _parse_question_date(
        question, reference_date, project_surfaces, work_surfaces
    )
    if reference_time is None:
        for edge in assignments:
            traversal.use(edge)
        if len(assignments) >= 2:
            raise ResolutionError(
                "reference_time_required",
                "複数の担当期間があるため、基準日を指定してください。",
            )
        raise ResolutionError(
            "reference_time_required", "担当者を決める基準日を指定してください。"
        )
    target = date.fromisoformat(reference_time)
    active = [
        edge for edge in assignments
        if (_effective_start(edge) is not None)
        and _effective_start(edge) <= target
        and (_effective_end(edge) is None or target <= _effective_end(edge))
    ]
    if len(active) != 1:
        raise ResolutionError(
            "assignment_at_time_not_unique",
            f"reference time resolves to {len(active)} assignments",
        )
    assignment = traversal.use(active[0])
    employee = traversal.node(assignment.to_node_id)
    person, identity = _person_for_employee(traversal, employee.node_id)
    facts = [
        _fact("reference_time", reference_time, assignment),
        _fact("role", role, assignment),
        _fact("assignee_id", employee.canonical_key, assignment),
        _fact("assignee_name", person.canonical_key, assignment, identity),
    ]
    answer = (
        f"{reference_time}時点の{role}は{person.canonical_key}"
        f"（社員ID: {employee.canonical_key}）です。"
    )
    return answer, facts, []


def _answer_assignment_change(
    traversal: Traversal, question: str, work: Node
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    assignments = traversal.candidates("ASSIGNED_TO", from_node_id=work.node_id)
    _validate_assignments(assignments)
    role = _role_for_question(question, assignments)
    assignments = [
        edge for edge in assignments if edge.properties.get("role") == role
    ]
    ordered = sorted(
        assignments,
        key=lambda edge: (_effective_start(edge), edge.edge_id),
    )
    changes: list[tuple[Edge, Edge]] = []
    for previous, current in zip(ordered, ordered[1:]):
        previous_end = _effective_end(previous)
        current_start = _effective_start(current)
        if previous_end is None or current_start is None:
            raise ResolutionError(
                "assignment_period_incomplete", "assignment boundary is missing"
            )
        if (
            previous.to_node_id != current.to_node_id
            and previous_end + timedelta(days=1) == current_start
        ):
            changes.append((previous, current))
    if len(changes) != 1:
        raise ResolutionError(
            "assignment_change_not_unique",
            f"question resolves to {len(changes)} assignment changes",
        )
    previous, current = changes[0]
    traversal.use(previous)
    traversal.use(current)
    previous_employee = traversal.node(previous.to_node_id)
    current_employee = traversal.node(current.to_node_id)
    previous_person, previous_identity = _person_for_employee(
        traversal, previous_employee.node_id
    )
    current_person, current_identity = _person_for_employee(
        traversal, current_employee.node_id
    )
    previous_end = _effective_end(previous)
    current_start = _effective_start(current)
    assert previous_end is not None and current_start is not None
    facts = [
        _fact("change_effective_date", current_start.isoformat(), current),
        _fact("previous_valid_to", previous_end.isoformat(), previous),
        _fact("from_assignee_id", previous_employee.canonical_key, previous),
        _fact(
            "from_assignee_name",
            previous_person.canonical_key,
            previous,
            previous_identity,
        ),
        _fact("to_assignee_id", current_employee.canonical_key, current),
        _fact(
            "to_assignee_name",
            current_person.canonical_key,
            current,
            current_identity,
        ),
    ]
    answer = (
        f"{role}は{current_start.isoformat()}に"
        f"{previous_person.canonical_key}（{previous_employee.canonical_key}）から"
        f"{current_person.canonical_key}（{current_employee.canonical_key}）へ"
        f"切り替わりました。変更前の有効期限は{previous_end.isoformat()}です。"
    )
    return answer, facts, []


def _required_string(
    properties: dict[str, Any], key: str, context: str
) -> str:
    value = properties.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ResolutionError(
            "claim_semantics_inconsistent", f"{context}.{key} is missing"
        )
    return value


def _required_claim_date(
    properties: dict[str, Any], key: str, context: str
) -> str:
    value = _required_string(properties, key, context)
    if re.fullmatch(r"[12]\d{3}-\d{2}-\d{2}", value) is None:
        raise ResolutionError(
            "claim_semantics_inconsistent", f"{context}.{key} is invalid"
        )
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ResolutionError(
            "claim_semantics_inconsistent", f"{context}.{key} is invalid"
        ) from exc
    return value


def _validate_claims(
    *,
    work: Node,
    current_claim: Node,
    old_claim: Node,
    current_link: Edge,
    old_link: Edge,
    current_assignment: Edge,
    old_assignment: Edge,
    contradiction: Edge,
) -> tuple[str, str, str]:
    current_records = (
        ("current_claim", current_claim.properties),
        ("current_link", current_link.properties),
        ("current_assignment", current_assignment.properties),
    )
    old_records = (
        ("old_claim", old_claim.properties),
        ("old_link", old_link.properties),
        ("old_assignment", old_assignment.properties),
    )
    for context, properties in current_records:
        if (
            properties.get("current") is not True
            or properties.get("claim_status") != "APPROVED"
        ):
            raise ResolutionError(
                "claim_semantics_inconsistent", f"{context} is not current approved"
            )
    for context, properties in old_records:
        if (
            properties.get("current") is not False
            or properties.get("claim_status") != "DRAFT"
        ):
            raise ResolutionError(
                "claim_semantics_inconsistent", f"{context} is not old draft"
            )
    current_dates = {
        _required_claim_date(properties, "effective_from", context)
        for context, properties in current_records
    }
    old_dates = {
        _required_claim_date(properties, "effective_from", context)
        for context, properties in old_records
    }
    if len(current_dates) != 1 or old_dates != current_dates:
        raise ResolutionError(
            "claim_semantics_inconsistent", "effective_from values differ"
        )
    roles = {
        _required_string(current_claim.properties, "role", "current_claim"),
        _required_string(old_claim.properties, "role", "old_claim"),
        _required_string(current_assignment.properties, "role", "current_assignment"),
        _required_string(old_assignment.properties, "role", "old_assignment"),
    }
    if len(roles) != 1:
        raise ResolutionError("claim_semantics_inconsistent", "roles differ")
    project_ids = {
        _required_string(current_claim.properties, "project_id", "current_claim"),
        _required_string(old_claim.properties, "project_id", "old_claim"),
    }
    work_ids = {
        _required_string(current_claim.properties, "work_id", "current_claim"),
        _required_string(old_claim.properties, "work_id", "old_claim"),
    }
    if len(project_ids) != 1 or work_ids != {work.canonical_key}:
        raise ResolutionError(
            "claim_semantics_inconsistent", "claims do not identify selected Work"
        )
    versions = {
        _required_string(current_claim.properties, "version", "current_claim"),
        _required_string(old_claim.properties, "version", "old_claim"),
    }
    if len(versions) != 2 or old_assignment.to_node_id == current_assignment.to_node_id:
        raise ResolutionError(
            "claim_semantics_inconsistent", "versions or assignees do not differ"
        )
    dimensions = contradiction.properties.get("comparison_dimensions")
    if (
        not isinstance(dimensions, list)
        or any(not isinstance(value, str) for value in dimensions)
        or not {"work", "role", "effective_from", "assignee"}.issubset(
            set(dimensions)
        )
    ):
        raise ResolutionError(
            "claim_semantics_inconsistent", "comparison dimensions are incomplete"
        )
    return next(iter(current_dates)), "DRAFT", "APPROVED"


def _answer_version_change(
    traversal: Traversal, work: Node
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    current_link = traversal.one(
        "HAS_CURRENT_CLAIM",
        from_node_id=work.node_id,
        reason_code="current_claim_missing",
    )
    current_claim = traversal.node(current_link.to_node_id)
    supersedes = traversal.one(
        "SUPERSEDES",
        from_node_id=current_claim.node_id,
        reason_code="superseded_claim_missing",
    )
    old_claim = traversal.node(supersedes.to_node_id)
    old_link = traversal.one(
        "HAS_CLAIM",
        from_node_id=work.node_id,
        to_node_id=old_claim.node_id,
        reason_code="old_claim_work_link_missing",
    )
    contradiction = traversal.one(
        "CONTRADICTS",
        from_node_id=old_claim.node_id,
        to_node_id=current_claim.node_id,
        reason_code="claim_comparison_missing",
    )
    old_assignment = traversal.one(
        "CLAIMS_ASSIGNEE",
        from_node_id=old_claim.node_id,
        reason_code="old_claim_assignee_missing",
    )
    current_assignment = traversal.one(
        "CLAIMS_ASSIGNEE",
        from_node_id=current_claim.node_id,
        reason_code="current_claim_assignee_missing",
    )
    old_employee = traversal.node(old_assignment.to_node_id)
    current_employee = traversal.node(current_assignment.to_node_id)
    effective_from, old_status, current_status = _validate_claims(
        work=work,
        current_claim=current_claim,
        old_claim=old_claim,
        current_link=current_link,
        old_link=old_link,
        current_assignment=current_assignment,
        old_assignment=old_assignment,
        contradiction=contradiction,
    )
    old_person, old_identity = _person_for_employee(
        traversal, old_employee.node_id
    )
    current_person, current_identity = _person_for_employee(
        traversal, current_employee.node_id
    )
    reason_edge = traversal.one(
        "HAS_CHANGE_REASON",
        from_node_id=current_claim.node_id,
        reason_code="change_reason_missing",
    )
    reason = traversal.node(reason_edge.to_node_id)
    facts = [
        _fact("effective_from", effective_from, current_link, current_assignment),
        _fact("old_plan_status", old_status, old_link, old_assignment),
        _fact("old_plan_assignee_id", old_employee.canonical_key, old_assignment),
        _fact(
            "old_plan_assignee_name", old_person.canonical_key,
            old_assignment, old_identity,
        ),
        _fact(
            "current_plan_status", current_status,
            current_link, current_assignment,
        ),
        _fact(
            "current_plan_assignee_id",
            current_employee.canonical_key,
            current_assignment,
        ),
        _fact(
            "current_plan_assignee_name", current_person.canonical_key,
            current_assignment, current_identity,
        ),
        _fact("change_reason", reason.canonical_key, reason_edge),
    ]
    relations = [
        {
            "from": current_claim.canonical_key,
            "relation": "SUPERSEDES",
            "to": old_claim.canonical_key,
            "proof_edge_ids": [supersedes.edge_id],
        },
        {
            "from": old_claim.canonical_key,
            "relation": "CONTRADICTS",
            "to": current_claim.canonical_key,
            "proof_edge_ids": [contradiction.edge_id],
        },
    ]
    answer = (
        f"承認済み計画では、{effective_from}から担当を"
        f"{old_person.canonical_key}（{old_employee.canonical_key}）から"
        f"{current_person.canonical_key}（{current_employee.canonical_key}）へ変更しています。"
        f"変更理由は「{reason.canonical_key}」です。"
    )
    return answer, facts, relations


def _source_references(
    snapshot: Snapshot, used_edge_ids: list[str]
) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for edge_id in used_edge_ids:
        edge = snapshot.edges[edge_id]
        for evidence_id in edge.supporting_evidence_ids:
            evidence = snapshot.evidence[evidence_id]
            references.append({
                "edge_id": edge_id,
                "evidence_id": evidence_id,
                "document_id": evidence.document_id,
                "path": evidence.relative_path,
                "source_sha256": evidence.source_sha256,
                "locator": evidence.locator,
                "observed_text_sha256": evidence.observed_sha256,
                "quote": evidence.observed_text,
            })
    return sorted(
        references, key=lambda item: (item["edge_id"], item["evidence_id"])
    )


def _trace(
    traversal: Traversal,
    question_hash: str,
    decision: str,
    reference_date: str | None,
) -> dict[str, Any]:
    edge_ids = list(traversal.used_edge_ids)
    edges = [traversal.snapshot.edges[edge_id] for edge_id in edge_ids]
    references = _source_references(traversal.snapshot, edge_ids)
    run_identity: dict[str, Any] = {
        "graph_snapshot_id": traversal.snapshot.graph_snapshot_id,
        "question_hash": question_hash,
        "disabled_edge_ids": [],
    }
    if reference_date is not None:
        run_identity["question_reference_date"] = reference_date
    return {
        "run_id": RUN_PREFIX + sha256_value(run_identity)[:32],
        "graph_snapshot_id": traversal.snapshot.graph_snapshot_id,
        "question_hash": question_hash,
        "question_reference_date": reference_date,
        "visited_node_ids": sorted(traversal.visited_node_ids),
        "visited_node_hashes": sorted(
            traversal.snapshot.nodes[node_id].record_sha256
            for node_id in traversal.visited_node_ids
        ),
        "visited_edge_ids": edge_ids,
        "visited_edge_hashes": [edge.record_sha256 for edge in edges],
        "used_semantic_edge_ids": edge_ids,
        "used_semantic_edge_count": len(edge_ids),
        "used_edge_statuses": sorted({edge.status for edge in edges}),
        "visited_document_paths": sorted({
            reference["path"] for reference in references
        }),
        "resolved_source_references": references,
        "disabled_edge_ids": [],
        "decision": decision,
        "outbound_network_attempt_count": 0,
        "database_opened": True,
    }


def _empty_trace(
    decision: str, reference_date: str | None
) -> dict[str, Any]:
    return {
        "graph_snapshot_id": None,
        "question_reference_date": reference_date,
        "visited_node_ids": [],
        "visited_node_hashes": [],
        "visited_edge_ids": [],
        "visited_edge_hashes": [],
        "used_semantic_edge_ids": [],
        "used_semantic_edge_count": 0,
        "used_edge_statuses": [],
        "visited_document_paths": [],
        "resolved_source_references": [],
        "disabled_edge_ids": [],
        "decision": decision,
        "outbound_network_attempt_count": 0,
        "database_opened": False,
    }


def _candidate_result(
    *,
    status: str,
    decision: str,
    operation: str | None,
    reason_code: str | None,
    answer_text: str,
    asserted_facts: list[dict[str, Any]],
    asserted_relations: list[dict[str, Any]],
    trace: dict[str, Any],
    runtime_attestation: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": CANDIDATE_RECORD_TYPE,
        "adapter": CANDIDATE_ADAPTER,
        "adapter_version": CANDIDATE_ADAPTER_VERSION,
        "status": status,
        "decision": decision,
        "reason_code": reason_code,
        "diagnostic_code": None,
        "operation": operation,
        "answer_text": answer_text,
        "asserted_facts": asserted_facts,
        "asserted_relations": asserted_relations,
        "trace": trace,
        "runtime_attestation": runtime_attestation,
        "used_for_answers": False,
        "independent_edge_audit_status": CANDIDATE_PRE_AUDIT_MARKER,
    }


def reconstruct_candidate(
    loaded: LoadedGraph,
    question: str,
    operation: str,
    reference_date: str | None,
) -> dict[str, Any]:
    normalized_question = _normalize_surface(question)
    traversal = Traversal(loaded.snapshot)
    decision = "HOLD"
    reason_code: str | None = None
    answer_text = "必要な検証済みグラフ経路が足りないため回答できません。"
    facts: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    try:
        if operation != "owner" and _has_question_time(normalized_question):
            raise ResolutionError(
                "temporal_context_unsupported", "time filter is unsupported"
            )
        project, work = _resolve_subject(traversal, normalized_question)
        if operation in {"assignment_change", "version_change"}:
            project_surfaces, work_surfaces = _subject_surfaces(
                traversal, project, work
            )
            if not _non_owner_form_supported(
                normalized_question,
                operation,
                project_surfaces,
                work_surfaces,
            ):
                raise ResolutionError(
                    "temporal_context_unsupported", "question grammar is unsupported"
                )
        if operation == "owner":
            answer_text, facts, relations = _answer_owner(
                traversal,
                normalized_question,
                project,
                work,
                reference_date,
            )
        elif operation == "assignment_change":
            answer_text, facts, relations = _answer_assignment_change(
                traversal, normalized_question, work
            )
        elif operation == "version_change":
            answer_text, facts, relations = _answer_version_change(
                traversal, work
            )
        else:
            raise AuditContractError("edge_audit_operation_invalid")
        decision = "ACCEPTED"
    except ResolutionError as exc:
        reason_code = exc.reason_code
        facts = []
        relations = []
        if exc.reason_code == "reference_time_required":
            answer_text = exc.message
    return _candidate_result(
        status="accepted" if decision == "ACCEPTED" else "held",
        decision=decision,
        operation=operation,
        reason_code=reason_code,
        answer_text=answer_text,
        asserted_facts=facts,
        asserted_relations=relations,
        trace=_trace(
            traversal,
            sha256_text(normalized_question),
            decision,
            reference_date,
        ),
        runtime_attestation=loaded.candidate_attestation,
    )


def _validate_string_list(value: Any, label: str) -> None:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
    ):
        _fail(label)


def _validate_candidate_contract(candidate: dict[str, Any]) -> None:
    if set(candidate) != CANDIDATE_FIELDS:
        _fail("edge_audit_candidate_fields_invalid")
    required = {
        "schema_version": SCHEMA_VERSION,
        "record_type": CANDIDATE_RECORD_TYPE,
        "adapter": CANDIDATE_ADAPTER,
        "adapter_version": CANDIDATE_ADAPTER_VERSION,
        "used_for_answers": False,
        "independent_edge_audit_status": CANDIDATE_PRE_AUDIT_MARKER,
    }
    if any(candidate.get(key) != value for key, value in required.items()):
        _fail("edge_audit_candidate_identity_invalid")
    status_pair = (candidate.get("status"), candidate.get("decision"))
    if status_pair not in {
        ("accepted", "ACCEPTED"),
        ("held", "HOLD"),
        ("not_applicable", "NOT_APPLICABLE"),
    }:
        _fail("edge_audit_candidate_decision_invalid")
    if candidate.get("operation") is not None and (
        candidate["operation"] not in SUPPORTED_OPERATIONS
    ):
        _fail("edge_audit_candidate_operation_invalid")
    if any(
        value is not None and (not isinstance(value, str) or not value)
        for value in (
            candidate.get("reason_code"), candidate.get("diagnostic_code")
        )
    ):
        _fail("edge_audit_candidate_reason_invalid")
    if (
        not isinstance(candidate.get("answer_text"), str)
        or not isinstance(candidate.get("asserted_facts"), list)
        or not isinstance(candidate.get("asserted_relations"), list)
        or not isinstance(candidate.get("trace"), dict)
    ):
        _fail("edge_audit_candidate_payload_invalid")
    for fact in candidate["asserted_facts"]:
        if (
            not isinstance(fact, dict)
            or set(fact) != {"field", "value", "proof_edge_ids"}
            or not isinstance(fact["field"], str)
            or not fact["field"]
            or not isinstance(fact["value"], str)
            or not fact["value"]
        ):
            _fail("edge_audit_candidate_fact_invalid")
        _validate_string_list(
            fact["proof_edge_ids"], "edge_audit_candidate_fact_proof_invalid"
        )
    for relation in candidate["asserted_relations"]:
        if (
            not isinstance(relation, dict)
            or set(relation) != {"from", "relation", "to", "proof_edge_ids"}
            or any(
                not isinstance(relation[key], str) or not relation[key]
                for key in ("from", "relation", "to")
            )
        ):
            _fail("edge_audit_candidate_relation_invalid")
        _validate_string_list(
            relation["proof_edge_ids"],
            "edge_audit_candidate_relation_proof_invalid",
        )
    trace = candidate["trace"]
    database_opened = trace.get("database_opened")
    expected_trace_fields = (
        TRACE_DATABASE_FIELDS if database_opened is True else TRACE_BASE_FIELDS
    )
    if set(trace) != expected_trace_fields or not isinstance(database_opened, bool):
        _fail("edge_audit_candidate_trace_fields_invalid")
    for field in (
        "visited_node_ids", "visited_node_hashes", "visited_edge_ids",
        "visited_edge_hashes", "used_semantic_edge_ids", "used_edge_statuses",
        "visited_document_paths", "disabled_edge_ids",
    ):
        _validate_string_list(
            trace.get(field), f"edge_audit_candidate_trace_{field}_invalid"
        )
    if (
        trace.get("visited_edge_ids") != trace.get("used_semantic_edge_ids")
        or type(trace.get("used_semantic_edge_count")) is not int
        or trace["used_semantic_edge_count"] != len(trace["used_semantic_edge_ids"])
        or trace.get("decision") != candidate["decision"]
        or trace.get("outbound_network_attempt_count") != 0
        or not isinstance(trace.get("resolved_source_references"), list)
    ):
        _fail("edge_audit_candidate_trace_contract_invalid")
    trace_reference_date = trace.get("question_reference_date")
    try:
        _strict_reference_date(trace_reference_date)
    except ValueError as exc:
        raise AuditContractError(
            "edge_audit_candidate_reference_date_invalid"
        ) from exc
    if database_opened:
        if (
            not isinstance(trace.get("run_id"), str)
            or re.fullmatch(r"xkgr_[0-9a-f]{32}", trace["run_id"]) is None
            or not _is_sha256(trace.get("question_hash"))
            or not isinstance(trace.get("elapsed_ms"), (int, float))
            or isinstance(trace.get("elapsed_ms"), bool)
            or trace["elapsed_ms"] < 0
            or type(trace.get("peak_rss_bytes")) is not int
            or trace["peak_rss_bytes"] < 0
        ):
            _fail("edge_audit_candidate_trace_telemetry_invalid")
    for reference in trace["resolved_source_references"]:
        if (
            not isinstance(reference, dict)
            or set(reference) != {
                "edge_id", "evidence_id", "document_id", "path",
                "source_sha256", "locator", "observed_text_sha256", "quote",
            }
            or any(
                not isinstance(reference.get(key), str) or not reference[key]
                for key in (
                    "edge_id", "evidence_id", "document_id", "path", "quote"
                )
            )
            or not _is_sha256(reference.get("source_sha256"))
            or not _is_sha256(reference.get("observed_text_sha256"))
            or not isinstance(reference.get("locator"), dict)
        ):
            _fail("edge_audit_candidate_source_reference_invalid")
    attestation = candidate.get("runtime_attestation")
    if database_opened:
        if (
            not isinstance(attestation, dict)
            or set(attestation) != RUNTIME_ATTESTATION_FIELDS
            or attestation.get("adapter") != CANDIDATE_ADAPTER
            or attestation.get("adapter_version") != CANDIDATE_ADAPTER_VERSION
            or attestation.get("read_only") is not True
            or attestation.get("read_snapshot") != "single_sqlite_transaction"
            or attestation.get("outbound_network_attempt_count") != 0
            or not isinstance(attestation.get("generation"), str)
            or GENERATION_PATTERN.fullmatch(attestation["generation"]) is None
            or not isinstance(attestation.get("build_id"), str)
            or not attestation["build_id"]
            or not isinstance(attestation.get("graph_snapshot_id"), str)
            or not attestation["graph_snapshot_id"].startswith(
                GRAPH_SNAPSHOT_PREFIX
            )
            or any(
                not _is_sha256(attestation.get(field))
                for field in (
                    "index_sha256", "logical_snapshot_sha256", "projection_sha256"
                )
            )
            or any(
                type(attestation.get(field)) is not int
                or attestation[field] < 1
                for field in (
                    "node_count", "edge_count", "edge_evidence_count",
                    "eligible_evidence_count",
                )
            )
        ):
            _fail("edge_audit_candidate_runtime_attestation_invalid")
    elif attestation is not None:
        _fail("edge_audit_candidate_runtime_attestation_unexpected")


def deterministic_candidate_semantics(
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Project all candidate fields, excluding only nondeterministic telemetry."""
    result = {
        key: candidate[key] for key in DETERMINISTIC_CANDIDATE_FIELDS
    }
    trace = dict(candidate["trace"])
    trace.pop("elapsed_ms", None)
    trace.pop("peak_rss_bytes", None)
    result["trace"] = trace
    return result


def _mismatch_code(
    actual: dict[str, Any], expected: dict[str, Any]
) -> str:
    field_codes = (
        ("status", "candidate_status_mismatch"),
        ("decision", "candidate_decision_mismatch"),
        ("operation", "candidate_operation_mismatch"),
        ("reason_code", "candidate_reason_code_mismatch"),
        ("diagnostic_code", "candidate_diagnostic_code_mismatch"),
        ("answer_text", "candidate_answer_text_mismatch"),
        ("asserted_facts", "candidate_asserted_facts_mismatch"),
        ("asserted_relations", "candidate_asserted_relations_mismatch"),
        ("trace", "candidate_trace_mismatch"),
        ("runtime_attestation", "candidate_runtime_attestation_mismatch"),
        (
            "independent_edge_audit_status",
            "candidate_pre_audit_marker_mismatch",
        ),
    )
    for field, code in field_codes:
        if actual.get(field) != expected.get(field):
            if field == "trace":
                actual_trace = actual.get("trace", {})
                expected_trace = expected.get("trace", {})
                if actual_trace.get("question_reference_date") != expected_trace.get(
                    "question_reference_date"
                ):
                    return "candidate_reference_time_binding_mismatch"
                if actual_trace.get("resolved_source_references") != expected_trace.get(
                    "resolved_source_references"
                ):
                    return "candidate_source_reference_mismatch"
            return code
    return "candidate_semantics_mismatch"


def _empty_audit_attestation() -> dict[str, Any]:
    return {
        "read_only": True,
        "read_snapshot": None,
        "database_opened": False,
        "generation": None,
        "index_sha256": None,
        "graph_snapshot_id": None,
        "logical_snapshot_sha256": None,
        "projection_sha256": None,
        "node_count": None,
        "edge_count": None,
        "edge_evidence_count": None,
        "eligible_evidence_count": None,
        "outbound_network_attempt_count": 0,
    }


def _attestation_with_network_count(
    attestation: dict[str, Any],
    network_boundary: DenyNetworkBoundary | None,
) -> dict[str, Any]:
    result = dict(attestation)
    result["outbound_network_attempt_count"] = (
        network_boundary.attempt_count if network_boundary is not None else 0
    )
    return result


def _audit_record(
    *,
    status: str,
    verdict: str,
    reason_code: str | None,
    diagnostic_code: str | None,
    operation: str | None,
    candidate_sha256: str,
    registration_sha256: str,
    question_sha256: str,
    reference_date: str | None,
    graph_snapshot_id: str | None,
    reconstructed_semantics_sha256: str | None,
    checks: dict[str, str],
    audit_attestation: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": RECORD_TYPE,
        "auditor": AUDITOR,
        "auditor_version": AUDITOR_VERSION,
        "status": status,
        "verdict": verdict,
        "reason_code": reason_code,
        "diagnostic_code": diagnostic_code,
        "operation": operation,
        "candidate_sha256": candidate_sha256,
        "registration_sha256": registration_sha256,
        "question_sha256": question_sha256,
        "question_reference_date": reference_date,
        "graph_snapshot_id": graph_snapshot_id,
        "reconstructed_semantics_sha256": reconstructed_semantics_sha256,
        "checks": checks,
        "audit_attestation": audit_attestation,
        "used_for_answers": False,
        "allows_answer_activation": False,
    }


def audit_candidate(
    index_path: Path,
    question: str,
    registration: dict[str, Any],
    candidate: dict[str, Any],
    *,
    reference_date: str | None = None,
    network_boundary: DenyNetworkBoundary | None = None,
) -> dict[str, Any]:
    """Return a strict sibling audit record for one immutable candidate."""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")
    if not isinstance(registration, dict):
        raise ValueError("registration must be an object")
    if not isinstance(candidate, dict):
        raise ValueError("candidate must be an object")
    reference_date = _strict_reference_date(reference_date)
    normalized_question = _normalize_surface(question)
    candidate_hash = sha256_value(candidate)
    registration_hash = sha256_value(registration)
    question_hash = sha256_text(normalized_question)
    checks = {
        "candidate_contract": "NOT_APPLICABLE",
        "question_classification": "NOT_APPLICABLE",
        "registered_storage_integrity": "NOT_APPLICABLE",
        "independent_graph_reconstruction": "NOT_APPLICABLE",
        "candidate_semantics": "NOT_APPLICABLE",
    }
    empty_attestation = _empty_audit_attestation()

    def current_attestation(value: dict[str, Any]) -> dict[str, Any]:
        return _attestation_with_network_count(value, network_boundary)

    def network_rejection(
        operation: str | None,
        attestation: dict[str, Any],
        reconstructed_hash: str | None,
    ) -> dict[str, Any]:
        checks["independent_graph_reconstruction"] = "FAIL"
        checks["candidate_semantics"] = "NOT_APPLICABLE"
        observed = current_attestation(attestation)
        return _audit_record(
            status="rejected",
            verdict="REJECT",
            reason_code="independent_audit_contract_invalid",
            diagnostic_code="edge_audit_outbound_network_attempted",
            operation=operation,
            candidate_sha256=candidate_hash,
            registration_sha256=registration_hash,
            question_sha256=question_hash,
            reference_date=reference_date,
            graph_snapshot_id=observed.get("graph_snapshot_id"),
            reconstructed_semantics_sha256=reconstructed_hash,
            checks=checks,
            audit_attestation=observed,
        )
    try:
        _validate_candidate_contract(candidate)
    except AuditContractError as exc:
        checks["candidate_contract"] = "FAIL"
        return _audit_record(
            status="rejected",
            verdict="REJECT",
            reason_code="candidate_contract_invalid",
            diagnostic_code=exc.code,
            operation=None,
            candidate_sha256=candidate_hash,
            registration_sha256=registration_hash,
            question_sha256=question_hash,
            reference_date=reference_date,
            graph_snapshot_id=None,
            reconstructed_semantics_sha256=None,
            checks=checks,
            audit_attestation=current_attestation(empty_attestation),
        )
    checks["candidate_contract"] = "PASS"
    classification = classify_question(normalized_question)
    checks["question_classification"] = "PASS"

    if not classification["applicable"]:
        expected = _candidate_result(
            status="not_applicable",
            decision="NOT_APPLICABLE",
            operation=None,
            reason_code="question_operation_unsupported",
            answer_text="",
            asserted_facts=[],
            asserted_relations=[],
            trace=_empty_trace("NOT_APPLICABLE", reference_date),
            runtime_attestation=None,
        )
        checks["registered_storage_integrity"] = "NOT_APPLICABLE"
        checks["independent_graph_reconstruction"] = "PASS"
        expected_semantics = deterministic_candidate_semantics(expected)
        actual_semantics = deterministic_candidate_semantics(candidate)
        reconstructed_hash = sha256_value(expected_semantics)
        if actual_semantics != expected_semantics:
            checks["candidate_semantics"] = "FAIL"
            return _audit_record(
                status="rejected",
                verdict="REJECT",
                reason_code="candidate_semantics_mismatch",
                diagnostic_code=_mismatch_code(
                    actual_semantics, expected_semantics
                ),
                operation=None,
                candidate_sha256=candidate_hash,
                registration_sha256=registration_hash,
                question_sha256=question_hash,
                reference_date=reference_date,
                graph_snapshot_id=None,
                reconstructed_semantics_sha256=reconstructed_hash,
                checks=checks,
                audit_attestation=current_attestation(empty_attestation),
            )
        checks["candidate_semantics"] = "PASS"
        if network_boundary is not None and network_boundary.attempt_count:
            return network_rejection(None, empty_attestation, reconstructed_hash)
        return _audit_record(
            status="passed",
            verdict="PASS",
            reason_code=None,
            diagnostic_code=None,
            operation=None,
            candidate_sha256=candidate_hash,
            registration_sha256=registration_hash,
            question_sha256=question_hash,
            reference_date=reference_date,
            graph_snapshot_id=None,
            reconstructed_semantics_sha256=reconstructed_hash,
            checks=checks,
            audit_attestation=current_attestation(empty_attestation),
        )

    operation = classification["operation"]
    loaded: LoadedGraph | None = None
    try:
        loaded = load_registered_graph(Path(index_path), registration)
        checks["registered_storage_integrity"] = "PASS"
        expected = reconstruct_candidate(
            loaded, normalized_question, operation, reference_date
        )
        checks["independent_graph_reconstruction"] = "PASS"
    except AuditContractError as exc:
        if checks["registered_storage_integrity"] != "PASS":
            checks["registered_storage_integrity"] = "FAIL"
        else:
            checks["independent_graph_reconstruction"] = "FAIL"
        failure_attestation = (
            exc.partial_attestation
            if exc.partial_attestation is not None
            else (
                loaded.audit_attestation
                if loaded is not None
                else empty_attestation
            )
        )
        observed = current_attestation(failure_attestation)
        return _audit_record(
            status="rejected",
            verdict="REJECT",
            reason_code="independent_audit_contract_invalid",
            diagnostic_code=exc.code,
            operation=operation,
            candidate_sha256=candidate_hash,
            registration_sha256=registration_hash,
            question_sha256=question_hash,
            reference_date=reference_date,
            graph_snapshot_id=observed.get("graph_snapshot_id"),
            reconstructed_semantics_sha256=None,
            checks=checks,
            audit_attestation=observed,
        )
    except OutboundNetworkDenied:
        failure_attestation = (
            loaded.audit_attestation
            if loaded is not None
            else empty_attestation
        )
        return network_rejection(operation, failure_attestation, None)
    assert loaded is not None
    expected_semantics = deterministic_candidate_semantics(expected)
    actual_semantics = deterministic_candidate_semantics(candidate)
    reconstructed_hash = sha256_value(expected_semantics)
    if actual_semantics != expected_semantics:
        checks["candidate_semantics"] = "FAIL"
        return _audit_record(
            status="rejected",
            verdict="REJECT",
            reason_code="candidate_semantics_mismatch",
            diagnostic_code=_mismatch_code(actual_semantics, expected_semantics),
            operation=operation,
            candidate_sha256=candidate_hash,
            registration_sha256=registration_hash,
            question_sha256=question_hash,
            reference_date=reference_date,
            graph_snapshot_id=loaded.snapshot.graph_snapshot_id,
            reconstructed_semantics_sha256=reconstructed_hash,
            checks=checks,
            audit_attestation=current_attestation(loaded.audit_attestation),
        )
    checks["candidate_semantics"] = "PASS"
    if network_boundary is not None and network_boundary.attempt_count:
        return network_rejection(
            operation,
            loaded.audit_attestation,
            reconstructed_hash,
        )
    return _audit_record(
        status="passed",
        verdict="PASS",
        reason_code=None,
        diagnostic_code=None,
        operation=operation,
        candidate_sha256=candidate_hash,
        registration_sha256=registration_hash,
        question_sha256=question_hash,
        reference_date=reference_date,
        graph_snapshot_id=loaded.snapshot.graph_snapshot_id,
        reconstructed_semantics_sha256=reconstructed_hash,
        checks=checks,
        audit_attestation=current_attestation(loaded.audit_attestation),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Independently audit one semantic-graph query candidate."
    )
    parser.add_argument("--request-file", required=True, type=Path)
    parser.add_argument("--candidate-file", required=True, type=Path)
    return parser


def _validate_request(request: dict[str, Any]) -> None:
    if set(request) != REQUEST_FIELDS:
        _fail("edge_audit_request_fields_invalid")
    if request.get("schema_version") != SCHEMA_VERSION:
        _fail("edge_audit_request_schema_invalid")
    if (
        not isinstance(request.get("question"), str)
        or not request["question"].strip()
        or not isinstance(request.get("index_path"), str)
        or not request["index_path"]
        or not Path(request["index_path"]).is_absolute()
        or not isinstance(request.get("registration"), dict)
    ):
        _fail("edge_audit_request_contract_invalid")
    try:
        _strict_reference_date(request.get("question_reference_date"))
    except ValueError as exc:
        raise AuditContractError(
            "edge_audit_request_reference_date_invalid"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    network_boundary = install_deny_network_boundary()
    args = _parser().parse_args(argv)
    request = _private_canonical_object_file(
        args.request_file, "edge_audit_request_file"
    )
    _validate_request(request)
    candidate = _private_canonical_object_file(
        args.candidate_file, "edge_audit_candidate_file"
    )
    result = audit_candidate(
        Path(request["index_path"]),
        request["question"],
        request["registration"],
        candidate,
        reference_date=request["question_reference_date"],
        network_boundary=network_boundary,
    )
    sys.stdout.write(canonical_json(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
