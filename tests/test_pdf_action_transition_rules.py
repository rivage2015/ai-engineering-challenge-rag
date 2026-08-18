import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag"))

from glossary import build_glossary
from evidence_graph_memory import load_graph
from pdf_action_transition_rules import Q045, Q070, decide_question, graph_contract_for_question, validate_graph_contract
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine


class PdfActionTransitionRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = ROOT / "share/共有ドライブ"
        cls.engine = StructuredCandidateEngine(cls.root, build_glossary(cls.root))
        with (ROOT / "share/質問回答/questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            cls.questions = dict(csv.reader(handle))

    def test_exact_contract_is_dispatched(self):
        question = self.questions["34"]
        contract = graph_contract_for_question(question)
        self.assertIsNotNone(contract)
        self.assertTrue(validate_graph_contract(question, contract))
        self.assertEqual(contract, dispatch_contract(question))

    def test_paraphrase_and_answer_injection_are_rejected(self):
        self.assertIsNone(graph_contract_for_question("M01からM02で完了したものを教えてください。"))
        self.assertIsNone(graph_contract_for_question(self.questions["34"] + " A08、A09"))

    def test_actual_scanned_minutes_resolve_transitions(self):
        decision = decide_question(self.engine, self.questions["34"])
        self.assertEqual("resolved", decision.status)
        self.assertEqual("certified_pdf_action_transition", decision.reason)
        self.assertEqual("A08、A09", decision.result.answer)
        self.assertEqual(3, len(decision.result.source_paths))

    def test_q045_exact_contract_is_dispatched_and_paraphrase_is_rejected(self):
        self.assertEqual(self.questions["45"], Q045)
        contract = graph_contract_for_question(Q045)
        self.assertIsNotNone(contract)
        self.assertTrue(validate_graph_contract(Q045, contract))
        self.assertEqual(contract, dispatch_contract(Q045))
        self.assertIsNone(graph_contract_for_question(Q045 + "推測でも構いません。"))

    def test_actual_q045_resolves_only_open_to_closed_shared_ids(self):
        decision = decide_question(self.engine, Q045)
        self.assertEqual(
            ("resolved", "certified_audited_pdf_action_transition_graph"),
            (decision.status, decision.reason),
        )
        self.assertEqual("A01、A02、A03、A07、A08、A09、A10", decision.result.answer)
        self.assertEqual(2, len(decision.result.source_paths))
        self.assertEqual(7, decision.result.output_count)
        self.assertEqual(11, decision.result.operation_count)

    def test_actual_q045_persists_verified_json_graph_and_reloads(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = StructuredCandidateEngine(self.root, build_glossary(self.root))
            engine.evidence_graph_memory_dir = Path(directory)
            first = decide_question(engine, Q045)
            graph = load_graph(Path(directory) / "Q045.evidence-graph.json")
            self.assertEqual("ready", graph["state"])
            self.assertEqual("verified", graph["answer_projection"]["status"])
            self.assertEqual(8, len(graph["edges"]))
            self.assertTrue(all(edge["status"] == "verified" for edge in graph["edges"]))
            self.assertEqual(first, decide_question(engine, Q045))

    def test_q070_exact_contract_is_dispatched(self):
        self.assertEqual(self.questions["70"], Q070)
        contract = graph_contract_for_question(Q070)
        self.assertIsNotNone(contract)
        self.assertTrue(validate_graph_contract(Q070, contract))
        self.assertEqual(contract, dispatch_contract(Q070))
        self.assertIsNone(graph_contract_for_question(Q070 + "AI-05"))

    def test_actual_q070_joins_report_priority_open_to_minutes_status(self):
        decision = decide_question(self.engine, Q070)
        self.assertEqual(
            ("resolved", "certified_audited_report_minutes_action_graph"),
            (decision.status, decision.reason),
        )
        self.assertEqual("AI-05", decision.result.answer)
        self.assertEqual(2, len(decision.result.source_paths))
        self.assertEqual(1, decision.result.output_count)
        self.assertEqual(12, decision.result.operation_count)

    def test_actual_q070_persists_three_verified_cross_document_edges(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = StructuredCandidateEngine(self.root, build_glossary(self.root))
            engine.evidence_graph_memory_dir = Path(directory)
            first = decide_question(engine, Q070)
            graph = load_graph(Path(directory) / "Q070.evidence-graph.json")
            self.assertEqual("ready", graph["state"])
            self.assertEqual("verified", graph["answer_projection"]["status"])
            self.assertEqual(3, len(graph["edges"]))
            self.assertEqual(6, len(graph["nodes"]))
            self.assertTrue(all(edge["status"] == "verified" for edge in graph["edges"]))
            self.assertEqual(first, decide_question(engine, Q070))

    def test_live_contract_requires_graph_plan(self):
        decision = self.engine.decide("34", self.questions["34"])
        self.assertEqual(("hold", "extended_graph_plan_required"), (decision.status, decision.reason))
        decision = self.engine.decide("45", Q045)
        self.assertEqual(("hold", "extended_graph_plan_required"), (decision.status, decision.reason))
        decision = self.engine.decide("70", Q070)
        self.assertEqual(("hold", "extended_graph_plan_required"), (decision.status, decision.reason))


if __name__ == "__main__":
    unittest.main()
