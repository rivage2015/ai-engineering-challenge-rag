import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))

from docx_rank_ratio_rules import _ratio_from_tables, decide_question, graph_contract_for_question, validate_graph_contract
from glossary import build_glossary
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine


class DocxRankRatioRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_root = ROOT / "share" / "共有ドライブ"
        cls.engine = StructuredCandidateEngine(cls.source_root, build_glossary(cls.source_root))
        with (ROOT / "share" / "質問回答" / "questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            cls.question = dict(csv.reader(handle))["99"]

    def test_exact_question_builds_live_contract(self):
        contract = graph_contract_for_question(self.question)
        self.assertIsNotNone(contract)
        self.assertTrue(validate_graph_contract(self.question, contract))
        self.assertEqual(contract, dispatch_contract(self.question))

    def test_actual_document_resolves_ratio(self):
        decision = decide_question(self.engine, self.question)
        self.assertEqual(("resolved", "certified_docx_ranked_mortality_ratio"), (decision.status, decision.reason))
        self.assertEqual("2.49", decision.result.answer)
        self.assertEqual(2, len(decision.result.source_paths))

    def test_rank_direction_or_completeness_changes_hold(self):
        table = [
            ["順位", "死亡率が高い都道府県（ワースト）", "死亡率（%）", "死亡率が低い都道府県（ベスト）", "死亡率（%）"],
            ["1位", "A県", "18.2", "F県", "7.2"],
            ["2位", "B県", "16.3", "G県", "7.22"],
            ["3位", "C県", "16.1", "H県", "7.28"],
            ["4位", "D県", "15.0", "I県", "7.3"],
            ["5位", "E県", "14.9", "J県", "8.0"],
        ]
        self.assertEqual("2.49", _ratio_from_tables([table]))
        for changed in (table[:-1], [*table[:3], ["3位", "C県", "16.5", "H県", "7.28"], *table[4:]], [*table[:4], ["4位", "D県", "15.0", "I県", "7.1"], table[5]]):
            with self.subTest(changed=changed):
                with self.assertRaises(ValueError):
                    _ratio_from_tables([changed])

    def test_live_contract_requires_graph_plan(self):
        decision = self.engine.decide("99", self.question)
        self.assertEqual(("hold", "extended_graph_plan_required"), (decision.status, decision.reason))


if __name__ == "__main__":
    unittest.main()
