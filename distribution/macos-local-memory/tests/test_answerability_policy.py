#!/usr/bin/env python3
"""Meaningful answerability boundaries using synthetic, non-personal evidence."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import answerability_policy as policy
import claim_graph_validator as validator


def packet(evidence_id, text, path="資料/案内.pdf", page=2):
    return {"evidence_id": evidence_id, "text": text, "path": path, "locator": {"page_number": page}}


def record(fields, query="資料から各項目を教えてください。"):
    items, rows, lines = [], [], []
    for field_id, label, value, ids in fields:
        item = {"item_id": field_id, "label": label, "required_claim": label, "required": True}
        items.append(item)
        audit = {"item_id": field_id, "verdict": "supported" if value else "insufficient", "supported_value": value,
                 "supporting_packet_ids": ids if value else [], "competing_packet_ids": [], "reason_code": "none" if value else "missing_evidence",
                 "defect": "" if value else "直接支持する記載がありません。", "missing_information": [] if value else [label]}
        rows.append({"item": item, "retrieved_evidence_ids": ids, "audit": audit})
        if value:
            lines.append(f"- {label}: {value}")
    return {"query": query, "question_plan": {"items": items, "partial_answer_allowed": True}, "field_runs": rows,
            "answer": {"answer": "\n".join(lines) or "わかりません", "answer_mode": "grounded" if len(lines) == len(fields) else "qualified",
                       "answer_status": "answered" if lines else "insufficient", "evidence_ids": [eid for _, _, value, ids in fields if value for eid in ids]}}


class AnswerabilityPolicyTests(unittest.TestCase):
    def validate(self, result, packets):
        _, graph, report = validator.build_and_validate(result, packets)
        self.assertEqual(report["status"], "pass", report)
        return graph

    def test_metadata_path_can_be_sourced_from_provisional_packet(self):
        packets = [packet("E1", "[暫定読取] 古い神社", "資料/Guide.pdf")]
        original = record([("F1", "資料の名称と場所", "資料/guide.pdf", ["E1"])])
        before = deepcopy(original)
        result = policy.prepare_record(original, packets)
        self.assertEqual(original, before)
        self.assertEqual(result["field_runs"][0]["audit"]["supported_value"], "資料/Guide.pdf")
        graph = self.validate(result, packets)
        self.assertEqual(graph["claims"][0]["claim_kind"], "source_metadata")
        self.assertNotIn("古い神社", result["answer"]["answer"])

    def test_metadata_must_not_treat_body_title_as_existing_file(self):
        packets = [packet("E1", "[[案内資料_導入]]", "資料/索引.md")]
        original = record([("F1", "導入資料のファイル名", "案内資料_導入", ["E1"]),
                           ("F2", "導入資料の記載場所", "索引.md", ["E1"])])
        result = policy.prepare_record(original, packets)
        self.assertEqual(result["answerability_policy"]["confirmed_field_ids"], ["F2"])
        self.assertIn("参照先ファイルの実在は未確認", result["answer"]["answer"])
        self.assertNotIn("案内資料_導入.md", result["answer"]["answer"])
        self.validate(result, packets)

    def test_ocr_quote_keeps_question_marks_and_is_not_a_confirmed_year(self):
        literal = "[暫定読取] 870年頃？（平安時代）小さな神社創建"
        packets = [packet("E1", literal)]
        result = policy.prepare_record(record([("F1", "神社の創建年", "870年頃？（平安時代）", ["E1"])]), packets)
        self.assertTrue(result["answerability_policy"]["reference_only"])
        self.assertEqual(result["answer"]["answer_mode"], "qualified")
        self.assertEqual(result["answer"]["answer_status"], "insufficient")
        self.assertEqual(result["answer"]["evidence_ids"], [])
        self.assertEqual(result["answerability_policy"]["observations"][0]["quote"], literal)
        graph = self.validate(result, packets)
        self.assertEqual(graph["claims"], [])

    def test_name_only_does_not_prove_itinerary_membership(self):
        packets = [packet("E1", "小さな神社"), packet("E2", "[暫定読取] 870年頃？創建")]
        result = policy.prepare_record(record([("F1", "行程に小さな神社が含まれているか", "小さな神社", ["E1"]),
                                              ("F2", "創建年", "870年頃？", ["E2"])]), packets)
        self.assertEqual(result["answerability_policy"]["confirmed_field_ids"], [])
        self.assertIn("包含を確認できません", result["answer"]["answer"])
        self.validate(result, packets)

    def test_independent_confirmed_field_survives_ocr_and_missing_field(self):
        packets = [packet("E1", "案内所の開館時間は9時。"), packet("E2", "[暫定読取] 開設は1990年頃？")]
        result = policy.prepare_record(record([("F1", "開館時間", "9時", ["E1"]),
                                              ("F2", "開設年", "1990年頃？", ["E2"]),
                                              ("F3", "住所", "", ["E1"])]), packets)
        self.assertEqual(result["answerability_policy"]["confirmed_field_ids"], ["F1"])
        self.assertEqual(result["answer"]["answer_status"], "answered")
        self.assertEqual(result["answer"]["answer_mode"], "qualified")
        self.validate(result, packets)

    def test_missing_dependency_prevents_dependent_conclusion(self):
        packets = [packet("E1", "[暫定読取] 第一段階は承認済み"), packet("E2", "実行可能")]
        original = record([("F1", "承認状態", "承認済み", ["E1"]), ("F2", "実行状態", "実行可能", ["E2"])])
        original["question_plan"]["items"][1]["depends_on"] = ["F1"]
        result = policy.prepare_record(original, packets)
        self.assertEqual(result["answerability_policy"]["confirmed_field_ids"], [])
        self.assertNotIn("確認済み — 実行状態", result["answer"]["answer"])
        self.validate(result, packets)

    def test_whole_results_and_structured_operations_are_unchanged(self):
        packets = [packet("E1", "[暫定読取] 3件")]
        for query in ("すべての資料を教えて", "両者を比較して", "手順の順番を教えて"):
            original = record([("F1", "結果", "3件", ["E1"])], query=query)
            self.assertEqual(policy.prepare_record(original, packets), original)
        for operation in ("record_lookup", "aggregate_count", "ordered_section_lookup"):
            original = record([("F1", "結果", "3件", ["E1"])])
            original["question_evidence_graph"] = {"intent": {"operation": operation}}
            self.assertEqual(policy.prepare_record(original, packets), original)
        original = record([("F1", "結果", "3件", ["E1"])])
        original["question_plan"]["partial_answer_allowed"] = False
        self.assertEqual(policy.prepare_record(original, packets), original)

    def test_unknown_evidence_and_fabricated_metadata_are_not_rescued(self):
        original = record([("F1", "資料の名称と場所", "資料/架空.pdf", ["UNKNOWN"])])
        self.assertEqual(policy.prepare_record(original, []), original)
        packets = [packet("E1", "普通の本文")]
        fabricated = record([("F1", "資料の名称と場所", "資料/架空.pdf", ["E1"])])
        result = policy.prepare_record(fabricated, packets)
        self.assertEqual(result["answerability_policy"]["metadata_claims"], [])
        self.assertNotIn("架空.pdf", result["answer"]["answer"])

    def test_tampered_quotation_source_or_id_is_blocked_even_after_render(self):
        packets = [packet("E1", "[暫定読取] 870年頃？")]
        original = policy.prepare_record(record([("F1", "創建年", "870年頃？", ["E1"])]), packets)
        for key, value in (("quote", "870年"), ("path", "架空.pdf"), ("locator", {"page_number": 9}), ("evidence_id", "UNKNOWN")):
            tampered = deepcopy(original)
            tampered["answerability_policy"]["observations"][0][key] = value
            tampered["answer"]["answer"] = policy.render_answer(tampered)
            _, _, report = validator.build_and_validate(tampered, packets)
            self.assertEqual(report["status"], "blocked", key)
        tampered = deepcopy(original)
        tampered["answer"]["answer"] += "\n870年に創建されました。"
        _, _, report = validator.build_and_validate(tampered, packets)
        self.assertEqual(report["status"], "blocked")

    def test_metadata_cannot_be_used_for_an_unrelated_factual_claim(self):
        packets = [packet("E1", "本文", "資料/870年.pdf")]
        original = record([("F1", "創建年", "870年", ["E1"])])
        result = policy.prepare_record(original, packets)
        graph = self.validate(result, packets)
        self.assertEqual(graph["claims"], [])
        self.assertNotIn("確認済み — 創建年", result["answer"]["answer"])

    def test_reference_only_flag_or_completion_cannot_be_forged(self):
        packets = [packet("E1", "[暫定読取] 870年頃？")]
        original = policy.prepare_record(record([("F1", "創建年", "870年頃？", ["E1"])]), packets)
        for mutation in ("flag", "status"):
            tampered = deepcopy(original)
            if mutation == "flag":
                tampered["answerability_policy"]["reference_only"] = False
            else:
                tampered["answer"]["answer_status"] = "answered"
            self.assertTrue(policy.validate_projection(tampered, packets))

    def test_disabled_policy_cannot_sneak_in_metadata_claim(self):
        packets = [packet("E1", "[暫定読取] 神社", "資料/案内.pdf")]
        original = policy.prepare_record(record([("F1", "資料の場所", "資料/案内.pdf", ["E1"])]), packets)
        original["answerability_policy"]["applied"] = False
        _, _, report = validator.build_and_validate(original, packets)
        self.assertEqual(report["status"], "blocked")

    def test_provisional_id_cannot_be_laundered_into_confirmed_citations(self):
        packets = [packet("E1", "開館9時"), packet("E2", "[暫定読取] 開設1990年頃？")]
        result = policy.prepare_record(record([("F1", "開館時間", "9時", ["E1"]),
                                              ("F2", "開設年", "1990年頃？", ["E2"])]), packets)
        result["answer"]["evidence_ids"].append("E2")
        self.assertTrue(policy.validate_projection(result, packets))

    def test_related_sentence_cannot_be_relabelled_as_document_title(self):
        packets = [packet("E1", "日本橋にある施設")]
        result = policy.prepare_record(record([("F1", "施設の住所", "", ["E1"])]), packets)
        result["answerability_policy"]["observations"][0]["kind"] = "document_title"
        result["answer"]["answer"] = policy.render_answer(result)
        self.assertTrue(policy.validate_projection(result, packets))

    def test_original_contract_and_artifact_integrity_remain_enforced(self):
        packets = [packet("E1", "[暫定読取] 870年頃？")]
        original = record([("F1", "創建年", "870年頃？", ["E1"])])
        old_contract, _, _ = validator.build_and_validate(original, packets)
        result = policy.prepare_record(original, packets)
        contract, graph, _ = validator.build_and_validate(result, packets)
        self.assertEqual(old_contract, contract)
        graph["artifact_hash"] = "f" * 64
        report = validator.validate_claim_graph(result, packets, contract, graph)
        self.assertIn("artifact_hash_mismatch", {entry["code"] for entry in report["failures"]})

    def test_exclusion_preserves_independent_field_and_drops_failed_claim(self):
        packets = [packet("E1", "開館9時"), packet("E2", "所在は京都")]
        original = record([("F1", "開館時間", "9時", ["E1"]), ("F2", "所在地", "京都", ["E2"])])
        result = policy.prepare_record(original, packets, excluded_field_ids=("F2",))
        self.assertEqual(result["answerability_policy"]["confirmed_field_ids"], ["F1"])
        self.assertNotIn("京都", result["answer"]["answer"])
        self.validate(result, packets)

    def test_existing_success_stays_identical(self):
        packets = [packet("E1", "開館は9時")]
        original = record([("F1", "開館時間", "9時", ["E1"])])
        self.assertEqual(policy.prepare_record(original, packets), original)

    def test_composite_source_answer_uses_canonical_provenance(self):
        packets = [packet("E1", "[暫定読取] 神社", "資料/Guide.pdf", 3),
                   packet("E2", "神社の案内", "資料/Guide.pdf", 3)]
        original = record([("F1", "神社の情報が記載されている資料の名称と場所",
                            "資料/guide.pdf (page_number: 999)", ["E1", "E2"])])
        result = policy.prepare_record(original, packets)
        self.assertEqual(result["field_runs"][0]["audit"]["supported_value"], "資料/Guide.pdf")
        self.assertEqual(result["answerability_policy"]["metadata_claims"][0]["evidence_id"], "E2")
        self.assertEqual(result["answerability_policy"]["metadata_claims"][0]["locator"], {"page_number": 3})
        self.assertIn("確認済み（記載の出典）", result["answer"]["answer"])
        self.assertNotIn("999", result["answer"]["answer"])
        self.validate(result, packets)

    def test_multiple_sources_are_not_collapsed_into_one_arbitrary_file(self):
        packets = [packet("E1", "案内", "資料/A.pdf"), packet("E2", "別の案内", "資料/B.pdf")]
        result = policy.prepare_record(record([("F1", "案内が記載されている資料の名称と場所",
                                               "複数資料に記載", ["E1", "E2"])]), packets)
        self.assertEqual(result["answerability_policy"]["metadata_claims"], [])
        self.assertEqual(result["answerability_policy"]["confirmed_field_ids"], [])

    def test_target_filename_cannot_be_replaced_with_index_filename(self):
        packets = [packet("E1", "[[導入資料]]", "資料/索引.md")]
        result = policy.prepare_record(record([("F1", "導入資料のファイル名", "導入資料 (段落 3)", ["E1"])]), packets)
        self.assertEqual(result["answerability_policy"]["metadata_claims"], [])
        self.assertEqual(result["answerability_policy"]["confirmed_field_ids"], [])

    def test_same_page_ocr_duplicates_keep_one_unmodified_quote(self):
        packets = [packet("E1", "[暫定読取] 【資料】 870年頃? (平安時代) 神社創建", page=3),
                   packet("E2", "[暫定読取] 870年頃？（平安時代）神社創建", page=3)]
        result = policy.prepare_record(record([("F1", "神社の創建年", "870年頃？", ["E1", "E2"])]), packets)
        observations = result["answerability_policy"]["observations"]
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["quote"], packets[1]["text"])
        self.assertEqual(result["answer"]["diagnostic_evidence_ids"], ["E2"])
        self.validate(result, packets)

    def test_ocr_dedup_does_not_remove_uncertainty_or_conflicting_values(self):
        for second_text, second_page in (("[暫定読取] 870年頃", 3),
                                         ("[暫定読取] 860年頃？", 3),
                                         ("[暫定読取] 870年頃？", 4)):
            packets = [packet("E1", "[暫定読取] 870年頃？", page=3), packet("E2", second_text, page=second_page)]
            result = policy.prepare_record(record([("F1", "創建年", "870年頃？", ["E1", "E2"])]), packets)
            self.assertEqual(len(result["answerability_policy"]["observations"]), 2)
            self.validate(result, packets)

    def test_identical_excerpt_for_two_fields_is_displayed_once_with_both_labels(self):
        packets = [packet("E1", "地域の案内資料")]
        result = policy.prepare_record(record([("F1", "住所", "", ["E1"]), ("F2", "電話番号", "", ["E1"])]), packets)
        self.assertEqual(result["answer"]["answer"].count("「地域の案内資料」"), 1)
        self.assertIn("住所、電話番号", result["answer"]["answer"])
        self.validate(result, packets)

    def test_metadata_binding_cannot_switch_to_another_valid_packet(self):
        packets = [packet("E1", "[暫定読取] 神社", "資料/案内.pdf", 3),
                   packet("E2", "神社の案内", "資料/案内.pdf", 9)]
        result = policy.prepare_record(record([("F1", "情報が記載されている資料の名称と場所", "資料/案内.pdf (p3)", ["E1", "E2"])]), packets)
        binding = result["answerability_policy"]["metadata_claims"][0]
        binding["evidence_id"] = "E1"
        binding["locator"] = {"page_number": 3}
        result["field_runs"][0]["audit"]["supporting_packet_ids"] = ["E1"]
        result["answer"]["evidence_ids"] = ["E1"]
        self.assertTrue(policy.validate_projection(result, packets))


if __name__ == "__main__":
    unittest.main()
