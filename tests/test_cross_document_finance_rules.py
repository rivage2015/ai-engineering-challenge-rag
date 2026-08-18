import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))

from cross_document_finance_rules import (
    decide_question,
    graph_contract_for_question,
    validate_graph_contract,
)
from glossary import build_glossary
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine


class CrossDocumentFinanceRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_root = ROOT / "share" / "共有ドライブ"
        cls.engine = StructuredCandidateEngine(
            cls.source_root, build_glossary(cls.source_root)
        )
        with (ROOT / "share" / "質問回答" / "questions_test.csv").open(
            encoding="utf-8-sig", newline=""
        ) as handle:
            cls.questions = dict(csv.reader(handle))

    def test_complete_grammars_build_stable_live_contracts(self):
        for qid in ("6", "23", "36", "40", "46", "55", "98"):
            question = self.questions[qid]
            contract = graph_contract_for_question(question)
            self.assertIsNotNone(contract)
            self.assertTrue(contract["graph_contract_id"].startswith("crossdoc_finance_"))
            self.assertTrue(validate_graph_contract(question, contract))
            self.assertEqual(contract, dispatch_contract(question))

    def test_partial_or_paraphrased_questions_do_not_match(self):
        self.assertIsNone(graph_contract_for_question("RATEが変わったのはいつですか。"))
        self.assertIsNone(graph_contract_for_question("提案と請求の差額はいくらですか。"))

    def test_actual_q006_is_source_derived(self):
        decision = decide_question(self.engine, self.questions["6"])
        self.assertEqual("resolved", decision.status)
        self.assertEqual("0円", decision.result.answer)
        self.assertEqual(2, len(decision.result.source_paths))

    def test_actual_q098_scans_all_six_tm_contracts(self):
        decision = decide_question(self.engine, self.questions["98"])
        self.assertEqual("resolved", decision.status)
        self.assertEqual("2025年7月1日", decision.result.answer)
        self.assertEqual(6, len(decision.result.source_paths))

    def test_actual_q055_compares_all_six_tm_projects(self):
        decision = decide_question(self.engine, self.questions["55"])
        self.assertEqual("resolved", decision.status)
        self.assertEqual("AOMINE", decision.result.answer)
        self.assertEqual(12, len(decision.result.source_paths))

    def test_actual_q040_aggregates_all_contract_payment_rows(self):
        decision = decide_question(self.engine, self.questions["40"])
        self.assertEqual("resolved", decision.status)
        self.assertEqual(
            "2025年10月：11,412,500円、2025年9月：9,350,000円、2025年8月：8,415,000円",
            decision.result.answer,
        )
        self.assertEqual(10, len(decision.result.source_paths))

    def test_actual_q036_uses_reported_measured_values(self):
        decision = decide_question(self.engine, self.questions["36"])
        self.assertEqual("resolved", decision.status)
        self.assertEqual("0.0962328831921873", decision.result.answer)
        self.assertEqual(2, len(decision.result.source_paths))

    def test_actual_q046_binds_maximum_upfront_sponsor_to_seat_map(self):
        decision = decide_question(self.engine, self.questions["46"])
        self.assertEqual("resolved", decision.status)
        self.assertEqual("7201", decision.result.answer)
        self.assertEqual(11, len(decision.result.source_paths))

    def test_actual_q023_connects_proposal_amount_to_contract_billing_terms(self):
        decision = decide_question(self.engine, self.questions["23"])
        self.assertEqual("resolved", decision.status)
        self.assertEqual("398,750円", decision.result.answer)
        self.assertEqual(2, len(decision.result.source_paths))
        self.assertEqual(12, decision.result.operation_count)

    def test_live_contract_cannot_bypass_graph_plan(self):
        for qid in ("6", "23", "36", "40", "46", "55", "98"):
            decision = self.engine.decide(qid, self.questions[qid])
            self.assertEqual("hold", decision.status)
            self.assertEqual("extended_graph_plan_required", decision.reason)


if __name__ == "__main__":
    unittest.main()
