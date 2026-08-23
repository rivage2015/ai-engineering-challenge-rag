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
    _audit_secondary_role_mentions_text,
    _audit_secondary_role_mentions_units,
    _extract_role_roster_text,
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
        operators = {
            node["operator"] for node in contract["operation_graph"]["nodes"]
        }
        self.assertIn("extract_bidirectional_plan_fr_role_person_mentions", operators)
        self.assertIn("reject_person_outside_authoritative_project_roster", operators)
        self.assertIn("audit_plan_role_name_table_and_controlled_saito_variant", operators)
        decision = decide_question(self.engine, QUESTION_COUNT)
        self.assertEqual(
            ("resolved", "certified_cross_project_role_personnel_evidence_graph"),
            (decision.status, decision.reason),
        )
        self.assertEqual("19人", decision.result.answer)
        self.assertEqual(16, decision.result.operation_count)
        self.assertEqual(41, len(decision.result.source_paths))
        self.assertEqual(len(decision.result.source_paths), len(set(decision.result.source_paths)))

    def test_actual_q086_persists_audited_edges_and_reloads(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = StructuredCandidateEngine(self.root, build_glossary(self.root))
            engine.evidence_graph_memory_dir = Path(directory)
            first = decide_question(engine, QUESTION_COUNT)
            graph = load_graph(Path(directory) / "Q086.evidence-graph.json")
            self.assertEqual("ready", graph["state"])
            self.assertEqual("verified", graph["answer_projection"]["status"])
            self.assertEqual(60, len(graph["edges"]))
            self.assertTrue(all(edge["status"] == "verified" for edge in graph["edges"]))
            audited_classes = {
                document_class
                for node in graph["nodes"]
                for document_class in node.get("source", {})
                .get("locator", {})
                .get("audited_document_classes", [])
            }
            self.assertEqual({"PP", "\u5951\u7d04\u66f8", "PLAN", "FR"}, audited_classes)
            scan_counts = {
                tuple(sorted(node.get("source", {}).get("locator", {}).get("secondary_scan_counts", {}).items()))
                for node in graph["nodes"]
                if node["node_type"] == "project_participation"
            }
            self.assertIn((("FR", 0), ("PLAN", 0)), scan_counts)
            self.assertEqual(first, decide_question(engine, QUESTION_COUNT))

    def test_role_roster_requires_six_unique_role_name_bindings(self):
        text = " ".join(
            (
                "エグゼクティブスポンサー：山田 直樹",
                "プロジェクトマネージャー：佐藤 健一",
                "リードデータサイエンティスト：鈴木 美咲",
                "データエンジニア：斎藤 悠斗",
                "ビジネスアナリスト：井上 里奈",
                "QAレビューアー：池田 恒一",
                "クライアント窓口：池田 直哉",
            )
        )
        roster = _extract_role_roster_text(text)
        self.assertIsNotNone(roster)
        self.assertEqual(6, len(roster))
        self.assertEqual(
            {"山田 直樹", "佐藤 健一", "鈴木 美咲", "斎藤 悠斗", "井上 里奈", "池田 恒一"},
            {value["person"] for value in roster.values()},
        )
        self.assertIsNone(_extract_role_roster_text(text + " QAレビュー担当：池田 直哉"))

    def test_secondary_role_audit_accepts_both_directions_and_abbreviations(self):
        roster_text = " ".join(
            (
                "エグゼクティブスポンサー：山田 直樹",
                "プロジェクトマネージャー：佐藤 健一",
                "リードデータサイエンティスト：鈴木 美咲",
                "データエンジニア：斎藤 悠斗",
                "ビジネスアナリスト：井上 里奈",
                "QAレビューアー：池田 恒一",
            )
        )
        roster = _extract_role_roster_text(roster_text)
        self.assertIsNotNone(roster)
        role_first = _audit_secondary_role_mentions_text(
            "実施体制\nPM\n佐藤 健一\nリードDS\n鈴木 美咲\nBA\n井上 里奈\nQA\n池田 恒一",
            roster,
        )
        person_first = _audit_secondary_role_mentions_text(
            "実施体制\n佐藤 健一\nPM\n鈴木 美咲\nリードDS\n井上 里奈\nBA\n池田 恒一\nQA",
            roster,
        )
        expected = {"佐藤健一", "鈴木美咲", "井上里奈", "池田恒一"}
        self.assertEqual(expected, set(role_first))
        self.assertEqual(expected, set(person_first))

    def test_secondary_role_audit_controls_saito_variant_and_rejects_outsider(self):
        roster_text = " ".join(
            (
                "エグゼクティブスポンサー：中村 誠",
                "プロジェクトマネージャー：加藤 大輔",
                "リードデータサイエンティスト：渡辺 遥",
                "データエンジニア：斎藤 悠斗",
                "ビジネスアナリスト：井上 里奈",
                "QAレビューアー：清水 麻衣",
            )
        )
        roster = _extract_role_roster_text(roster_text)
        self.assertIsNotNone(roster)
        audited = _audit_secondary_role_mentions_text(
            "役割 氏名\nデータエンジニア\n斉藤 悠斗",
            roster,
        )
        self.assertEqual({"斎藤悠斗"}, set(audited))
        with self.assertRaises(ValueError):
            _audit_secondary_role_mentions_text("実施体制\nPM\n未知 太郎", roster)

    def test_secondary_role_audit_never_hides_late_mixed_direction_or_wrong_role(self):
        roster_text = " ".join(
            (
                "エグゼクティブスポンサー：山田 直樹",
                "プロジェクトマネージャー：佐藤 健一",
                "リードデータサイエンティスト：鈴木 美咲",
                "データエンジニア：斎藤 悠斗",
                "ビジネスアナリスト：井上 里奈",
                "QAレビューアー：池田 恒一",
            )
        )
        roster = _extract_role_roster_text(roster_text)
        self.assertIsNotNone(roster)
        with self.assertRaises(ValueError):
            _audit_secondary_role_mentions_text(
                "PM\n佐藤 健一\n未知 太郎\nPM", roster
            )
        with self.assertRaises(ValueError):
            _audit_secondary_role_mentions_text(
                "PM\n佐藤 健一\n佐藤 健一 QA", roster
            )
        with self.assertRaises(ValueError):
            _audit_secondary_role_mentions_text(
                "実施体制\nPM\n佐藤 健一\n"
                + ("無関係\n" * 1300)
                + "PM 未知 太郎",
                roster,
            )

    def test_secondary_role_audit_does_not_treat_natural_language_as_a_person(self):
        roster_text = " ".join(
            (
                "エグゼクティブスポンサー：山田 直樹",
                "プロジェクトマネージャー：佐藤 健一",
                "リードデータサイエンティスト：鈴木 美咲",
                "データエンジニア：斎藤 悠斗",
                "ビジネスアナリスト：井上 里奈",
                "QAレビューアー：池田 恒一",
            )
        )
        roster = _extract_role_roster_text(roster_text)
        self.assertIsNotNone(roster)
        for text in ("DE基盤 構築", "DE 基盤 構築", "DE：基盤 構築"):
            with self.subTest(text=text):
                self.assertEqual({}, _audit_secondary_role_mentions_text(text, roster))

    def test_secondary_role_audit_holds_conflicting_candidates_on_both_sides(self):
        roster_text = " ".join(
            (
                "エグゼクティブスポンサー：山田 直樹",
                "プロジェクトマネージャー：佐藤 健一",
                "リードデータサイエンティスト：鈴木 美咲",
                "データエンジニア：斎藤 悠斗",
                "ビジネスアナリスト：井上 里奈",
                "QAレビューアー：池田 恒一",
            )
        )
        roster = _extract_role_roster_text(roster_text)
        self.assertIsNotNone(roster)
        for unit in (
            ("佐藤 健一", "PM", "未知 太郎"),
            ("池田 恒一", "PM", "佐藤 健一"),
            ("佐藤 健一 / PM / 未知 太郎",),
            ("未知 太郎", "PM", "佐藤 健一", "BA", "井上 里奈"),
            ("池田 恒一", "PM", "佐藤 健一", "BA", "井上 里奈"),
        ):
            with self.subTest(unit=unit), self.assertRaises(ValueError):
                _audit_secondary_role_mentions_units((unit,), roster)

    def test_live_path_requires_graph_plan(self):
        decision = self.engine.decide("13", QUESTION)
        self.assertEqual(("hold", "extended_graph_plan_required"), (decision.status, decision.reason))
        decision = self.engine.decide("86", QUESTION_COUNT)
        self.assertEqual(("hold", "extended_graph_plan_required"), (decision.status, decision.reason))


if __name__ == "__main__":
    unittest.main()
