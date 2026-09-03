#!/usr/bin/env python3
"""Copy a validated semantic graph into an unpublished answer-index candidate.

The active safe-answer SQLite is opened read-only and copied with SQLite's
backup API.  Project/Work/Person/Claim nodes are written only to separately
named storage tables in the copied candidate.  This module never accepts a
question, evaluation gold, or a network endpoint, and it deliberately leaves
semantic-graph retrieval disabled.
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
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable
from urllib.parse import quote


SCHEMA_VERSION = "0.1"
PROJECTOR = "cross-document-semantic-graph-answer-index-projector"
PROJECTOR_VERSION = "0.1.0"
GENERATION_PATTERN = re.compile(r"generation-[0-9a-f]{32}")
SHADOW_DIRECTORY = "04-semantic-graph-shadow"
OUTPUT_DIRECTORY = "05-semantic-answer-index.building"
OUTPUT_DATABASE = "safe-answer-index.sqlite3"
OUTPUT_STATE = "semantic-answer-index-state.json"
SHADOW_FILES = {
    "database": "semantic-graph.sqlite3",
    "builder_state": "semantic-graph-state.json",
    "validation_state": "semantic-graph-validation.json",
    "run_state": "shadow-run-state.json",
}
SECURITY_OUTPUT_FILES = (
    "content-security-classifications.jsonl",
    "content-security-documents.jsonl",
    "safe-answer-evidence.jsonl",
    "prompt-library-evidence.jsonl",
    "quarantine-evidence.jsonl",
    "content-security-exclusions.jsonl",
)
SEMANTIC_TABLES = {
    "semantic_graph_nodes",
    "semantic_graph_edges",
    "semantic_graph_edge_evidence",
}
SEMANTIC_INDEXES = {
    "semantic_graph_nodes_type_key_idx",
    "semantic_graph_edges_from_type_idx",
    "semantic_graph_edges_to_type_idx",
    "semantic_graph_edge_evidence_evidence_idx",
}
SEMANTIC_OBJECTS = SEMANTIC_TABLES | SEMANTIC_INDEXES
METADATA_PREFIX = "cross_document_semantic_graph_"


class ProjectionError(ValueError):
    """Raised when a graph cannot be stored without changing answer behavior."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectionError(f"{label}_invalid") from exc
    if not isinstance(value, dict):
        raise ProjectionError(f"{label}_not_object")
    return value


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise ProjectionError(f"state_output_exists:{path.name}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(
            descriptor, "w", encoding="utf-8", newline="\n",
        ) as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise ProjectionError(f"state_output_exists:{path.name}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _load_module(name: str, path: Path) -> ModuleType:
    if not path.is_file() or path.is_symlink():
        raise ProjectionError(f"module_missing_or_symlink:{path.name}")
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise ProjectionError(f"module_unavailable:{path.name}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def _sibling_module(name: str, filename: str) -> ModuleType:
    return _load_module(name, Path(__file__).resolve().with_name(filename))


def _answer_validator() -> ModuleType:
    here = Path(__file__).resolve()
    candidates = (
        here.parents[1]
        / "distribution"
        / "macos-local-memory"
        / "engine"
        / "answer_local_memory.py",
        here.parents[2] / "answer_local_memory.py",
    )
    for path in candidates:
        if path.is_file() and not path.is_symlink():
            return _load_module("semantic_storage_answer_validator", path)
    raise ProjectionError("answer_validator_missing")


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _normalized_sql_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {
            "type": "blob",
            "size": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": int(value)}
    if isinstance(value, int):
        return {"type": "integer", "value": value}
    if isinstance(value, float):
        return {"type": "real", "value": repr(value)}
    if isinstance(value, str):
        return {"type": "text", "value": value}
    raise ProjectionError(f"unsupported_sql_value:{type(value).__name__}")


def _table_rows_sha256(
    connection: sqlite3.Connection,
    table: str,
    *,
    metadata_keys: frozenset[str] | None = None,
) -> str:
    table_info = list(
        connection.execute(f"PRAGMA table_info({_quote_identifier(table)})")
    )
    columns = [str(row[1]) for row in table_info]
    if not columns:
        raise ProjectionError(f"base_table_columns_missing:{table}")
    select = f"SELECT {', '.join(_quote_identifier(value) for value in columns)} "
    select += f"FROM {_quote_identifier(table)}"
    parameters: tuple[Any, ...] = ()
    if table == "metadata" and metadata_keys is not None:
        placeholders = ",".join("?" for _ in metadata_keys)
        select += f" WHERE key IN ({placeholders})"
        parameters = tuple(sorted(metadata_keys))
    primary_key_columns = [
        str(row[1])
        for row in sorted(table_info, key=lambda value: int(value[5]))
        if int(row[5]) > 0
    ]
    order_columns = primary_key_columns or columns
    select += " ORDER BY " + ", ".join(
        _quote_identifier(value) for value in order_columns
    )
    digest = hashlib.sha256()
    digest.update(canonical_json({"table": table, "columns": columns}).encode("utf-8"))
    for row in connection.execute(select, parameters):
        normalized = [_normalized_sql_value(value) for value in row]
        digest.update(b"\n")
        digest.update(canonical_json(normalized).encode("utf-8"))
    return digest.hexdigest()


def _base_logical_snapshot(
    connection: sqlite3.Connection,
    *,
    object_names: frozenset[str] | None = None,
    metadata_keys: frozenset[str] | None = None,
) -> dict[str, Any]:
    master_rows = [
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table": str(row[2]),
            "sql": row[3],
        }
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )
        if object_names is None or str(row[1]) in object_names
    ]
    names = frozenset(item["name"] for item in master_rows)
    table_names = sorted(
        item["name"] for item in master_rows if item["type"] == "table"
    )
    snapshot = {
        "objects": master_rows,
        "tables": {
            table: _table_rows_sha256(
                connection,
                table,
                metadata_keys=metadata_keys if table == "metadata" else None,
            )
            for table in table_names
        },
    }
    return {
        "object_names": names,
        "snapshot": snapshot,
        "sha256": sha256_json(snapshot),
    }


def _metadata(connection: sqlite3.Connection) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in connection.execute("SELECT key, value FROM metadata"):
        if key in result:
            raise ProjectionError(f"base_metadata_duplicate:{key}")
        try:
            result[str(key)] = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProjectionError(f"base_metadata_json_invalid:{key}") from exc
    return result


def _require_file(path: Path, generation: Path, expected_name: str) -> Path:
    if path.name != expected_name or path.is_symlink() or not path.is_file():
        raise ProjectionError(f"input_invalid:{expected_name}")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(generation)
    except (OSError, ValueError) as exc:
        raise ProjectionError(f"input_outside_generation:{expected_name}") from exc
    return resolved


def _create_output_guard(path: Path) -> int:
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ProjectionError("projection_output_create_failed") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise ProjectionError("projection_output_guard_invalid")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _assert_output_identity(path: Path, descriptor: int) -> None:
    try:
        opened = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ProjectionError("projection_output_identity_changed") from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or opened.st_dev != current.st_dev
        or opened.st_ino != current.st_ino
        or opened.st_nlink != 1
        or current.st_nlink != 1
    ):
        raise ProjectionError("projection_output_identity_changed")


