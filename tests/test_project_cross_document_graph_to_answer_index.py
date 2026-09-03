from __future__ import annotations

import array
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "scripts"
ENGINE = REPOSITORY / "distribution" / "macos-local-memory" / "engine"
BOOTSTRAP = (
    REPOSITORY / "distribution" / "macos-local-memory" / "app" / "bootstrap.py"
)
DATASET = REPOSITORY / "evaluation" / "cross-format-kg-v0.1"
RUNTIME_PYTHON = (
    REPOSITORY / "rag" / ".venv" / "bin" / "python"
    if (REPOSITORY / "rag" / ".venv" / "bin" / "python").is_file()
    else Path(sys.executable)
)


def load_module(name: str, path: Path) -> Any:
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


projector = load_module(
    "semantic_answer_index_projector_test_target",
    SCRIPTS / "project_cross_document_graph_to_answer_index.py",
)
graph_builder = load_module(
    "semantic_answer_index_projector_test_graph_builder",
    SCRIPTS / "build_cross_document_semantic_graph.py",
)
graph_validator = load_module(
    "semantic_answer_index_projector_test_graph_validator",
    SCRIPTS / "validate_cross_document_semantic_graph.py",
)
security_builder = load_module(
    "semantic_answer_index_projector_test_security_builder",
    ENGINE / "content_security_gate.py",
)
answer_index_builder = load_module(
    "semantic_answer_index_projector_test_answer_index_builder",
    ENGINE / "build_local_semantic_index.py",
)
answer_validator = load_module(
    "semantic_answer_index_projector_test_answer_validator",
    ENGINE / "answer_local_memory.py",
)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(canonical_json(record) + "\n" for record in records),
        encoding="utf-8",
    )


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def packed(values: list[float]) -> bytes:
    return array.array("f", values).tobytes()


