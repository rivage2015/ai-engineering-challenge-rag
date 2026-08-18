import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))

from cross_project_personnel_graph_rules import (
    QUESTION,
    QUESTION_COUNT,
    decide_question,
    graph_contract_for_question,
    validate_graph_contract,
)
from evidence_graph_memory import load_graph
from glossary import build_glossary
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine


class CrossProjectPersonnelGraphRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = (ROOT / "share/共有ドライブ").resolve()
        cls.engine = StructuredCandidateEngine(cls.root, build_glossary(cls.root))
        with (ROOT / "share/質問回答/questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            cls.questions = dict(csv.reader(handle))

    def test_q013_contract_is_exact_and_dispatched(self):
        self.assertEqual(self.questions["13"], QUESTION)
        contract = graph_contract_for_question(QUESTION)
        self.assertIsNotNone(contract)
        self.assertTrue(contract["graph_contract_id"].startswith("personnel_graph_"))
        self.assertTrue(validate_graph_contract(QUESTION, contract))
        self.assertEqual(contract, dispatch_contract(QUESTION))
        self.assertIsNone(graph_contract_for_question(QUESTION + "推測でも構いません。"))

    def test_actual_q013_resolves_unique_maximum_to_extension(self):
        decision = decide_question(self.engine, QUESTION)
        self.assertEqual(("resolved", "certified_cross_project_personnel_evidence_graph"), (decision.status, decision.reason))
        self.assertEqual("7104", decision.result.answer)
        self.assertEqual(14, decision.result.operation_count)
        self.assertEqual(31, len(decision.result.source_paths))

    def test_actual_q013_persists_and_reloads_validated_json_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = StructuredCandidateEngine(self.root, build_glossary(self.root))
            engine.evidence_graph_memory_dir = Path(directory)
            first = decide_question(engine, QUESTION)
            path = Path(directory) / "Q013.evidence-graph.json"
            graph = load_graph(path)
            self.assertEqual("ready", graph["state"])
            self.assertEqual("verified", graph["answer_projection"]["status"])
            self.assertEqual(45, len(graph["edges"]))
            self.assertTrue(all(edge["status"] == "verified" for edge in graph["edges"]))
            second = decide_question(engine, QUESTION)
            self.assertEqual(first, second)

    def test_actual_q086_counts_only_certified_da_people_across_four_document_classes(self):
        self.assertEqual(self.questions["86"], QUESTION_COUNT)
        contract = graph_contract_for_question(QUESTION_COUNT)
        self.assertIsNotNone(contract)
        self.assertTrue(validate_graph_contract(QUESTION_COUNT, contract))
        self.assertEqual(contract, dispatch_contract(QUESTION_COUNT))
        decision = decide_question(self.engine, QUESTION_COUNT)
        self.assertEqual(
            ("resolved", "certified_cross_project_role_personnel_evidence_graph"),
            (decision.status, decision.reason),
        )
        self.assertEqual("12人", decision.result.answer)
        self.assertEqual(14, decision.result.operation_count)
        self.assertEqual(42, len(decision.result.source_paths))
        self.assertEqual(len(decision.result.source_paths), len(set(decision.result.source_paths)))

    def test_actual_q086_persists_audited_edges_and_reloads(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = StructuredCandidateEngine(self.root, build_glossary(self.root))
            engine.evidence_graph_memory_dir = Path(directory)
            first = decide_question(engine, QUESTION_COUNT)
            graph = load_graph(Path(directory) / "Q086.evidence-graph.json")
            self.assertEqual("ready", graph["state"])
            self.assertEqual("verified", graph["answer_projection"]["status"])
            self.assertEqual(45, len(graph["edges"]))
            self.assertTrue(all(edge["status"] == "verified" for edge in graph["edges"]))
            self.assertEqual(first, decide_question(engine, QUESTION_COUNT))

    def test_live_path_requires_graph_plan(self):
        decision = self.engine.decide("13", QUESTION)
        self.assertEqual(("hold", "extended_graph_plan_required"), (decision.status, decision.reason))
        decision = self.engine.decide("86", QUESTION_COUNT)
        self.assertEqual(("hold", "extended_graph_plan_required"), (decision.status, decision.reason))


if __name__ == "__main__":
    unittest.main()
