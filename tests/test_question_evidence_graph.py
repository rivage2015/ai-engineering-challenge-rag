from __future__ import annotations

import copy
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "distribution" / "macos-local-memory" / "engine"
APP = ROOT / "distribution" / "macos-local-memory" / "app"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


qeg = load_module("question_evidence_graph_test", ENGINE / "question_evidence_graph.py")
answer = load_module("answer_local_memory_v2_qeg_test", ENGINE / "answer_local_memory_v2.py")
claim_validator = load_module("claim_graph_validator_qeg_test", APP / "claim_graph_validator.py")


def record(evidence_id: str, locator: dict, text: str, document_id: str = "doc_1") -> dict:
    return {
        "evidence_id": evidence_id,
        "document_id": document_id,
        "relative_path": "2026.08_work-report.xlsx",
        "locator": locator,
        "text": text,
    }


def fixture(first: int = 2, second: int = 1, saved: int | None = None) -> list[dict]:
    total = first + second if saved is None else saved
    return [
        record("ev_header", {"sheet_name": "集計", "row_index": 1}, "A: 日付\nB: 作業枠"),
        record("ev_r2", {"sheet_name": "集計", "row_index": 2}, f"日付: 1\n作業枠: {first}"),
        record("ev_r3", {"sheet_name": "集計", "row_index": 3}, "日付: 2"),
        record("ev_r4", {"sheet_name": "集計", "row_index": 4}, f"日付: 3\n作業枠: {second}"),
        record(
            "ev_total_row", {"sheet_name": "集計", "row_index": 5},
            f"日付: 合計\n作業枠: =SUM(B2:B4) [保存値・ファイル保存時・未再計算: {total}]",
        ),
        record("ev_total_value", {"sheet_name": "集計", "cell": "B5"}, str(total)),
        record("ev_total_formula", {"sheet_name": "集計", "cell": "B5"}, "=SUM(B2:B4)"),
        record(
            "ev_reference", {"sheet_name": "報告書", "row_index": 7},
            f"B: 作業枠\nC: =集計!B5 [保存値・ファイル保存時・未再計算: {total}]\nD: 枠",
        ),
    ]


