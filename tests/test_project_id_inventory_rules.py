import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))

from glossary import build_glossary
from project_id_inventory_rules import QUESTION, _action_ids, _contiguous_ids, _sources, decide_question, graph_contract_for_question, validate_graph_contract
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine


class ProjectIdInventoryRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = (ROOT / "share/共有ドライブ").resolve()
        cls.engine = StructuredCandidateEngine(cls.root, build_glossary(cls.root))
        with (ROOT / "share/質問回答/questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            cls.questions = dict(csv.reader(handle))

    def test_exact_contract_is_dispatched(self):
        self.assertEqual(QUESTION, self.questions["92"])
        contract = graph_contract_for_question(QUESTION)
        self.assertTrue(validate_graph_contract(QUESTION, contract))
        self.assertEqual(contract, dispatch_contract(QUESTION))
        self.assertIsNone(graph_contract_for_question(QUESTION + " 49"))

    def test_actual_minutes_have_complete_unique_action_namespace(self):
        _root, _glossary, _plan, minutes = _sources(self.engine)
        self.assertEqual(tuple(f"A{i:02d}" for i in range(1, 20)), _action_ids(minutes))

    def test_actual_graph_resolves_current_answer(self):
        decision = decide_question(self.engine, QUESTION)
        self.assertEqual(("resolved", "certified_project_id_inventory_total"), (decision.status, decision.reason))
        self.assertEqual("49", decision.result.answer)
        self.assertEqual(11, decision.result.operation_count)
        self.assertEqual(1, decision.result.output_count)

    def test_id_series_rejects_gaps_and_bad_width(self):
        with self.assertRaises(ValueError):
            _contiguous_ids(("T01", "T03"), "T", 2)
        with self.assertRaises(ValueError):
            _contiguous_ids(("T1", "T02"), "T", 2)

    def test_live_contract_requires_graph_plan(self):
        decision = self.engine.decide("92", QUESTION)
        self.assertEqual(("hold", "extended_graph_plan_required"), (decision.status, decision.reason))


if __name__ == "__main__":
    unittest.main()
