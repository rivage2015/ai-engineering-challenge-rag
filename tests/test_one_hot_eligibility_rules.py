import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))

from glossary import build_glossary
from one_hot_eligibility_rules import QUESTION, _eligible_columns, _implementation_contract, _proposal_relation, _sources, decide_question, graph_contract_for_question, validate_graph_contract
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine


class OneHotEligibilityRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = (ROOT / "share/共有ドライブ").resolve()
        cls.engine = StructuredCandidateEngine(cls.root, build_glossary(cls.root))
        with (ROOT / "share/質問回答/questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            cls.questions = dict(csv.reader(handle))

    def test_exact_contract_is_dispatched(self):
        self.assertEqual(QUESTION, self.questions["73"])
        contract = graph_contract_for_question(QUESTION)
        self.assertTrue(validate_graph_contract(QUESTION, contract))
        self.assertEqual(contract, dispatch_contract(QUESTION))
        self.assertIsNone(graph_contract_for_question(QUESTION + " Gender"))

    def test_actual_sources_bind_threshold_and_gender(self):
        _root, _glossary, proposal, config, run_train, features, modeling, csv_path = _sources(self.engine)
        _proposal_relation(proposal)
        limit, target, identifiers = _implementation_contract(config, run_train, features, modeling)
        self.assertEqual(100, limit)
        self.assertEqual(("Gender",), _eligible_columns(csv_path, target, identifiers, limit))

    def test_actual_graph_resolves_current_answer(self):
        decision = decide_question(self.engine, QUESTION)
        self.assertEqual(("resolved", "certified_one_hot_threshold_eligibility"), (decision.status, decision.reason))
        self.assertEqual("カテゴリ数100未満がOne-Hot Encodingの対象で、該当するカテゴリ列はGenderです。", decision.result.answer)
        self.assertEqual(11, decision.result.operation_count)
        self.assertEqual(1, decision.result.output_count)

    def test_live_contract_requires_graph_plan(self):
        decision = self.engine.decide("73", QUESTION)
        self.assertEqual(("hold", "extended_graph_plan_required"), (decision.status, decision.reason))


if __name__ == "__main__":
    unittest.main()
