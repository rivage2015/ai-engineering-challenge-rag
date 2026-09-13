from __future__ import annotations

import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "distribution/macos-local-memory/app"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = load("answerability_integration_audit", APP / "final_answer_audit.py")
engine = load("answerability_integration_engine", APP.parent / "engine/answer_local_memory_v2.py")
FIELDS = ("evidence_sha256", "graph_sha256", "graph_security_partition_sha256",
          "graph_retrievable_evidence_set_sha256", "graph_embeddings_sha256")


def fixture():
    packets = [
        {"evidence_id": "E1", "document_id": "D1", "relative_path": "資料/案内.pdf", "locator": {"page_number": 1}, "text": "表示色は青です。"},
        {"evidence_id": "E2", "document_id": "D1", "relative_path": "資料/案内.pdf", "locator": {"page_number": 2}, "text": "[暫定読取] 870年頃？（平安時代）"},
    ]
    runs = []
    for field_id, label, value, ids in (
        ("F1", "表示色", "青", ["E1"]),
        ("F2", "設立年", "870年頃？（平安時代）", ["E2"]),
        ("F3", "資料の名称と場所", "資料/案内.pdf", ["E2"]),
    ):
        item = {"item_id": field_id, "label": label, "required_claim": label, "required": True}
        field_audit = {"item_id": field_id, "verdict": "supported", "supported_value": value,
                       "supporting_packet_ids": ids, "competing_packet_ids": [], "reason_code": "none",
                       "defect": "", "missing_information": []}
        runs.append({"item": item, "audit": field_audit, "retrieved_evidence_ids": ids})
    return {
        "query": "案内資料の表示色と設立年を教えてください。",
        "question_plan": {"items": [run["item"] for run in runs], "partial_answer_allowed": True},
        "question_evidence_graph": {"status": "unsupported", "intent": {"operation": "unknown"}},
        "graph_route": {"required": False, "used": False, "operation": "unknown"},
        "field_runs": runs,
        "answer": {"answer_status": "answered", "answer_mode": "grounded", "answer": "青、870年頃？（平安時代）、資料/案内.pdf",
                   "evidence_ids": ["E1", "E2"], "diagnostic_evidence_ids": [], "basis_summary": "原文",
                   "uncertainties": [], "non_answer_reason": {"code": "none", "explanation": ""},
                   "needed_information": [], "follow_up_question": "", "reconsideration_condition": "", "verification_reminder": ""},
        "index": {field: "hash-" + field for field in FIELDS},
    }, packets


def run_main(record, packets, results=None):
    metadata = {field: "hash-" + field for field in FIELDS}
    policy = {"eligible_evidence_ids": {p["evidence_id"] for p in packets}, "metadata": metadata,
              "graph_sha256": metadata["graph_sha256"], "partition_sha256": metadata["graph_security_partition_sha256"],
              "eligible_evidence_set_sha256": metadata["graph_retrievable_evidence_set_sha256"], "source_graph": {}}
    verified = ({"verdict": "verified", "reason": "引用と出典が一致", "unsupported_claims": []}, {"wall_seconds": 0.0})
    llm = mock.Mock(side_effect=results) if results else mock.Mock(return_value=verified)
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "record.json"
        path.write_text(json.dumps(record, ensure_ascii=False))
        with mock.patch.object(sys, "argv", ["audit", "--record", str(path), "--index", "unused.sqlite"]), \
             mock.patch.object(audit.answer_engine, "load_answer_evidence_records", return_value=(packets, policy)), \
             mock.patch.object(audit.question_graph, "validate_question_evidence_graph", return_value={"status": "not_applicable", "failures": []}), \
             mock.patch.object(audit, "audit", llm), redirect_stdout(io.StringIO()) as out:
            audit.main()
        return json.loads(out.getvalue()), llm


