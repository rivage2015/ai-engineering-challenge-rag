import csv
import unittest
from pathlib import Path
from types import SimpleNamespace

from answer import validate_graph_answer
from document_answerability_rules import (
    Q048,
    Q084,
    decide_question,
    graph_contract_for_question,
)
from glossary import build_glossary
from score_candidate_rules import graph_contract_for_question as dispatched_contract
from structured_candidate import StructuredCandidateEngine


ROOT = Path(__file__).resolve().parents[1]


class DocumentAnswerabilityRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_root = ROOT / "share" / "共有ドライブ"
        cls.engine = StructuredCandidateEngine(
            cls.source_root, build_glossary(cls.source_root)
        )
        with (ROOT / "share" / "質問回答" / "questions_test.csv").open(
            encoding="utf-8-sig", newline=""
        ) as handle:
            cls.questions = dict(csv.reader(handle))

    def test_question_constants_match_test_set(self):
        self.assertEqual(self.questions["48"], Q048)
        self.assertEqual(self.questions["84"], Q084)

    def test_contracts_are_dispatched_and_answers_validate(self):
        for question in (Q048, Q084):
            with self.subTest(question=question):
                contract = graph_contract_for_question(question)
                self.assertEqual(dispatched_contract(question), contract)
                self.assertEqual(contract["scope"]["ambiguity_policy"], "abstain")
                self.assertEqual(validate_graph_answer("わかりません", contract), ())

    def test_actual_sources_certify_abstention(self):
        for question in (Q048, Q084):
            with self.subTest(question=question):
                decision = decide_question(self.engine, question)
                self.assertEqual(decision.status, "resolved")
                self.assertEqual(decision.result.answer, "わかりません")
                self.assertEqual(
                    decision.reason, "certified_condition_insufficiency_abstention"
                )
                self.assertEqual(len(decision.result.source_paths), 1)

    def test_live_rule_requires_matching_graph_plan(self):
        for question in (Q048, Q084):
            with self.subTest(question=question):
                decision = self.engine.decide("opaque", question)
                self.assertEqual(decision.status, "hold")
                self.assertEqual(decision.reason, "extended_graph_plan_required")

    def test_exact_certified_graph_plan_executes(self):
        for question in (Q048, Q084):
            with self.subTest(question=question):
                contract = graph_contract_for_question(question)
                plan = SimpleNamespace(
                    original_question=question,
                    strict_status="pass",
                    branch_intents=(
                        {
                            "status": "resolved",
                            "intent": {"extended_graph_contract": contract},
                        },
                    ),
                )
                decision = self.engine.decide_from_graph("opaque", question, plan)
                self.assertEqual(decision.status, "resolved")
                self.assertEqual(decision.result.answer, "わかりません")


if __name__ == "__main__":
    unittest.main()
