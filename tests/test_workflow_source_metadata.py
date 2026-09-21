"""Synthetic provenance regression: no business answers or production writes."""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP = Path(os.environ.get("LMS_PROVENANCE_APP_DIR", ROOT / "distribution/macos-local-memory/app"))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, APP / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy = load("workflow_source_policy", "answerability_policy.py")
validator = load("workflow_source_validator", "claim_graph_validator.py")
final_audit = load("workflow_source_final_audit", "final_answer_audit.py")
INDEX_FIELDS = ("evidence_sha256", "graph_sha256", "graph_security_partition_sha256",
                "graph_retrievable_evidence_set_sha256", "graph_embeddings_sha256")


def fixture():
    packets = [
        {"evidence_id": "E1", "path": "作業/発送手引2026.xlsx",
         "locator": {"sheet_name": "発送", "row_index": 3}, "text": "箱を点検する。"},
        {"evidence_id": "E2", "path": "作業/発送手引2026.xlsx",
         "locator": {"sheet_name": "発送", "row_index": 8}, "text": "記録して終了する。"},
    ]
    provenance = "、".join(Path(p["path"]).name + "（" + json.dumps(p["locator"], ensure_ascii=False) + "）" for p in packets)
    runs = []
    for field_id, label, value, ids in (
        ("F1", "発送の手順", "箱を点検する。\n記録して終了する。", ["E1", "E2"]),
        ("F2", "根拠資料の場所", provenance, ["E1", "E2"]),
    ):
        item = {"item_id": field_id, "label": label, "required_claim": label, "required": True}
        field_audit = {"item_id": field_id, "verdict": "supported", "supported_value": value,
                       "supporting_packet_ids": ids, "competing_packet_ids": [], "reason_code": "none",
                       "defect": "", "missing_information": []}
        runs.append({"item": item, "audit": field_audit, "retrieved_evidence_ids": ids})
    record = {
        "query": "発送の仕事の手順を教えてください。",
        "question_plan": {"items": [run["item"] for run in runs], "partial_answer_allowed": True},
        "question_evidence_graph": {"status": "unsupported", "intent": {"operation": "unknown"}},
        "graph_route": {"required": False, "used": False, "operation": "unknown"},
        "field_runs": runs,
        "answer": {"answer_status": "answered", "answer_mode": "grounded",
                   "answer": "\n".join(run["audit"]["supported_value"] for run in runs),
                   "evidence_ids": ["E1", "E2"], "diagnostic_evidence_ids": [], "basis_summary": "原文",
                   "uncertainties": [], "non_answer_reason": {"code": "none", "explanation": ""},
                   "needed_information": [], "follow_up_question": "", "reconsideration_condition": "", "verification_reminder": ""},
        "index": {field: "hash-" + field for field in INDEX_FIELDS},
    }
    return record, packets


def run_final(record, packets):
    metadata = {field: "hash-" + field for field in INDEX_FIELDS}
    graph_policy = {"eligible_evidence_ids": {p["evidence_id"] for p in packets}, "metadata": metadata,
                    "graph_sha256": metadata["graph_sha256"], "partition_sha256": metadata["graph_security_partition_sha256"],
                    "eligible_evidence_set_sha256": metadata["graph_retrievable_evidence_set_sha256"], "source_graph": {}}
    indexed = [{**p, "document_id": "D1", "relative_path": p["path"]} for p in packets]
    llm = mock.Mock(return_value=({"verdict": "verified", "reason": "原文・位置を確認", "unsupported_claims": []}, {"wall_seconds": 0.0}))
    with tempfile.TemporaryDirectory(prefix="workflow-provenance-test-") as temp:
        path = Path(temp) / "record.json"
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(sys, "argv", ["audit", "--record", str(path), "--index", str(Path(temp) / "unused.sqlite")]), \
             mock.patch.object(final_audit.answer_engine, "load_answer_evidence_records", return_value=(indexed, graph_policy)), \
             mock.patch.object(final_audit.question_graph, "validate_question_evidence_graph", return_value={"status": "not_applicable", "failures": []}), \
             mock.patch.object(final_audit, "audit", llm), redirect_stdout(io.StringIO()) as output:
            final_audit.main()
        return json.loads(output.getvalue()), llm


class WorkflowSourceMetadataTests(unittest.TestCase):
    def test_failure_is_reproduced_without_binding(self):
        record, packets = fixture()
        self.assertFalse(policy._eligible(record))
        self.assertEqual(policy.prepare_record(record, packets), record)
        _, graph, result = validator.build_and_validate(record, packets)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any(f["code"] == "value_not_in_evidence" and f["claim_id"] == "C2" for f in result["failures"]))

    def test_workflow_binding_retains_all_locations_without_changing_answer(self):
        record, packets = fixture()
        bound = policy.bind_source_metadata(record, packets)
        self.assertEqual(bound["answer"], record["answer"])
        self.assertEqual(bound["field_runs"], record["field_runs"])
        self.assertNotIn("answerability_policy", bound)
        self.assertEqual(policy.validate_source_metadata(bound, packets), [])
        binding = bound["source_metadata_policy"]["metadata_claims"][0]
        self.assertEqual(binding["evidence_ids"], ["E1", "E2"])
        self.assertEqual([s["locator"]["row_index"] for s in binding["sources"]], [3, 8])
        self.assertEqual({s["path"] for s in binding["sources"]}, {packets[0]["path"]})
        _, graph, result = validator.build_and_validate(bound, packets)
        self.assertEqual(result["status"], "pass", result)
        self.assertEqual(graph["claims"][1]["claim_kind"], "source_metadata")
        self.assertEqual(len(graph["claims"][1]["value_parts"]), 1)

    def test_forged_locations_names_and_added_content_are_not_repaired(self):
        record, packets = fixture()
        original = record["field_runs"][1]["audit"]["supported_value"]
        for changed in (
            original.replace('"row_index": 3', '"row_index": 999'),
            original.replace("発送手引2026.xlsx", "別資料2026.xlsx", 1),
            original + "注意事項はありません。",
            original.replace('"row_index": 3', '"row_index": true'),
            original.replace('"row_index": 3', '"row_index": 999, "row_index": 3'),
        ):
            with self.subTest(value=changed):
                modified = copy.deepcopy(record)
                modified["field_runs"][1]["audit"]["supported_value"] = changed
                modified["answer"]["answer"] = modified["answer"]["answer"].replace(original, changed)
                bound = policy.bind_source_metadata(modified, packets)
                self.assertNotIn("source_metadata_policy", bound)
                self.assertEqual(validator.build_and_validate(bound, packets)[2]["status"], "blocked")

    def test_unknown_ids_are_not_corrected(self):
        record, packets = fixture()
        record["field_runs"][1]["audit"]["supporting_packet_ids"].append("UNKNOWN")
        self.assertEqual(policy.bind_source_metadata(record, packets), record)
        self.assertEqual(validator.build_and_validate(record, packets)[2]["status"], "blocked")

    def test_content_claims_still_require_literal_source(self):
        record, packets = fixture()
        record["field_runs"][0]["audit"]["supported_value"] = "箱の点検は不要です。"
        bound = policy.bind_source_metadata(record, packets)
        result = validator.build_and_validate(bound, packets)[2]
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any(f["code"] == "value_not_in_evidence" and f["claim_id"] == "C1" for f in result["failures"]))

    def test_tampering_structured_path_locator_or_ids_is_rejected(self):
        record, packets = fixture()
        bound = policy.bind_source_metadata(record, packets)
        for field, value in (("path", "別資料.xlsx"), ("locator", {"row_index": 999}), ("evidence_id", "UNKNOWN")):
            with self.subTest(field=field):
                changed = copy.deepcopy(bound)
                changed["source_metadata_policy"]["metadata_claims"][0]["sources"][0][field] = value
                result = validator.build_and_validate(changed, packets)[2]
                self.assertEqual(result["status"], "blocked")
                self.assertTrue(any(f["code"] == "source_metadata_projection_invalid" for f in result["failures"]))

    def test_multiple_source_paths_are_bound_to_their_own_locators(self):
        record, packets = fixture()
        packets[1]["path"] = "補足/返却手引.pdf"
        packets[1]["locator"] = {"page_number": 4}
        value = "、".join(p["path"] + json.dumps(p["locator"], ensure_ascii=False) for p in packets)
        record["field_runs"][1]["audit"]["supported_value"] = value
        record["answer"]["answer"] = record["field_runs"][0]["audit"]["supported_value"] + "\n" + value
        bound = policy.bind_source_metadata(record, packets)
        self.assertEqual(validator.build_and_validate(bound, packets)[2]["status"], "pass")
        record["field_runs"][1]["audit"]["supported_value"] = packets[0]["path"] + json.dumps(packets[1]["locator"])
        self.assertNotIn("source_metadata_policy", policy.bind_source_metadata(record, packets))

    def test_metadata_like_content_label_is_not_provenance(self):
        record, packets = fixture()
        record["question_plan"]["items"][1]["label"] = "発送の説明"
        self.assertNotIn("source_metadata_policy", policy.bind_source_metadata(record, packets))

    def test_final_gate_calls_semantic_auditor_without_partial_rescue(self):
        record, packets = fixture()
        result, llm = run_final(record, packets)
        self.assertEqual(result["deterministic_claim_validation"]["status"], "pass", result)
        self.assertEqual(result["orchestration_decision"]["status"], "accepted")
        self.assertEqual(result["answer"], record["answer"])
        self.assertNotIn("answerability_policy", result)
        self.assertEqual(llm.call_count, 1)
        self.assertTrue(llm.call_args.args[5]["source_metadata_policy"]["applied"])

    def test_final_gate_still_rejects_invented_content(self):
        record, packets = fixture()
        record["field_runs"][0]["audit"]["supported_value"] = "工程は省略できます。"
        result, llm = run_final(record, packets)
        self.assertEqual(result["orchestration_decision"]["status"], "rejected")
        llm.assert_not_called()

    def test_final_gate_still_rejects_unknown_source(self):
        record, packets = fixture()
        record["answer"]["evidence_ids"].append("UNKNOWN")
        result, llm = run_final(record, packets)
        self.assertEqual(result["orchestration_decision"]["status"], "rejected")
        llm.assert_not_called()

    def test_forged_source_fails_before_semantic_audit(self):
        record, packets = fixture()
        value = record["field_runs"][1]["audit"]["supported_value"]
        changed = value.replace('"row_index": 3', '"row_index": 333')
        record["field_runs"][1]["audit"]["supported_value"] = changed
        record["answer"]["answer"] = record["answer"]["answer"].replace(value, changed)
        result, llm = run_final(record, packets)
        self.assertEqual(result["orchestration_decision"]["status"], "rejected")
        llm.assert_not_called()

    def test_metadata_binding_does_not_turn_an_incomplete_answer_into_complete(self):
        record, packets = fixture()
        record["field_runs"][0]["audit"].update(verdict="insufficient", supported_value="", supporting_packet_ids=[])
        record["answer"].update(answer_mode="qualified", uncertainties=["終了作業が未確認"])
        bound = policy.bind_source_metadata(record, packets)
        self.assertEqual(bound["field_runs"], record["field_runs"])
        self.assertEqual(bound["answer"], record["answer"])
        self.assertFalse(policy._eligible(bound))

    def test_ambiguous_basename_does_not_choose_an_arbitrary_document(self):
        record, packets = fixture()
        packets[1]["path"] = "別室/発送手引2026.xlsx"
        self.assertNotIn("source_metadata_policy", policy.bind_source_metadata(record, packets))


