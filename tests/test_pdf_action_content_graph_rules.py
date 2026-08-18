import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))

from glossary import build_glossary
from pdf_action_content_graph_rules import (
    _supported_small_kana_correction,
    decide_question,
    graph_contract_for_question,
    validate_graph_contract,
)
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine


class PdfActionContentGraphRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_root = ROOT / "share" / "共有ドライブ"
        cls.engine = StructuredCandidateEngine(cls.source_root, build_glossary(cls.source_root))
        with (ROOT / "share" / "質問回答" / "questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            cls.questions = dict(csv.reader(handle))

    def test_actual_q093_builds_and_dispatches_stable_graph(self):
        question = self.questions["93"]
        contract = graph_contract_for_question(question)
        self.assertIsNotNone(contract)
        self.assertTrue(contract["graph_contract_id"].startswith("pdf_action_content_"))
        self.assertTrue(validate_graph_contract(question, contract))
        self.assertEqual(contract, dispatch_contract(question))

    def test_actual_q093_recovers_original_action_text(self):
        decision = decide_question(self.engine, self.questions["93"])
        self.assertEqual("resolved", decision.status)
        self.assertEqual(
            "前処理パイプライン実装：0値を疑似欠損（NA）扱いにする処理と補完ロジック（中央値等）を実装・ドキュメント化",
            decision.result.answer,
        )
        self.assertEqual(12, decision.result.operation_count)
        self.assertEqual(3, len(decision.result.source_paths))

    def test_glyph_correction_requires_two_supporting_observations(self):
        primary = "実装・ドキユメント化"
        self.assertEqual(
            "実装・ドキュメント化",
            _supported_small_kana_correction(primary, ("実装・ドキュメント化", "実装・ドキュメント化")),
        )
        self.assertIsNone(
            _supported_small_kana_correction(primary, ("実装・ドキュメント化", "実装・ドキユメント化"))
        )

    def test_paraphrase_does_not_match(self):
        self.assertIsNone(graph_contract_for_question("A10の内容は何ですか。"))

    def test_live_contract_requires_certified_graph_plan(self):
        decision = self.engine.decide("93", self.questions["93"])
        self.assertEqual("hold", decision.status)
        self.assertEqual("extended_graph_plan_required", decision.reason)


if __name__ == "__main__":
    unittest.main()