def _validate_paths(
    *,
    base_index: Path,
    shadow_dir: Path,
    documents: Path,
    source_evidence: Path,
    evidence: Path,
    security_state: Path,
    security_gate_dir: Path,
    security_validator: Path,
    generation_dir: Path,
    output: Path,
    state: Path,
    existing_projection: bool = False,
) -> dict[str, Path]:
    if generation_dir.is_symlink():
        raise ProjectionError("generation_directory_is_symlink")
    try:
        generation = generation_dir.resolve(strict=True)
    except OSError as exc:
        raise ProjectionError("generation_directory_missing") from exc
    if not generation.is_dir() or GENERATION_PATTERN.fullmatch(generation.name) is None:
        raise ProjectionError("generation_directory_invalid")
    if shadow_dir.is_symlink():
        raise ProjectionError("shadow_directory_is_symlink")
    try:
        shadow = shadow_dir.resolve(strict=True)
    except OSError as exc:
        raise ProjectionError("shadow_directory_missing") from exc
    if shadow != generation / SHADOW_DIRECTORY or not shadow.is_dir():
        raise ProjectionError("shadow_directory_layout_invalid")

    base = _require_file(base_index, generation, OUTPUT_DATABASE)
    if base.parent != generation:
        raise ProjectionError("base_index_layout_invalid")
    docs = _require_file(documents, generation, "semantic-documents.jsonl")
    source = _require_file(source_evidence, generation, "semantic-evidence.jsonl")
    safe = _require_file(evidence, generation, "safe-answer-evidence.jsonl")
    security = _require_file(security_state, generation, "content-security-state.json")
    if docs.parent != source.parent:
        raise ProjectionError("semantic_input_directory_mismatch")
    if security_gate_dir.is_symlink():
        raise ProjectionError("security_gate_directory_is_symlink")
    try:
        gate = security_gate_dir.resolve(strict=True)
    except OSError as exc:
        raise ProjectionError("security_gate_directory_missing") from exc
    if gate != security.parent or safe.parent != gate or not gate.is_dir():
        raise ProjectionError("security_input_directory_mismatch")
    security_outputs = {
        name: _require_file(gate / name, generation, name)
        for name in SECURITY_OUTPUT_FILES
    }
    if security_outputs["safe-answer-evidence.jsonl"] != safe:
        raise ProjectionError("safe_evidence_security_output_mismatch")
    if any(path.parent != gate for path in security_outputs.values()):
        raise ProjectionError("security_output_directory_mismatch")

    try:
        validator = security_validator.resolve(strict=True)
    except OSError as exc:
        raise ProjectionError("security_validator_invalid") from exc
    if security_validator.is_symlink() or not validator.is_file():
        raise ProjectionError("security_validator_invalid")
    if validator.with_name("content_security_gate.py").is_symlink() or not validator.with_name(
        "content_security_gate.py"
    ).is_file():
        raise ProjectionError("security_builder_invalid")

    if output.parent.is_symlink() or state.parent.is_symlink():
        raise ProjectionError("projection_output_directory_is_symlink")
    permitted_output_directories = (
        {generation / OUTPUT_DIRECTORY, generation / "05-semantic-answer-index"}
        if existing_projection
        else {generation / OUTPUT_DIRECTORY}
    )
    output_directory = output.parent.resolve(strict=False)
    if (
        output_directory not in permitted_output_directories
        or state.parent.resolve(strict=False) != output_directory
    ):
        raise ProjectionError("projection_output_directory_invalid")
    if output.name != OUTPUT_DATABASE or state.name != OUTPUT_STATE:
        raise ProjectionError("projection_output_name_invalid")
    if output == state or output in {base, docs, source, safe, security}:
        raise ProjectionError("projection_output_aliases_input")
    if output_directory.is_symlink():
        raise ProjectionError("projection_output_directory_is_symlink")
    if existing_projection:
        if (
            not output_directory.is_dir()
            or output.is_symlink()
            or not output.is_file()
            or state.is_symlink()
            or not state.is_file()
        ):
            raise ProjectionError("existing_projection_artifact_invalid")
    elif output.exists() or output.is_symlink():
        raise ProjectionError("projection_output_exists")
    elif state.exists() or state.is_symlink():
        raise ProjectionError("projection_state_exists")
    elif output_directory.exists():
        if not output_directory.is_dir() or any(output_directory.iterdir()):
            raise ProjectionError("projection_output_directory_not_empty")
    else:
        output_directory.mkdir()

    shadow_paths = {
        key: _require_file(shadow / filename, generation, filename)
        for key, filename in SHADOW_FILES.items()
    }
    return {
        "generation": generation,
        "base": base,
        "shadow": shadow,
        "documents": docs,
        "source_evidence": source,
        "evidence": safe,
        "security_state": security,
        "security_gate": gate,
        "security_validator": validator,
        "output": output.resolve(strict=False),
        "state": state.resolve(strict=False),
        **{
            f"security_output:{name}": path
            for name, path in security_outputs.items()
        },
        **{f"shadow_{key}": value for key, value in shadow_paths.items()},
    }


