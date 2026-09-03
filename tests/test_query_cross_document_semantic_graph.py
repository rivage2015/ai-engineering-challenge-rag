from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = REPOSITORY_ROOT / "scripts" / "build_cross_document_semantic_graph.py"
QUERY_PATH = REPOSITORY_ROOT / "scripts" / "query_cross_document_semantic_graph.py"
DEFAULT_ADAPTER_ROOT = Path(
    "/private/tmp/cross-format-kg-v0.1-baseline/layer1-adapter"
)
ADAPTER_ROOT = Path(
    os.environ.get("CROSS_FORMAT_KG_LAYER1_ADAPTER_DIR", DEFAULT_ADAPTER_ROOT)
)

OWNER_2022_QUESTION = (
    "Project Orionの「移行リハーサル統括」は、2022年8月1日時点で"
    "誰が主担当でしたか。"
)
OWNER_2023_QUESTION = (
    "Project Orionの「移行リハーサル統括」は、2023年5月1日時点で"
    "誰が主担当でしたか。"
)
ASSIGNMENT_CHANGE_QUESTION = (
    "Project Orionの「移行リハーサル統括」で、主担当が切り替わった日と、"
    "変更前・変更後の担当者を答えてください。"
)
VERSION_CHANGE_QUESTION = (
    "Project Orionの「移行リハーサル統括」について、承認済みの担当変更理由と、"
    "旧案から何が変わったかを答えてください。"
)


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


builder = _load_module("test_target_cross_document_builder", BUILDER_PATH)
query = _load_module("test_target_cross_document_query", QUERY_PATH)