class WorkflowQuoteContractTests(unittest.TestCase):
    def test_generic_business_quotes_and_separate_headings(self):
        for name in ("発送", "備品貸出", "検品"):
            text = f"{name}の担当者は、異常があれば責任者に連絡する。"
            entries = [{"heading": "条件・注意", "quote": text, "evidence_id": "x"}]
            rendered, ids = validator.validate_workflow_quotes(entries, {"x": text})
            self.assertEqual(ids, ["x"])
            self.assertEqual(rendered, "【整理区分：条件・注意】\n" + text)

    def test_forged_quote_wrong_id_provisional_and_heading_rejected(self):
        valid = {"heading": "準備", "quote": "箱を点検する。", "evidence_id": "x"}
        for changed, texts in [
            ({"quote": "点検しなくてよい。"}, {"x": "箱を点検する。"}),
            ({"evidence_id": "y"}, {"x": "箱を点検する。"}),
            ({}, {"x": "[暫定読取]箱を点検する。"}),
            ({"heading": "絶対安全"}, {"x": "箱を点検する。"}),
            ({}, {"x": "終了する。", "y": "箱を点検する。"}),
        ]:
            with self.subTest(changed=changed, texts=texts), self.assertRaises(ValueError):
                validator.validate_workflow_quotes([{**valid, **changed}], texts)

    def test_formal_rebinding_rejects_tamper_and_undelivered_source(self):
        record, packets = fixture()
        entries = [{"heading": "準備", "quote": packets[0]["text"], "evidence_id": "E1"}]
        value, ids = validator.validate_workflow_quotes(entries, {"E1": packets[0]["text"]})
        audit = record["field_runs"][0]["audit"]
        audit.update(workflow_quotes=entries, workflow_quote_input_ids=["E1"],
                     supported_value=value, supporting_packet_ids=ids)
        record["field_runs"][0]["model_context_attempts"] = [{"evidence_ids": ["E1"]}]
        record["field_runs"][0]["workflow_context_attempts"] = [{"delivery_status": "response_received",
                                                              "input_evidence_ids": ["E1"]}]
        self.assertFalse(validator.workflow_quote_bindings(record, packets)[1])
        audit["workflow_selection"] = [{"heading": "準備", "evidence_id": "E1"}]
        self.assertFalse(validator.workflow_quote_bindings(record, packets)[1])
        changed_source = copy.deepcopy(packets)
        changed_source[0]["text"] = "条件がある場合だけ、" + packets[0]["text"]
        self.assertTrue(validator.workflow_quote_bindings(record, changed_source)[1])
        record["answer"]["answer"] = "\n".join(r["audit"]["supported_value"] for r in record["field_runs"])
        bound = policy.bind_source_metadata(record, packets)
        _, graph, result = validator.build_and_validate(bound, packets)
        self.assertEqual(result["status"], "pass", result)
        self.assertEqual(graph["claims"][0]["claim_kind"], "workflow_quotes")
        self.assertEqual(graph["claims"][0]["value_parts"], [packets[0]["text"]])
        final, llm = run_final(bound, packets)
        self.assertTrue(llm.called)
        for key, wrong in [("supported_value", "捏造"), ("supporting_packet_ids", ["E2"]),
                           ("workflow_quote_input_ids", []), ("workflow_quotes", [])]:
            changed = copy.deepcopy(record)
            changed["field_runs"][0]["audit"][key] = wrong
            self.assertTrue(validator.workflow_quote_bindings(changed, packets)[1])
        for trace in ("workflow_context_attempts",):
            changed = copy.deepcopy(record)
            changed["field_runs"][0][trace] = []
            self.assertTrue(validator.workflow_quote_bindings(changed, packets)[1])


def grouped_fixture():
    record, packets = fixture()
    packets = [{**packets[0], 'evidence_id': f'E{i}',
                'locator': {'sheet_name': '発送', 'row_index': i}, 'text': text}
               for i, text in enumerate(('発送を保留して連絡する。', '送り先に不備がある場合。',
                                         '確認と連絡は発送担当者が行う。', '作業後に記録する。'), 1)]
    groups = [{'phase': '実施', 'kind': '条件付き', 'action_ids': ['E1'],
               'condition_ids': ['E2'], 'actor_ids': ['E3']}]
    value, ids, quotes = validator.project_workflow_groups(groups, {p['evidence_id']: p['text'] for p in packets})
    record['field_runs'] = record['field_runs'][:1]
    run = record['field_runs'][0]
    run['audit'].update(workflow_groups=groups, workflow_quotes=quotes,
                        workflow_selection=[{'heading': q['heading'], 'evidence_id': q['evidence_id']} for q in quotes],
                        workflow_quote_input_ids=[p['evidence_id'] for p in packets],
                        supporting_packet_ids=ids, supported_value=value)
    run['workflow_context_attempts'] = [{'delivery_status': 'response_received',
                                       'input_evidence_ids': [p['evidence_id'] for p in packets]}]
    run['retrieved_evidence_ids'] = ids  # Final audit still needs the omitted E4 input.
    record['question_plan']['items'] = [run['item']]
    record['answer'].update(answer=value, evidence_ids=ids)
    return record, packets