def _attest_final_shadow(paths: dict[str, Path]) -> dict[str, Any]:
    builder = _sibling_module(
        "semantic_storage_graph_replay_builder",
        "build_cross_document_semantic_graph.py",
    )
    graph_contract = _sibling_module(
        "semantic_storage_graph_contract",
        "query_cross_document_semantic_graph.py",
    )
    validator_path = Path(__file__).resolve().with_name(
        "validate_cross_document_semantic_graph.py"
    )
    validator_tool = _load_module(
        "semantic_storage_shadow_validator_contract", validator_path,
    )
    security_tool = _load_module(
        "semantic_storage_security_validator", paths["security_validator"],
    )

    input_hashes = {
        "documents_input_sha256": sha256_file(paths["documents"]),
        "source_evidence_input_sha256": sha256_file(paths["source_evidence"]),
        "evidence_input_sha256": sha256_file(paths["evidence"]),
        "content_security_state_sha256": sha256_file(paths["security_state"]),
    }
    security_state_record = load_object(
        paths["security_state"], "content_security_state"
    )
    security_outputs = security_state_record.get("outputs")
    if (
        not isinstance(security_outputs, dict)
        or set(security_outputs) != set(SECURITY_OUTPUT_FILES)
    ):
        raise ProjectionError("content_security_output_manifest_invalid")
    security_report = security_tool.validate(
        paths["source_evidence"],
        paths["documents"],
        paths["security_gate"],
    )
    if not isinstance(security_report, dict) or security_report.get("status") != "PASS":
        raise ProjectionError("content_security_independent_validation_failed")
    attestation = security_report.get("attestation")
    security_output_hashes = {
        name: sha256_file(paths[f"security_output:{name}"])
        for name in SECURITY_OUTPUT_FILES
    }
    if (
        not isinstance(attestation, dict)
        or attestation.get("state_sha256")
        != input_hashes["content_security_state_sha256"]
        or attestation.get("source_evidence_sha256")
        != input_hashes["source_evidence_input_sha256"]
        or attestation.get("source_documents_sha256")
        != input_hashes["documents_input_sha256"]
        or attestation.get("output_sha256") != security_output_hashes
    ):
        raise ProjectionError("content_security_attestation_mismatch")

    graph_state = load_object(paths["shadow_builder_state"], "graph_builder_state")
    validation = load_object(paths["shadow_validation_state"], "graph_validation_state")
    run_state = load_object(paths["shadow_run_state"], "graph_shadow_run_state")
    graph_sha256 = sha256_file(paths["shadow_database"])
    builder_state_sha256 = sha256_file(paths["shadow_builder_state"])
    validation_state_sha256 = sha256_file(paths["shadow_validation_state"])
    run_state_sha256 = sha256_file(paths["shadow_run_state"])

    required_builder = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "cross_document_semantic_graph_state",
        "builder": "cross-document-semantic-graph-builder",
        "builder_version": builder.BUILDER_VERSION,
        "status": "complete",
        "question_independent": True,
        "external_network_used": False,
    }
    if any(graph_state.get(key) != value for key, value in required_builder.items()):
        raise ProjectionError("graph_builder_state_contract_invalid")
    if graph_state.get("output") != {
        "sqlite_file": paths["shadow_database"].name,
        "state_file": paths["shadow_builder_state"].name,
    }:
        raise ProjectionError("graph_builder_output_binding_invalid")
    required_validation = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "cross_document_semantic_graph_validation_state",
        "validator": "cross-document-semantic-graph-validator",
        "status": "complete",
        "question_independent": True,
        "external_network_used": False,
    }
    if any(validation.get(key) != value for key, value in required_validation.items()):
        raise ProjectionError("graph_validation_state_contract_invalid")
    required_run = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "cross_document_semantic_graph_shadow_run",
        "status": "complete",
        "shadow_only": True,
        "used_for_index": False,
        "used_for_answers": False,
        "external_network_used": False,
        "generation": paths["generation"].name,
        "output_directory": SHADOW_DIRECTORY,
    }
    if any(run_state.get(key) != value for key, value in required_run.items()):
        raise ProjectionError("graph_shadow_run_state_contract_invalid")
    build_id = run_state.get("build_id")
    if not isinstance(build_id, str) or not build_id.strip():
        raise ProjectionError("graph_shadow_build_id_invalid")

    for state, label in (
        (graph_state, "builder"),
        (validation, "validation"),
        (run_state, "run"),
    ):
        if state.get("graph_snapshot_id") != graph_state.get("graph_snapshot_id"):
            raise ProjectionError(f"graph_snapshot_id_mismatch:{label}")
        if state.get("logical_snapshot_sha256") != graph_state.get(
            "logical_snapshot_sha256"
        ):
            raise ProjectionError(f"graph_logical_snapshot_mismatch:{label}")
        if state.get("sqlite_sha256") != graph_sha256:
            raise ProjectionError(f"graph_sqlite_hash_mismatch:{label}")
        if state.get("relation_type_counts") != graph_state.get(
            "relation_type_counts"
        ):
            raise ProjectionError(f"graph_relation_counts_mismatch:{label}")
    validation_counts = validation.get("counts")
    builder_counts = graph_state.get("counts")
    if (
        not isinstance(validation_counts, dict)
        or not isinstance(builder_counts, dict)
        or run_state.get("counts") != validation_counts
        or any(
            builder_counts.get(key) != expected
            for key, expected in validation_counts.items()
        )
    ):
        raise ProjectionError("graph_counts_mismatch")
    if validation.get("builder_state_sha256") != builder_state_sha256:
        raise ProjectionError("graph_builder_state_hash_mismatch")
    for key, expected in input_hashes.items():
        if validation.get(key) != expected:
            raise ProjectionError(f"graph_validation_input_hash_mismatch:{key}")
    if graph_state.get("documents_input_sha256") != input_hashes[
        "documents_input_sha256"
    ] or graph_state.get("evidence_input_sha256") != input_hashes[
        "evidence_input_sha256"
    ]:
        raise ProjectionError("graph_builder_input_hash_mismatch")
    upstream = run_state.get("upstream")
    expected_upstream = {
        "semantic_documents_sha256": input_hashes["documents_input_sha256"],
        "semantic_evidence_sha256": input_hashes[
            "source_evidence_input_sha256"
        ],
        "safe_answer_evidence_sha256": input_hashes["evidence_input_sha256"],
        "content_security_state_sha256": input_hashes[
            "content_security_state_sha256"
        ],
    }
    if upstream != expected_upstream:
        raise ProjectionError("graph_shadow_upstream_hash_mismatch")
    artifacts = run_state.get("artifacts")
    if artifacts != SHADOW_FILES:
        raise ProjectionError("graph_shadow_artifact_manifest_invalid")
    expected_tool_hashes = {
        "build_cross_document_semantic_graph.py": sha256_file(
            Path(__file__).resolve().with_name(
                "build_cross_document_semantic_graph.py"
            )
        ),
        "query_cross_document_semantic_graph.py": sha256_file(
            Path(__file__).resolve().with_name(
                "query_cross_document_semantic_graph.py"
            )
        ),
        "validate_cross_document_semantic_graph.py": sha256_file(validator_path),
    }
    if run_state.get("tool_sha256") != expected_tool_hashes:
        raise ProjectionError("graph_shadow_tool_hash_mismatch")
    security_tool_hashes = validation.get("content_security_tool_sha256")
    expected_security_hashes = {
        paths["security_validator"].name: sha256_file(paths["security_validator"]),
        "content_security_gate.py": sha256_file(
            paths["security_validator"].with_name("content_security_gate.py")
        ),
    }
    if security_tool_hashes != dict(sorted(expected_security_hashes.items())):
        raise ProjectionError("content_security_tool_hash_mismatch")
    if validation.get("content_security_output_sha256") != attestation.get(
        "output_sha256"
    ):
        raise ProjectionError("content_security_output_hash_mismatch")

    try:
        snapshot = graph_contract.GraphSnapshot.load(paths["shadow_database"])
    except Exception as exc:
        raise ProjectionError("graph_snapshot_contract_invalid") from exc
    graph_connection = sqlite3.connect(
        f"file:{quote(str(paths['shadow_database']))}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        graph_metadata = _metadata(graph_connection)
    finally:
        graph_connection.close()
    for key in (
        "schema_version",
        "builder",
        "builder_version",
        "status",
        "question_independent",
        "external_network_used",
        "graph_snapshot_id",
        "logical_snapshot_sha256",
        "documents_input_sha256",
        "evidence_input_sha256",
        "document_count",
        "source_evidence_count",
        "node_count",
        "edge_count",
    ):
        if graph_metadata.get(key) != graph_state.get(key):
            raise ProjectionError(f"graph_builder_metadata_mismatch:{key}")

    with tempfile.TemporaryDirectory(
        prefix="semantic-graph-promotion-replay.", dir=paths["generation"],
    ) as replay_raw:
        replay_dir = Path(replay_raw)
        replay_database = replay_dir / "semantic-graph.sqlite3"
        replay_state_path = replay_dir / "semantic-graph-state.json"
        replay_state = builder.build(
            paths["documents"],
            paths["evidence"],
            replay_database,
            replay_state_path,
        )
        try:
            replay_snapshot = graph_contract.GraphSnapshot.load(replay_database)
        except Exception as exc:
            raise ProjectionError("replayed_graph_contract_invalid") from exc
        if (
            replay_snapshot.graph_snapshot_id != snapshot.graph_snapshot_id
            or replay_snapshot.nodes != snapshot.nodes
            or replay_snapshot.edges != snapshot.edges
            or replay_snapshot.evidence != snapshot.evidence
            or replay_state.get("logical_snapshot_sha256")
            != graph_state.get("logical_snapshot_sha256")
            or replay_state.get("counts") != graph_state.get("counts")
            or replay_state.get("relation_type_counts")
            != graph_state.get("relation_type_counts")
        ):
            raise ProjectionError("graph_independent_replay_mismatch")

    stable_artifacts = {
        "documents_input_sha256": paths["documents"],
        "source_evidence_input_sha256": paths["source_evidence"],
        "evidence_input_sha256": paths["evidence"],
        "content_security_state_sha256": paths["security_state"],
        "shadow_sqlite_sha256": paths["shadow_database"],
        "builder_state_sha256": paths["shadow_builder_state"],
        "validation_state_sha256": paths["shadow_validation_state"],
        "shadow_run_state_sha256": paths["shadow_run_state"],
        **{
            f"security_output:{name}": paths[f"security_output:{name}"]
            for name in SECURITY_OUTPUT_FILES
        },
    }
    expected_hashes = {
        **input_hashes,
        "shadow_sqlite_sha256": graph_sha256,
        "builder_state_sha256": builder_state_sha256,
        "validation_state_sha256": validation_state_sha256,
        "shadow_run_state_sha256": run_state_sha256,
        **{
            f"security_output:{name}": digest
            for name, digest in security_output_hashes.items()
        },
    }
    if any(sha256_file(path) != expected_hashes[key] for key, path in stable_artifacts.items()):
        raise ProjectionError("graph_inputs_changed_during_revalidation")
    if validator_tool.VALIDATOR_VERSION != validation.get("validator_version"):
        raise ProjectionError("graph_validator_version_mismatch")
    return {
        "snapshot": snapshot,
        "graph_state": graph_state,
        "validation": validation,
        "run_state": run_state,
        "input_hashes": input_hashes,
        "shadow_sqlite_sha256": graph_sha256,
        "builder_state_sha256": builder_state_sha256,
        "validation_state_sha256": validation_state_sha256,
        "shadow_run_state_sha256": run_state_sha256,
        "security_output_hashes": security_output_hashes,
    }


def _semantic_projection_content(snapshot: Any) -> dict[str, Any]:
    return {
        "graph_snapshot_id": snapshot.graph_snapshot_id,
        "node_record_sha256": sorted(
            item.record_sha256 for item in snapshot.nodes.values()
        ),
        "edge_record_sha256": sorted(
            item.record_sha256 for item in snapshot.edges.values()
        ),
        "edge_evidence": sorted(
            [edge.edge_id, evidence_id]
            for edge in snapshot.edges.values()
            for evidence_id in edge.supporting_evidence_ids
        ),
    }


def _create_semantic_tables(connection: sqlite3.Connection) -> None:
    statements = (
        """CREATE TABLE semantic_graph_nodes (
            node_id TEXT PRIMARY KEY,
            node_type TEXT NOT NULL,
            canonical_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status = 'verified'),
            properties_json TEXT NOT NULL CHECK (
                json_valid(properties_json) AND json_type(properties_json) = 'object'
            ),
            record_sha256 TEXT NOT NULL,
            UNIQUE(node_type, canonical_key)
        )""",
        """CREATE TABLE semantic_graph_edges (
            edge_id TEXT PRIMARY KEY,
            from_node_id TEXT NOT NULL REFERENCES semantic_graph_nodes(node_id),
            relation_type TEXT NOT NULL,
            to_node_id TEXT NOT NULL REFERENCES semantic_graph_nodes(node_id),
            relation_class TEXT NOT NULL CHECK (relation_class = 'semantic'),
            status TEXT NOT NULL CHECK (status = 'verified'),
            basis_kind TEXT NOT NULL,
            basis_rule TEXT NOT NULL,
            properties_json TEXT NOT NULL CHECK (
                json_valid(properties_json) AND json_type(properties_json) = 'object'
            ),
            record_sha256 TEXT NOT NULL
        )""",
        """CREATE TABLE semantic_graph_edge_evidence (
            edge_id TEXT NOT NULL REFERENCES semantic_graph_edges(edge_id),
            evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
            PRIMARY KEY(edge_id, evidence_id)
        ) WITHOUT ROWID""",
        """CREATE INDEX semantic_graph_nodes_type_key_idx
            ON semantic_graph_nodes(node_type, canonical_key)""",
        """CREATE INDEX semantic_graph_edges_from_type_idx
            ON semantic_graph_edges(from_node_id, relation_type)""",
        """CREATE INDEX semantic_graph_edges_to_type_idx
            ON semantic_graph_edges(to_node_id, relation_type)""",
        """CREATE INDEX semantic_graph_edge_evidence_evidence_idx
            ON semantic_graph_edge_evidence(evidence_id)""",
    )
    for statement in statements:
        connection.execute(statement)


def _validate_shadow_evidence_binding(
    connection: sqlite3.Connection,
    snapshot: Any,
) -> None:
    indexed = {
        str(row[0]): {
            "document_id": str(row[1]),
            "relative_path": str(row[2]),
            "locator_json": str(row[3]),
            "observed_text": str(row[4]),
            "observed_sha256": str(row[5]),
        }
        for row in connection.execute(
            "SELECT evidence_id, document_id, relative_path, locator_json, "
            "observed_text, observed_sha256 FROM evidence ORDER BY evidence_id"
        )
    }
    if set(indexed) != set(snapshot.evidence):
        raise ProjectionError("semantic_source_evidence_universe_mismatch")
    for evidence_id, source in snapshot.evidence.items():
        row = indexed[evidence_id]
        try:
            locator = json.loads(row["locator_json"])
        except json.JSONDecodeError as exc:
            raise ProjectionError(
                f"base_evidence_locator_invalid:{evidence_id}"
            ) from exc
        if (
            row["document_id"] != source.document_id
            or row["relative_path"] != source.relative_path
            or locator != source.locator
            or row["observed_text"] != source.observed_text
            or row["observed_sha256"] != source.observed_sha256
        ):
            raise ProjectionError(
                f"semantic_source_evidence_binding_mismatch:{evidence_id}"
            )


def _copy_semantic_graph(
    connection: sqlite3.Connection,
    snapshot: Any,
    eligible_evidence_ids: frozenset[str],
) -> dict[str, int]:
    _validate_semantic_support(snapshot, eligible_evidence_ids)
    connection.executemany(
        "INSERT INTO semantic_graph_nodes VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                item.node_id,
                item.node_type,
                item.canonical_key,
                item.status,
                canonical_json(item.properties),
                item.record_sha256,
            )
            for item in sorted(snapshot.nodes.values(), key=lambda value: value.node_id)
        ],
    )
    connection.executemany(
        "INSERT INTO semantic_graph_edges VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                item.edge_id,
                item.from_node_id,
                item.relation_type,
                item.to_node_id,
                item.relation_class,
                item.status,
                item.basis_kind,
                item.basis_rule,
                canonical_json(item.properties),
                item.record_sha256,
            )
            for item in sorted(snapshot.edges.values(), key=lambda value: value.edge_id)
        ],
    )
    support_rows = sorted(
        (edge.edge_id, evidence_id)
        for edge in snapshot.edges.values()
        for evidence_id in edge.supporting_evidence_ids
    )
    connection.executemany(
        "INSERT INTO semantic_graph_edge_evidence VALUES (?, ?)", support_rows,
    )
    return {
        "nodes": len(snapshot.nodes),
        "edges": len(snapshot.edges),
        "edge_evidence": len(support_rows),
    }


