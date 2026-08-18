from __future__ import annotations

import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "rag"), str(ROOT / "scripts")]

from contract_contact_graph_rules import Q021, Q043, _glossary_bindings, _kou_primary_contact, _q021_role, _q021_source, _sources, graph_contract_for_question, validate_graph_contract
from glossary import build_glossary
from question_graph_runtime import build_graph_plan
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine


class ContractContactGraphRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = (ROOT / "share" / "共有ドライブ").resolve()
        cls.engine = StructuredCandidateEngine(cls.root, build_glossary(cls.root))

    def test_exact_question_contract_is_dispatched(self):
        with (ROOT / "share" / "質問回答" / "questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            questions = dict(csv.reader(handle))
        self.assertEqual(questions["21"], Q021)
        self.assertEqual(questions["43"], Q043)
        for question in (Q021, Q043):
            contract = graph_contract_for_question(question)
            self.assertTrue(validate_graph_contract(question, contract))
            self.assertEqual(contract, dispatch_contract(question))
        self.assertIsNone(graph_contract_for_question(Q043 + " "))

    def test_glossary_expands_both_project_and_document_aliases(self):
        bound = _sources(self.engine)
        self.assertIsNotNone(bound)
        self.assertEqual(("株式会社東都人材プラットフォーム", "契約書"), _glossary_bindings(bound[1]))
        self.assertEqual("契約書.docx", bound[2].name)

    def test_native_contract_party_scope_returns_full_name(self):
        bound = _sources(self.engine)
        self.assertEqual("石川 直樹", _kou_primary_contact(bound[2]))

    def test_actual_q043_resolves_through_graph_plan(self):
        plan = build_graph_plan("43", Q043, fast_advisory=True)
        decision = self.engine.decide_from_graph("43", Q043, plan)
        self.assertEqual("pass", plan.strict_status)
        self.assertEqual(("resolved", "certified_contract_contact_graph"), (decision.status, decision.reason))
        self.assertEqual("石川 直樹", decision.result.answer)
        self.assertEqual(12, decision.result.operation_count)

    def test_q021_binds_role_to_same_kou_primary_contact_and_signature(self):
        bound = _q021_source(self.engine)
        self.assertIsNotNone(bound)
        self.assertEqual(("山田 太一", "人材戦略部長"), _q021_role(bound[1]))
        plan = build_graph_plan("21", Q021, fast_advisory=True)
        decision = self.engine.decide_from_graph("21", Q021, plan)
        self.assertEqual("pass", plan.strict_status)
        self.assertEqual(("resolved", "certified_contract_contact_graph"), (decision.status, decision.reason))
        self.assertEqual("人材戦略部長", decision.result.answer)
        self.assertEqual(9, decision.result.operation_count)


if __name__ == "__main__":
    unittest.main()
