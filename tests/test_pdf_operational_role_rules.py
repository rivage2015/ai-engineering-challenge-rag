import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))

from glossary import build_glossary
from pdf_operational_role_rules import decide_question, graph_contract_for_question, validate_graph_contract
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine


class PdfOperationalRoleRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = ROOT / "share" / "共有ドライブ"
        cls.engine = StructuredCandidateEngine(cls.root, build_glossary(cls.root))
        with (ROOT / "share" / "質問回答" / "questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            cls.question = dict(csv.reader(handle))["52"]

    def test_exact_question_builds_live_contract(self):
        contract = graph_contract_for_question(self.question)
        self.assertTrue(contract["graph_contract_id"].startswith("pdfrole_"))
        self.assertTrue(validate_graph_contract(self.question, contract))
        self.assertEqual(contract, dispatch_contract(self.question))

    def test_paraphrase_does_not_match(self):
        self.assertIsNone(graph_contract_for_question("別契約の役割は何ですか。"))

    def test_actual_source_requires_independent_row_consensus(self):
        decision = decide_question(self.engine, self.question)
        self.assertEqual("resolved", decision.status)
        self.assertEqual("監視ダッシュボード構築（別契約）", decision.result.answer)
        self.assertEqual(1, len(decision.result.source_paths))

    def test_live_contract_cannot_bypass_graph_plan(self):
        decision = self.engine.decide("52", self.question)
        self.assertEqual("hold", decision.status)
        self.assertEqual("extended_graph_plan_required", decision.reason)


if __name__ == "__main__":
    unittest.main()
