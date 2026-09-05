#!/usr/bin/env python3
from __future__ import annotations

import array
import hashlib
import importlib.util
import io
import json
import sqlite3
import subprocess
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare_reader_contract_semantic_fixture(
    bootstrap,
    semantic: Path,
    security: Path,
    *,
    manifest_paths: list[str] | None = None,
    status: str = "complete",
    limitations: dict | None = None,
    **reader_state_fields,
) -> dict:
    """Write the minimum real, hash-bound Reader generation artifacts."""
    # The checked-in bootstrap is copied beside ``engine`` in the packaged app;
    # tests execute it from ``app/`` and therefore provide the source-layout
    # engine explicitly.  Hash the real shipped inputs instead of weakening or
    # mocking the production contract.
    bootstrap.ENGINE = ENGINE
    semantic.mkdir(parents=True)
    security.mkdir(parents=True)
    bootstrap.atomic_json(
        semantic / "layer1-input-manifest.json",
        {"paths": list(manifest_paths or [])},
    )

    resources = bootstrap._current_reader_resource_contract()
    extractor = "test_intermediate_extractor"
    extractor_version = "0.1-test"
    fingerprint_payload = {
        "code": resources["processing_code"],
        "extractor": extractor,
        "extractor_version": extractor_version,
    }
    intermediate_dir = semantic / "layer1-intermediate"
    intermediate_dir.mkdir()
    intermediate_state_path = intermediate_dir / "build-state.json"
    bootstrap.atomic_json(intermediate_state_path, {
        "status": "complete",
        "extractor": extractor,
        "extractor_version": extractor_version,
        "processing_fingerprint": {
            "payload": fingerprint_payload,
            "sha256": bootstrap._canonical_json_sha256(
                fingerprint_payload
            ),
        },
    })
    intermediate_sha256 = bootstrap.sha256_file(intermediate_state_path)

    validation_path = semantic / "layer1-validation-state.json"
    bootstrap.atomic_json(validation_path, {
        "status": "pass",
        "schema_validation": "structural_contract_only",
        "intermediate_state_sha256": intermediate_sha256,
    })
    search_dir = semantic / "layer1-search"
    search_dir.mkdir()
    search_state_path = search_dir / "search-build-state.json"
    bootstrap.atomic_json(search_state_path, {"status": "complete"})

    documents_path = semantic / "semantic-documents.jsonl"
    evidence_path = semantic / "semantic-evidence.jsonl"
    documents_path.write_text("", encoding="utf-8")
    evidence_path.write_text("", encoding="utf-8")
    documents_sha256 = bootstrap.sha256_file(documents_path)
    evidence_sha256 = bootstrap.sha256_file(evidence_path)

    adapter_dir = semantic / "layer1-adapter"
    adapter_dir.mkdir()
    adapter_state_path = adapter_dir / "layer1-adapter-state.json"
    bootstrap.atomic_json(adapter_state_path, {
        "status": "complete",
        "adapter": "test_layer1_adapter",
        "adapter_version": "0.1-test",
        "source_state": {"sha256": intermediate_sha256},
        "outputs": {
            "documents": {"sha256": documents_sha256},
            "evidence": {"sha256": evidence_sha256},
        },
    })

    stages = {
        "intermediate": {
            "path": "layer1-intermediate/build-state.json",
            "sha256": intermediate_sha256,
        },
        "intermediate_validation": {
            "path": "layer1-validation-state.json",
            "sha256": bootstrap.sha256_file(validation_path),
        },
        "search": {
            "path": "layer1-search/search-build-state.json",
            "sha256": bootstrap.sha256_file(search_state_path),
        },
        "adapter": {
            "path": "layer1-adapter/layer1-adapter-state.json",
            "sha256": bootstrap.sha256_file(adapter_state_path),
        },
    }
    reader_state = {
        "status": status,
        "builder": "test_adaptive_reader",
        "builder_version": "0.1-test",
        "limitations": dict(limitations or {}),
        "stages": stages,
        "outputs": {
            "documents": {
                "path": documents_path.name,
                "sha256": documents_sha256,
                "count": 0,
            },
            "evidence": {
                "path": evidence_path.name,
                "sha256": evidence_sha256,
                "count": 0,
            },
        },
        **reader_state_fields,
    }
    bootstrap.atomic_json(
        semantic / "adaptive-reader-state.json", reader_state
    )
    return reader_state


def prepare_shadow_inputs(bootstrap, generation: Path) -> tuple[Path, Path]:
    semantic = generation / "02-semantic-model-ready"
    security = generation / "03-security-model-ready"
    semantic.mkdir(parents=True)
    security.mkdir(parents=True)
    documents = semantic / "semantic-documents.jsonl"
    source_evidence = semantic / "semantic-evidence.jsonl"
    evidence = security / "safe-answer-evidence.jsonl"
    documents.write_text('{"document_id":"doc"}\n', encoding="utf-8")
    source_evidence.write_text('{"evidence_id":"ev"}\n', encoding="utf-8")
    evidence.write_text('{"evidence_id":"ev"}\n', encoding="utf-8")
    bootstrap.atomic_json(security / "content-security-state.json", {
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
        "source_evidence": {
            "sha256": bootstrap.sha256_file(source_evidence),
        },
        "source_documents": {"sha256": bootstrap.sha256_file(documents)},
        "outputs": {
            "safe-answer-evidence.jsonl": {
                "sha256": bootstrap.sha256_file(evidence),
                "size_bytes": evidence.stat().st_size,
            }
        },
    })
    return semantic, security


