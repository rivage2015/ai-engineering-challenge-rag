"""Synthetic candidate-relation tests; no model, network or private documents."""

import copy
import importlib.util
import json
from pathlib import Path
import unittest


MODULE = Path(__file__).resolve().parents[1] / "distribution/macos-local-memory/engine/workflow_relation_graph.py"
SPEC = importlib.util.spec_from_file_location("workflow_relation_graph_test", MODULE)
graph = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(graph)


def record(text, evidence_id, *, doc="d1", path="demo.xlsx", sheet="窓口", row=1):
    return {"text": json.dumps(text, ensure_ascii=False), "evidence_id": evidence_id,
            "document_id": doc, "relative_path": path,
            "locator": {"sheet_name": sheet, "row_index": row}}


def fixture():
    sources = {
        "E1": record("担当者", "actor"),
        "E2": record("予約がある場合", "condition"),
        "E3": record("番号を確認する", "action"),
        "E4": record("担当者は予約がある場合、番号を確認する。", "row"),
        "E5": record("その後、席へ案内する。", "later", row=2),
    }
    payload = {"nodes": [
        {"id": "N1", "kind": "actor", "packet_id": "E1", "quote": "担当者"},
        {"id": "N2", "kind": "condition", "packet_id": "E2", "quote": "予約がある場合"},
        {"id": "N3", "kind": "action", "packet_id": "E3", "quote": "番号を確認する"},
    ], "edges": [
        {"id": "R1", "source": "N1", "target": "N3", "kind": "performs",
         "support_packet_id": "E4", "support_quote": "担当者は予約がある場合、番号を確認する。"},
        {"id": "R2", "source": "N2", "target": "N3", "kind": "when",
         "support_packet_id": "E4", "support_quote": "担当者は予約がある場合、番号を確認する。"},
    ], "unresolved_packet_ids": []}
    return payload, sources


class QuestionGraphTests(unittest.TestCase):
    def test_question_graph_contains_unknown_requested_relations_not_answers(self):
        query = "窓口の声がけと担当、条件分岐を含む一連の手順と注意を教えて"
        result = graph.build_question_graph(query, {"items": [{"item_id": "F1", "required_claim": "一連の手順"}]})
        self.assertEqual(result["status"], "ready")
        self.assertEqual({r["kind"] for r in result["requirements"]},
                         {"actions", "speech", "conditions", "actors", "order", "cautions"})
        self.assertEqual({e["kind"] for e in result["edges"]} - {"asks_for"},
                         {"says", "when", "performs", "next", "warns"})
        for node in result["nodes"]:
            self.assertIn(node["provenance"]["quote"], query)
            if node["kind"] != "request":
                self.assertEqual(node["answer_status"], "unknown")
            self.assertNotIn("supported_value", node)

    def test_plan_does_not_invent_user_requirements(self):
        result = graph.build_question_graph("営業時間は？", {"items": [{"item_id": "F1", "required_claim": "担当と条件分岐"}]})
        self.assertEqual(result["requirements"], [])
        self.assertEqual(result["status"], "not_applicable")

    def test_question_and_plan_are_not_mutated(self):
        plan = {"items": [{"item_id": "F1", "required_claim": "対応手順"}]}
        before = copy.deepcopy(plan)
        result = graph.build_question_graph("対応手順を教えて", plan)
        self.assertEqual(plan, before)
        self.assertEqual(result["requirements"][0]["item_ids"], ["F1"])

    def test_empty_question_has_no_graph(self):
        self.assertEqual(graph.build_question_graph("", {})["nodes"], [])


