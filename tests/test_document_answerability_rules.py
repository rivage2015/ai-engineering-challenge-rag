import csv
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from answer import validate_graph_answer
from document_answerability_rules import (
    Q048,
    Q052,
    Q084,
    _absolute_difference_interval,
    _parse_q048_table,
    _unique_interval_argmin,
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
        self.assertEqual(self.questions["52"], Q052)
        self.assertEqual(self.questions["84"], Q084)

    def test_contracts_are_dispatched_and_answers_validate(self):
        for question in (Q048, Q052, Q084):
            with self.subTest(question=question):
                contract = graph_contract_for_question(question)
                self.assertEqual(dispatched_contract(question), contract)
                expected_policy = "hold" if question == Q048 else "abstain"
                self.assertEqual(contract["scope"]["ambiguity_policy"], expected_policy)
                answer = "100 万ドル超 - 500 万ドル以下" if question == Q048 else "わかりません"
                self.assertEqual(validate_graph_answer(answer, contract), ())

    def test_actual_sources_certify_answer_or_abstention(self):
        decision = decide_question(self.engine, Q048)
        self.assertEqual(("resolved", "certified_interval_dominance_argmin"), (decision.status, decision.reason))
        self.assertEqual("100 万ドル超 - 500 万ドル以下", decision.result.answer)
        self.assertEqual(len(decision.result.source_paths), 1)

        for question in (Q052, Q084):
            with self.subTest(question=question):
                decision = decide_question(self.engine, question)
                self.assertEqual(decision.status, "resolved")
                self.assertEqual(decision.result.answer, "わかりません")
                self.assertIn(decision.reason, {
                    "certified_condition_insufficiency_abstention",
                    "certified_entity_identity_insufficiency_abstention",
                })
                self.assertEqual(len(decision.result.source_paths), 1)

    def test_live_rule_requires_matching_graph_plan(self):
        for question in (Q048, Q052, Q084):
            with self.subTest(question=question):
                decision = self.engine.decide("opaque", question)
                self.assertEqual(decision.status, "hold")
                self.assertEqual(decision.reason, "extended_graph_plan_required")

    def test_exact_certified_graph_plan_executes(self):
        for question in (Q048, Q052, Q084):
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
                expected = "100 万ドル超 - 500 万ドル以下" if question == Q048 else "わかりません"
                self.assertEqual(decision.result.answer, expected)

    def test_interval_dominance_selects_only_a_robust_unique_minimum(self):
        rows = [
            ("A", Decimal("0"), Decimal("0"), Decimal("1.425")),
            ("B", Decimal("1.00"), Decimal("1.50"), Decimal("1.425")),
            ("C", Decimal("2.25"), Decimal("2.25"), Decimal("3.675")),
        ]
        winner, intervals = _unique_interval_argmin(rows)
        self.assertEqual("B", winner)
        self.assertEqual(("B", Decimal("0"), Decimal("0.425")), intervals[1])

    def test_interval_overlap_or_tie_is_not_forced(self):
        cases = (
            [
                ("A", Decimal("0"), Decimal("2"), Decimal("1")),
                ("B", Decimal("0"), Decimal("0"), Decimal("0.5")),
            ],
            [
                ("A", Decimal("0"), Decimal("0"), Decimal("1")),
                ("B", Decimal("2"), Decimal("2"), Decimal("1")),
            ],
        )
        for rows in cases:
            with self.subTest(rows=rows):
                with self.assertRaises(ValueError):
                    _unique_interval_argmin(rows)

    def test_absolute_difference_interval_covers_below_inside_above_and_invalid(self):
        self.assertEqual(
            (Decimal("1"), Decimal("2")),
            _absolute_difference_interval(Decimal("2"), Decimal("3"), Decimal("1")),
        )
        self.assertEqual(
            (Decimal("0"), Decimal("1.5")),
            _absolute_difference_interval(Decimal("1"), Decimal("3"), Decimal("1.5")),
        )
        self.assertEqual(
            (Decimal("1"), Decimal("2")),
            _absolute_difference_interval(Decimal("1"), Decimal("2"), Decimal("3")),
        )
        for values in (
            (Decimal("2"), Decimal("1"), Decimal("1.5")),
            (Decimal("NaN"), Decimal("1"), Decimal("1.5")),
        ):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    _absolute_difference_interval(*values)

    def test_table_parser_rejects_unconsumed_rate_and_missing_inclusive_tail(self):
        table = (
            "物件価格帯現行税率提案されている新税率"
            "50万ドル超-100万ドル以0.0%1.425%下"
            "100万ドル超-500万ドル以1.00%-1.50%1.425%下"
            "500万ドル超-1,000万ドル2.25%3.675%以下"
            "1,000万ドル超-1,500万ド3.25%4.675%ル以下"
            "1,500万ドル超-2,000万ド3.50%4.925%ル以下"
            "2,000万ドル超-2,500万ド3.75%5.175%ル以下"
            "2,500万ドル超3.90%5.325%\n19"
        )
        self.assertEqual(7, len(_parse_q048_table(table)))
        mutations = (
            table.replace("1.425%下100", "1.425%99.9%下100", 1),
            table.replace("1.425%下100", "1.425%100", 1),
            table.replace("5.325%\n19", "5.325%7\n19"),
            table.replace("5.325%\n19", "5.325%719"),
        )
        for changed in mutations:
            with self.subTest(changed=changed):
                with self.assertRaises(ValueError):
                    _parse_q048_table(changed)


if __name__ == "__main__":
    unittest.main()