def prepare_real_shadow_inputs(
    generation: Path,
    *,
    semantic_name: str = "02-semantic-model-ready",
    security_name: str = "03-security-model-ready",
) -> tuple[Path, Path]:
    semantic = generation / semantic_name
    security = generation / security_name
    semantic.mkdir(parents=True, exist_ok=True)
    security.mkdir(parents=True, exist_ok=True)
    documents = semantic / "semantic-documents.jsonl"
    source_evidence = semantic / "semantic-evidence.jsonl"
    source = {
        "relative_path": "assignments.xlsx",
        "sha256": "1" * 64,
        "extension": "xlsx",
    }
    headers = (
        "Project ID",
        "Work ID",
        "Role",
        "Assignee ID",
        "Valid From",
        "Status",
    )
    values = (
        "PRJ-1",
        "WORK-1",
        "主担当",
        "EMP-1",
        "2022-01-01",
        "final",
    )
    records = []
    for row_number, row in enumerate((headers, values), 1):
        for column_number, value in enumerate(row, 1):
            records.append({
                "evidence_id": f"ev_{row_number}_{column_number}",
                "document_id": "doc_assignments",
                "source": source,
                "locator": {
                    "sheet_name": "Assignments",
                    "cell": f"{chr(64 + column_number)}{row_number}",
                },
                "observed_text": value,
                "ordinal": len(records) + 1,
                "adapter": {
                    "execution_policy": "never_execute",
                    "source_record_type": "table_cell",
                },
                "status": "observed",
            })
    documents.write_text(
        json.dumps({
            "document_id": "doc_assignments",
            "source": source,
            "evidence_ids": [item["evidence_id"] for item in records],
            "status": "extracted",
        }, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    source_evidence.write_text(
        "".join(
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )
    security_builder = load_module(
        "runtime_shadow_real_security_builder",
        ENGINE / "content_security_gate.py",
    )
    security_builder.build(
        source_evidence,
        documents,
        security,
        created_at="2026-09-03T00:00:00+09:00",
    )
    return semantic, security


def prepare_published_generation(bootstrap, base: Path) -> tuple[Path, dict]:
    support = base / "support"
    workspace = support / "data"
    generation = (
        workspace / "generations" / ("generation-" + "9" * 32)
    )
    paths = generation / "01-path"
    semantic = generation / "02-semantic"
    security = generation / "03-security"
    for directory in (paths, semantic, security):
        directory.mkdir(parents=True)
    index = generation / "safe-answer-index.sqlite3"
    index.write_bytes(b"published-production-index")
    bootstrap.atomic_json(
        semantic / "adaptive-reader-state.json",
        {"status": "complete", "limitations": {}},
    )
    bootstrap.SUPPORT = support
    bootstrap.CONFIG = support / "config.json"
    bootstrap.STATE = support / "state.json"
    pending = bootstrap._shadow_run_base(
        generation,
        "build-published-shadow",
        status="pending",
        reason_code="scheduled_after_production_publish",
        elapsed_ms=0,
    )
    source = base / "source"
    source.mkdir()
    bootstrap.atomic_json(bootstrap.CONFIG, {
        "source_root": str(source),
        "workspace": str(workspace),
        "active_generation": generation.name,
        "path_graph_path": str(paths),
        "semantic_path": str(semantic),
        "security_path": str(security),
        "index_path": str(index),
        bootstrap.CROSS_DOCUMENT_SHADOW_FLAG: True,
    })
    bootstrap.atomic_json(
        generation / bootstrap.GENERATION_MARKER,
        {
            "schema_version": "0.1",
            "status": "published",
            "build_id": "build-published-shadow",
            "generation": generation.name,
            "owner_pid": 99_999_999,
            "cross_document_semantic_graph_shadow": pending,
        },
    )
    bootstrap.atomic_json(bootstrap.STATE, {
        "phase": "ready",
        "message": "索引の作成が完了しました。",
        "error": "",
        "cross_document_semantic_graph_shadow": pending,
    })
    return generation, pending


def replace_with_ready_answer_index(path: Path) -> None:
    """Replace the byte sentinel with the repository's valid schema 0.3 fixture."""
    from tests.test_answer_graph_retrieval_policy import create_ready_index

    path.unlink(missing_ok=True)
    create_ready_index(path)


def read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def packed(values: list[float]) -> bytes:
    return array.array("f", values).tobytes()


def prepare_ready_answer_index_for_records(
    base_index: Path,
    records: list[dict],
    *,
    documents: Path,
    evidence: Path,
    security_state: Path,
) -> dict:
    """Build a real schema-0.3 answer index over the semantic input Evidence."""
    index_builder = load_module(
        "runtime_storage_answer_index_builder",
        ENGINE / "build_local_semantic_index.py",
    )
    answer_reader = load_module(
        "runtime_storage_answer_index_reader",
        ENGINE / "answer_local_memory.py",
    )
    base_index.unlink(missing_ok=True)
    connection = sqlite3.connect(base_index)
    try:
        index_builder.initialize(connection)
        document_ids: set[str] = set()
        vectors: dict[str, list[float]] = {}
        for offset, record in enumerate(records, 1):
            evidence_id = record["evidence_id"]
            document_id = record["document_id"]
            document_ids.add(document_id)
            locator = record["locator"]
            text = record["observed_text"]
            relative_path = record["source"]["relative_path"]
            embedding_text = (
                f"ファイル: {relative_path}\n"
                f"場所: {json.dumps(locator, ensure_ascii=False, sort_keys=True)}\n"
                f"内容:\n{text}"
            )
            vector = [float(offset), float(offset + 1)]
            vectors[evidence_id] = vector
            connection.execute(
                "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence_id,
                    document_id,
                    relative_path,
                    json.dumps(locator, ensure_ascii=False, sort_keys=True),
                    text,
                    embedding_text,
                    0,
                    hashlib.sha256(text.encode("utf-8")).hexdigest(),
                ),
            )
            connection.execute(
                "INSERT INTO embeddings VALUES (?, ?, ?)",
                (evidence_id, 2, packed(vector)),
            )

        partition = {
            "schema_version": "0.1",
            "partitioner": "content-security-graph-partitioner",
            "partitioner_version": "0.1.0",
            "status": "pass",
            "question_independent": True,
            "security_policy_version": "0.2.0",
            "security_state_sha256": hashlib.sha256(
                security_state.read_bytes()
            ).hexdigest(),
            "safe_answer_evidence_sha256": hashlib.sha256(
                evidence.read_bytes()
            ).hexdigest(),
            "document_source_set_sha256": "c" * 64,
            "evidence_source_set_sha256": "d" * 64,
            "source_relation_set_sha256": "e" * 64,
            "projected_relation_set_sha256": "f" * 64,
            "promoted_relation_ids": [],
            "held_relations": [],
            "held_derived_evidence": [],
            "counts": {
                "source_relations": 0,
                "promoted_relations": 0,
                "held_relations": 0,
                "safe_evidence": len(records),
                "held_derived_evidence": 0,
            },
        }
        partition["partition_sha256"] = answer_reader.record_sha256(partition)

        nodes: list[dict] = []
        for document_id in sorted(document_ids):
            node = {
                "node_id": document_id,
                "node_type": "document",
                "payload": {
                    "record_type": "document",
                    "record_id": document_id,
                    "source_record": {"status": "extracted"},
                },
                "status": "observed",
            }
            node["record_sha256"] = answer_reader.record_sha256(node)
            nodes.append(node)
        for record in records:
            text = record["observed_text"]
            node = {
                "node_id": record["evidence_id"],
                "node_type": "evidence",
                "payload": {
                    "record_type": "evidence",
                    "record_id": record["evidence_id"],
                    "source_record": {
                        "document_id": record["document_id"],
                        "locator": record["locator"],
                        "source": {
                            "relative_path": record["source"]["relative_path"]
                        },
                    },
                    "observed_sha256": hashlib.sha256(
                        text.encode("utf-8")
                    ).hexdigest(),
                },
                "status": "observed",
            }
            node["record_sha256"] = answer_reader.record_sha256(node)
            nodes.append(node)
        nodes.sort(
            key=lambda item: (
                0 if item["node_type"] == "document" else 1,
                item["node_id"],
            )
        )
        for node in nodes:
            connection.execute(
                "INSERT INTO graph_nodes VALUES (?, ?, ?, ?, ?)",
                (
                    node["node_id"],
                    node["node_type"],
                    answer_reader.canonical_json(node["payload"]),
                    node["status"],
                    node["record_sha256"],
                ),
            )
        graph = {
            "graph_schema_version": "0.1",
            "nodes": nodes,
            "edges": [],
        }
        eligible_rows = [
            {
                "evidence_id": node["node_id"],
                "status": node["status"],
                "record_sha256": node["record_sha256"],
            }
            for node in nodes
            if node["node_type"] == "evidence"
        ]
        probe = packed([1.0, 0.0])
        embedding_rows = [
            {
                "evidence_id": evidence_id,
                "dimension": 2,
                "vector_f32_sha256": hashlib.sha256(
                    packed(vectors[evidence_id])
                ).hexdigest(),
            }
            for evidence_id in sorted(vectors)
        ]
        metadata = {
            "schema_version": "0.3",
            "model": "embedding-test",
            "embedding_dimension": 2,
            "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
            "documents_sha256": hashlib.sha256(documents.read_bytes()).hexdigest(),
            "content_security_state_sha256": hashlib.sha256(
                security_state.read_bytes()
            ).hexdigest(),
            "content_security_gate": True,
            "content_security_execution_policy": "never_execute",
            "index_purpose": "safe_answer",
            "answer_generation_allowed": True,
            "graph_schema_version": "0.1",
            "graph_status": "validated_safe_partition",
            "graph_retrieval_enabled": True,
            "graph_node_count": len(nodes),
            "graph_edge_count": 0,
            "graph_document_node_count": len(document_ids),
            "graph_evidence_node_count": len(records),
            "graph_sha256": answer_reader.record_sha256(graph),
            "graph_security_partition": partition,
            "graph_security_partition_sha256": partition["partition_sha256"],
            "graph_source_relation_input_count": 0,
            "graph_retrievable_evidence_count": len(eligible_rows),
            "graph_unresolved_evidence_count": 0,
            "graph_held_derived_evidence_count": 0,
            "graph_nonindexed_held_derived_evidence_count": 0,
            "graph_retrievable_evidence_set_sha256": answer_reader.record_sha256(
                eligible_rows
            ),
            "embedding_space_probe_version": (
                answer_reader.EMBEDDING_SPACE_PROBE_VERSION
            ),
            "embedding_space_probe_text_sha256": hashlib.sha256(
                answer_reader.EMBEDDING_SPACE_PROBE_TEXT.encode("utf-8")
            ).hexdigest(),
            "embedding_space_probe_dimension": 2,
            "embedding_space_probe_vector_f32_sha256": hashlib.sha256(
                probe
            ).hexdigest(),
            "graph_embeddings_sha256": answer_reader.record_sha256({
                "model": "embedding-test",
                "probe": {
                    "version": answer_reader.EMBEDDING_SPACE_PROBE_VERSION,
                    "text_sha256": hashlib.sha256(
                        answer_reader.EMBEDDING_SPACE_PROBE_TEXT.encode("utf-8")
                    ).hexdigest(),
                    "dimension": 2,
                    "vector_f32_sha256": hashlib.sha256(probe).hexdigest(),
                },
                "records": embedding_rows,
            }),
        }
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            [
                (key, json.dumps(value, ensure_ascii=False, sort_keys=True))
                for key, value in metadata.items()
            ],
        )
        connection.commit()
        return answer_reader.validate_answer_graph_contract(connection)
    finally:
        connection.close()


def validated_answer_policy(path: Path) -> dict:
    """Open an index read-only through the production answer validator."""
    answer_reader = load_module(
        "runtime_storage_answer_policy_reader",
        ENGINE / "answer_local_memory.py",
    )
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        return answer_reader.validate_answer_graph_contract(connection)
    finally:
        connection.close()


def prepare_completed_semantic_storage(
    bootstrap,
    generation: Path,
    *,
    build_id: str,
    directory_name: str | None = None,
) -> tuple[dict, Path]:
    """Create and independently validate a real completed storage fixture."""
    base_index = generation / "safe-answer-index.sqlite3"
    semantic, security = prepare_real_shadow_inputs(
        generation,
        semantic_name="02-semantic",
        security_name="03-security",
    )
    documents = semantic / "semantic-documents.jsonl"
    evidence = security / "safe-answer-evidence.jsonl"
    security_state = security / "content-security-state.json"
    records = read_jsonl(evidence)
    prepare_ready_answer_index_for_records(
        base_index,
        records,
        documents=documents,
        evidence=evidence,
        security_state=security_state,
    )

    fixture_log = generation / "semantic-storage-fixture.log"
    with fixture_log.open("w+", encoding="utf-8") as log:
        shadow_state = bootstrap.run_cross_document_semantic_graph_shadow(
            {bootstrap.CROSS_DOCUMENT_SHADOW_FLAG: True},
            semantic,
            security,
            generation,
            build_id,
            log,
        )
    if shadow_state.get("status") != "complete":
        raise AssertionError(f"real shadow fixture failed: {shadow_state}")

    with fixture_log.open("a+", encoding="utf-8") as log:
        state = bootstrap.run_cross_document_semantic_graph_storage(
            {bootstrap.CROSS_DOCUMENT_STORAGE_FLAG: True},
            semantic,
            security,
            generation,
            build_id,
            base_index,
            shadow_state,
            log,
        )
    if state.get("status") != "complete":
        raise AssertionError(f"real storage fixture failed: {state}")
    storage_dir = generation / (
        directory_name or bootstrap.CROSS_DOCUMENT_STORAGE_DIR
    )
    default_storage_dir = generation / bootstrap.CROSS_DOCUMENT_STORAGE_DIR
    if storage_dir != default_storage_dir:
        default_storage_dir.rename(storage_dir)
    output = storage_dir / "safe-answer-index.sqlite3"
    validated = bootstrap._independently_validate_semantic_storage(
        generation=generation,
        semantic=semantic,
        security=security,
        base_index=base_index,
        index=output,
        state_path=(
            storage_dir / bootstrap.CROSS_DOCUMENT_STORAGE_RUN_STATE
        ),
        expected_build_id=build_id,
    )
    if validated != state:
        raise AssertionError("real storage fixture validation changed state")
    return state, output


class FakeSemanticGraphTrustStore:
    """In-memory Keychain stand-in; runtime recovery never touches login Keychain."""

    def __init__(self) -> None:
        self.roots: dict[str, str] = {}
        self.create_calls: list[tuple[str, str]] = []
        self.read_calls: list[str] = []

    def create_root(self, generation: str, root_sha256: str) -> None:
        self.create_calls.append((generation, root_sha256))
        if generation in self.roots:
            raise ValueError("trust_store_create_failed")
        self.roots[generation] = root_sha256

    def read_root(self, generation: str) -> str:
        self.read_calls.append(generation)
        if generation not in self.roots:
            raise ValueError("trust_store_read_failed")
        return self.roots[generation]


def prepare_trust_recovery_fixture(
    bootstrap,
    base: Path,
    *,
    promotion_enabled: bool,
) -> tuple[Path, str, dict, dict, Path]:
    """Prepare the real storage boundary at the pre-registration crash window."""
    generation, _pending = prepare_published_generation(bootstrap, base)
    build_id = "b" * 32
    pending_shadow = bootstrap._shadow_run_base(
        generation,
        build_id,
        status="pending",
        reason_code="scheduled_after_production_publish",
        elapsed_ms=0,
    )
    pending_storage = bootstrap._storage_run_base(
        generation,
        build_id,
        status="pending",
        reason_code="awaiting_validated_shadow",
        elapsed_ms=0,
    )
    state, output = prepare_completed_semantic_storage(
        bootstrap,
        generation,
        build_id=build_id,
    )
    base_index = generation / "safe-answer-index.sqlite3"
    base_hash = bootstrap.sha256_file(base_index)
    configured = bootstrap.load_json(bootstrap.CONFIG)
    configured.update({
        "index_path": str(base_index),
        bootstrap.BASE_ANSWER_INDEX_SHA256_KEY: base_hash,
        bootstrap.CROSS_DOCUMENT_STORAGE_FLAG: True,
        bootstrap.CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG: promotion_enabled,
    })
    configured.pop(bootstrap.CROSS_DOCUMENT_STORAGE_CONFIG_KEY, None)
    configured.pop(bootstrap.CROSS_DOCUMENT_TRUST_CONFIG_KEY, None)
    bootstrap.atomic_json(bootstrap.CONFIG, configured)

    marker_path = generation / bootstrap.GENERATION_MARKER
    marker = bootstrap.load_json(marker_path)
    marker.update({
        "status": "published",
        "build_id": build_id,
        bootstrap.BASE_ANSWER_INDEX_SHA256_KEY: base_hash,
        "cross_document_semantic_graph_storage_enabled": True,
        "cross_document_semantic_graph_answer_promotion_enabled": (
            promotion_enabled
        ),
        "cross_document_semantic_graph_shadow": pending_shadow,
        "cross_document_semantic_graph_storage": pending_storage,
    })
    bootstrap.atomic_json(marker_path, marker)
    bootstrap.atomic_json(bootstrap.STATE, {
        "phase": "ready",
        "message": "索引の作成が完了しました。",
        "error": "",
        "cross_document_semantic_graph_answer_promotion_enabled": (
            promotion_enabled
        ),
        "cross_document_semantic_graph_shadow": pending_shadow,
        "cross_document_semantic_graph_storage": pending_storage,
    })
    registration = bootstrap._semantic_storage_registration(
        generation,
        state,
        semantic=generation / "02-semantic",
        security=generation / "03-security",
        expected_build_id=build_id,
    )
    return generation, build_id, registration, state, output


class RuntimeRecoveryTests(unittest.TestCase):
    def test_corrupt_config_and_state_fail_closed_with_readable_status(self) -> None:
        for corrupt_target in ("config", "state"):
            with self.subTest(corrupt_target=corrupt_target), TemporaryDirectory() as temporary:
                bootstrap = load_module(
                    f"runtime_bootstrap_corrupt_{corrupt_target}",
                    ROOT / "app" / "bootstrap.py",
                )
                support = Path(temporary) / "support"
                support.mkdir()
                bootstrap.SUPPORT = support
                bootstrap.CONFIG = support / "config.json"
                bootstrap.STATE = support / "state.json"
                bootstrap.atomic_json(bootstrap.CONFIG, {})
                bootstrap.atomic_json(bootstrap.STATE, {
                    "phase": "ready",
                    "message": "索引の作成が完了しました。",
                    "error": "",
                })
                target = (
                    bootstrap.CONFIG
                    if corrupt_target == "config"
                    else bootstrap.STATE
                )
                target.write_text('{"broken":', encoding="utf-8")

                report = bootstrap.recover_interrupted_build()

                expected_status = (
                    "invalid_configuration"
                    if corrupt_target == "config"
                    else "invalid_runtime_state"
                )
                self.assertEqual(expected_status, report["status"])
                recovered_state = bootstrap.load_json(bootstrap.STATE)
                self.assertEqual("error", recovered_state["phase"])
                self.assertFalse(bootstrap.diagnose()["index_ready"])

    def test_configure_source_enables_shadow_without_publishing_a_graph_pointer(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_shadow_configure", ROOT / "app" / "bootstrap.py"
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            bootstrap.SUPPORT = base / "support"
            bootstrap.CONFIG = bootstrap.SUPPORT / "config.json"
            bootstrap.atomic_json(bootstrap.CONFIG, {
                "embedding_model": "embeddinggemma:latest",
                "answer_model": "gemma4:12b",
                "audit_model": "gemma4:12b",
                "active_generation": "generation-" + "f" * 32,
                "semantic_graph_shadow_path": "/stale/shadow.sqlite3",
            })

            configured = bootstrap.configure_source(source)

            self.assertTrue(configured[bootstrap.CROSS_DOCUMENT_SHADOW_FLAG])
            self.assertNotIn(
                bootstrap.CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG,
                configured,
            )
            self.assertNotIn("active_generation", configured)
            self.assertNotIn("semantic_graph_shadow_path", configured)

    def test_configure_source_preserves_explicit_shadow_rollback(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_shadow_rollback", ROOT / "app" / "bootstrap.py"
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            bootstrap.SUPPORT = base / "support"
            bootstrap.CONFIG = bootstrap.SUPPORT / "config.json"
            bootstrap.atomic_json(bootstrap.CONFIG, {
                "embedding_model": "embeddinggemma:latest",
                "answer_model": "gemma4:12b",
                "audit_model": "gemma4:12b",
                bootstrap.CROSS_DOCUMENT_SHADOW_FLAG: False,
            })

            configured = bootstrap.configure_source(source)

            self.assertFalse(configured[bootstrap.CROSS_DOCUMENT_SHADOW_FLAG])

    def test_configure_and_diagnose_preserve_candidate_rollback(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_candidate_rollback",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            bootstrap.SUPPORT = base / "support"
            bootstrap.CONFIG = bootstrap.SUPPORT / "config.json"
            bootstrap.atomic_json(bootstrap.CONFIG, {
                "embedding_model": "embeddinggemma:latest",
                "answer_model": "gemma4:12b",
                "audit_model": "gemma4:12b",
                bootstrap.CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG: False,
            })

            configured = bootstrap.configure_source(source)

            self.assertFalse(
                configured[bootstrap.CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG]
            )
            with (
                mock.patch.object(bootstrap, "total_memory_gb", return_value=24),
                mock.patch.object(bootstrap, "free_gb", return_value=80),
                mock.patch.object(bootstrap, "ollama_binary", return_value=None),
                mock.patch.object(bootstrap, "ollama_online", return_value=False),
            ):
                diagnosis = bootstrap.diagnose()
            self.assertFalse(
                diagnosis[
                    "cross_document_semantic_graph_query_candidate_enabled"
                ]
            )

    def test_configure_and_diagnose_preserve_independent_audit_rollback(
        self,
    ) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_independent_audit_rollback",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            bootstrap.SUPPORT = base / "support"
            bootstrap.CONFIG = bootstrap.SUPPORT / "config.json"
            bootstrap.atomic_json(bootstrap.CONFIG, {
                "embedding_model": "embeddinggemma:latest",
                "answer_model": "gemma4:12b",
                "audit_model": "gemma4:12b",
                bootstrap.CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG: False,
            })

            configured = bootstrap.configure_source(source)

            self.assertFalse(
                configured[
                    bootstrap.CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG
                ]
            )
            with (
                mock.patch.object(bootstrap, "total_memory_gb", return_value=24),
                mock.patch.object(bootstrap, "free_gb", return_value=80),
                mock.patch.object(bootstrap, "ollama_binary", return_value=None),
                mock.patch.object(bootstrap, "ollama_online", return_value=False),
            ):
                diagnosis = bootstrap.diagnose()
            self.assertFalse(
                diagnosis[
                    "cross_document_semantic_graph_independent_edge_audit_enabled"
                ]
            )

    def test_configure_defaults_independent_edge_audit_to_enabled(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_independent_audit_default",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            bootstrap.SUPPORT = base / "support"
            bootstrap.CONFIG = bootstrap.SUPPORT / "config.json"

            configured = bootstrap.configure_source(source)

            self.assertTrue(
                configured[
                    bootstrap.CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG
                ]
            )
            self.assertTrue(
                configured[bootstrap.CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG]
            )

    def test_configure_source_enables_storage_and_clears_stale_registration(
        self,
    ) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_storage_configure", ROOT / "app" / "bootstrap.py"
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            bootstrap.SUPPORT = base / "support"
            bootstrap.CONFIG = bootstrap.SUPPORT / "config.json"
            bootstrap.atomic_json(bootstrap.CONFIG, {
                "embedding_model": "embeddinggemma:latest",
                "answer_model": "gemma4:12b",
                "audit_model": "gemma4:12b",
                bootstrap.CROSS_DOCUMENT_STORAGE_CONFIG_KEY: {
                    "database_path": "/stale/semantic.sqlite3",
                },
            })

            configured = bootstrap.configure_source(source)

            self.assertTrue(configured[bootstrap.CROSS_DOCUMENT_STORAGE_FLAG])
            self.assertNotIn(
                bootstrap.CROSS_DOCUMENT_STORAGE_CONFIG_KEY, configured
            )

    def test_semantic_storage_disabled_never_runs_projector(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_storage_disabled", ROOT / "app" / "bootstrap.py"
        )
        with TemporaryDirectory() as temporary:
            generation = Path(temporary) / ("generation-" + "1" * 32)
            generation.mkdir()
            semantic, security = prepare_shadow_inputs(bootstrap, generation)
            base_index = generation / "safe-answer-index.sqlite3"
            base_index.write_bytes(b"base")
            shadow_state = bootstrap._shadow_run_base(
                generation,
                "build-disabled",
                status="complete",
                reason_code="none",
                elapsed_ms=1,
            )
            with mock.patch.object(
                bootstrap, "run_shadow_command"
            ) as projector:
                state = bootstrap.run_cross_document_semantic_graph_storage(
                    {bootstrap.CROSS_DOCUMENT_STORAGE_FLAG: False},
                    semantic,
                    security,
                    generation,
                    "build-disabled",
                    base_index,
                    shadow_state,
                    io.StringIO(),
                )

            projector.assert_not_called()
            self.assertEqual("disabled", state["status"])
            self.assertFalse(state["retrieval_enabled"])
            self.assertFalse(state["used_for_answers"])
            self.assertFalse(
                (generation / bootstrap.CROSS_DOCUMENT_STORAGE_DIR).exists()
            )

    def test_semantic_storage_publishes_validated_copy_without_mutating_base(
        self,
    ) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_storage_success", ROOT / "app" / "bootstrap.py"
        )
        with TemporaryDirectory() as temporary:
            generation = Path(temporary) / ("generation-" + "2" * 32)
            generation.mkdir()
            semantic, security = prepare_shadow_inputs(bootstrap, generation)
            shadow_dir = generation / bootstrap.CROSS_DOCUMENT_SHADOW_DIR
            shadow_dir.mkdir()
            base_index = generation / "safe-answer-index.sqlite3"
            base_index.write_bytes(b"immutable-base")
            base_hash = bootstrap.sha256_file(base_index)
            build_id = "build-storage-success"
            snapshot_id = "xkgs_" + "3" * 32
            shadow_state = {
                **bootstrap._shadow_run_base(
                    generation,
                    build_id,
                    status="complete",
                    reason_code="none",
                    elapsed_ms=1,
                ),
                "graph_snapshot_id": snapshot_id,
            }

            def fake_projector(command, _log, _timeout):
                output = Path(command[command.index("--output") + 1])
                state_path = Path(command[command.index("--state") + 1])
                output.parent.mkdir()
                output.write_bytes(b"validated-enriched-copy")
                bootstrap.atomic_json(state_path, {
                    "schema_version": "0.1",
                    "record_type": (
                        "cross_document_semantic_graph_answer_index_projection_state"
                    ),
                    "status": "complete",
                    "question_independent": True,
                    "external_network_used": False,
                    "storage_only": True,
                    "retrieval_enabled": False,
                    "used_for_answers": False,
                    "answer_behavior_changed": False,
                    "generation": generation.name,
                    "base": {
                        "sqlite_file": base_index.name,
                        "sqlite_sha256": base_hash,
                    },
                    "shadow": {
                        "directory": shadow_dir.name,
                        "build_id": build_id,
                        "graph_snapshot_id": snapshot_id,
                    },
                    "inputs": {
                        "content_security_state_sha256": bootstrap.sha256_file(
                            security / "content-security-state.json"
                        ),
                        "documents_input_sha256": bootstrap.sha256_file(
                            semantic / "semantic-documents.jsonl"
                        ),
                        "evidence_input_sha256": bootstrap.sha256_file(
                            security / "safe-answer-evidence.jsonl"
                        ),
                        "source_evidence_input_sha256": bootstrap.sha256_file(
                            semantic / "semantic-evidence.jsonl"
                        ),
                    },
                    "output": {
                        "sqlite_file": output.name,
                        "state_file": state_path.name,
                        "sqlite_sha256": bootstrap.sha256_file(output),
                    },
                })

            with (
                mock.patch.object(
                    bootstrap,
                    "_cross_document_storage_tool",
                    return_value=Path("/tools/projector.py"),
                ),
                mock.patch.object(
                    bootstrap,
                    "_content_security_shadow_validator",
                    return_value=Path("/tools/security-validator.py"),
                ),
                mock.patch.object(
                    bootstrap,
                    "run_shadow_command",
                    side_effect=fake_projector,
                ) as runner,
            ):
                state = bootstrap.run_cross_document_semantic_graph_storage(
                    {},
                    semantic,
                    security,
                    generation,
                    build_id,
                    base_index,
                    shadow_state,
                    io.StringIO(),
                )

            self.assertEqual("complete", state["status"])
            self.assertFalse(state["retrieval_enabled"])
            self.assertFalse(state["used_for_answers"])
            self.assertEqual(base_hash, bootstrap.sha256_file(base_index))
            self.assertFalse(
                (
                    generation
                    / (bootstrap.CROSS_DOCUMENT_STORAGE_DIR + ".building")
                ).exists()
            )
            self.assertTrue(
                (
                    generation
                    / bootstrap.CROSS_DOCUMENT_STORAGE_DIR
                    / "safe-answer-index.sqlite3"
                ).is_file()
            )
            runner.assert_called_once()

    def test_build_switches_to_storage_copy_only_after_base_is_ready(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_storage_build", ROOT / "app" / "bootstrap.py"
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            support = base / "support"
            source = base / "source"
            source.mkdir()
            workspace = support / "data"
            bootstrap.SUPPORT = support
            bootstrap.CONFIG = support / "config.json"
            bootstrap.STATE = support / "state.json"
            bootstrap.atomic_json(bootstrap.CONFIG, {
                "source_root": str(source),
                "workspace": str(workspace),
                "embedding_model": "embeddinggemma:latest",
                "answer_model": "gemma4:12b",
                "audit_model": "gemma4:12b",
                bootstrap.CROSS_DOCUMENT_SHADOW_FLAG: True,
                bootstrap.CROSS_DOCUMENT_STORAGE_FLAG: True,
            })

            def fake_semantic(_source, _paths, semantic, security, _log):
                return prepare_reader_contract_semantic_fixture(
                    bootstrap,
                    semantic,
                    security,
                )

            def fake_run(command, _log):
                if any(
                    str(value).endswith("build_path_graph.py")
                    for value in command
                ):
                    output = Path(command[command.index("--output-dir") + 1])
                    (output / "path-source-inventory.jsonl").write_text(
                        "", encoding="utf-8"
                    )
                if any(
                    str(value).endswith("build_local_semantic_index.py")
                    for value in command
                ):
                    Path(command[command.index("--output") + 1]).write_bytes(
                        b"immutable-base-index"
                    )

            def fake_shadow(
                _config, _semantic, _security, generation, build_id, _log
            ):
                return {
                    **bootstrap._shadow_run_base(
                        generation,
                        build_id,
                        status="complete",
                        reason_code="none",
                        elapsed_ms=1,
                    ),
                    "graph_snapshot_id": "xkgs_" + "4" * 32,
                }

            promoted_output: list[Path] = []

            def fake_storage(
                _config,
                _semantic,
                _security,
                generation,
                build_id,
                base_index,
                shadow_state,
                _log,
            ):
                published = bootstrap.load_json(bootstrap.CONFIG)
                self.assertEqual(str(base_index), published["index_path"])
                self.assertTrue(base_index.is_file())
                self.assertEqual(
                    "pending",
                    bootstrap.load_json(bootstrap.STATE)[
                        "cross_document_semantic_graph_storage"
                    ]["status"],
                )
                output = (
                    generation
                    / bootstrap.CROSS_DOCUMENT_STORAGE_DIR
                    / "safe-answer-index.sqlite3"
                )
                output.parent.mkdir()
                output.write_bytes(b"validated-storage-copy")
                promoted_output.append(output)
                return {
                    **bootstrap._storage_run_base(
                        generation,
                        build_id,
                        status="complete",
                        reason_code="none",
                        elapsed_ms=2,
                    ),
                    "graph_snapshot_id": shadow_state["graph_snapshot_id"],
                }

            def fake_registration(
                generation,
                state,
                *,
                semantic,
                security,
                expected_build_id=None,
            ):
                self.assertEqual(state["status"], "complete")
                self.assertEqual(state["build_id"], expected_build_id)
                self.assertEqual("02-semantic", semantic.name)
                self.assertEqual("03-security", security.name)
                output = promoted_output[0]
                return {
                    "status": "validated_storage_only",
                    "generation": generation.name,
                    "database_path": str(output),
                    "retrieval_enabled": False,
                    "used_for_answers": False,
                }

            identifiers = [
                types.SimpleNamespace(hex="5" * 32),
                types.SimpleNamespace(hex="6" * 32),
            ]
            trust_locator = {
                "status": "trusted",
                "generation": "generation-" + "5" * 32,
            }
            with (
                mock.patch.object(
                    bootstrap.uuid, "uuid4", side_effect=identifiers
                ),
                mock.patch.object(bootstrap, "run", side_effect=fake_run),
                mock.patch.object(
                    bootstrap,
                    "run_semantic_pipeline",
                    side_effect=fake_semantic,
                ),
                mock.patch.object(bootstrap, "ensure_models", return_value=[]),
                mock.patch.object(
                    bootstrap, "local_model_available", return_value=True
                ),
                mock.patch.object(
                    bootstrap,
                    "run_cross_document_semantic_graph_shadow",
                    side_effect=fake_shadow,
                ),
                mock.patch.object(
                    bootstrap,
                    "run_cross_document_semantic_graph_storage",
                    side_effect=fake_storage,
                ),
                mock.patch.object(
                    bootstrap,
                    "_semantic_storage_registration",
                    side_effect=fake_registration,
                ),
                mock.patch.object(
                    bootstrap,
                    "_publish_semantic_graph_trust_root",
                    return_value=trust_locator,
                ) as trust_publish,
            ):
                bootstrap.build_index()

            configured = bootstrap.load_json(bootstrap.CONFIG)
            self.assertEqual(str(promoted_output[0]), configured["index_path"])
            self.assertEqual(
                "validated_storage_only",
                configured[bootstrap.CROSS_DOCUMENT_STORAGE_CONFIG_KEY][
                    "status"
                ],
            )
            self.assertTrue(
                configured[bootstrap.CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG]
            )
            self.assertEqual(
                trust_locator,
                configured[bootstrap.CROSS_DOCUMENT_TRUST_CONFIG_KEY],
            )
            trust_publish.assert_called_once()
            immutable_base = (
                promoted_output[0].parents[1] / "safe-answer-index.sqlite3"
            )
            self.assertEqual(b"immutable-base-index", immutable_base.read_bytes())
            runtime_state = bootstrap.load_json(bootstrap.STATE)
            self.assertEqual("ready", runtime_state["phase"])
            self.assertTrue(
                configured[bootstrap.CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG]
            )
            self.assertTrue(
                runtime_state[
                    "cross_document_semantic_graph_query_candidate_enabled"
                ]
            )
            self.assertTrue(
                configured[
                    bootstrap.CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG
                ]
            )
            self.assertTrue(
                runtime_state[
                    "cross_document_semantic_graph_independent_edge_audit_enabled"
                ]
            )
            marker = bootstrap.load_json(
                promoted_output[0].parents[1] / bootstrap.GENERATION_MARKER
            )
            self.assertTrue(
                marker[
                    "cross_document_semantic_graph_query_candidate_enabled"
                ]
            )
            self.assertTrue(
                marker[
                    "cross_document_semantic_graph_independent_edge_audit_enabled"
                ]
            )
            self.assertEqual(
                "complete",
                runtime_state["cross_document_semantic_graph_storage"][
                    "status"
                ],
            )

    def test_build_honors_storage_disable_change_during_registration(
        self,
    ) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_storage_disable_race",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            support = base / "support"
            source = base / "source"
            source.mkdir()
            workspace = support / "data"
            bootstrap.SUPPORT = support
            bootstrap.CONFIG = support / "config.json"
            bootstrap.STATE = support / "state.json"
            bootstrap.atomic_json(bootstrap.CONFIG, {
                "source_root": str(source),
                "workspace": str(workspace),
                "embedding_model": "embeddinggemma:latest",
                "answer_model": "gemma4:12b",
                "audit_model": "gemma4:12b",
                bootstrap.CROSS_DOCUMENT_SHADOW_FLAG: True,
                bootstrap.CROSS_DOCUMENT_STORAGE_FLAG: True,
                bootstrap.CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG: False,
                bootstrap.CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG: False,
            })

            def fake_semantic(_source, _paths, semantic, security, _log):
                return prepare_reader_contract_semantic_fixture(
                    bootstrap,
                    semantic,
                    security,
                )

            def fake_run(command, _log):
                if any(
                    str(value).endswith("build_path_graph.py")
                    for value in command
                ):
                    output_dir = Path(
                        command[command.index("--output-dir") + 1]
                    )
                    (output_dir / "path-source-inventory.jsonl").write_text(
                        "", encoding="utf-8"
                    )
                if any(
                    str(value).endswith("build_local_semantic_index.py")
                    for value in command
                ):
                    Path(command[command.index("--output") + 1]).write_bytes(
                        b"immutable-base-index"
                    )

            def fake_shadow(
                _config, _semantic, _security, generation, build_id, _log
            ):
                return {
                    **bootstrap._shadow_run_base(
                        generation,
                        build_id,
                        status="complete",
                        reason_code="none",
                        elapsed_ms=1,
                    ),
                    "graph_snapshot_id": "xkgs_" + "7" * 32,
                }

            prepared_output: list[Path] = []

            def fake_storage(
                _config,
                _semantic,
                _security,
                generation,
                build_id,
                base_index,
                shadow_state,
                _log,
            ):
                current = bootstrap.load_json(bootstrap.CONFIG)
                self.assertTrue(
                    current[bootstrap.CROSS_DOCUMENT_STORAGE_FLAG]
                )
                self.assertEqual(str(base_index), current["index_path"])
                output = (
                    generation
                    / bootstrap.CROSS_DOCUMENT_STORAGE_DIR
                    / "safe-answer-index.sqlite3"
                )
                output.parent.mkdir()
                output.write_bytes(b"prepared-storage-copy")
                prepared_output.append(output)
                return {
                    **bootstrap._storage_run_base(
                        generation,
                        build_id,
                        status="complete",
                        reason_code="none",
                        elapsed_ms=2,
                    ),
                    "output": {
                        "sqlite_file": output.name,
                        "sqlite_sha256": bootstrap.sha256_file(output),
                    },
                    "shadow": {
                        "build_id": build_id,
                        "graph_snapshot_id": shadow_state[
                            "graph_snapshot_id"
                        ],
                    },
                }

            def fake_registration(
                generation,
                _state,
                *,
                semantic,
                security,
                expected_build_id=None,
            ):
                current = bootstrap.load_json(bootstrap.CONFIG)
                self.assertTrue(
                    current[bootstrap.CROSS_DOCUMENT_STORAGE_FLAG]
                )
                current[bootstrap.CROSS_DOCUMENT_STORAGE_FLAG] = False
                bootstrap.atomic_json(bootstrap.CONFIG, current)
                return {
                    "status": "validated_storage_only",
                    "generation": generation.name,
                    "database_path": str(prepared_output[0]),
                    "base_index_sha256": current[
                        bootstrap.BASE_ANSWER_INDEX_SHA256_KEY
                    ],
                    "retrieval_enabled": False,
                    "used_for_answers": False,
                }

            identifiers = [
                types.SimpleNamespace(hex="7" * 32),
                types.SimpleNamespace(hex="8" * 32),
            ]
            with (
                mock.patch.object(
                    bootstrap.uuid, "uuid4", side_effect=identifiers
                ),
                mock.patch.object(bootstrap, "run", side_effect=fake_run),
                mock.patch.object(
                    bootstrap,
                    "run_semantic_pipeline",
                    side_effect=fake_semantic,
                ),
                mock.patch.object(bootstrap, "ensure_models", return_value=[]),
                mock.patch.object(
                    bootstrap, "local_model_available", return_value=True
                ),
                mock.patch.object(
                    bootstrap,
                    "run_cross_document_semantic_graph_shadow",
                    side_effect=fake_shadow,
                ),
                mock.patch.object(
                    bootstrap,
                    "run_cross_document_semantic_graph_storage",
                    side_effect=fake_storage,
                ),
                mock.patch.object(
                    bootstrap,
                    "_semantic_storage_registration",
                    side_effect=fake_registration,
                ) as registration,
            ):
                bootstrap.build_index()

            registration.assert_called_once()
            self.assertEqual(1, len(prepared_output))
            self.assertEqual(
                b"prepared-storage-copy", prepared_output[0].read_bytes()
            )
            generation = prepared_output[0].parents[1]
            base_index = generation / "safe-answer-index.sqlite3"
            configured = bootstrap.load_json(bootstrap.CONFIG)
            self.assertFalse(
                configured[bootstrap.CROSS_DOCUMENT_STORAGE_FLAG]
            )
            self.assertEqual(str(base_index), configured["index_path"])
            self.assertNotIn(
                bootstrap.CROSS_DOCUMENT_STORAGE_CONFIG_KEY, configured
            )
            self.assertEqual(b"immutable-base-index", base_index.read_bytes())
            storage_state = bootstrap.load_json(bootstrap.STATE)[
                "cross_document_semantic_graph_storage"
            ]
            runtime_state = bootstrap.load_json(bootstrap.STATE)
            self.assertFalse(
                runtime_state[
                    "cross_document_semantic_graph_query_candidate_enabled"
                ]
            )
            self.assertFalse(
                runtime_state[
                    "cross_document_semantic_graph_independent_edge_audit_enabled"
                ]
            )
            marker = bootstrap.load_json(
                generation / bootstrap.GENERATION_MARKER
            )
            self.assertFalse(
                marker[
                    "cross_document_semantic_graph_query_candidate_enabled"
                ]
            )
            self.assertFalse(
                marker[
                    "cross_document_semantic_graph_independent_edge_audit_enabled"
                ]
            )
            self.assertEqual("disabled", storage_state["status"])
            self.assertEqual(
                "feature_disabled_during_registration",
                storage_state["reason_code"],
            )
            self.assertFalse(storage_state["used_for_answers"])

    def test_recovery_finishes_valid_semantic_storage_pointer_switch(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_storage_recovery", ROOT / "app" / "bootstrap.py"
        )
        with TemporaryDirectory() as temporary:
            generation, _pending = prepare_published_generation(
                bootstrap, Path(temporary)
            )
            state, output = prepare_completed_semantic_storage(
                bootstrap,
                generation,
                build_id="build-published-shadow",
            )
            base_index = generation / "safe-answer-index.sqlite3"
            base_hash = bootstrap.sha256_file(base_index)
            base_policy = validated_answer_policy(base_index)
            output_policy = validated_answer_policy(output)
            self.assertEqual(
                base_policy["eligible_evidence_ids"],
                output_policy["eligible_evidence_ids"],
            )
            self.assertEqual(
                base_policy["graph_sha256"], output_policy["graph_sha256"]
            )
            self.assertEqual(
                base_policy["partition_sha256"],
                output_policy["partition_sha256"],
            )

            with mock.patch.object(
                bootstrap, "_pid_is_alive", return_value=False
            ):
                report = bootstrap.recover_interrupted_build()

            self.assertEqual("recovered_published_observers", report["status"])
            self.assertEqual("complete", report["storage_status"])
            self.assertEqual(
                "completed_pointer_switch", report["storage_action"]
            )
            configured = bootstrap.load_json(bootstrap.CONFIG)
            self.assertEqual(str(output), configured["index_path"])
            registration = configured[
                bootstrap.CROSS_DOCUMENT_STORAGE_CONFIG_KEY
            ]
            self.assertFalse(registration["retrieval_enabled"])
            self.assertFalse(registration["used_for_answers"])
            runtime_state = bootstrap.load_json(bootstrap.STATE)
            self.assertEqual(
                state,
                runtime_state["cross_document_semantic_graph_storage"],
            )
            self.assertEqual(base_hash, bootstrap.sha256_file(base_index))
            self.assertEqual(
                base_policy["eligible_evidence_ids"],
                validated_answer_policy(Path(configured["index_path"]))[
                    "eligible_evidence_ids"
                ],
            )
            marker_path = generation / bootstrap.GENERATION_MARKER
            stable_files = {
                "config": bootstrap.CONFIG.read_bytes(),
                "state": bootstrap.STATE.read_bytes(),
                "marker": marker_path.read_bytes(),
            }

            with mock.patch.object(
                bootstrap, "_pid_is_alive", return_value=False
            ):
                second_report = bootstrap.recover_interrupted_build()

            self.assertEqual("unchanged", second_report["status"])
            self.assertEqual(
                "verified_complete", second_report["storage_action"]
            )
            self.assertEqual(stable_files["config"], bootstrap.CONFIG.read_bytes())
            self.assertEqual(stable_files["state"], bootstrap.STATE.read_bytes())
            self.assertEqual(stable_files["marker"], marker_path.read_bytes())

            for enabled in (False, True):
                with self.subTest(query_candidate_flag=enabled):
                    configured = bootstrap.load_json(bootstrap.CONFIG)
                    configured[
                        bootstrap.CROSS_DOCUMENT_QUERY_CANDIDATE_FLAG
                    ] = enabled
                    bootstrap.atomic_json(bootstrap.CONFIG, configured)
                    with mock.patch.object(
                        bootstrap, "_pid_is_alive", return_value=False
                    ):
                        flag_report = bootstrap.recover_interrupted_build()
                    self.assertEqual(
                        "recovered_published_observers",
                        flag_report["status"],
                    )
                    self.assertIs(
                        bootstrap.load_json(bootstrap.STATE)[
                            "cross_document_semantic_graph_query_candidate_enabled"
                        ],
                        enabled,
                    )
                    self.assertIs(
                        bootstrap.load_json(marker_path)[
                            "cross_document_semantic_graph_query_candidate_enabled"
                        ],
                        enabled,
                    )

            for enabled in (False, True):
                with self.subTest(independent_edge_audit_flag=enabled):
                    configured = bootstrap.load_json(bootstrap.CONFIG)
                    configured[
                        bootstrap.CROSS_DOCUMENT_INDEPENDENT_EDGE_AUDIT_FLAG
                    ] = enabled
                    bootstrap.atomic_json(bootstrap.CONFIG, configured)
                    with mock.patch.object(
                        bootstrap, "_pid_is_alive", return_value=False
                    ):
                        flag_report = bootstrap.recover_interrupted_build()
                    self.assertEqual(
                        "recovered_published_observers",
                        flag_report["status"],
                    )
                    self.assertIs(
                        bootstrap.load_json(bootstrap.STATE)[
                            "cross_document_semantic_graph_independent_edge_audit_enabled"
                        ],
                        enabled,
                    )
                    self.assertIs(
                        bootstrap.load_json(marker_path)[
                            "cross_document_semantic_graph_independent_edge_audit_enabled"
                        ],
                        enabled,
                    )

    def test_promotion_trust_root_publish_then_recovery_binds_config_locator(
        self,
    ) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_trust_publish_recovery",
            ROOT / "app" / "bootstrap.py",
        )
        trust_module = load_module(
            "runtime_bootstrap_trust_publish_module",
            ROOT / "app" / "semantic_graph_trust.py",
        )
        store = FakeSemanticGraphTrustStore()
        trust_module.KeychainTrustStore = lambda: store
        with TemporaryDirectory() as temporary:
            generation, build_id, registration, state, output = (
                prepare_trust_recovery_fixture(
                    bootstrap,
                    Path(temporary),
                    promotion_enabled=True,
                )
            )
            with mock.patch.object(
                bootstrap,
                "_semantic_graph_trust_module",
                return_value=trust_module,
            ):
                published_locator = (
                    bootstrap._publish_semantic_graph_trust_root(
                        generation,
                        build_id,
                        registration,
                        state,
                    )
                )
                self.assertEqual(
                    trust_module.TRUST_REGISTRATION_FIELDS,
                    set(published_locator),
                )
                self.assertEqual(1, len(store.create_calls))
                self.assertEqual(generation.name, store.create_calls[0][0])
                # Crash window: the independent root exists, while CONFIG still
                # points at the base index and contains no trust locator.
                configured_before = bootstrap.load_json(bootstrap.CONFIG)
                self.assertEqual(
                    str(generation / "safe-answer-index.sqlite3"),
                    configured_before["index_path"],
                )
                self.assertNotIn(
                    bootstrap.CROSS_DOCUMENT_TRUST_CONFIG_KEY,
                    configured_before,
                )
                with mock.patch.object(
                    bootstrap, "_pid_is_alive", return_value=False
                ):
                    report = bootstrap.recover_interrupted_build()

            self.assertEqual(
                "recovered_published_observers", report["status"]
            )
            self.assertEqual(
                "completed_pointer_switch", report["storage_action"]
            )
            configured = bootstrap.load_json(bootstrap.CONFIG)
            self.assertEqual(str(output), configured["index_path"])
            self.assertTrue(
                configured[bootstrap.CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG]
            )
            recovered_locator = configured[
                bootstrap.CROSS_DOCUMENT_TRUST_CONFIG_KEY
            ]
            self.assertEqual(published_locator, recovered_locator)
            verified = trust_module.validate_trust_root(
                generation, registration, store
            )
            trust_module.validate_trust_registration(
                recovered_locator,
                generation,
                registration,
                verified_root=verified,
            )
            self.assertTrue(
                bootstrap.load_json(bootstrap.STATE)[
                    "cross_document_semantic_graph_answer_promotion_enabled"
                ]
            )
            self.assertTrue(
                bootstrap.load_json(
                    generation / bootstrap.GENERATION_MARKER
                )[
                    "cross_document_semantic_graph_answer_promotion_enabled"
                ]
            )

    def test_promotion_recovery_with_manifest_but_missing_root_fails_closed(
        self,
    ) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_trust_missing_root",
            ROOT / "app" / "bootstrap.py",
        )
        trust_module = load_module(
            "runtime_bootstrap_trust_missing_root_module",
            ROOT / "app" / "semantic_graph_trust.py",
        )
        store = FakeSemanticGraphTrustStore()
        trust_module.KeychainTrustStore = lambda: store
        with TemporaryDirectory() as temporary:
            generation, build_id, registration, state, _output = (
                prepare_trust_recovery_fixture(
                    bootstrap,
                    Path(temporary),
                    promotion_enabled=True,
                )
            )
            manifest = trust_module.build_trust_manifest(
                generation, build_id, registration, state
            )
            trust_module.write_trust_manifest(
                generation, manifest, registration
            )
            with (
                mock.patch.object(
                    bootstrap,
                    "_semantic_graph_trust_module",
                    return_value=trust_module,
                ),
                mock.patch.object(
                    bootstrap, "_pid_is_alive", return_value=False
                ),
            ):
                report = bootstrap.recover_interrupted_build()

            self.assertEqual("held", report["storage_status"])
            self.assertEqual("kept_base", report["storage_action"])
            configured = bootstrap.load_json(bootstrap.CONFIG)
            self.assertEqual(
                str(generation / "safe-answer-index.sqlite3"),
                configured["index_path"],
            )
            self.assertTrue(
                configured[bootstrap.CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG]
            )
            self.assertNotIn(
                bootstrap.CROSS_DOCUMENT_STORAGE_CONFIG_KEY, configured
            )
            self.assertNotIn(
                bootstrap.CROSS_DOCUMENT_TRUST_CONFIG_KEY, configured
            )
            self.assertEqual([], store.create_calls)
            self.assertGreaterEqual(len(store.read_calls), 1)

    def test_promotion_recovery_before_manifest_never_mints_missing_root(
        self,
    ) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_trust_before_manifest",
            ROOT / "app" / "bootstrap.py",
        )
        trust_module = load_module(
            "runtime_bootstrap_trust_before_manifest_module",
            ROOT / "app" / "semantic_graph_trust.py",
        )
        store = FakeSemanticGraphTrustStore()
        trust_module.KeychainTrustStore = lambda: store
        with TemporaryDirectory() as temporary:
            generation, _build_id, _registration, _state, _output = (
                prepare_trust_recovery_fixture(
                    bootstrap,
                    Path(temporary),
                    promotion_enabled=True,
                )
            )
            self.assertFalse(
                trust_module.trust_manifest_path(generation).exists()
            )
            with (
                mock.patch.object(
                    bootstrap,
                    "_semantic_graph_trust_module",
                    return_value=trust_module,
                ),
                mock.patch.object(
                    bootstrap, "_pid_is_alive", return_value=False
                ),
            ):
                report = bootstrap.recover_interrupted_build()

            self.assertEqual("held", report["storage_status"])
            self.assertEqual("kept_base", report["storage_action"])
            configured = bootstrap.load_json(bootstrap.CONFIG)
            self.assertEqual(
                str(generation / "safe-answer-index.sqlite3"),
                configured["index_path"],
            )
            self.assertNotIn(
                bootstrap.CROSS_DOCUMENT_TRUST_CONFIG_KEY, configured
            )
            self.assertEqual([], store.create_calls)
            self.assertGreaterEqual(len(store.read_calls), 1)

    def test_promotion_recovery_after_config_before_marker_revalidates_root(
        self,
    ) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_trust_config_marker_window",
            ROOT / "app" / "bootstrap.py",
        )
        trust_module = load_module(
            "runtime_bootstrap_trust_config_marker_module",
            ROOT / "app" / "semantic_graph_trust.py",
        )
        store = FakeSemanticGraphTrustStore()
        trust_module.KeychainTrustStore = lambda: store
        with TemporaryDirectory() as temporary:
            generation, build_id, registration, state, output = (
                prepare_trust_recovery_fixture(
                    bootstrap,
                    Path(temporary),
                    promotion_enabled=True,
                )
            )
            with mock.patch.object(
                bootstrap,
                "_semantic_graph_trust_module",
                return_value=trust_module,
            ):
                locator = bootstrap._publish_semantic_graph_trust_root(
                    generation, build_id, registration, state
                )
                configured = bootstrap.load_json(bootstrap.CONFIG)
                configured.update({
                    "index_path": str(output),
                    bootstrap.CROSS_DOCUMENT_STORAGE_CONFIG_KEY: registration,
                    bootstrap.CROSS_DOCUMENT_TRUST_CONFIG_KEY: locator,
                })
                bootstrap.atomic_json(bootstrap.CONFIG, configured)
                # Marker and STATE deliberately remain at their pending values.
                with mock.patch.object(
                    bootstrap, "_pid_is_alive", return_value=False
                ):
                    report = bootstrap.recover_interrupted_build()

            self.assertEqual(
                "recovered_published_observers", report["status"]
            )
            self.assertEqual("verified_complete", report["storage_action"])
            recovered = bootstrap.load_json(bootstrap.CONFIG)
            self.assertEqual(locator, recovered[
                bootstrap.CROSS_DOCUMENT_TRUST_CONFIG_KEY
            ])
            self.assertEqual(str(output), recovered["index_path"])
            self.assertGreaterEqual(len(store.read_calls), 2)

    def test_promotion_false_survives_storage_recovery_without_trust_access(
        self,
    ) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_trust_disabled_recovery",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            generation, _build_id, _registration, _state, output = (
                prepare_trust_recovery_fixture(
                    bootstrap,
                    Path(temporary),
                    promotion_enabled=False,
                )
            )
            with (
                mock.patch.object(
                    bootstrap,
                    "_recover_semantic_graph_trust_root",
                ) as trust_recovery,
                mock.patch.object(
                    bootstrap, "_pid_is_alive", return_value=False
                ),
            ):
                report = bootstrap.recover_interrupted_build()

            self.assertEqual(
                "completed_pointer_switch", report["storage_action"]
            )
            trust_recovery.assert_not_called()
            configured = bootstrap.load_json(bootstrap.CONFIG)
            self.assertEqual(str(output), configured["index_path"])
            self.assertFalse(
                configured[bootstrap.CROSS_DOCUMENT_ANSWER_PROMOTION_FLAG]
            )
            self.assertNotIn(
                bootstrap.CROSS_DOCUMENT_TRUST_CONFIG_KEY, configured
            )
            self.assertFalse(
                bootstrap.load_json(bootstrap.STATE)[
                    "cross_document_semantic_graph_answer_promotion_enabled"
                ]
            )
            self.assertFalse(
                bootstrap.load_json(
                    generation / bootstrap.GENERATION_MARKER
                )[
                    "cross_document_semantic_graph_answer_promotion_enabled"
                ]
            )

    def test_bootstrap_independently_compares_stored_rows_with_shadow(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_storage_independent_rows",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            generation, _pending = prepare_published_generation(
                bootstrap, Path(temporary)
            )
            state, output = prepare_completed_semantic_storage(
                bootstrap,
                generation,
                build_id="build-published-shadow",
            )
            connection = sqlite3.connect(output)
            try:
                connection.execute(
                    "UPDATE semantic_graph_nodes SET properties_json = ? "
                    "WHERE node_id = (SELECT node_id FROM semantic_graph_nodes "
                    "ORDER BY node_id LIMIT 1)",
                    ('{"independent_check":"must_reject"}',),
                )
                connection.commit()
            finally:
                connection.close()
            state["output"]["sqlite_sha256"] = bootstrap.sha256_file(output)
            state_path = (
                output.parent / bootstrap.CROSS_DOCUMENT_STORAGE_RUN_STATE
            )
            bootstrap.atomic_json(state_path, state)

            with mock.patch.object(
                bootstrap,
                "_independently_validate_semantic_storage",
                return_value=state,
            ):
                with self.assertRaisesRegex(
                    ValueError, "semantic_storage_independent_row_mismatch"
                ):
                    bootstrap._semantic_storage_registration(
                        generation,
                        state,
                        semantic=generation / "02-semantic",
                        security=generation / "03-security",
                        expected_build_id="build-published-shadow",
                    )

    def test_ready_state_recovers_when_publish_marker_write_was_interrupted(
        self,
    ) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_storage_marker_window",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            generation, _pending = prepare_published_generation(
                bootstrap, Path(temporary)
            )
            state, output = prepare_completed_semantic_storage(
                bootstrap,
                generation,
                build_id="build-published-shadow",
            )
            marker_path = generation / bootstrap.GENERATION_MARKER
            marker = bootstrap.load_json(marker_path)
            marker["status"] = "building"
            marker.pop("published_at", None)
            bootstrap.atomic_json(marker_path, marker)

            with mock.patch.object(
                bootstrap, "_pid_is_alive", return_value=False
            ):
                report = bootstrap.recover_interrupted_build()

            self.assertEqual("complete", report["storage_status"])
            self.assertEqual(str(output), bootstrap.load_json(bootstrap.CONFIG)["index_path"])
            self.assertEqual(
                "published", bootstrap.load_json(marker_path)["status"]
            )

    def test_ready_state_reconstructs_missing_marker_before_real_promotion(
        self,
    ) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_storage_missing_marker",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            generation, _pending = prepare_published_generation(
                bootstrap, Path(temporary)
            )
            state, output = prepare_completed_semantic_storage(
                bootstrap,
                generation,
                build_id="build-published-shadow",
            )
            marker_path = generation / bootstrap.GENERATION_MARKER
            marker_path.unlink()

            with mock.patch.object(
                bootstrap, "_pid_is_alive", return_value=False
            ):
                report = bootstrap.recover_interrupted_build()

            self.assertEqual(
                "recovered_published_observers", report["status"]
            )
            self.assertEqual("complete", report["storage_status"])
            self.assertEqual(
                "completed_pointer_switch", report["storage_action"]
            )
            marker = bootstrap.load_json(marker_path)
            self.assertTrue(marker["reconstructed_after_interruption"])
            self.assertEqual("published", marker["status"])
            self.assertEqual("build-published-shadow", marker["build_id"])
            self.assertEqual(str(output), marker["index_path"])
            self.assertEqual(
                state,
                bootstrap.load_json(bootstrap.STATE)[
                    "cross_document_semantic_graph_storage"
                ],
            )

    def test_ready_state_corrupt_marker_and_tampered_copy_roll_back(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_storage_corrupt_marker",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            generation, _pending = prepare_published_generation(
                bootstrap, Path(temporary)
            )
            _state, output = prepare_completed_semantic_storage(
                bootstrap,
                generation,
                build_id="build-published-shadow",
            )
            configured = bootstrap.load_json(bootstrap.CONFIG)
            configured["index_path"] = str(output)
            configured[bootstrap.CROSS_DOCUMENT_STORAGE_CONFIG_KEY] = {
                "database_path": str(output),
            }
            bootstrap.atomic_json(bootstrap.CONFIG, configured)
            marker_path = generation / bootstrap.GENERATION_MARKER
            marker_path.write_text('{"status":', encoding="utf-8")
            with output.open("ab") as handle:
                handle.write(b"tamper-after-validation")
            base_index = generation / "safe-answer-index.sqlite3"
            base_hash = bootstrap.sha256_file(base_index)

            with mock.patch.object(
                bootstrap, "_pid_is_alive", return_value=False
            ):
                report = bootstrap.recover_interrupted_build()

            self.assertEqual("held", report["storage_status"])
            self.assertEqual("rolled_back_to_base", report["storage_action"])
            recovered = bootstrap.load_json(bootstrap.CONFIG)
            self.assertEqual(str(base_index), recovered["index_path"])
            self.assertEqual(base_hash, bootstrap.sha256_file(base_index))
            marker = bootstrap.load_json(marker_path)
            self.assertTrue(marker["reconstructed_after_interruption"])
            self.assertEqual(
                "semantic_storage_invalid_after_interruption",
                marker["cross_document_semantic_graph_storage"]["reason_code"],
            )

    def test_storage_recovery_refuses_symlink_rollback_anchor(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_storage_base_symlink",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            generation, _pending = prepare_published_generation(
                bootstrap, Path(temporary)
            )
            base_index = generation / "safe-answer-index.sqlite3"
            real_index = generation / "real-safe-answer-index.sqlite3"
            base_index.rename(real_index)
            base_index.symlink_to(real_index.name)
            candidate = generation / (
                bootstrap.CROSS_DOCUMENT_STORAGE_DIR + ".building"
            )
            candidate.mkdir()

            with mock.patch.object(
                bootstrap, "_pid_is_alive", return_value=False
            ):
                report = bootstrap.recover_interrupted_build()

            self.assertEqual("invalid_active_index_pointer", report["status"])
            self.assertTrue(candidate.exists())
            self.assertTrue(base_index.is_symlink())
            runtime_state = bootstrap.load_json(bootstrap.STATE)
            self.assertEqual("error", runtime_state["phase"])
            self.assertEqual(
                "active_index_pointer_boundary_invalid",
                runtime_state["error"],
            )

    def test_recovery_removes_partial_storage_and_keeps_base_index(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_storage_partial", ROOT / "app" / "bootstrap.py"
        )
        with TemporaryDirectory() as temporary:
            generation, _pending = prepare_published_generation(
                bootstrap, Path(temporary)
            )
            replace_with_ready_answer_index(
                generation / "safe-answer-index.sqlite3"
            )
            candidate = generation / (
                bootstrap.CROSS_DOCUMENT_STORAGE_DIR + ".building"
            )
            candidate.mkdir()
            (candidate / "partial.sqlite3").write_bytes(b"partial")
            base_index = generation / "safe-answer-index.sqlite3"
            base_hash = bootstrap.sha256_file(base_index)

            with mock.patch.object(
                bootstrap, "_pid_is_alive", return_value=False
            ):
                report = bootstrap.recover_interrupted_build()

            self.assertEqual("held", report["storage_status"])
            self.assertFalse(candidate.exists())
            configured = bootstrap.load_json(bootstrap.CONFIG)
            self.assertEqual(str(base_index), configured["index_path"])
            self.assertEqual(base_hash, bootstrap.sha256_file(base_index))

    def test_recovery_rolls_back_tampered_storage_to_immutable_base(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_storage_tamper", ROOT / "app" / "bootstrap.py"
        )
        with TemporaryDirectory() as temporary:
            generation, _pending = prepare_published_generation(
                bootstrap, Path(temporary)
            )
            _state, output = prepare_completed_semantic_storage(
                bootstrap,
                generation,
                build_id="build-published-shadow",
            )
            state_path = (
                output.parent / bootstrap.CROSS_DOCUMENT_STORAGE_RUN_STATE
            )
            pre_tamper = bootstrap._independently_validate_semantic_storage(
                generation=generation,
                semantic=generation / "02-semantic",
                security=generation / "03-security",
                base_index=generation / "safe-answer-index.sqlite3",
                index=output,
                state_path=state_path,
                expected_build_id="build-published-shadow",
            )
            self.assertEqual("complete", pre_tamper["status"])
            configured = bootstrap.load_json(bootstrap.CONFIG)
            configured["index_path"] = str(output)
            configured[bootstrap.CROSS_DOCUMENT_STORAGE_CONFIG_KEY] = {
                "database_path": str(output),
            }
            bootstrap.atomic_json(bootstrap.CONFIG, configured)
            with output.open("ab") as handle:
                handle.write(b"tamper")
            base_index = generation / "safe-answer-index.sqlite3"
            base_hash = bootstrap.sha256_file(base_index)

            with mock.patch.object(
                bootstrap, "_pid_is_alive", return_value=False
            ):
                report = bootstrap.recover_interrupted_build()

            self.assertEqual("held", report["storage_status"])
            self.assertEqual("rolled_back_to_base", report["storage_action"])
            recovered = bootstrap.load_json(bootstrap.CONFIG)
            self.assertEqual(str(base_index), recovered["index_path"])
            self.assertNotIn(
                bootstrap.CROSS_DOCUMENT_STORAGE_CONFIG_KEY, recovered
            )
            self.assertEqual(base_hash, bootstrap.sha256_file(base_index))
            recovered_state = bootstrap.load_json(bootstrap.STATE)[
                "cross_document_semantic_graph_storage"
            ]
            self.assertEqual(
                "semantic_storage_invalid_after_interruption",
                recovered_state["reason_code"],
            )
            self.assertIn(
                "semantic_storage_registration_invalid",
                recovered_state["error"],
            )

    def test_recovery_never_rolls_back_to_a_replaced_valid_base_index(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_storage_replaced_base",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            generation, _pending = prepare_published_generation(
                bootstrap, Path(temporary)
            )
            _state, output = prepare_completed_semantic_storage(
                bootstrap,
                generation,
                build_id="build-published-shadow",
            )
            with mock.patch.object(
                bootstrap, "_pid_is_alive", return_value=False
            ):
                first = bootstrap.recover_interrupted_build()
            self.assertEqual("completed_pointer_switch", first["storage_action"])

            base_index = generation / "safe-answer-index.sqlite3"
            trusted_hash = bootstrap.sha256_file(base_index)
            replace_with_ready_answer_index(base_index)
            self.assertNotEqual(trusted_hash, bootstrap.sha256_file(base_index))
            self.assertTrue(
                validated_answer_policy(base_index)["eligible_evidence_ids"]
            )

            with mock.patch.object(
                bootstrap, "_pid_is_alive", return_value=False
            ):
                report = bootstrap.recover_interrupted_build()

            self.assertEqual(
                "semantic_storage_recovery_failed_closed", report["status"]
            )
            self.assertEqual("base_index_invalid", report["storage_action"])
            configured = bootstrap.load_json(bootstrap.CONFIG)
            self.assertEqual(str(output), configured["index_path"])
            self.assertNotEqual(str(base_index), configured["index_path"])
            recovered_state = bootstrap.load_json(bootstrap.STATE)
            self.assertEqual("error", recovered_state["phase"])
            self.assertEqual(
                "semantic_storage_base_index_invalid",
                recovered_state["error"],
            )
            self.assertNotIn(
                "semantic_storage_recovered_after_interruption",
                recovered_state,
            )

    def test_explicit_storage_rollback_flag_restores_base_without_deleting_copy(
        self,
    ) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_storage_rollback", ROOT / "app" / "bootstrap.py"
        )
        with TemporaryDirectory() as temporary:
            generation, _pending = prepare_published_generation(
                bootstrap, Path(temporary)
            )
            _state, output = prepare_completed_semantic_storage(
                bootstrap,
                generation,
                build_id="build-published-shadow",
            )
            configured = bootstrap.load_json(bootstrap.CONFIG)
            configured[bootstrap.CROSS_DOCUMENT_STORAGE_FLAG] = False
            configured["index_path"] = str(output)
            configured[bootstrap.CROSS_DOCUMENT_STORAGE_CONFIG_KEY] = {
                "database_path": str(output),
            }
            bootstrap.atomic_json(bootstrap.CONFIG, configured)

            with mock.patch.object(
                bootstrap, "_pid_is_alive", return_value=False
            ):
                report = bootstrap.recover_interrupted_build()

            self.assertEqual("disabled", report["storage_status"])
            self.assertEqual("rolled_back_to_base", report["storage_action"])
            recovered = bootstrap.load_json(bootstrap.CONFIG)
            self.assertEqual(
                str(generation / "safe-answer-index.sqlite3"),
                recovered["index_path"],
            )
            self.assertFalse(
                recovered[bootstrap.CROSS_DOCUMENT_STORAGE_FLAG]
            )
            self.assertNotIn(
                bootstrap.CROSS_DOCUMENT_STORAGE_CONFIG_KEY, recovered
            )
            self.assertTrue(output.is_file())
            marker_path = generation / bootstrap.GENERATION_MARKER
            stable_files = {
                "config": bootstrap.CONFIG.read_bytes(),
                "state": bootstrap.STATE.read_bytes(),
                "marker": marker_path.read_bytes(),
            }

            with mock.patch.object(
                bootstrap, "_pid_is_alive", return_value=False
            ):
                second_report = bootstrap.recover_interrupted_build()

            self.assertEqual("unchanged", second_report["status"])
            self.assertEqual(
                "disabled_steady", second_report["storage_action"]
            )
            self.assertEqual(stable_files["config"], bootstrap.CONFIG.read_bytes())
            self.assertEqual(stable_files["state"], bootstrap.STATE.read_bytes())
            self.assertEqual(stable_files["marker"], marker_path.read_bytes())

    def test_llm_extraction_flag_is_derived_from_layer1_provenance(self) -> None:
        builder = load_module(
            "runtime_builder", ENGINE / "build_adaptive_semantic_graph.py"
        )
        validator = load_module(
            "runtime_validator", ENGINE / "validate_adaptive_semantic_graph.py"
        )
        records = [{
            "evidence_id": "ev_local_vlm",
            "provenance": {
                "extraction_method": "local_vlm_unlocated_transcript_provisional"
            },
            "native_properties": {"runner": "ollama_loopback_chat"},
        }]
        expected = {
            "used": True,
            "evidence_count": 1,
            "methods": {"local_vlm_unlocated_transcript_provisional": 1},
        }
        self.assertEqual(builder.derive_llm_extraction(records), expected)
        self.assertEqual(validator.derive_llm_extraction(records), expected)
        self.assertEqual(
            builder.derive_llm_extraction([{
                "provenance": {"extraction_method": "dual_local_ocr_consensus"},
                "native_properties": {},
            }]),
            {"used": False, "evidence_count": 0, "methods": {}},
        )

    def test_clean_first_build_rebuilds_semantic_after_gemma_pull(self) -> None:
        bootstrap = load_module("runtime_bootstrap_build", ROOT / "app" / "bootstrap.py")
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            support = base / "support"
            source = base / "source"
            source.mkdir()
            (source / "scan.png").write_bytes(b"not-read-by-this-mock")
            workspace = support / "data"
            config_path = support / "config.json"
            state_path = support / "state.json"
            bootstrap.SUPPORT = support
            bootstrap.CONFIG = config_path
            bootstrap.STATE = state_path
            bootstrap.atomic_json(config_path, {
                "source_root": str(source),
                "workspace": str(workspace),
                "embedding_model": "embeddinggemma:latest",
                "answer_model": "gemma4:12b",
                "audit_model": "gemma4:12b",
                "semantic_graph_shadow_path": "/stale/shadow.sqlite3",
            })

            semantic_calls: list[Path] = []

            def fake_semantic(_source, _paths, semantic, security, _log):
                semantic_calls.append(semantic)
                return prepare_reader_contract_semantic_fixture(
                    bootstrap,
                    semantic,
                    security,
                    manifest_paths=["scan.png"],
                    status="complete_with_limits",
                    limitations={"partial_documents": 1},
                    llm_used_for_extraction=len(semantic_calls) == 2,
                )

            def fake_run(command, _log):
                if any(str(value).endswith("build_path_graph.py") for value in command):
                    output = Path(command[command.index("--output-dir") + 1])
                    (output / "path-source-inventory.jsonl").write_text(
                        json.dumps({
                            "kind": "file",
                            "relative_path": "scan.png",
                            "read_status": "observed",
                        }) + "\n",
                        encoding="utf-8",
                    )
                if any(str(value).endswith("build_local_semantic_index.py") for value in command):
                    Path(command[command.index("--output") + 1]).write_bytes(b"index")

            identifiers = [
                types.SimpleNamespace(hex="1" * 32),
                types.SimpleNamespace(hex="2" * 32),
            ]
            with (
                mock.patch.object(bootstrap.uuid, "uuid4", side_effect=identifiers),
                mock.patch.object(bootstrap, "run", side_effect=fake_run),
                mock.patch.object(
                    bootstrap, "run_semantic_pipeline", side_effect=fake_semantic
                ),
                mock.patch.object(
                    bootstrap,
                    "ensure_models",
                    return_value=[bootstrap.IMAGE_FALLBACK_MODEL],
                ),
                mock.patch.object(
                    bootstrap,
                    "local_model_available",
                    side_effect=[False, True],
                ),
                mock.patch.object(
                    bootstrap,
                    "run_cross_document_semantic_graph_shadow",
                    return_value={
                        "status": "complete",
                        "shadow_only": True,
                        "used_for_answers": False,
                    },
                ) as shadow,
            ):
                bootstrap.build_index()

            self.assertEqual(len(semantic_calls), 2)
            self.assertEqual(semantic_calls[0].name, "02-semantic")
            self.assertEqual(semantic_calls[1].name, "02-semantic-model-ready")
            published = bootstrap.load_json(config_path)
            self.assertTrue(published["semantic_path"].endswith("02-semantic-model-ready"))
            self.assertTrue(published["security_path"].endswith("03-security-model-ready"))
            self.assertTrue(Path(published["index_path"]).is_file())
            self.assertTrue(published[bootstrap.CROSS_DOCUMENT_SHADOW_FLAG])
            published_marker = bootstrap.load_json(
                workspace
                / "generations"
                / published["active_generation"]
                / bootstrap.GENERATION_MARKER
            )
            self.assertEqual(
                bootstrap.BUILD_EXECUTION_LEASE_VERSION,
                published_marker[
                    bootstrap.BUILD_EXECUTION_LEASE_VERSION_KEY
                ],
            )
            runtime_state = bootstrap.load_json(state_path)
            self.assertEqual(runtime_state["phase"], "ready_with_limits")
            self.assertEqual(
                "complete",
                runtime_state["cross_document_semantic_graph_shadow"]["status"],
            )
            shadow.assert_called_once()
            shadow_args = shadow.call_args.args
            self.assertEqual("02-semantic-model-ready", shadow_args[1].name)
            self.assertEqual("03-security-model-ready", shadow_args[2].name)

    def test_dead_build_owner_removes_only_marked_unpublished_generation(self) -> None:
        bootstrap = load_module("runtime_bootstrap_recovery", ROOT / "app" / "bootstrap.py")
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            support = base / "support"
            workspace = support / "data"
            generations = workspace / "generations"
            orphan_name = "generation-" + "a" * 32
            orphan = generations / orphan_name
            orphan.mkdir(parents=True)
            bootstrap.SUPPORT = support
            bootstrap.CONFIG = support / "config.json"
            bootstrap.STATE = support / "state.json"
            bootstrap.atomic_json(bootstrap.CONFIG, {"workspace": str(workspace)})
            bootstrap.atomic_json(orphan / bootstrap.GENERATION_MARKER, {
                "status": "building",
                "generation": orphan_name,
                "owner_pid": 99999999,
            })
            bootstrap.atomic_json(bootstrap.STATE, {
                "phase": "building",
                "generation": orphan_name,
                "owner_pid": 99999999,
            })

            with mock.patch.object(bootstrap, "_pid_is_alive", return_value=False):
                result = bootstrap.recover_interrupted_build()

            self.assertFalse(orphan.exists())
            self.assertIn(orphan_name, result["removed"])
            recovered_state = bootstrap.load_json(bootstrap.STATE)
            self.assertEqual(recovered_state["phase"], "error")
            self.assertEqual(recovered_state["recovery_action"], "retry_build")

    def test_legacy_live_build_owner_remains_untouched(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_legacy_live_owner",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            support = base / "support"
            workspace = support / "data"
            name = "generation-" + "c" * 32
            generation = workspace / "generations" / name
            generation.mkdir(parents=True)
            bootstrap.SUPPORT = support
            bootstrap.CONFIG = support / "config.json"
            bootstrap.STATE = support / "state.json"
            bootstrap.atomic_json(
                bootstrap.CONFIG,
                {"workspace": str(workspace)},
            )
            bootstrap.atomic_json(
                generation / bootstrap.GENERATION_MARKER,
                {
                    "status": "building",
                    "generation": name,
                    "owner_pid": 99_999_999,
                },
            )
            original_state = {
                "phase": "building",
                "generation": name,
                "owner_pid": 99_999_999,
            }
            bootstrap.atomic_json(bootstrap.STATE, original_state)

            with mock.patch.object(
                bootstrap,
                "_pid_is_alive",
                return_value=True,
            ):
                result = bootstrap.recover_interrupted_build()

            self.assertEqual("active", result["status"])
            self.assertTrue(generation.is_dir())
            self.assertEqual(
                original_state,
                bootstrap.load_json(bootstrap.STATE),
            )

    def test_legacy_reused_build_owner_pid_is_recovered(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_legacy_reused_owner",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            support = base / "support"
            workspace = support / "data"
            name = "generation-" + "e" * 32
            generation = workspace / "generations" / name
            generation.mkdir(parents=True)
            bootstrap.SUPPORT = support
            bootstrap.CONFIG = support / "config.json"
            bootstrap.STATE = support / "state.json"
            started_at = "2026-09-04T10:00:00+09:00"
            bootstrap.atomic_json(
                bootstrap.CONFIG,
                {"workspace": str(workspace)},
            )
            bootstrap.atomic_json(
                generation / bootstrap.GENERATION_MARKER,
                {
                    "status": "building",
                    "generation": name,
                    "owner_pid": 99_999_999,
                    "started_at": started_at,
                },
            )
            bootstrap.atomic_json(
                bootstrap.STATE,
                {
                    "phase": "building",
                    "generation": name,
                    "owner_pid": 99_999_999,
                    "started_at": started_at,
                },
            )
            reused_process_start = (
                bootstrap.datetime.fromisoformat(started_at).timestamp() + 60
            )

            with (
                mock.patch.object(
                    bootstrap,
                    "_pid_is_alive",
                    return_value=True,
                ),
                mock.patch.object(
                    bootstrap,
                    "_legacy_process_identity",
                    return_value={
                        "started_at": reused_process_start,
                        "state": "S",
                        "command": "/usr/bin/unrelated-process",
                    },
                ),
            ):
                result = bootstrap.recover_interrupted_build()

            self.assertEqual(
                "interrupted_build_failed_closed",
                result["status"],
            )
            self.assertFalse(generation.exists())
            recovered_state = bootstrap.load_json(bootstrap.STATE)
            self.assertEqual("error", recovered_state["phase"])
            self.assertEqual("retry_build", recovered_state["recovery_action"])

    def test_legacy_zombie_build_owner_is_not_live(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_legacy_zombie_owner",
            ROOT / "app" / "bootstrap.py",
        )
        started_at = "2026-09-04T10:00:00+09:00"
        with (
            mock.patch.object(
                bootstrap,
                "_pid_is_alive",
                return_value=True,
            ),
            mock.patch.object(
                bootstrap,
                "_legacy_process_identity",
                return_value={
                    "started_at": (
                        bootstrap.datetime.fromisoformat(
                            started_at
                        ).timestamp()
                        - 60
                    ),
                    "state": "Z+",
                    "command": "[Python] <defunct>",
                },
            ),
        ):
            self.assertFalse(
                bootstrap._legacy_build_owner_is_live(
                    99_999_999,
                    {"started_at": started_at},
                )
            )

    def test_legacy_pending_observer_recovers_after_pid_reuse(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_legacy_observer_reused_pid",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            generation, _pending = prepare_published_generation(
                bootstrap,
                Path(temporary),
            )
            marker_path = generation / bootstrap.GENERATION_MARKER
            marker = bootstrap.load_json(marker_path)
            started_at = "2026-09-04T10:00:00+09:00"
            marker["started_at"] = started_at
            bootstrap.atomic_json(marker_path, marker)
            reused_process_start = (
                bootstrap.datetime.fromisoformat(started_at).timestamp() + 60
            )

            with (
                mock.patch.object(
                    bootstrap,
                    "_pid_is_alive",
                    return_value=True,
                ),
                mock.patch.object(
                    bootstrap,
                    "_legacy_process_identity",
                    return_value={
                        "started_at": reused_process_start,
                        "state": "S",
                        "command": "/usr/bin/unrelated-process",
                    },
                ),
            ):
                result = bootstrap.recover_interrupted_build()

            self.assertEqual("recovered_published_shadow", result["status"])
            recovered_state = bootstrap.load_json(bootstrap.STATE)
            self.assertEqual("ready", recovered_state["phase"])
            self.assertEqual(
                "held",
                recovered_state[
                    "cross_document_semantic_graph_shadow"
                ]["status"],
            )

    def test_legacy_process_identity_parses_bounded_ps_record(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_legacy_process_identity",
            ROOT / "app" / "bootstrap.py",
        )
        completed = subprocess.CompletedProcess(
            args=["/bin/ps"],
            returncode=0,
            stdout=(
                "Thu Sep  4 09:30:00 2026 S+ "
                "/usr/bin/python3 local_memory_server.py --port 8765\n"
            ),
            stderr="",
        )
        with mock.patch.object(
            bootstrap.subprocess,
            "run",
            return_value=completed,
        ) as run:
            identity = bootstrap._legacy_process_identity(12345)
        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertEqual("S+", identity["state"])
        self.assertIn("local_memory_server.py", identity["command"])
        self.assertIsInstance(identity["started_at"], float)
        self.assertEqual("C", run.call_args.kwargs["env"]["LC_ALL"])
        self.assertEqual(1, run.call_args.kwargs["timeout"])

    def test_lease_managed_build_ignores_reused_live_pid(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_lease_managed_reused_pid",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            support = base / "support"
            workspace = support / "data"
            name = "generation-" + "d" * 32
            generation = workspace / "generations" / name
            generation.mkdir(parents=True)
            bootstrap.SUPPORT = support
            bootstrap.CONFIG = support / "config.json"
            bootstrap.STATE = support / "state.json"
            lease_binding = {
                bootstrap.BUILD_EXECUTION_LEASE_VERSION_KEY: (
                    bootstrap.BUILD_EXECUTION_LEASE_VERSION
                )
            }
            bootstrap.atomic_json(
                bootstrap.CONFIG,
                {"workspace": str(workspace)},
            )
            bootstrap.atomic_json(
                generation / bootstrap.GENERATION_MARKER,
                {
                    "status": "building",
                    "generation": name,
                    "owner_pid": 99_999_999,
                    **lease_binding,
                },
            )
            bootstrap.atomic_json(
                bootstrap.STATE,
                {
                    "phase": "building",
                    "generation": name,
                    "owner_pid": 99_999_999,
                    **lease_binding,
                },
            )

            with mock.patch.object(
                bootstrap,
                "_pid_is_alive",
                return_value=True,
            ):
                result = bootstrap.recover_interrupted_build()

            self.assertEqual(
                "interrupted_build_failed_closed",
                result["status"],
            )
            self.assertFalse(generation.exists())
            recovered_state = bootstrap.load_json(bootstrap.STATE)
            self.assertEqual("error", recovered_state["phase"])
            self.assertEqual("retry_build", recovered_state["recovery_action"])

    def test_lease_managed_pending_observer_ignores_reused_live_pid(
        self,
    ) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_observer_reused_pid",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            generation, _pending = prepare_published_generation(
                bootstrap,
                Path(temporary),
            )
            marker_path = generation / bootstrap.GENERATION_MARKER
            marker = bootstrap.load_json(marker_path)
            marker[bootstrap.BUILD_EXECUTION_LEASE_VERSION_KEY] = (
                bootstrap.BUILD_EXECUTION_LEASE_VERSION
            )
            bootstrap.atomic_json(marker_path, marker)

            with mock.patch.object(
                bootstrap,
                "_pid_is_alive",
                return_value=True,
            ):
                result = bootstrap.recover_interrupted_build()

            self.assertEqual("recovered_published_shadow", result["status"])
            recovered_state = bootstrap.load_json(bootstrap.STATE)
            self.assertEqual("ready", recovered_state["phase"])
            self.assertEqual(
                "held",
                recovered_state[
                    "cross_document_semantic_graph_shadow"
                ]["status"],
            )

    def test_dead_owner_restores_generation_already_atomically_published(self) -> None:
        bootstrap = load_module("runtime_bootstrap_published", ROOT / "app" / "bootstrap.py")
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            support = base / "support"
            workspace = support / "data"
            name = "generation-" + "b" * 32
            generation = workspace / "generations" / name
            paths = generation / "01-path"
            semantic = generation / "02-semantic"
            security = generation / "03-security"
            for path in (paths, semantic, security):
                path.mkdir(parents=True)
            index = generation / "safe-answer-index.sqlite3"
            index.write_bytes(b"published-index")
            bootstrap.SUPPORT = support
            bootstrap.CONFIG = support / "config.json"
            bootstrap.STATE = support / "state.json"
            bootstrap.atomic_json(semantic / "adaptive-reader-state.json", {
                "status": "complete",
                "limitations": {},
            })
            bootstrap.atomic_json(generation / bootstrap.GENERATION_MARKER, {
                "status": "building",
                "generation": name,
                "build_id": "build-published",
                "owner_pid": 99999999,
            })
            bootstrap.atomic_json(bootstrap.CONFIG, {
                "workspace": str(workspace),
                "active_generation": name,
                "path_graph_path": str(paths),
                "semantic_path": str(semantic),
                "security_path": str(security),
                "index_path": str(index),
            })
            bootstrap.atomic_json(bootstrap.STATE, {
                "phase": "building",
                "generation": name,
                "build_id": "build-published",
                "owner_pid": 99999999,
            })

            with mock.patch.object(bootstrap, "_pid_is_alive", return_value=False):
                result = bootstrap.recover_interrupted_build()

            self.assertEqual(result["status"], "recovered_published")
            self.assertEqual(result["shadow_status"], "held")
            self.assertTrue(generation.is_dir())
            recovered_state = bootstrap.load_json(bootstrap.STATE)
            self.assertEqual(recovered_state["phase"], "ready")
            self.assertTrue(recovered_state["recovered_after_interruption"])
            self.assertEqual(
                "held",
                recovered_state[
                    "cross_document_semantic_graph_shadow"
                ]["status"],
            )
            self.assertEqual(
                bootstrap.load_json(generation / bootstrap.GENERATION_MARKER)["status"],
                "published",
            )

    def test_building_state_with_published_pending_shadow_recovers_in_one_startup(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_shadow_publish_window_recovery",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            generation, _pending = prepare_published_generation(
                bootstrap, base
            )
            candidate = generation / (
                bootstrap.CROSS_DOCUMENT_SHADOW_DIR + ".building"
            )
            candidate.mkdir()
            bootstrap.atomic_json(bootstrap.STATE, {
                "phase": "building",
                "generation": generation.name,
                "build_id": "build-published-shadow",
                "owner_pid": 99_999_999,
            })

            with mock.patch.object(
                bootstrap, "_pid_is_alive", return_value=False
            ):
                report = bootstrap.recover_interrupted_build()

            self.assertEqual("recovered_published", report["status"])
            self.assertEqual("held", report["shadow_status"])
            self.assertFalse(candidate.exists())
            recovered_state = bootstrap.load_json(bootstrap.STATE)
            self.assertEqual("ready", recovered_state["phase"])
            self.assertEqual(
                "held",
                recovered_state[
                    "cross_document_semantic_graph_shadow"
                ]["status"],
            )

    def test_building_state_marker_build_id_mismatch_fails_closed(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_build_id_mismatch",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            generation, _pending = prepare_published_generation(
                bootstrap, Path(temporary)
            )
            bootstrap.atomic_json(bootstrap.STATE, {
                "phase": "building",
                "generation": generation.name,
                "build_id": "different-build-id",
                "owner_pid": 99_999_999,
            })
            base_index = generation / "safe-answer-index.sqlite3"

            with mock.patch.object(
                bootstrap, "_pid_is_alive", return_value=False
            ):
                report = bootstrap.recover_interrupted_build()

            self.assertEqual("interrupted_build_failed_closed", report["status"])
            self.assertTrue(generation.is_dir())
            self.assertEqual(
                str(base_index), bootstrap.load_json(bootstrap.CONFIG)["index_path"]
            )
            recovered_state = bootstrap.load_json(bootstrap.STATE)
            self.assertEqual("error", recovered_state["phase"])
            self.assertEqual("retry_build", recovered_state["recovery_action"])

    def test_building_state_never_recovers_with_cross_generation_pointer(
        self,
    ) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_building_cross_generation_pointer",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            generation, _pending = prepare_published_generation(
                bootstrap, Path(temporary)
            )
            other = generation.parent / ("generation-" + "f" * 32)
            other.mkdir()
            other_index = other / "safe-answer-index.sqlite3"
            replace_with_ready_answer_index(other_index)
            configured = bootstrap.load_json(bootstrap.CONFIG)
            configured["index_path"] = str(other_index)
            bootstrap.atomic_json(bootstrap.CONFIG, configured)
            bootstrap.atomic_json(bootstrap.STATE, {
                "phase": "building",
                "generation": generation.name,
                "build_id": "build-published-shadow",
                "owner_pid": 99_999_999,
            })

            with mock.patch.object(
                bootstrap, "_pid_is_alive", return_value=False
            ):
                report = bootstrap.recover_interrupted_build()

            self.assertEqual(
                "interrupted_build_failed_closed", report["status"]
            )
            recovered_state = bootstrap.load_json(bootstrap.STATE)
            self.assertEqual("error", recovered_state["phase"])
            self.assertEqual("retry_build", recovered_state["recovery_action"])

    def test_ready_state_rejects_active_generation_directory_symlink(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_active_generation_symlink",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            support = base / "support"
            workspace = support / "data"
            generations = workspace / "generations"
            real_generation = workspace / "real-generation"
            real_generation.mkdir(parents=True)
            name = "generation-" + "d" * 32
            generations.mkdir()
            generation_link = generations / name
            generation_link.symlink_to(real_generation, target_is_directory=True)
            bootstrap.SUPPORT = support
            bootstrap.CONFIG = support / "config.json"
            bootstrap.STATE = support / "state.json"
            bootstrap.atomic_json(bootstrap.CONFIG, {
                "workspace": str(workspace),
                "active_generation": name,
            })
            bootstrap.atomic_json(bootstrap.STATE, {
                "phase": "ready",
                "message": "索引の作成が完了しました。",
                "error": "",
            })

            report = bootstrap.recover_interrupted_build()

            self.assertEqual("invalid_active_generation", report["status"])
            self.assertTrue(generation_link.is_symlink())
            recovered_state = bootstrap.load_json(bootstrap.STATE)
            self.assertEqual("error", recovered_state["phase"])
            self.assertEqual(
                "active_generation_boundary_invalid", recovered_state["error"]
            )
            self.assertEqual("rebuild_index", recovered_state["recovery_action"])

    def test_ready_state_rejects_missing_active_generation(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_missing_active_generation",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            support = base / "support"
            workspace = support / "data"
            support.mkdir()
            index = base / "valid-answer-index.sqlite3"
            replace_with_ready_answer_index(index)
            bootstrap.SUPPORT = support
            bootstrap.CONFIG = support / "config.json"
            bootstrap.STATE = support / "state.json"
            bootstrap.atomic_json(bootstrap.CONFIG, {
                "workspace": str(workspace),
                "index_path": str(index),
            })
            bootstrap.atomic_json(bootstrap.STATE, {
                "phase": "ready",
                "message": "索引の作成が完了しました。",
                "error": "",
            })

            report = bootstrap.recover_interrupted_build()

            self.assertEqual("invalid_active_generation", report["status"])
            recovered_state = bootstrap.load_json(bootstrap.STATE)
            self.assertEqual("error", recovered_state["phase"])
            self.assertEqual(
                "active_generation_missing", recovered_state["error"]
            )

    def test_ready_state_rejects_cross_generation_index_pointer(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_cross_generation_pointer",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            generation, _pending = prepare_published_generation(bootstrap, base)
            replace_with_ready_answer_index(
                generation / "safe-answer-index.sqlite3"
            )
            other = (
                generation.parent / ("generation-" + "e" * 32)
            )
            other.mkdir()
            other_index = other / "safe-answer-index.sqlite3"
            replace_with_ready_answer_index(other_index)
            configured = bootstrap.load_json(bootstrap.CONFIG)
            configured["index_path"] = str(other_index)
            bootstrap.atomic_json(bootstrap.CONFIG, configured)
            bootstrap.atomic_json(bootstrap.STATE, {
                "phase": "ready",
                "message": "索引の作成が完了しました。",
                "error": "",
            })

            report = bootstrap.recover_interrupted_build()

            self.assertEqual("invalid_active_index_pointer", report["status"])
            recovered_state = bootstrap.load_json(bootstrap.STATE)
            self.assertEqual("error", recovered_state["phase"])
            self.assertEqual(
                "active_index_pointer_boundary_invalid",
                recovered_state["error"],
            )

    def test_ready_state_rejects_outside_and_symlink_index_pointers(self) -> None:
        for pointer_kind in ("outside", "symlink"):
            with self.subTest(pointer_kind=pointer_kind), TemporaryDirectory() as temporary:
                bootstrap = load_module(
                    f"runtime_bootstrap_{pointer_kind}_pointer",
                    ROOT / "app" / "bootstrap.py",
                )
                base = Path(temporary)
                generation, _pending = prepare_published_generation(
                    bootstrap, base
                )
                base_index = generation / "safe-answer-index.sqlite3"
                replace_with_ready_answer_index(base_index)
                configured = bootstrap.load_json(bootstrap.CONFIG)
                if pointer_kind == "outside":
                    pointer = base / "outside-answer-index.sqlite3"
                    replace_with_ready_answer_index(pointer)
                    configured["index_path"] = str(pointer)
                else:
                    real_index = generation / "real-answer-index.sqlite3"
                    base_index.rename(real_index)
                    base_index.symlink_to(real_index.name)
                    configured["index_path"] = str(base_index)
                bootstrap.atomic_json(bootstrap.CONFIG, configured)
                bootstrap.atomic_json(bootstrap.STATE, {
                    "phase": "ready",
                    "message": "索引の作成が完了しました。",
                    "error": "",
                })

                report = bootstrap.recover_interrupted_build()

                self.assertEqual(
                    "invalid_active_index_pointer", report["status"]
                )
                recovered_state = bootstrap.load_json(bootstrap.STATE)
                self.assertEqual("error", recovered_state["phase"])
                self.assertEqual(
                    "active_index_pointer_boundary_invalid",
                    recovered_state["error"],
                )

    def test_ready_state_rejects_changed_base_hash_without_storage_artifact(
        self,
    ) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_changed_active_base",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            generation, _pending = prepare_published_generation(
                bootstrap, Path(temporary)
            )
            base_index = generation / "safe-answer-index.sqlite3"
            replace_with_ready_answer_index(base_index)
            trusted_hash = bootstrap.sha256_file(base_index)
            configured = bootstrap.load_json(bootstrap.CONFIG)
            configured[bootstrap.BASE_ANSWER_INDEX_SHA256_KEY] = trusted_hash
            configured[bootstrap.CROSS_DOCUMENT_STORAGE_FLAG] = False
            bootstrap.atomic_json(bootstrap.CONFIG, configured)
            bootstrap.atomic_json(bootstrap.STATE, {
                "phase": "ready",
                "message": "索引の作成が完了しました。",
                "error": "",
            })
            connection = sqlite3.connect(base_index)
            try:
                connection.execute("PRAGMA user_version=1")
                connection.commit()
            finally:
                connection.close()
            self.assertNotEqual(trusted_hash, bootstrap.sha256_file(base_index))
            self.assertTrue(
                validated_answer_policy(base_index)["eligible_evidence_ids"]
            )

            report = bootstrap.recover_interrupted_build()

            self.assertEqual("active_index_hash_invalid", report["status"])
            recovered_state = bootstrap.load_json(bootstrap.STATE)
            self.assertEqual("error", recovered_state["phase"])
            self.assertEqual(
                "active_base_index_hash_mismatch", recovered_state["error"]
            )

    def test_ready_generation_recovers_interrupted_shadow_as_held(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_shadow_pending_recovery",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            generation, _pending = prepare_published_generation(
                bootstrap, base
            )
            candidate = generation / (
                bootstrap.CROSS_DOCUMENT_SHADOW_DIR + ".building"
            )
            candidate.mkdir()
            (candidate / "incomplete.sqlite3").write_bytes(b"incomplete")
            held_candidate = generation / (
                bootstrap.CROSS_DOCUMENT_SHADOW_DIR + ".held-building"
            )
            held_candidate.mkdir()
            (held_candidate / "partial-state.json").write_text(
                "{}\n", encoding="utf-8"
            )
            production_sentinel = generation / "production-sentinel.txt"
            production_sentinel.write_text("keep", encoding="utf-8")

            with mock.patch.object(
                bootstrap, "_pid_is_alive", return_value=False
            ):
                report = bootstrap.recover_interrupted_build()

            self.assertEqual("recovered_published_shadow", report["status"])
            self.assertEqual("held", report["shadow_status"])
            self.assertFalse(candidate.exists())
            self.assertFalse(held_candidate.exists())
            self.assertEqual(
                b"published-production-index",
                Path(bootstrap.load_json(bootstrap.CONFIG)["index_path"])
                .read_bytes(),
            )
            self.assertEqual(
                "keep", production_sentinel.read_text(encoding="utf-8")
            )
            final = generation / bootstrap.CROSS_DOCUMENT_SHADOW_DIR
            recovered_shadow = bootstrap.load_json(
                final / bootstrap.CROSS_DOCUMENT_SHADOW_RUN_STATE
            )
            self.assertEqual("held", recovered_shadow["status"])
            self.assertTrue(recovered_shadow["removed_incomplete_candidate"])
            runtime_state = bootstrap.load_json(bootstrap.STATE)
            self.assertEqual("ready", runtime_state["phase"])
            self.assertEqual(
                "held",
                runtime_state["cross_document_semantic_graph_shadow"]["status"],
            )

    def test_recovery_preserves_completed_shadow_if_state_update_was_interrupted(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_shadow_complete_recovery",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            generation, _pending = prepare_published_generation(
                bootstrap, base
            )
            final = generation / bootstrap.CROSS_DOCUMENT_SHADOW_DIR
            final.mkdir()
            graph = final / "semantic-graph.sqlite3"
            graph.write_bytes(b"completed-shadow")
            complete = {
                **bootstrap._shadow_run_base(
                    generation,
                    "build-published-shadow",
                    status="complete",
                    reason_code="none",
                    elapsed_ms=12,
                ),
                "external_network_used": False,
            }
            bootstrap.atomic_json(
                final / bootstrap.CROSS_DOCUMENT_SHADOW_RUN_STATE,
                complete,
            )

            with mock.patch.object(
                bootstrap, "_pid_is_alive", return_value=False
            ):
                report = bootstrap.recover_interrupted_build()

            self.assertEqual("recovered_published_shadow", report["status"])
            self.assertEqual("complete", report["shadow_status"])
            self.assertEqual(b"completed-shadow", graph.read_bytes())
            runtime_state = bootstrap.load_json(bootstrap.STATE)
            self.assertEqual("ready", runtime_state["phase"])
            self.assertEqual(
                complete,
                runtime_state["cross_document_semantic_graph_shadow"],
            )

    def test_cross_document_shadow_disabled_creates_no_artifact(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_shadow_disabled", ROOT / "app" / "bootstrap.py"
        )
        with TemporaryDirectory() as temporary:
            generation = Path(temporary) / ("generation-" + "c" * 32)
            generation.mkdir()
            semantic, security = prepare_shadow_inputs(bootstrap, generation)
            log = io.StringIO()
            with mock.patch.object(bootstrap, "run_shadow_command") as runner:
                state = bootstrap.run_cross_document_semantic_graph_shadow(
                    {bootstrap.CROSS_DOCUMENT_SHADOW_FLAG: False},
                    semantic,
                    security,
                    generation,
                    "build-disabled",
                    log,
                )

            runner.assert_not_called()
            self.assertEqual("disabled", state["status"])
            self.assertFalse(
                (generation / bootstrap.CROSS_DOCUMENT_SHADOW_DIR).exists()
            )
            self.assertFalse(state["used_for_index"])
            self.assertFalse(state["used_for_answers"])

    def test_cross_document_shadow_publishes_only_after_validation(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_shadow_success", ROOT / "app" / "bootstrap.py"
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            generation = base / ("generation-" + "d" * 32)
            generation.mkdir()
            semantic, security = prepare_shadow_inputs(bootstrap, generation)
            tools_dir = base / "tools"
            tools_dir.mkdir()
            for name in bootstrap.CROSS_DOCUMENT_SHADOW_TOOLS:
                (tools_dir / name).write_text(f"# {name}\n", encoding="utf-8")
            commands: list[list[str]] = []
            real_write_log = bootstrap._write_log

            def fail_only_after_shadow_publication(log, message):
                if "semantic graph shadow complete" in message:
                    raise OSError("completion log unavailable")
                return real_write_log(log, message)

            def fake_shadow_command(command, _log, _timeout):
                commands.append(command)
                if command[1].endswith("build_cross_document_semantic_graph.py"):
                    database = Path(command[command.index("--output") + 1])
                    state_path = Path(command[command.index("--state") + 1])
                    database.write_bytes(b"validated-shadow-database")
                    bootstrap.atomic_json(state_path, {"status": "complete"})
                    return
                validation_path = Path(command[command.index("--output") + 1])
                database = Path(command[command.index("--database") + 1])
                documents = Path(command[command.index("--documents") + 1])
                evidence = Path(command[command.index("--evidence") + 1])
                source_evidence = Path(
                    command[command.index("--source-evidence") + 1]
                )
                security_state = Path(
                    command[command.index("--security-state") + 1]
                )
                bootstrap.atomic_json(validation_path, {
                    "status": "complete",
                    "question_independent": True,
                    "external_network_used": False,
                    "graph_snapshot_id": "xkgs_" + "2" * 32,
                    "logical_snapshot_sha256": "3" * 64,
                    "sqlite_sha256": bootstrap.sha256_file(database),
                    "documents_input_sha256": bootstrap.sha256_file(documents),
                    "source_evidence_input_sha256": bootstrap.sha256_file(
                        source_evidence
                    ),
                    "evidence_input_sha256": bootstrap.sha256_file(evidence),
                    "content_security_state_sha256": bootstrap.sha256_file(
                        security_state
                    ),
                    "counts": {
                        "documents": 1,
                        "source_evidence": 1,
                        "nodes": 3,
                        "edges": 1,
                        "edge_evidence": 6,
                    },
                    "relation_type_counts": {"ASSIGNED_TO": 1},
                })

            with (
                mock.patch.object(
                    bootstrap,
                    "_cross_document_shadow_tools_dir",
                    return_value=tools_dir,
                ),
                mock.patch.object(
                    bootstrap,
                    "run_shadow_command",
                    side_effect=fake_shadow_command,
                ),
                mock.patch.object(
                    bootstrap,
                    "_write_log",
                    side_effect=fail_only_after_shadow_publication,
                ),
            ):
                state = bootstrap.run_cross_document_semantic_graph_shadow(
                    {},
                    semantic,
                    security,
                    generation,
                    "build-success",
                    io.StringIO(),
                )

            final = generation / bootstrap.CROSS_DOCUMENT_SHADOW_DIR
            self.assertEqual("complete", state["status"], state)
            self.assertTrue(final.is_dir())
            self.assertFalse(
                (generation / (bootstrap.CROSS_DOCUMENT_SHADOW_DIR + ".building")).exists()
            )
            self.assertEqual(2, len(commands))
            builder_command = commands[0]
            self.assertEqual(
                semantic / "semantic-documents.jsonl",
                Path(builder_command[builder_command.index("--documents") + 1]),
            )
            self.assertEqual(
                security / "safe-answer-evidence.jsonl",
                Path(builder_command[builder_command.index("--evidence") + 1]),
            )
            self.assertNotEqual(
                final / "semantic-graph.sqlite3",
                generation / "safe-answer-index.sqlite3",
            )
            self.assertFalse(state["used_for_index"])
            self.assertFalse(state["used_for_answers"])
            self.assertEqual(
                state,
                bootstrap.load_json(final / bootstrap.CROSS_DOCUMENT_SHADOW_RUN_STATE),
            )

    def test_real_shadow_cli_pipeline_builds_validated_candidate(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_shadow_real_pipeline",
            ROOT / "app" / "bootstrap.py",
        )
        with TemporaryDirectory() as temporary:
            generation = Path(temporary) / ("generation-" + "c" * 32)
            generation.mkdir()
            semantic, security = prepare_real_shadow_inputs(generation)

            shadow_log = generation / "shadow-test.log"
            with shadow_log.open("w+", encoding="utf-8") as log:
                state = bootstrap.run_cross_document_semantic_graph_shadow(
                    {bootstrap.CROSS_DOCUMENT_SHADOW_FLAG: True},
                    semantic,
                    security,
                    generation,
                    "build-real-pipeline",
                    log,
                )

            final = generation / bootstrap.CROSS_DOCUMENT_SHADOW_DIR
            self.assertEqual(
                "complete",
                state["status"],
                {"state": state, "log": shadow_log.read_text(encoding="utf-8")},
            )
            self.assertEqual(3, state["counts"]["nodes"])
            self.assertEqual(1, state["counts"]["edges"])
            self.assertTrue((final / "semantic-graph.sqlite3").is_file())
            self.assertTrue(
                (final / "semantic-graph-validation.json").is_file()
            )
            self.assertFalse(
                (generation / (bootstrap.CROSS_DOCUMENT_SHADOW_DIR + ".building"))
                .exists()
            )

    def test_cross_document_shadow_failure_is_held_without_graph_publication(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_shadow_failure", ROOT / "app" / "bootstrap.py"
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            generation = base / ("generation-" + "e" * 32)
            generation.mkdir()
            semantic, security = prepare_shadow_inputs(bootstrap, generation)
            tools_dir = base / "tools"
            tools_dir.mkdir()
            for name in bootstrap.CROSS_DOCUMENT_SHADOW_TOOLS:
                (tools_dir / name).write_text(f"# {name}\n", encoding="utf-8")
            with (
                mock.patch.object(
                    bootstrap,
                    "_cross_document_shadow_tools_dir",
                    return_value=tools_dir,
                ),
                mock.patch.object(
                    bootstrap,
                    "run_shadow_command",
                    side_effect=RuntimeError("shadow boom"),
                ),
            ):
                state = bootstrap.run_cross_document_semantic_graph_shadow(
                    {bootstrap.CROSS_DOCUMENT_SHADOW_FLAG: True},
                    semantic,
                    security,
                    generation,
                    "build-held",
                    io.StringIO(),
                )

            final = generation / bootstrap.CROSS_DOCUMENT_SHADOW_DIR
            self.assertEqual("held", state["status"])
            self.assertEqual(
                "shadow_generation_failed_non_gating", state["reason_code"]
            )
            self.assertTrue(final.is_dir())
            self.assertFalse((final / "semantic-graph.sqlite3").exists())
            self.assertTrue(
                (final / bootstrap.CROSS_DOCUMENT_SHADOW_RUN_STATE).is_file()
            )

    def test_shadow_command_timeout_terminates_the_observer(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_shadow_timeout", ROOT / "app" / "bootstrap.py"
        )
        process = mock.Mock()
        process.wait.side_effect = [
            subprocess.TimeoutExpired(cmd=["python", "shadow.py"], timeout=1),
            0,
        ]
        with mock.patch.object(
            bootstrap.subprocess, "Popen", return_value=process
        ):
            with self.assertRaisesRegex(
                RuntimeError, "shadow_command_timeout:shadow.py"
            ):
                bootstrap.run_shadow_command(
                    ["python", "shadow.py"],
                    io.StringIO(),
                    timeout_seconds=1,
                )
        process.kill.assert_called_once_with()

    def test_shadow_orchestrator_exception_does_not_abort_main_index(self) -> None:
        bootstrap = load_module(
            "runtime_bootstrap_shadow_non_gating", ROOT / "app" / "bootstrap.py"
        )
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            support = base / "support"
            source = base / "source"
            source.mkdir()
            workspace = support / "data"
            bootstrap.SUPPORT = support
            bootstrap.CONFIG = support / "config.json"
            bootstrap.STATE = support / "state.json"
            bootstrap.atomic_json(bootstrap.CONFIG, {
                "source_root": str(source),
                "workspace": str(workspace),
                "embedding_model": "embeddinggemma:latest",
                "answer_model": "gemma4:12b",
                "audit_model": "gemma4:12b",
                bootstrap.CROSS_DOCUMENT_SHADOW_FLAG: True,
                "semantic_graph_shadow_path": "/stale/shadow.sqlite3",
            })

            def fake_semantic(_source, _paths, semantic, security, _log):
                return prepare_reader_contract_semantic_fixture(
                    bootstrap,
                    semantic,
                    security,
                )

            def fake_run(command, _log):
                if any(str(value).endswith("build_path_graph.py") for value in command):
                    output = Path(command[command.index("--output-dir") + 1])
                    (output / "path-source-inventory.jsonl").write_text(
                        "", encoding="utf-8"
                    )
                if any(
                    str(value).endswith("build_local_semantic_index.py")
                    for value in command
                ):
                    Path(command[command.index("--output") + 1]).write_bytes(
                        b"production-index"
                    )

            def fail_after_publication(*_args):
                published_during_shadow = bootstrap.load_json(bootstrap.CONFIG)
                state_during_shadow = bootstrap.load_json(bootstrap.STATE)
                self.assertTrue(
                    Path(published_during_shadow["index_path"]).is_file()
                )
                self.assertIn(
                    state_during_shadow["phase"],
                    {"ready", "ready_with_limits"},
                )
                self.assertEqual(
                    "pending",
                    state_during_shadow[
                        "cross_document_semantic_graph_shadow"
                    ]["status"],
                )
                raise RuntimeError("shadow implementation bug")

            real_write_log = bootstrap._write_log

            def fail_shadow_fallback_logs(log, message):
                if "semantic graph shadow" in message:
                    raise OSError("shadow log unavailable")
                return real_write_log(log, message)

            identifiers = [
                types.SimpleNamespace(hex="3" * 32),
                types.SimpleNamespace(hex="4" * 32),
            ]
            with (
                mock.patch.object(bootstrap.uuid, "uuid4", side_effect=identifiers),
                mock.patch.object(bootstrap, "run", side_effect=fake_run),
                mock.patch.object(
                    bootstrap,
                    "run_semantic_pipeline",
                    side_effect=fake_semantic,
                ),
                mock.patch.object(bootstrap, "ensure_models", return_value=[]),
                mock.patch.object(
                    bootstrap, "local_model_available", return_value=True
                ),
                mock.patch.object(
                    bootstrap,
                    "run_cross_document_semantic_graph_shadow",
                    side_effect=fail_after_publication,
                ),
                mock.patch.object(
                    bootstrap,
                    "_publish_shadow_failure_state",
                    side_effect=OSError("shadow state unavailable"),
                ),
                mock.patch.object(
                    bootstrap,
                    "_write_log",
                    side_effect=fail_shadow_fallback_logs,
                ),
            ):
                bootstrap.build_index()

            published = bootstrap.load_json(bootstrap.CONFIG)
            runtime_state = bootstrap.load_json(bootstrap.STATE)
            self.assertTrue(Path(published["index_path"]).is_file())
            self.assertEqual("ready", runtime_state["phase"])
            self.assertEqual(
                "held",
                runtime_state["cross_document_semantic_graph_shadow"]["status"],
            )
            self.assertNotIn("semantic_graph_shadow_path", published)
            marker = bootstrap.load_json(
                Path(published["index_path"]).parent / bootstrap.GENERATION_MARKER
            )
            self.assertEqual("published", marker["status"])
            self.assertEqual(
                "held", marker["cross_document_semantic_graph_shadow"]["status"]
            )

    def test_cli_only_ollama_is_started_on_loopback_with_owned_log(self) -> None:
        bootstrap = load_module("runtime_bootstrap_ollama", ROOT / "app" / "bootstrap.py")
        with TemporaryDirectory() as temporary:
            bootstrap.SUPPORT = Path(temporary) / "support"
            process = mock.Mock(pid=321)
            log = io.StringIO()
            with (
                mock.patch.object(bootstrap, "ollama_online", return_value=False),
                mock.patch.object(bootstrap, "ollama_app_bundle", return_value=None),
                mock.patch.object(
                    bootstrap, "ollama_binary", return_value="/opt/homebrew/bin/ollama"
                ),
                mock.patch.object(bootstrap, "_wait_for_ollama", return_value=True),
                mock.patch.object(bootstrap.subprocess, "Popen", return_value=process) as popen,
            ):
                bootstrap.start_ollama(log=log, timeout=1)

            args, kwargs = popen.call_args
            self.assertEqual(args[0], ["/opt/homebrew/bin/ollama", "serve"])
            self.assertEqual(kwargs["env"]["OLLAMA_HOST"], "127.0.0.1:11434")
            self.assertIn("127.0.0.1", kwargs["env"]["NO_PROXY"].split(","))
            self.assertIn("localhost", kwargs["env"]["NO_PROXY"].split(","))
            self.assertIn("127.0.0.1", kwargs["env"]["no_proxy"].split(","))
            self.assertIn("localhost", kwargs["env"]["no_proxy"].split(","))
            self.assertTrue(kwargs["start_new_session"])
            serve_log = bootstrap.SUPPORT / "logs" / "ollama-serve.log"
            self.assertTrue(serve_log.is_file())
            self.assertIn("pid=321", log.getvalue())


if __name__ == "__main__":
    unittest.main()
