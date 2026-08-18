import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))

from cross_project_portfolio_rules import decide_question, graph_contract_for_question, validate_graph_contract
from glossary import build_glossary
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine


class CrossProjectPortfolioRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = (ROOT / "share/共有ドライブ").resolve()
        cls.engine = StructuredCandidateEngine(cls.root, build_glossary(cls.root))
        with (ROOT / "share/質問回答/questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            cls.questions = dict(csv.reader(handle))

    def test_exact_contracts_are_dispatched(self):
        for index in ("67", "87"):
            with self.subTest(index=index):
                question = self.questions[index]
                contract = graph_contract_for_question(question)
                self.assertIsNotNone(contract)
                self.assertTrue(validate_graph_contract(question, contract))
                self.assertEqual(contract, dispatch_contract(question))

    def test_paraphrases_and_answer_injections_are_rejected(self):
        self.assertIsNone(graph_contract_for_question("完了案件のAPRと金額差を教えてください。"))
        self.assertIsNone(graph_contract_for_question(self.questions["67"] + " AOSHIO、AOMINE、AOBM"))
        self.assertIsNone(graph_contract_for_question(self.questions["87"] + " AYM"))

    def test_actual_apr_m2_amount_differences_resolve(self):
        decision = decide_question(self.engine, self.questions["67"])
        self.assertEqual("resolved", decision.status)
        self.assertEqual("certified_cross_project_portfolio", decision.reason)
        self.assertEqual("AOSHIO、AOMINE、AOBM", decision.result.answer)
        self.assertEqual(3, decision.result.output_count)

    def test_actual_apr_m1_large_sample_resolves(self):
        decision = decide_question(self.engine, self.questions["87"])
        self.assertEqual("resolved", decision.status)
        self.assertEqual("AYM", decision.result.answer)
        self.assertEqual(1, decision.result.output_count)

    def test_live_contract_requires_graph_plan(self):
        for index in ("67", "87"):
            with self.subTest(index=index):
                decision = self.engine.decide(index, self.questions[index])
                self.assertEqual(("hold", "extended_graph_plan_required"), (decision.status, decision.reason))


if __name__ == "__main__":
    unittest.main()
