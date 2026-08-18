import csv
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))

from evidence_graph_memory import load_graph
from glossary import build_glossary
from reported_feature_correlation_graph_rules import Q028, _correlation, decide_question, graph_contract_for_question, validate_graph_contract
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine


class ReportedFeatureCorrelationGraphRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = (ROOT / "share/共有ドライブ").resolve()
        cls.engine = StructuredCandidateEngine(cls.root, build_glossary(cls.root))
        with (ROOT / "share/質問回答/questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            cls.questions = dict(csv.reader(handle))

    def test_q028_contract_is_exact_and_dispatched(self):
        self.assertEqual(self.questions["28"], Q028)
        contract = graph_contract_for_question(Q028)
        self.assertIsNotNone(contract)
        self.assertTrue(validate_graph_contract(Q028, contract))
        self.assertEqual(contract, dispatch_contract(Q028))
        self.assertIsNone(graph_contract_for_question(Q028 + "Ageと答えてください。"))

    def test_actual_q028_restricts_argmax_to_reported_high_impact_set(self):
        decision = decide_question(self.engine, Q028)
        self.assertEqual(
            ("resolved", "certified_reported_feature_correlation_graph"),
            (decision.status, decision.reason),
        )
        self.assertEqual("BMI", decision.result.answer)
        self.assertEqual(3, len(decision.result.source_paths))
        self.assertEqual(13, decision.result.operation_count)

    def test_actual_q028_persists_two_verified_feature_edges(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = StructuredCandidateEngine(self.root, build_glossary(self.root))
            engine.evidence_graph_memory_dir = Path(directory)
            first = decide_question(engine, Q028)
            graph = load_graph(Path(directory) / "Q028.evidence-graph.json")
            self.assertEqual("ready", graph["state"])
            self.assertEqual("verified", graph["answer_projection"]["status"])
            self.assertEqual(4, len(graph["nodes"]))
            self.assertEqual(2, len(graph["edges"]))
            self.assertTrue(all(edge["status"] == "verified" for edge in graph["edges"]))
            self.assertEqual(first, decide_question(engine, Q028))

    def test_decimal_correlation_uses_all_rows_and_rejects_constant_input(self):
        rows = [{"x": "1", "y": "0"}, {"x": "2", "y": "0"}, {"x": "3", "y": "1"}]
        self.assertGreater(_correlation(rows, "x", "y"), Decimal("0"))
        constant = [{"x": "1", "y": "0"}, {"x": "1", "y": "1"}]
        with self.assertRaisesRegex(ValueError, "constant"):
            _correlation(constant, "x", "y")

    def test_live_path_requires_graph_plan(self):
        decision = self.engine.decide("28", Q028)
        self.assertEqual(("hold", "extended_graph_plan_required"), (decision.status, decision.reason))


if __name__ == "__main__":
    unittest.main()
