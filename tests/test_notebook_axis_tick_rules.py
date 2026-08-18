import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))

from glossary import build_glossary
from notebook_axis_tick_rules import QUESTION, _maximum_tick, decide_question, graph_contract_for_question, validate_graph_contract
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine


class NotebookAxisTickRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = (ROOT / "share/共有ドライブ").resolve()
        cls.engine = StructuredCandidateEngine(cls.root, build_glossary(cls.root))
        with (ROOT / "share/質問回答/questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            cls.questions = dict(csv.reader(handle))

    def test_exact_question_contract_is_dispatched(self):
        self.assertEqual(QUESTION, self.questions["56"])
        contract = graph_contract_for_question(QUESTION)
        self.assertTrue(validate_graph_contract(QUESTION, contract))
        self.assertEqual(contract, dispatch_contract(QUESTION))
        self.assertIsNone(graph_contract_for_question(QUESTION + " 1200"))

    def test_actual_embedded_chart_resolves_displayed_maximum(self):
        decision = decide_question(self.engine, QUESTION)
        self.assertEqual(("resolved", "certified_notebook_embedded_axis_ticks"), (decision.status, decision.reason))
        self.assertEqual("1,200", decision.result.answer)
        self.assertEqual(10, decision.result.operation_count)
        self.assertEqual(2, len(decision.result.source_paths))

    def test_tick_reading_requires_complete_sequence_in_every_ocr_mode(self):
        import notebook_axis_tick_rules as rules
        original = rules._ocr
        try:
            rules._ocr = lambda _png, psm: "200 400 600 800 1000 1200" if psm != 11 else "200 400 600 800 1000"
            with self.assertRaises(ValueError):
                _maximum_tick(b"png")
        finally:
            rules._ocr = original

    def test_live_contract_requires_graph_plan(self):
        decision = self.engine.decide("56", QUESTION)
        self.assertEqual(("hold", "extended_graph_plan_required"), (decision.status, decision.reason))


if __name__ == "__main__":
    unittest.main()
