from __future__ import annotations

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


qeg = load_module("ordered_section_qeg_test", ENGINE / "question_evidence_graph.py")
answer = load_module("ordered_section_answer_test", ENGINE / "answer_local_memory_v2.py")
claim_validator = load_module(
    "ordered_section_claim_validator_test", APP / "claim_graph_validator.py",
)


QUESTION = "案内の受付でお客様に一番最初にすべきお声がけは何ですか？"


def record(evidence_id: str, locator: dict, text: str, document_id: str = "doc_1") -> dict:
    return {
        "evidence_id": evidence_id,
        "document_id": document_id,
        "relative_path": f"{document_id}/operations.xlsx",
        "locator": locator,
        "text": text,
    }


def records(document_id: str = "doc_1") -> list[dict]:
    return [
        record("ev_sheet_" + document_id, {"sheet_name": "受付スク"}, "{シート}", document_id),
        record("ev_a1_" + document_id, {"sheet_name": "受付スク", "cell": "A1"}, "受付スクリプト", document_id),
        record("ev_c2_" + document_id, {"sheet_name": "受付スク", "cell": "C2"}, "スクリプト他", document_id),
        record("ev_d2_" + document_id, {"sheet_name": "受付スク", "cell": "D2"}, "参考", document_id),
        record("ev_heading_" + document_id, {"sheet_name": "受付スク", "row_index": 5}, "受付スクリプト: 1.ご挨拶", document_id),
        record("ev_value_" + document_id, {"sheet_name": "受付スク", "cell": "C6"}, "ようこそ！\nこんにちは！", document_id),
        record(
            "ev_sensitive_" + document_id,
            {"sheet_name": "受付スク", "cell": "D6"},
            "PASS: example-not-a-real-secret",
            document_id,
        ),
    ]


def source_graph(items: list[dict]) -> dict:
    document_ids = sorted({item["document_id"] for item in items})
    nodes = [
        {
            "node_id": document_id, "node_type": "document", "status": "observed",
            "record_sha256": (str(index + 1) * 64)[:64],
        }
        for index, document_id in enumerate(document_ids)
    ]
    nodes.extend({
        "node_id": item["evidence_id"], "node_type": "evidence", "status": "observed",
        "record_sha256": ("a" + str(index % 10)) * 32,
    } for index, item in enumerate(items))
    edges = [{
        "relation_id": f"rel_{index}",
        "from_node_id": item["document_id"],
        "to_node_id": item["evidence_id"],
        "relation_type": "contains",
        "relation_class": "structural",
        "basis_kind": "explicit",
        "status": "verified",
        "record_sha256": ("b" + str(index % 10)) * 32,
    } for index, item in enumerate(items)]
    return {
        "nodes": nodes,
        "edges": edges,
        "eligible_evidence_ids": [item["evidence_id"] for item in items],
        "graph_sha256": "c" * 64,
        "partition_sha256": "d" * 64,
        "eligible_evidence_set_sha256": "e" * 64,
    }


