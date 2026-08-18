import csv
import unittest
from pathlib import Path
from types import SimpleNamespace

from glossary import build_glossary
from notebook_correlation_rules import Q004, decide_question, graph_contract_for_question
from score_candidate_rules import graph_contract_for_question as dispatched_contract
from structured_candidate import StructuredCandidateEngine


ROOT = Path(__file__).resolve().parents[1]


class NotebookCorrelationRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = ROOT / "share" / "共有ドライブ"
        cls.glossary = build_glossary(cls.root)
        cls.engine = StructuredCandidateEngine(cls.root, cls.glossary)
        with (ROOT / "share" / "質問回答" / "questions_test.csv").open(encoding="utf-8-sig", newline="") as handle:
            cls.questions = dict(csv.reader(handle))

    def test_actual_source_recomputes_bmi_not_stale_markdown_age(self):
        self.assertEqual(self.questions["4"], Q004)
        decision = decide_question(self.engine, Q004)
        self.assertEqual(decision.status, "resolved")
        self.assertEqual(decision.result.answer, "bmi")
        self.assertEqual(decision.reason, "certified_notebook_source_recomputed_correlation")
        self.assertEqual(len(decision.result.source_paths), 3)
        self.assertIn("社内管理/社内用語集.docx", decision.result.source_paths)

    def test_contract_dispatch_and_live_plan_gate(self):
        contract = graph_contract_for_question(Q004)
        self.assertEqual(dispatched_contract(Q004), contract)
        self.assertTrue(contract["graph_contract_id"].startswith("notebook_corr_"))
        self.assertEqual(self.engine.decide("4", Q004).reason, "extended_graph_plan_required")
        plan = SimpleNamespace(original_question=Q004, strict_status="pass", branch_intents=({"status": "resolved", "intent": {"extended_graph_contract": contract}},))
        self.assertEqual(self.engine.decide_from_graph("4", Q004, plan).result.answer, "bmi")

    def test_missing_glossary_location_edge_holds(self):
        entries = {key: list(values) for key, values in self.glossary.entries.items()}
        entries.pop("蒼泉会")
        engine = StructuredCandidateEngine(self.root, SimpleNamespace(entries=entries, primary_entries=self.glossary.primary_entries))
        decision = decide_question(engine, Q004)
        self.assertEqual(decision.status, "hold")
        self.assertEqual(decision.reason, "notebook_correlation_not_certified")


if __name__ == "__main__":
    unittest.main()