def _validate_semantic_support(
    snapshot: Any, eligible_evidence_ids: frozenset[str],
) -> None:
    unsupported = sorted(
        {
            evidence_id
            for edge in snapshot.edges.values()
            for evidence_id in edge.supporting_evidence_ids
        }
        - set(eligible_evidence_ids)
    )
    if unsupported:
        raise ProjectionError(
            f"semantic_edge_support_not_retrievable:{unsupported[:8]}"
        )


def _semantic_readback(connection: sqlite3.Connection) -> dict[str, Any]:
    return {
        "nodes": [
            tuple(row)
            for row in connection.execute(
                "SELECT node_id, node_type, canonical_key, status, properties_json, "
                "record_sha256 FROM semantic_graph_nodes ORDER BY node_id"
            )
        ],
        "edges": [
            tuple(row)
            for row in connection.execute(
                "SELECT edge_id, from_node_id, relation_type, to_node_id, "
                "relation_class, status, basis_kind, basis_rule, properties_json, "
                "record_sha256 FROM semantic_graph_edges ORDER BY edge_id"
            )
        ],
        "edge_evidence": [
            tuple(row)
            for row in connection.execute(
                "SELECT edge_id, evidence_id FROM semantic_graph_edge_evidence "
                "ORDER BY edge_id, evidence_id"
            )
        ],
    }


