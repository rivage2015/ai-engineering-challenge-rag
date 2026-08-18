import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "rag"), str(ROOT / "scripts")]

from glossary import build_glossary
from pptx_feature_legend_rules import _engineered_features, _source, graph_contract_for_question
from question_graph_runtime import build_graph_plan
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine


class PptxFeatureLegendRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = (ROOT / "share" / "共有ドライブ").resolve()
        cls.engine = StructuredCandidateEngine(cls.root, build_glossary(cls.root))
        with (ROOT / "share" / "質問回答" / "questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            cls.question = dict(csv.reader(handle))["53"]

    def test_exact_contract_is_dispatched(self):
        contract = graph_contract_for_question(self.question)
        self.assertTrue(contract["graph_contract_id"].startswith("pptx_feature_legend_"))
        self.assertEqual(contract, dispatch_contract(self.question))
        self.assertIsNone(graph_contract_for_question(self.question + " "))

    def test_glossary_and_actual_pptx_bind_uniquely(self):
        bound = _source(self.engine, "TOTO", "FR書", "ENG-FT")
        self.assertIsNotNone(bound)
        self.assertEqual(("Age_ord", "Exp_ord", "Edu_ord", "Age×Exp", "Age-Exp", "Edu×Exp"), _engineered_features(bound[2]))

    def test_actual_graph_resolves_six(self):
        plan = build_graph_plan("53", self.question, fast_advisory=True)
        decision = self.engine.decide_from_graph("53", self.question, plan)
        self.assertEqual("pass", plan.strict_status)
        self.assertEqual(("resolved", "certified_pptx_feature_legend_count"), (decision.status, decision.reason))
        self.assertEqual("6", decision.result.answer)
        self.assertEqual(10, decision.result.operation_count)


if __name__ == "__main__":
    unittest.main()
