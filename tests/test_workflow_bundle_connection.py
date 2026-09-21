"""Synthetic source-bundle regression; no private data, network, or model."""
import copy
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

from test_question_graph_executor import answer
from test_ordered_section_question_graph import source_graph


QUESTION = '施設の窓口対応のセリフとその後の手順を教えてください'


def record(eid, locator, text, sheet='窓口対応スク', doc='doc_demo', path='demo2023.xlsx'):
    return {'evidence_id': eid, 'document_id': doc, 'relative_path': path,
            'locator': {'sheet_name': sheet, **locator}, 'text': json.dumps(text, ensure_ascii=False)}


def fixture():
    return [
        record('title', {'cell': 'A1'}, '窓口対応スクリプト'),
        record('header', {'row_index': 2}, 'A: 手順\nB: 担当\nC: セリフ\nD: 参考'),
        record('heading', {'row_index': 5}, 'A: 1. ご案内'),
        record('actor', {'cell': 'B6'}, '窓口担当'),
        record('speech', {'cell': 'C6'}, '合成例：ようこそ。'),
        record('reference', {'cell': 'D6'}, '予約票がない場合は確認担当へ引き継ぐ。'),
        record('row6', {'row_index': 6}, 'B: 窓口担当\nC: 合成例：ようこそ。\nD: 予約票がない場合は確認担当へ引き継ぐ。'),
        record('later', {'cell': 'C7'}, '確認前には入室を案内しない。'),
    ]


def metadata():
    return {'index_purpose': 'safe_answer', 'document_version_graph': {
        'graph_sha256': 'a' * 64, 'decision_authority': {'mode': 'snapshot', 'sha256': 'b' * 64}},
        'model': 'unused', 'evidence_sha256': '1' * 64, 'graph_sha256': '2' * 64,
        'graph_security_partition_sha256': '3' * 64,
        'graph_retrievable_evidence_set_sha256': '4' * 64, 'graph_embeddings_sha256': '5' * 64}