def _metadata_value(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _edge_rows(
    connection: sqlite3.Connection, relation_type: str
) -> list[tuple[str, dict[str, Any]]]:
    return [
        (edge_id, json.loads(properties_json))
        for edge_id, properties_json in connection.execute(
            "SELECT edge_id, properties_json FROM edges "
            "WHERE relation_type = ? ORDER BY edge_id",
            (relation_type,),
        )
    ]


def _rehash_edge(connection: sqlite3.Connection, edge_id: str) -> None:
    row = connection.execute(
        "SELECT edge_id, from_node_id, relation_type, to_node_id, "
        "relation_class, status, basis_kind, basis_rule, properties_json "
        "FROM edges WHERE edge_id = ?",
        (edge_id,),
    ).fetchone()
    if row is None:
        raise AssertionError(f"missing Edge under test: {edge_id}")
    supporting = [
        item[0]
        for item in connection.execute(
            "SELECT evidence_id FROM edge_evidence "
            "WHERE edge_id = ? ORDER BY evidence_id",
            (edge_id,),
        )
    ]
    payload = {
        "edge_id": row[0],
        "from_node_id": row[1],
        "relation_type": row[2],
        "to_node_id": row[3],
        "relation_class": row[4],
        "status": row[5],
        "basis_kind": row[6],
        "basis_rule": row[7],
        "properties": json.loads(row[8]),
        "supporting_evidence_ids": supporting,
    }
    connection.execute(
        "UPDATE edges SET record_sha256 = ? WHERE edge_id = ?",
        (query.sha256_value(payload), edge_id),
    )


def _rehash_node(connection: sqlite3.Connection, node_id: str) -> None:
    row = connection.execute(
        "SELECT node_id, node_type, canonical_key, status, properties_json "
        "FROM nodes WHERE node_id = ?",
        (node_id,),
    ).fetchone()
    if row is None:
        raise AssertionError(f"missing Node under test: {node_id}")
    payload = {
        "node_id": row[0],
        "node_type": row[1],
        "canonical_key": row[2],
        "status": row[3],
        "properties": json.loads(row[4]),
    }
    connection.execute(
        "UPDATE nodes SET record_sha256 = ? WHERE node_id = ?",
        (query.sha256_value(payload), node_id),
    )


def _rehash_snapshot(connection: sqlite3.Connection) -> None:
    logical = {
        "evidence_record_sha256": sorted(
            row[0]
            for row in connection.execute(
                "SELECT record_sha256 FROM source_evidence"
            )
        ),
        "node_record_sha256": sorted(
            row[0] for row in connection.execute("SELECT record_sha256 FROM nodes")
        ),
        "edge_record_sha256": sorted(
            row[0] for row in connection.execute("SELECT record_sha256 FROM edges")
        ),
    }
    logical_sha256 = query.sha256_value(logical)
    connection.execute(
        "UPDATE metadata SET value = ? WHERE key = 'logical_snapshot_sha256'",
        (_metadata_value(logical_sha256),),
    )
    connection.execute(
        "UPDATE metadata SET value = ? WHERE key = 'graph_snapshot_id'",
        (_metadata_value("xkgs_" + logical_sha256[:32]),),
    )


def _replace_edge_properties(
    connection: sqlite3.Connection,
    relation_type: str,
    predicate: Callable[[dict[str, Any]], bool],
    mutation: Callable[[dict[str, Any]], None],
) -> str:
    matches = [
        (edge_id, properties)
        for edge_id, properties in _edge_rows(connection, relation_type)
        if predicate(properties)
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected one {relation_type} Edge under test; got {len(matches)}"
        )
    edge_id, properties = matches[0]
    mutation(properties)
    connection.execute(
        "UPDATE edges SET properties_json = ? WHERE edge_id = ?",
        (_metadata_value(properties), edge_id),
    )
    _rehash_edge(connection, edge_id)
    return edge_id


def _facts(answer: dict[str, Any]) -> dict[str, str]:
    return {item["field"]: item["value"] for item in answer["asserted_facts"]}


class CrossDocumentSemanticGraphQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.documents = ADAPTER_ROOT / "semantic-documents.jsonl"
        cls.evidence = ADAPTER_ROOT / "safe-answer-evidence.jsonl"
        if not cls.documents.is_file() or not cls.evidence.is_file():
            raise unittest.SkipTest(
                "real safe Layer 1 outputs are unavailable; set "
                "CROSS_FORMAT_KG_LAYER1_ADAPTER_DIR"
            )
        cls._baseline_directory = tempfile.TemporaryDirectory()
        baseline_root = Path(cls._baseline_directory.name)
        cls.baseline_graph = baseline_root / "semantic-graph.sqlite3"
        builder.build(
            cls.documents,
            cls.evidence,
            cls.baseline_graph,
            baseline_root / "semantic-graph-state.json",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._baseline_directory.cleanup()

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _graph_copy(self, name: str) -> Path:
        target = self.root / f"{name}.sqlite3"
        shutil.copy2(self.baseline_graph, target)
        return target

    def _answer(self, graph: Path, question_text: str) -> dict[str, Any]:
        snapshot = query.GraphSnapshot.load(graph)
        return query.answer_question(snapshot, question_text)

    def test_current_claim_semantic_tampering_holds_even_with_valid_hashes(self) -> None:
        """Record/snapshot hash consistency must not imply semantic validity."""

        mutations: dict[str, Callable[[sqlite3.Connection], None]] = {}

        def status_mutation(connection: sqlite3.Connection) -> None:
            for relation_type in ("HAS_CURRENT_CLAIM", "CLAIMS_ASSIGNEE"):
                _replace_edge_properties(
                    connection,
                    relation_type,
                    lambda value: value.get("current") is True,
                    lambda value: value.__setitem__("claim_status", "DRAFT"),
                )

        def current_mutation(connection: sqlite3.Connection) -> None:
            for relation_type in ("HAS_CURRENT_CLAIM", "CLAIMS_ASSIGNEE"):
                _replace_edge_properties(
                    connection,
                    relation_type,
                    lambda value: value.get("current") is True,
                    lambda value: value.__setitem__("current", False),
                )

        def effective_from_mutation(connection: sqlite3.Connection) -> None:
            for relation_type in ("HAS_CURRENT_CLAIM", "CLAIMS_ASSIGNEE"):
                _replace_edge_properties(
                    connection,
                    relation_type,
                    lambda value: value.get("current") is True,
                    lambda value: value.__setitem__("effective_from", "not-a-date"),
                )

        def role_mutation(connection: sqlite3.Connection) -> None:
            _replace_edge_properties(
                connection,
                "CLAIMS_ASSIGNEE",
                lambda value: value.get("current") is True,
                lambda value: value.__setitem__("role", "副担当"),
            )

        mutations["status"] = status_mutation
        mutations["current"] = current_mutation
        mutations["effective_from"] = effective_from_mutation
        mutations["role"] = role_mutation

        for name, mutate in mutations.items():
            with self.subTest(property=name):
                graph = self._graph_copy(f"claim-{name}")
                with sqlite3.connect(graph) as connection:
                    mutate(connection)
                    _rehash_snapshot(connection)
                    connection.commit()

                # Loading proves that every changed record and snapshot hash is valid.
                answer = self._answer(graph, VERSION_CHANGE_QUESTION)
                self.assertEqual("HOLD", answer["decision"])
                self.assertEqual([], answer["asserted_facts"])
                self.assertEqual([], answer["asserted_relations"])

    def test_relation_endpoint_type_tampering_is_rejected_with_valid_hashes(self) -> None:
        graph = self._graph_copy("bad-endpoint-type")
        with sqlite3.connect(graph) as connection:
            row = connection.execute(
                "SELECT target.node_id "
                "FROM edges AS edge "
                "JOIN nodes AS target ON target.node_id = edge.to_node_id "
                "WHERE edge.relation_type = 'ASSIGNED_TO' "
                "ORDER BY edge.edge_id LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(row)
            node_id = row[0]
            connection.execute(
                "UPDATE nodes SET node_type = 'Project' WHERE node_id = ?",
                (node_id,),
            )
            _rehash_node(connection, node_id)
            _rehash_snapshot(connection)
            connection.commit()

        with self.assertRaises(query.GraphContractError):
            query.GraphSnapshot.load(graph)

    def test_fullwidth_date_and_japanese_paraphrases_select_expected_operation(self) -> None:
        cases = [
            (
                "fullwidth-date",
                "Project Orionの「移行リハーサル統括」は、"
                "２０２３年５月１日時点で誰が主担当でしたか。",
                "owner",
                {"reference_time": "2023-05-01", "assignee_id": "EMP-208"},
            ),
            (
                "casefold-project",
                "project orionの「移行リハーサル統括」は、"
                "2023年5月1日時点で誰が主担当でしたか。",
                "owner",
                {"reference_time": "2023-05-01", "assignee_id": "EMP-208"},
            ),
            (
                "handover-former-successor",
                "Project Orionの「移行リハーサル統括」で、"
                "主担当が交代した日と前任・後任を教えてください。",
                "assignment_change",
                {"change_effective_date": "2023-04-01"},
            ),
            (
                "when-changed",
                "Project Orionの「移行リハーサル統括」の"
                "主担当がいつ変わったか、変更前後の担当者を教えてください。",
                "assignment_change",
                {"change_effective_date": "2023-04-01"},
            ),
            (
                "old-version-background",
                "Project Orionの「移行リハーサル統括」で、"
                "旧版から何が変わったのか、担当変更の背景も教えてください。",
                "version_change",
                {"current_plan_status": "APPROVED"},
            ),
        ]

        for name, question_text, operation, expected_facts in cases:
            with self.subTest(paraphrase=name):
                answer = self._answer(self.baseline_graph, question_text)
                self.assertEqual("ACCEPTED", answer["decision"])
                self.assertEqual(operation, answer["operation"])
                facts = _facts(answer)
                for field, expected in expected_facts.items():
                    self.assertEqual(expected, facts.get(field))

    def test_evidence_path_and_locator_tampering_without_hash_update_is_rejected(self) -> None:
        mutations = {
            "path": (
                "UPDATE source_evidence SET relative_path = relative_path || '.tampered' "
                "WHERE evidence_id = (SELECT evidence_id FROM source_evidence "
                "ORDER BY evidence_id LIMIT 1)",
                (),
            ),
            "locator": (
                "UPDATE source_evidence SET locator_json = ? "
                "WHERE evidence_id = (SELECT evidence_id FROM source_evidence "
                "ORDER BY evidence_id LIMIT 1)",
                (_metadata_value({"tampered": True}),),
            ),
        }
        for name, (statement, parameters) in mutations.items():
            with self.subTest(field=name):
                graph = self._graph_copy(f"evidence-{name}")
                with sqlite3.connect(graph) as connection:
                    connection.execute(statement, parameters)
                    connection.commit()
                with self.assertRaisesRegex(
                    query.GraphContractError, "source Evidence record hash mismatch"
                ):
                    query.GraphSnapshot.load(graph)

    def test_missing_first_valid_from_holds_after_self_consistent_rehash(self) -> None:
        graph = self._graph_copy("missing-valid-from")
        with sqlite3.connect(graph) as connection:
            assignments = sorted(
                _edge_rows(connection, "ASSIGNED_TO"),
                key=lambda item: str(item[1].get("valid_from", "")),
            )
            self.assertGreaterEqual(len(assignments), 2)
            edge_id, properties = assignments[0]
            self.assertIn("valid_from", properties)
            del properties["valid_from"]
            connection.execute(
                "UPDATE edges SET properties_json = ? WHERE edge_id = ?",
                (_metadata_value(properties), edge_id),
            )
            _rehash_edge(connection, edge_id)
            _rehash_snapshot(connection)
            connection.commit()

        answer = self._answer(graph, ASSIGNMENT_CHANGE_QUESTION)
        self.assertEqual("HOLD", answer["decision"])
        self.assertEqual("assignment_period_incomplete", answer["reason_code"])
        self.assertEqual([], answer["asserted_facts"])

    def test_exclusive_boundaries_are_normalized_before_temporal_resolution(self) -> None:
        def old_exclusive_end(connection: sqlite3.Connection) -> None:
            assignments = _edge_rows(connection, "ASSIGNED_TO")
            old_id, old_properties = min(
                assignments, key=lambda item: str(item[1].get("valid_from", ""))
            )
            old_properties["valid_to"] = "2023-04-01"
            old_properties["valid_to_inclusive"] = False
            connection.execute(
                "UPDATE edges SET properties_json = ? WHERE edge_id = ?",
                (_metadata_value(old_properties), old_id),
            )
            _rehash_edge(connection, old_id)

        def new_exclusive_start(connection: sqlite3.Connection) -> None:
            assignments = _edge_rows(connection, "ASSIGNED_TO")
            new_id, new_properties = max(
                assignments, key=lambda item: str(item[1].get("valid_from", ""))
            )
            new_properties["valid_from"] = "2023-03-31"
            new_properties["valid_from_inclusive"] = False
            connection.execute(
                "UPDATE edges SET properties_json = ? WHERE edge_id = ?",
                (_metadata_value(new_properties), new_id),
            )
            _rehash_edge(connection, new_id)

        for name, mutate in (
            ("old-exclusive-end", old_exclusive_end),
            ("new-exclusive-start", new_exclusive_start),
        ):
            with self.subTest(boundary=name):
                graph = self._graph_copy(name)
                with sqlite3.connect(graph) as connection:
                    mutate(connection)
                    _rehash_snapshot(connection)
                    connection.commit()

                change = self._answer(graph, ASSIGNMENT_CHANGE_QUESTION)
                self.assertEqual("ACCEPTED", change["decision"])
                self.assertEqual(
                    "2023-04-01", _facts(change).get("change_effective_date")
                )
                self.assertEqual(
                    "2023-03-31", _facts(change).get("previous_valid_to")
                )
                before = self._answer(
                    graph,
                    OWNER_2022_QUESTION.replace("2022年8月1日", "2023年3月31日"),
                )
                after = self._answer(
                    graph,
                    OWNER_2023_QUESTION.replace("2023年5月1日", "2023年4月1日"),
                )
                self.assertEqual("EMP-104", _facts(before).get("assignee_id"))
                self.assertEqual("EMP-208", _facts(after).get("assignee_id"))


if __name__ == "__main__":
    unittest.main()
