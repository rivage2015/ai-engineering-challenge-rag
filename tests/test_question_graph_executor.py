from __future__ import annotations

import copy
import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "distribution" / "macos-local-memory" / "engine"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


answer = load_module(
    "answer_local_memory_v2_question_graph_executor_test",
    ENGINE / "answer_local_memory_v2.py",
)


def evidence(evidence_id: str, text: str) -> dict:
    return {
        "evidence_id": evidence_id,
        "document_id": "doc_1",
        "relative_path": "project-plan.xlsx",
        "locator": {"sheet_name": "Plan", "cell": evidence_id},
        "text": text,
    }


class QuestionGraphExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records = {
            "ev_row": evidence(
                "ev_row", "Owner: Aki\nReview Date: 2026-09-10"
            ),
            "ev_owner": evidence("ev_owner", "Aki"),
            "ev_date": evidence("ev_date", "2026-09-10"),
        }
        self.lookup_artifact = {
            "artifact_id": "qeg_lookup",
            "status": "ready",
            "intent": {"operation": "record_lookup"},
            "selected_evidence_ids": ["ev_row", "ev_owner", "ev_date"],
            "branches": [
                {
                    "item_id": "F1",
                    "branch_id": "branch_owner",
                    "value": "Aki",
                    "selected_evidence_ids": ["ev_row", "ev_owner"],
                    "stored_graph_binding": {
                        "structured_record_lookup_lineage": {
                            "field": {"value_evidence_id": "ev_owner"},
                        },
                    },
                },
                {
                    "item_id": "F2",
                    "branch_id": "branch_date",
                    "value": "2026-09-10",
                    "selected_evidence_ids": ["ev_row", "ev_date"],
                    "stored_graph_binding": {
                        "structured_record_lookup_lineage": {
                            "field": {"value_evidence_id": "ev_date"},
                        },
                    },
                },
            ],
        }
        self.validation = {"status": "pass"}

    def test_record_lookup_prepends_only_the_matching_branch(self) -> None:
        vector_hit = {**evidence("ev_vector", "unrelated"), "score": 0.5}
        augmented, selected = answer.augment_with_question_graph(
            [vector_hit], self.records, self.lookup_artifact, self.validation,
            item_id="F1",
        )

        self.assertEqual(selected, ["ev_row", "ev_owner"])
        self.assertEqual(
            [item["evidence_id"] for item in augmented],
            ["ev_row", "ev_owner", "ev_vector"],
        )
        self.assertEqual(augmented[0]["retrieval_source"], "question_evidence_graph")
        self.assertEqual(
            answer.question_graph_branch_id(self.lookup_artifact, "F1"),
            "branch_owner",
        )

    def test_record_lookup_without_an_item_does_not_use_top_level_union(self) -> None:
        augmented, selected = answer.augment_with_question_graph(
            [], self.records, self.lookup_artifact, self.validation
        )

        self.assertEqual(augmented, [])
        self.assertEqual(selected, [])

    def test_aggregate_count_keeps_top_level_compatibility_path(self) -> None:
        artifact = {
            "artifact_id": "qeg_count",
            "status": "ready",
            "intent": {"operation": "aggregate_count"},
            "selected_evidence_ids": ["ev_owner"],
        }
        augmented, selected = answer.augment_with_question_graph(
            [], self.records, artifact, self.validation
        )

        self.assertEqual(selected, ["ev_owner"])
        self.assertEqual(augmented[0]["evidence_id"], "ev_owner")
        self.assertEqual(
            answer.question_graph_branch_id(artifact, "F1"), "qeg_count"
        )

    def test_record_lookup_is_graph_required_and_route_tracks_actual_use(self) -> None:
        self.assertTrue(answer.question_graph_blocks_answer(
            {**self.lookup_artifact, "status": "hold"},
            {"status": "blocked"},
        ))
        field_runs = [
            {
                "item": {"item_id": "F1", "required": True},
                "graph_primary_evidence_ids": ["ev_row", "ev_owner"],
                "graph_augmented_evidence_ids": ["ev_row", "ev_owner"],
            },
            {
                "item": {"item_id": "F2", "required": True},
                "graph_primary_evidence_ids": ["ev_row", "ev_date"],
                "graph_augmented_evidence_ids": ["ev_row", "ev_date"],
            },
        ]
        self.assertEqual(
            answer.build_graph_route(
                self.lookup_artifact, self.validation, field_runs
            ),
            {"operation": "record_lookup", "required": True, "used": True},
        )
        field_runs[1]["graph_augmented_evidence_ids"] = []
        self.assertFalse(
            answer.build_graph_route(
                self.lookup_artifact, self.validation, field_runs
            )["used"]
        )

    def test_graph_failure_audit_is_not_count_specific(self) -> None:
        audit = answer.graph_insufficient_audit(
            {"item_id": "F1"}, "record_lookup_branch_missing"
        )

        self.assertEqual(audit["verdict"], "insufficient")
        self.assertIn("Question Graph", audit["defect"])
        self.assertNotIn("集計", audit["defect"])
        self.assertNotIn("合計", " ".join(audit["missing_information"]))

    def test_shared_row_support_is_bound_to_the_branch_value_evidence(self) -> None:
        audit = {
            "item_id": "F1",
            "verdict": "supported",
            "supported_value": "Aki",
            "supporting_packet_ids": ["ev_row"],
            "competing_packet_ids": [],
            "reason_code": "none",
            "defect": "",
            "missing_information": [],
        }

        normalized = answer.bind_record_lookup_value_evidence(
            audit,
            {"item_id": "F1"},
            self.lookup_artifact,
            self.records,
        )

        self.assertEqual(normalized["verdict"], "supported")
        self.assertEqual(
            normalized["supporting_packet_ids"], ["ev_row", "ev_owner"]
        )

    def test_wrong_value_or_mismatched_value_cell_fails_closed(self) -> None:
        base_audit = {
            "item_id": "F1",
            "verdict": "supported",
            "supported_value": "Wrong Owner",
            "supporting_packet_ids": ["ev_row"],
            "competing_packet_ids": [],
            "reason_code": "none",
            "defect": "",
            "missing_information": [],
        }
        cases = (
            ("wrong_audit_value", base_audit, self.records),
            (
                "mismatched_value_evidence",
                {**base_audit, "supported_value": "Aki"},
                {**self.records, "ev_owner": evidence("ev_owner", "Other Owner")},
            ),
        )
        for name, audit, records in cases:
            with self.subTest(name=name):
                normalized = answer.bind_record_lookup_value_evidence(
                    copy.deepcopy(audit),
                    {"item_id": "F1"},
                    self.lookup_artifact,
                    records,
                )
                self.assertEqual(normalized["verdict"], "insufficient")
                self.assertEqual(normalized["supported_value"], "")
                self.assertNotIn("ev_owner", normalized["supporting_packet_ids"])

    def test_formula_only_value_can_bind_to_annotated_branch_value(self) -> None:
        artifact = copy.deepcopy(self.lookup_artifact)
        artifact["branches"][0]["value"] = (
            "=E3*F3 [保存値・ファイル保存時・未再計算: 10000]"
        )
        records = {**self.records, "ev_owner": evidence("ev_owner", "=E3*F3")}
        audit = {
            "item_id": "F1",
            "verdict": "supported",
            "supported_value": "= e3 * f3",
            "supporting_packet_ids": ["ev_row"],
            "competing_packet_ids": [],
            "reason_code": "none",
            "defect": "",
            "missing_information": [],
        }

        normalized = answer.bind_record_lookup_value_evidence(
            audit, {"item_id": "F1"}, artifact, records
        )

        self.assertEqual(normalized["verdict"], "supported")
        self.assertIn("ev_owner", normalized["supporting_packet_ids"])

    def test_formula_projection_preserves_semantic_whitespace(self) -> None:
        self.assertTrue(
            answer.record_lookup_value_matches("= d3 * e3", "=D3*E3")
        )
        self.assertFalse(answer.record_lookup_value_matches(
            '=IF(A1="East Division",1,0)',
            '=IF(A1="EastDivision",1,0)',
        ))
        self.assertFalse(answer.record_lookup_value_matches(
            "='East Division'!A1", "='EastDivision'!A1"
        ))
        self.assertFalse(answer.record_lookup_value_matches(
            "=Table[Sales Region]", "=Table[SalesRegion]"
        ))
        self.assertFalse(
            answer.record_lookup_value_matches("=A1 B1", "=A1B1")
        )
        self.assertFalse(
            answer.record_lookup_value_matches('="ABC"', '="abc"')
        )
        self.assertTrue(answer.record_lookup_value_matches(
            '= IF(A1="North ""Region""", 1, 0)',
            '=if(A1="North ""Region""",1,0)',
        ))
        self.assertTrue(answer.record_lookup_value_matches(
            "= 'North ''Region''' ! A1",
            "='North ''Region'''!a1",
        ))
        self.assertFalse(answer.record_lookup_value_matches(
            "=Table1[保存値]", "=Table1"
        ))
        self.assertTrue(answer.record_lookup_value_matches(
            "=E3*F3 [保存値・ファイル保存時・未再計算: 48000]",
            "=e3 * f3",
        ))

    def test_json_string_literal_value_cells_bind_without_erasing_punctuation(self) -> None:
        artifact = copy.deepcopy(self.lookup_artifact)
        artifact["branches"][0]["value"] = "Example Operations / Aoki"
        artifact["branches"][1]["value"] = "2030-01-15"
        records = {
            **self.records,
            "ev_owner": evidence(
                "ev_owner", json.dumps("Example Operations / Aoki")
            ),
            "ev_date": evidence("ev_date", json.dumps("2030-01-15")),
        }
        for item_id, value, evidence_id in (
            ("F1", "Example Operations / Aoki", "ev_owner"),
            ("F2", "2030-01-15", "ev_date"),
        ):
            with self.subTest(item_id=item_id):
                audit = {
                    "item_id": item_id,
                    "verdict": "supported",
                    "supported_value": value,
                    "supporting_packet_ids": ["ev_row"],
                    "competing_packet_ids": [],
                    "reason_code": "none",
                    "defect": "",
                    "missing_information": [],
                }
                normalized = answer.bind_record_lookup_value_evidence(
                    audit, {"item_id": item_id}, artifact, records
                )

                self.assertEqual(normalized["verdict"], "supported")
                self.assertIn(evidence_id, normalized["supporting_packet_ids"])

        self.assertFalse(
            answer.record_lookup_value_matches(json.dumps("F1"), "F-1")
        )

    def test_main_passes_plan_and_records_branch_specific_routes(self) -> None:
        plan = {
            "items": [
                {
                    "item_id": "F1", "label": "Owner",
                    "required_claim": "Owner", "retrieval_query": "Owner",
                    "required": True,
                },
                {
                    "item_id": "F2", "label": "Review Date",
                    "required_claim": "Review Date", "retrieval_query": "Review Date",
                    "required": True,
                },
            ],
            "answer_shape": "two fields",
        }
        metadata = {
            "model": "embedding-test",
            "evidence_sha256": "1" * 64,
            "graph_sha256": "2" * 64,
            "graph_security_partition_sha256": "3" * 64,
            "graph_retrievable_evidence_set_sha256": "4" * 64,
            "graph_embeddings_sha256": "5" * 64,
        }

        def audit_from_first_packet(_model, item, _context, packet_ids, _timeout):
            return {
                "item_id": item["item_id"],
                "verdict": "supported",
                "supported_value": "Aki" if item["item_id"] == "F1" else "2026-09-10",
                "supporting_packet_ids": [next(iter(packet_ids.values()))],
                "competing_packet_ids": [],
                "reason_code": "none",
                "defect": "",
                "missing_information": [],
            }

        argv = [
            str(ENGINE / "answer_local_memory_v2.py"),
            "Project AtlasのOwnerとReview Dateは？",
            "--index", str(ENGINE / "answer_local_memory_v2.py"),
            "--json",
        ]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            answer, "index_metadata", return_value=metadata,
        ), mock.patch.object(
            answer, "plan_question", return_value=plan,
        ), mock.patch.object(
            answer, "load_index_evidence_graph",
            return_value=(list(self.records.values()), self.records, {"graph": "stored"}),
        ), mock.patch.object(
            answer.question_graph, "build_question_evidence_graph",
            return_value=self.lookup_artifact,
        ) as build_graph, mock.patch.object(
            answer.question_graph, "validate_question_evidence_graph",
            return_value=self.validation,
        ) as validate_graph, mock.patch.object(
            answer, "retrieve_hybrid", return_value=(metadata, []),
        ), mock.patch.object(
            answer, "audit_field", side_effect=audit_from_first_packet,
        ), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(answer.main(), 0)

        record = json.loads(output.getvalue())
        self.assertEqual(
            record["graph_route"],
            {"operation": "record_lookup", "required": True, "used": True},
        )
        self.assertEqual(
            [row["question_graph_branch_id"] for row in record["field_runs"]],
            ["branch_owner", "branch_date"],
        )
        self.assertEqual(
            [row["graph_primary_evidence_ids"] for row in record["field_runs"]],
            [["ev_row", "ev_owner"], ["ev_row", "ev_date"]],
        )
        self.assertEqual(
            [row["graph_augmented_evidence_ids"] for row in record["field_runs"]],
            [["ev_row", "ev_owner"], ["ev_row", "ev_date"]],
        )
        self.assertEqual(
            [row["audit"]["supporting_packet_ids"] for row in record["field_runs"]],
            [["ev_row", "ev_owner"], ["ev_row", "ev_date"]],
        )
        self.assertEqual(
            build_graph.call_args.kwargs["question_plan"], record["question_plan"]
        )
        self.assertEqual(
            validate_graph.call_args.kwargs["question_plan"], record["question_plan"]
        )
        self.assertEqual(
            build_graph.call_args.kwargs["reference_date"],
            validate_graph.call_args.kwargs["reference_date"],
        )
        self.assertEqual(
            record["question_reference_date"],
            build_graph.call_args.kwargs["reference_date"],
        )


if __name__ == "__main__":
    unittest.main()
