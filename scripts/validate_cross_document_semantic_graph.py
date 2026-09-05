#!/usr/bin/env python3
"""Independently validate a cross-document semantic graph snapshot.

This validator is intentionally question-independent.  It reopens the SQLite
snapshot read-only, verifies every Evidence/Node/Edge record hash through the
already evaluated graph reader, checks SQLite integrity and foreign keys, and
binds the snapshot back to the final Layer 1 and Content Security Gate inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import quote


SCHEMA_VERSION = "0.1"
VALIDATOR = "cross-document-semantic-graph-validator"
VALIDATOR_VERSION = "0.1.0"
EXPECTED_BUILDER = "cross-document-semantic-graph-builder"


class ValidationError(ValueError):
    """Raised when the candidate graph cannot be safely shadow-published."""


def _load_graph_contract() -> ModuleType:
    path = Path(__file__).resolve().with_name(
        "query_cross_document_semantic_graph.py"
    )
    if not path.is_file() or path.is_symlink():
        raise ValidationError("graph_contract_reader_missing")
    module_name = "cross_document_semantic_graph_contract_reader"
    specification = importlib.util.spec_from_file_location(module_name, path)
    if specification is None or specification.loader is None:
        raise ValidationError("graph_contract_reader_unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    specification.loader.exec_module(module)
    return module


def _load_graph_builder() -> ModuleType:
    path = Path(__file__).resolve().with_name(
        "build_cross_document_semantic_graph.py"
    )
    if not path.is_file() or path.is_symlink():
        raise ValidationError("graph_builder_missing")
    module_name = "cross_document_semantic_graph_replay_builder"
    specification = importlib.util.spec_from_file_location(module_name, path)
    if specification is None or specification.loader is None:
        raise ValidationError("graph_builder_unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    specification.loader.exec_module(module)
    return module


def _load_content_security_validator(path: Path) -> ModuleType:
    if not path.is_file() or path.is_symlink():
        raise ValidationError("content_security_validator_missing")
    builder_path = path.with_name("content_security_gate.py")
    if not builder_path.is_file() or builder_path.is_symlink():
        raise ValidationError("content_security_builder_missing")
    module_name = "cross_document_graph_content_security_validator"
    specification = importlib.util.spec_from_file_location(module_name, path)
    if specification is None or specification.loader is None:
        raise ValidationError("content_security_validator_unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    specification.loader.exec_module(module)
    return module


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
        raise ValidationError(f"{label}_invalid") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label}_not_object")
    return value


def count_jsonl_records(path: Path, label: str) -> int:
    count = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValidationError(
                        f"{label}_record_not_object:{line_number}"
                    )
                count += 1
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{label}_invalid") from exc
    return count


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(
            descriptor, "w", encoding="utf-8", newline="\n"
        ) as handle:
            handle.write(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _require_within_generation(
    generation_dir: Path,
    paths: tuple[Path, ...],
) -> Path:
    if generation_dir.is_symlink():
        raise ValidationError("generation_directory_is_symlink")
    try:
        generation = generation_dir.resolve(strict=True)
    except OSError as exc:
        raise ValidationError("generation_directory_missing") from exc
    if not generation.is_dir():
        raise ValidationError("generation_path_not_directory")
    for path in paths:
        if path.is_symlink():
            raise ValidationError(f"shadow_path_is_symlink:{path.name}")
        try:
            resolved = path.resolve(strict=path.exists())
            resolved.relative_to(generation)
        except (OSError, ValueError) as exc:
            raise ValidationError(
                f"shadow_path_outside_generation:{path.name}"
            ) from exc
    return generation


def _validate_security_binding(
    security_state_path: Path,
    documents_path: Path,
    source_evidence_path: Path,
    evidence_path: Path,
    security_gate_dir: Path,
    security_validator_path: Path,
) -> tuple[str, dict[str, str], dict[str, str]]:
    security = load_object(security_state_path, "content_security_state")
    required_security = {
        "schema_version": "0.1",
        "policy_version": "0.2.0",
        "classifier": "deterministic_content_security_gate",
        "question_independent": True,
        "llm_used_for_classification": False,
        "all_source_content_trust": "untrusted",
        "execution_policy": "never_execute",
        "safe_answer_index_allowed": True,
        "prompt_library_requires_explicit_mode": True,
        "quarantine_index_allowed": False,
    }
    for key, expected in required_security.items():
        if security.get(key) != expected:
            raise ValidationError(f"content_security_contract_mismatch:{key}")
    source_evidence = security.get("source_evidence")
    source_documents = security.get("source_documents")
    outputs = security.get("outputs")
    safe_output = (
        outputs.get("safe-answer-evidence.jsonl")
        if isinstance(outputs, dict)
        else None
    )
    if (
        not isinstance(source_evidence, dict)
        or source_evidence.get("sha256") != sha256_file(source_evidence_path)
    ):
        raise ValidationError("semantic_evidence_security_hash_mismatch")
    if (
        not isinstance(source_documents, dict)
        or source_documents.get("sha256") != sha256_file(documents_path)
    ):
        raise ValidationError("semantic_documents_security_hash_mismatch")
    if (
        not isinstance(safe_output, dict)
        or safe_output.get("sha256") != sha256_file(evidence_path)
        or safe_output.get("size_bytes") != evidence_path.stat().st_size
    ):
        raise ValidationError("safe_evidence_security_hash_mismatch")

    security_validator = _load_content_security_validator(
        security_validator_path
    )
    try:
        report = security_validator.validate(
            source_evidence_path,
            documents_path,
            security_gate_dir,
        )
    except Exception as exc:
        raise ValidationError("content_security_replay_invalid") from exc
    if not isinstance(report, dict) or report.get("status") != "PASS":
        raise ValidationError("content_security_replay_not_pass")
    attestation = report.get("attestation")
    output_hashes = (
        attestation.get("output_sha256")
        if isinstance(attestation, dict)
        else None
    )
    if (
        not isinstance(attestation, dict)
        or attestation.get("state_sha256") != sha256_file(security_state_path)
        or attestation.get("source_evidence_sha256")
        != sha256_file(source_evidence_path)
        or attestation.get("source_documents_sha256")
        != sha256_file(documents_path)
        or not isinstance(output_hashes, dict)
        or output_hashes.get("safe-answer-evidence.jsonl")
        != sha256_file(evidence_path)
    ):
        raise ValidationError("content_security_replay_attestation_invalid")
    security_builder_path = security_validator_path.with_name(
        "content_security_gate.py"
    )
    tool_hashes = {
        security_validator_path.name: sha256_file(security_validator_path),
        security_builder_path.name: sha256_file(security_builder_path),
    }
    return sha256_file(security_state_path), dict(output_hashes), tool_hashes


def _sqlite_checks(database_path: Path) -> tuple[dict[str, Any], int]:
    uri = f"file:{quote(str(database_path.resolve()))}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise ValidationError("sqlite_integrity_check_failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValidationError("sqlite_foreign_key_check_failed")
        metadata: dict[str, Any] = {}
        for key, value in connection.execute(
            "SELECT key, value FROM metadata ORDER BY key"
        ):
            try:
                metadata[key] = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValidationError(f"metadata_json_invalid:{key}") from exc
        support_count = int(
            connection.execute("SELECT COUNT(*) FROM edge_evidence").fetchone()[0]
        )
        return metadata, support_count
    finally:
        connection.close()


def validate(
    database_path: Path,
    state_path: Path,
    documents_path: Path,
    source_evidence_path: Path,
    evidence_path: Path,
    security_state_path: Path,
    security_gate_dir: Path,
    security_validator_path: Path,
    generation_dir: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Validate one candidate and atomically write its validation state."""
    database_path = Path(database_path)
    state_path = Path(state_path)
    documents_path = Path(documents_path)
    source_evidence_path = Path(source_evidence_path)
    evidence_path = Path(evidence_path)
    security_state_path = Path(security_state_path)
    security_gate_dir = Path(security_gate_dir)
    security_validator_path = Path(security_validator_path)
    generation_dir = Path(generation_dir)
    output_path = Path(output_path)
    if security_validator_path.is_symlink():
        raise ValidationError("content_security_validator_is_symlink")
    _require_within_generation(
        generation_dir,
        (
            database_path,
            state_path,
            documents_path,
            source_evidence_path,
            evidence_path,
            security_state_path,
            security_gate_dir,
            output_path,
        ),
    )
    generation_dir = generation_dir.resolve(strict=True)
    database_path = database_path.resolve(strict=True)
    state_path = state_path.resolve(strict=True)
    documents_path = documents_path.resolve(strict=True)
    source_evidence_path = source_evidence_path.resolve(strict=True)
    evidence_path = evidence_path.resolve(strict=True)
    security_state_path = security_state_path.resolve(strict=True)
    security_gate_dir = security_gate_dir.resolve(strict=True)
    security_validator_path = security_validator_path.resolve(strict=True)
    output_path = output_path.resolve(strict=False)
    if output_path in {
        database_path,
        state_path,
        documents_path,
        source_evidence_path,
        evidence_path,
        security_state_path,
    }:
        raise ValidationError("validation_output_overwrites_input")
    candidate_dir = database_path.parent
    if (
        candidate_dir.parent != generation_dir
        or candidate_dir.name != "04-semantic-graph-shadow.building"
        or state_path.parent != candidate_dir
        or output_path.parent != candidate_dir
        or database_path.name != "semantic-graph.sqlite3"
        or state_path.name != "semantic-graph-state.json"
        or output_path.name != "semantic-graph-validation.json"
    ):
        raise ValidationError("shadow_candidate_layout_invalid")
    if documents_path.name != "semantic-documents.jsonl":
        raise ValidationError("documents_input_name_invalid")
    if evidence_path.name != "safe-answer-evidence.jsonl":
        raise ValidationError("evidence_input_name_invalid")
    if source_evidence_path.name != "semantic-evidence.jsonl":
        raise ValidationError("source_evidence_input_name_invalid")
    if security_state_path.name != "content-security-state.json":
        raise ValidationError("security_state_input_name_invalid")
    if security_gate_dir != security_state_path.parent:
        raise ValidationError("security_gate_directory_mismatch")
    for path, label in (
        (database_path, "graph_database"),
        (state_path, "graph_state"),
        (documents_path, "semantic_documents"),
        (source_evidence_path, "semantic_evidence"),
        (evidence_path, "safe_evidence"),
        (security_state_path, "security_state"),
    ):
        if not path.is_file():
            raise ValidationError(f"{label}_missing")
    (
        security_state_sha256,
        security_output_hashes,
        security_tool_hashes,
    ) = _validate_security_binding(
        security_state_path,
        documents_path,
        source_evidence_path,
        evidence_path,
        security_gate_dir,
        security_validator_path,
    )
    source_evidence_sha256 = sha256_file(source_evidence_path)
    builder_state_sha256 = sha256_file(state_path)
    builder_state = load_object(state_path, "semantic_graph_state")
    required_state = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "cross_document_semantic_graph_state",
        "builder": EXPECTED_BUILDER,
        "status": "complete",
        "question_independent": True,
        "external_network_used": False,
    }
    for key, expected in required_state.items():
        if builder_state.get(key) != expected:
            raise ValidationError(f"graph_state_contract_mismatch:{key}")
    if builder_state.get("documents_input_sha256") != sha256_file(documents_path):
        raise ValidationError("graph_documents_input_hash_mismatch")
    if builder_state.get("evidence_input_sha256") != sha256_file(evidence_path):
        raise ValidationError("graph_evidence_input_hash_mismatch")
    database_sha256 = sha256_file(database_path)
    if builder_state.get("sqlite_sha256") != database_sha256:
        raise ValidationError("graph_sqlite_hash_mismatch")
    declared_output = builder_state.get("output")
    if not isinstance(declared_output, dict) or declared_output != {
        "sqlite_file": database_path.name,
        "state_file": state_path.name,
    }:
        raise ValidationError("graph_output_binding_invalid")

    contract = _load_graph_contract()
    try:
        snapshot = contract.GraphSnapshot.load(database_path)
    except Exception as exc:
        raise ValidationError("graph_snapshot_contract_invalid") from exc
    metadata, support_count = _sqlite_checks(database_path)

    replay_builder = _load_graph_builder()
    with tempfile.TemporaryDirectory(
        prefix="semantic-graph-replay.", dir=output_path.parent
    ) as replay_raw:
        replay_dir = Path(replay_raw)
        replay_database = replay_dir / "semantic-graph.sqlite3"
        replay_state_path = replay_dir / "semantic-graph-state.json"
        replay_state = replay_builder.build(
            documents_path,
            evidence_path,
            replay_database,
            replay_state_path,
        )
        try:
            replay_snapshot = contract.GraphSnapshot.load(replay_database)
        except Exception as exc:
            raise ValidationError("replayed_graph_contract_invalid") from exc
        if (
            replay_snapshot.graph_snapshot_id != snapshot.graph_snapshot_id
            or replay_state.get("logical_snapshot_sha256")
            != builder_state.get("logical_snapshot_sha256")
            or replay_state.get("counts") != builder_state.get("counts")
            or replay_state.get("relation_type_counts")
            != builder_state.get("relation_type_counts")
        ):
            raise ValidationError("graph_input_replay_mismatch")
    for key in (
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
        if metadata.get(key) != builder_state.get(key):
            raise ValidationError(f"graph_state_metadata_mismatch:{key}")
    counts = builder_state.get("counts")
    expected_counts = {
        "input_documents": count_jsonl_records(
            documents_path, "semantic_documents"
        ),
        "documents": len({
            snapshot.evidence[evidence_id].document_id
            for edge in snapshot.edges.values()
            for evidence_id in edge.supporting_evidence_ids
        }),
        "source_evidence": len(snapshot.evidence),
        "nodes": len(snapshot.nodes),
        "edges": len(snapshot.edges),
        "edge_evidence": support_count,
    }
    if not isinstance(counts, dict):
        raise ValidationError("graph_counts_missing")
    for key, expected in expected_counts.items():
        if counts.get(key) != expected:
            raise ValidationError(f"graph_count_mismatch:{key}")
    relation_type_counts: dict[str, int] = {}
    for edge in snapshot.edges.values():
        relation_type_counts[edge.relation_type] = (
            relation_type_counts.get(edge.relation_type, 0) + 1
        )
    if builder_state.get("relation_type_counts") != dict(
        sorted(relation_type_counts.items())
    ):
        raise ValidationError("graph_relation_type_counts_mismatch")
    if (
        sha256_file(documents_path) != builder_state["documents_input_sha256"]
        or sha256_file(source_evidence_path) != source_evidence_sha256
        or sha256_file(evidence_path) != builder_state["evidence_input_sha256"]
        or sha256_file(security_state_path) != security_state_sha256
        or sha256_file(database_path) != database_sha256
        or sha256_file(state_path) != builder_state_sha256
        or any(
            not (security_gate_dir / name).is_file()
            or (security_gate_dir / name).is_symlink()
            or sha256_file(security_gate_dir / name) != expected
            for name, expected in security_output_hashes.items()
        )
    ):
        raise ValidationError("shadow_input_changed_during_validation")

    validation_state = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "cross_document_semantic_graph_validation_state",
        "validator": VALIDATOR,
        "validator_version": VALIDATOR_VERSION,
        "status": "complete",
        "question_independent": True,
        "external_network_used": False,
        "graph_snapshot_id": snapshot.graph_snapshot_id,
        "logical_snapshot_sha256": builder_state["logical_snapshot_sha256"],
        "sqlite_sha256": database_sha256,
        "documents_input_sha256": builder_state["documents_input_sha256"],
        "source_evidence_input_sha256": source_evidence_sha256,
        "evidence_input_sha256": builder_state["evidence_input_sha256"],
        "content_security_state_sha256": security_state_sha256,
        "content_security_output_sha256": dict(
            sorted(security_output_hashes.items())
        ),
        "content_security_tool_sha256": dict(
            sorted(security_tool_hashes.items())
        ),
        "builder_state_sha256": builder_state_sha256,
        "counts": {
            **expected_counts,
        },
        "relation_type_counts": dict(sorted(relation_type_counts.items())),
    }
    atomic_json(output_path, validation_state)
    return validation_state


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a question-independent cross-document semantic graph "
            "against its safe production inputs."
        )
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--documents", type=Path, required=True)
    parser.add_argument("--source-evidence", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--security-state", type=Path, required=True)
    parser.add_argument("--security-gate-dir", type=Path, required=True)
    parser.add_argument("--security-validator", type=Path, required=True)
    parser.add_argument("--generation-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    state = validate(
        args.database,
        args.state,
        args.documents,
        args.source_evidence,
        args.evidence,
        args.security_state,
        args.security_gate_dir,
        args.security_validator,
        args.generation_dir,
        args.output,
    )
    print(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