class WorkflowBundleTests(unittest.TestCase):
    def bundle(self, records=None, query=QUESTION, graph=None, meta=None):
        records = fixture() if records is None else records
        return answer.build_workflow_source_bundle(query, records,
            source_graph(records) if graph is None else graph, metadata() if meta is None else meta)

    def test_no_year_finds_native_table_with_all_roles_without_top_k_seed(self):
        rows, trace = self.bundle()
        self.assertEqual(trace['status'], 'ready')
        self.assertEqual(trace['scope']['sheet_name'], '窓口対応スク')
        self.assertEqual(trace['cell_coverage']['reference'], 'row6')
        self.assertEqual(trace['cell_coverage']['actor'], 'row6')
        self.assertEqual(trace['cell_coverage']['later'], 'later')
        self.assertIn('header', trace['evidence_ids'])
        context, ids = answer.compact_context(rows)
        self.assertIn('確認担当', context)
        self.assertEqual(set(ids.values()), set(trace['evidence_ids']))
        self.assertEqual(trace['coverage'], 'unknown')
        self.assertEqual(trace['order_kind'], 'source_order')
        self.assertTrue(trace['stored_graph_binding'])

    def test_input_order_and_values_are_not_changed(self):
        rows = fixture(); before = copy.deepcopy(rows)
        graph = source_graph(rows)
        self.assertEqual(self.bundle(rows, graph=graph), self.bundle(list(reversed(rows)), graph=graph))
        self.assertEqual(rows, before)

    def test_different_business_names_and_no_numbered_steps(self):
        rows = [r for r in fixture() if r['evidence_id'] != 'heading']
        for r in rows:
            r['locator']['sheet_name'] = '申請手続きスク'
            r['text'] = r['text'].replace('窓口対応', '申請手続き')
        selected, trace = self.bundle(rows, query='申請手続きの流れを教えて')
        self.assertEqual(trace['status'], 'ready')
        self.assertIn('later', trace['evidence_ids'])

    def test_title_finds_generic_sheet_name(self):
        rows = fixture()
        for r in rows:
            r['locator']['sheet_name'] = 'Sheet1'
        self.assertEqual(self.bundle(rows)[1]['status'], 'ready')

    def test_simple_question_does_not_change_retrieval(self):
        self.assertEqual(self.bundle(query='窓口担当は誰？')[1]['status'], 'not_applicable')

    def test_unbound_index_does_not_gain_yearless_route(self):
        self.assertEqual(self.bundle(meta={})[1]['status'], 'unsupported')

    def test_explicit_year_is_not_ignored(self):
        self.assertEqual(self.bundle(query='2023年の窓口対応の手順')[1]['status'], 'ready')
        self.assertEqual(self.bundle(query='2025年の窓口対応の手順')[0], [])
        self.assertEqual(self.bundle(query='２０２６年度の窓口対応の手順')[1]['status'], 'hold')
        self.assertEqual(self.bundle(query='2025年の窓口対応の手順')[1]['status'], 'hold')

    def test_multiple_years_and_documents_are_not_guessed(self):
        self.assertEqual(self.bundle(query='2023年と2025年の窓口対応の手順')[1]['status'], 'hold')
        rows = fixture()
        other = copy.deepcopy(rows)
        for r in other:
            r['document_id'] = 'doc_other'; r['evidence_id'] += '_other'
            r['relative_path'] = 'demo2025.xlsx'
        self.assertEqual(self.bundle(rows + other)[1]['status'], 'hold')
        self.assertEqual(self.bundle(rows + other, query='2023年の窓口対応の手順')[1]['status'], 'ready')

    def test_longer_facility_title_cannot_override_business_table(self):
        rows = fixture() + [record('intro', {'cell': 'A1'}, '施設の紹介', sheet='架空の展示施設')]
        selected, trace = self.bundle(rows, query='架空の展示施設の窓口対応のセリフとその後の手順')
        self.assertEqual(selected, [])
        self.assertEqual(trace['reason'], 'workflow_source_ambiguous_requires_confirmation')

    def test_english_table_is_not_mixed(self):
        rows = fixture(); other = copy.deepcopy(rows)
        for r in other:
            r['locator']['sheet_name'] += '(英語)'; r['evidence_id'] += '_en'
        selected, _ = self.bundle(rows + other)
        self.assertTrue(all(not r['evidence_id'].endswith('_en') for r in selected))
        selected, _ = self.bundle(rows + other, query='英語の窓口対応の手順')
        self.assertTrue(selected)
        self.assertTrue(all(r['evidence_id'].endswith('_en') for r in selected))

    def test_sensitive_mixed_row_falls_back_to_safe_native_cells(self):
        rows = fixture()
        rows.append(record('secret', {'cell': 'E6'}, 'PASS: not-a-real-secret'))
        row = next(r for r in rows if r['evidence_id'] == 'row6')
        row['text'] = json.dumps(json.loads(row['text']) + '\nE: PASS: not-a-real-secret')
        selected, trace = self.bundle(rows)
        self.assertEqual(trace['status'], 'ready')
        self.assertNotIn('secret', trace['evidence_ids'])
        self.assertNotIn('row6', trace['evidence_ids'])
        self.assertIn('speech', trace['evidence_ids'])
        self.assertIn('reference', trace['evidence_ids'])
        context, _ = answer.compact_context(selected)
        self.assertNotIn('not-a-real-secret', context)
        blocked_ids = {x['evidence_id'] for x in trace['excluded_evidence']}
        merged = answer.merge_workflow_bundle(selected, [row], blocked_ids)
        context, _ = answer.compact_context(merged)
        self.assertNotIn('not-a-real-secret', context)

    def test_cell_only_order_uses_excel_column_numbers(self):
        rows = [record('b', {'cell': 'B6'}, '手順'), record('aa', {'cell': 'AA6'}, '参考')]
        selected, trace = self.bundle(rows)
        self.assertEqual(trace['status'], 'ready')
        self.assertEqual([r['evidence_id'] for r in selected], ['b', 'aa'])

    def test_incomplete_row_summary_cannot_hide_reference_cell(self):
        rows = fixture()
        next(r for r in rows if r['evidence_id'] == 'row6')['text'] = json.dumps('C: 合成例：ようこそ。')
        selected, trace = self.bundle(rows)
        self.assertEqual(trace['status'], 'ready')
        self.assertIn('reference', trace['evidence_ids'])
        self.assertIn('actor', trace['evidence_ids'])

    def test_provisional_data_holds(self):
        rows = fixture()
        rows.append(record('provisional', {'cell': 'C8'}, '[暫定読取] 次の指示'))
        self.assertEqual(self.bundle(rows)[1]['reason'], 'workflow_provisional_source')

    def test_missing_graph_path_does_not_bypass(self):
        rows = fixture(); graph = source_graph(rows); graph['edges'] = []
        self.assertEqual(self.bundle(rows, graph=graph)[1]['reason'], 'workflow_source_path_missing')

    def test_graph_eligibility_mismatch_holds(self):
        rows = fixture(); graph = source_graph(rows); graph['eligible_evidence_ids'].remove('later')
        self.assertEqual(self.bundle(rows, graph=graph)[1]['reason'], 'workflow_source_graph_invalid')

    def test_large_packet_or_total_budget_holds_without_partial_delivery(self):
        for count, length in [(1, 1801), (4, 1600)]:
            with self.subTest(count=count):
                rows = fixture() + [record('large' + str(n), {'cell': f'C{10+n}'}, '長'*length) for n in range(count)]
                selected, trace = self.bundle(rows)
                self.assertEqual(selected, [])
                self.assertEqual(trace['reason'], 'workflow_context_outside_budget')
                self.assertTrue(trace['omitted_evidence_ids'])

    def test_duplicate_locator_and_row_budget_hold(self):
        rows = fixture() + [record('duplicate', {'cell': 'C7'}, '競合')]
        self.assertEqual(self.bundle(rows)[1]['reason'], 'workflow_duplicate_cell_locator')
        rows = fixture() + [record('n'+str(n), {'cell': f'C{20+n}'}, '一行') for n in range(41)]
        self.assertEqual(self.bundle(rows)[1]['reason'], 'workflow_row_budget_exceeded')

    def test_invalid_excel_positions_cannot_be_forwarded(self):
        for locator in [{'cell': 'XFE6'}, {'cell': 'A1048577'}, {'cell': 'A0'},
                        {'cell': 'A1', 'row_index': True}]:
            with self.subTest(locator=locator):
                self.assertEqual(self.bundle(fixture() + [record('bad', locator, '合成値')])[1]['reason'],
                                 'workflow_cell_locator_invalid')

    def test_scoped_metadata_does_not_alias_different_documents(self):
        rows, _ = self.bundle()
        other = copy.deepcopy(rows)
        for r in other:
            r['document_id'] = 'other'; r['relative_path'] = 'other2023.xlsx'
            r['evidence_id'] += '_other'
        context, mapping = answer.compact_context(rows + other)
        self.assertIn('[SOURCE S1]', context)
        self.assertIn('[SOURCE S2]', context)
        self.assertIn('source_scope=S2', context)
        self.assertEqual(len(mapping), len(rows) + len(other))

    def test_normal_candidates_are_kept_after_bundle(self):
        rows, _ = self.bundle()
        ordinary = record('ordinary', {'cell': 'B3'}, '別の説明', sheet='資料説明')
        merged = answer.merge_workflow_bundle(rows, [rows[0], ordinary])
        self.assertEqual(len(merged), len(rows) + 1)
        self.assertEqual(merged[-1]['evidence_id'], 'ordinary')


