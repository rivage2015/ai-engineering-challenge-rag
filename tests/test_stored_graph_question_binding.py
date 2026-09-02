from __future__ import annotations

import copy
import hashlib
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "distribution" / "macos-local-memory" / "engine"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


qeg = load_module(
    "stored_graph_question_binding_qeg",
    ENGINE / "question_evidence_graph.py",
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def record(
    evidence_id: str,
    locator: dict,
    text: str,
    document_id: str = "doc_1",
) -> dict:
    return {
        "evidence_id": evidence_id,
        "document_id": document_id,
        "relative_path": "2026.08_work-report.xlsx",
        "locator": locator,
        "text": text,
    }


def evidence_fixture(
    first: int = 2,
    second: int = 1,
    saved: int | None = None,
) -> list[dict]:
    total = first + second if saved is None else saved
    return [
        record(
            "ev_header",
            {"sheet_name": "集計", "row_index": 1},
            "A: 日付\nB: 作業枠",
        ),
        record(
            "ev_r2",
            {"sheet_name": "集計", "row_index": 2},
            f"日付: 1\n作業枠: {first}",
        ),
        record(
            "ev_b2",
            {"sheet_name": "集計", "cell": "B2"},
            str(first),
        ),
        record(
            "ev_r3",
            {"sheet_name": "集計", "row_index": 3},
            "日付: 2",
        ),
        record(
            "ev_r4",
            {"sheet_name": "集計", "row_index": 4},
            f"日付: 3\n作業枠: {second}",
        ),
        record(
            "ev_b4",
            {"sheet_name": "集計", "cell": "B4"},
            str(second),
        ),
        record(
            "ev_total_row",
            {"sheet_name": "集計", "row_index": 5},
            (
                "日付: 合計\n"
                f"作業枠: =SUM(B2:B4) "
                f"[保存値・ファイル保存時・未再計算: {total}]"
            ),
        ),
        record(
            "ev_total_value",
            {"sheet_name": "集計", "cell": "B5"},
            str(total),
        ),
        record(
            "ev_total_formula",
            {"sheet_name": "集計", "cell": "B5"},
            "=SUM(B2:B4)",
        ),
        record(
            "ev_reference",
            {"sheet_name": "報告書", "row_index": 7},
            (
                "B: 作業枠\n"
                f"C: =集計!B5 "
                f"[保存値・ファイル保存時・未再計算: {total}]\n"
                "D: 枠"
            ),
        ),
    ]


def source_graph(records: list[dict]) -> dict:
    document_id = records[0]["document_id"]
    nodes = [{
        "node_id": document_id,
        "node_type": "document",
        "status": "observed",
        "record_sha256": digest(f"node:{document_id}"),
    }]
    nodes.extend({
        "node_id": item["evidence_id"],
        "node_type": "evidence",
        "status": "observed",
        "record_sha256": digest(f"node:{item['evidence_id']}"),
    } for item in records)

    def edge(
        relation_id: str,
        source: str,
        relation_type: str,
        target: str,
        relation_class: str,
        basis_rule: str,
    ) -> dict:
        return {
            "relation_id": relation_id,
            "from_node_id": source,
            "relation_type": relation_type,
            "to_node_id": target,
            "relation_class": relation_class,
            "basis_kind": "explicit",
            "basis_rule": basis_rule,
            "status": "verified",
            "record_sha256": digest(f"edge:{relation_id}"),
        }

    contained = (
        "ev_header", "ev_b2", "ev_b4", "ev_total_value",
        "ev_total_formula", "ev_reference",
    )
    edges = [
        edge(
            f"rel_doc_contains_{evidence_id}",
            document_id,
            "contains",
            evidence_id,
            "structural",
            "native containment",
        )
        for evidence_id in contained
    ]
    for evidence_id in ("ev_r2", "ev_r3", "ev_r4", "ev_total_row"):
        relation_id = f"rel_{evidence_id}_derived_from_header"
        edges.append({
            **edge(
                relation_id,
                evidence_id,
                "derived_from",
                "ev_header",
                "lineage",
                "validated SearchUnit lineage",
            )
        })
    edges.extend([
        edge(
            "rel_ev_r2_derived_from_b2", "ev_r2", "derived_from", "ev_b2",
            "lineage", "validated SearchUnit lineage",
        ),
        edge(
            "rel_ev_r4_derived_from_b4", "ev_r4", "derived_from", "ev_b4",
            "lineage", "validated SearchUnit lineage",
        ),
        edge(
            "rel_total_derived_from_formula", "ev_total_row", "derived_from",
            "ev_total_formula", "lineage", "validated SearchUnit lineage",
        ),
        edge(
            "rel_total_derived_from_saved", "ev_total_row", "derived_from",
            "ev_total_value", "lineage", "validated SearchUnit lineage",
        ),
    ])

    return {
        "graph_schema_version": "0.1",
        "graph_sha256": "a" * 64,
        "partition_sha256": "b" * 64,
        "eligible_evidence_set_sha256": "c" * 64,
        "nodes": nodes,
        "edges": edges,
    }


class StoredGraphQuestionBindingTests(unittest.TestCase):
    QUESTION = "2026年8月の作業枠は何回ありましたか？"

    def test_ready_values_are_not_fixed_and_bind_stored_relation_ids(self) -> None:
        three_records = evidence_fixture(2, 1)
        three_graph = source_graph(three_records)
        three = qeg.build_question_evidence_graph(
            self.QUESTION,
            three_records,
            source_graph=three_graph,
        )

        five_records = evidence_fixture(2, 3)
        five_graph = source_graph(five_records)
        five = qeg.build_question_evidence_graph(
            self.QUESTION,
            five_records,
            source_graph=five_graph,
        )

        self.assertEqual(
            (three["status"], three["selection"]["value"]),
            ("ready", "3"),
        )
        self.assertEqual(
            (five["status"], five["selection"]["value"]),
            ("ready", "5"),
        )
        self.assertEqual(
            qeg.validate_question_evidence_graph(
                self.QUESTION,
                three_records,
                three,
                source_graph=three_graph,
            )["status"],
            "pass",
        )
        self.assertEqual(
            qeg.validate_question_evidence_graph(
                self.QUESTION,
                five_records,
                five,
                source_graph=five_graph,
            )["status"],
            "pass",
        )

        binding = three["stored_graph_binding"]
        self.assertEqual(binding["graph_sha256"], three_graph["graph_sha256"])
        self.assertEqual(
            binding["eligible_evidence_set_sha256"],
            three_graph["eligible_evidence_set_sha256"],
        )
        self.assertIn(
            "rel_doc_contains_ev_header",
            binding["traversed_relation_ids"],
        )
        self.assertIn(
            "rel_ev_total_row_derived_from_header",
            binding["traversed_relation_ids"],
        )
        self.assertTrue(
            set(binding["traversed_relation_ids"])
            <= {edge["relation_id"] for edge in three_graph["edges"]}
        )
        edge_pairs = {
            (edge["source"], edge["target"])
            for edge in three["edges"]
        }
        self.assertTrue(all(
            pair in edge_pairs
            for pair in zip(three["primary_path"], three["primary_path"][1:])
        ))

    def test_missing_required_row_lineage_edge_holds(self) -> None:
        records = evidence_fixture()
        graph = source_graph(records)
        graph["edges"] = [
            edge
            for edge in graph["edges"]
            if edge["relation_id"] != "rel_ev_r3_derived_from_header"
        ]

        artifact = qeg.build_question_evidence_graph(
            self.QUESTION,
            records,
            source_graph=graph,
        )

        self.assertEqual(artifact["status"], "hold")

    def test_blank_row_with_reachable_raw_cell_but_no_lineage_holds(self) -> None:
        records = evidence_fixture(2, 0)
        for item in records:
            if item["evidence_id"] == "ev_r4":
                item["text"] = "日付: 3"
        graph = source_graph(records)
        graph["edges"] = [
            edge for edge in graph["edges"]
            if edge["relation_id"] != "rel_ev_r4_derived_from_b4"
        ]

        artifact = qeg.build_question_evidence_graph(
            self.QUESTION,
            records,
            source_graph=graph,
        )

        self.assertEqual(
            (artifact["status"], artifact["reason"]),
            ("hold", "stored_graph_lineage_failed"),
        )
        self.assertEqual(
            artifact["audit"][0]["details"]["code"],
            "blank_row_has_target_source",
        )

    def test_blank_row_with_unreachable_raw_cell_holds(self) -> None:
        records = evidence_fixture()
        records.append(record(
            "ev_b3",
            {"sheet_name": "集計", "cell": "B3"},
            "9",
        ))
        graph = source_graph(records)

        artifact = qeg.build_question_evidence_graph(
            self.QUESTION,
            records,
            source_graph=graph,
        )

        self.assertEqual(
            (artifact["status"], artifact["reason"]),
            ("hold", "stored_graph_lineage_failed"),
        )
        self.assertEqual(
            artifact["audit"][0]["details"]["code"],
            "blank_row_has_target_source",
        )

    def test_plain_containment_cannot_replace_required_lineage(self) -> None:
        records = evidence_fixture()
        graph = source_graph(records)
        document_id = records[0]["document_id"]
        graph["edges"] = [
            {
                "relation_id": f"rel_direct_{item['evidence_id']}",
                "from_node_id": document_id,
                "relation_type": "contains",
                "to_node_id": item["evidence_id"],
                "relation_class": "structural",
                "basis_kind": "explicit",
                "basis_rule": "native containment",
                "status": "verified",
                "record_sha256": digest(f"direct:{item['evidence_id']}"),
            }
            for item in records
        ]

        artifact = qeg.build_question_evidence_graph(
            self.QUESTION, records, source_graph=graph
        )

        self.assertEqual(
            (artifact["status"], artifact["reason"]),
            ("hold", "stored_graph_lineage_failed"),
        )

    def test_unrelated_unreachable_document_does_not_block_valid_candidate(self) -> None:
        records = evidence_fixture()
        graph = source_graph(records)
        unrelated = record(
            "ev_unrelated",
            {"sheet_name": "別紙", "cell": "A1"},
            "無関係な安全Evidence",
            document_id="doc_2",
        )
        records.append(unrelated)
        graph["nodes"].extend([
            {
                "node_id": "doc_2",
                "node_type": "document",
                "status": "observed",
                "record_sha256": digest("node:doc_2"),
            },
            {
                "node_id": unrelated["evidence_id"],
                "node_type": "evidence",
                "status": "observed",
                "record_sha256": digest("node:ev_unrelated"),
            },
        ])

        artifact = qeg.build_question_evidence_graph(
            self.QUESTION, records, source_graph=graph
        )

        self.assertEqual(
            (artifact["status"], artifact["selection"]["value"]),
            ("ready", "3"),
        )

    def test_rehashed_relation_binding_tamper_is_blocked(self) -> None:
        records = evidence_fixture()
        graph = source_graph(records)
        artifact = qeg.build_question_evidence_graph(
            self.QUESTION,
            records,
            source_graph=graph,
        )
        self.assertEqual(artifact["status"], "ready")

        tampered = copy.deepcopy(artifact)
        tampered["stored_graph_binding"]["traversed_relation_ids"][0] = (
            "rel_forged"
        )
        body = {
            key: value
            for key, value in tampered.items()
            if key not in {"artifact_hash", "artifact_id"}
        }
        artifact_hash = qeg.stable_hash(body)
        tampered["artifact_hash"] = artifact_hash
        tampered["artifact_id"] = f"qeg_{artifact_hash[:24]}"

        validation = qeg.validate_question_evidence_graph(
            self.QUESTION,
            records,
            tampered,
            source_graph=graph,
        )

        self.assertEqual(validation["status"], "blocked")

    def test_primary_path_must_be_backed_by_actual_edges(self) -> None:
        records = evidence_fixture()
        graph = source_graph(records)
        artifact = qeg.build_question_evidence_graph(
            self.QUESTION,
            records,
            source_graph=graph,
        )
        tampered = copy.deepcopy(artifact)
        tampered["edges"] = [
            edge for edge in tampered["edges"]
            if edge["edge_id"] != "edge_range_recomputed_as_value"
        ]
        body = {
            key: value
            for key, value in tampered.items()
            if key not in {"artifact_hash", "artifact_id"}
        }
        artifact_hash = qeg.stable_hash(body)
        tampered["artifact_hash"] = artifact_hash
        tampered["artifact_id"] = f"qeg_{artifact_hash[:24]}"

        validation = qeg.validate_question_evidence_graph(
            self.QUESTION,
            records,
            tampered,
            source_graph=graph,
        )

        self.assertEqual(validation["status"], "blocked")
        self.assertIn(
            "primary_path_edge_missing",
            {failure["code"] for failure in validation["failures"]},
        )

    def test_non_count_question_keeps_normal_retrieval_path(self) -> None:
        records = evidence_fixture()
        graph = source_graph(records)
        question = "作業内容を教えてください。"
        artifact = qeg.build_question_evidence_graph(
            question, records, source_graph=graph
        )
        validation = qeg.validate_question_evidence_graph(
            question, records, artifact, source_graph=graph
        )

        self.assertEqual(artifact["status"], "unsupported")
        self.assertEqual(validation["status"], "not_applicable")


if __name__ == "__main__":
    unittest.main()
