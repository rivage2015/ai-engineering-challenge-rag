import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))

from cross_project_portfolio_rules import (
    _alias,
    _apr,
    _apr_policy_sources,
    _contract_facts,
    _contract_path,
    _projects,
    decide_question,
    graph_contract_for_question,
    validate_graph_contract,
)
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
        for index in ("31", "38", "67", "87"):
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
        self.assertIsNone(graph_contract_for_question(self.questions["38"] + " APR-M3は0件"))
        self.assertIsNone(graph_contract_for_question(self.questions["31"].replace("切り上げ", "四捨五入")))

    def test_actual_fixed_price_gross_per_training_row_has_unique_maximum(self):
        decision = decide_question(self.engine, self.questions["31"])
        self.assertEqual(("resolved", "certified_cross_project_portfolio"), (decision.status, decision.reason))
        self.assertEqual("MINAMINO、1,320円", decision.result.answer)
        self.assertEqual(11, decision.result.operation_count)
        self.assertEqual(21, len(decision.result.source_paths))

    def test_actual_apr_m3_policy_and_all_ten_contracts_produce_empty_set(self):
        self.assertIsNotNone(_apr_policy_sources(self.root))
        projects = _projects(self.root)
        self.assertEqual(10, len(projects))
        levels = {}
        for project in projects:
            facts = _contract_facts(_contract_path(project), project)
            levels[_alias(self.engine, project)] = _apr(*facts)
        self.assertNotIn("APR-M3", levels.values())
        self.assertEqual(9, sum(level == "APR-M2" for level in levels.values()))
        self.assertEqual("APR-M1", levels["AYM"])

    def test_actual_apr_m3_contract_total_resolves_empty_result(self):
        decision = decide_question(self.engine, self.questions["38"])
        self.assertEqual(("resolved", "certified_cross_project_portfolio"), (decision.status, decision.reason))
        self.assertEqual("該当なし、合計0円", decision.result.answer)
        self.assertEqual(18, decision.result.operation_count)
        self.assertEqual(22, len(decision.result.source_paths))

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
        for index in ("31", "38", "67", "87"):
            with self.subTest(index=index):
                decision = self.engine.decide(index, self.questions[index])
                self.assertEqual(("hold", "extended_graph_plan_required"), (decision.status, decision.reason))


if __name__ == "__main__":
    unittest.main()
