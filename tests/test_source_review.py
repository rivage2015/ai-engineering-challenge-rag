"""Unresolved-source display uses formal local evidence, not generated quotes."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_dated_hitl_http_e2e import load_server
import test_answerability_ui_logging as ui_tests


def fixture(module):
    metadata = {key: 'a' * 64 for key in module.SOURCE_REVIEW_BINDINGS}
    record = {
        'answer': {'answer': '受付は9時です。引き継ぎの担当は未確認です。',
                   'answer_mode': 'qualified', 'answer_status': 'answered'},
        'independent_final_audit': {'verdict': 'verified'},
        'index': {**metadata, 'path': '/untrusted/model/path.sqlite3'},
        'field_runs': [
            {'item': {'label': '受付時間'}, 'audit': {'verdict': 'supported'}},
            {'item': {'label': '引き継ぎ担当'}, 'retrieved_evidence_ids': ['e1', 'e2'],
             'audit': {'verdict': 'ambiguous', 'reason_code': 'unsupported_relation',
                       'competing_packet_ids': ['e2'], 'defect': '連絡する人が明記されていません。',
                       'supported_value': 'MODEL_FABRICATED_QUOTE'}}],
        'answerability_policy': {'applied': True, 'confirmed_field_ids': ['1'],
                                'unresolved_field_ids': ['2'], 'observations': []},
    }
    packets = [{'evidence_id': 'e2', 'relative_path': '手順2026.xlsx',
                'locator': {'sheet_name': '引継ぎ', 'cell': 'B15'},
                'text': '必要なら次の担当者へ連絡する。'}]
    policy = {'metadata': metadata, 'eligible_evidence_ids': {'e1', 'e2'}}
    return record, packets, policy


class SourceReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_server()

    def prepare(self, record, packets, policy, *, current=True):
        m = self.module
        with mock.patch.object(m.bootstrap, 'load_config_snapshot', return_value=(True, {'index_path': '/trusted/index.sqlite3'})), \
             mock.patch.object(m.bootstrap, 'answer_config_matches_revision', return_value=current), \
             mock.patch.object(m, 'load_source_review_packets', return_value=(packets, policy)) as loader:
            view = m.prepare_source_review(record, {'generation': 'one'})
        return view, loader

    def test_partial_answer_untouched_original_from_formal_index(self):
        r, packets, policy = fixture(self.module)
        before = copy.deepcopy(r)
        view, loader = self.prepare(r, packets, policy)
        self.assertEqual(r, before)
        loader.assert_called_once_with(Path('/trusted/index.sqlite3'), ['e2'])
        body = self.module.source_review_notice(view)
        for text in ('必要なら次の担当者へ連絡する。', 'B15', '引継ぎ', '手順2026.xlsx',
                     '連絡する人が明記されていません', 'モデルの判断メモ', '正しさは未確認'):
            self.assertIn(text, body)
        self.assertNotIn('MODEL_FABRICATED_QUOTE', body)
        self.assertNotIn('受付時間', body)

    def test_complete_answer_hidden_without_reading_index(self):
        r, p, policy = fixture(self.module)
        r['field_runs'] = r['field_runs'][:1]
        view, loader = self.prepare(r, p, policy)
        loader.assert_not_called()
        self.assertEqual(self.module.source_review_notice(view), '')

    def test_final_semantic_rejection_only_uses_diagnostic_candidates(self):
        r, p, policy = fixture(self.module)
        r['field_runs'] = r['field_runs'][:1]
        r['answer'].update(answer='わかりません', answer_status='insufficient',
                           non_answer_reason={'code': 'unsupported_relation'},
                           diagnostic_evidence_ids=['e2', 'invented'])
        r['retrieved'] = [{'evidence_id': 'e2'}]
        r['independent_final_audit']['verdict'] = 'rejected'
        view, loader = self.prepare(r, p, policy)
        self.assertEqual(loader.call_args.args[1], ['e2'])
        body = self.module.source_review_notice(view)
        self.assertIn(p[0]['text'], body)
        self.assertIn('検索候補', body)

    def test_successful_graph_answer_does_not_show_old_failed_route(self):
        r, p, policy = fixture(self.module)
        r[self.module.SEMANTIC_GRAPH_ANSWER_PROMOTION_KEY] = {'decision': 'PROMOTE', 'used_for_answers': True}
        r['answer'].update(answer_status='answered', answer_mode='grounded')
        view, loader = self.prepare(r, p, policy)
        self.assertEqual(view['status'], 'hidden')
        loader.assert_not_called()

    def test_locations_are_readable_without_guessing_missing_cells(self):
        self.assertEqual(self.module.source_review_location({'sheet_name': '引継ぎ', 'cell': 'B15'}),
                         'シート：引継ぎ／セル：B15')
        self.assertEqual(self.module.source_review_location({'sheet_name': '引継ぎ', 'row_index': 15}),
                         'シート：引継ぎ／行：15')

    def test_no_explicit_source_is_labeled_search_candidate(self):
        r, p, policy = fixture(self.module)
        r['field_runs'][1]['audit']['competing_packet_ids'] = ['unknown']
        view, loader = self.prepare(r, p, policy)
        self.assertEqual(loader.call_args.args[1], ['e1', 'e2'])
        body = self.module.source_review_notice(view)
        self.assertIn('問題の一文を特定できていない', body)
        self.assertIn('すべての候補は表示していません', body)

    def test_excluded_ids_and_stale_index_never_leak(self):
        for stale in (False, True):
            r, p, policy = fixture(self.module)
            if stale:
                policy['metadata']['graph_sha256'] = 'b' * 64
            else:
                policy['eligible_evidence_ids'] = set()
            view, _ = self.prepare(r, p, policy)
            self.assertNotIn(p[0]['text'], self.module.source_review_notice(view))

    def test_all_binding_fields_required(self):
        for key in self.module.SOURCE_REVIEW_BINDINGS:
            r, p, policy = fixture(self.module)
            del r['index'][key]
            view, _ = self.prepare(r, p, policy)
            self.assertEqual(view['status'], 'unavailable')

    def test_revision_change_does_not_load_sources(self):
        r, p, policy = fixture(self.module)
        view, loader = self.prepare(r, p, policy, current=False)
        loader.assert_not_called()
        self.assertEqual(view['status'], 'unavailable')

    def test_failures_are_not_called_document_ambiguity(self):
        for mode in ('machine', 'incomplete', 'version', 'validation'):
            r, p, policy = fixture(self.module)
            if mode == 'machine':
                r['field_runs'][1]['audit']['reason_code'] = 'machine_validation_failure'
            elif mode == 'incomplete':
                r['independent_final_audit']['status'] = 'incomplete'
            elif mode == 'version':
                r['registered_version_scope'] = {'status': 'hold'}
            else:
                r['deterministic_claim_validation'] = {'status': 'fail'}
            view, loader = self.prepare(r, p, policy)
            loader.assert_not_called()
            body = self.module.source_review_notice(view)
            self.assertNotIn(p[0]['text'], body)
            self.assertNotIn('連絡する人が明記', body)

    def test_no_matching_evidence_is_explicit(self):
        r, _, policy = fixture(self.module)
        view, _ = self.prepare(r, [], policy)
        self.assertIn('該当原文を特定できませんでした', self.module.source_review_notice(view))

    def test_escaping_and_excerpt_limit(self):
        r, p, policy = fixture(self.module)
        p[0]['text'] = '<script>alert(1)</script>' + '長' * 3000
        p[0]['relative_path'] = '<img src=x>.xlsx'
        r['field_runs'][1]['audit']['defect'] = '<b>偽の判断</b>'
        view, _ = self.prepare(r, p, policy)
        self.assertEqual(len(view['items'][0]['sources'][0]['text']), 2400)
        body = self.module.source_review_notice(view)
        self.assertIn('&lt;script&gt;', body)
        self.assertNotIn('<script>', body)
        self.assertNotIn('<img', body)
        self.assertNotIn('<b>', body)
        self.assertIn('ここまでの抜粋', body)

    def test_field_and_source_limits_explicit(self):
        r, p, policy = fixture(self.module)
        row = r['field_runs'][1]
        row['audit']['competing_packet_ids'] = []
        row['retrieved_evidence_ids'] = ['e2', 'e1', 'e3', 'e4']
        r['field_runs'] = [copy.deepcopy(row) for _ in range(5)]
        view, _ = self.prepare(r, p, policy)
        self.assertEqual(len(view['items']), 3)
        self.assertEqual(view['omitted_items'], 2)
        self.assertEqual(view['items'][0]['omitted_sources'], 1)

    def test_real_read_only_index_loader_excludes_held_evidence(self):
        from test_answer_graph_retrieval_policy import create_ready_index, SAFE_ID, HELD_ID
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'safe.sqlite3'
            create_ready_index(path)
            before = path.read_bytes()
            packets, policy = self.module.load_source_review_packets(path, [SAFE_ID, HELD_ID])
            self.assertEqual([p['evidence_id'] for p in packets], [SAFE_ID])
            self.assertNotIn(HELD_ID, policy['eligible_evidence_ids'])
            self.assertEqual(before, path.read_bytes())


class SourceReviewHttpTests(unittest.TestCase):
    setUpClass = classmethod(ui_tests.AnswerabilityUiLoggingTests.setUpClass.__func__)
    setUp = ui_tests.AnswerabilityUiLoggingTests.setUp
    tearDown = ui_tests.AnswerabilityUiLoggingTests.tearDown
    post = ui_tests.AnswerabilityUiLoggingTests.post
    search = ui_tests.AnswerabilityUiLoggingTests.search

    def test_partial_answer_and_unresolved_source_in_real_handler(self):
        m = self.server_module
        r, p, policy = fixture(m)
        with mock.patch.object(m.bootstrap, 'load_config_snapshot', return_value=(True, {'index_path': '/trusted/index.sqlite3'})), \
             mock.patch.object(m.bootstrap, 'answer_config_matches_revision', return_value=True), \
             mock.patch.object(m, 'load_source_review_packets', return_value=(p, policy)):
            status, body = self.search(r)
        self.assertEqual(status, 200)
        self.assertIn(r['answer']['answer'], body)
        self.assertIn(p[0]['text'], body)
        self.assertIn('B15', body)
        self.assertIn('回答は未完了', body)
        self.assertNotIn('MODEL_FABRICATED_QUOTE', body)

    def test_machine_failure_hides_misleading_add_documents_text(self):
        r, _, _ = fixture(self.server_module)
        r['answer']['answer'] = '資料不足なので書き直してください_SENTINEL'
        r['answer']['non_answer_reason'] = {'code': 'machine_validation_failure'}
        status, body = self.search(r)
        self.assertEqual(status, 200)
        self.assertIn('処理上の問題', body)
        self.assertNotIn('書き直してください_SENTINEL', body)
        self.assertNotIn('合意した内容を確認できました', body)

    def test_verified_partial_answer_survives_failure_in_other_field(self):
        r, _, _ = fixture(self.server_module)
        r['field_runs'][1]['audit']['reason_code'] = 'machine_validation_failure'
        status, body = self.search(r)
        self.assertEqual(status, 200)
        self.assertIn(r['answer']['answer'], body)
        self.assertIn('処理上の問題', body)
        self.assertNotIn('判断できなかった部分を原文で確認', body)

    def test_revision_change_withholds_even_prepared_source(self):
        m = self.server_module
        r, p, policy = fixture(m)
        with mock.patch.object(m.bootstrap, 'load_config_snapshot', return_value=(True, {'index_path': '/trusted/index.sqlite3'})), \
             mock.patch.object(m.bootstrap, 'answer_config_matches_revision', return_value=True), \
             mock.patch.object(m, 'load_source_review_packets', return_value=(p, policy)):
            status, body = self.search(r, revision_changed=True)
        self.assertEqual(status, 409)
        self.assertNotIn(p[0]['text'], body)


if __name__ == '__main__':
    unittest.main()
