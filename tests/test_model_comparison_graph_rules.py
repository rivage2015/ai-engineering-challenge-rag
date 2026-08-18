import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))

from evidence_graph_memory import load_graph
from glossary import build_glossary
from model_comparison_graph_rules import (
    Q035,
    Q062,
    _q035_rows,
    _q035_source,
    _leaderboard_top_two,
    decide_question,
    graph_contract_for_question,
    validate_graph_contract,
)
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine


class ModelComparisonGraphRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = (ROOT / "share/共有ドライブ").resolve()
        cls.engine = StructuredCandidateEngine(cls.root, build_glossary(cls.root))
        with (ROOT / "share/質問回答/questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            cls.questions = dict(csv.reader(handle))

    def test_q062_contract_is_exact_and_dispatched(self):
        self.assertEqual(self.questions["62"], Q062)
        contract = graph_contract_for_question(Q062)
        self.assertIsNotNone(contract)
        self.assertTrue(validate_graph_contract(Q062, contract))
        self.assertEqual(contract, dispatch_contract(Q062))
        self.assertIsNone(graph_contract_for_question(Q062 + "答えは500と300です。"))

    def test_q035_contract_is_exact_and_dispatched(self):
        self.assertEqual(self.questions["35"], Q035)
        contract = graph_contract_for_question(Q035)
        self.assertIsNotNone(contract)
        self.assertTrue(validate_graph_contract(Q035, contract))
        self.assertEqual(contract, dispatch_contract(Q035))
        self.assertIsNone(graph_contract_for_question(Q035 + " "))

    def test_actual_q035_native_table_keeps_rank_and_metrics_on_same_row(self):
        bound = _q035_source(self.engine)
        self.assertIsNotNone(bound)
        rows = _q035_rows(bound[1])
        self.assertEqual((1, "gradient_boosting", "0.72243", "0.89993"), (rows[0]["rank"], rows[0]["model"], rows[0]["f1_display"], rows[0]["accuracy_display"]))
        self.assertEqual((2, "random_forest", "0.71486", "0.90527"), (rows[1]["rank"], rows[1]["model"], rows[1]["f1_display"], rows[1]["accuracy_display"]))

    def test_actual_q035_resolves_immediate_f1_successor_accuracy(self):
        decision = decide_question(self.engine, Q035)
        self.assertEqual(("resolved", "certified_pptx_rank_successor_metric_graph"), (decision.status, decision.reason))
        self.assertEqual("0.90527", decision.result.answer)
        self.assertEqual(13, decision.result.operation_count)

    def test_actual_q062_resolves_only_explicit_setting_difference(self):
        decision = decide_question(self.engine, Q062)
        self.assertEqual(
            ("resolved", "certified_audited_model_comparison_graph"),
            (decision.status, decision.reason),
        )
        self.assertEqual("n_estimatorsが500と300で異なります。", decision.result.answer)
        self.assertEqual(2, len(decision.result.source_paths))
        self.assertEqual(12, decision.result.operation_count)
        self.assertEqual(1, decision.result.output_count)

    def test_actual_q062_persists_two_verified_rank_to_trial_edges(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = StructuredCandidateEngine(self.root, build_glossary(self.root))
            engine.evidence_graph_memory_dir = Path(directory)
            first = decide_question(engine, Q062)
            graph = load_graph(Path(directory) / "Q062.evidence-graph.json")
            self.assertEqual("ready", graph["state"])
            self.assertEqual("verified", graph["answer_projection"]["status"])
            self.assertEqual(4, len(graph["nodes"]))
            self.assertEqual(2, len(graph["edges"]))
            self.assertTrue(all(edge["status"] == "verified" for edge in graph["edges"]))
            self.assertEqual(first, decide_question(engine, Q062))

    def test_tied_second_score_is_rejected(self):
        fields = [
            "trial_index", "status", "model_type", "n_estimators", "use_date_features",
            "random_state", "test_size", "task_type", "primary_metric", "primary_value",
            "secondary_metric", "secondary_value",
        ]
        rows = [
            ["1", "ok", "extra_trees", "500", "True", "42", "0.2", "classification", "f1_macro", "0.60", "accuracy", "0.8"],
            ["2", "ok", "extra_trees", "300", "True", "42", "0.2", "classification", "f1_macro", "0.59", "accuracy", "0.8"],
            ["3", "ok", "random_forest", "300", "True", "42", "0.2", "classification", "f1_macro", "0.59", "accuracy", "0.8"],
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "leaderboard.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(fields)
                writer.writerows(rows)
            with self.assertRaisesRegex(ValueError, "ranking is not unique"):
                _leaderboard_top_two(path)

    def test_live_path_requires_graph_plan(self):
        decision = self.engine.decide("62", Q062)
        self.assertEqual(("hold", "extended_graph_plan_required"), (decision.status, decision.reason))


if __name__ == "__main__":
    unittest.main()
