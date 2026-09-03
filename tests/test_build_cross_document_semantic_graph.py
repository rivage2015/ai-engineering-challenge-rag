from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = REPOSITORY_ROOT / "scripts" / "build_cross_document_semantic_graph.py"
DEFAULT_ADAPTER_ROOT = Path(
    "/private/tmp/cross-format-kg-v0.1-baseline/layer1-adapter"
)
ADAPTER_ROOT = Path(os.environ.get("CROSS_FORMAT_KG_LAYER1_ADAPTER_DIR", DEFAULT_ADAPTER_ROOT))
EXPECTED_GRAPH = (
    REPOSITORY_ROOT
    / "evaluation"
    / "cross-format-kg-v0.1"
    / "gold"
    / "expected-graph.jsonl"
)


def _load_builder() -> Any:
    spec = importlib.util.spec_from_file_location(
        "test_target_build_cross_document_semantic_graph", BUILDER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


builder = _load_builder()


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


def _metadata(connection: sqlite3.Connection) -> dict[str, Any]:
    return {
        key: json.loads(value)
        for key, value in connection.execute("SELECT key, value FROM metadata")
    }


def _edge_tuples(connection: sqlite3.Connection) -> set[tuple[Any, ...]]:
    return {
        (
            from_type,
            from_key,
            relation_type,
            to_type,
            to_key,
            relation_class,
            status,
            basis_kind,
            properties_json,
        )
        for (
            from_type,
            from_key,
            relation_type,
            to_type,
            to_key,
            relation_class,
            status,
            basis_kind,
            properties_json,
        ) in connection.execute(
            """
            SELECT source.node_type, source.canonical_key, edge.relation_type,
                   target.node_type, target.canonical_key, edge.relation_class,
                   edge.status, edge.basis_kind, edge.properties_json
            FROM edges AS edge
            JOIN nodes AS source ON source.node_id = edge.from_node_id
            JOIN nodes AS target ON target.node_id = edge.to_node_id
            """
        )
    }


class CrossDocumentSemanticGraphBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.documents = ADAPTER_ROOT / "semantic-documents.jsonl"
        cls.evidence = ADAPTER_ROOT / "safe-answer-evidence.jsonl"

    def _require_real_inputs(self) -> None:
        if not self.documents.is_file() or not self.evidence.is_file():
            self.skipTest(
                "real Layer 1 adapter outputs are unavailable; set "
                "CROSS_FORMAT_KG_LAYER1_ADAPTER_DIR"
            )

    def _build(self, directory: Path, documents: Path | None = None, evidence: Path | None = None):
        output = directory / "graph.sqlite3"
        state_path = directory / "state.json"
        state = builder.build(
            documents or self.documents,
            evidence or self.evidence,
            output,
            state_path,
        )
        return state, output, state_path

    def test_real_safe_adapter_outputs_contain_all_fourteen_gold_tuples_and_hashes(self) -> None:
        self._require_real_inputs()
        with tempfile.TemporaryDirectory() as raw_directory:
            state, database_path, _ = self._build(Path(raw_directory))
            with closing(sqlite3.connect(database_path)) as connection:
                actual = _edge_tuples(connection)

                # The builder has already completed before the evaluator opens gold.
                expected_records = _jsonl(EXPECTED_GRAPH)
                expected = {
                    (
                        record["from"]["node_type"],
                        record["from"]["canonical_key"],
                        record["relation_type"],
                        record["to"]["node_type"],
                        record["to"]["canonical_key"],
                        record["relation_class"],
                        record["expected_status"],
                        record["basis_kind"],
                        builder.canonical_json(record["properties"]),
                    )
                    for record in expected_records
                }
                self.assertEqual(14, len(expected))
                self.assertEqual(set(), expected - actual)
                self.assertEqual(
                    {"verified"},
                    {row[0] for row in connection.execute("SELECT DISTINCT status FROM nodes")},
                )
                self.assertEqual(
                    {"verified"},
                    {row[0] for row in connection.execute("SELECT DISTINCT status FROM edges")},
                )

                evidence_hashes: list[str] = []
                for row in connection.execute(
                    """
                    SELECT evidence_id, document_id, relative_path, source_sha256,
                           locator_json, observed_text, observed_sha256, record_sha256
                    FROM source_evidence
                    """
                ):
                    (
                        evidence_id,
                        document_id,
                        relative_path,
                        source_sha256,
                        locator_json,
                        observed_text,
                        observed_sha256,
                        record_sha256,
                    ) = row
                    self.assertEqual(
                        hashlib.sha256(observed_text.encode("utf-8")).hexdigest(),
                        observed_sha256,
                    )
                    payload = {
                        "evidence_id": evidence_id,
                        "document_id": document_id,
                        "relative_path": relative_path,
                        "source_sha256": source_sha256,
                        "locator": json.loads(locator_json),
                        "observed_text": observed_text,
                        "observed_sha256": observed_sha256,
                    }
                    self.assertEqual(builder.sha256_json(payload), record_sha256)
                    evidence_hashes.append(record_sha256)

                node_hashes: list[str] = []
                for row in connection.execute(
                    """
                    SELECT node_id, node_type, canonical_key, status,
                           properties_json, record_sha256
                    FROM nodes
                    """
                ):
                    node_id, node_type, canonical_key, status_value, properties_json, record_hash = row
                    payload = {
                        "node_id": node_id,
                        "node_type": node_type,
                        "canonical_key": canonical_key,
                        "status": status_value,
                        "properties": json.loads(properties_json),
                    }
                    self.assertEqual(builder.sha256_json(payload), record_hash)
                    node_hashes.append(record_hash)

                edge_hashes: list[str] = []
                for row in connection.execute(
                    """
                    SELECT edge_id, from_node_id, relation_type, to_node_id,
                           relation_class, status, basis_kind, basis_rule,
                           properties_json, record_sha256
                    FROM edges
                    """
                ):
                    (
                        edge_id,
                        from_node_id,
                        relation_type,
                        to_node_id,
                        relation_class,
                        status_value,
                        basis_kind,
                        basis_rule,
                        properties_json,
                        record_hash,
                    ) = row
                    supporting = [
                        item[0]
                        for item in connection.execute(
                            "SELECT evidence_id FROM edge_evidence WHERE edge_id = ? ORDER BY evidence_id",
                            (edge_id,),
                        )
                    ]
                    identity = {
                        "from_node_id": from_node_id,
                        "relation_type": relation_type,
                        "to_node_id": to_node_id,
                        "relation_class": relation_class,
                        "status": status_value,
                        "basis_kind": basis_kind,
                        "basis_rule": basis_rule,
                        "properties": json.loads(properties_json),
                        "supporting_evidence_ids": supporting,
                    }
                    self.assertEqual(
                        "edge_" + builder.sha256_json(identity)[:32],
                        edge_id,
                    )
                    payload = {"edge_id": edge_id, **identity}
                    self.assertEqual(builder.sha256_json(payload), record_hash)
                    edge_hashes.append(record_hash)

                logical_payload = {
                    "evidence_record_sha256": sorted(evidence_hashes),
                    "node_record_sha256": sorted(node_hashes),
                    "edge_record_sha256": sorted(edge_hashes),
                }
                logical_hash = builder.sha256_json(logical_payload)
                metadata = _metadata(connection)
                self.assertEqual(logical_hash, metadata["logical_snapshot_sha256"])
                self.assertEqual("xkgs_" + logical_hash[:32], metadata["graph_snapshot_id"])
                self.assertEqual(metadata["graph_snapshot_id"], state["graph_snapshot_id"])

    def test_source_rename_and_observed_value_change_are_reflected_without_rules_changes(self) -> None:
        self._require_real_inputs()
        with tempfile.TemporaryDirectory() as raw_directory:
            root = Path(raw_directory)
            baseline_root = root / "baseline"
            baseline_root.mkdir()
            baseline_state, baseline_database, _ = self._build(baseline_root)
            with closing(sqlite3.connect(baseline_database)) as connection:
                employee_key, person_key, document_id = connection.execute(
                    """
                    SELECT source.canonical_key, target.canonical_key, evidence.document_id
                    FROM edges AS edge
                    JOIN nodes AS source ON source.node_id = edge.from_node_id
                    JOIN nodes AS target ON target.node_id = edge.to_node_id
                    JOIN edge_evidence AS support ON support.edge_id = edge.edge_id
                    JOIN source_evidence AS evidence ON evidence.evidence_id = support.evidence_id
                    WHERE edge.relation_type = 'IDENTIFIES_PERSON'
                    ORDER BY source.canonical_key, evidence.evidence_id
                    LIMIT 1
                    """
                ).fetchone()

            changed_root = root / "changed"
            changed_root.mkdir()
            documents_path = changed_root / "semantic-documents.jsonl"
            evidence_path = changed_root / "safe-answer-evidence.jsonl"
            document_records = _jsonl(self.documents)
            evidence_records = _jsonl(self.evidence)
            selected_document = next(
                record for record in document_records if record["document_id"] == document_id
            )
            old_relative_path = selected_document["source"]["relative_path"]
            new_relative_path = "renamed-source/identity-register.pdf"
            new_person_key = person_key + "（更新）"
            new_source_hash = hashlib.sha256(
                (selected_document["source"]["sha256"] + new_relative_path + new_person_key).encode("utf-8")
            ).hexdigest()
            selected_document["source"]["relative_path"] = new_relative_path
            selected_document["source"]["sha256"] = new_source_hash
            selected_document["source"].pop("absolute_path", None)
            changed_values = 0
            for record in evidence_records:
                if record["document_id"] != document_id:
                    continue
                record["source"]["relative_path"] = new_relative_path
                record["source"]["sha256"] = new_source_hash
                if person_key in record["observed_text"]:
                    record["observed_text"] = record["observed_text"].replace(
                        person_key, new_person_key
                    )
                    changed_values += 1
            self.assertGreaterEqual(changed_values, 1)
            self.assertNotEqual(old_relative_path, new_relative_path)
            _write_jsonl(documents_path, document_records)
            _write_jsonl(evidence_path, evidence_records)

            changed_state, changed_database, _ = self._build(
                changed_root, documents_path, evidence_path
            )
            with closing(sqlite3.connect(changed_database)) as connection:
                targets = {
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT target.canonical_key
                        FROM edges AS edge
                        JOIN nodes AS source ON source.node_id = edge.from_node_id
                        JOIN nodes AS target ON target.node_id = edge.to_node_id
                        WHERE edge.relation_type = 'IDENTIFIES_PERSON'
                          AND source.canonical_key = ?
                        """,
                        (employee_key,),
                    )
                }
                self.assertIn(new_person_key, targets)
                self.assertNotIn(person_key, targets)
                paths = {
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT relative_path FROM source_evidence WHERE document_id = ?",
                        (document_id,),
                    )
                }
                self.assertEqual({new_relative_path}, paths)
            self.assertNotEqual(
                baseline_state["graph_snapshot_id"], changed_state["graph_snapshot_id"]
            )

    def test_pdf_identity_table_uses_order_fallback_when_geometry_is_unavailable(self) -> None:
        self._require_real_inputs()
        with tempfile.TemporaryDirectory() as raw_directory:
            root = Path(raw_directory)
            coordinate_root = root / "coordinate"
            coordinate_root.mkdir()
            coordinate_state, coordinate_database, _ = self._build(coordinate_root)
            with closing(sqlite3.connect(coordinate_database)) as connection:
                coordinate_identities = {
                    row[:2]
                    for row in connection.execute(
                        """
                        SELECT source.canonical_key, target.canonical_key
                        FROM edges AS edge
                        JOIN nodes AS source ON source.node_id = edge.from_node_id
                        JOIN nodes AS target ON target.node_id = edge.to_node_id
                        WHERE edge.relation_type = 'IDENTIFIES_PERSON'
                        """
                    )
                }

            fallback_root = root / "fallback"
            fallback_root.mkdir()
            documents_path = fallback_root / "semantic-documents.jsonl"
            evidence_path = fallback_root / "safe-answer-evidence.jsonl"
            document_records = _jsonl(self.documents)
            pdf_document_ids = {
                record["document_id"]
                for record in document_records
                if record["source"].get("file_type") == "pdf"
                or record["source"]["relative_path"].casefold().endswith(".pdf")
            }
            self.assertTrue(pdf_document_ids)
            evidence_records = _jsonl(self.evidence)
            removed = 0
            for record in evidence_records:
                if record["document_id"] in pdf_document_ids and "geometry" in record:
                    del record["geometry"]
                    removed += 1
            self.assertGreater(removed, 0)
            _write_jsonl(documents_path, document_records)
            _write_jsonl(evidence_path, evidence_records)
            fallback_state, fallback_database, _ = self._build(
                fallback_root, documents_path, evidence_path
            )
            with closing(sqlite3.connect(fallback_database)) as connection:
                fallback_identities = {
                    row[:2]
                    for row in connection.execute(
                        """
                        SELECT source.canonical_key, target.canonical_key
                        FROM edges AS edge
                        JOIN nodes AS source ON source.node_id = edge.from_node_id
                        JOIN nodes AS target ON target.node_id = edge.to_node_id
                        WHERE edge.relation_type = 'IDENTIFIES_PERSON'
                        """
                    )
                }
            self.assertEqual(coordinate_identities, fallback_identities)
            self.assertEqual(2, coordinate_state["counts"]["pdf_coordinate_rows"])
            self.assertEqual(2, fallback_state["counts"]["pdf_order_fallback"])

    def test_cli_contract_is_question_independent_and_rejects_non_safe_input_name(self) -> None:
        self.assertEqual(
            ["documents_path", "evidence_path", "output_path", "state_path"],
            list(inspect.signature(builder.build).parameters),
        )
        with tempfile.TemporaryDirectory() as raw_directory:
            root = Path(raw_directory)
            documents = root / "semantic-documents.jsonl"
            documents.write_text("{}\n", encoding="utf-8")
            wrongly_named = root / "semantic-evidence.jsonl"
            wrongly_named.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError, "evidence_input_must_be_safe_answer_evidence_jsonl"
            ):
                builder.build(
                    documents,
                    wrongly_named,
                    root / "graph.sqlite3",
                    root / "state.json",
                )

            source = {"relative_path": "sample/document.txt", "sha256": "a" * 64}
            _write_jsonl(
                documents,
                [{
                    "document_id": "doc_sample",
                    "source": source,
                    "evidence_ids": ["ev_sample"],
                    "status": "extracted",
                }],
            )
            unsafe_evidence = root / "safe-answer-evidence.jsonl"
            _write_jsonl(
                unsafe_evidence,
                [{
                    "evidence_id": "ev_sample",
                    "document_id": "doc_sample",
                    "source": source,
                    "locator": {"paragraph_index": 1},
                    "observed_text": "sample",
                    "adapter": {
                        "execution_policy": "execute",
                        "source_record_type": "paragraph",
                    },
                    "status": "observed",
                }],
            )
            with self.assertRaisesRegex(ValueError, "evidence_execution_policy_invalid"):
                builder.build(
                    documents,
                    unsafe_evidence,
                    root / "unsafe.sqlite3",
                    root / "unsafe-state.json",
                )


if __name__ == "__main__":
    unittest.main()
