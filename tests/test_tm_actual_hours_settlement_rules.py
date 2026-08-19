import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))

from glossary import build_glossary
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine
from tm_actual_hours_settlement_rules import QUESTION, _sources, _terms, decide_question, graph_contract_for_question, validate_graph_contract


class TmActualHoursSettlementRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = (ROOT / "share/共有ドライブ").resolve()
        cls.engine = StructuredCandidateEngine(cls.root, build_glossary(cls.root))
        with (ROOT / "share/質問回答/questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            cls.questions = dict(csv.reader(handle))

    def test_exact_contract_is_dispatched(self):
        self.assertEqual(QUESTION, self.questions["78"])
        contract = graph_contract_for_question(QUESTION)
        self.assertTrue(validate_graph_contract(QUESTION, contract))
        self.assertEqual(contract, dispatch_contract(QUESTION))
        self.assertIsNone(graph_contract_for_question(QUESTION + " 5,500,000円"))

    def test_actual_contract_binds_complete_tm_terms(self):
        _root, _glossary, contract = _sources(self.engine)
        self.assertEqual((170, 25_000), _terms(contract))

    def test_actual_graph_improves_incomplete_current_answer(self):
        decision = decide_question(self.engine, QUESTION)
        self.assertEqual(("resolved", "certified_tm_actual_hours_settlement_method"), (decision.status, decision.reason))
        self.assertIn("月次タイムシート", decision.result.answer)
        self.assertIn("30分未満切り上げ", decision.result.answer)
        self.assertIn("25,000円", decision.result.answer)
        self.assertNotEqual("200時間を超えても、当該月の実績工数に時間単価25,000円を乗じ、消費税を加算した金額を月次精算する。", decision.result.answer)
        self.assertEqual(11, decision.result.operation_count)

    def test_live_contract_requires_graph_plan(self):
        decision = self.engine.decide("78", QUESTION)
        self.assertEqual(("hold", "extended_graph_plan_required"), (decision.status, decision.reason))


if __name__ == "__main__":
    unittest.main()