def _expected_semantic_readback(snapshot: Any) -> dict[str, Any]:
    return {
        "nodes": [
            (
                item.node_id,
                item.node_type,
                item.canonical_key,
                item.status,
                canonical_json(item.properties),
                item.record_sha256,
            )
            for item in sorted(snapshot.nodes.values(), key=lambda value: value.node_id)
        ],
        "edges": [
            (
                item.edge_id,
                item.from_node_id,
                item.relation_type,
                item.to_node_id,
                item.relation_class,
                item.status,
                item.basis_kind,
                item.basis_rule,
                canonical_json(item.properties),
                item.record_sha256,
            )
            for item in sorted(snapshot.edges.values(), key=lambda value: value.edge_id)
        ],
        "edge_evidence": sorted(
            (edge.edge_id, evidence_id)
            for edge in snapshot.edges.values()
            for evidence_id in edge.supporting_evidence_ids
        ),
    }


def _semantic_metadata_additions(
    shadow: dict[str, Any],
    counts: dict[str, int],
    projection_sha256: str,
    base_logical_snapshot_sha256: str,
) -> dict[str, Any]:
    return {
        METADATA_PREFIX + "storage_schema_version": SCHEMA_VERSION,
        METADATA_PREFIX + "storage_status": "validated_storage_only",
        METADATA_PREFIX + "retrieval_enabled": False,
        METADATA_PREFIX + "used_for_answers": False,
        METADATA_PREFIX + "question_independent": True,
        METADATA_PREFIX + "external_network_used": False,
        METADATA_PREFIX + "snapshot_id": shadow["snapshot"].graph_snapshot_id,
        METADATA_PREFIX + "logical_snapshot_sha256": shadow["graph_state"][
            "logical_snapshot_sha256"
        ],
        METADATA_PREFIX + "source_sqlite_sha256": shadow[
            "shadow_sqlite_sha256"
        ],
        METADATA_PREFIX + "builder_state_sha256": shadow[
            "builder_state_sha256"
        ],
        METADATA_PREFIX + "validation_state_sha256": shadow[
            "validation_state_sha256"
        ],
        METADATA_PREFIX + "shadow_run_state_sha256": shadow[
            "shadow_run_state_sha256"
        ],
        METADATA_PREFIX + "documents_input_sha256": shadow["input_hashes"][
            "documents_input_sha256"
        ],
        METADATA_PREFIX + "source_evidence_input_sha256": shadow[
            "input_hashes"
        ]["source_evidence_input_sha256"],
        METADATA_PREFIX + "evidence_input_sha256": shadow["input_hashes"][
            "evidence_input_sha256"
        ],
        METADATA_PREFIX + "content_security_state_sha256": shadow[
            "input_hashes"
        ]["content_security_state_sha256"],
        METADATA_PREFIX + "content_security_outputs_sha256": sha256_json(
            shadow["security_output_hashes"]
        ),
        METADATA_PREFIX + "node_count": counts["nodes"],
        METADATA_PREFIX + "edge_count": counts["edges"],
        METADATA_PREFIX + "edge_evidence_count": counts["edge_evidence"],
        METADATA_PREFIX + "projection_sha256": projection_sha256,
        METADATA_PREFIX + "base_logical_snapshot_sha256": (
            base_logical_snapshot_sha256
        ),
    }


