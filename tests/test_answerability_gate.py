import unittest

from answerability_gate import evaluate_answerability


class AnswerabilityGateTests(unittest.TestCase):
    def test_answers_only_with_complete_unique_evidence(self):
        decision = evaluate_answerability(
            required_conditions={"complete": True},
            interpretations={"document_value": "A", "visual_value": "A"},
            selected_candidates=("A",),
        )
        self.assertEqual(decision.action, "answer")
        self.assertEqual(decision.reason_codes, ())

    def test_abstains_when_a_required_condition_is_missing(self):
        decision = evaluate_answerability(
            required_conditions={"unit_known": False, "source_complete": True},
            interpretations={"literal": 1},
            selected_candidates=(1,),
        )
        self.assertEqual(decision.action, "abstain")
        self.assertIn("extraction_unresolved", decision.reason_codes)
        self.assertEqual(decision.details["missing_conditions"], ("unit_known",))

    def test_abstains_when_interpretations_conflict(self):
        decision = evaluate_answerability(
            required_conditions={"complete": True},
            interpretations={"physical_page": 6, "printed_page": 5},
            selected_candidates=(6,),
        )
        self.assertEqual(decision.action, "abstain")
        self.assertIn("intent_ambiguous", decision.reason_codes)

    def test_abstains_on_zero_or_multiple_winners(self):
        for candidates in ((), ("A", "B"), ("A", "A")):
            with self.subTest(candidates=candidates):
                decision = evaluate_answerability(
                    required_conditions={"complete": True},
                    interpretations={"literal": "A"},
                    selected_candidates=candidates,
                )
                self.assertEqual(decision.action, "abstain")
                self.assertIn("conflicting_evidence", decision.reason_codes)


if __name__ == "__main__":
    unittest.main()
