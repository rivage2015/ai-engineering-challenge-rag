#!/usr/bin/env python3
"""Safely evaluate a persisted cross-document graph as a dual-run candidate.

Questions outside the three bounded operations are classified before SQLite is
opened. Applicable questions load the Step 2 projection and answer-safe
Evidence in one read transaction, then reuse the frozen traversal and answer
logic from query_cross_document_semantic_graph.py.

This module never enables retrieval and never replaces the legacy answer path.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import stat
import sys
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable


SCHEMA_VERSION = "0.1"
RUNTIME_ADAPTER = "cross-document-semantic-graph-runtime"
RUNTIME_ADAPTER_VERSION = "0.1.0"
GENERATION_PATTERN = re.compile(r"generation-[0-9a-f]{32}")
INDEX_DIRECTORY = "05-semantic-answer-index"
INDEX_FILENAME = "safe-answer-index.sqlite3"
STATE_FILENAME = "semantic-answer-index-state.json"
METADATA_PREFIX = "cross_document_semantic_graph_"
SUPPORTED_OPERATIONS = frozenset({
    "owner",
    "assignment_change",
    "version_change",
})
OPERATION_FACT_FIELDS = {
    "owner": frozenset({
        "reference_time", "role", "assignee_id", "assignee_name",
    }),
    "assignment_change": frozenset({
        "change_effective_date", "previous_valid_to",
        "from_assignee_id", "from_assignee_name",
        "to_assignee_id", "to_assignee_name",
    }),
    "version_change": frozenset({
        "effective_from", "old_plan_status", "old_plan_assignee_id",
        "old_plan_assignee_name", "current_plan_status",
        "current_plan_assignee_id", "current_plan_assignee_name",
        "change_reason",
    }),
}
OPERATION_RELATION_TYPES = {
    "owner": frozenset(),
    "assignment_change": frozenset(),
    "version_change": frozenset({"SUPERSEDES", "CONTRADICTS"}),
}
SEMANTIC_TABLES = frozenset({
    "semantic_graph_nodes",
    "semantic_graph_edges",
    "semantic_graph_edge_evidence",
})
SEMANTIC_INDEXES = frozenset({
    "semantic_graph_nodes_type_key_idx",
    "semantic_graph_edges_from_type_idx",
    "semantic_graph_edges_to_type_idx",
    "semantic_graph_edge_evidence_evidence_idx",
})
SEMANTIC_OBJECTS = SEMANTIC_TABLES | SEMANTIC_INDEXES
REGISTRATION_FIELDS = frozenset({
    "schema_version",
    "status",
    "generation",
    "database_path",
    "database_sha256",
    "state_path",
    "state_sha256",
    "base_index_path",
    "base_index_sha256",
    "graph_snapshot_id",
    "logical_snapshot_sha256",
    "counts",
    "retrieval_enabled",
    "used_for_answers",
})
SEMANTIC_METADATA_KEYS = frozenset({
    METADATA_PREFIX + "storage_schema_version",
    METADATA_PREFIX + "storage_status",
    METADATA_PREFIX + "retrieval_enabled",
    METADATA_PREFIX + "used_for_answers",
    METADATA_PREFIX + "question_independent",
    METADATA_PREFIX + "external_network_used",
    METADATA_PREFIX + "snapshot_id",
    METADATA_PREFIX + "logical_snapshot_sha256",
    METADATA_PREFIX + "source_sqlite_sha256",
    METADATA_PREFIX + "builder_state_sha256",
    METADATA_PREFIX + "validation_state_sha256",
    METADATA_PREFIX + "shadow_run_state_sha256",
    METADATA_PREFIX + "documents_input_sha256",
    METADATA_PREFIX + "source_evidence_input_sha256",
    METADATA_PREFIX + "evidence_input_sha256",
    METADATA_PREFIX + "content_security_state_sha256",
    METADATA_PREFIX + "content_security_outputs_sha256",
    METADATA_PREFIX + "node_count",
    METADATA_PREFIX + "edge_count",
    METADATA_PREFIX + "edge_evidence_count",
    METADATA_PREFIX + "projection_sha256",
    METADATA_PREFIX + "base_logical_snapshot_sha256",
})
EXPECTED_COLUMNS = {
    "semantic_graph_nodes": (
        "node_id", "node_type", "canonical_key", "status",
        "properties_json", "record_sha256",
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
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class RuntimeGraphContractError(ValueError):
    """A persisted runtime graph failed its immutable projection contract."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int


@dataclass(frozen=True)
class LoadedRuntimeGraph:
    snapshot: Any
    attestation: dict[str, Any]
    eligible_evidence_ids: frozenset[str]


def _fail(code: str) -> None:
    raise RuntimeGraphContractError(code)


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None


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
        raise RuntimeGraphContractError(f"{label}_invalid_json") from exc


def _strict_object(value: str | bytes, label: str) -> dict[str, Any]:
    parsed = _strict_json(value, label)
    if not isinstance(parsed, dict):
        _fail(f"{label}_object_required")
    return parsed


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _regular_file_bytes(path: Path, label: str) -> tuple[bytes, FileIdentity]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeGraphContractError(f"{label}_unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            _fail(f"{label}_not_single_regular_file")
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
            or after.st_nlink != 1
        ):
            _fail(f"{label}_changed_while_reading")
        try:
            current = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise RuntimeGraphContractError(f"{label}_path_changed") from exc
        if (
            current.st_dev != after.st_dev
            or current.st_ino != after.st_ino
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
        ):
            _fail(f"{label}_path_changed")
        identity = FileIdentity(
            device=after.st_dev,
            inode=after.st_ino,
            size=after.st_size,
            modified_ns=after.st_mtime_ns,
        )
        return b"".join(blocks), identity
    finally:
        os.close(descriptor)