class AnswerabilityIntegrationTests(unittest.TestCase):
    def test_partial_answer_survives_all_gates_and_auditor_sees_exact_quotes(self):
        record, packets = fixture()
        result, llm = run_main(record, packets)
        self.assertEqual(result["orchestration_decision"]["status"], "accepted")
        self.assertEqual(result["answer"]["answer_mode"], "qualified")
        self.assertIn("確認済み — 表示色: 青", result["answer"]["answer"])
        self.assertIn("[暫定読取] 870年頃？（平安時代）", result["answer"]["answer"])
        self.assertNotIn("確認済み — 設立年", result["answer"]["answer"])
        self.assertEqual(result["deterministic_claim_validation"]["status"], "pass")
        self.assertEqual(llm.call_count, 1)

    def test_unknown_id_is_not_repaired_by_partial_policy(self):
        record, packets = fixture()
        record["answer"]["evidence_ids"].append("FABRICATED")
        result, llm = run_main(record, packets)
        self.assertEqual(result["orchestration_decision"]["status"], "rejected")
        self.assertEqual(result["answer"]["answer"], "わかりません")
        llm.assert_not_called()

    def test_snapshot_mismatch_still_stops_everything(self):
        record, packets = fixture()
        record["index"]["evidence_sha256"] = "wrong"
        result, llm = run_main(record, packets)
        self.assertEqual(result["answer_graph_validation"]["status"], "blocked")
        self.assertEqual(result["answer"]["answer"], "わかりません")
        llm.assert_not_called()

    def test_saved_contract_hash_mismatch_cannot_be_rebuilt_away(self):
        record, packets = fixture()
        claim_packets = [{**p, "path": p["relative_path"]} for p in packets]
        contract, graph, _ = audit.claim_validator.build_and_validate(record, claim_packets)
        record["question_contract"] = contract
        record["claim_graph"] = graph
        record["claim_graph"]["contract_hash"] = "tampered"
        result, llm = run_main(record, packets)
        self.assertEqual(result["answer_graph_validation"]["status"], "blocked")
        self.assertEqual(result["answer"]["answer"], "わかりません")
        llm.assert_not_called()

    def test_rejected_observation_is_removed_without_losing_confirmed_field(self):
        record, packets = fixture()
        results = [
            ({"verdict": "qualified", "reason": "暫定引用の扱いを再確認", "unsupported_claims": ["870年頃？（平安時代）"]}, {"wall_seconds": 0.0}),
            ({"verdict": "verified", "reason": "残る回答は支持される", "unsupported_claims": []}, {"wall_seconds": 0.0}),
        ]
        result, llm = run_main(record, packets, results)
        self.assertEqual(llm.call_count, 2)
        self.assertEqual(result["answerability_reaudit"]["excluded_field_ids"], ["F2"])
        self.assertIn("確認済み — 表示色: 青", result["answer"]["answer"])
        self.assertNotIn("870年頃", result["answer"]["answer"])
        self.assertEqual(result["orchestration_decision"]["status"], "accepted")

    def test_semantic_rejection_removes_field_and_reaudits_once(self):
        record, packets = fixture()
        results = [
            ({"verdict": "qualified", "reason": "表示色の関係未確認", "unsupported_claims": ["表示色: 青"]}, {"wall_seconds": 0.0}),
            ({"verdict": "verified", "reason": "残った引用と出典は一致", "unsupported_claims": []}, {"wall_seconds": 0.0}),
        ]
        result, llm = run_main(record, packets, results)
        self.assertEqual(llm.call_count, 2)
        self.assertEqual(result["answerability_reaudit"]["excluded_field_ids"], ["F1"])
        self.assertNotIn("確認済み — 表示色", result["answer"]["answer"])
        self.assertEqual(result["orchestration_decision"]["status"], "accepted")

    def test_reference_schema_is_opt_in_not_a_global_unknown_bypass(self):
        record, packets = fixture()
        record["field_runs"] = [record["field_runs"][1]]
        record["question_plan"]["items"] = [record["field_runs"][0]["item"]]
        result, _ = run_main(record, packets)
        self.assertTrue(result["answerability_policy"]["reference_only"])
        self.assertEqual(result["answer"]["answer_status"], "insufficient")
        with self.assertRaises(ValueError):
            audit.answer_engine.validate_answer(result["answer"], {"E1", "E2"})
        audit.answer_engine.validate_answer(result["answer"], {"E1", "E2"}, reference_only=True)


class BoundedContextTests(unittest.TestCase):
    def test_neighborhood_adds_only_same_document_and_page_with_limits(self):
        anchor = {"evidence_id": "A", "document_id": "D1", "relative_path": "guide.pdf", "locator": {"page_number": 3}, "text": "福徳神社"}
        records = {}
        for i in range(8):
            records[str(i)] = {**anchor, "evidence_id": str(i), "text": "訪問ルートには福徳神社が含まれます。" * 8}
        records["foreign"] = {**records["0"], "evidence_id": "foreign", "document_id": "D2"}
        records["nextpage"] = {**records["0"], "evidence_id": "nextpage", "locator": {"page_number": 4}}
        result = engine.augment_relation_context([anchor], records, {"required_claim": "福徳神社が行程に含まれるか"}, {"status": "unsupported"})
        self.assertEqual(len(result), 4)
        self.assertFalse({"foreign", "nextpage"} & {p["evidence_id"] for p in result})
        self.assertTrue(all(p.get("retrieval_source") == "bounded_same_document_context" for p in result[1:]))

    def test_structured_graph_selection_is_never_widened(self):
        original = [{"evidence_id": "A"}]
        result = engine.augment_relation_context(original, {}, {"required_claim": "行程"}, {"status": "ready"})
        self.assertIs(result, original)


if __name__ == "__main__":
    unittest.main()
