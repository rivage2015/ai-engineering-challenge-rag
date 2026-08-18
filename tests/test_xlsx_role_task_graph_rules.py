import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))

from glossary import build_glossary
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine
from xlsx_role_task_graph_rules import decide_question, graph_contract_for_question, validate_graph_contract


class XlsxRoleTaskGraphRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_root = ROOT / "share" / "共有ドライブ"
        cls.engine = StructuredCandidateEngine(cls.source_root, build_glossary(cls.source_root))
        with (ROOT / "share" / "質問回答" / "questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            cls.questions = dict(csv.reader(handle))

    def test_actual_q072_builds_and_dispatches_stable_graph(self):
        question = self.questions["72"]
        contract = graph_contract_for_question(question)
        self.assertIsNotNone(contract)
        self.assertTrue(contract["graph_contract_id"].startswith("xlsx_role_task_"))
        self.assertTrue(validate_graph_contract(question, contract))
        self.assertEqual(contract, dispatch_contract(question))

    def test_actual_q072_joins_role_row_to_all_wbs_assignments(self):
        decision = decide_question(self.engine, self.questions["72"])
        self.assertEqual("resolved", decision.status)
        self.assertEqual("5", decision.result.answer)
        self.assertEqual(12, decision.result.operation_count)
        self.assertEqual(2, len(decision.result.source_paths))

    def test_paraphrase_and_partial_question_do_not_match(self):
        self.assertIsNone(graph_contract_for_question("KSSのデータエンジニアのタスク数は？"))

    def test_live_contract_requires_certified_graph_plan(self):
        decision = self.engine.decide("72", self.questions["72"])
        self.assertEqual("hold", decision.status)
        self.assertEqual("extended_graph_plan_required", decision.reason)


if __name__ == "__main__":
    unittest.main()