def _assert_promotion_inputs_unchanged(
    paths: dict[str, Path], shadow: dict[str, Any],
) -> None:
    artifacts = {
        "documents_input_sha256": paths["documents"],
        "source_evidence_input_sha256": paths["source_evidence"],
        "evidence_input_sha256": paths["evidence"],
        "content_security_state_sha256": paths["security_state"],
        "shadow_sqlite_sha256": paths["shadow_database"],
        "builder_state_sha256": paths["shadow_builder_state"],
        "validation_state_sha256": paths["shadow_validation_state"],
        "shadow_run_state_sha256": paths["shadow_run_state"],
        **{
            f"security_output:{name}": paths[f"security_output:{name}"]
            for name in SECURITY_OUTPUT_FILES
        },
    }
    for key, path in artifacts.items():
        if key in shadow["input_hashes"]:
            expected = shadow["input_hashes"][key]
        elif key.startswith("security_output:"):
            expected = shadow["security_output_hashes"][
                key.removeprefix("security_output:")
            ]
        else:
            expected = shadow[key]
        if sha256_file(path) != expected:
            raise ProjectionError(f"promotion_input_changed:{key}")


def project(
    *,
    base_index: Path,
    shadow_dir: Path,
    documents: Path,
    source_evidence: Path,
    evidence: Path,
    security_state: Path,
    security_gate_dir: Path,
    security_validator: Path,
    generation_dir: Path,
    output: Path,
    state: Path,
) -> dict[str, Any]:
    paths = _validate_paths(
        base_index=Path(base_index),
        shadow_dir=Path(shadow_dir),
        documents=Path(documents),
        source_evidence=Path(source_evidence),
        evidence=Path(evidence),
        security_state=Path(security_state),
        security_gate_dir=Path(security_gate_dir),
        security_validator=Path(security_validator),
        generation_dir=Path(generation_dir),
        output=Path(output),
        state=Path(state),
    )
    output_created = False
    output_guard: int | None = None
    destination: sqlite3.Connection | None = None
    try:
        shadow = _attest_final_shadow(paths)
        answer_validator = _answer_validator()
        base_file_sha256 = sha256_file(paths["base"])
        base_connection = sqlite3.connect(
            f"file:{quote(str(paths['base']))}?mode=ro", uri=True,
        )
        try:
            base_connection.execute("PRAGMA query_only=ON")
            base_connection.execute("BEGIN")
            base_policy = answer_validator.validate_answer_graph_contract(
                base_connection
            )
            _validate_shadow_evidence_binding(
                base_connection, shadow["snapshot"]
            )
            base_metadata = _metadata(base_connection)
            if (
                base_metadata.get("evidence_sha256")
                != shadow["input_hashes"]["evidence_input_sha256"]
                or base_metadata.get("documents_sha256")
                != shadow["input_hashes"]["documents_input_sha256"]
                or base_metadata.get("content_security_state_sha256")
                != shadow["input_hashes"]["content_security_state_sha256"]
            ):
                raise ProjectionError("base_index_generation_binding_mismatch")
            if any(key.startswith(METADATA_PREFIX) for key in base_metadata):
                raise ProjectionError("base_index_already_contains_semantic_graph")
            base_snapshot = _base_logical_snapshot(base_connection)
            output_guard = _create_output_guard(paths["output"])
            output_created = True
            destination = sqlite3.connect(paths["output"])
            _assert_output_identity(paths["output"], output_guard)
            base_connection.backup(destination)
        finally:
            base_connection.close()

        if sha256_file(paths["base"]) != base_file_sha256:
            raise ProjectionError("base_index_changed_during_backup")

        if destination is None or output_guard is None:
            raise ProjectionError("projection_output_not_open")
        connection = destination
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise ProjectionError("projection_foreign_keys_disabled")
            connection.execute("BEGIN IMMEDIATE")
            _create_semantic_tables(connection)
            counts = _copy_semantic_graph(
                connection,
                shadow["snapshot"],
                frozenset(base_policy["eligible_evidence_ids"]),
            )
            projection_sha256 = sha256_json(
                _semantic_projection_content(shadow["snapshot"])
            )
            metadata_additions = _semantic_metadata_additions(
                shadow,
                counts,
                projection_sha256,
                base_snapshot["sha256"],
            )
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                [
                    (key, canonical_json(value))
                    for key, value in sorted(metadata_additions.items())
                ],
            )
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ProjectionError("projection_foreign_key_check_failed")
            if _semantic_readback(connection) != _expected_semantic_readback(
                shadow["snapshot"]
            ):
                raise ProjectionError("semantic_graph_readback_mismatch")
            candidate_snapshot = _base_logical_snapshot(
                connection,
                object_names=base_snapshot["object_names"],
                metadata_keys=frozenset(base_metadata),
            )
            if candidate_snapshot["sha256"] != base_snapshot["sha256"]:
                raise ProjectionError("base_logical_snapshot_changed")
            candidate_metadata = _metadata(connection)
            if (
                {key: candidate_metadata.get(key) for key in base_metadata}
                != base_metadata
                or set(candidate_metadata) != set(base_metadata) | set(metadata_additions)
            ):
                raise ProjectionError("base_metadata_changed")
            connection.commit()
            answer_policy_after = answer_validator.validate_answer_graph_contract(
                connection
            )
            if (
                frozenset(answer_policy_after["eligible_evidence_ids"])
                != frozenset(base_policy["eligible_evidence_ids"])
                or answer_policy_after["graph_sha256"] != base_policy["graph_sha256"]
                or answer_policy_after["partition_sha256"]
                != base_policy["partition_sha256"]
            ):
                raise ProjectionError("answer_graph_contract_changed")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ProjectionError("projection_foreign_key_check_failed")
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            if integrity != [("ok",)]:
                raise ProjectionError("projection_integrity_check_failed")
            final_candidate_snapshot = _base_logical_snapshot(
                connection,
                object_names=base_snapshot["object_names"],
                metadata_keys=frozenset(base_metadata),
            )
            if final_candidate_snapshot["sha256"] != base_snapshot["sha256"]:
                raise ProjectionError("base_logical_snapshot_changed_after_commit")
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
            destination = None

        if sha256_file(paths["base"]) != base_file_sha256:
            raise ProjectionError("base_index_changed_during_projection")
        _assert_promotion_inputs_unchanged(paths, shadow)

        _assert_output_identity(paths["output"], output_guard)
        os.fsync(output_guard)
        output_sha256 = sha256_file(paths["output"])
        result = {
            "schema_version": SCHEMA_VERSION,
            "record_type": (
                "cross_document_semantic_graph_answer_index_projection_state"
            ),
            "projector": PROJECTOR,
            "projector_version": PROJECTOR_VERSION,
            "status": "complete",
            "question_independent": True,
            "external_network_used": False,
            "storage_only": True,
            "retrieval_enabled": False,
            "used_for_answers": False,
            "answer_behavior_changed": False,
            "generation": paths["generation"].name,
            "base": {
                "sqlite_file": paths["base"].name,
                "sqlite_sha256": base_file_sha256,
                "logical_snapshot_sha256": base_snapshot["sha256"],
                "answer_graph_sha256": base_policy["graph_sha256"],
                "answer_partition_sha256": base_policy["partition_sha256"],
            },
            "shadow": {
                "directory": paths["shadow"].name,
                "build_id": shadow["run_state"]["build_id"],
                "graph_snapshot_id": shadow["snapshot"].graph_snapshot_id,
                "logical_snapshot_sha256": shadow["graph_state"][
                    "logical_snapshot_sha256"
                ],
                "sqlite_sha256": shadow["shadow_sqlite_sha256"],
                "builder_state_sha256": shadow["builder_state_sha256"],
                "validation_state_sha256": shadow["validation_state_sha256"],
                "run_state_sha256": shadow["shadow_run_state_sha256"],
            },
            "inputs": dict(sorted(shadow["input_hashes"].items())),
            "counts": counts,
            "projection_sha256": projection_sha256,
            "output": {
                "sqlite_file": paths["output"].name,
                "state_file": paths["state"].name,
                "sqlite_sha256": output_sha256,
            },
        }
        atomic_json(paths["state"], result)
        return result
    except BaseException:
        if destination is not None:
            destination.close()
            destination = None
        if output_created and output_guard is not None:
            try:
                _assert_output_identity(paths["output"], output_guard)
            except ProjectionError:
                pass
            else:
                paths["output"].unlink(missing_ok=True)
        raise
    finally:
        if output_guard is not None:
            os.close(output_guard)


