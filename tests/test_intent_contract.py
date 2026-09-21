"""Confirmation gates and fail-closed answer completeness checks."""
import sys
import json
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'distribution/macos-local-memory/app'))
import intent_contract as ic
import test_dated_hitl_http_e2e as http_tests


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.contract = ic.make_contract('受付の最初の声がけは？', '受付の一連の手順',
            '開始時の声がけと行動\n確認する事項と質問する順序\n条件分岐と次の行動\n対応の完了条件', {'generation': 'one'})

    def test_generic_drafts_preserve_question_without_service_slots(self):
        for query in ['装置の再起動までの手順は？', '2023年と2025年の費用の違いは？',
                      '受付で予約がない場合の流れは？', 'この用語の意味は？']:
            for scope in ['workflow', 'fact', 'custom']:
                with self.subTest(query=query, scope=scope):
                    goal, requirements = ic.draft_intent(query, scope)
                    self.assertIn(query, goal)
                    for forbidden in ['声がけ', '案内', '対応完了', '業務手順']:
                        self.assertNotIn(forbidden, requirements)
                    contract = ic.make_contract(query, goal, requirements, {'generation': 'one'})
                    self.assertIn(query, ic.search_question(contract))

    def test_workflow_draft_does_not_expand_requested_scope(self):
        goal, _ = ic.draft_intent('起動前の点検だけ', 'workflow')
        self.assertIn('その外まで広げない', goal)
        self.assertNotIn('開始から対応完了まで', goal)

    def test_tamper_wrong_key_expiry_and_source_change_rejected(self):
        payload, sig = ic.seal(self.contract, 'server-secret')
        self.assertEqual(ic.verify(payload, sig, 'server-secret', self.contract['revision']), self.contract)
        for p, s, key, rev in [(payload + ' ', sig, 'server-secret', self.contract['revision']),
                (payload, sig, 'browser-csrf', self.contract['revision']),
                (payload, sig, 'server-secret', {'generation': 'two'})]:
            with self.subTest(key=key, revision=rev), self.assertRaises(ValueError):
                ic.verify(p, s, key, rev)
        with mock.patch.object(ic.time, 'time', return_value=self.contract['expires_at'] + 1):
            with self.assertRaises(ValueError):
                ic.verify(payload, sig, 'server-secret', self.contract['revision'])

    def test_search_uses_confirmed_goal_not_original_first_greeting(self):
        question = ic.search_question(self.contract)
        self.assertIn('受付の一連の手順', question)
        self.assertIn('条件分岐', question)
        self.assertNotIn('最初の声がけは？', question)

    def test_greeting_only_is_incomplete(self):
        result = ic.check_coverage(self.contract, 'こんにちは', {'items': [
            {'index': 0, 'covered': True, 'quote': 'こんにちは'},
            {'index': 1, 'covered': False, 'quote': ''}]})
        self.assertFalse(result['complete'])
        self.assertEqual([x['covered'] for x in result['items']], [True, False, False, False])

    def test_malformed_and_invented_quotes_fail_closed(self):
        for verdict in [None, [], {'items': None}, {'items': 3}, {'items': {}},
                {'items': [{'index': 0, 'covered': True, 'quote': 'invented'}]},
                {'items': [{'index': 0, 'covered': True, 'quote': 'こんにちは'}] * 2}]:
            with self.subTest(verdict=verdict):
                self.assertFalse(ic.check_coverage(self.contract, 'こんにちは', verdict)['complete'])

    def test_all_exact_quotes_can_complete(self):
        verdict = {'items': [{'index': i, 'covered': True, 'quote': f'内容{i}'} for i in range(4)]}
        self.assertTrue(ic.check_coverage(self.contract, '内容0 内容1 内容2 内容3', verdict)['complete'])

    def test_coverage_prompt_includes_original_question_and_reviewed_goal(self):
        prompt = ic.coverage_prompt(self.contract, 'こんにちは')
        data = json.loads(prompt.split('\n', 1)[1])
        self.assertEqual(data['question'], self.contract['question'])
        self.assertEqual(data['goal'], self.contract['goal'])
        self.assertEqual(data['requirements'], self.contract['requirements'])
        self.assertEqual(data['answer'], 'こんにちは')

    def test_short_fact_answer_can_complete_without_workflow(self):
        contract = ic.make_contract('会議室の名前は？', '会議室の名前は？',
            '知りたいことに対する、資料で裏付けられる具体的な回答', {})
        result = ic.check_coverage(contract, '青空です。', {
            'items': [{'index': 0, 'covered': True, 'quote': '青空'}]})
        self.assertTrue(result['complete'])
        self.assertEqual(result['status'], 'complete')

    def test_semantic_missing_is_distinct_from_malformed_verdict(self):
        verdict = {'items': [{'index': i, 'covered': False, 'quote': '',
            'reason': '必要な手順がない'} for i in range(4)]}
        missing = ic.check_coverage(self.contract, 'こんにちは', verdict)
        unavailable = ic.check_coverage(self.contract, 'こんにちは', {'items': []})
        self.assertEqual(missing['status'], 'incomplete')
        self.assertEqual(unavailable['status'], 'unavailable')
        self.assertIn('不足する内容', ic.coverage_heading(missing))
        self.assertNotIn('不足する内容', ic.coverage_heading(unavailable))

    def test_invalid_indices_and_nonboolean_verdict_cannot_complete(self):
        contract = ic.make_contract('部屋名は？', '部屋名', '名称', {})
        for entries in [
            [{'index': False, 'covered': True, 'quote': '青空'}],
            [{'index': 0, 'covered': 1, 'quote': '青空'}],
            [{'index': 0, 'covered': True, 'quote': '青空'},
             {'index': 1, 'covered': True, 'quote': '青空'}],
        ]:
            with self.subTest(entries=entries):
                result = ic.check_coverage(contract, '青空', {'items': entries})
                self.assertFalse(result['complete'])
                self.assertEqual(result['status'], 'unavailable')


