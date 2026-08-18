import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))

from glossary import build_glossary
from notebook_date_chart_rules import QUESTION, decide_question, graph_contract_for_question, validate_graph_contract
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine


class NotebookDateChartRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = (ROOT / "share/共有ドライブ").resolve()
        cls.engine = StructuredCandidateEngine(cls.root, build_glossary(cls.root))
        with (ROOT / "share/質問回答/questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            cls.questions = dict(csv.reader(handle))

    def test_exact_contract_is_dispatched(self):
        self.assertEqual(QUESTION, self.questions["66"])
        contract = graph_contract_for_question(QUESTION)
        self.assertTrue(validate_graph_contract(QUESTION, contract))
        self.assertEqual(contract, dispatch_contract(QUESTION))
        self.assertIsNone(graph_contract_for_question(QUESTION + " 20日"))

    def test_actual_notebook_png_and_csv_resolve_twentieth(self):
        decision = decide_question(self.engine, QUESTION)
        self.assertEqual(("resolved", "certified_notebook_date_chart_maximum"), (decision.status, decision.reason))
        self.assertEqual("20日", decision.result.answer)
        self.assertEqual(10, decision.result.operation_count)
        self.assertEqual(4, len(decision.result.source_paths))

    def test_live_contract_requires_graph_plan(self):
        decision = self.engine.decide("66", QUESTION)
        self.assertEqual(("hold", "extended_graph_plan_required"), (decision.status, decision.reason))


if __name__ == "__main__":
    unittest.main()