def _sha256_regular_file(path: Path, label: str) -> tuple[str, FileIdentity]:
    """Hash a stable single-link regular file without buffering it in RAM."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeGraphContractError(f"{label}_unavailable") from exc
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
        try:
            current = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise RuntimeGraphContractError(f"{label}_path_changed") from exc
        if (
            current.st_dev != after.st_dev
            or current.st_ino != after.st_ino
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
        ):
            _fail(f"{label}_path_changed")
        return digest.hexdigest(), FileIdentity(
            device=after.st_dev,
            inode=after.st_ino,
            size=after.st_size,
            modified_ns=after.st_mtime_ns,
        )
    finally:
        os.close(descriptor)


def _module_from_candidates(
    module_name: str,
    candidates: Iterable[Path],
    error_code: str,
) -> ModuleType:
    target = next(
        (
            candidate
            for candidate in candidates
            if candidate.is_file() and not candidate.is_symlink()
        ),
        None,
    )
    if target is None:
        _fail(error_code + "_missing")
    specification = importlib.util.spec_from_file_location(module_name, target)
    if specification is None or specification.loader is None:
        _fail(error_code + "_unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    try:
        specification.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise RuntimeGraphContractError(error_code + "_load_failed") from exc
    return module


@lru_cache(maxsize=1)
def _query_contract() -> ModuleType:
    here = Path(__file__).resolve()
    return _module_from_candidates(
        "_local_memory_cross_document_query_contract",
        (
            here.parent / "layer1" / "scripts"
            / "query_cross_document_semantic_graph.py",
            here.parents[3] / "scripts" / "query_cross_document_semantic_graph.py",
        ),
        "semantic_query_contract",
    )


@lru_cache(maxsize=1)
def _answer_contract() -> ModuleType:
    here = Path(__file__).resolve()
    return _module_from_candidates(
        "_local_memory_answer_graph_contract",
        (here.with_name("answer_local_memory.py"),),
        "answer_graph_contract",
    )


def classify_question(question: str) -> dict[str, Any]:
    """Classify a question without touching the runtime SQLite or state file."""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")
    query = _query_contract()
    try:
        operation = query._operation(question)
    except query.ResolutionError as exc:
        if exc.reason_code != "question_operation_unsupported":
            raise RuntimeGraphContractError(
                "semantic_question_classification_failed"
            ) from exc
        return {
            "applicable": False,
            "operation": None,
            "reason_code": "question_operation_unsupported",
        }
    if operation not in SUPPORTED_OPERATIONS:
        _fail("semantic_question_operation_contract_invalid")
    return {
        "applicable": True,
        "operation": operation,
        "reason_code": None,
    }


def _validate_runtime_paths(
    index_path: Path,
    expected_generation: str | None,
) -> tuple[Path, Path, str]:
    index = Path(index_path)
    if index.name != INDEX_FILENAME or index.is_symlink() or not index.is_file():
        _fail("semantic_runtime_index_path_invalid")
    storage = index.parent
    generation = storage.parent
    if (
        storage.name != INDEX_DIRECTORY
        or storage.is_symlink()
        or not storage.is_dir()
        or generation.is_symlink()
        or not generation.is_dir()
        or GENERATION_PATTERN.fullmatch(generation.name) is None
    ):
        _fail("semantic_runtime_generation_layout_invalid")
    if expected_generation is not None and generation.name != expected_generation:
        _fail("semantic_runtime_generation_mismatch")
    state = storage / STATE_FILENAME
    if state.is_symlink() or not state.is_file():
        _fail("semantic_runtime_projection_state_invalid")
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = index.with_name(index.name + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            _fail("semantic_runtime_index_sidecar_present")
    try:
        resolved = index.resolve(strict=True)
        resolved_state = state.resolve(strict=True)
        generation_root = generation.resolve(strict=True)
        resolved.relative_to(generation_root)
        resolved_state.relative_to(generation_root)
    except (OSError, ValueError) as exc:
        raise RuntimeGraphContractError(
            "semantic_runtime_index_boundary_invalid"
        ) from exc
    if resolved != generation_root / INDEX_DIRECTORY / INDEX_FILENAME:
        _fail("semantic_runtime_index_layout_invalid")
    if resolved_state != generation_root / INDEX_DIRECTORY / STATE_FILENAME:
        _fail("semantic_runtime_projection_state_layout_invalid")
    return resolved, resolved_state, generation.name


def _validate_projection_state(
    state: dict[str, Any],
    *,
    generation: str,
    index_sha256: str,
    expected_build_id: str | None,
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
        _fail("semantic_runtime_projection_state_contract_invalid")
    expected_top_level = set(required) | {
        "base", "shadow", "inputs", "counts", "projection_sha256", "output",
    }
    if set(state) != expected_top_level:
        _fail("semantic_runtime_projection_state_fields_invalid")
    output = state.get("output")
    if output != {
        "sqlite_file": INDEX_FILENAME,
        "state_file": STATE_FILENAME,
        "sqlite_sha256": index_sha256,
    }:
        _fail("semantic_runtime_projection_output_binding_invalid")

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
        _fail("semantic_runtime_projection_base_binding_invalid")

    shadow = state.get("shadow")
    if (
        not isinstance(shadow, dict)
        or set(shadow) != {
            "directory", "build_id", "graph_snapshot_id",
            "logical_snapshot_sha256", "sqlite_sha256",
            "builder_state_sha256", "validation_state_sha256",
            "run_state_sha256",
        }
        or shadow.get("directory") != "04-semantic-graph-shadow"
        or not isinstance(shadow.get("build_id"), str)
        or not shadow["build_id"].strip()
        or not isinstance(shadow.get("graph_snapshot_id"), str)
        or not shadow["graph_snapshot_id"].startswith("xkgs_")
        or any(
            not _is_sha256(shadow.get(key))
            for key in (
                "logical_snapshot_sha256", "sqlite_sha256",
                "builder_state_sha256", "validation_state_sha256",
                "run_state_sha256",
            )
        )
    ):
        _fail("semantic_runtime_projection_shadow_binding_invalid")
    if expected_build_id is not None and shadow["build_id"] != expected_build_id:
        _fail("semantic_runtime_build_id_mismatch")

    inputs = state.get("inputs")
    if (
        not isinstance(inputs, dict)
        or set(inputs) != {
            "documents_input_sha256", "source_evidence_input_sha256",
            "evidence_input_sha256", "content_security_state_sha256",
        }
        or any(not _is_sha256(value) for value in inputs.values())
    ):
        _fail("semantic_runtime_projection_inputs_invalid")
    counts = state.get("counts")
    if (
        not isinstance(counts, dict)
        or set(counts) != {"nodes", "edges", "edge_evidence"}
        or any(type(value) is not int or value < 1 for value in counts.values())
        or not _is_sha256(state.get("projection_sha256"))
    ):
        _fail("semantic_runtime_projection_counts_invalid")


def _validate_registration_anchor(
    registration: dict[str, Any],
    *,
    index: Path,
    state_path: Path,
    generation: str,
    index_sha256: str,
    state_bytes: bytes,
    state: dict[str, Any],
) -> None:
    """Bind the self-consistent pair to the independently published CONFIG."""
    if set(registration) != REGISTRATION_FIELDS:
        _fail("semantic_runtime_registration_fields_invalid")
    logical_sha256 = registration.get("logical_snapshot_sha256")
    graph_snapshot_id = registration.get("graph_snapshot_id")
    counts = registration.get("counts")
    expected_state = index.parent / STATE_FILENAME
    expected_base = index.parent.parent / INDEX_FILENAME
    required = {
        "schema_version": SCHEMA_VERSION,
        "status": "validated_storage_only",
        "generation": generation,
        "database_path": str(index),
        "database_sha256": index_sha256,
        "state_path": str(expected_state),
        "state_sha256": hashlib.sha256(state_bytes).hexdigest(),
        "base_index_path": str(expected_base),
        "retrieval_enabled": False,
        "used_for_answers": False,
    }
    if any(registration.get(key) != value for key, value in required.items()):
        _fail("semantic_runtime_registration_binding_invalid")
    if state_path != expected_state:
        _fail("semantic_runtime_registration_state_path_invalid")
    if (
        not _is_sha256(registration.get("base_index_sha256"))
        or not _is_sha256(logical_sha256)
        or graph_snapshot_id != "xkgs_" + logical_sha256[:32]
        or graph_snapshot_id != state["shadow"]["graph_snapshot_id"]
        or logical_sha256 != state["shadow"]["logical_snapshot_sha256"]
        or counts != state["counts"]
        or not isinstance(counts, dict)
        or set(counts) != {"nodes", "edges", "edge_evidence"}
        or any(type(value) is not int or value < 1 for value in counts.values())
        or registration["base_index_sha256"]
        != state["base"]["sqlite_sha256"]
    ):
        _fail("semantic_runtime_registration_contract_invalid")
    base_sha256, _base_identity = _sha256_regular_file(
        expected_base,
        "semantic_runtime_registered_base_index",
    )
    if base_sha256 != registration["base_index_sha256"]:
        _fail("semantic_runtime_registered_base_index_mismatch")


def _read_metadata(connection: sqlite3.Connection) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for row in connection.execute("SELECT key, value FROM metadata ORDER BY key"):
        key = row["key"]
        if not isinstance(key, str) or key in metadata:
            _fail("semantic_runtime_metadata_key_invalid")
        metadata[key] = _strict_json(
            row["value"], "semantic_runtime_metadata_value"
        )
    semantic_keys = frozenset(
        key for key in metadata if key.startswith(METADATA_PREFIX)
    )
    if semantic_keys != SEMANTIC_METADATA_KEYS:
        _fail("semantic_runtime_metadata_fields_invalid")
    return metadata


def _validate_semantic_metadata(
    metadata: dict[str, Any],
    state: dict[str, Any],
) -> None:
    required = {
        METADATA_PREFIX + "storage_schema_version": SCHEMA_VERSION,
        METADATA_PREFIX + "storage_status": "validated_storage_only",
        METADATA_PREFIX + "retrieval_enabled": False,
        METADATA_PREFIX + "used_for_answers": False,
        METADATA_PREFIX + "question_independent": True,
        METADATA_PREFIX + "external_network_used": False,
    }
    if any(metadata.get(key) != value for key, value in required.items()):
        _fail("semantic_runtime_metadata_status_invalid")
    for key in (
        "logical_snapshot_sha256",
        "source_sqlite_sha256",
        "builder_state_sha256",
        "validation_state_sha256",
        "shadow_run_state_sha256",
        "documents_input_sha256",
        "source_evidence_input_sha256",
        "evidence_input_sha256",
        "content_security_state_sha256",
        "content_security_outputs_sha256",
        "projection_sha256",
        "base_logical_snapshot_sha256",
    ):
        if not _is_sha256(metadata.get(METADATA_PREFIX + key)):
            _fail(f"semantic_runtime_metadata_hash_invalid:{key}")
    snapshot_id = metadata.get(METADATA_PREFIX + "snapshot_id")
    logical_hash = metadata[METADATA_PREFIX + "logical_snapshot_sha256"]
    if snapshot_id != "xkgs_" + logical_hash[:32]:
        _fail("semantic_runtime_snapshot_identity_invalid")
    for key in ("node_count", "edge_count", "edge_evidence_count"):
        value = metadata.get(METADATA_PREFIX + key)
        if type(value) is not int or value < 1:
            _fail(f"semantic_runtime_metadata_count_invalid:{key}")

    shadow = state["shadow"]
    inputs = state["inputs"]
    counts = state["counts"]
    bindings = {
        "snapshot_id": shadow["graph_snapshot_id"],
        "logical_snapshot_sha256": shadow["logical_snapshot_sha256"],
        "source_sqlite_sha256": shadow["sqlite_sha256"],
        "builder_state_sha256": shadow["builder_state_sha256"],
        "validation_state_sha256": shadow["validation_state_sha256"],
        "shadow_run_state_sha256": shadow["run_state_sha256"],
        "documents_input_sha256": inputs["documents_input_sha256"],
        "source_evidence_input_sha256": inputs[
            "source_evidence_input_sha256"
        ],
        "evidence_input_sha256": inputs["evidence_input_sha256"],
        "content_security_state_sha256": inputs[
            "content_security_state_sha256"
        ],
        "node_count": counts["nodes"],
        "edge_count": counts["edges"],
        "edge_evidence_count": counts["edge_evidence"],
        "projection_sha256": state["projection_sha256"],
        "base_logical_snapshot_sha256": state["base"][
            "logical_snapshot_sha256"
        ],
    }
    if any(
        metadata.get(METADATA_PREFIX + key) != value
        for key, value in bindings.items()
    ):
        _fail("semantic_runtime_metadata_state_binding_mismatch")


def _validate_schema(connection: sqlite3.Connection) -> None:
    objects = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE name LIKE 'semantic_graph_%'"
        )
    }
    if objects != SEMANTIC_OBJECTS:
        _fail("semantic_runtime_schema_objects_invalid")
    for table, expected in EXPECTED_COLUMNS.items():
        columns = tuple(
            row["name"]
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        )
        if columns != expected:
            _fail(f"semantic_runtime_schema_columns_invalid:{table}")


def _source_sha256_from_payload(payload: dict[str, Any], evidence_id: str) -> str:
    source_record = payload.get("source_record")
    source = (
        source_record.get("source")
        if isinstance(source_record, dict)
        else None
    )
    source_sha256 = source.get("sha256") if isinstance(source, dict) else None
    if not _is_sha256(source_sha256):
        _fail(f"semantic_runtime_evidence_source_hash_missing:{evidence_id}")
    return source_sha256


def _load_semantic_snapshot(
    connection: sqlite3.Connection,
    metadata: dict[str, Any],
    eligible_evidence_ids: frozenset[str],
) -> Any:
    query = _query_contract()
    nodes: dict[str, Any] = {}
    for row in connection.execute(
        "SELECT node_id, node_type, canonical_key, status, properties_json, "
        "record_sha256 FROM semantic_graph_nodes ORDER BY node_id"
    ):
        node_id = row["node_id"]
        properties = _strict_object(
            row["properties_json"], f"semantic_node_properties:{node_id}"
        )
        if row["properties_json"] != _canonical_json(properties):
            _fail(f"semantic_runtime_node_json_not_canonical:{node_id}")
        payload = {
            "node_id": node_id,
            "node_type": row["node_type"],
            "canonical_key": row["canonical_key"],
            "status": row["status"],
            "properties": properties,
        }
        if not _is_sha256(row["record_sha256"]) or (
            query.sha256_value(payload) != row["record_sha256"]
        ):
            _fail(f"semantic_runtime_node_hash_mismatch:{node_id}")
        if (
            not isinstance(node_id, str)
            or not node_id
            or node_id in nodes
            or row["node_type"] not in query.NODE_TYPES
            or row["status"] != "verified"
            or not isinstance(row["canonical_key"], str)
            or not row["canonical_key"].strip()
        ):
            _fail(f"semantic_runtime_node_contract_invalid:{node_id}")
        nodes[node_id] = query.Node(
            node_id=node_id,
            node_type=row["node_type"],
            canonical_key=row["canonical_key"],
            status=row["status"],
            properties=properties,
            record_sha256=row["record_sha256"],
        )

    evidence_payloads: dict[str, dict[str, Any]] = {}
    evidence_statuses: dict[str, str] = {}
    for row in connection.execute(
        "SELECT node_id, payload_json, status FROM graph_nodes "
        "WHERE node_type = 'evidence' ORDER BY node_id"
    ):
        evidence_payloads[row["node_id"]] = _strict_object(
            row["payload_json"],
            f"semantic_evidence_node_payload:{row['node_id']}",
        )
        evidence_statuses[row["node_id"]] = row["status"]

    evidence: dict[str, Any] = {}
    for row in connection.execute(
        "SELECT evidence_id, document_id, relative_path, locator_json, "
        "observed_text, observed_sha256 FROM evidence ORDER BY evidence_id"
    ):
        evidence_id = row["evidence_id"]
        locator = _strict_object(
            row["locator_json"], f"semantic_evidence_locator:{evidence_id}"
        )
        if (
            not isinstance(evidence_id, str)
            or not evidence_id
            or evidence_id in evidence
            or not isinstance(row["document_id"], str)
            or not row["document_id"]
            or not isinstance(row["relative_path"], str)
            or not row["relative_path"]
            or not isinstance(row["observed_text"], str)
            or hashlib.sha256(
                row["observed_text"].encode("utf-8")
            ).hexdigest() != row["observed_sha256"]
        ):
            _fail(f"semantic_runtime_evidence_contract_invalid:{evidence_id}")
        node_payload = evidence_payloads.get(evidence_id)
        if node_payload is None:
            _fail(f"semantic_runtime_evidence_node_missing:{evidence_id}")
        source_sha256 = _source_sha256_from_payload(node_payload, evidence_id)
        core = {
            "evidence_id": evidence_id,
            "document_id": row["document_id"],
            "relative_path": row["relative_path"],
            "source_sha256": source_sha256,
            "locator": locator,
            "observed_text": row["observed_text"],
            "observed_sha256": row["observed_sha256"],
        }
        evidence[evidence_id] = query.SourceEvidence(
            evidence_id=evidence_id,
            document_id=row["document_id"],
            relative_path=row["relative_path"],
            source_sha256=source_sha256,
            locator=locator,
            observed_text=row["observed_text"],
            observed_sha256=row["observed_sha256"],
            record_sha256=query.sha256_value(core),
        )
    if set(evidence) != set(evidence_payloads):
        _fail("semantic_runtime_evidence_universe_mismatch")

    support: dict[str, list[str]] = {}
    support_pairs: set[tuple[str, str]] = set()
    for row in connection.execute(
        "SELECT edge_id, evidence_id FROM semantic_graph_edge_evidence "
        "ORDER BY edge_id, evidence_id"
    ):
        pair = (row["edge_id"], row["evidence_id"])
        if pair in support_pairs:
            _fail("semantic_runtime_support_duplicate")
        support_pairs.add(pair)
        evidence_id = row["evidence_id"]
        if evidence_id not in evidence:
            _fail(f"semantic_runtime_support_evidence_missing:{evidence_id}")
        if (
            evidence_id not in eligible_evidence_ids
            or evidence_statuses.get(evidence_id) not in {"observed", "verified"}
            or evidence_payloads[evidence_id].get("security_graph_hold") is not None
        ):
            _fail(f"semantic_runtime_support_not_verified:{evidence_id}")
        support.setdefault(row["edge_id"], []).append(evidence_id)

    edges: dict[str, Any] = {}
    for row in connection.execute(
        "SELECT edge_id, from_node_id, relation_type, to_node_id, "
        "relation_class, status, basis_kind, basis_rule, properties_json, "
        "record_sha256 FROM semantic_graph_edges ORDER BY edge_id"
    ):
        edge_id = row["edge_id"]
        properties = _strict_object(
            row["properties_json"], f"semantic_edge_properties:{edge_id}"
        )
        if row["properties_json"] != _canonical_json(properties):
            _fail(f"semantic_runtime_edge_json_not_canonical:{edge_id}")
        supporting_ids = tuple(sorted(support.get(edge_id, [])))
        payload = {
            "edge_id": edge_id,
            "from_node_id": row["from_node_id"],
            "relation_type": row["relation_type"],
            "to_node_id": row["to_node_id"],
            "relation_class": row["relation_class"],
            "status": row["status"],
            "basis_kind": row["basis_kind"],
            "basis_rule": row["basis_rule"],
            "properties": properties,
            "supporting_evidence_ids": list(supporting_ids),
        }
        endpoint_types = query.RELATION_ENDPOINT_TYPES.get(row["relation_type"])
        actual_endpoint_types = (
            nodes[row["from_node_id"]].node_type
            if row["from_node_id"] in nodes
            else None,
            nodes[row["to_node_id"]].node_type
            if row["to_node_id"] in nodes
            else None,
        )
        if (
            not isinstance(edge_id, str)
            or not edge_id
            or edge_id in edges
            or not supporting_ids
            or endpoint_types is None
            or actual_endpoint_types != endpoint_types
            or row["relation_class"] != "semantic"
            or row["status"] != "verified"
            or not isinstance(row["basis_kind"], str)
            or not row["basis_kind"].strip()
            or not isinstance(row["basis_rule"], str)
            or not row["basis_rule"].strip()
            or not _is_sha256(row["record_sha256"])
            or query.sha256_value(payload) != row["record_sha256"]
        ):
            _fail(f"semantic_runtime_edge_contract_invalid:{edge_id}")
        edges[edge_id] = query.Edge(
            edge_id=edge_id,
            from_node_id=row["from_node_id"],
            relation_type=row["relation_type"],
            to_node_id=row["to_node_id"],
            relation_class=row["relation_class"],
            status=row["status"],
            basis_kind=row["basis_kind"],
            basis_rule=row["basis_rule"],
            properties=properties,
            supporting_evidence_ids=supporting_ids,
            record_sha256=row["record_sha256"],
        )
    unknown_support_edges = sorted(set(support) - set(edges))
    if unknown_support_edges:
        _fail("semantic_runtime_support_edge_missing")

    counts = {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "edge_evidence_count": len(support_pairs),
    }
    if any(
        metadata.get(METADATA_PREFIX + key) != value
        for key, value in counts.items()
    ):
        _fail("semantic_runtime_counts_mismatch")
    snapshot_payload = {
        "evidence_record_sha256": sorted(
            item.record_sha256 for item in evidence.values()
        ),
        "node_record_sha256": sorted(
            item.record_sha256 for item in nodes.values()
        ),
        "edge_record_sha256": sorted(
            item.record_sha256 for item in edges.values()
        ),
    }
    logical_snapshot_sha256 = query.sha256_value(snapshot_payload)
    if (
        logical_snapshot_sha256
        != metadata[METADATA_PREFIX + "logical_snapshot_sha256"]
    ):
        _fail("semantic_runtime_logical_snapshot_mismatch")
    graph_snapshot_id = "xkgs_" + logical_snapshot_sha256[:32]
    if graph_snapshot_id != metadata[METADATA_PREFIX + "snapshot_id"]:
        _fail("semantic_runtime_graph_snapshot_id_mismatch")
    projection = {
        "graph_snapshot_id": graph_snapshot_id,
        "node_record_sha256": sorted(
            item.record_sha256 for item in nodes.values()
        ),
        "edge_record_sha256": sorted(
            item.record_sha256 for item in edges.values()
        ),
        "edge_evidence": sorted(
            [edge_id, evidence_id]
            for edge_id, evidence_id in support_pairs
        ),
    }
    if (
        _sha256_json(projection)
        != metadata[METADATA_PREFIX + "projection_sha256"]
    ):
        _fail("semantic_runtime_projection_hash_mismatch")
    return query.GraphSnapshot(
        graph_snapshot_id,
        nodes,
        edges,
        evidence,
    )


def load_runtime_graph(
    index_path: Path,
    *,
    expected_generation: str | None = None,
    expected_build_id: str | None = None,
    expected_registration: dict[str, Any] | None = None,
) -> LoadedRuntimeGraph:
    """Load a validated in-memory graph from one SQLite read transaction."""
    if expected_generation is not None and (
        not isinstance(expected_generation, str)
        or GENERATION_PATTERN.fullmatch(expected_generation) is None
    ):
        raise ValueError("expected_generation must be generation plus 32 hex chars")
    if expected_build_id is not None and (
        not isinstance(expected_build_id, str) or not expected_build_id.strip()
    ):
        raise ValueError("expected_build_id must be a non-empty string")
    if expected_registration is not None and not isinstance(
        expected_registration, dict
    ):
        raise ValueError("expected_registration must be an object")

    index, state_path, generation = _validate_runtime_paths(
        Path(index_path), expected_generation
    )
    state_bytes, state_identity = _regular_file_bytes(
        state_path, "semantic_runtime_projection_state"
    )
    state = _strict_object(
        state_bytes, "semantic_runtime_projection_state"
    )
    index_sha256, index_identity = _sha256_regular_file(
        index, "semantic_runtime_index"
    )
    _validate_projection_state(
        state,
        generation=generation,
        index_sha256=index_sha256,
        expected_build_id=expected_build_id,
    )
    if expected_registration is not None:
        _validate_registration_anchor(
            expected_registration,
            index=index,
            state_path=state_path,
            generation=generation,
            index_sha256=index_sha256,
            state_bytes=state_bytes,
            state=state,
        )

    answer_contract = _answer_contract()
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            index.as_uri() + "?mode=ro&immutable=1",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN")
        quick_check = connection.execute("PRAGMA quick_check").fetchall()
        if len(quick_check) != 1 or quick_check[0][0] != "ok":
            _fail("semantic_runtime_sqlite_integrity_invalid")
        metadata = _read_metadata(connection)
        _validate_semantic_metadata(metadata, state)
        _validate_schema(connection)
        try:
            answer_policy = answer_contract.validate_answer_graph_contract(
                connection, metadata
            )
        except (ValueError, sqlite3.Error) as exc:
            raise RuntimeGraphContractError(
                "semantic_runtime_answer_graph_contract_invalid"
            ) from exc
        eligible = answer_policy.get("eligible_evidence_ids")
        if not isinstance(eligible, frozenset):
            _fail("semantic_runtime_answer_policy_invalid")
        snapshot = _load_semantic_snapshot(
            connection,
            metadata,
            eligible,
        )
        connection.commit()
    except sqlite3.Error as exc:
        raise RuntimeGraphContractError(
            "semantic_runtime_sqlite_read_failed"
        ) from exc
    finally:
        if connection is not None:
            connection.close()

    final_index_sha256, final_index_identity = _sha256_regular_file(
        index, "semantic_runtime_index"
    )
    final_state_bytes, final_state_identity = _regular_file_bytes(
        state_path, "semantic_runtime_projection_state"
    )
    if (
        final_index_sha256 != index_sha256
        or final_index_identity != index_identity
        or final_state_bytes != state_bytes
        or final_state_identity != state_identity
    ):
        _fail("semantic_runtime_artifact_changed_during_read")
    attestation = {
        "adapter": RUNTIME_ADAPTER,
        "adapter_version": RUNTIME_ADAPTER_VERSION,
        "read_only": True,
        "read_snapshot": "single_sqlite_transaction",
        "generation": generation,
        "build_id": state["shadow"]["build_id"],
        "index_sha256": index_sha256,
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
        "eligible_evidence_count": len(eligible),
        "outbound_network_attempt_count": 0,
    }
    return LoadedRuntimeGraph(
        snapshot=snapshot,
        attestation=attestation,
        eligible_evidence_ids=eligible,
    )


def _edge_proves_fact(
    query: ModuleType,
    snapshot: Any,
    edge: Any,
    field: str,
    value: str,
) -> bool:
    """Recompute whether one traversed Edge actually supports a fact."""

    target = snapshot.nodes[edge.to_node_id]
    if field == "reference_time":
        try:
            reference = date.fromisoformat(value)
            start = query._effective_start(edge)
            end = query._effective_end(edge)
        except (ValueError, query.ResolutionError):
            return False
        return start is not None and start <= reference and (
            end is None or reference <= end
        )
    if field == "role":
        return edge.properties.get("role") == value
    if field.endswith("assignee_id") or field == "assignee_id":
        return target.node_type == "Employee" and target.canonical_key == value
    if field.endswith("assignee_name") or field == "assignee_name":
        return target.node_type == "Person" and target.canonical_key == value
    if field == "change_reason":
        return target.node_type == "Reason" and target.canonical_key == value
    property_candidates = {
        "change_effective_date": ("valid_from", "effective_from"),
        "previous_valid_to": ("valid_to",),
        "effective_from": ("effective_from",),
        "old_plan_status": ("claim_status",),
        "current_plan_status": ("claim_status",),
    }.get(field)
    if property_candidates is None:
        return False
    if any(edge.properties.get(key) == value for key in property_candidates):
        return True
    return False


def _used_edges_are_connected(snapshot: Any, edge_ids: list[str]) -> bool:
    if not edge_ids:
        return True
    adjacency: dict[str, set[str]] = {}
    for edge_id in edge_ids:
        edge = snapshot.edges[edge_id]
        adjacency.setdefault(edge.from_node_id, set()).add(edge.to_node_id)
        adjacency.setdefault(edge.to_node_id, set()).add(edge.from_node_id)
    pending = [next(iter(adjacency))]
    reached: set[str] = set()
    while pending:
        node_id = pending.pop()
        if node_id in reached:
            continue
        reached.add(node_id)
        pending.extend(adjacency[node_id] - reached)
    return reached == set(adjacency)


def _validate_answer_result(
    answer: dict[str, Any],
    loaded: LoadedRuntimeGraph,
    expected_operation: str,
    question: str,
    disabled_edge_ids: tuple[str, ...],
    reference_date: str | None,
) -> None:
    query = _query_contract()
    if (
        not isinstance(answer, dict)
        or answer.get("schema_version") != query.SCHEMA_VERSION
        or answer.get("record_type")
        != "cross_document_semantic_graph_answer"
        or answer.get("answerer") != query.ANSWERER
        or answer.get("operation") != expected_operation
        or answer.get("decision") not in {"ACCEPTED", "HOLD"}
        or not isinstance(answer.get("answer_text"), str)
        or not isinstance(answer.get("asserted_facts"), list)
        or not isinstance(answer.get("asserted_relations"), list)
        or not isinstance(answer.get("trace"), dict)
    ):
        _fail("semantic_runtime_answer_contract_invalid")
    trace = answer["trace"]
    used_edge_ids = trace.get("used_semantic_edge_ids")
    expected_question_hash = query.sha256_text(
        query._normalize_surface(question)
    )
    expected_disabled = sorted(set(disabled_edge_ids))
    expected_run_id = query.RUN_PREFIX + query.sha256_value({
        "graph_snapshot_id": loaded.snapshot.graph_snapshot_id,
        "question_hash": expected_question_hash,
        "disabled_edge_ids": expected_disabled,
        **(
            {"question_reference_date": reference_date}
            if reference_date is not None else {}
        ),
    })[:32]
    expected_edge_statuses = sorted({
        loaded.snapshot.edges[edge_id].status
        for edge_id in used_edge_ids
        if edge_id in loaded.snapshot.edges
    })
    if (
        not isinstance(used_edge_ids, list)
        or len(used_edge_ids) != len(set(used_edge_ids))
        or any(edge_id not in loaded.snapshot.edges for edge_id in used_edge_ids)
        or trace.get("graph_snapshot_id") != loaded.snapshot.graph_snapshot_id
        or trace.get("question_hash") != expected_question_hash
        or trace.get("question_reference_date") != reference_date
        or trace.get("run_id") != expected_run_id
        or trace.get("decision") != answer["decision"]
        or trace.get("disabled_edge_ids") != expected_disabled
        or trace.get("visited_edge_ids") != used_edge_ids
        or trace.get("visited_edge_hashes") != [
            loaded.snapshot.edges[edge_id].record_sha256
            for edge_id in used_edge_ids
        ]
        or trace.get("used_semantic_edge_count") != len(used_edge_ids)
        or trace.get("used_edge_statuses") != expected_edge_statuses
        or trace.get("outbound_network_attempt_count") != 0
        or trace.get("database_opened") is not True
    ):
        _fail("semantic_runtime_answer_trace_invalid")
    used = set(used_edge_ids)
    expected_node_ids = sorted({
        node_id
        for edge_id in used_edge_ids
        for node_id in (
            loaded.snapshot.edges[edge_id].from_node_id,
            loaded.snapshot.edges[edge_id].to_node_id,
        )
    })
    if (
        trace.get("visited_node_ids") != expected_node_ids
        or trace.get("visited_node_hashes") != sorted(
            loaded.snapshot.nodes[node_id].record_sha256
            for node_id in expected_node_ids
        )
    ):
        _fail("semantic_runtime_answer_node_trace_invalid")
    if not _used_edges_are_connected(loaded.snapshot, used_edge_ids):
        _fail("semantic_runtime_answer_graph_disconnected")
    fact_fields: set[str] = set()
    for item in answer["asserted_facts"]:
        if not isinstance(item, dict) or set(item) != {
            "field", "value", "proof_edge_ids",
        }:
            _fail("semantic_runtime_answer_fact_invalid")
        field = item["field"]
        value = item["value"]
        proof = item["proof_edge_ids"]
        if (
            not isinstance(field, str)
            or not field.strip()
            or field in fact_fields
            or not isinstance(value, str)
            or not value.strip()
            or not isinstance(proof, list)
            or not proof
            or len(proof) != len(set(proof))
            or any(
                not isinstance(edge_id, str) or edge_id not in used
                for edge_id in proof
            )
            or not any(
                _edge_proves_fact(
                    query,
                    loaded.snapshot,
                    loaded.snapshot.edges[edge_id],
                    field,
                    value,
                )
                for edge_id in proof
            )
        ):
            _fail("semantic_runtime_answer_fact_invalid")
        fact_fields.add(field)

    relation_tuples: set[tuple[str, str, str]] = set()
    for item in answer["asserted_relations"]:
        if not isinstance(item, dict) or set(item) != {
            "from", "relation", "to", "proof_edge_ids",
        }:
            _fail("semantic_runtime_answer_relation_invalid")
        asserted_tuple = (item["from"], item["relation"], item["to"])
        proof = item["proof_edge_ids"]
        if (
            any(
                not isinstance(value, str) or not value.strip()
                for value in asserted_tuple
            )
            or asserted_tuple in relation_tuples
            or not isinstance(proof, list)
            or not proof
            or len(proof) != len(set(proof))
            or any(
                not isinstance(edge_id, str) or edge_id not in used
                for edge_id in proof
            )
            or not any(
                (
                    loaded.snapshot.nodes[
                        loaded.snapshot.edges[edge_id].from_node_id
                    ].canonical_key,
                    loaded.snapshot.edges[edge_id].relation_type,
                    loaded.snapshot.nodes[
                        loaded.snapshot.edges[edge_id].to_node_id
                    ].canonical_key,
                )
                == asserted_tuple
                for edge_id in proof
            )
        ):
            _fail("semantic_runtime_answer_relation_invalid")
        relation_tuples.add(asserted_tuple)
    if answer["decision"] == "ACCEPTED":
        if (
            not used_edge_ids
            or answer.get("reason_code") is not None
            or not answer["asserted_facts"]
            or fact_fields != OPERATION_FACT_FIELDS[expected_operation]
            or {item[1] for item in relation_tuples}
            != OPERATION_RELATION_TYPES[expected_operation]
        ):
            _fail("semantic_runtime_accepted_answer_invalid")
    elif answer["asserted_facts"] or answer["asserted_relations"]:
        _fail("semantic_runtime_hold_contains_assertions")

    expected_references = {
        (edge_id, evidence_id)
        for edge_id in used
        for evidence_id in loaded.snapshot.edges[
            edge_id
        ].supporting_evidence_ids
    }
    actual_references: set[tuple[str, str]] = set()
    references = trace.get("resolved_source_references")
    if not isinstance(references, list):
        _fail("semantic_runtime_source_trace_invalid")
    for reference in references:
        if not isinstance(reference, dict):
            _fail("semantic_runtime_source_trace_invalid")
        pair = (reference.get("edge_id"), reference.get("evidence_id"))
        evidence = loaded.snapshot.evidence.get(pair[1])
        if (
            pair in actual_references
            or pair not in expected_references
            or evidence is None
            or pair[1] not in loaded.eligible_evidence_ids
            or reference.get("document_id") != evidence.document_id
            or reference.get("path") != evidence.relative_path
            or reference.get("source_sha256") != evidence.source_sha256
            or reference.get("locator") != evidence.locator
            or reference.get("observed_text_sha256") != evidence.observed_sha256
            or reference.get("quote") != evidence.observed_text
        ):
            _fail("semantic_runtime_source_trace_invalid")
        actual_references.add(pair)
    if actual_references != expected_references:
        _fail("semantic_runtime_source_trace_incomplete")
    if trace.get("visited_document_paths") != sorted({
        loaded.snapshot.evidence[evidence_id].relative_path
        for _edge_id, evidence_id in expected_references
    }):
        _fail("semantic_runtime_document_trace_invalid")


def _empty_trace(
    decision: str,
    reference_date: str | None = None,
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


def _result(
    *,
    status: str,
    decision: str,
    operation: str | None,
    reason_code: str | None,
    diagnostic_code: str | None,
    answer_text: str,
    asserted_facts: list[dict[str, Any]],
    asserted_relations: list[dict[str, Any]],
    trace: dict[str, Any],
    runtime_attestation: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "cross_document_semantic_graph_query_candidate",
        "adapter": RUNTIME_ADAPTER,
        "adapter_version": RUNTIME_ADAPTER_VERSION,
        "status": status,
        "decision": decision,
        "reason_code": reason_code,
        "diagnostic_code": diagnostic_code,
        "operation": operation,
        "answer_text": answer_text,
        "asserted_facts": asserted_facts,
        "asserted_relations": asserted_relations,
        "trace": trace,
        "runtime_attestation": runtime_attestation,
        "used_for_answers": False,
        "independent_edge_audit_status": "not_implemented_step4",
    }


def evaluate_candidate(
    index_path: Path,
    question: str,
    expected_generation: str | None = None,
    *,
    expected_build_id: str | None = None,
    expected_registration: dict[str, Any] | None = None,
    disabled_edge_ids: Iterable[str] = (),
    reference_date: str | None = None,
) -> dict[str, Any]:
    """Evaluate the production graph only as a non-answering dual-run candidate."""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")
    if expected_generation is not None and (
        not isinstance(expected_generation, str)
        or GENERATION_PATTERN.fullmatch(expected_generation) is None
    ):
        raise ValueError("expected_generation must be generation plus 32 hex chars")
    if expected_build_id is not None and (
        not isinstance(expected_build_id, str) or not expected_build_id.strip()
    ):
        raise ValueError("expected_build_id must be a non-empty string")
    if expected_registration is not None and not isinstance(
        expected_registration, dict
    ):
        raise ValueError("expected_registration must be an object")
    reference_date = _strict_reference_date(reference_date)
    try:
        disabled = tuple(disabled_edge_ids)
    except TypeError as exc:
        raise ValueError("disabled_edge_ids must be an iterable of strings") from exc
    if any(not isinstance(value, str) or not value for value in disabled):
        raise ValueError("disabled_edge_ids must contain non-empty strings")

    operation: str | None = None
    try:
        classification = classify_question(question)
        if not classification["applicable"]:
            return _result(
                status="not_applicable",
                decision="NOT_APPLICABLE",
                operation=None,
                reason_code=classification["reason_code"],
                diagnostic_code=None,
                answer_text="",
                asserted_facts=[],
                asserted_relations=[],
                trace=_empty_trace("NOT_APPLICABLE", reference_date),
                runtime_attestation=None,
            )
        operation = classification["operation"]
        if expected_registration is None:
            _fail("semantic_runtime_registration_required")
        registration_generation = expected_registration.get("generation")
        if (
            not isinstance(registration_generation, str)
            or GENERATION_PATTERN.fullmatch(registration_generation) is None
        ):
            _fail("semantic_runtime_registration_generation_invalid")
        if (
            expected_generation is not None
            and expected_generation != registration_generation
        ):
            _fail("semantic_runtime_registration_generation_mismatch")
        loaded = load_runtime_graph(
            Path(index_path),
            expected_generation=registration_generation,
            expected_build_id=expected_build_id,
            expected_registration=expected_registration,
        )
        query = _query_contract()
        try:
            answer = query.answer_question(
                loaded.snapshot,
                question,
                disabled_edge_ids=disabled,
                reference_date=reference_date,
            )
        except query.GraphContractError as exc:
            raise RuntimeGraphContractError(
                "semantic_runtime_traversal_contract_invalid"
            ) from exc
        answer["trace"]["database_opened"] = True
        _validate_answer_result(
            answer,
            loaded,
            operation,
            question,
            disabled,
            reference_date,
        )
        return _result(
            status=(
                "accepted" if answer["decision"] == "ACCEPTED" else "held"
            ),
            decision=answer["decision"],
            operation=operation,
            reason_code=answer["reason_code"],
            diagnostic_code=None,
            answer_text=answer["answer_text"],
            asserted_facts=answer["asserted_facts"],
            asserted_relations=answer["asserted_relations"],
            trace=answer["trace"],
            runtime_attestation=loaded.attestation,
        )
    except RuntimeGraphContractError as exc:
        trace = _empty_trace("HOLD", reference_date)
        return _result(
            status="held",
            decision="HOLD",
            operation=operation,
            reason_code="semantic_graph_runtime_contract_invalid",
            diagnostic_code=exc.code,
            answer_text="必要な検証済みグラフ経路を確認できないため回答できません。",
            asserted_facts=[],
            asserted_relations=[],
            trace=trace,
            runtime_attestation=None,
        )
    except Exception:
        trace = _empty_trace("HOLD", reference_date)
        return _result(
            status="held",
            decision="HOLD",
            operation=operation,
            reason_code="semantic_graph_runtime_contract_invalid",
            diagnostic_code="semantic_runtime_unexpected_failure",
            answer_text="必要な検証済みグラフ経路を確認できないため回答できません。",
            asserted_facts=[],
            asserted_relations=[],
            trace=trace,
            runtime_attestation=None,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    parser.add_argument("--index", required=True)
    parser.add_argument("--registration-json", required=True)
    parser.add_argument("--reference-date")
    args = parser.parse_args(argv)
    registration = _strict_object(
        args.registration_json,
        "semantic_runtime_registration",
    )
    result = evaluate_candidate(
        Path(args.index),
        args.question,
        expected_registration=registration,
        reference_date=args.reference_date,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