class SourceGraphTests(unittest.TestCase):
    def test_native_cells_can_bind_to_same_row_support(self):
        payload, sources = fixture()
        result = graph.normalize_source_graph(payload, sources)
        self.assertEqual(len(result["nodes"]), 3)
        self.assertEqual(len(result["edges"]), 2)
        self.assertEqual(result["edges"][0]["evidence_id"], "row")
        self.assertEqual(result["semantic_validation"], "not_performed")
        self.assertEqual(result["unresolved_packet_ids"], ["E5"])
        self.assertEqual(result["status"], "partial")

    def test_fabricated_node_quote_is_rejected_with_incident_edges(self):
        payload, sources = fixture()
        payload["nodes"][0]["quote"] = "責任者"
        result = graph.normalize_source_graph(payload, sources)
        self.assertEqual([n["id"] for n in result["nodes"]], ["N2", "N3"])
        self.assertEqual([e["id"] for e in result["edges"]], ["R2"])
        self.assertIn("E1", result["unresolved_packet_ids"])

    def test_unknown_packet_and_endpoint_are_rejected(self):
        payload, sources = fixture()
        payload["nodes"][0]["packet_id"] = "E999"
        payload["edges"][1]["target"] = "N999"
        result = graph.normalize_source_graph(payload, sources)
        self.assertEqual(result["edges"], [])
        self.assertTrue(any(i["code"] == "invalid_node_kind_or_packet" for i in result["issues"]))

    def test_support_quote_must_include_both_endpoints_and_be_exact(self):
        for quote in ("担当者", "担当者は番号を確認する。", "担当者は予約がある場合、番号を確認する。追加"):
            with self.subTest(quote=quote):
                payload, sources = fixture()
                payload["edges"][0]["support_quote"] = quote
                result = graph.normalize_source_graph(payload, sources)
                self.assertNotIn("R1", [e["id"] for e in result["edges"]])

    def test_cross_document_sheet_or_version_path_is_rejected(self):
        for key, value in (("document_id", "d2"), ("relative_path", "2025.xlsx"), ("sheet_name", "別の窓口")):
            with self.subTest(key=key):
                payload, sources = fixture()
                if key == "sheet_name":
                    sources["E4"]["locator"][key] = value
                else:
                    sources["E4"][key] = value
                result = graph.normalize_source_graph(payload, sources)
                self.assertEqual(result["edges"], [])
                self.assertEqual(len(result["nodes"]), 3)

    def test_reversed_actor_action_kinds_are_rejected(self):
        payload, sources = fixture()
        payload["edges"][0].update(source="N3", target="N1")
        result = graph.normalize_source_graph(payload, sources)
        self.assertNotIn("R1", [e["id"] for e in result["edges"]])
        self.assertTrue(any(i["code"] == "edge_endpoint_kind_mismatch" for i in result["issues"]))

    def test_adjacent_rows_do_not_create_next_edges(self):
        payload, sources = fixture()
        payload["nodes"].append({"id": "N4", "kind": "action", "packet_id": "E5", "quote": "席へ案内する"})
        payload["edges"] = []
        result = graph.normalize_source_graph(payload, sources)
        self.assertEqual(result["edges"], [])
        self.assertEqual(len(result["nodes"]), 4)
        matches = graph.build_relation_matches(graph.build_question_graph("一連の手順", {}), result)
        self.assertIn("Q_order", matches["unmatched_requirement_ids"])

    def test_duplicate_ids_reject_all_conflicting_definitions(self):
        payload, sources = fixture()
        payload["nodes"].append(copy.deepcopy(payload["nodes"][0]))
        result = graph.normalize_source_graph(payload, sources)
        self.assertNotIn("N1", [n["id"] for n in result["nodes"]])
        self.assertNotIn("R1", [e["id"] for e in result["edges"]])

    def test_global_node_edge_id_collision_is_rejected(self):
        payload, sources = fixture()
        payload["edges"][0]["id"] = "N2"
        result = graph.normalize_source_graph(payload, sources)
        self.assertNotIn("N2", [n["id"] for n in result["nodes"]])
        self.assertEqual(result["edges"], [])

    def test_budget_is_fail_closed_not_silent_truncation(self):
        for key, count in (("nodes", 49), ("edges", 65)):
            with self.subTest(key=key):
                payload, sources = fixture()
                payload[key] = [copy.deepcopy(payload[key][0]) for _ in range(count)]
                result = graph.normalize_source_graph(payload, sources)
                self.assertEqual(result["status"], "invalid")
                self.assertEqual(result["nodes"], [])
                self.assertEqual(result["edges"], [])
                self.assertEqual(result["unresolved_packet_ids"], list(sources))

    def test_empty_graph_and_unrepresented_packets_remain_unknown(self):
        _, sources = fixture()
        result = graph.normalize_source_graph({"nodes": [], "edges": [], "unresolved_packet_ids": []}, sources)
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["unresolved_packet_ids"], list(sources))

    def test_invalid_envelope_and_explicit_unresolved(self):
        payload, sources = fixture()
        self.assertEqual(graph.normalize_source_graph({}, sources)["status"], "invalid")
        payload["unresolved_packet_ids"] = ["E1", "E999", []]
        result = graph.normalize_source_graph(payload, sources)
        self.assertIn("E1", result["unresolved_packet_ids"])
        self.assertNotIn("E999", result["unresolved_packet_ids"])
        self.assertTrue(any(i["code"] == "unknown_unresolved_packet" for i in result["issues"]))

    def test_input_payload_and_sources_are_not_mutated_or_aliased(self):
        payload, sources = fixture()
        before = copy.deepcopy((payload, sources))
        result = graph.normalize_source_graph(payload, sources)
        self.assertEqual((payload, sources), before)
        result["nodes"][0]["provenance"]["locator"]["row_index"] = 99
        self.assertEqual(sources["E1"]["locator"]["row_index"], 1)

    def test_plain_and_json_literal_text_have_equal_quotes(self):
        payload, sources = fixture()
        for source in sources.values():
            source["text"] = json.loads(source["text"])
        result = graph.normalize_source_graph(payload, sources)
        self.assertEqual(len(result["edges"]), 2)

    def test_invalid_source_metadata_is_not_treated_as_common_scope(self):
        payload, sources = fixture()
        for source in sources.values():
            del source["document_id"]
        result = graph.normalize_source_graph(payload, sources)
        self.assertEqual(result["nodes"], [])
        self.assertEqual(result["edges"], [])

    def test_grounded_quotes_do_not_certify_semantics(self):
        payload, sources = fixture()
        # Co-occurrence alone could be a negative statement; quotation checks
        # cannot certify the proposed positive relation or business meaning.
        sources["E4"]["text"] = "担当者は番号を確認する役ではない。"
        payload["edges"] = [payload["edges"][0]]
        payload["edges"][0]["support_quote"] = sources["E4"]["text"]
        result = graph.normalize_source_graph(payload, sources)
        self.assertEqual(result["edges"][0]["status"], "grounded_candidate")
        self.assertEqual(result["semantic_validation"], "not_performed")

    def test_candidate_matching_and_rendering_are_not_verified(self):
        payload, sources = fixture()
        question = graph.build_question_graph("窓口担当と条件ごとの手順、声がけを教えて", {})
        source = graph.normalize_source_graph(payload, sources)
        matches = graph.build_relation_matches(question, source)
        self.assertFalse(matches["verified"])
        self.assertIn("Q_speech", matches["unmatched_requirement_ids"])
        self.assertIn("Q_order", matches["unmatched_requirement_ids"])
        self.assertNotIn("Q_actors", matches["unmatched_requirement_ids"])
        rendered = json.loads(graph.render_graph_context(question, source, matches))
        self.assertEqual([e["id"] for e in rendered["source_graph"]["edges"]],
                         [e["id"] for e in source["edges"]])
        self.assertEqual([n["id"] for n in rendered["source_graph"]["nodes"]],
                         [n["id"] for n in source["nodes"]])
        for edge in rendered["source_graph"]["edges"]:
            self.assertIn("support_quote", edge)
            self.assertIn("support_packet_id", edge)
            self.assertNotIn("provenance", edge)
        self.assertEqual(rendered["relation_matches"], matches)


if __name__ == "__main__":
    unittest.main()
