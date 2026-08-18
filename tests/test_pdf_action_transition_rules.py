import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))

from glossary import build_glossary
from pdf_action_transition_rules import decide_question, graph_contract_for_question, validate_graph_contract
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine


class PdfActionTransitionRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = ROOT / "share/共有ドライブ"
        cls.engine = StructuredCandidateEngine(cls.root, build_glossary(cls.root))
        with (ROOT / "share/質問回答/questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            cls.questions = dict(csv.reader(handle))

    def test_exact_contract_is_dispatched(self):
        question = self.questions["34"]
        contract = graph_contract_for_question(question)
        self.assertIsNotNone(contract)
        self.assertTrue(validate_graph_contract(question, contract))
        self.assertEqual(contract, dispatch_contract(question))

    def test_paraphrase_and_answer_injection_are_rejected(self):
        self.assertIsNone(graph_contract_for_question("M01からM02で完了したものを教えてください。"))
        self.assertIsNone(graph_contract_for_question(self.questions["34"] + " A08、A09"))

    def test_actual_scanned_minutes_resolve_transitions(self):
        decision = decide_question(self.engine, self.questions["34"])
        self.assertEqual("resolved", decision.status)
        self.assertEqual("certified_pdf_action_transition", decision.reason)
        self.assertEqual("A08、A09", decision.result.answer)
        self.assertEqual(3, len(decision.result.source_paths))

    def test_live_contract_requires_graph_plan(self):
        decision = self.engine.decide("34", self.questions["34"])
        self.assertEqual(("hold", "extended_graph_plan_required"), (decision.status, decision.reason))


if __name__ == "__main__":
    unittest.main()