class OrderedSectionQuestionGraphTests(unittest.TestCase):
    def test_builds_and_validates_heading_to_first_script_path(self) -> None:
        items = records()
        graph = source_graph(items)
        artifact = qeg.build_question_evidence_graph(QUESTION, items, source_graph=graph)
        validation = qeg.validate_question_evidence_graph(
            QUESTION, items, artifact, source_graph=graph,
        )

        self.assertEqual((artifact["status"], artifact["intent"]["operation"]), (
            "ready", "ordered_section_lookup",
        ))
        self.assertEqual(validation["status"], "pass", validation)
        self.assertEqual(artifact["selection"]["value"], "ようこそ！ こんにちは！")
        self.assertEqual(artifact["selection"]["value_cell"], "C6")
        self.assertEqual(
            artifact["selected_evidence_ids"],
            ["ev_heading_doc_1", "ev_value_doc_1"],
        )
        self.assertNotIn("ev_sensitive_doc_1", artifact["selected_evidence_ids"])
        self.assertEqual(
            [edge["predicate"] for edge in artifact["edges"]],
            ["requires_section", "first_numbered_speech_step", "has_script_text"],
        )

    def test_executor_prepends_ordered_graph_evidence(self) -> None:
        items = records()
        graph = source_graph(items)
        artifact = qeg.build_question_evidence_graph(QUESTION, items, source_graph=graph)
        validation = qeg.validate_question_evidence_graph(
            QUESTION, items, artifact, source_graph=graph,
        )
        by_id = {item["evidence_id"]: item for item in items}
        augmented, selected = answer.augment_with_question_graph(
            [], by_id, artifact, validation, item_id="1",
        )

        self.assertEqual(selected, artifact["selected_evidence_ids"])
        self.assertEqual(
            [item["evidence_id"] for item in augmented],
            ["ev_heading_doc_1", "ev_value_doc_1"],
        )
        self.assertTrue(answer.question_graph_blocks_answer(artifact, validation) is False)

    def test_competing_documents_hold_instead_of_guessing(self) -> None:
        items = records("doc_1") + records("doc_2")
        graph = source_graph(items)
        artifact = qeg.build_question_evidence_graph(QUESTION, items, source_graph=graph)

        self.assertEqual(
            (artifact["status"], artifact["reason"]),
            ("hold", "ordered_section_candidate_ambiguous"),
        )
        self.assertTrue(answer.question_graph_blocks_answer(
            artifact, {"status": "blocked"},
        ))

    def test_unrelated_first_question_stays_on_legacy_route(self) -> None:
        items = records()
        artifact = qeg.build_question_evidence_graph(
            "今朝最初に開いたファイルは何ですか？", items,
            source_graph=source_graph(items),
        )
        self.assertEqual(artifact["status"], "unsupported")

    def test_credential_pattern_is_excluded_by_runtime_gate(self) -> None:
        self.assertTrue(qeg.SENSITIVE_VALUE_SURFACE.search("PASS: example"))
        self.assertFalse(qeg.SENSITIVE_VALUE_SURFACE.search("お客様にご挨拶します"))

    def test_claim_validator_binds_answer_to_ordered_value_evidence(self) -> None:
        items = records()
        stored = source_graph(items)
        artifact = qeg.build_question_evidence_graph(QUESTION, items, source_graph=stored)
        validation = qeg.validate_question_evidence_graph(
            QUESTION, items, artifact, source_graph=stored,
        )
        plan_item = {
            "item_id": "F1", "label": "最初のお声がけ内容",
            "required_claim": QUESTION, "retrieval_query": "受付 最初 お声がけ",
            "required": True,
        }
        answer_record = {
            "query": QUESTION,
            "question_plan": {"items": [plan_item], "answer_shape": "single_value"},
            "question_evidence_graph": artifact,
            "question_evidence_graph_validation": validation,
            "index": {"graph_sha256": stored["graph_sha256"]},
            "field_runs": [{
                "item": plan_item,
                "audit": {
                    "item_id": "F1", "verdict": "supported",
                    "supported_value": "ようこそ！\nこんにちは！",
                    "supporting_packet_ids": ["ev_value_doc_1"],
                    "competing_packet_ids": [], "reason_code": "none",
                    "defect": "", "missing_information": [],
                },
            }],
            "answer": {
                "answer_status": "answered", "answer_mode": "grounded",
                "answer": "確認できた内容:\n- 最初のお声がけ内容: ようこそ！\nこんにちは！",
                "evidence_ids": ["ev_value_doc_1"],
            },
        }
        packets = [
            {"evidence_id": item["evidence_id"], "text": item["text"]}
            for item in items
            if item["evidence_id"] in artifact["selected_evidence_ids"]
        ]
        _contract, graph, report = claim_validator.build_and_validate(
            answer_record, packets,
        )
        self.assertEqual(report["status"], "pass", report)

        graph["claims"][0]["value"] = "別の挨拶"
        graph["artifact_hash"] = claim_validator.stable_hash({
            key: graph.get(key) for key in ("contract_hash", "nodes", "edges", "claims")
        })
        blocked = claim_validator.validate_claim_graph(
            answer_record, packets, _contract, graph,
        )
        self.assertIn(
            "ordered_section_value_mismatch",
            {failure["code"] for failure in blocked["failures"]},
        )


if __name__ == "__main__":
    unittest.main()