class QuestionEvidenceGraphTests(unittest.TestCase):
    QUESTION = "2026年8月の作業枠は何回ありましたか？"

    def test_builds_verified_aggregate_graph_without_fixed_answer(self) -> None:
        three = qeg.build_question_evidence_graph(self.QUESTION, fixture(2, 1))
        five = qeg.build_question_evidence_graph(self.QUESTION, fixture(2, 3))
        self.assertEqual((three["status"], three["selection"]["value"]), ("ready", "3"))
        self.assertEqual((five["status"], five["selection"]["value"]), ("ready", "5"))
        self.assertEqual(three["selection"]["coverage"], {"expected_rows": 3, "covered_rows": 3})
        self.assertEqual(
            qeg.validate_question_evidence_graph(self.QUESTION, fixture(2, 1), three)["status"],
            "pass",
        )
        self.assertEqual({edge["predicate"] for edge in three["edges"]}, {
            "requires", "targets", "aggregates", "recomputed_as", "answers",
        })
        self.assertTrue(all(edge.get("basis", {}).get("rule") for edge in three["edges"]))

    def test_conflicting_saved_total_holds(self) -> None:
        artifact = qeg.build_question_evidence_graph(self.QUESTION, fixture(2, 1, saved=4))
        self.assertEqual((artifact["status"], artifact["reason"]), ("hold", "aggregate_value_conflict"))
        validation = qeg.validate_question_evidence_graph(self.QUESTION, fixture(2, 1, saved=4), artifact)
        self.assertEqual(validation["status"], "blocked")
        self.assertTrue(answer.question_graph_blocks_answer(artifact, validation))

        legacy_escape = {
            **artifact, "status": "unsupported", "reason": "structured_aggregate_not_found",
        }
        legacy_validation = {"status": "not_applicable"}
        self.assertTrue(answer.question_graph_blocks_answer(legacy_escape, legacy_validation))

    def test_incomplete_sum_range_holds(self) -> None:
        records = [item for item in fixture() if item["evidence_id"] != "ev_r3"]
        artifact = qeg.build_question_evidence_graph(self.QUESTION, records)
        self.assertEqual((artifact["status"], artifact["reason"]), ("hold", "aggregation_coverage_incomplete"))

    def test_count_question_without_structured_aggregate_holds(self) -> None:
        records = [record(
            "ev_unbound", {"sheet_name": "集計", "row_index": 2}, "作業枠: 999"
        )]
        artifact = qeg.build_question_evidence_graph(self.QUESTION, records)
        validation = qeg.validate_question_evidence_graph(self.QUESTION, records, artifact)
        self.assertEqual(
            (artifact["status"], artifact["reason"]),
            ("hold", "structured_aggregate_not_found"),
        )
        self.assertEqual(validation["status"], "blocked")

    def test_malformed_evidence_record_holds_instead_of_being_skipped(self) -> None:
        records = fixture() + [{
            "evidence_id": "ev_malformed", "document_id": "doc_1",
            "relative_path": "2026.08_work-report.xlsx", "locator": "B2",
            "text": "作業枠: 999",
        }]
        artifact = qeg.build_question_evidence_graph(self.QUESTION, records)
        self.assertEqual(
            (artifact["status"], artifact["reason"]),
            ("hold", "malformed_evidence_records"),
        )
        non_mapping = qeg.build_question_evidence_graph(
            self.QUESTION, fixture() + [["not", "an", "evidence", "object"]]
        )
        self.assertEqual(non_mapping["reason"], "malformed_evidence_records")

    def test_duplicate_aggregate_sources_are_ambiguous_and_order_stable(self) -> None:
        records = fixture()
        records.append(record(
            "ev_total_row_duplicate", {"sheet_name": "集計", "row_index": 6},
            "日付: 合計\n作業枠: =SUM(B2:B4) [保存値・ファイル保存時・未再計算: 3]",
        ))
        forward = qeg.build_question_evidence_graph(self.QUESTION, records)
        reverse = qeg.build_question_evidence_graph(self.QUESTION, reversed(records))
        self.assertEqual(
            (forward["status"], forward["reason"]),
            ("hold", "aggregate_candidate_ambiguous"),
        )
        self.assertEqual(forward["artifact_hash"], reverse["artifact_hash"])

    def test_competing_lower_score_aggregate_in_another_document_holds(self) -> None:
        primary = fixture(2, 1)
        competing = fixture(2, 3)
        for item in competing:
            item["evidence_id"] = "doc2_" + item["evidence_id"]
            item["document_id"] = "doc_2"
            item["relative_path"] = "2026.08_second-work-report.xlsx"
            item["text"] = item["text"].replace("作業枠", "作業枠数")

        artifact = qeg.build_question_evidence_graph(
            self.QUESTION, primary + competing
        )

        self.assertEqual(
            (artifact["status"], artifact["reason"]),
            ("hold", "aggregate_candidate_competing"),
        )

    def test_provisional_total_cannot_be_confirmed(self) -> None:
        records = fixture()
        for item in records:
            if item["evidence_id"] == "ev_total_row":
                item["text"] = "[暫定読取] " + item["text"]
        artifact = qeg.build_question_evidence_graph(self.QUESTION, records)
        self.assertEqual((artifact["status"], artifact["reason"]), ("hold", "provisional_aggregate_evidence"))

    def test_provisional_range_record_cannot_count_as_complete_coverage(self) -> None:
        records = fixture()
        for item in records:
            if item["evidence_id"] == "ev_r3":
                item["text"] = "[暫定読取] " + item["text"]
        artifact = qeg.build_question_evidence_graph(self.QUESTION, records)
        self.assertEqual(
            (artifact["status"], artifact["reason"]),
            ("hold", "provisional_aggregate_evidence"),
        )

    def test_mismatched_evidence_text_hash_holds(self) -> None:
        records = fixture()
        records[1]["observed_sha256"] = "0" * 64
        artifact = qeg.build_question_evidence_graph(self.QUESTION, records)
        self.assertEqual(
            (artifact["status"], artifact["reason"]),
            ("hold", "evidence_text_hash_mismatch"),
        )

    def test_tampered_artifact_is_rejected(self) -> None:
        artifact = qeg.build_question_evidence_graph(self.QUESTION, fixture())
        artifact["selection"]["value"] = "999"
        validation = qeg.validate_question_evidence_graph(self.QUESTION, fixture(), artifact)
        self.assertEqual(validation["status"], "blocked")
        self.assertIn(
            "artifact_hash_mismatch",
            {failure["code"] for failure in validation["failures"]},
        )

    def test_non_count_question_does_not_override_normal_retrieval(self) -> None:
        artifact = qeg.build_question_evidence_graph("作業内容を教えてください。", fixture())
        self.assertEqual(artifact["status"], "unsupported")
        validation = qeg.validate_question_evidence_graph("作業内容を教えてください。", fixture(), artifact)
        self.assertEqual(validation["status"], "not_applicable")

    def test_answer_engine_prepends_graph_evidence_before_vector_hits(self) -> None:
        records = fixture()
        artifact = qeg.build_question_evidence_graph(self.QUESTION, records)
        validation = qeg.validate_question_evidence_graph(self.QUESTION, records, artifact)
        by_id = {item["evidence_id"]: item for item in records}
        existing = [{
            "score": 0.5, "rerank_score": 0.5, "document_support_bonus": 0.0,
            "semantic_score": 0.5, "lexical_score": 0.0, "token_score": 0.0,
            "evidence_id": "vector_hit", "document_id": "doc_1",
            "relative_path": "2026.08_work-report.xlsx", "locator": {"row_index": 99},
            "text": "unrelated",
        }]
        augmented, selected = answer.augment_with_question_graph(existing, by_id, artifact, validation)
        self.assertEqual(augmented[0]["evidence_id"], "ev_total_row")
        self.assertEqual(augmented[0]["retrieval_source"], "question_evidence_graph")
        self.assertEqual(selected, artifact["selected_evidence_ids"])
        self.assertEqual(augmented[-1]["evidence_id"], "vector_hit")

    def test_fast_plan_recognizes_count_contract(self) -> None:
        plan = answer.try_fast_plan(self.QUESTION)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["answer_shape"], "整数")
        self.assertEqual(plan["items"][0]["label"], "回数")
        self.assertIn("合計", plan["items"][0]["retrieval_query"])

    def test_post_answer_claim_graph_is_bound_to_pre_answer_aggregate_graph(self) -> None:
        records = fixture()
        artifact = qeg.build_question_evidence_graph(self.QUESTION, records)
        validation = qeg.validate_question_evidence_graph(self.QUESTION, records, artifact)
        answer_record = {
            "query": self.QUESTION,
            "question_plan": {
                "items": [{
                    "item_id": "F1", "label": "回数", "required_claim": self.QUESTION,
                    "retrieval_query": self.QUESTION, "required": True,
                }],
                "answer_shape": "整数",
            },
            "field_runs": [{
                "item": {"item_id": "F1", "label": "回数"},
                "audit": {
                    "item_id": "F1", "verdict": "supported", "supported_value": "3",
                    "supporting_packet_ids": ["ev_total_row"], "competing_packet_ids": [],
                    "reason_code": "none", "defect": "", "missing_information": [],
                },
            }],
            "answer": {
                "answer_status": "answered", "answer_mode": "grounded",
                "answer": "確認できた内容:\n- 回数: 3", "evidence_ids": ["ev_total_row"],
            },
            "question_evidence_graph": artifact,
            "question_evidence_graph_validation": validation,
        }
        validation_ids = set(artifact["selection"]["validation_evidence_ids"])
        packets = [
            {"evidence_id": item["evidence_id"], "text": item["text"]}
            for item in records if item["evidence_id"] in validation_ids
        ]
        contract, graph, report = claim_validator.build_and_validate(answer_record, packets)
        self.assertEqual(report["status"], "pass", report)
        self.assertEqual(contract["items"][0]["entity_type"], "numeric_count")
        node_ids = {node["node_id"] for node in graph["nodes"]}
        self.assertIn("C1", node_ids)
        self.assertTrue(all(
            edge["source"] in node_ids and edge["target"] in node_ids
            for edge in graph["edges"]
        ))
        self.assertEqual(contract["items"][0]["time_scope"], "specified_period")

        for wrong_answer in (
            "回数は3人です。",
            "回数は3件です。",
            "回数は3割です。",
            "回数は3とは限りません。",
            "回数が3かどうか不明です。",
            "回数は3回または4回です。",
        ):
            with self.subTest(wrong_answer=wrong_answer):
                mutated = copy.deepcopy(answer_record)
                mutated["answer"]["answer"] = wrong_answer
                _, _, mutated_report = claim_validator.build_and_validate(
                    mutated, packets,
                )
                self.assertIn(
                    "aggregate_answer_projection_mismatch",
                    {
                        failure["code"]
                        for failure in mutated_report["failures"]
                    },
                )

        mismatched = copy.deepcopy(answer_record)
        mismatched_artifact = mismatched["question_evidence_graph"]
        mismatched_artifact["selection"]["value"] = "4"
        mismatched_body = {
            key: value
            for key, value in mismatched_artifact.items()
            if key not in {"artifact_hash", "artifact_id"}
        }
        mismatched_hash = qeg.stable_hash(mismatched_body)
        mismatched_artifact["artifact_hash"] = mismatched_hash
        mismatched_artifact["artifact_id"] = f"qeg_{mismatched_hash[:24]}"
        _, _, mismatched_report = claim_validator.build_and_validate(
            mismatched, packets
        )
        self.assertIn(
            "question_graph_selection_recomputed_mismatch",
            {failure["code"] for failure in mismatched_report["failures"]},
        )

        dangling = {**graph, "edges": [dict(edge) for edge in graph["edges"]]}
        dangling["edges"][0]["target"] = "missing_node"
        body = {key: dangling.get(key) for key in ("contract_hash", "nodes", "edges", "claims")}
        dangling["artifact_hash"] = claim_validator.stable_hash(body)
        structurally_blocked = claim_validator.validate_claim_graph(
            answer_record, packets, contract, dangling
        )
        self.assertIn(
            "graph_edge_endpoint_missing",
            {failure["code"] for failure in structurally_blocked["failures"]},
        )

        escaped = dict(answer_record)
        escaped["field_runs"] = [{
            **answer_record["field_runs"][0],
            "audit": {
                **answer_record["field_runs"][0]["audit"],
                "supporting_packet_ids": ["ev_r2"],
            },
        }]
        _, _, blocked = claim_validator.build_and_validate(escaped, packets)
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn(
            "question_graph_evidence_escape",
            {failure["code"] for failure in blocked["failures"]},
        )


if __name__ == "__main__":
    unittest.main()