class IntentHttpTests(unittest.TestCase):
    setUpClass = classmethod(http_tests.DatedHitlHttpE2ETests.setUpClass.__func__)
    setUp = http_tests.DatedHitlHttpE2ETests.setUp
    tearDown = http_tests.DatedHitlHttpE2ETests.tearDown
    post = http_tests.DatedHitlHttpE2ETests.post
    def test_workflow_proposal_preserves_specific_original_scope(self):
        module = self.server_module
        query = '分身ロボットカフェの受付で、予約がないお客様への確認までの流れは？'
        status, body = self.post('/intent-scope', {
            module.UI_CSRF_FIELD: self.httpd.ui_csrf_token, 'query': query, 'scope': 'workflow'})
        self.assertEqual(status, 200)
        self.assertIn('元の質問：' + query, body)
        self.assertNotIn('案内が完了するまで', body)

    def test_invalid_requirements_preserve_draft_without_search(self):
        module = self.server_module
        goal = '受付で予約がない場合 <確認>'
        requirements = '\n'.join('項目' + str(i) for i in range(9))
        with mock.patch.object(module.bootstrap, 'active_answer_revision_identity',
                return_value=(True, 'current', {'generation': 'one'})), mock.patch.object(module, 'answer_query') as answer:
            status, body = self.post('/intent-preview', {
                module.UI_CSRF_FIELD: self.httpd.ui_csrf_token, 'query': '受付は？',
                'goal': goal, 'requirements': requirements})
            self.assertEqual(status, 400)
            self.assertIn('入力内容は保持しています', body)
            self.assertIn('受付で予約がない場合 &lt;確認&gt;', body)
            self.assertIn(requirements, body)
            self.assertIn('action="/intent-preview"', body)
            answer.assert_not_called()

    def test_unavailable_revision_preserves_draft_without_search(self):
        module = self.server_module
        with mock.patch.object(module.bootstrap, 'active_answer_revision_identity',
                return_value=(False, None, None)), mock.patch.object(module, 'answer_query') as answer:
            status, body = self.post('/intent-preview', {
                module.UI_CSRF_FIELD: self.httpd.ui_csrf_token, 'query': '受付は？',
                'goal': '予約なしの受付手順', 'requirements': '確認\n担当者への引き継ぎ'})
            self.assertEqual(status, 409)
            self.assertIn('予約なしの受付手順', body)
            self.assertIn('確認\n担当者への引き継ぎ', body)
            self.assertIn('入力内容は保持しています', body)
            answer.assert_not_called()

    def test_review_steps_never_start_search(self):
        module = self.server_module
        fields = {module.UI_CSRF_FIELD: self.httpd.ui_csrf_token, 'query': '受付の最初の声がけは？'}
        with mock.patch.object(module, 'answer_query') as answer, mock.patch.object(
                module.bootstrap, 'active_answer_revision_identity', return_value=(True, 'current', {'generation': 'one'})):
            for route, extra, expected in [('/intent-dialog', {}, '一連の流れ'),
                    ('/intent-scope', {'scope': 'workflow'}, '条件分岐'),
                    ('/intent-preview', {'goal': '受付の手順', 'requirements': '順番\n分岐'}, 'この回答を目指して検索します')]:
                status, body = self.post(route, {**fields, **extra})
                self.assertEqual(status, 200)
                self.assertIn(expected, body)
            answer.assert_not_called()

    def test_missing_contract_cannot_start_search(self):
        module = self.server_module
        with mock.patch.object(module, 'state', return_value={'phase': 'ready'}), mock.patch.object(
                module.bootstrap, 'active_answer_revision_identity', return_value=(True, 'current', {'generation': 'one'})), mock.patch.object(module, 'answer_query') as answer:
            status, body = self.post('/local-search-answer', {module.UI_CSRF_FIELD: self.httpd.ui_csrf_token,
                'query': '受付は？', 'intent_action': 'confirm'})
            self.assertEqual(status, 409)
            self.assertIn('知りたい範囲', body)
            answer.assert_not_called()

    def test_edit_keeps_agreement_and_does_not_search(self):
        module = self.server_module
        contract = module.intent_contract.make_contract('受付は？', '受付の業務手順', '順番\n分岐', {'generation': 'one'})
        payload, sig = module.intent_contract.seal(contract, module.intent_contract.SIGNING_KEY)
        with mock.patch.object(module, 'state', return_value={'phase': 'ready'}), mock.patch.object(
                module.bootstrap, 'active_answer_revision_identity', return_value=(True, 'current', contract['revision'])), mock.patch.object(module, 'answer_query') as answer:
            status, body = self.post('/local-search-answer', {module.UI_CSRF_FIELD: self.httpd.ui_csrf_token,
                'query': '受付は？', 'intent_action': 'edit', 'intent_payload': payload, 'intent_signature': sig})
            self.assertEqual(status, 200)
            self.assertIn('受付の業務手順', body)
            self.assertIn('順番\n分岐', body)
            answer.assert_not_called()

    def test_confirm_forwards_agreement_and_marks_partial_answer_incomplete(self):
        module = self.server_module
        contract = module.intent_contract.make_contract('受付の最初は？', '受付の全手順', '声がけ\n条件分岐', {'generation': 'one'})
        payload, sig = module.intent_contract.seal(contract, module.intent_contract.SIGNING_KEY)
        coverage = module.intent_contract.check_coverage(contract, 'こんにちは', {'items': [
            {'index': 0, 'covered': True, 'quote': 'こんにちは'},
            {'index': 1, 'covered': False, 'quote': ''}]})
        with mock.patch.object(module, 'state', return_value={'phase': 'ready'}), mock.patch.object(
                module.bootstrap, 'active_answer_revision_identity', return_value=(True, 'current', contract['revision'])), mock.patch.object(
                module, 'answer_query', return_value={'answer': {'answer': 'こんにちは'}}) as answer, mock.patch.object(
                module, 'audit_intent_coverage', return_value=coverage), mock.patch.object(
                module, 'answer_source_notice', return_value=('出典', '', '')), mock.patch.object(
                module, 'semantic_graph_candidate_notice', return_value=''), mock.patch.object(
                module, 'security_exclusion_notice', return_value=''):
            status, body = self.post('/local-search-answer', {module.UI_CSRF_FIELD: self.httpd.ui_csrf_token,
                'query': contract['question'], 'intent_action': 'confirm', 'intent_payload': payload, 'intent_signature': sig})
            self.assertEqual(status, 200)
            self.assertIn('回答は未完了です', body)
            self.assertIn('確認不足：条件分岐', body)
            self.assertIn('入力内容を保持', body)
            answer.assert_called_once_with(module.intent_contract.search_question(contract), expected_active_revision=contract['revision'])

    def test_http_distinguishes_missing_content_from_missing_verdict(self):
        module = self.server_module
        contract = module.intent_contract.make_contract(
            '受付の仕事は？', '受付の手順', '声がけ\n条件分岐', {'generation': 'one'})
        payload, sig = module.intent_contract.seal(contract, module.intent_contract.SIGNING_KEY)
        first = {'index': 0, 'covered': True, 'quote': 'こんにちは'}
        for items, label, heading in [
            ([first, {'index': 1, 'covered': False, 'quote': ''}],
             '確認不足：条件分岐', '不足する内容があります'),
            ([first], '判定できず：条件分岐', '充足確認を完了できませんでした'),
        ]:
            with self.subTest(label=label):
                coverage = module.intent_contract.check_coverage(contract, 'こんにちは', {'items': items})
                with mock.patch.object(module, 'state', return_value={'phase': 'ready'}), mock.patch.object(
                        module.bootstrap, 'active_answer_revision_identity', return_value=(True, 'current', contract['revision'])), mock.patch.object(
                        module, 'answer_query', return_value={'answer': {'answer': 'こんにちは'}}), mock.patch.object(
                        module, 'audit_intent_coverage', return_value=coverage), mock.patch.object(
                        module, 'answer_source_notice', return_value=('出典', '', '')), mock.patch.object(
                        module, 'semantic_graph_candidate_notice', return_value=''), mock.patch.object(
                        module, 'security_exclusion_notice', return_value=''):
                    status, body = self.post('/local-search-answer', {
                        module.UI_CSRF_FIELD: self.httpd.ui_csrf_token, 'query': contract['question'],
                        'intent_action': 'confirm', 'intent_payload': payload, 'intent_signature': sig})
                self.assertEqual(status, 200)
                self.assertIn(label, body)
                self.assertIn(heading, body)
                self.assertNotIn('合意した内容を確認できました', body)
                if coverage['status'] == 'unavailable':
                    self.assertIn('資料や回答の不足とは断定していません', body)
                    self.assertNotIn('確認不足：条件分岐', body)


if __name__ == '__main__':
    unittest.main()
