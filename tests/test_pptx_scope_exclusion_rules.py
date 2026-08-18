import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "rag"), str(ROOT / "scripts")]

from glossary import build_glossary
from pptx_scope_exclusion_rules import _scope_exclusions, _source, graph_contract_for_question
from question_graph_runtime import build_graph_plan
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine


class PptxScopeExclusionRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = (ROOT / "share" / "共有ドライブ").resolve()
        cls.engine = StructuredCandidateEngine(cls.root, build_glossary(cls.root))
        with (ROOT / "share" / "質問回答" / "questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            cls.question = dict(csv.reader(handle))["27"]

    def test_exact_contract_is_dispatched(self):
        contract = graph_contract_for_question(self.question)
        self.assertTrue(contract["graph_contract_id"].startswith("pptx_scope_"))
        self.assertEqual(contract, dispatch_contract(self.question))
        self.assertIsNone(graph_contract_for_question(self.question + " "))

    def test_actual_notes_contain_seven_explicit_items(self):
        bound = _source(self.engine, "恒一会 かえで総合病院")
        self.assertIsNotNone(bound)
        items = _scope_exclusions(bound[1])
        self.assertEqual(7, len(items))
        self.assertIn("実運用システムへの組込み、API化、電子カルテ連携", items)
        self.assertIn("個票レベルでの個人特定、患者追跡、属性拡張", items)

    def test_actual_graph_resolves_count(self):
        plan = build_graph_plan("27", self.question, fast_advisory=True)
        decision = self.engine.decide_from_graph("27", self.question, plan)
        self.assertEqual("pass", plan.strict_status)
        self.assertEqual(("resolved", "certified_pptx_speaker_notes_scope_count"), (decision.status, decision.reason))
        self.assertEqual("7", decision.result.answer)
        self.assertEqual(8, decision.result.operation_count)


if __name__ == "__main__":
    unittest.main()
