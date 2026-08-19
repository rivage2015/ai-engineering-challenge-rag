import csv
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))

from encrypted_plan_workload_rules import _winner, decide_question, graph_contract_for_question, validate_graph_contract
from glossary import build_glossary
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine


class EncryptedPlanWorkloadRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_root = ROOT / "share" / "共有ドライブ"
        cls.engine = StructuredCandidateEngine(cls.source_root, build_glossary(cls.source_root))
        with (ROOT / "share" / "質問回答" / "questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            cls.question = dict(csv.reader(handle))["79"]

    def test_exact_question_builds_live_contract(self):
        contract = graph_contract_for_question(self.question)
        self.assertIsNotNone(contract)
        self.assertTrue(validate_graph_contract(self.question, contract))
        self.assertEqual(contract, dispatch_contract(self.question))

    def test_actual_encrypted_workbook_resolves_unique_winner(self):
        decision = decide_question(self.engine, self.question)
        self.assertEqual(("resolved", "certified_encrypted_plan_workload_ratio"), (decision.status, decision.reason))
        self.assertEqual("池田 直哉、7.00", decision.result.answer)
        self.assertGreaterEqual(len(decision.result.source_paths), 4)

    def test_zero_task_member_is_excluded_and_multi_assignees_are_counted(self):
        wbs = [
            ("タスクID", "担当者"),
            ("T01", "甲野 太郎、乙野 花子"),
            ("T02", "甲野 太郎"),
            ("T03", "乙野 花子"),
        ]
        resources = [
            ("役割", "氏名", "想定工数（時間）"),
            ("担当", "甲野 太郎", 6),
            ("担当", "乙野 花子", 8),
            ("スポンサー", "丙野 次郎", 100),
        ]
        self.assertEqual(("乙野 花子", Decimal("4"), 2, Decimal("8")), _winner(wbs, resources))

    def test_incomplete_tasks_unknown_people_and_ties_hold(self):
        resources = [("役割", "氏名", "想定工数（時間）"), ("担当", "甲野 太郎", 4), ("担当", "乙野 花子", 4)]
        cases = (
            [("タスクID", "担当者"), ("T02", "甲野 太郎")],
            [("タスクID", "担当者"), ("T01", "丙野 次郎")],
            [("タスクID", "担当者"), ("T01", "甲野 太郎"), ("T02", "乙野 花子")],
        )
        for rows in cases:
            with self.subTest(rows=rows):
                with self.assertRaises(ValueError):
                    _winner(rows, resources)

    def test_live_contract_requires_graph_plan(self):
        decision = self.engine.decide("79", self.question)
        self.assertEqual(("hold", "extended_graph_plan_required"), (decision.status, decision.reason))


if __name__ == "__main__":
    unittest.main()
