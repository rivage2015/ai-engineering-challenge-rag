"""Synthetic model-boundary guards; no model, database, network, or sockets."""
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock
from test_question_graph_executor import answer, evidence


class GraphContextCoverageTests(unittest.TestCase):
    def field(self, count=2, oversized=False, graph=True):
        records = [evidence(f'ev_{i}', ('x'*1801 if oversized and i == 1 else f'手順{i}')) for i in range(count)]
        context, packet_ids = answer.compact_context(records)
        return {'item': {'item_id': 'F1', 'required_claim': '受付手順'},
            'retrieved': records, 'context': context, 'packet_ids': packet_ids,
            'graph_primary_evidence_ids': [x['evidence_id'] for x in records] if graph else []}

    def test_batch_rejects_missing_later_primary_even_if_first_is_present(self):
        field = self.field()
        with self.assertRaises(ValueError):
            answer.require_batch_primary_coverage([field], {'E1': 'ev_0'})

    def test_incomplete_normal_and_retry_never_reach_model(self):
        field = self.field(oversized=True)
        with mock.patch.object(answer, 'audit_field', return_value={'verdict': 'supported'}) as model:
            result = answer.audit_field_safely('unused', field, 1)
        self.assertEqual(result['verdict'], 'insufficient')
        self.assertEqual(result['supported_value'], '')
        self.assertEqual(result['supporting_packet_ids'], [])
        self.assertIn('graph_context_missing_primary_evidence', result['defect'])
        model.assert_not_called()

    def test_retry_must_not_drop_third_selected_evidence(self):
        field = self.field(count=3)
        with mock.patch.object(answer, 'audit_field', side_effect=[RuntimeError('synthetic model failure'), {'verdict':'supported'}]) as model:
            result = answer.audit_field_safely('unused', field, 1)
        self.assertEqual(result['verdict'], 'insufficient')
        self.assertEqual(model.call_count, 1)

    def test_complete_bundle_preserves_success(self):
        field = self.field(count=3)
        expected = {'verdict': 'supported', 'supported_value': '合成の回答'}
        answer.require_batch_primary_coverage([field], field['packet_ids'])
        with mock.patch.object(answer, 'audit_field', return_value=expected) as model:
            self.assertEqual(answer.audit_field_safely('unused', field, 1), expected)
        model.assert_called_once()

    def test_total_budget_omission_never_reaches_model(self):
        field = self.field(count=3)
        for row in field['retrieved']:
            row['text'] = '手' * 1700
        field['context'], field['packet_ids'] = answer.compact_context(field['retrieved'])
        self.assertNotIn('ev_2', field['packet_ids'].values())
        with mock.patch.object(answer, 'audit_field', return_value={'verdict': 'supported'}) as model:
            result = answer.audit_field_safely('unused', field, 1)
        self.assertEqual(result['verdict'], 'insufficient')
        self.assertEqual(result['supporting_packet_ids'], [])
        model.assert_not_called()

    def test_legacy_without_graph_preserves_retry(self):
        field = self.field(count=3, graph=False)
        expected = {'verdict': 'supported'}
        with mock.patch.object(answer, 'audit_field', side_effect=[RuntimeError('first'), expected]) as model:
            self.assertEqual(answer.audit_field_safely('unused', field, 1), expected)
        self.assertEqual(model.call_count, 2)

    def test_batch_fallback_keeps_complete_branch_and_blocks_incomplete_branch(self):
        records = {row['evidence_id']: row for row in self.field(oversized=True)['retrieved']}
        records['ev_complete'] = evidence('ev_complete', '合成の完全項目')
        plan = {'items': [{'item_id': item_id, 'label': item_id,
            'required_claim': item_id, 'retrieval_query': item_id, 'required': True}
            for item_id in ['F1', 'F2']], 'answer_shape': 'two fields'}
        artifact = {'artifact_id': 'qeg_mixed', 'status': 'ready',
            'intent': {'operation': 'record_lookup'}, 'selected_evidence_ids': list(records),
            'branches': [
                {'item_id': 'F1', 'branch_id': 'B1', 'selected_evidence_ids': ['ev_0', 'ev_1']},
                {'item_id': 'F2', 'branch_id': 'B2', 'selected_evidence_ids': ['ev_complete'],
                 'value': '合成の完全項目', 'stored_graph_binding': {
                     'structured_record_lookup_lineage': {'field': {'value_evidence_id': 'ev_complete'}}}},
            ]}
        metadata = {'model': 'unused', 'evidence_sha256': '1'*64, 'graph_sha256': '2'*64,
            'graph_security_partition_sha256': '3'*64,
            'graph_retrievable_evidence_set_sha256': '4'*64, 'graph_embeddings_sha256': '5'*64}

        def complete_only(_model, item, context, packet_ids, _timeout):
            self.assertEqual(item['item_id'], 'F2')
            self.assertEqual(set(packet_ids.values()), {'ev_complete'})
            self.assertIn('合成の完全項目', context)
            return {'item_id': 'F2', 'verdict': 'supported', 'supported_value': '合成の完全項目',
                'supporting_packet_ids': ['ev_complete'], 'competing_packet_ids': [],
                'reason_code': 'none', 'defect': '', 'missing_information': []}

        with mock.patch.object(sys, 'argv', [answer.__file__, '受付の手順は？',
                '--index', answer.__file__, '--json', '--audit-mode', 'batched']), mock.patch.object(
                answer, 'index_metadata', return_value=metadata), mock.patch.object(
                answer, 'plan_question', return_value=plan), mock.patch.object(
                answer, 'load_index_evidence_graph', return_value=(list(records.values()), records, {})), mock.patch.object(
                answer.question_graph, 'build_question_evidence_graph', return_value=artifact), mock.patch.object(
                answer.question_graph, 'validate_question_evidence_graph', return_value={'status': 'pass'}), mock.patch.object(
                answer, 'retrieve_hybrid', return_value=(metadata, [])), mock.patch.object(
                answer, 'audit_field', side_effect=complete_only) as model, mock.patch.object(
                answer.base, 'post_json', side_effect=AssertionError('partial batch must not reach model')) as batch_model, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(answer.main(), 0)
        result = json.loads(output.getvalue())
        first, second = [row['audit'] for row in result['field_runs']]
        self.assertEqual(first['verdict'], 'insufficient')
        self.assertEqual(first['supported_value'], '')
        self.assertEqual(first['supporting_packet_ids'], [])
        self.assertIn('graph_context_missing_primary_evidence', first['defect'])
        self.assertEqual(second['verdict'], 'supported')
        self.assertEqual(second['supporting_packet_ids'], ['ev_complete'])
        model.assert_called_once()
        batch_model.assert_not_called()

    def test_main_all_modes_block_incomplete_graph_context(self):
        field = self.field(oversized=True)
        plan = {'items': [{**field['item'], 'label':'受付', 'retrieval_query':'受付', 'required':True}], 'answer_shape':'single_value'}
        records = {r['evidence_id']:r for r in field['retrieved']}
        artifact = {'artifact_id':'qeg_synthetic', 'status':'ready',
            'intent':{'operation':'ordered_section_lookup'},
            'selected_evidence_ids':list(records)}
        metadata = {'model':'unused', 'evidence_sha256':'1'*64, 'graph_sha256':'2'*64,
            'graph_security_partition_sha256':'3'*64, 'graph_retrievable_evidence_set_sha256':'4'*64,
            'graph_embeddings_sha256':'5'*64}
        for mode in ['sequential', 'parallel', 'batched']:
            with self.subTest(mode=mode), mock.patch.object(sys, 'argv', [answer.__file__,
                    '受付の最初の声がけは？', '--index', answer.__file__, '--json', '--audit-mode', mode]), mock.patch.object(
                    answer, 'index_metadata', return_value=metadata), mock.patch.object(
                    answer, 'plan_question', return_value=plan), mock.patch.object(
                    answer, 'load_index_evidence_graph', return_value=(list(records.values()), records, {})), mock.patch.object(
                    answer.question_graph, 'build_question_evidence_graph', return_value=artifact), mock.patch.object(
                    answer.question_graph, 'validate_question_evidence_graph', return_value={'status':'pass'}), mock.patch.object(
                    answer, 'retrieve_hybrid', return_value=(metadata, [])), mock.patch.object(
                    answer, 'audit_field', side_effect=AssertionError('partial evidence must not reach model')) as model, mock.patch.object(
                    answer.base, 'post_json', side_effect=AssertionError('partial batch must not reach model')) as batch_model, redirect_stdout(io.StringIO()) as output:
                self.assertEqual(answer.main(), 0)
                result = json.loads(output.getvalue())
                self.assertEqual(result['field_runs'][0]['audit']['verdict'], 'insufficient')
                model.assert_not_called()
                batch_model.assert_not_called()


if __name__ == '__main__':
    unittest.main()