def validate_existing_projection(
    *,
    base_index: Path,
    shadow_dir: Path,
    documents: Path,
    source_evidence: Path,
    evidence: Path,
    security_state: Path,
    security_gate_dir: Path,
    security_validator: Path,
    generation_dir: Path,
    output: Path,
    state: Path,
    expected_build_id: str | None = None,
) -> dict[str, Any]:
    """Revalidate an existing candidate or final projection without writes."""
    if expected_build_id is not None and (
        not isinstance(expected_build_id, str) or not expected_build_id.strip()
    ):
        raise ProjectionError("expected_build_id_invalid")
    paths = _validate_paths(
        base_index=Path(base_index),
        shadow_dir=Path(shadow_dir),
        documents=Path(documents),
        source_evidence=Path(source_evidence),
        evidence=Path(evidence),
        security_state=Path(security_state),
        security_gate_dir=Path(security_gate_dir),
        security_validator=Path(security_validator),
        generation_dir=Path(generation_dir),
        output=Path(output),
        state=Path(state),
        existing_projection=True,
    )
    if os.path.samefile(paths["base"], paths["output"]):
        raise ProjectionError("projection_output_aliases_base_inode")

    initial_hashes = {
        "base": sha256_file(paths["base"]),
        "output": sha256_file(paths["output"]),
        "state": sha256_file(paths["state"]),
    }
    projection_state = load_object(paths["state"], "projection_state")
    required_state = {
        "schema_version": SCHEMA_VERSION,
        "record_type": (
            "cross_document_semantic_graph_answer_index_projection_state"
        ),
        "projector": PROJECTOR,
        "projector_version": PROJECTOR_VERSION,
        "status": "complete",
        "question_independent": True,
        "external_network_used": False,
        "storage_only": True,
        "retrieval_enabled": False,
        "used_for_answers": False,
        "answer_behavior_changed": False,
        "generation": paths["generation"].name,
    }
    if any(
        projection_state.get(key) != expected
        for key, expected in required_state.items()
    ):
        raise ProjectionError("projection_state_contract_invalid")

    shadow = _attest_final_shadow(paths)
    build_id = shadow["run_state"]["build_id"]
    if expected_build_id is not None and build_id != expected_build_id:
        raise ProjectionError("projection_build_id_mismatch")
    expected_inputs = dict(sorted(shadow["input_hashes"].items()))
    if projection_state.get("inputs") != expected_inputs:
        raise ProjectionError("projection_state_input_binding_mismatch")
    expected_shadow_state = {
        "directory": paths["shadow"].name,
        "build_id": build_id,
        "graph_snapshot_id": shadow["snapshot"].graph_snapshot_id,
        "logical_snapshot_sha256": shadow["graph_state"][
            "logical_snapshot_sha256"
        ],
        "sqlite_sha256": shadow["shadow_sqlite_sha256"],
        "builder_state_sha256": shadow["builder_state_sha256"],
        "validation_state_sha256": shadow["validation_state_sha256"],
        "run_state_sha256": shadow["shadow_run_state_sha256"],
    }
    if projection_state.get("shadow") != expected_shadow_state:
        raise ProjectionError("projection_state_shadow_binding_mismatch")

    expected_counts = {
        "nodes": len(shadow["snapshot"].nodes),
        "edges": len(shadow["snapshot"].edges),
        "edge_evidence": sum(
            len(edge.supporting_evidence_ids)
            for edge in shadow["snapshot"].edges.values()
        ),
    }
    if projection_state.get("counts") != expected_counts:
        raise ProjectionError("projection_state_counts_mismatch")
    projection_sha256 = sha256_json(
        _semantic_projection_content(shadow["snapshot"])
    )
    if projection_state.get("projection_sha256") != projection_sha256:
        raise ProjectionError("projection_state_content_hash_mismatch")

    answer_validator = _answer_validator()
    base_connection = sqlite3.connect(
        f"file:{quote(str(paths['base']))}?mode=ro", uri=True,
    )
    projected_connection = sqlite3.connect(
        f"file:{quote(str(paths['output']))}?mode=ro", uri=True,
    )
    try:
        base_connection.execute("PRAGMA query_only=ON")
        projected_connection.execute("PRAGMA query_only=ON")
        base_connection.execute("BEGIN")
        projected_connection.execute("BEGIN")
        base_policy = answer_validator.validate_answer_graph_contract(
            base_connection
        )
        projected_policy = answer_validator.validate_answer_graph_contract(
            projected_connection
        )
        if (
            frozenset(projected_policy["eligible_evidence_ids"])
            != frozenset(base_policy["eligible_evidence_ids"])
            or projected_policy["graph_sha256"] != base_policy["graph_sha256"]
            or projected_policy["partition_sha256"]
            != base_policy["partition_sha256"]
        ):
            raise ProjectionError("existing_projection_answer_policy_changed")
        _validate_semantic_support(
            shadow["snapshot"],
            frozenset(base_policy["eligible_evidence_ids"]),
        )
        _validate_shadow_evidence_binding(
            base_connection, shadow["snapshot"]
        )
        _validate_shadow_evidence_binding(
            projected_connection, shadow["snapshot"]
        )

        base_metadata = _metadata(base_connection)
        if any(key.startswith(METADATA_PREFIX) for key in base_metadata):
            raise ProjectionError("base_index_already_contains_semantic_graph")
        base_snapshot = _base_logical_snapshot(base_connection)
        projected_original_snapshot = _base_logical_snapshot(
            projected_connection,
            object_names=base_snapshot["object_names"],
            metadata_keys=frozenset(base_metadata),
        )
        if projected_original_snapshot["sha256"] != base_snapshot["sha256"]:
            raise ProjectionError("existing_projection_base_snapshot_changed")
        projected_snapshot = _base_logical_snapshot(projected_connection)
        if projected_snapshot["object_names"] != (
            base_snapshot["object_names"] | SEMANTIC_OBJECTS
        ):
            raise ProjectionError("existing_projection_schema_objects_invalid")
        projected_metadata = _metadata(projected_connection)
        expected_metadata = _semantic_metadata_additions(
            shadow,
            expected_counts,
            projection_sha256,
            base_snapshot["sha256"],
        )
        if (
            {key: projected_metadata.get(key) for key in base_metadata}
            != base_metadata
            or {
                key: value
                for key, value in projected_metadata.items()
                if key.startswith(METADATA_PREFIX)
            }
            != expected_metadata
            or set(projected_metadata)
            != set(base_metadata) | set(expected_metadata)
        ):
            raise ProjectionError("existing_projection_metadata_invalid")
        if _semantic_readback(
            projected_connection
        ) != _expected_semantic_readback(shadow["snapshot"]):
            raise ProjectionError("existing_projection_semantic_readback_mismatch")
        if projected_connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ProjectionError("existing_projection_foreign_key_check_failed")
        if projected_connection.execute("PRAGMA integrity_check").fetchall() != [
            ("ok",)
        ]:
            raise ProjectionError("existing_projection_integrity_check_failed")
    finally:
        projected_connection.close()
        base_connection.close()

    expected_base_state = {
        "sqlite_file": paths["base"].name,
        "sqlite_sha256": initial_hashes["base"],
        "logical_snapshot_sha256": base_snapshot["sha256"],
        "answer_graph_sha256": base_policy["graph_sha256"],
        "answer_partition_sha256": base_policy["partition_sha256"],
    }
    if projection_state.get("base") != expected_base_state:
        raise ProjectionError("projection_state_base_binding_mismatch")
    if projection_state.get("output") != {
        "sqlite_file": paths["output"].name,
        "state_file": paths["state"].name,
        "sqlite_sha256": initial_hashes["output"],
    }:
        raise ProjectionError("projection_state_output_binding_mismatch")

    _assert_promotion_inputs_unchanged(paths, shadow)
    if (
        sha256_file(paths["base"]) != initial_hashes["base"]
        or sha256_file(paths["output"]) != initial_hashes["output"]
        or sha256_file(paths["state"]) != initial_hashes["state"]
    ):
        raise ProjectionError("existing_projection_changed_during_validation")
    return projection_state


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-index", required=True, type=Path)
    parser.add_argument("--shadow-dir", required=True, type=Path)
    parser.add_argument("--documents", required=True, type=Path)
    parser.add_argument("--source-evidence", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--security-state", required=True, type=Path)
    parser.add_argument("--security-gate-dir", required=True, type=Path)
    parser.add_argument("--security-validator", required=True, type=Path)
    parser.add_argument("--generation-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    result = project(
        base_index=args.base_index,
        shadow_dir=args.shadow_dir,
        documents=args.documents,
        source_evidence=args.source_evidence,
        evidence=args.evidence,
        security_state=args.security_state,
        security_gate_dir=args.security_gate_dir,
        security_validator=args.security_validator,
        generation_dir=args.generation_dir,
        output=args.output,
        state=args.state,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
