from __future__ import annotations

import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "rag"), str(ROOT / "scripts")]

from glossary import build_glossary
from priority_task_owner_graph_rules import Q020, _action_rows, _sources, graph_contract_for_question, validate_graph_contract
from question_graph_runtime import build_graph_plan
from structured_candidate import StructuredCandidateEngine


class PriorityTaskOwnerGraphRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_root = (ROOT / "share" / "共有ドライブ").resolve()
        cls.engine = StructuredCandidateEngine(cls.source_root, build_glossary(cls.source_root))

    def test_exact_contract_and_tamper_rejection(self):
        contract = graph_contract_for_question(Q020)
        self.assertIsNotNone(contract)
        self.assertTrue(validate_graph_contract(Q020, contract))
        tampered = dict(contract)
        tampered["bindings"] = {**contract["bindings"], "owners": ["渡辺遥"]}
        self.assertFalse(validate_graph_contract(Q020, tampered))
        self.assertIsNone(graph_contract_for_question(Q020 + " "))

    def test_actual_action_table_preserves_distinct_owner_sets(self):
        bound = _sources(self.engine)
        self.assertIsNotNone(bound)
        rows = _action_rows(bound[2])
        self.assertEqual(rows["T03"], ("分析計画書初版作成", ("渡辺遥", "藤田彩")))
        self.assertEqual(rows["T09"][1], ("渡辺遥", "斎藤悠斗"))
        self.assertEqual(rows["T11"][1], ("渡辺遥", "藤田彩"))

    def test_actual_question_resolves_only_report_priority_member(self):
        plan = build_graph_plan("20", Q020, fast_advisory=True)
        decision = self.engine.decide_from_graph("20", Q020, plan)
        self.assertEqual(plan.strict_status, "pass")
        self.assertEqual(decision.status, "resolved")
        self.assertEqual(decision.reason, "certified_priority_task_owner_graph")
        self.assertEqual(decision.result.answer, "分析計画書初版作成")
        self.assertEqual(decision.result.operation_count, 13)

    def test_csv_question_is_exact_q020(self):
        with (ROOT / "share" / "質問回答" / "questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            questions = dict(csv.reader(handle))
        self.assertEqual(questions["20"], Q020)


if __name__ == "__main__":
    unittest.main()
