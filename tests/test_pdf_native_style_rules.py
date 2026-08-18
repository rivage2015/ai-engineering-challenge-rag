import csv
import sys
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"rag"))

from glossary import build_glossary
from pdf_native_style_rules import decide_question,graph_contract_for_question,validate_graph_contract
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine


class PdfNativeStyleRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root=ROOT/"share/共有ドライブ"
        cls.engine=StructuredCandidateEngine(cls.root,build_glossary(cls.root))
        with (ROOT/"share/質問回答/questions_test.csv").open(encoding="utf-8-sig",newline="") as handle:cls.questions=dict(csv.reader(handle))

    def test_exact_contract_is_dispatched(self):
        question=self.questions["11"];contract=graph_contract_for_question(question)
        self.assertIsNotNone(contract);self.assertTrue(validate_graph_contract(question,contract));self.assertEqual(contract,dispatch_contract(question))

    def test_paraphrase_is_rejected(self):
        self.assertIsNone(graph_contract_for_question("太字と下線の場所を教えて。"))

    def test_actual_reports_have_one_triple_style_value(self):
        decision=decide_question(self.engine,self.questions["11"])
        self.assertEqual("resolved",decision.status);self.assertEqual("4,675,000円",decision.result.answer);self.assertEqual(2,len(decision.result.source_paths))

    def test_live_contract_requires_plan(self):
        decision=self.engine.decide("11",self.questions["11"])
        self.assertEqual(("hold","extended_graph_plan_required"),(decision.status,decision.reason))


if __name__=="__main__":unittest.main()