class WorkflowAssignmentContractTests(unittest.TestCase):
    def test_sparse_groups_keep_shared_actions_and_cross_source_qualifiers(self):
        assignments = {
            'E1': ['準備/通常', 1, [], [1]],
            'E2': ['準備/通常', 1, [], [1]],
            'E3': ['実施/条件付き', 3, [], []],
            'E4': ['実施/条件付き', 4, [], []],
            'E5': ['補足', 0, [3, 4], []],
            'E6': ['補足', 0, [], [3, 4]],
            'E7': ['終了/通常', 7, [], []],
            'E8': ['不採用', 0, [], []],
        }
        groups = validator.workflow_groups_from_assignments(assignments, list(assignments))
        self.assertEqual([g['phase'] for g in groups], ['準備', '実施', '実施', '終了'])
        self.assertEqual(groups[0]['action_ids'], ['E1', 'E2'])
        self.assertEqual(groups[0]['actor_ids'], ['E1', 'E2'])
        self.assertTrue(all(g['condition_ids'] == ['E5'] and g['actor_ids'] == ['E6']
                            for g in groups[1:3]))
        self.assertEqual(groups[3]['actor_ids'], [])
        self.assertEqual(groups[3]['condition_ids'], [])
        self.assertEqual([eid for group in groups for eid in group['action_ids']],
                         ['E1', 'E2', 'E3', 'E4', 'E7'])

    def test_all_phases_in_group_one_remains_a_hard_failure(self):
        assignments = {'E1': ['準備/通常', 1, [], []],
                       'E2': ['実施/通常', 1, [], []],
                       'E3': ['終了/通常', 1, [], []]}
        original = copy.deepcopy(assignments)
        with self.assertRaisesRegex(ValueError, 'workflow_assignment_group_classification_conflict'):
            validator.workflow_groups_from_assignments(assignments, list(assignments))
        self.assertEqual(assignments, original)  # No repair or silent regrouping.

    def assignments(self):
        return {'E1': ['実施/条件付き', 1, [], []],
                'E2': ['補足', 0, [1], []],
                'E3': ['補足', 0, [], [1]],
                'E4': ['不採用', 0, [], []]}

    def record(self):
        record, packets = grouped_fixture()
        record['field_runs'][0]['audit']['workflow_assignments'] = self.assignments()
        return record, packets

    def test_three_businesses_keep_condition_actor_and_omitted_source(self):
        for business in ('発送', '備品貸出', '検品'):
            with self.subTest(business=business):
                assignments = self.assignments()
                groups = validator.workflow_groups_from_assignments(assignments, list(assignments))
                texts = {'E1': business + 'を保留して連絡する。', 'E2': '異常がある場合。',
                         'E3': business + 'の担当者が行う。', 'E4': '別作業の説明。'}
                value, ids, quotes = validator.project_workflow_groups(groups, texts)
                self.assertEqual(ids, ['E1', 'E2', 'E3'])
                self.assertIn('適用条件（原文）：\n' + texts['E2'], value)
                self.assertIn('担当（原文）：\n' + texts['E3'], value)
                self.assertNotIn(texts['E4'], value)
                self.assertEqual([q['quote'] for q in quotes], [texts[eid] for eid in ids])

    def test_group_number_sort_and_input_order_ignore_json_key_order(self):
        assignments = {'E2': ['実施/通常', 7, [], []], 'E10': ['実施/通常', 7, [], []],
                       'E1': ['準備/通常', 2, [], []], 'E3': ['補足', 0, [7, 2], [2, 7]]}
        expected_ids = ['E10', 'E2', 'E3', 'E1']
        groups = validator.workflow_groups_from_assignments(assignments, expected_ids)
        self.assertEqual([g['phase'] for g in groups], ['準備', '実施'])
        self.assertEqual(groups[1]['action_ids'], ['E10', 'E2'])
        self.assertTrue(all(g['condition_ids'] == ['E3'] and g['actor_ids'] == ['E3'] for g in groups))

    def test_source_can_be_action_condition_and_actor_without_duplicate_action(self):
        assignments = {'E1': ['実施/条件付き', 1, [1, 2], [1, 2]],
                       'E2': ['終了/通常', 2, [], []]}
        groups = validator.workflow_groups_from_assignments(assignments, ['E1', 'E2'])
        value, ids, quotes = validator.project_workflow_groups(
            groups, {'E1': '異常時は作業担当者が保留する。', 'E2': '記録する。'})
        self.assertEqual(groups[0]['action_ids'], ['E1'])
        self.assertEqual(groups[0]['condition_ids'], ['E1'])
        self.assertEqual(groups[0]['actor_ids'], ['E1'])
        self.assertEqual(groups[1]['condition_ids'], ['E1'])
        self.assertEqual(groups[1]['actor_ids'], ['E1'])
        self.assertEqual(ids, ['E1', 'E2'])
        self.assertEqual(len(quotes), 2)
        self.assertEqual(value.count('異常時は作業担当者が保留する。'), 2)

    def test_exact_input_ids_and_entry_shapes_required(self):
        valid = self.assignments()
        for expected in (None, [], ['E1', 'E1'], [''], ['E1', True], list(valid) + ['x'] * 80):
            with self.subTest(expected=expected), self.assertRaises(ValueError):
                validator.workflow_groups_from_assignments(valid, expected)
        for assignments in (None, [], {}, {key: value for key, value in valid.items() if key != 'E4'},
                            {**valid, 'UNKNOWN': ['不採用', 0, [], []]}):
            with self.subTest(assignments=assignments), self.assertRaises(ValueError):
                validator.workflow_groups_from_assignments(assignments, list(valid))
        for entry in (None, {}, ['実施/通常', 1, []], ['実施/通常', 1, [], [], 'extra'],
                      ('実施/通常', 1, [], []), [None, 1, [], []], ['終了/架空', 1, [], []]):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                validator.workflow_groups_from_assignments({**valid, 'E1': entry}, list(valid))

    def test_numbers_and_qualifier_references_are_strict(self):
        valid = self.assignments()
        for number in (True, False, 1.0, '1', -1, 81, None):
            changed = copy.deepcopy(valid)
            changed['E1'][1] = number
            with self.subTest(number=number), self.assertRaises(ValueError):
                validator.workflow_groups_from_assignments(changed, list(valid))
        for role in (2, 3):
            for references in (None, (1,), [True], [1.0], ['1'], [0], [81], [1, 1], [1] * 81):
                changed = copy.deepcopy(valid)
                changed['E3'][role] = references
                with self.subTest(role=role, references=references), self.assertRaises(ValueError):
                    validator.workflow_groups_from_assignments(changed, list(valid))

    def test_rejected_supplement_missing_action_and_classification_conflicts_reject(self):
        valid = self.assignments()
        cases = [('E4', ['不採用', 1, [], []]), ('E4', ['不採用', 0, [1], []]),
                 ('E4', ['不採用', 0, [], [1]]), ('E4', ['補足', 1, [1], []]),
                 ('E4', ['補足', 0, [], []]), ('E1', ['実施/通常', 0, [], []]),
                 ('E4', ['実施/通常', 1, [], []]), ('E3', ['補足', 0, [], [2]])]
        for eid, entry in cases:
            with self.subTest(eid=eid, entry=entry), self.assertRaises(ValueError):
                validator.workflow_groups_from_assignments({**valid, eid: entry}, list(valid))
        with self.assertRaisesRegex(ValueError, 'actions_missing'):
            validator.workflow_groups_from_assignments({'E1': ['不採用', 0, [], []]}, ['E1'])
        with self.assertRaisesRegex(ValueError, 'condition_missing'):
            validator.workflow_groups_from_assignments({'E1': ['実施/条件付き', 1, [], []]}, ['E1'])

    def test_formal_rebinding_and_final_audit_keep_all_input_sources(self):
        record, packets = self.record()
        bindings, failures = validator.workflow_quote_bindings(record, packets)
        self.assertFalse(failures, failures)
        self.assertEqual(bindings['F1']['evidence_ids'], ['E1', 'E2', 'E3'])
        _, _, validation = validator.build_and_validate(record, packets)
        self.assertEqual(validation['status'], 'pass', validation)
        result, llm = run_final(record, packets)
        self.assertEqual(result['deterministic_claim_validation']['status'], 'pass')
        self.assertEqual([p['id'] for p in llm.call_args.args[5]['workflow_groups']['sources']],
                         ['E1', 'E2', 'E3', 'E4'])

    def test_empty_assignments_do_not_enable_workflow_binding_for_other_fields(self):
        record, packets = fixture()
        for run in record['field_runs']:
            run['audit']['workflow_assignments'] = {}
        self.assertEqual(validator.workflow_quote_bindings(record, packets), ({}, []))
        record['field_runs'][0]['audit'].update(verdict='insufficient', supported_value='',
                                               supporting_packet_ids=[])
        self.assertEqual(validator.workflow_quote_bindings(record, packets), ({}, []))

    def test_tampered_assignment_or_group_and_missing_projection_fail(self):
        record, packets = self.record()
        mutations = [
            ('workflow_assignments', {**self.assignments(), 'E2': ['補足', 0, [], [1]]}),
            ('workflow_assignments', {**self.assignments(), 'E4': ['終了/通常', 2, [], []]}),
            ('workflow_assignments', {}), ('workflow_groups', []), ('workflow_quotes', []),
            ('workflow_quote_input_ids', ['E1', 'E2', 'E3']),
        ]
        for key, value in mutations:
            changed = copy.deepcopy(record)
            changed['field_runs'][0]['audit'][key] = value
            with self.subTest(key=key, value=value):
                self.assertTrue(validator.workflow_quote_bindings(changed, packets)[1])
        changed = copy.deepcopy(record)
        del changed['field_runs'][0]['audit']['workflow_groups']
        self.assertTrue(validator.workflow_quote_bindings(changed, packets)[1])

    def test_cannot_hide_or_forge_unused_source_in_assignment_input(self):
        record, packets = self.record()
        changed = copy.deepcopy(record)
        audit = changed['field_runs'][0]['audit']
        del audit['workflow_assignments']['E4']
        audit['workflow_quote_input_ids'].remove('E4')
        failures = validator.workflow_quote_bindings(changed, packets)[1]
        self.assertIn('input_coverage_unconfirmed', failures[0]['detail'])
        self.assertTrue(validator.workflow_quote_bindings(record, packets[:-1])[1])
        changed = copy.deepcopy(record)
        changed['field_runs'][0]['workflow_context_attempts'] = []
        self.assertTrue(validator.workflow_quote_bindings(changed, packets)[1])


