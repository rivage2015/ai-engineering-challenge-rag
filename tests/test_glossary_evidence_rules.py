import csv
import unittest
from pathlib import Path
from types import SimpleNamespace

from glossary import build_glossary
from glossary_evidence_rules import (
    Q026,
    Q037,
    Q076,
    decide_question,
    graph_contract_for_question,
    validate_graph_contract,
)
from score_candidate_rules import graph_contract_for_question as dispatched_contract
from structured_candidate import StructuredCandidateEngine


ROOT = Path(__file__).resolve().parents[1]


class GlossaryEvidenceRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_root = ROOT / "share" / "共有ドライブ"
        cls.glossary = build_glossary(cls.source_root)
        cls.engine = StructuredCandidateEngine(cls.source_root, cls.glossary)
        with (ROOT / "share" / "質問回答" / "questions_test.csv").open(
            encoding="utf-8-sig", newline=""
        ) as handle:
            cls.questions = dict(csv.reader(handle))

    def test_exact_question_grammars_and_dispatch(self):
        for qid, question in (("26", Q026), ("37", Q037), ("76", Q076)):
            with self.subTest(qid=qid):
                self.assertEqual(self.questions[qid], question)
                contract = graph_contract_for_question(question)
                self.assertTrue(validate_graph_contract(question, contract))
                self.assertEqual(dispatched_contract(question), contract)
                self.assertTrue(contract["graph_contract_id"].startswith("glossary_evidence_"))

    def test_actual_answers_are_derived_with_glossary_provenance(self):
        expected = {
            Q026: "TOTO、AOMINE",
            Q037: "22,000円",
            Q076: "73,260円増加します。",
        }
        for question, answer in expected.items():
            with self.subTest(question=question):
                decision = decide_question(self.engine, question)
                self.assertEqual(decision.status, "resolved")
                self.assertEqual(decision.result.answer, answer)
                self.assertIn("社内管理/社内用語集.docx", decision.result.source_paths)
                self.assertEqual(decision.reason, "certified_glossary_evidence_graph")

    def test_missing_or_ambiguous_glossary_edge_holds(self):
        canonical = "株式会社青葉バイオメディカル機器"
        cases = []
        missing = {key: list(values) for key, values in self.glossary.primary_entries.items()}
        missing.pop("AOBM")
        cases.append(missing)
        ambiguous = {key: list(values) for key, values in self.glossary.primary_entries.items()}
        ambiguous["AOBM-ALT"] = [canonical]
        cases.append(ambiguous)
        for primary in cases:
            with self.subTest(primary_count=len(primary)):
                glossary = SimpleNamespace(
                    entries=self.glossary.entries,
                    primary_entries=primary,
                )
                engine = StructuredCandidateEngine(self.source_root, glossary)
                decision = decide_question(engine, Q037)
                self.assertEqual(decision.status, "hold")
                self.assertEqual(decision.reason, "glossary_evidence_not_certified")

    def test_live_contract_requires_exact_certified_plan(self):
        for question in (Q026, Q037, Q076):
            with self.subTest(question=question):
                direct = self.engine.decide("opaque", question)
                self.assertEqual(direct.status, "hold")
                self.assertEqual(direct.reason, "extended_graph_plan_required")

                contract = graph_contract_for_question(question)
                plan = SimpleNamespace(
                    original_question=question,
                    strict_status="pass",
                    branch_intents=(
                        {"status": "resolved", "intent": {"extended_graph_contract": contract}},
                    ),
                )
                resolved = self.engine.decide_from_graph("opaque", question, plan)
                self.assertEqual(resolved.status, "resolved")


if __name__ == "__main__":
    unittest.main()
