from __future__ import annotations

import csv
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "rag"), str(ROOT / "scripts")]

from glossary import build_glossary
from question_graph_runtime import build_graph_plan
from report_metric_delta_graph_rules import Q036, _docx_f1, _pptx_f1, _sources, graph_contract_for_question, validate_graph_contract
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine


class ReportMetricDeltaGraphRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = (ROOT / "share" / "共有ドライブ").resolve()
        cls.engine = StructuredCandidateEngine(cls.root, build_glossary(cls.root))

    def test_exact_contract_is_dispatched(self):
        with (ROOT / "share" / "質問回答" / "questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            self.assertEqual(dict(csv.reader(handle))["36"], Q036)
        contract = graph_contract_for_question(Q036)
        self.assertTrue(validate_graph_contract(Q036, contract))
        self.assertEqual(contract, dispatch_contract(Q036))
        self.assertIsNone(graph_contract_for_question(Q036 + " "))

    def test_actual_reports_preserve_their_own_display_precision(self):
        bound = _sources(self.engine)
        self.assertIsNotNone(bound)
        self.assertEqual("0.7329671168078127", _docx_f1(bound[1]))
        self.assertEqual(("0.8292", 8), _pptx_f1(bound[2]))
        self.assertEqual(Decimal("0.0962328831921873"), abs(Decimal("0.8292") - Decimal("0.7329671168078127")))

    def test_actual_q036_resolves_report_to_report_delta(self):
        plan = build_graph_plan("36", Q036, fast_advisory=True)
        decision = self.engine.decide_from_graph("36", Q036, plan)
        self.assertEqual("pass", plan.strict_status)
        self.assertEqual(("resolved", "certified_cross_report_metric_delta_graph"), (decision.status, decision.reason))
        self.assertEqual("0.0962328831921873", decision.result.answer)
        self.assertEqual(12, decision.result.operation_count)

    def test_live_path_requires_graph_plan(self):
        decision = self.engine.decide("36", Q036)
        self.assertEqual(("hold", "extended_graph_plan_required"), (decision.status, decision.reason))


if __name__ == "__main__":
    unittest.main()
