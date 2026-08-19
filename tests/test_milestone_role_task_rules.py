import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))

from glossary import build_glossary
from milestone_role_task_rules import QUESTION, _matching_task_ids, _role_person, _sources, decide_question, graph_contract_for_question, validate_graph_contract
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine


class MilestoneRoleTaskRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = (ROOT / "share/共有ドライブ").resolve()
        cls.engine = StructuredCandidateEngine(cls.root, build_glossary(cls.root))
        with (ROOT / "share/質問回答/questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            cls.questions = dict(csv.reader(handle))

    def test_exact_contract_is_dispatched(self):
        self.assertEqual(QUESTION, self.questions["94"])
        contract = graph_contract_for_question(QUESTION)
        self.assertTrue(validate_graph_contract(QUESTION, contract))
        self.assertEqual(contract, dispatch_contract(QUESTION))
        self.assertIsNone(graph_contract_for_question(QUESTION + " T09"))

    def test_actual_roster_uniquely_binds_business_analyst(self):
        _root, _glossary, proposal, _schedule = _sources(self.engine)
        self.assertEqual("松本 真央", _role_person(proposal, "ビジネスアナリスト"))

    def test_actual_graph_resolves_current_answer(self):
        decision = decide_question(self.engine, QUESTION)
        self.assertEqual(("resolved", "certified_milestone_role_task_ids"), (decision.status, decision.reason))
        self.assertEqual("T09", decision.result.answer)
        self.assertEqual(10, decision.result.operation_count)
        self.assertEqual(1, decision.result.output_count)

    def test_row_join_requires_both_edges_on_same_row(self):
        rows = [
            ("タスクID", "担当者", "関連マイルストーン"),
            ("T01", "松本 真央", "MS2"),
            ("T02", "他担当", "MS3"),
        ]
        with self.assertRaises(ValueError):
            _matching_task_ids(rows, "MS3", "松本 真央")

    def test_live_contract_requires_graph_plan(self):
        decision = self.engine.decide("94", QUESTION)
        self.assertEqual(("hold", "extended_graph_plan_required"), (decision.status, decision.reason))


if __name__ == "__main__":
    unittest.main()