def run_checked(argv: list[str]) -> None:
    completed = subprocess.run(
        argv,
        cwd=REPOSITORY,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(
            f"command failed ({completed.returncode}): {' '.join(argv)}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )


class CrossDocumentGraphAnswerIndexProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.generation = self.root / ("generation-" + "a" * 32)
        self.semantic = self.generation / "02-semantic"
        self.security = self.generation / "03-security"
        self.shadow_candidate = (
            self.generation / "04-semantic-graph-shadow.building"
        )
        for path in (self.semantic, self.security):
            path.mkdir(parents=True)
        self.documents = self.semantic / "semantic-documents.jsonl"
        self.source_evidence = self.semantic / "semantic-evidence.jsonl"
        self.evidence = self.security / "safe-answer-evidence.jsonl"
        self.security_state = self.security / "content-security-state.json"
        self.security_validator = ENGINE / "validate_content_security_gate.py"
        self.base_index = self.generation / "safe-answer-index.sqlite3"
        self.output_dir = self.generation / "05-semantic-answer-index.building"
        self.output = self.output_dir / "safe-answer-index.sqlite3"
        self.state = self.output_dir / "semantic-answer-index-state.json"
        self._build_inputs_and_shadow()
        self._build_ready_answer_index()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _build_inputs_and_shadow(self) -> None:
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
        self.records: list[dict[str, Any]] = []
        for row_number, row in enumerate((headers, values), 1):
            for column_number, value in enumerate(row, 1):
                self.records.append({
                    "evidence_id": f"ev_{row_number}_{column_number}",
                    "document_id": "doc_assignments",
                    "source": source,
                    "locator": {
                        "sheet_name": "Assignments",
                        "cell": f"{chr(64 + column_number)}{row_number}",
                    },
                    "observed_text": value,
                    "ordinal": len(self.records) + 1,
                    "adapter": {
                        "execution_policy": "never_execute",
                        "source_record_type": "table_cell",
                    },
                    "status": "observed",
                })
        document_records = [{
            "document_id": "doc_assignments",
            "source": source,
            "evidence_ids": [item["evidence_id"] for item in self.records],
            "status": "extracted",
        }]
        write_jsonl(self.documents, document_records)
        write_jsonl(self.source_evidence, self.records)
        security_builder.build(
            self.source_evidence,
            self.documents,
            self.security,
            created_at="2026-09-03T00:00:00+09:00",
        )
        self._publish_current_shadow()

    def _publish_current_shadow(self, build_id: str = "test-build") -> None:
        self.shadow_candidate = (
            self.generation / "04-semantic-graph-shadow.building"
        )
        self.shadow_candidate.mkdir(parents=True, exist_ok=False)
        self.graph_database = self.shadow_candidate / "semantic-graph.sqlite3"
        self.graph_state = self.shadow_candidate / "semantic-graph-state.json"
        self.graph_validation = (
            self.shadow_candidate / "semantic-graph-validation.json"
        )
        graph_builder.build(
            self.documents,
            self.evidence,
            self.graph_database,
            self.graph_state,
        )
        validation = graph_validator.validate(
            self.graph_database,
            self.graph_state,
            self.documents,
            self.source_evidence,
            self.evidence,
            self.security_state,
            self.security,
            self.security_validator,
            self.generation,
            self.graph_validation,
        )
        run_state = {
            "schema_version": "0.1",
            "record_type": "cross_document_semantic_graph_shadow_run",
            "status": "complete",
            "reason_code": "none",
            "shadow_only": True,
            "used_for_index": False,
            "used_for_answers": False,
            "feature_flag": "cross_document_semantic_graph_shadow_enabled",
            "generation": self.generation.name,
            "build_id": build_id,
            "elapsed_ms": 1,
            "output_directory": "04-semantic-graph-shadow",
            "execution_mode": "post_publish_observer",
            "failure_gates_production_index": False,
            "external_network_used": False,
            "timeout_seconds": 300,
            "graph_snapshot_id": validation["graph_snapshot_id"],
            "logical_snapshot_sha256": validation[
                "logical_snapshot_sha256"
            ],
            "sqlite_sha256": validation["sqlite_sha256"],
            "sqlite_size_bytes": self.graph_database.stat().st_size,
            "counts": validation["counts"],
            "relation_type_counts": validation["relation_type_counts"],
            "upstream": {
                "semantic_documents_sha256": projector.sha256_file(
                    self.documents
                ),
                "semantic_evidence_sha256": projector.sha256_file(
                    self.source_evidence
                ),
                "safe_answer_evidence_sha256": projector.sha256_file(
                    self.evidence
                ),
                "content_security_state_sha256": projector.sha256_file(
                    self.security_state
                ),
            },
            "tool_sha256": {
                name: projector.sha256_file(SCRIPTS / name)
                for name in (
                    "build_cross_document_semantic_graph.py",
                    "query_cross_document_semantic_graph.py",
                    "validate_cross_document_semantic_graph.py",
                )
            },
            "artifacts": dict(projector.SHADOW_FILES),
        }
        (self.shadow_candidate / "shadow-run-state.json").write_text(
            json.dumps(run_state, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        self.shadow = self.generation / "04-semantic-graph-shadow"
        os.replace(self.shadow_candidate, self.shadow)
        self.graph_database = self.shadow / "semantic-graph.sqlite3"
        self.graph_state = self.shadow / "semantic-graph-state.json"
        self.graph_validation = self.shadow / "semantic-graph-validation.json"
        self.run_state = self.shadow / "shadow-run-state.json"

    def _build_ready_answer_index(self, unresolved_id: str | None = None) -> None:
        connection = sqlite3.connect(self.base_index)
        answer_index_builder.initialize(connection)
        locator_by_id: dict[str, dict[str, Any]] = {}
        document_by_id: dict[str, dict[str, Any]] = {}
        vectors: dict[str, list[float]] = {}
        for offset, record in enumerate(self.records, 1):
            evidence_id = record["evidence_id"]
            document_by_id[record["document_id"]] = record["source"]
            locator = record["locator"]
            locator_by_id[evidence_id] = locator
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
                    record["document_id"],
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

        held = []
        if unresolved_id is not None:
            held = [{
                "evidence_id": unresolved_id,
                "reason_codes": ["excluded_lineage_source"],
                "excluded_source_evidence_ids": ["ev_excluded_source"],
            }]
        partition = {
            "schema_version": "0.1",
            "partitioner": "content-security-graph-partitioner",
            "partitioner_version": "0.1.0",
            "status": "pass",
            "question_independent": True,
            "security_policy_version": "0.2.0",
            "security_state_sha256": projector.sha256_file(self.security_state),
            "safe_answer_evidence_sha256": projector.sha256_file(self.evidence),
            "document_source_set_sha256": "c" * 64,
            "evidence_source_set_sha256": "d" * 64,
            "source_relation_set_sha256": "e" * 64,
            "projected_relation_set_sha256": "f" * 64,
            "promoted_relation_ids": [],
            "held_relations": [],
            "held_derived_evidence": held,
            "counts": {
                "source_relations": 0,
                "promoted_relations": 0,
                "held_relations": 0,
                "safe_evidence": len(self.records),
                "held_derived_evidence": len(held),
            },
        }
        partition["partition_sha256"] = answer_validator.record_sha256(partition)

        nodes: list[dict[str, Any]] = []
        for document_id in sorted(document_by_id):
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
            node["record_sha256"] = answer_validator.record_sha256(node)
            nodes.append(node)
        for record in self.records:
            evidence_id = record["evidence_id"]
            text = record["observed_text"]
            status = "unresolved" if evidence_id == unresolved_id else "observed"
            payload = {
                "record_type": "evidence",
                "record_id": evidence_id,
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
            }
            if status == "unresolved":
                payload["security_graph_hold"] = {
                    "reason_codes": held[0]["reason_codes"],
                    "excluded_source_evidence_ids": held[0][
                        "excluded_source_evidence_ids"
                    ],
                    "partition_sha256": partition["partition_sha256"],
                }
            node = {
                "node_id": evidence_id,
                "node_type": "evidence",
                "payload": payload,
                "status": status,
            }
            node["record_sha256"] = answer_validator.record_sha256(node)
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
                    answer_validator.canonical_json(node["payload"]),
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
            and node["status"] in {"observed", "verified"}
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
            "evidence_sha256": projector.sha256_file(self.evidence),
            "documents_sha256": projector.sha256_file(self.documents),
            "content_security_state_sha256": projector.sha256_file(
                self.security_state
            ),
            "content_security_gate": True,
            "content_security_execution_policy": "never_execute",
            "index_purpose": "safe_answer",
            "answer_generation_allowed": True,
            "graph_schema_version": "0.1",
            "graph_status": "validated_safe_partition",
            "graph_retrieval_enabled": True,
            "graph_node_count": len(nodes),
            "graph_edge_count": 0,
            "graph_document_node_count": len(document_by_id),
            "graph_evidence_node_count": len(self.records),
            "graph_sha256": answer_validator.record_sha256(graph),
            "graph_security_partition": partition,
            "graph_security_partition_sha256": partition["partition_sha256"],
            "graph_source_relation_input_count": 0,
            "graph_retrievable_evidence_count": len(eligible_rows),
            "graph_unresolved_evidence_count": len(held),
            "graph_held_derived_evidence_count": len(held),
            "graph_nonindexed_held_derived_evidence_count": 0,
            "graph_retrievable_evidence_set_sha256": answer_validator.record_sha256(
                eligible_rows
            ),
            "embedding_space_probe_version": (
                answer_validator.EMBEDDING_SPACE_PROBE_VERSION
            ),
            "embedding_space_probe_text_sha256": hashlib.sha256(
                answer_validator.EMBEDDING_SPACE_PROBE_TEXT.encode("utf-8")
            ).hexdigest(),
            "embedding_space_probe_dimension": 2,
            "embedding_space_probe_vector_f32_sha256": hashlib.sha256(
                probe
            ).hexdigest(),
            "graph_embeddings_sha256": answer_validator.record_sha256({
                "model": "embedding-test",
                "probe": {
                    "version": answer_validator.EMBEDDING_SPACE_PROBE_VERSION,
                    "text_sha256": hashlib.sha256(
                        answer_validator.EMBEDDING_SPACE_PROBE_TEXT.encode(
                            "utf-8"
                        )
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
        connection.close()
        with closing(sqlite3.connect(self.base_index)) as validation_connection:
            answer_validator.validate_answer_graph_contract(
                validation_connection
            )

    def _arguments(self) -> dict[str, Path]:
        return {
            "base_index": self.base_index,
            "shadow_dir": self.shadow,
            "documents": self.documents,
            "source_evidence": self.source_evidence,
            "evidence": self.evidence,
            "security_state": self.security_state,
            "security_gate_dir": self.security,
            "security_validator": self.security_validator,
            "generation_dir": self.generation,
            "output": self.output,
            "state": self.state,
        }

    def _replace_with_five_document_fixture(self) -> None:
        for path in (self.semantic, self.security, self.shadow):
            shutil.rmtree(path)
        self.base_index.unlink()
        fixture_work = self.root / "five-document-layer1"
        intermediate = fixture_work / "intermediate"
        search = fixture_work / "search"
        corpus = DATASET / "corpus"
        run_checked([
            str(RUNTIME_PYTHON),
            str(SCRIPTS / "build_intermediate_records.py"),
            "--root",
            str(corpus),
            "--out",
            str(intermediate),
            "--run-at",
            "2026-08-27T00:00:00+00:00",
        ])
        run_checked([
            str(RUNTIME_PYTHON),
            str(SCRIPTS / "build_search_units.py"),
            "--intermediate",
            str(intermediate),
            "--out",
            str(search),
        ])
        run_checked([
            str(RUNTIME_PYTHON),
            str(SCRIPTS / "adapt_layer1_to_local_memory.py"),
            "--intermediate",
            str(intermediate),
            "--search-output",
            str(search),
            "--source-root",
            str(corpus),
            "--out",
            str(self.semantic),
        ])
        self.security.mkdir(parents=True)
        security_builder.build(
            self.source_evidence,
            self.documents,
            self.security,
            created_at="2026-09-03T00:00:00+09:00",
        )
        self.records = read_jsonl(self.evidence)
        self._publish_current_shadow(build_id="five-document-build")
        self._build_ready_answer_index()

    def test_success_copies_validated_graph_without_changing_base(self) -> None:
        before = self.base_index.read_bytes()

        state = projector.project(**self._arguments())

        self.assertEqual(before, self.base_index.read_bytes())
        self.assertEqual("complete", state["status"])
        self.assertTrue(state["storage_only"])
        self.assertFalse(state["retrieval_enabled"])
        self.assertFalse(state["used_for_answers"])
        self.assertFalse(state["answer_behavior_changed"])
        self.assertEqual("test-build", state["shadow"]["build_id"])
        self.assertEqual(projector.sha256_file(self.output), state["output"]["sqlite_sha256"])
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state, persisted)
        with closing(sqlite3.connect(self.output)) as connection:
            answer_validator.validate_answer_graph_contract(connection)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue(projector.SEMANTIC_TABLES <= tables)
            node_types = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT node_type FROM semantic_graph_nodes"
                )
            }
            self.assertTrue({"Project", "Work", "Employee"} <= node_types)
            metadata = {
                key: json.loads(value)
                for key, value in connection.execute(
                    "SELECT key, value FROM metadata"
                )
            }
            self.assertEqual(
                "validated_storage_only",
                metadata[
                    "cross_document_semantic_graph_storage_status"
                ],
            )
            self.assertFalse(
                metadata["cross_document_semantic_graph_retrieval_enabled"]
            )
            self.assertFalse(
                metadata["cross_document_semantic_graph_used_for_answers"]
            )
            unsupported = connection.execute(
                "SELECT COUNT(*) FROM semantic_graph_edge_evidence s "
                "LEFT JOIN graph_nodes g ON g.node_id = s.evidence_id "
                "WHERE g.node_type != 'evidence' "
                "OR g.status NOT IN ('observed', 'verified')"
            ).fetchone()[0]
            self.assertEqual(0, unsupported)

    def test_five_document_graph_is_stored_without_changing_answer_policy(self) -> None:
        self._replace_with_five_document_fixture()
        before = self.base_index.read_bytes()
        answer_tables = (
            "evidence",
            "embeddings",
            "graph_nodes",
            "graph_edges",
        )
        with closing(sqlite3.connect(self.base_index)) as connection:
            before_policy = answer_validator.validate_answer_graph_contract(
                connection
            )
            before_rows = {
                table: list(connection.execute(f"SELECT * FROM {table} ORDER BY 1"))
                for table in answer_tables
            }
            before_metadata = dict(
                connection.execute("SELECT key, value FROM metadata ORDER BY key")
            )

        state = projector.project(**self._arguments())

        self.assertEqual(13, state["counts"]["nodes"])
        self.assertEqual(16, state["counts"]["edges"])
        self.assertEqual(before, self.base_index.read_bytes())
        with closing(sqlite3.connect(self.output)) as connection:
            after_policy = answer_validator.validate_answer_graph_contract(
                connection
            )
            self.assertEqual(
                before_policy["graph_sha256"], after_policy["graph_sha256"]
            )
            self.assertEqual(
                before_policy["partition_sha256"],
                after_policy["partition_sha256"],
            )
            self.assertEqual(
                frozenset(before_policy["eligible_evidence_ids"]),
                frozenset(after_policy["eligible_evidence_ids"]),
            )
            for table in answer_tables:
                self.assertEqual(
                    before_rows[table],
                    list(connection.execute(f"SELECT * FROM {table} ORDER BY 1")),
                )
            after_metadata = dict(
                connection.execute(
                    "SELECT key, value FROM metadata "
                    "WHERE key NOT LIKE ? ORDER BY key",
                    (projector.METADATA_PREFIX + "%",),
                )
            )
            self.assertEqual(before_metadata, after_metadata)
            self.assertEqual(
                13,
                connection.execute(
                    "SELECT COUNT(*) FROM semantic_graph_nodes"
                ).fetchone()[0],
            )
            self.assertEqual(
                16,
                connection.execute(
                    "SELECT COUNT(*) FROM semantic_graph_edges"
                ).fetchone()[0],
            )

        final_dir = self.generation / "05-semantic-answer-index"
        os.replace(self.output_dir, final_dir)
        audited = projector.validate_existing_projection(
            **{
                **self._arguments(),
                "output": final_dir / self.output.name,
                "state": final_dir / self.state.name,
            },
            expected_build_id="five-document-build",
        )
        self.assertEqual(state, audited)
        bootstrap = load_module(
            "semantic_answer_index_projector_bootstrap_integration",
            BOOTSTRAP,
        )
        registration = bootstrap._semantic_storage_registration(
            self.generation,
            state,
            semantic=self.semantic,
            security=self.security,
            expected_build_id="five-document-build",
        )
        self.assertEqual(str(final_dir / self.output.name), registration["database_path"])
        self.assertEqual(13, registration["counts"]["nodes"])
        self.assertEqual(16, registration["counts"]["edges"])

    def test_shadow_database_tamper_is_rejected_and_output_removed(self) -> None:
        with closing(sqlite3.connect(self.graph_database)) as connection:
            connection.execute(
                "UPDATE nodes SET canonical_key = 'tampered' "
                "WHERE node_id = (SELECT node_id FROM nodes ORDER BY node_id LIMIT 1)"
            )
            connection.commit()

        with self.assertRaisesRegex(
            projector.ProjectionError,
            "graph_sqlite_hash_mismatch|graph_snapshot_contract_invalid",
        ):
            projector.project(**self._arguments())

        self.assertFalse(self.output.exists())
        self.assertFalse(self.state.exists())

    def test_security_output_symlink_is_rejected(self) -> None:
        security_output = self.security / "content-security-classifications.jsonl"
        external_copy = self.root / security_output.name
        shutil.copy2(security_output, external_copy)
        security_output.unlink()
        security_output.symlink_to(external_copy)

        with self.assertRaisesRegex(
            projector.ProjectionError,
            "input_invalid:content-security-classifications.jsonl",
        ):
            projector.project(**self._arguments())

        self.assertFalse(self.output.exists())
        self.assertFalse(self.state.exists())

    def test_linked_builder_state_tamper_is_rejected(self) -> None:
        graph_state = json.loads(self.graph_state.read_text(encoding="utf-8"))
        graph_state["output"] = {
            "sqlite_file": "not-the-shadow.sqlite3",
            "state_file": "not-the-shadow-state.json",
        }
        self.graph_state.write_text(
            json.dumps(graph_state, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        validation = json.loads(self.graph_validation.read_text(encoding="utf-8"))
        validation["builder_state_sha256"] = projector.sha256_file(
            self.graph_state
        )
        self.graph_validation.write_text(
            json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            projector.ProjectionError, "graph_builder_output_binding_invalid"
        ):
            projector.project(**self._arguments())

        self.assertFalse(self.output.exists())
        self.assertFalse(self.state.exists())

    def test_output_path_swap_cannot_modify_base_index(self) -> None:
        before = self.base_index.read_bytes()
        original_create = projector._create_semantic_tables

        def swap_output_path(connection: sqlite3.Connection) -> None:
            self.output.unlink()
            os.link(self.base_index, self.output)
            original_create(connection)

        with mock.patch.object(
            projector,
            "_create_semantic_tables",
            side_effect=swap_output_path,
        ):
            with self.assertRaises(
                (projector.ProjectionError, sqlite3.OperationalError)
            ):
                projector.project(**self._arguments())

        self.assertEqual(before, self.base_index.read_bytes())
        with closing(sqlite3.connect(self.base_index)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertTrue(projector.SEMANTIC_TABLES.isdisjoint(tables))
        self.assertFalse(self.state.exists())

    def test_unretrievable_edge_support_is_rejected(self) -> None:
        with closing(sqlite3.connect(self.graph_database)) as connection:
            supported = connection.execute(
                "SELECT evidence_id FROM edge_evidence ORDER BY evidence_id LIMIT 1"
            ).fetchone()[0]
        self.base_index.unlink()
        self._build_ready_answer_index(unresolved_id=supported)

        with self.assertRaisesRegex(
            projector.ProjectionError,
            "semantic_edge_support_not_retrievable",
        ):
            projector.project(**self._arguments())

        self.assertFalse(self.output.exists())
        self.assertFalse(self.state.exists())

    def test_existing_output_is_refused_without_overwrite(self) -> None:
        self.output_dir.mkdir()
        self.output.write_bytes(b"do-not-overwrite")

        with self.assertRaisesRegex(
            projector.ProjectionError,
            "projection_output_exists|projection_output_directory_not_empty",
        ):
            projector.project(**self._arguments())

        self.assertEqual(b"do-not-overwrite", self.output.read_bytes())
        self.assertFalse(self.state.exists())

    def test_base_index_is_unchanged_when_validation_state_is_tampered(self) -> None:
        before = self.base_index.read_bytes()
        validation = json.loads(self.graph_validation.read_text(encoding="utf-8"))
        validation["graph_snapshot_id"] = "xkgs_" + "f" * 32
        self.graph_validation.write_text(
            json.dumps(validation, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            projector.ProjectionError,
            "graph_snapshot_id_mismatch:validation",
        ):
            projector.project(**self._arguments())

        self.assertEqual(before, self.base_index.read_bytes())
        self.assertFalse(self.output.exists())

    def test_shadow_run_state_requires_a_nonempty_build_id(self) -> None:
        run_state = json.loads(self.run_state.read_text(encoding="utf-8"))
        run_state["build_id"] = ""
        self.run_state.write_text(
            json.dumps(run_state, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            projector.ProjectionError, "graph_shadow_build_id_invalid"
        ):
            projector.project(**self._arguments())

        self.assertFalse(self.output.exists())
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