class WorkflowBundleMainTests(unittest.TestCase):
    def run_main(self, mode, records=None, extra_args=()):
        records = fixture() if records is None else records
        plan = {'items': [{'item_id': 'F1', 'label': '手順', 'required_claim': QUESTION,
                          'retrieval_query': '窓口対応 手順', 'required': True}], 'answer_shape': '手順'}
        # Standard top-k deliberately misses the correct table altogether.
        irrelevant = record('unrelated', {'cell': 'A1'}, '建物の紹介', sheet='外観写真')
        irrelevant.update(score=0.8, rerank_score=0.8, document_support_bonus=0,
                          semantic_score=0.8, lexical_score=0, token_score=0)
        all_records = records + [irrelevant]
        by_id = {r['evidence_id']: r for r in all_records}
        calls = []

        def llm(_url, payload, _timeout):
            # Capture exactly what crosses the model boundary, not just candidates.
            calls.append(payload['messages'][-1]['content'])
            audit = {'item_id': 'F1', 'verdict': 'insufficient', 'supported_value': '',
                     'supporting_packet_ids': [], 'competing_packet_ids': [],
                     'reason_code': 'coverage_unknown', 'defect': '合成モデルは意味の合否を判定しない',
                     'missing_information': ['実モデルでの意味確認']}
            data = {'audits': [audit]} if 'audits' in payload['format']['properties'] else audit
            return {'message': {'content': json.dumps(data)}}

        with mock.patch.object(sys, 'argv', [answer.__file__, QUESTION, '--index', answer.__file__,
                '--audit-mode', mode, '--json', *extra_args]), mock.patch.object(
                answer, 'index_metadata', return_value=metadata()), mock.patch.object(
                answer, 'plan_question', return_value=plan), mock.patch.object(
                answer, 'load_index_evidence_graph', return_value=(all_records, by_id, source_graph(all_records))), mock.patch.object(
                answer, 'retrieve_hybrid', return_value=(metadata(), [irrelevant])), mock.patch.object(
                answer.base, 'post_json', side_effect=llm), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(answer.main(), 0)
        return json.loads(output.getvalue()), calls

    def test_all_modes_deliver_full_bundle_even_with_no_correct_top_k(self):
        for mode in ['batched', 'parallel', 'sequential']:
            with self.subTest(mode=mode):
                result, calls = self.run_main(mode)
                bundle = result['workflow_source_bundle']
                self.assertEqual(bundle['status'], 'ready')
                self.assertTrue(calls)
                self.assertIn('確認担当', calls[0])
                self.assertIn('確認前には入室', calls[0])
                attempts = result['field_runs'][0]['model_context_attempts']
                self.assertEqual(len(attempts), 1)
                self.assertTrue(set(bundle['evidence_ids']) <= set(attempts[0]['evidence_ids']))
                self.assertIn('unrelated', result['field_runs'][0]['retrieved_evidence_ids'])

    def test_off_flag_keeps_legacy_for_comparison(self):
        result, calls = self.run_main('batched', extra_args=['--no-workflow-bundle'])
        self.assertEqual(result['workflow_source_bundle']['status'], 'disabled')
        self.assertNotIn('確認担当', calls[0])

    def test_ambiguous_bundle_is_not_bypassed_by_normal_retrieval(self):
        rows = fixture(); other = copy.deepcopy(rows)
        for r in other:
            r['document_id'] = 'other'; r['evidence_id'] += '_other'
        for mode in ['batched', 'parallel', 'sequential']:
            with self.subTest(mode=mode):
                result, calls = self.run_main(mode, rows + other)
                self.assertEqual(calls, [])
                self.assertEqual(result['workflow_source_bundle']['status'], 'hold')
                self.assertEqual(result['answer']['answer_mode'], 'insufficient')
                self.assertEqual(result['field_runs'][0]['audit']['reason_code'], 'version_or_time_ambiguity')


if __name__ == '__main__':
    unittest.main()
