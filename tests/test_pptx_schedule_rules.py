import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))

from glossary import build_glossary
from pptx_schedule_rules import decide_question, graph_contract_for_question, validate_graph_contract
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine


class PptxScheduleRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_root = ROOT / "share" / "共有ドライブ"
        cls.engine = StructuredCandidateEngine(cls.source_root, build_glossary(cls.source_root))
        with (ROOT / "share" / "質問回答" / "questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            cls.questions = dict(csv.reader(handle))

    def test_exact_grammars_build_live_contracts(self):
        for qid in ("51", "69", "75", "88"):
            contract = graph_contract_for_question(self.questions[qid])
            self.assertIsNotNone(contract)
            self.assertTrue(validate_graph_contract(self.questions[qid], contract))
            self.assertEqual(contract, dispatch_contract(self.questions[qid]))

    def test_paraphrases_are_not_accepted(self):
        self.assertIsNone(graph_contract_for_question("パイロット運用は何週目ですか。"))

    def test_actual_proposal_schedule_span(self):
        decision = decide_question(self.engine, self.questions["51"])
        self.assertEqual("resolved", decision.status)
        self.assertEqual("第6週目から第8週目", decision.result.answer)
        self.assertEqual(1, len(decision.result.source_paths))

    def test_actual_final_schedule_span(self):
        decision = decide_question(self.engine, self.questions["69"])
        self.assertEqual("resolved", decision.status)
        self.assertEqual("第5週目から第6週目", decision.result.answer)
        self.assertEqual(1, len(decision.result.source_paths))

    def test_actual_minamino_model_build_week(self):
        decision = decide_question(self.engine, self.questions["75"])
        self.assertEqual("resolved", decision.status)
        self.assertEqual("4週目", decision.result.answer)
        self.assertEqual(2, len(decision.result.source_paths))

    def test_actual_minamino_week_five_items(self):
        decision = decide_question(self.engine, self.questions["88"])
        self.assertEqual("resolved", decision.status)
        self.assertEqual("解釈・業務示唆整理", decision.result.answer)
        self.assertEqual(1, decision.result.output_count)
        self.assertEqual(2, len(decision.result.source_paths))

    def test_live_contract_requires_graph_plan(self):
        for qid in ("51", "69", "75", "88"):
            decision = self.engine.decide(qid, self.questions[qid])
            self.assertEqual("hold", decision.status)
            self.assertEqual("extended_graph_plan_required", decision.reason)


if __name__ == "__main__":
    unittest.main()
