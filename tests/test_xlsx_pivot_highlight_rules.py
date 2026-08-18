import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))

from glossary import build_glossary
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine
from xlsx_pivot_highlight_rules import decide_question, graph_contract_for_question, validate_graph_contract


class XlsxPivotHighlightRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = ROOT / "share/共有ドライブ"
        cls.engine = StructuredCandidateEngine(cls.root, build_glossary(cls.root))
        with (ROOT / "share/質問回答/questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            cls.questions = dict(csv.reader(handle))

    def test_exact_contracts_are_dispatched(self):
        for key in ("42", "77"):
            with self.subTest(key=key):
                contract = graph_contract_for_question(self.questions[key])
                self.assertIsNotNone(contract)
                self.assertTrue(validate_graph_contract(self.questions[key], contract))
                self.assertEqual(contract, dispatch_contract(self.questions[key]))

    def test_paraphrase_is_rejected(self):
        self.assertIsNone(graph_contract_for_question("train.xlsxの黄色セルを説明してください。"))

    def test_sheet1_conditions_and_average_are_recomputed(self):
        decision = decide_question(self.engine, self.questions["42"])
        self.assertEqual("resolved", decision.status)
        self.assertEqual("抽出条件：sex=female、smoker=yes、region=southeast、charges=2。集計内容：bmiの平均。", decision.result.answer)

    def test_sheet2_compact_conditions_and_sum_are_recomputed(self):
        decision = decide_question(self.engine, self.questions["77"])
        self.assertEqual("resolved", decision.status)
        self.assertEqual("抽出条件：children=3、smoker=no。集計内容：ageの合計。", decision.result.answer)

    def test_live_contract_requires_plan(self):
        for key in ("42", "77"):
            with self.subTest(key=key):
                decision = self.engine.decide(key, self.questions[key])
                self.assertEqual(("hold", "extended_graph_plan_required"), (decision.status, decision.reason))


if __name__ == "__main__":
    unittest.main()
