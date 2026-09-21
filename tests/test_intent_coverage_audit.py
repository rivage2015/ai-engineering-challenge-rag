"""Completion checks use the approved question; no live models or settings."""
import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from test_dated_hitl_http_e2e import load_server


class IntentCoverageAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_server()

    def setUp(self):
        self.contract = self.module.intent_contract.make_contract(
            '会議室の名前は？', '会議室の名前を知りたい', '具体的な名称', {})
        self.record = {'request_id': 'synthetic-coverage',
            'answer': {'answer': '青空です。', 'answer_mode': 'grounded'},
            'independent_final_audit': {'verdict': 'verified'}}

    @staticmethod
    def response(items):
        return {'done': True, 'done_reason': 'stop', 'prompt_eval_count': 600,
                'eval_count': 70, 'response': json.dumps({'items': items}, ensure_ascii=False)}

    def run_check(self, raw=None, error=None, record=None):
        m = self.module
        record = copy.deepcopy(record or self.record)
        raw = raw if raw is not None else self.response([
            {'index': 0, 'covered': True, 'quote': '青空', 'reason': '名称を回答している'}])
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(raw).encode()
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(mock.patch.object(m.bootstrap, 'SUPPORT', Path(directory)))
            stack.enter_context(mock.patch.object(m.bootstrap, 'load_json', return_value={
                'audit_model': 'gemma4:12b', 'sequential_model_loading': False}))
            call = stack.enter_context(mock.patch.object(m.LOCAL_HTTP_OPENER, 'open',
                return_value=response, side_effect=error))
            result = m.audit_intent_coverage(self.contract, record)
            saved = json.loads((Path(directory) / 'logs/intent-answers.jsonl').read_text())
        return result, saved, call

    def test_prompt_contains_question_goal_and_capacity_is_explicit(self):
        result, saved, call = self.run_check()
        self.assertTrue(result['complete'])
        self.assertEqual(saved['coverage']['status'], 'complete')
        request = json.loads(call.call_args.args[0].data)
        self.assertIn(self.contract['question'], request['prompt'])
        self.assertIn(self.contract['goal'], request['prompt'])
        self.assertFalse(request['think'])
        self.assertEqual(request['options']['num_ctx'], 8192)
        self.assertEqual(request['options']['num_predict'], 1200)
        self.assertEqual(saved['coverage']['diagnostics']['status'], 'observed')
        self.assertEqual(call.call_count, 1)

    def test_valid_missing_verdict_remains_incomplete(self):
        self.contract = self.module.intent_contract.make_contract(
            '受付の仕事の手順は？', '一連の手順と分岐', '手順と分岐', {})
        self.record['answer']['answer'] = 'こんにちは。'
        result, saved, _ = self.run_check(self.response([
            {'index': 0, 'covered': False, 'quote': '', 'reason': '挨拶だけで手順がない'}]))
        self.assertFalse(result['complete'])
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(saved['coverage']['items'][0]['reason'], '挨拶だけで手順がない')

    def test_invalid_quote_duplicate_index_and_missing_item_are_unavailable(self):
        valid = {'index': 0, 'covered': True, 'quote': '青空'}
        for items in [[{**valid, 'quote': '別の名称'}], [valid, valid], [],
                      [{**valid, 'covered': 'true'}], [{**valid, 'index': False}]]:
            with self.subTest(items=items):
                result, _, _ = self.run_check(self.response(items))
                self.assertFalse(result['complete'])
                self.assertEqual(result['status'], 'unavailable')

    def test_truncated_exhausted_or_missing_usage_cannot_complete(self):
        raw = self.response([{'index': 0, 'covered': True, 'quote': '青空'}])
        for change in [{'done_reason': 'length'}, {'prompt_eval_count': 8192},
                       {'eval_count': None}, {'done': False},
                       {'response': '{"items":[]'},
                       {'response': '{"items":[],"items":[]}'}]:
            with self.subTest(change=change):
                result, saved, call = self.run_check({**raw, **change})
                self.assertFalse(result['complete'])
                self.assertEqual(result['status'], 'unavailable')
                self.assertEqual(saved['coverage']['diagnostics']['status'], 'incomplete')
                self.assertEqual(call.call_count, 1)

    def test_transport_failure_is_not_semantic_missing(self):
        result, saved, call = self.run_check(error=TimeoutError('synthetic timeout'))
        self.assertFalse(result['complete'])
        self.assertEqual(saved['coverage']['status'], 'unavailable')
        self.assertNotIn('不足', self.module.intent_contract.coverage_heading(result))
        self.assertEqual(call.call_count, 1)

    def test_evidence_and_policy_gates_cannot_be_overridden(self):
        for change in [
            {'independent_final_audit': {'verdict': 'rejected'}},
            {'answer': {'answer': '青空です。', 'answer_mode': 'insufficient'}},
            {'answerability_policy': {'reference_only': True}},
            {'answerability_policy': {'observations': ['provisional']}},
            {'answerability_policy': {'unresolved_field_ids': ['f1']}},
            {'grounded_guidance': {'status': 'incomplete'}},
        ]:
            with self.subTest(change=change):
                result, _, _ = self.run_check(record={**self.record, **change})
                self.assertFalse(result['complete'])
                self.assertEqual(result['status'], 'blocked')


if __name__ == '__main__':
    unittest.main()
