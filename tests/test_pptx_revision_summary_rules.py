import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))

from glossary import build_glossary
from pptx_revision_summary_rules import decide_question, graph_contract_for_question, validate_graph_contract
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine


class PptxRevisionSummaryRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = ROOT / "share" / "共有ドライブ"
        cls.engine = StructuredCandidateEngine(cls.root, build_glossary(cls.root))
        with (ROOT / "share/質問回答/questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            cls.questions = dict(csv.reader(handle))

    def test_exact_contracts_are_dispatched(self):
        for qid in ("0", "9"):
            contract = graph_contract_for_question(self.questions[qid])
            self.assertIsNotNone(contract)
            self.assertTrue(validate_graph_contract(self.questions[qid], contract))
            self.assertEqual(contract, dispatch_contract(self.questions[qid]))

    def test_partial_question_is_rejected(self):
        self.assertIsNone(graph_contract_for_question("提案書の変更点は何ですか。"))

    def test_actual_proposal_addition_is_reported(self):
        decision = decide_question(self.engine, self.questions["0"])
        self.assertEqual("resolved", decision.status)
        self.assertIn("データ理解・品質確認", decision.result.answer)
        self.assertEqual(2, len(decision.result.source_paths))

    def test_actual_report_reflow_has_no_execution_change(self):
        decision = decide_question(self.engine, self.questions["9"])
        self.assertEqual("resolved", decision.status)
        self.assertEqual("なし", decision.result.answer)
        self.assertEqual(2, len(decision.result.source_paths))

    def test_live_contract_requires_plan(self):
        for qid in ("0", "9"):
            decision = self.engine.decide(qid, self.questions[qid])
            self.assertEqual("hold", decision.status)
            self.assertEqual("extended_graph_plan_required", decision.reason)


if __name__ == "__main__":
    unittest.main()
