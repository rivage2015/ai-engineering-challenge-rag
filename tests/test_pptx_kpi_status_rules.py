import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))

from glossary import build_glossary
from pptx_kpi_status_rules import QUESTION, _kpi_rows, _sources, decide_question, graph_contract_for_question, validate_graph_contract
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine


class PptxKpiStatusRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = (ROOT / "share/共有ドライブ").resolve()
        cls.engine = StructuredCandidateEngine(cls.root, build_glossary(cls.root))
        with (ROOT / "share/質問回答/questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            cls.questions = dict(csv.reader(handle))

    def test_exact_contract_is_dispatched(self):
        self.assertEqual(QUESTION, self.questions["85"])
        contract = graph_contract_for_question(QUESTION)
        self.assertTrue(validate_graph_contract(QUESTION, contract))
        self.assertEqual(contract, dispatch_contract(QUESTION))
        self.assertIsNone(graph_contract_for_question(QUESTION + " 該当なし"))

    def test_actual_table_contains_six_explicit_achieved_rows(self):
        _root, _glossary, report = _sources(self.engine)
        rows = _kpi_rows(report)
        self.assertEqual(6, len(rows))
        self.assertEqual({"達成"}, {evaluation for _label, evaluation in rows})

    def test_actual_graph_resolves_explicit_empty_result(self):
        decision = decide_question(self.engine, QUESTION)
        self.assertEqual(("resolved", "certified_pptx_complete_kpi_status_table"), (decision.status, decision.reason))
        self.assertEqual("該当するものはありません。", decision.result.answer)
        self.assertEqual(10, decision.result.operation_count)
        self.assertEqual(0, decision.result.output_count)

    def test_live_contract_requires_graph_plan(self):
        decision = self.engine.decide("85", QUESTION)
        self.assertEqual(("hold", "extended_graph_plan_required"), (decision.status, decision.reason))


if __name__ == "__main__":
    unittest.main()
