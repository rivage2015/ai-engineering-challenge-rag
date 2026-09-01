from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from copy import deepcopy
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT / "distribution" / "macos-local-memory" / "engine"
    / "build_local_semantic_index.py"
)
SPEC = importlib.util.spec_from_file_location("local_graph_index_schema", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise ImportError(f"cannot load {MODULE_PATH}")
index_builder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = index_builder
SPEC.loader.exec_module(index_builder)


def canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: dict) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


DOC_ID = "doc_fe91154dcfd93e9e3ca1c78067ad1237"
EVIDENCE_ID = "ev_22222222222222222222222222222222"
SECOND_EVIDENCE_ID = "ev_33333333333333333333333333333333"


def semantic_document() -> dict:
    return {
        "schema_version": "0.1",
        "document_id": DOC_ID,
        "source": {
            "relative_path": "dawn.xlsx",
            "absolute_path": "/private/tmp/dawn.xlsx",
            "sha256": "a" * 64,
            "size_bytes": 13,
            "file_type": "xlsx",
        },
        "classification": "extractable",
        "classification_reason": "verified_layer1_intermediate_record",
        "project_id": None,
        "extraction_method": "openpyxl+ooxml",
        "status": "extracted",
        "evidence_ids": [EVIDENCE_ID],
        "extraction_metadata": {},
        "error": None,
    }


def semantic_evidence(
    evidence_id: str = EVIDENCE_ID, observed_text: str = "稼働回数 13回",
) -> dict:
    return {
        "schema_version": "0.1",
        "evidence_id": evidence_id,
        "document_id": DOC_ID,
        "ordinal": 1,
        "locator": {"sheet_name": "集計表", "cell": "B33"},
        "observed_text": observed_text,
        "source": {"relative_path": "dawn.xlsx", "sha256": "a" * 64},
        "extraction_method": "native_parser",
        "status": "observed",
        "adapter": {
            "name": "layer1-to-local-memory-evidence-adapter",
            "version": "0.4.0",
        },
    }


def relation(
    *,
    from_record_type: str = "document",
    from_id: str = DOC_ID,
    target_id: str = EVIDENCE_ID,
    status: str = "verified",
    relation_class: str = "structural",
    relation_type: str = "contains",
    generated_by: str = "intermediate-record-extractor",
    generator_version: str = "0.7.0",
    rule_or_model: str = "native containment",
    supporting_evidence_ids: list[str] | None = None,
) -> dict:
    value = {
        "schema_version": "0.1",
        "record_type": "relation",
        "relation_class": relation_class,
        "relation_type": relation_type,
        "from_ref": {"record_type": from_record_type, "record_id": from_id},
        "to_ref": {"record_type": "evidence", "record_id": target_id},
        "properties": {"ordinal": 1},
        "supporting_evidence_ids": (
            [] if supporting_evidence_ids is None else supporting_evidence_ids
        ),
        "provenance": {
            "generated_by": generated_by,
            "generator_version": generator_version,
            "generated_at": "2026-09-01T00:00:00+00:00",
            "deterministic": True,
            "confidence": 1.0,
            "rule_or_model": rule_or_model,
            "warnings": [],
        },
        "status": status,
    }
    identity = {
        "class": value["relation_class"],
        "type": value["relation_type"],
        "from": value["from_ref"],
        "to": value["to_ref"],
        "generator": value["provenance"]["generated_by"],
        "generator_version": value["provenance"]["generator_version"],
    }
    value["relation_id"] = f"rel_{digest(identity)[:32]}"
    return value


def insert_indexed_evidence(connection: sqlite3.Connection, record: dict) -> None:
    observed_text = record["observed_text"]
    connection.execute(
        "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record["evidence_id"], record["document_id"],
            record["source"]["relative_path"], canonical(record["locator"]),
            observed_text, observed_text, 0,
            hashlib.sha256(observed_text.encode("utf-8")).hexdigest(),
        ),
    )


class LocalGraphIndexSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        index_builder.initialize(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    def test_adds_empty_graph_tables_without_replacing_evidence_index(self) -> None:
        tables = {
            name
            for (name,) in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertEqual(
            tables,
            {"metadata", "evidence", "embeddings", "graph_nodes", "graph_edges"},
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0],
            0,
        )
        self.assertEqual(index_builder.INDEX_SCHEMA_VERSION, "0.3")
        self.assertEqual(index_builder.GRAPH_SCHEMA_VERSION, "0.1")
        self.connection.execute(
            "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "ev_existing", "doc_existing", "existing.txt", "{}", "text", "text",
                0, hashlib.sha256(b"text").hexdigest(),
            ),
        )
        self.connection.execute(
            "INSERT INTO embeddings VALUES (?, ?, ?)",
            ("ev_existing", 2, b"12345678"),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT observed_text FROM evidence JOIN embeddings USING(evidence_id)"
            ).fetchone()[0],
            "text",
        )

    def test_edge_requires_existing_nodes_and_meaningful_basis(self) -> None:
        source_payload = {"record_type": "document", "record_id": "doc_source"}
        target_payload = {"record_type": "evidence", "record_id": "ev_target"}
        self.connection.executemany(
            "INSERT INTO graph_nodes VALUES (?, ?, ?, ?, ?)",
            [
                (
                    "doc_source", "document", canonical(source_payload),
                    "observed", digest(source_payload),
                ),
                (
                    "ev_target", "evidence", canonical(target_payload),
                    "observed", digest(target_payload),
                ),
            ],
        )
        basis = {"supporting_evidence_ids": ["ev_target"]}
        properties = {"ordinal": 1}
        edge_record = {
            "source": "doc_source", "type": "contains", "target": "ev_target",
            "basis_kind": "explicit", "basis_rule": "native_structure",
            "basis": basis, "properties": properties, "status": "verified",
        }
        self.connection.execute(
            "INSERT INTO graph_edges VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "rel_contains", "doc_source", "contains", "ev_target", "structural",
                "explicit", "native_structure", canonical(basis), canonical(properties),
                "verified", digest(edge_record),
            ),
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO graph_edges VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "rel_missing", "missing_node", "contains", "ev_target", "structural",
                    "explicit", "native_structure", canonical(basis), canonical(properties),
                    "verified", digest({"edge": "missing"}),
                ),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO graph_edges VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "rel_no_basis", "doc_source", "contains", "ev_target", "structural",
                    "explicit", "", canonical(basis), canonical(properties),
                    "verified", digest({"edge": "no_basis"}),
                ),
            )

    def test_graph_indexes_support_bounded_outgoing_and_incoming_traversal(self) -> None:
        indexes = {
            name
            for (name,) in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
            )
        }
        self.assertTrue({
            "graph_nodes_type_status_idx",
            "graph_edges_from_type_status_idx",
            "graph_edges_to_type_status_idx",
        } <= indexes)
        self.assertEqual(
            self.connection.execute("PRAGMA foreign_keys").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute("PRAGMA foreign_key_check").fetchall(),
            [],
        )

    def test_projects_authorized_nodes_and_only_verified_structural_edges(self) -> None:
        document = semantic_document()
        document["evidence_ids"].append("ev_ffffffffffffffffffffffffffffffff")
        evidence = semantic_evidence()
        insert_indexed_evidence(self.connection, evidence)
        proposed = relation(status="proposed", relation_type="includes")
        semantic = relation(relation_class="semantic")
        unknown_producer = relation(
            target_id="ev_dddddddddddddddddddddddddddddddd",
            generated_by="unreviewed-generator",
            rule_or_model="unknown containment",
        )
        unknown_producer_version = relation(generator_version="999-unreviewed")
        verified = relation()

        report = index_builder.project_verified_structural_graph(
            self.connection,
            [document],
            [evidence],
            [semantic, unknown_producer, unknown_producer_version, verified, proposed],
        )

        self.assertEqual(report["document_node_count"], 1)
        self.assertEqual(report["evidence_node_count"], 1)
        self.assertEqual(report["node_count"], 2)
        self.assertEqual(report["edge_count"], 1)
        self.assertEqual(report["isolated_node_count"], 0)
        self.assertEqual(
            report["skipped_relations"],
            {
                "not_verified": [proposed["relation_id"]],
                "non_structural": [semantic["relation_id"]],
                "not_explicit": sorted([
                    unknown_producer["relation_id"],
                    unknown_producer_version["relation_id"],
                ]),
            },
        )
        self.assertEqual(
            report["skipped_edge_count_by_reason"],
            {"not_verified": 1, "non_structural": 1, "not_explicit": 2},
        )
        self.assertEqual(len(report["graph_sha256"]), 64)

        document_payload = json.loads(self.connection.execute(
            "SELECT payload_json FROM graph_nodes WHERE node_id = ?", (DOC_ID,),
        ).fetchone()[0])
        self.assertNotIn("evidence_ids", document_payload["source_record"])
        self.assertEqual(document_payload["authorized_evidence_ids"], [EVIDENCE_ID])
        self.assertNotIn(
            "ev_ffffffffffffffffffffffffffffffff",
            canonical(document_payload),
        )

        node_id, node_type, payload_json, node_status, node_hash = self.connection.execute(
            "SELECT node_id, node_type, payload_json, status, record_sha256 "
            "FROM graph_nodes WHERE node_id = ?",
            (EVIDENCE_ID,),
        ).fetchone()
        payload = json.loads(payload_json)
        self.assertEqual(
            (node_id, node_type, node_status),
            (EVIDENCE_ID, "evidence", "observed"),
        )
        self.assertNotIn("observed_text", payload)
        self.assertNotIn("observed_text", payload["source_record"])
        self.assertEqual(payload["source_record"]["locator"], evidence["locator"])
        self.assertEqual(payload["source_record_sha256"], digest(evidence))
        self.assertEqual(
            payload["observed_sha256"],
            hashlib.sha256(evidence["observed_text"].encode("utf-8")).hexdigest(),
        )
        self.assertEqual(node_hash, digest({
            "node_id": node_id,
            "node_type": node_type,
            "payload": payload,
            "status": node_status,
        }))
        self.assertEqual(
            self.connection.execute(
                "SELECT observed_text FROM evidence WHERE evidence_id = ?",
                (EVIDENCE_ID,),
            ).fetchone()[0],
            evidence["observed_text"],
        )

        edge = self.connection.execute(
            "SELECT relation_id, from_node_id, relation_type, to_node_id, "
            "relation_class, basis_kind, basis_rule, basis_json, properties_json, "
            "status, record_sha256 FROM graph_edges"
        ).fetchone()
        basis = json.loads(edge[7])
        properties = json.loads(edge[8])
        self.assertEqual(edge[:7], (
            verified["relation_id"], DOC_ID, "contains", EVIDENCE_ID, "structural",
            "explicit", "native containment",
        ))
        self.assertEqual(edge[9], "verified")
        self.assertEqual(basis["from_ref"], verified["from_ref"])
        self.assertEqual(basis["to_ref"], verified["to_ref"])
        self.assertEqual(basis["supporting_evidence_ids"], [])
        self.assertEqual(basis["source_relation_sha256"], digest(verified))
        self.assertEqual(properties, verified["properties"])
        projected_edge = {
            "relation_id": edge[0],
            "from_node_id": edge[1],
            "relation_type": edge[2],
            "to_node_id": edge[3],
            "relation_class": edge[4],
            "basis_kind": edge[5],
            "basis_rule": edge[6],
            "basis": basis,
            "properties": properties,
            "status": edge[9],
        }
        self.assertEqual(edge[10], digest(projected_edge))

        reconstructed_relation = {
            "schema_version": basis["source_schema_version"],
            "record_type": basis["source_record_type"],
            "relation_id": edge[0],
            "relation_class": edge[4],
            "relation_type": edge[2],
            "from_ref": basis["from_ref"],
            "to_ref": basis["to_ref"],
            "provenance": basis["provenance"],
            "status": edge[9],
        }
        if basis["optional_fields_present"]["properties"]:
            reconstructed_relation["properties"] = properties
        if basis["optional_fields_present"]["supporting_evidence_ids"]:
            reconstructed_relation["supporting_evidence_ids"] = basis[
                "supporting_evidence_ids"
            ]
        self.assertEqual(reconstructed_relation, verified)
        self.assertEqual(digest(reconstructed_relation), basis["source_relation_sha256"])

    def test_relation_basis_preserves_omitted_optional_fields(self) -> None:
        evidence = semantic_evidence()
        insert_indexed_evidence(self.connection, evidence)
        source_relation = relation()
        source_relation.pop("properties")
        source_relation.pop("supporting_evidence_ids")

        index_builder.project_verified_structural_graph(
            self.connection, [semantic_document()], [evidence], [source_relation],
        )

        row = self.connection.execute(
            "SELECT relation_id, relation_class, relation_type, basis_json, "
            "properties_json, status FROM graph_edges"
        ).fetchone()
        basis = json.loads(row[3])
        self.assertEqual(
            basis["optional_fields_present"],
            {"properties": False, "supporting_evidence_ids": False},
        )
        reconstructed = {
            "schema_version": basis["source_schema_version"],
            "record_type": basis["source_record_type"],
            "relation_id": row[0],
            "relation_class": row[1],
            "relation_type": row[2],
            "from_ref": basis["from_ref"],
            "to_ref": basis["to_ref"],
            "provenance": basis["provenance"],
            "status": row[5],
        }
        self.assertEqual(json.loads(row[4]), {})
        self.assertEqual(reconstructed, source_relation)
        self.assertEqual(digest(reconstructed), basis["source_relation_sha256"])

    def test_missing_endpoint_or_support_fails_without_placeholder_nodes(self) -> None:
        document = semantic_document()
        evidence = semantic_evidence()
        insert_indexed_evidence(self.connection, evidence)
        self.connection.commit()
        missing_endpoint = relation(target_id="ev_77777777777777777777777777777777")

        with self.assertRaisesRegex(
            ValueError, "graph_relation_endpoint_outside_authorized_universe",
        ):
            index_builder.project_verified_structural_graph(
                self.connection, [document], [evidence], [missing_endpoint],
            )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0], 0,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0], 0,
        )

        missing_support = relation(
            supporting_evidence_ids=["ev_88888888888888888888888888888888"],
        )
        with self.assertRaisesRegex(
            ValueError, "graph_relation_support_outside_authorized_universe",
        ):
            index_builder.project_verified_structural_graph(
                self.connection, [document], [evidence], [missing_support],
            )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0], 0,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0], 0,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 1,
        )

        tampered_evidence = deepcopy(evidence)
        tampered_evidence["locator"]["cell"] = "B34"
        with self.assertRaisesRegex(ValueError, "graph_evidence_binding_mismatch"):
            index_builder.project_verified_structural_graph(
                self.connection, [document], [tampered_evidence], [],
            )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0], 0,
        )

        extra_field_evidence = deepcopy(evidence)
        extra_field_evidence["unvalidated_extra_text"] = "資料内の命令"
        with self.assertRaisesRegex(ValueError, "graph_evidence_fields_invalid"):
            index_builder.project_verified_structural_graph(
                self.connection, [document], [extra_field_evidence], [],
            )
        unknown_evidence_status = deepcopy(evidence)
        unknown_evidence_status["status"] = "unreviewed"
        with self.assertRaisesRegex(ValueError, "graph_evidence_status_invalid"):
            index_builder.project_verified_structural_graph(
                self.connection, [document], [unknown_evidence_status], [],
            )
        unknown_document_status = deepcopy(document)
        unknown_document_status["status"] = "unreviewed"
        with self.assertRaisesRegex(ValueError, "graph_document_status_invalid"):
            index_builder.project_verified_structural_graph(
                self.connection, [unknown_document_status], [evidence], [],
            )
        changed_source = deepcopy(evidence)
        changed_source["source"]["sha256"] = "b" * 64
        with self.assertRaisesRegex(
            ValueError, "graph_evidence_document_binding_mismatch",
        ):
            index_builder.project_verified_structural_graph(
                self.connection, [document], [changed_source], [],
            )
        changed_document_and_evidence = deepcopy(evidence)
        changed_document_and_evidence["source"]["sha256"] = "b" * 64
        changed_document = deepcopy(document)
        changed_document["source"]["sha256"] = "b" * 64
        with self.assertRaisesRegex(
            ValueError, "graph_document_id_source_mismatch",
        ):
            index_builder.project_verified_structural_graph(
                self.connection,
                [changed_document],
                [changed_document_and_evidence],
                [],
            )

    def test_projection_rolls_back_if_a_graph_insert_fails(self) -> None:
        document = semantic_document()
        evidence = semantic_evidence()
        insert_indexed_evidence(self.connection, evidence)
        self.connection.commit()

        real_record_sha256 = index_builder.record_sha256

        def fail_on_final_graph_hash(value: object) -> str:
            if isinstance(value, dict) and set(value) == {
                "graph_schema_version", "nodes", "edges",
            }:
                raise RuntimeError("forced graph hash failure")
            return real_record_sha256(value)

        with mock.patch.object(
            index_builder, "record_sha256", side_effect=fail_on_final_graph_hash,
        ), self.assertRaisesRegex(RuntimeError, "forced graph hash failure"):
            index_builder.project_verified_structural_graph(
                self.connection, [document], [evidence], [relation()],
            )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0], 0,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0], 0,
        )

        self.connection.execute(
            "CREATE TRIGGER reject_evidence_graph_node "
            "BEFORE INSERT ON graph_nodes "
            "WHEN NEW.node_type = 'evidence' "
            "BEGIN SELECT RAISE(ABORT, 'forced graph insert failure'); END"
        )

        with self.assertRaises(sqlite3.IntegrityError):
            index_builder.project_verified_structural_graph(
                self.connection, [document], [evidence], [relation()],
            )

        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0], 0,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0], 0,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 1,
        )
        self.connection.execute("DROP TRIGGER reject_evidence_graph_node")
        self.connection.execute(
            "CREATE TRIGGER mutate_evidence_graph_node "
            "AFTER INSERT ON graph_nodes "
            "WHEN NEW.node_type = 'evidence' "
            "BEGIN UPDATE graph_nodes SET status = 'ambiguous' "
            "WHERE node_id = NEW.node_id; END"
        )
        with self.assertRaisesRegex(ValueError, "graph_node_record_hash_mismatch"):
            index_builder.project_verified_structural_graph(
                self.connection, [document], [evidence], [relation()],
            )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0], 0,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0], 0,
        )
        self.connection.execute("DROP TRIGGER mutate_evidence_graph_node")
        tampered_hash = hashlib.sha256(b"999").hexdigest()
        self.connection.execute(
            "CREATE TRIGGER mutate_indexed_evidence "
            "AFTER INSERT ON graph_nodes "
            "WHEN NEW.node_type = 'evidence' "
            "BEGIN UPDATE evidence SET observed_text = '999', "
            f"observed_sha256 = '{tampered_hash}' "
            f"WHERE evidence_id = '{EVIDENCE_ID}'; END"
        )
        with self.assertRaisesRegex(
            ValueError, "graph_evidence_changed_during_projection",
        ):
            index_builder.project_verified_structural_graph(
                self.connection, [document], [evidence], [relation()],
            )
        self.assertEqual(
            self.connection.execute(
                "SELECT observed_text FROM evidence WHERE evidence_id = ?",
                (EVIDENCE_ID,),
            ).fetchone()[0],
            evidence["observed_text"],
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0], 0,
        )
        self.connection.execute("DROP TRIGGER mutate_indexed_evidence")
        self.connection.execute(
            "CREATE TRIGGER mutate_indexed_embedding_text "
            "AFTER INSERT ON graph_nodes "
            "WHEN NEW.node_type = 'evidence' "
            "BEGIN UPDATE evidence SET embedding_text = 'poison' "
            f"WHERE evidence_id = '{EVIDENCE_ID}'; END"
        )
        with self.assertRaisesRegex(
            ValueError, "graph_evidence_changed_during_projection",
        ):
            index_builder.project_verified_structural_graph(
                self.connection, [document], [evidence], [relation()],
            )
        self.assertEqual(
            self.connection.execute(
                "SELECT embedding_text FROM evidence WHERE evidence_id = ?",
                (EVIDENCE_ID,),
            ).fetchone()[0],
            evidence["observed_text"],
        )
        self.connection.execute("DROP TRIGGER mutate_indexed_embedding_text")
        self.connection.execute(
            "INSERT INTO embeddings VALUES (?, ?, ?)",
            (EVIDENCE_ID, 1, b"\x00\x00\x00\x00"),
        )
        self.connection.commit()
        self.connection.execute(
            "CREATE TRIGGER mutate_indexed_embedding_vector "
            "AFTER INSERT ON graph_nodes "
            "WHEN NEW.node_type = 'evidence' "
            "BEGIN UPDATE embeddings SET vector_f32 = X'01010101' "
            f"WHERE evidence_id = '{EVIDENCE_ID}'; END"
        )
        with self.assertRaisesRegex(
            ValueError, "graph_embeddings_changed_during_projection",
        ):
            index_builder.project_verified_structural_graph(
                self.connection, [document], [evidence], [relation()],
            )
        self.assertEqual(
            self.connection.execute(
                "SELECT vector_f32 FROM embeddings WHERE evidence_id = ?",
                (EVIDENCE_ID,),
            ).fetchone()[0],
            b"\x00\x00\x00\x00",
        )
        self.connection.execute("DROP TRIGGER mutate_indexed_embedding_vector")

        def reject_commit(
            action: int, argument_one: str | None, _argument_two: str | None,
            _database: str | None, _source: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_TRANSACTION and argument_one == "COMMIT":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.connection.set_authorizer(reject_commit)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                index_builder.project_verified_structural_graph(
                    self.connection, [document], [evidence], [relation()],
                )
        finally:
            self.connection.set_authorizer(None)
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0], 0,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0], 0,
        )

    def test_projection_holds_one_write_transaction_from_evidence_read_to_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "projection.sqlite3"
            projection_connection = sqlite3.connect(database_path)
            competing_connection = sqlite3.connect(database_path, timeout=0)
            try:
                index_builder.initialize(projection_connection)
                evidence = semantic_evidence()
                insert_indexed_evidence(projection_connection, evidence)
                projection_connection.commit()
                competing_results: list[str] = []

                def attempt_competing_write(statement: str) -> None:
                    if (
                        statement.startswith("SELECT evidence_id, document_id")
                        and not competing_results
                    ):
                        try:
                            competing_connection.execute(
                                "UPDATE evidence SET observed_text = '999' "
                                "WHERE evidence_id = ?",
                                (EVIDENCE_ID,),
                            )
                            competing_connection.commit()
                        except sqlite3.OperationalError as exc:
                            competing_connection.rollback()
                            competing_results.append(str(exc))

                projection_connection.set_trace_callback(attempt_competing_write)
                report = index_builder.project_verified_structural_graph(
                    projection_connection,
                    [semantic_document()],
                    [evidence],
                    [relation()],
                )
                projection_connection.set_trace_callback(None)

                self.assertEqual(report["edge_count"], 1)
                self.assertTrue(competing_results)
                self.assertIn("locked", competing_results[0].lower())
                self.assertEqual(
                    projection_connection.execute(
                        "SELECT observed_text FROM evidence WHERE evidence_id = ?",
                        (EVIDENCE_ID,),
                    ).fetchone()[0],
                    evidence["observed_text"],
                )
            finally:
                competing_connection.close()
                projection_connection.close()

    def test_projection_is_stable_when_input_order_changes(self) -> None:
        first = semantic_evidence()
        second = semantic_evidence(SECOND_EVIDENCE_ID, "勤務日 2026-08-31")
        document = semantic_document()
        document["evidence_ids"] = [EVIDENCE_ID, SECOND_EVIDENCE_ID]
        relations = [
            relation(),
            relation(target_id=SECOND_EVIDENCE_ID),
        ]

        results = []
        for evidence_order, relation_order in (
            ([first, second], relations),
            ([second, first], list(reversed(relations))),
        ):
            connection = sqlite3.connect(":memory:")
            try:
                index_builder.initialize(connection)
                for item in evidence_order:
                    insert_indexed_evidence(connection, item)
                report = index_builder.project_verified_structural_graph(
                    connection, [deepcopy(document)], evidence_order, relation_order,
                )
                rows = {
                    "nodes": connection.execute(
                        "SELECT * FROM graph_nodes ORDER BY node_id"
                    ).fetchall(),
                    "edges": connection.execute(
                        "SELECT * FROM graph_edges ORDER BY relation_id"
                    ).fetchall(),
                }
                results.append((report, rows))
            finally:
                connection.close()

        self.assertEqual(results[0], results[1])

    def test_dawn_sized_topology_keeps_all_relations_and_derived_isolates(self) -> None:
        evidence_records = [
            semantic_evidence(
                f"ev_{index + 1:032x}", f"Evidence {index + 1}",
            )
            for index in range(144)
        ]
        document = semantic_document()
        document["evidence_ids"] = [
            record["evidence_id"] for record in evidence_records
        ]
        relations = [
            relation(target_id=evidence_records[0]["evidence_id"]),
            relation(target_id=evidence_records[1]["evidence_id"]),
        ]
        relations.extend(
            relation(
                from_record_type="evidence",
                from_id=evidence_records[0]["evidence_id"],
                target_id=evidence_records[index]["evidence_id"],
            )
            for index in range(2, 101)
        )
        for record in evidence_records:
            insert_indexed_evidence(self.connection, record)

        report = index_builder.project_verified_structural_graph(
            self.connection, [document], evidence_records, relations,
        )

        self.assertEqual(report["document_node_count"], 1)
        self.assertEqual(report["evidence_node_count"], 144)
        self.assertEqual(report["node_count"], 145)
        self.assertEqual(report["edge_count"], 101)
        self.assertEqual(report["isolated_node_count"], 43)
        self.assertEqual(report["relation_input_count"], 101)
        self.assertEqual(
            report["skipped_edge_count_by_reason"],
            {"not_verified": 0, "non_structural": 0, "not_explicit": 0},
        )
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_built_index_declares_schema_only_graph_capability(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            evidence_path = base / "safe-answer-evidence.jsonl"
            documents_path = base / "semantic-documents.jsonl"
            security_path = base / "content-security-state.json"
            output_path = base / "safe-answer-index.sqlite3"
            evidence = {
                "evidence_id": "ev_one",
                "document_id": "doc_one",
                "locator": {"line": 1},
                "observed_text": "one",
            }
            document = {
                "document_id": "doc_one",
                "source": {"relative_path": "one.txt"},
            }
            evidence_path.write_text(canonical(evidence) + "\n", encoding="utf-8")
            documents_path.write_text(canonical(document) + "\n", encoding="utf-8")
            security = {
                "classifier": "deterministic_content_security_gate",
                "execution_policy": "never_execute",
                "question_independent": True,
                "policy_version": "test",
                "safe_answer_index_allowed": True,
                "outputs": {
                    evidence_path.name: {
                        "sha256": index_builder.sha256_file(evidence_path),
                    },
                },
            }
            security_path.write_text(canonical(security), encoding="utf-8")
            argv = [
                str(MODULE_PATH),
                "--evidence", str(evidence_path),
                "--documents", str(documents_path),
                "--output", str(output_path),
                "--security-state", str(security_path),
                "--index-purpose", "safe_answer",
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                index_builder, "embed", return_value=[[0.25, 0.75]],
            ), redirect_stdout(io.StringIO()):
                self.assertEqual(index_builder.main(), 0)

            connection = sqlite3.connect(output_path)
            try:
                metadata = {
                    key: json.loads(value)
                    for key, value in connection.execute("SELECT key, value FROM metadata")
                }
                self.assertEqual(metadata["schema_version"], "0.3")
                self.assertEqual(metadata["graph_schema_version"], "0.1")
                self.assertEqual(metadata["graph_status"], "schema_only")
                self.assertFalse(metadata["graph_retrieval_enabled"])
                self.assertEqual(metadata["graph_node_count"], 0)
                self.assertEqual(metadata["graph_edge_count"], 0)
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
