import csv
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))

from glossary import build_glossary
from pdf_investment_coefficient_rules import (
    _values,
    decide_question,
    graph_contract_for_question,
    validate_graph_contract,
)
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine


class PdfInvestmentCoefficientRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = ROOT / "share/共有ドライブ"
        cls.engine = StructuredCandidateEngine(cls.root, build_glossary(cls.root))
        with (ROOT / "share/質問回答/questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            cls.questions = dict(csv.reader(handle))

    def test_exact_contract_is_dispatched(self):
        question = self.questions["68"]
        contract = graph_contract_for_question(question)
        self.assertIsNotNone(contract)
        self.assertTrue(validate_graph_contract(question, contract))
        self.assertEqual(contract, dispatch_contract(question))
        self.assertEqual("pdf_investment_coefficient_formula_evaluation", contract["rule_id"])

    def test_paraphrase_and_embedded_answer_are_rejected(self):
        self.assertIsNone(graph_contract_for_question("投資実装係数は1.3986ですか。"))
        self.assertIsNone(graph_contract_for_question(self.questions["68"] + " 答えは1.3986。"))

    def test_source_reading_extracts_values_not_answer(self):
        text = "3.7倍  +15.2% / +22.6%  コスト削減 / 生産性向上  注釈: 投資実装係数 = (...) x ROI倍率"
        self.assertEqual((Decimal("15.2"), Decimal("22.6"), Decimal("3.7")), _values(text))
        changed = text.replace("22.6", "24.1")
        self.assertEqual((Decimal("15.2"), Decimal("24.1"), Decimal("3.7")), _values(changed))

    def test_ambiguous_or_incomplete_reading_holds_at_parser(self):
        self.assertIsNone(_values("3.7倍 +15.2% / +22.6%"))
        self.assertIsNone(_values("3.7倍 4.1倍 +15.2% / +22.6% コスト削減 生産性向上 ROI 投資実装係数"))

    def test_actual_pdf_is_recomputed(self):
        decision = decide_question(self.engine, self.questions["68"])
        self.assertEqual("resolved", decision.status)
        self.assertEqual("certified_pdf_investment_coefficient", decision.reason)
        self.assertEqual("1.3986", decision.result.answer)
        self.assertEqual(1, len(decision.result.source_paths))

    def test_live_contract_requires_graph_plan(self):
        decision = self.engine.decide("68", self.questions["68"])
        self.assertEqual(("hold", "extended_graph_plan_required"), (decision.status, decision.reason))


if __name__ == "__main__":
    unittest.main()