class WorkflowGroupAuditTests(unittest.TestCase):
    def test_native_cell_originals_and_unused_header_reach_final_audit_unchanged(self):
        record, packets = grouped_fixture()
        for packet, cell in zip(packets, ('B3', 'C3', 'D3', 'B1')):
            packet['locator'] = {'sheet_name': '発送', 'cell': cell}
        # This unselected original is a candidate header, not an actor. It
        # stays in the complete input for the auditor to judge independently.
        packets[-1]['text'] = '担当者一覧はこちら'
        _, graph, validation = validator.build_and_validate(record, packets)
        self.assertEqual(validation['status'], 'pass', validation)
        context = final_audit.workflow_group_audit_context(record, graph, packets)
        self.assertEqual([s['text'] for s in context['sources']], [p['text'] for p in packets])
        self.assertEqual([s['locator'] for s in context['sources']], [p['locator'] for p in packets])
        self.assertEqual(context['groups'][0]['condition_ids'], ['E2'])
        self.assertEqual(context['groups'][0]['actor_ids'], ['E3'])
        self.assertNotIn('E4', context['groups'][0]['actor_ids'])
        self.assertEqual(context['source_bindings'], {p['evidence_id']: p['evidence_id'] for p in packets})
        _, _, payload = self.call_group_audit(record, packets, context, self.result(context))
        prompt = payload['messages'][1]['content']
        for packet in packets:
            self.assertIn(packet['text'], prompt)
            self.assertIn(packet['locator']['cell'], prompt)
        with self.assertRaisesRegex(ValueError, 'source_missing'):
            final_audit.workflow_group_audit_context(record, graph, packets[:-1])
        # A selected-only subset cannot pretend the unselected header was never sent.
        changed = copy.deepcopy(record)
        changed['field_runs'][0]['audit']['workflow_quote_input_ids'].remove('E4')
        with self.assertRaisesRegex(ValueError, 'input_coverage_unconfirmed'):
            final_audit.workflow_group_audit_context(changed, graph, packets)

    def context(self, group_count=1):
        record, packets = grouped_fixture()
        if group_count > 1:
            packets = [{**packets[0], 'evidence_id': f'E{i}',
                        'locator': {'sheet_name': '発送', 'row_index': i},
                        'text': f'箱{i}を点検する。'} for i in range(1, group_count + 1)]
            groups = [{'phase': '実施', 'kind': '通常', 'action_ids': [p['evidence_id']],
                       'condition_ids': [], 'actor_ids': []} for p in packets]
            value, ids, quotes = validator.project_workflow_groups(
                groups, {p['evidence_id']: p['text'] for p in packets})
            run = record['field_runs'][0]
            run['audit'].update(
                workflow_groups=groups, workflow_quotes=quotes,
                workflow_selection=[{'heading': q['heading'], 'evidence_id': q['evidence_id']}
                                    for q in quotes],
                workflow_quote_input_ids=ids, supporting_packet_ids=ids, supported_value=value)
            run['workflow_context_attempts'] = [
                {'delivery_status': 'response_received', 'input_evidence_ids': ids}]
            run['retrieved_evidence_ids'] = ids
            record['answer'].update(answer=value, evidence_ids=ids)
        _, graph, validation = validator.build_and_validate(record, packets)
        self.assertEqual(validation['status'], 'pass', validation)
        return record, packets, graph, final_audit.workflow_group_audit_context(record, graph, packets)

    def result(self, context):
        return {'verdict': 'verified', 'reason': '確認済み', 'unsupported_claims': [],
                'group_checks': [{'group_id': g['group_id'], 'checks': ['pass', 'pass', 'pass']}
                                 for g in context['groups']],
                'coverage': 'pass', 'missing_evidence_ids': []}

    def call_group_audit(self, record, packets, context, result, **metadata):
        raw = {'done': True, 'done_reason': 'stop', 'prompt_eval_count': 100,
               'eval_count': 1700, 'message': {'content': json.dumps(result)}, **metadata}
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(raw).encode()
        with mock.patch.object(final_audit.LOCAL_HTTP_OPENER, 'open', return_value=response) as opened:
            observed, performance = final_audit.audit_fail_closed(
                'test-model', record['query'], record['answer'], packets, 180,
                {'workflow_groups': context})
        self.assertEqual(opened.call_count, 1)
        payload = json.loads(opened.call_args.args[0].data)
        self.assertEqual(payload['options']['num_ctx'], 8192)
        self.assertEqual(payload['options']['num_predict'], 2000)
        self.assertEqual(performance['context_usage']['requested_output_tokens'], 2000)
        return observed, performance, payload

    def test_formal_condition_actor_groups_preserved_in_final_audit(self):
        record, packets, _, context = self.context()
        self.assertEqual(context['groups'][0]['condition_ids'], ['E2'])
        self.assertEqual(context['groups'][0]['actor_ids'], ['E3'])
        self.assertEqual([p['text'] for p in context['sources']], [p['text'] for p in packets])
        final, llm = run_final(record, packets)
        self.assertTrue(llm.called)
        self.assertEqual(len(llm.call_args.args[5]['workflow_groups']['sources']), 4)
        self.assertEqual(final['deterministic_claim_validation']['status'], 'pass')
        self.assertEqual(final['workflow_group_audit_context']['source_bindings']['E4'], 'E4')
        self.assertEqual(final['workflow_group_audit_context']['groups'][0]['group_id'], 'G1')

    def test_tampered_condition_actor_and_rendering_fail_formal_binding(self):
        record, packets, _, _ = self.context()
        for role, wrong in (('condition_ids', ['E3']), ('actor_ids', ['E2'])):
            changed = copy.deepcopy(record)
            changed['field_runs'][0]['audit']['workflow_groups'][0][role] = wrong
            self.assertTrue(validator.workflow_quote_bindings(changed, packets)[1])
        changed = copy.deepcopy(record)
        changed['field_runs'][0]['audit']['supported_value'] += '例外はない。'
        self.assertTrue(validator.workflow_quote_bindings(changed, packets)[1])

    def test_blanket_verified_cannot_override_group_or_coverage_failure(self):
        _, _, _, context = self.context()
        for position in range(3):
            for verdict in ('fail', 'unverified'):
                result = self.result(context)
                result['group_checks'][0]['checks'][position] = verdict
                final_audit.validate_workflow_group_audit(result, context)
                self.assertEqual(result['verdict'], 'rejected')
        for verdict in ('fail', 'unverified'):
            result = self.result(context)
            result['coverage'] = verdict
            final_audit.validate_workflow_group_audit(result, context)
            self.assertEqual(result['verdict'], 'rejected')
        result = self.result(context)
        # A missing source now means unselected content, not coverage=pass.
        result['coverage'] = 'fail'
        result['missing_evidence_ids'] = ['E4']
        final_audit.validate_workflow_group_audit(result, context)
        self.assertEqual(result['verdict'], 'rejected')

    def test_unknown_explicit_references_are_rejected_before_normalization(self):
        _, _, _, context = self.context()
        for field in ('reason', 'unsupported_claims'):
            for reference in ('G46', 'E99', 'G0', 'E0', 'G001', 'E004'):
                result = self.result(context)
                result['group_checks'][0]['checks'][1] = 'fail'
                text = f'{reference}について条件を確認できません。'
                result[field] = text if field == 'reason' else [text]
                before = copy.deepcopy(result)
                with self.subTest(field=field, reference=reference), self.assertRaisesRegex(
                        ValueError, '^workflow_group_audit_reference_invalid$'):
                    final_audit.validate_workflow_group_audit(result, context)
                # Invalid explanations must not be silently erased by normalizing.
                self.assertEqual(result, before)

    def test_known_group_ranges_and_source_references_are_valid(self):
        _, _, _, context = self.context(group_count=34)
        for separator in ('〜', '～', '-', '–'):
            result = self.result(context)
            result['reason'] = f'G1{separator}G34をE1〜E34で確認済み。'
            final_audit.validate_workflow_group_audit(result, context)
            self.assertEqual(result['verdict'], 'verified')
            invalid = self.result(context)
            invalid['reason'] = f'G1{separator}G35を確認した。'
            with self.subTest(separator=separator), self.assertRaisesRegex(
                    ValueError, '^workflow_group_audit_reference_invalid$'):
                final_audit.validate_workflow_group_audit(invalid, context)

    def test_cell_addresses_and_embedded_words_are_not_alias_references(self):
        _, _, _, context = self.context()
        result = self.result(context)
        result['reason'] = 'B1、C31、STAGE99、EDGE99、prefixG99suffix、xE99x、_G99、E99_suffixを確認。'
        final_audit.validate_workflow_group_audit(result, context)
        self.assertEqual(result['verdict'], 'verified')

    def test_alias_ranges_validate_omitted_prefix_and_every_member(self):
        _, _, _, context = self.context(group_count=34)
        result = self.result(context)
        result['reason'] = 'G1〜34とE1-34を確認。'
        final_audit.validate_workflow_group_audit(result, context)
        self.assertEqual(result['verdict'], 'verified')
        for reason in ('G1〜35を確認。', 'E1-99が不足。', 'G34〜G1を確認。'):
            invalid = self.result(context)
            invalid['reason'] = reason
            with self.subTest(reason=reason), self.assertRaisesRegex(
                    ValueError, '^workflow_group_audit_reference_invalid$'):
                final_audit.validate_workflow_group_audit(invalid, context)
        # Endpoint existence does not prove all intermediate group IDs exist.
        context['groups'] = [g for g in context['groups'] if g['group_id'] != 'G2']
        invalid = self.result(context)
        invalid['reason'] = 'G1〜G34を確認。'
        with self.assertRaisesRegex(ValueError, '^workflow_group_audit_reference_invalid$'):
            final_audit.validate_workflow_group_audit(invalid, context)

    def test_known_references_do_not_cancel_condition_or_actor_failures(self):
        record, packets, _, context = self.context()
        for position, evidence_id in ((1, 'E2'), (2, 'E3')):
            for verdict in ('fail', 'unverified'):
                result = self.result(context)
                result['reason'] = f'G1と{evidence_id}の対応を確認できません。'
                result['unsupported_claims'] = [f'G1: {evidence_id}の対応']
                result['group_checks'][0]['checks'][position] = verdict
                before = copy.deepcopy(result)
                observed, performance, _ = self.call_group_audit(record, packets, context, result)
                with self.subTest(position=position, verdict=verdict):
                    self.assertEqual(observed['verdict'], 'rejected')
                    self.assertNotEqual(observed.get('status'), 'incomplete')
                    self.assertNotIn('failed', performance)
                    self.assertEqual(performance['workflow_group_model_result'], before)
                    self.assertNotEqual(observed['reason'], before['reason'])

    def test_missing_selected_action_condition_or_actor_is_inconsistent(self):
        _, _, _, context = self.context()
        for role in ('action_ids', 'condition_ids', 'actor_ids'):
            result = self.result(context)
            result['coverage'] = 'fail'
            result['missing_evidence_ids'] = context['groups'][0][role][:1]
            self.assertTrue(result['missing_evidence_ids'])
            with self.subTest(role=role), self.assertRaisesRegex(
                    ValueError, '^workflow_group_audit_missing_selected_source$'):
                final_audit.validate_workflow_group_audit(result, context)

    def test_missing_source_selection_includes_later_groups_and_other_claims(self):
        _, _, _, context = self.context(group_count=3)
        for evidence_id in ('E2', 'E3'):
            result = self.result(context)
            result.update(coverage='fail', missing_evidence_ids=[evidence_id])
            with self.subTest(evidence_id=evidence_id), self.assertRaisesRegex(
                    ValueError, '^workflow_group_audit_missing_selected_source$'):
                final_audit.validate_workflow_group_audit(result, context)
        _, _, _, context = self.context()
        context['other_claims'] = [{'claim_id': 'C2', 'evidence_ids': ['E4'],
                                    'value': '参考情報の原文。'}]
        result = self.result(context)
        result.update(coverage='fail', missing_evidence_ids=['E4'])
        with self.assertRaisesRegex(ValueError, '^workflow_group_audit_missing_selected_source$'):
            final_audit.validate_workflow_group_audit(result, context)
        # A source used elsewhere is not proof that all required relations hold.
        result = self.result(context)
        result['coverage'] = 'unverified'
        final_audit.validate_workflow_group_audit(result, context)
        self.assertEqual(result['verdict'], 'rejected')

    def test_unselected_missing_source_is_a_content_rejection_not_invalid_audit(self):
        record, packets, _, context = self.context()
        result = self.result(context)
        result.update(coverage='fail', missing_evidence_ids=['E4'])
        before = copy.deepcopy(result)
        observed, performance, _ = self.call_group_audit(record, packets, context, result)
        self.assertEqual(observed['verdict'], 'rejected')
        self.assertNotEqual(observed.get('status'), 'incomplete')
        self.assertNotIn('failed', performance)
        self.assertEqual(performance['workflow_group_model_result'], before)

    def test_pass_coverage_cannot_list_missing_unselected_source(self):
        _, _, _, context = self.context()
        result = self.result(context)
        result['missing_evidence_ids'] = ['E4']
        with self.assertRaisesRegex(ValueError, '^workflow_group_audit_coverage_invalid$'):
            final_audit.validate_workflow_group_audit(result, context)

    def test_missing_source_ids_must_be_present_unique_and_known(self):
        _, _, _, context = self.context()
        for missing in (None, ['E99'], ['E4', 'E4']):
            result = self.result(context)
            result.update(coverage='fail', missing_evidence_ids=missing)
            with self.subTest(missing=missing), self.assertRaisesRegex(
                    ValueError, '^workflow_group_audit_coverage_invalid$'):
                final_audit.validate_workflow_group_audit(result, context)
        result = self.result(context)
        del result['missing_evidence_ids']
        with self.assertRaisesRegex(ValueError, '^workflow_group_audit_coverage_invalid$'):
            final_audit.validate_workflow_group_audit(result, context)

    def test_inconsistent_model_reports_are_retained_without_silent_repair(self):
        record, packets, _, context = self.context()
        cases = [({'reason': 'G46について確認しました。'}, 'workflow_group_audit_reference_invalid'),
                 ({'unsupported_claims': ['E99が不足']}, 'workflow_group_audit_reference_invalid'),
                 ({'coverage': 'fail', 'missing_evidence_ids': ['E1']},
                  'workflow_group_audit_missing_selected_source'),
                 ({'missing_evidence_ids': ['E4']}, 'workflow_group_audit_coverage_invalid'),
                 ({'group_checks': []}, 'workflow_group_audit_check_coverage_missing'),
                 ({'unsupported_claims': ['G1の対応']}, 'verified_with_unsupported_claim'),
                 ({'verdict': 'qualified'}, 'qualified_without_unsupported_claim')]
        for updates, reason_code in cases:
            result = self.result(context)
            result.update(updates)
            before = copy.deepcopy(result)
            observed, performance, _ = self.call_group_audit(record, packets, context, result)
            with self.subTest(updates=updates):
                self.assertEqual(observed['verdict'], 'rejected')
                self.assertEqual(observed['status'], 'incomplete')
                self.assertEqual(observed['reason_code'], reason_code)
                self.assertEqual(observed['unsupported_claims'], [])
                self.assertTrue(performance['failed'])
                self.assertEqual(performance['workflow_group_model_result'], before)
                self.assertEqual(result, before)
                self.assertEqual(performance['context_usage']['input_tokens'], 100)
                self.assertEqual(performance['context_usage']['output_tokens'], 1700)

    def test_all_pass_keeps_independent_raw_report_and_existing_schema_caps(self):
        record, packets, _, context = self.context(group_count=34)
        result = self.result(context)
        result['reason'] = 'G1〜G34とE1〜E34を確認済み。'
        observed, performance, payload = self.call_group_audit(record, packets, context, result)
        self.assertEqual(observed['verdict'], 'verified')
        self.assertEqual(performance['workflow_group_model_result'], result)
        observed['group_checks'][0]['checks'][0] = 'fail'
        self.assertEqual(performance['workflow_group_model_result']['group_checks'][0]['checks'][0], 'pass')
        self.assertEqual(payload['format']['properties']['group_checks']['maxItems'], 80)
        self.assertEqual(payload['format']['properties']['missing_evidence_ids']['maxItems'], 6)

    def test_group_prompt_distinguishes_spoken_actions_headers_and_missing_sources(self):
        record, _, _, context = self.context()
        prompt = final_audit.workflow_group_audit_prompt(record['query'], context)
        for instruction in (
                'スクリプト・セリフでも確認、依頼、受け渡し、引継ぎ',
                'スクリプトという見出しだけで、その中の行動を一律に除外しません',
                '逆に引用が一致するだけで必要な手順とは認めません',
                'missing_evidence_idsが空でもcoverageのfail/unverifiedは可能',
                'G番号は回答の組、E番号は原文',
                '判定欄でpassとした内容を理由欄で否定しません',
                '同じ行/近接するセルだけで条件、担当、業務順を証明できません'):
            self.assertIn(instruction, prompt)
        # A general example must not bake private business answers into the audit.
        self.assertIn('貸出担当', prompt)
        self.assertNotIn('DAWN', prompt)
        self.assertNotIn('配膳', prompt)
        transmitted = json.loads(prompt.split('<UNTRUSTED_WORKFLOW_DATA>\n', 1)[1]
                                 .split('\n</UNTRUSTED_WORKFLOW_DATA>', 1)[0])
        self.assertEqual(transmitted['groups'], context['groups'])
        self.assertEqual([s['text'] for s in transmitted['sources']],
                         [s['text'] for s in context['sources']])

    def test_absent_actor_and_condition_preserve_auditor_pass_without_fabrication(self):
        record, packets, _, context = self.context(group_count=2)
        before = copy.deepcopy(context)
        for group in context['groups']:
            self.assertEqual(group['actor_ids'], [])
            self.assertEqual(group['condition_ids'], [])
        result = self.result(context)
        observed, performance, payload = self.call_group_audit(record, packets, context, result)
        # This is a transport/gate test, not proof that a live model will
        # correctly judge whether an actor or condition is stated in a source.
        self.assertEqual(observed['verdict'], 'verified')
        self.assertEqual(performance['workflow_group_model_result'], result)
        transmitted = json.loads(payload['messages'][1]['content']
                                 .split('<UNTRUSTED_WORKFLOW_DATA>\n', 1)[1]
                                 .split('\n</UNTRUSTED_WORKFLOW_DATA>', 1)[0])
        self.assertEqual(transmitted['groups'], before['groups'])
        self.assertEqual([s['text'] for s in transmitted['sources']],
                         [p['text'] for p in packets])
        self.assertEqual(context, before)

    def test_absence_prompt_resolves_sources_and_separates_omission_from_nonstatement(self):
        record, packets, _, context = self.context()
        _, _, payload = self.call_group_audit(record, packets, context, self.result(context))
        prompt = payload['messages'][1]['content']
        for instruction in (
                'action_ids/condition_ids/actor_idsからsourcesの同じE番号の原文を読みます',
                'Gの数字でEを選びません',
                'その時点とphaseを照合します',
                '時点のない原文へ前後関係を創作しません',
                'condition_idsの空欄自体は誤りではありません',
                '行動原文に全文保持された禁止・数量制限も読み',
                '条件付き作業を通常扱いする誤りや、別の条件へのすり替えはfail',
                '行動原文または担当原文に担当と行動の対応が保持されているか',
                '脱落・入違いをfail',
                '明示がない場合は、担当を勝手に補っていないか',
                '原文に担当の指定がなく回答も指定していなければ、この担当判定はpass',
                'actor_idsが空という理由だけでfail/unverifiedにしません',
                '逆に空欄なら常にpassにもせず',
                '対応が曖昧な場合は区別して検査',
                'G番号・判定軸（分類/条件/担当）・照合したE番号と具体的な相違'):
            with self.subTest(instruction=instruction):
                self.assertIn(instruction, prompt)
        self.assertNotIn('DAWN', prompt)
        self.assertNotIn('配膳', prompt)

    def test_inline_actor_source_reaches_auditor_without_filling_separate_actor_ids(self):
        record, packets = grouped_fixture()
        packets[0]['text'] = '発送担当者が発送を保留して連絡する。'
        run = record['field_runs'][0]
        groups = copy.deepcopy(run['audit']['workflow_groups'])
        groups[0]['actor_ids'] = []
        value, ids, quotes = validator.project_workflow_groups(
            groups, {packet['evidence_id']: packet['text'] for packet in packets})
        run['audit'].update(
            workflow_groups=groups, workflow_quotes=quotes, supported_value=value,
            supporting_packet_ids=ids,
            workflow_selection=[{'heading': q['heading'], 'evidence_id': q['evidence_id']}
                                for q in quotes])
        run['retrieved_evidence_ids'] = ids
        record['answer'].update(answer=value, evidence_ids=ids)
        _, graph, validation = validator.build_and_validate(record, packets)
        self.assertEqual(validation['status'], 'pass', validation)
        context = final_audit.workflow_group_audit_context(record, graph, packets)
        observed, performance, payload = self.call_group_audit(
            record, packets, context, self.result(context))
        self.assertEqual(observed['verdict'], 'verified')
        self.assertEqual(context['groups'][0]['actor_ids'], [])
        self.assertIn(packets[0]['text'], payload['messages'][1]['content'])
        self.assertIn(packets[2]['text'], payload['messages'][1]['content'])
        self.assertEqual(performance['workflow_group_model_result']['group_checks'][0]
                         ['checks'][2], 'pass')

    def test_empty_actor_or_condition_never_overrides_semantic_fail_or_unverified(self):
        record, packets, _, context = self.context(group_count=2)
        for position, label in ((1, '条件'), (2, '担当')):
            for verdict in ('fail', 'unverified'):
                result = self.result(context)
                result['group_checks'][0]['checks'][position] = verdict
                result['reason'] = f'G1の{label}とE1の対応を確認できません。'
                before = copy.deepcopy(result)
                observed, performance, _ = self.call_group_audit(record, packets, context, result)
                with self.subTest(position=position, verdict=verdict):
                    self.assertEqual(observed['verdict'], 'rejected')
                    self.assertNotEqual(observed.get('status'), 'incomplete')
                    self.assertEqual(observed['group_checks'][0]['checks'][position], verdict)
                    self.assertEqual(observed['group_checks'][1]['checks'], ['pass'] * 3)
                    self.assertEqual(performance['workflow_group_model_result'], before)
                    self.assertEqual(result, before)

    def test_empty_role_lists_do_not_waive_complete_three_axis_audit(self):
        record, packets, _, context = self.context(group_count=2)
        result = self.result(context)
        # Missing actor verification is still an incomplete audit, even when
        # the answer has no separate actor IDs. No automatic PASS is supplied.
        result['group_checks'][0]['checks'] = ['pass', 'pass']
        observed, performance, payload = self.call_group_audit(record, packets, context, result)
        self.assertEqual(observed['status'], 'incomplete')
        self.assertEqual(observed['verdict'], 'rejected')
        self.assertEqual(observed['reason_code'], 'workflow_group_audit_checks_invalid')
        self.assertEqual(performance['workflow_group_model_result'], result)
        self.assertEqual(payload['format'], final_audit.workflow_group_audit_schema(context))
        self.assertEqual(payload['format']['properties']['group_checks']['items']
                         ['properties']['checks']['items']['enum'], ['pass', 'fail', 'unverified'])

    def test_every_classification_condition_actor_check_is_required(self):
        _, _, _, context = self.context()
        invalid = [[], [{'group_id': 'G1', 'checks': ['pass', 'pass']}],
                   [{'group_id': 'G1', 'checks': ['pass', 'pass', 'pass']}] * 2,
                   [{'group_id': 'UNKNOWN', 'checks': ['pass', 'pass', 'pass']}]]
        for checks in invalid:
            result = self.result(context)
            result['group_checks'] = checks
            with self.subTest(checks=checks), self.assertRaises(ValueError):
                final_audit.validate_workflow_group_audit(result, context)

    def test_missing_duplicate_or_hidden_input_cannot_pass_coverage(self):
        record, packets, graph, _ = self.context()
        for changed in (packets[:-1], packets + [packets[0]]):
            with self.assertRaises(ValueError):
                final_audit.workflow_group_audit_context(record, graph, changed)
        changed = copy.deepcopy(record)
        changed['field_runs'][0]['audit']['workflow_quote_input_ids'].pop()
        with self.assertRaisesRegex(ValueError, 'input_coverage_unconfirmed'):
            final_audit.workflow_group_audit_context(changed, graph, packets)

    def test_full_unselected_source_is_sent_without_prefix_truncation(self):
        record, packets, graph, _ = self.context()
        packets[-1]['text'] = '追加条件の原文。' * 60 + '末尾の担当情報を落とさない。'
        context = final_audit.workflow_group_audit_context(record, graph, packets)
        prompt = final_audit.workflow_group_audit_prompt(record['query'], context)
        self.assertIn(packets[-1]['text'], prompt)
        self.assertEqual(prompt.count(packets[-1]['text']), 1)
        self.assertNotIn('value_parts', prompt)
        packets[-1]['text'] = 'x' * 12001
        with self.assertRaisesRegex(ValueError, 'source_characters_limit'):
            final_audit.workflow_group_audit_context(record, graph, packets)

    def test_same_call_uses_structured_schema_and_approved_group_capacity(self):
        record, packets, _, context = self.context()
        result = self.result(context)
        result['group_checks'][0]['checks'][1] = 'fail'
        raw = {'done': True, 'done_reason': 'stop', 'prompt_eval_count': 100,
               'eval_count': 80, 'message': {'content': json.dumps(result)}}
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(raw).encode()
        with mock.patch.object(final_audit.LOCAL_HTTP_OPENER, 'open', return_value=response) as opened:
            observed, performance = final_audit.audit('test-model', record['query'], record['answer'],
                                                       packets, 180, {'workflow_groups': context})
        self.assertEqual(opened.call_count, 1)
        payload = json.loads(opened.call_args.args[0].data)
        self.assertEqual(payload['options']['num_ctx'], 8192)
        self.assertEqual(payload['options']['num_predict'], 2000)
        self.assertIn('group_checks', payload['format']['required'])
        self.assertEqual(observed['verdict'], 'rejected')
        self.assertEqual(performance['context_usage']['status'], 'observed')

    def test_complete_32_group_result_is_accepted_in_one_call(self):
        record, packets, _, context = self.context(group_count=32)
        result = self.result(context)
        observed, performance, payload = self.call_group_audit(record, packets, context, result)
        expected_ids = [f'G{i}' for i in range(1, 33)]
        self.assertEqual([check['group_id'] for check in observed['group_checks']], expected_ids)
        self.assertEqual(payload['format']['properties']['group_checks']['items']
                         ['properties']['group_id']['enum'], expected_ids)
        self.assertEqual(observed['verdict'], 'verified')
        self.assertNotIn('failed', performance)
        self.assertEqual(performance['context_usage']['status'], 'observed')
        self.assertEqual(performance['context_usage']['output_tokens'], 1700)

    def test_32_group_result_missing_last_group_is_incomplete(self):
        record, packets, _, context = self.context(group_count=32)
        result = self.result(context)
        self.assertEqual(result['group_checks'].pop()['group_id'], 'G32')
        observed, performance, _ = self.call_group_audit(record, packets, context, result)
        self.assertEqual(observed['verdict'], 'rejected')
        self.assertEqual(observed['status'], 'incomplete')
        self.assertEqual(observed['reason_code'], 'workflow_group_audit_check_coverage_missing')
        self.assertTrue(performance['failed'])

    def test_last_group_failure_or_uncertainty_rejects_32_group_result(self):
        record, packets, _, context = self.context(group_count=32)
        for check in ('fail', 'unverified'):
            with self.subTest(check=check):
                result = self.result(context)
                result['group_checks'][-1]['checks'][2] = check
                observed, performance, _ = self.call_group_audit(record, packets, context, result)
                self.assertEqual(observed['verdict'], 'rejected')
                self.assertEqual(observed['unsupported_claims'], ['G32の分類・条件・担当の結び付き'])
                self.assertNotIn('failed', performance)

    def test_length_at_group_capacity_rejects_even_complete_32_group_json(self):
        record, packets, _, context = self.context(group_count=32)
        observed, performance, _ = self.call_group_audit(
            record, packets, context, self.result(context), done_reason='length', eval_count=2000)
        self.assertEqual(observed['verdict'], 'rejected')
        self.assertEqual(observed['status'], 'incomplete')
        self.assertEqual(observed['reason_code'], 'audit_response_truncated')
        self.assertTrue(performance['failed'])
        self.assertEqual(performance['context_usage']['done_reason'], 'length')
        self.assertEqual(performance['context_usage']['output_tokens'], 2000)

    def test_group_transport_failure_records_approved_output_capacity(self):
        record, packets, _, context = self.context(group_count=32)
        with mock.patch.object(final_audit.LOCAL_HTTP_OPENER, 'open',
                               side_effect=TimeoutError('synthetic timeout')) as opened:
            result, performance = final_audit.audit_fail_closed(
                'test-model', record['query'], record['answer'], packets, 180,
                {'workflow_groups': context})
        self.assertEqual(opened.call_count, 1)
        payload = json.loads(opened.call_args.args[0].data)
        self.assertEqual(payload['options']['num_predict'], 2000)
        self.assertEqual(result['verdict'], 'rejected')
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['reason_code'], 'audit_transport_error')
        self.assertTrue(performance['failed'])
        self.assertEqual(performance['context_usage']['requested_output_tokens'], 2000)
        self.assertIsNone(performance['context_usage']['output_tokens'])

    def scope_context(self):
        """Transport-only fixture: overlapping names must not merge sources."""
        record, packets, _, context = self.context()
        context['documents'] = {'D1': '発送/手引2026.xlsx', 'D2': '別事業/手引2026.xlsx',
                                'D3': '注意/原本.pdf'}
        context['sources'][0].update(document='D1', text='  条件付きの原文\n末尾も保持。\n',
            locator={'sheet_name': '発送', 'cell': 'B3', 'row_index': 3,
                     'merged_range': 'B3:C3', 'native_extra': {'reading_order': [3, 1], 'flag': False}})
        context['sources'][1].update(document='D1', locator={'sheet_name': '返却', 'cell': 'B3'})
        context['sources'][2].update(document='D2', locator={'sheet_name': '発送', 'cell': 'B3'})
        # E4 is unselected but was delivered to the generator; keep it for
        # the auditor's independent completeness check.
        context['sources'][3].update(document='D3', text='未選択の追加条件も削らない。',
            locator={'page_number': 4, 'bbox': [1, 2, 30, 40]})
        return record, packets, context

    def restore_scope_sources(self, payload):
        restored = []
        for source in payload['sources']:
            item = copy.deepcopy(source)
            if 'source_scope' in item:
                scope = payload['source_scopes'][item.pop('source_scope')]
                item['document'] = scope['document']
                item['locator']['sheet_name'] = scope['sheet_name']
            restored.append(item)
        return restored

    def test_scope_payload_roundtrip_preserves_every_source_and_locator_field(self):
        _, _, context = self.scope_context()
        before = copy.deepcopy(context)
        payload = final_audit.workflow_group_audit_payload(context)
        self.assertEqual(self.restore_scope_sources(payload), context['sources'])
        self.assertEqual(context, before)
        self.assertNotIn('source_bindings', payload)
        for key in ('groups', 'documents', 'requirements', 'other_claims'):
            self.assertEqual(payload[key], context[key])
        final_audit.validate_workflow_group_audit_payload(context, payload)
        payload_before = copy.deepcopy(payload)
        final_audit.validate_workflow_group_audit_payload(context, payload)
        self.assertEqual(payload, payload_before)
        self.assertEqual(context, before)
        # Returned data must be detached at every nested level.
        payload['sources'][0]['locator']['native_extra']['reading_order'].append(99)
        payload['groups'][0]['action_ids'].append('FORGED')
        self.assertEqual(context, before)

    def test_scope_payload_does_not_merge_equal_sheet_names_across_documents(self):
        _, _, context = self.scope_context()
        payload = final_audit.workflow_group_audit_payload(context)
        self.assertEqual(len(payload['source_scopes']), 3)
        self.assertEqual({(scope['document'], scope['sheet_name'])
                          for scope in payload['source_scopes'].values()},
                         {('D1', '発送'), ('D1', '返却'), ('D2', '発送')})
        self.assertEqual(len({s['source_scope'] for s in payload['sources'][:3]}), 3)
        for source in payload['sources'][:3]:
            self.assertNotIn('document', source)
            self.assertNotIn('sheet_name', source['locator'])
        self.assertEqual(payload['sources'][3], context['sources'][3])
        self.assertEqual(payload['sources'][3]['id'], 'E4')

    def test_scope_payload_reuses_scope_only_for_same_document_and_sheet(self):
        _, _, _, context = self.context()
        payload = final_audit.workflow_group_audit_payload(context)
        self.assertEqual(len(payload['source_scopes']), 1)
        self.assertEqual(len({s['source_scope'] for s in payload['sources']}), 1)
        self.assertEqual(self.restore_scope_sources(payload), context['sources'])
        self.assertIn('E4', [s['id'] for s in payload['sources']])

    def test_scope_payload_keeps_nonsheet_sources_unchanged(self):
        _, _, _, context = self.context()
        for i, source in enumerate(context['sources'], 1):
            source['locator'] = {'page_number': i, 'paragraph': {'index': i, 'empty': None}}
        before = copy.deepcopy(context)
        payload = final_audit.workflow_group_audit_payload(context)
        self.assertEqual(payload['source_scopes'], {})
        self.assertEqual(payload['sources'], before['sources'])
        final_audit.validate_workflow_group_audit_payload(context, payload)
        self.assertEqual(context, before)

    def test_scope_payload_rejects_missing_wrong_or_cross_scope_bindings(self):
        _, _, context = self.scope_context()
        original = final_audit.workflow_group_audit_payload(context)
        for change in ('missing_ref', 'unknown_ref', 'missing_scope', 'cross_document',
                       'cross_sheet', 'corrupt_document', 'corrupt_sheet', 'missing_scope_field'):
            with self.subTest(change=change):
                payload = copy.deepcopy(original)
                source = payload['sources'][0]
                scope_id = source['source_scope']
                if change == 'missing_ref':
                    source.pop('source_scope')
                elif change == 'unknown_ref':
                    source['source_scope'] = 'S_unknown'
                elif change == 'missing_scope':
                    payload['source_scopes'].pop(scope_id)
                elif change == 'cross_document':
                    source['source_scope'] = payload['sources'][2]['source_scope']
                elif change == 'cross_sheet':
                    source['source_scope'] = payload['sources'][1]['source_scope']
                elif change == 'corrupt_document':
                    payload['source_scopes'][scope_id]['document'] = 'D2'
                elif change == 'corrupt_sheet':
                    payload['source_scopes'][scope_id]['sheet_name'] = '別のシート'
                else:
                    payload['source_scopes'][scope_id].pop('sheet_name')
                with self.assertRaises(ValueError):
                    final_audit.validate_workflow_group_audit_payload(context, payload)

    def test_scope_payload_rejects_missing_duplicate_added_or_forged_sources(self):
        _, _, context = self.scope_context()
        original = final_audit.workflow_group_audit_payload(context)
        for change in ('missing_unselected', 'duplicate', 'added', 'changed_id', 'changed_text',
                       'lost_locator', 'lost_locator_field', 'lost_text', 'changed_type', 'added_field'):
            with self.subTest(change=change):
                payload = copy.deepcopy(original)
                if change == 'missing_unselected':
                    payload['sources'].pop()
                elif change == 'duplicate':
                    payload['sources'].append(copy.deepcopy(payload['sources'][0]))
                elif change == 'added':
                    payload['sources'].append({**copy.deepcopy(payload['sources'][0]), 'id': 'E_unknown'})
                elif change == 'changed_id':
                    payload['sources'][0]['id'] = 'E_unknown'
                elif change == 'changed_text':
                    payload['sources'][0]['text'] = payload['sources'][0]['text'].strip()
                elif change == 'lost_locator':
                    payload['sources'][0].pop('locator')
                elif change == 'lost_locator_field':
                    payload['sources'][0]['locator'].pop('merged_range')
                elif change == 'lost_text':
                    payload['sources'][0].pop('text')
                elif change == 'changed_type':
                    payload['sources'][0]['locator']['native_extra']['flag'] = 0
                else:
                    payload['sources'][0]['unbound_extra'] = 'data'
                with self.assertRaises(ValueError):
                    final_audit.validate_workflow_group_audit_payload(context, payload)

    def test_scope_payload_rejects_unused_duplicate_or_malformed_scope_definitions(self):
        _, _, context = self.scope_context()
        original = final_audit.workflow_group_audit_payload(context)
        for change in ('unused', 'duplicate_identity', 'bad_alias', 'bad_document',
                       'bad_sheet_type', 'extra_scope_field', 'scopes_not_mapping'):
            payload = copy.deepcopy(original)
            sid = payload['sources'][0]['source_scope']
            if change == 'unused':
                payload['source_scopes']['S99'] = {'document': 'D1', 'sheet_name': '未使用'}
            elif change == 'duplicate_identity':
                payload['source_scopes']['S99'] = copy.deepcopy(payload['source_scopes'][sid])
            elif change == 'bad_alias':
                payload['source_scopes']['not-a-scope'] = payload['source_scopes'].pop(sid)
                payload['sources'][0]['source_scope'] = 'not-a-scope'
            elif change == 'bad_document':
                payload['source_scopes'][sid]['document'] = 'D_missing'
            elif change == 'bad_sheet_type':
                payload['source_scopes'][sid]['sheet_name'] = None
            elif change == 'extra_scope_field':
                payload['source_scopes'][sid]['invented'] = 'metadata'
            else:
                payload['source_scopes'] = []
            with self.subTest(change=change), self.assertRaises(ValueError):
                final_audit.validate_workflow_group_audit_payload(context, payload)

    def test_scope_payload_rejects_changed_group_requirements_and_documents(self):
        _, _, context = self.scope_context()
        for key in ('groups', 'requirements', 'documents', 'other_claims'):
            payload = final_audit.workflow_group_audit_payload(context)
            if key == 'documents':
                payload[key]['D1'] = '偽の場所.xlsx'
            elif key == 'other_claims':
                payload[key].append({'claim_id': 'invented'})
            else:
                payload[key] = []
            with self.subTest(key=key), self.assertRaises(ValueError):
                final_audit.validate_workflow_group_audit_payload(context, payload)

    def test_scope_compaction_transport_keeps_formal_context_and_caps(self):
        record, packets, _, context = self.context()
        before = copy.deepcopy(context)
        observed, performance, request = self.call_group_audit(record, packets, context, self.result(context))
        prompt = request['messages'][1]['content']
        serialized = prompt.split('<UNTRUSTED_WORKFLOW_DATA>\n', 1)[1].split('\n</UNTRUSTED_WORKFLOW_DATA>', 1)[0]
        payload = json.loads(serialized)
        self.assertIn('source_scopes', payload)
        self.assertNotIn('source_bindings', payload)
        self.assertEqual(self.restore_scope_sources(payload), before['sources'])
        self.assertEqual(payload['groups'], before['groups'])
        self.assertEqual(context, before)
        self.assertLessEqual(len(serialized), 12000)
        self.assertEqual(request['options']['num_ctx'], 8192)
        self.assertEqual(request['options']['num_predict'], 2000)
        self.assertEqual(observed['verdict'], 'verified')
        self.assertNotIn('failed', performance)

    def test_scope_compaction_does_not_hide_oversized_original_or_locator(self):
        record, packets, _, original = self.context()
        for field in ('text', 'locator'):
            context = copy.deepcopy(original)
            if field == 'text':
                context['sources'][-1]['text'] = 'x' * 12001
            else:
                context['sources'][-1]['locator']['native_extra'] = 'x' * 12001
            before = copy.deepcopy(context)
            with mock.patch.object(final_audit.LOCAL_HTTP_OPENER, 'open') as opened:
                result, performance = final_audit.audit_fail_closed('test-model', record['query'],
                    record['answer'], packets, 180, {'workflow_groups': context})
            opened.assert_not_called()
            self.assertEqual(context, before)
            self.assertEqual(result['status'], 'incomplete')
            self.assertEqual(result['reason_code'], 'workflow_group_audit_input_characters_limit')
            self.assertTrue(performance['failed'])

    def test_scope_payload_preserves_exact_12000_character_boundary(self):
        record, _, _, context = self.context()
        payload = final_audit.workflow_group_audit_payload(context)
        size = len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')))
        self.assertLess(size, 12000)
        context['sources'][-1]['text'] += 'x' * (12000 - size)
        accepted = final_audit.workflow_group_audit_prompt(record['query'], context)
        data = accepted.split('<UNTRUSTED_WORKFLOW_DATA>\n', 1)[1].split('\n</UNTRUSTED_WORKFLOW_DATA>', 1)[0]
        self.assertEqual(len(data), 12000)
        self.assertEqual(self.restore_scope_sources(json.loads(data)), context['sources'])
        context['sources'][-1]['text'] += 'x'
        with self.assertRaisesRegex(ValueError, 'input_characters_limit'):
            final_audit.workflow_group_audit_prompt(record['query'], context)

    def test_scope_payload_corruption_is_rejected_before_transport(self):
        record, packets, _, context = self.context()
        payload = final_audit.workflow_group_audit_payload(context)
        payload['sources'][0]['source_scope'] = 'missing'
        with mock.patch.object(final_audit, 'workflow_group_audit_payload', return_value=payload), \
             mock.patch.object(final_audit.LOCAL_HTTP_OPENER, 'open') as opened:
            result, performance = final_audit.audit_fail_closed('test-model', record['query'],
                record['answer'], packets, 180, {'workflow_groups': context})
        opened.assert_not_called()
        self.assertEqual(result['status'], 'incomplete')
        self.assertTrue(performance['failed'])

    def test_input_over_budget_fails_before_model_call(self):
        record, packets, _, context = self.context()
        context['sources'][0]['locator'] = {'oversized': 'x' * 12001}
        with mock.patch.object(final_audit.LOCAL_HTTP_OPENER, 'open') as opened:
            result, performance = final_audit.audit_fail_closed('test-model', record['query'], record['answer'],
                                                               packets, 180, {'workflow_groups': context})
        opened.assert_not_called()
        self.assertTrue(performance['failed'])
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['reason_code'], 'workflow_group_audit_input_characters_limit')

    def test_flat_pass_response_is_not_accepted_for_grouped_answer(self):
        record, packets, _, context = self.context()
        raw = {'done': True, 'done_reason': 'stop', 'prompt_eval_count': 100,
               'eval_count': 80, 'message': {'content': json.dumps({
                   'verdict': 'verified', 'reason': '問題なし', 'unsupported_claims': []})}}
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(raw).encode()
        with mock.patch.object(final_audit.LOCAL_HTTP_OPENER, 'open', return_value=response):
            result, performance = final_audit.audit_fail_closed('test-model', record['query'], record['answer'],
                                                               packets, 180, {'workflow_groups': context})
        self.assertEqual(result['reason_code'], 'audit_response_schema_invalid')
        self.assertTrue(performance['failed'])


if __name__ == "__main__":
    unittest.main()
