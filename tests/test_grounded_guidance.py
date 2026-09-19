"""Synthetic offline checks for the separately audited guidance sidecar."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'tested_grounded_guidance', ROOT / 'distribution/macos-local-memory/app/grounded_guidance.py')
guidance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guidance)


def fixture():
    contract = {'version': 1, 'question': '展示館の工作実演はいつですか。お客様にはどう案内すればよいですか？',
                'goal': '工作実演の曜日・開始時刻とお客様に伝える案内文を知りたい。',
                'requirements': ['工作実演の曜日と開始時刻', 'お客様向けの案内文'],
                'revision': 'synthetic-index', 'expires_at': 123456789}
    metadata = {field: 'hash-' + field for field in guidance.BINDING_FIELDS}
    rows = [
        {'evidence_id': 'E1', 'relative_path': '展示館/案内.txt', 'locator': {'paragraph': 1},
         'text': '工作実演は火曜日と金曜日に開催。開始時刻は11:10、15:40です。'},
        {'evidence_id': 'E2', 'relative_path': '展示館/案内.txt', 'locator': {'paragraph': 2},
         'text': '工作実演をご覧いただけます。時間は○○です。\n館内ライブは日曜日の13:00です。'},
    ]
    audit = {'verdict': 'supported', 'supported_value': '火曜日と金曜日',
             'supporting_packet_ids': ['E1'], 'competing_packet_ids': [], 'reason_code': 'none',
             'item_id': 'F1', 'defect': '', 'missing_information': []}
    record = {
        'query': guidance.intent.search_question(contract), 'index': {'path': '/tmp/synthetic.sqlite', **metadata},
        'question_plan': {'items': [{'item_id': 'F1', 'required_claim': '曜日と開始時刻', 'required': True}]},
        'question_evidence_graph': {'status': 'unsupported', 'intent': {'operation': 'unknown'}},
        'graph_route': {'operation': 'unknown', 'required': False, 'used': False},
        'field_runs': [{'item': {'item_id': 'F1'}, 'audit': audit},
                       {'item': {'item_id': 'F2'}, 'audit': {'item_id': 'F2', 'verdict': 'insufficient',
                                                         'reason_code': 'unsupported_relation'}}],
        'answer': {'answer': '曜日:火曜日と金曜日。関連記載:時間は○○です。', 'answer_status': 'answered',
                   'answer_mode': 'qualified', 'evidence_ids': ['E1'], 'diagnostic_evidence_ids': ['E2']},
        'independent_final_audit': {'verdict': 'verified', 'unsupported_claims': [], 'reason': '抽出と原文が一致'},
        'orchestration_decision': {'status': 'accepted', 'checks': {key: True for key in
            ('answer_graph', 'question_graph', 'graph_retrieval_trace', 'deterministic_claims', 'independent_audit')}},
    }
    policy = {'metadata': metadata, 'eligible_evidence_ids': {'E1', 'E2'}}
    draft = {'facts': [
        {'id': 'F1', 'text': '工作実演は火曜日と金曜日、11:10と15:40に始まります。',
         'quotes': [{'evidence_id': 'E1', 'quote': rows[0]['text']}]},
        {'id': 'F2', 'text': '工作実演をご覧いただけます。',
         'quotes': [{'evidence_id': 'E2', 'quote': '工作実演をご覧いただけます。'}]},
    ], 'guidance': [{'id': 'G1', 'text': '工作実演は火曜日と金曜日の11:10、15:40開始です。実演をご覧いただけます。',
                      'fact_ids': ['F1', 'F2']}], 'unresolved': []}
    review = {'verdict': 'verified', 'reason': '同じ実演の情報と案内が対応しています。',
              'checks': [{'id': item, 'support': 'pass', 'same_subject': 'pass',
                          'conditions': 'pass', 'relevance': 'pass', 'reason': '原文と一致'}
                         for item in ('F1', 'F2', 'G1')],
              'requirements': [
                  {'requirement': contract['requirements'][0], 'status': 'fulfilled',
                   'quote': draft['facts'][0]['text']},
                  {'requirement': contract['requirements'][1], 'status': 'fulfilled',
                   'quote': draft['guidance'][0]['text']},
              ]}
    return record, contract, rows, policy, draft, review


def model_selection(draft):
    """Produce a model-shaped fixture by selecting existing synthetic spans."""
    _, _, rows, _, _, _ = fixture()
    _, mapping = guidance._quote_sources(rows)
    value = copy.deepcopy(draft)
    for fact in value['facts']:
        ids = []
        for quote in fact.pop('quotes'):
            matches = [key for key, part in mapping.items()
                       if part['evidence_id'] == quote['evidence_id']
                       and part['quote'].strip() and quote['quote'].strip()
                       and (part['quote'].strip() in quote['quote'] or quote['quote'] in part['quote'])]
            ids.extend(matches or ['Q99999'])
        fact['quote_ids'] = list(dict.fromkeys(ids))
    return value


def response(value, **metadata):
    if isinstance(value, dict) and value.get('facts') and 'quotes' in value['facts'][0]:
        value = model_selection(value)
    return {'done': True, 'done_reason': 'stop', 'prompt_eval_count': 2300,
            'eval_count': 500, 'message': {'content': json.dumps(value, ensure_ascii=False)}, **metadata}


class GroundedGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.record, self.contract, self.rows, self.policy, self.draft, self.review = fixture()
        self.loader = mock.patch.object(guidance.engine, 'load_answer_evidence_records',
                                        return_value=(self.rows, self.policy))
        self.loader.start()
        self.addCleanup(self.loader.stop)

    def compose(self, responses=None, error=None):
        with mock.patch.object(guidance.engine, 'post_json',
                               side_effect=error or responses or [response(self.draft), response(self.review)]) as calls:
            value = guidance.compose_guidance(self.record, self.contract, Path('/tmp/synthetic.sqlite'), 'local-test', 999)
        return value, calls

    def view(self, artifact):
        return guidance.verified_view({**self.record, 'grounded_guidance': artifact}, self.contract)

    def test_confirmed_extraction_is_forwarded_with_actual_source_references(self):
        artifact, calls = self.compose()
        self.assertEqual(artifact['status'], 'verified')
        payload = calls.call_args_list[0].args[1]
        inputs = json.loads(payload['messages'][1]['content'])
        self.assertEqual(inputs['confirmed_facts'][0]['value'], '火曜日と金曜日')
        self.assertEqual(inputs['confirmed_facts'][0]['quote_ids'], ['Q1'])
        self.assertEqual(len(inputs['confirmed_facts']), 1)
        self.assertEqual(inputs['requirements'], self.contract['requirements'])

    def test_source_ids_cannot_masquerade_as_fact_or_guidance_ids(self):
        for replace in ('fact_id', 'guidance_id', 'binding'):
            selection = model_selection(self.draft)
            if replace == 'fact_id':
                selection['facts'][0]['id'] = 'Q1'
            elif replace == 'guidance_id':
                selection['guidance'][0]['id'] = 'Q1'
            else:
                selection['guidance'][0]['fact_ids'] = ['Q1']
            with self.subTest(replace=replace):
                self.assertFalse(guidance.guard.validate_schema(selection, guidance.GENERATION_SCHEMA))

    def test_two_sources_are_composed_and_reviewed_without_mutating_extraction(self):
        original = copy.deepcopy(self.record)
        artifact, calls = self.compose()
        self.assertEqual(artifact['status'], 'verified')
        self.assertEqual(self.record, original)
        view = self.view(artifact)
        self.assertTrue(view['coverage']['complete'])
        self.assertEqual({x['evidence_id'] for x in view['sources']}, {'E1', 'E2'})
        self.assertIn('原文引用ではありません', view['answer'])
        self.assertNotIn('○○', view['answer'])
        self.assertNotIn('館内ライブ', view['answer'])
        self.assertIn('○○', self.record['answer']['answer'])
        self.assertEqual(calls.call_count, 2)
        for number, call in enumerate(calls.call_args_list):
            self.assertEqual(call.args[0], 'http://127.0.0.1:11434/api/chat')
            self.assertEqual(call.args[2], 120)
            self.assertEqual(call.args[1]['options'], {'temperature': 0, 'num_ctx': 8192,
                                                      'num_predict': 1200 if number == 0 else 900})
            self.assertFalse(call.args[1]['think'])
        self.assertNotEqual(calls.call_args_list[0].args[1]['messages'], calls.call_args_list[1].args[1]['messages'])
        for call in calls.call_args_list:
            model_input = json.loads(call.args[1]['messages'][1]['content'])
            self.assertEqual([''.join(part['text'] for part in packet['ordered_parts'])
                              for packet in model_input['sources']], [row['text'] for row in self.rows])
        schema = calls.call_args_list[0].args[1]['format']
        properties = schema['properties']['facts']['items']['properties']
        self.assertIn('quote_ids', properties)
        self.assertNotIn('quotes', properties)

    def test_gate_requires_explicit_guidance_and_exact_intent_contract(self):
        self.assertTrue(guidance.eligible(self.record, self.contract))
        self.contract['question'] = self.contract['goal'] = '案内資料の表示色は何ですか。'
        self.record['query'] = guidance.intent.search_question(self.contract)
        self.assertFalse(guidance.eligible(self.record, self.contract))
        self.contract['question'] = 'お客様にどう案内すればよいですか。'
        self.assertFalse(guidance.eligible(self.record, self.contract))

    def test_other_graph_operations_and_workflow_are_not_affected(self):
        for operation in ('record_lookup', 'aggregate_count', 'ordered_section_lookup'):
            with self.subTest(operation=operation):
                self.record['graph_route']['operation'] = operation
                artifact, calls = self.compose()
                self.assertEqual(artifact['status'], 'not_applicable')
                calls.assert_not_called()
        self.record['graph_route']['operation'] = 'unknown'
        self.record['workflow_reasoning'] = {'status': 'checked'}
        self.assertFalse(guidance.eligible(self.record, self.contract))

    def test_accepted_audit_and_all_original_gates_are_required(self):
        for key in ('answer_graph', 'question_graph', 'graph_retrieval_trace', 'deterministic_claims', 'independent_audit'):
            with self.subTest(key=key):
                self.record['orchestration_decision']['checks'][key] = False
                self.assertFalse(guidance.eligible(self.record, self.contract))
                self.record['orchestration_decision']['checks'][key] = True
        self.record['independent_final_audit']['status'] = 'incomplete'
        artifact, calls = self.compose()
        self.assertEqual(artifact['status'], 'not_applicable')
        calls.assert_not_called()

    def test_conflict_version_uncertainty_and_machine_failure_are_never_promoted(self):
        for reason in guidance.BLOCKED_REASONS:
            self.record['field_runs'][1]['audit']['reason_code'] = reason
            self.assertFalse(guidance.eligible(self.record, self.contract))

    def test_ambiguous_and_contradicted_current_or_original_audits_block_composition(self):
        for audit_key in ('audit', 'original_audit'):
            for verdict in ('ambiguous', 'contradicted'):
                for reason in ('unsupported_relation', 'coverage_unknown'):
                    with self.subTest(audit_key=audit_key, verdict=verdict, reason=reason):
                        original = copy.deepcopy(self.record)
                        self.record['field_runs'][1][audit_key] = {'verdict': verdict, 'reason_code': reason}
                        artifact, calls = self.compose()
                        self.assertEqual(artifact['status'], 'not_applicable')
                        calls.assert_not_called()
                        self.record = original
        for reason in guidance.BLOCKED_REASONS:
            self.record['field_runs'][1]['original_audit'] = {'verdict': 'insufficient', 'reason_code': reason}
            self.assertFalse(guidance.eligible(self.record, self.contract))

    def test_snapshot_or_path_mismatch_and_ineligible_id_stop_before_model(self):
        for kind in ('hash', 'path', 'id'):
            with self.subTest(kind=kind):
                original = copy.deepcopy(self.record)
                if kind == 'hash':
                    self.record['index']['graph_sha256'] = 'different'
                elif kind == 'path':
                    self.record['index']['path'] = '/tmp/other.sqlite'
                else:
                    self.record['answer']['diagnostic_evidence_ids'].append('invented')
                artifact, calls = self.compose()
                self.assertEqual(artifact['status'], 'incomplete')
                self.assertIsNone(self.view(artifact))
                calls.assert_not_called()
                self.record = original

    def test_provisional_ocr_cannot_be_used_as_citation(self):
        self.rows[1]['text'] = '[暫定読取] ' + self.rows[1]['text']
        artifact, calls = self.compose()
        self.assertEqual(artifact['reason_code'], 'guidance_quote_id_invalid')
        self.assertEqual(calls.call_count, 1)

    def test_empty_oversized_or_unknown_quotes_fail_without_review(self):
        for quoted in ({'evidence_id': 'E1', 'quote': ''},
                       {'evidence_id': 'E1', 'quote': '日曜日です。'},
                       {'evidence_id': 'FAKE', 'quote': self.rows[0]['text']}):
            with self.subTest(quoted=quoted):
                self.draft['facts'][0]['quotes'] = [quoted]
                artifact, calls = self.compose()
                self.assertEqual(artifact['reason_code'], 'guidance_quote_id_invalid')
                self.assertEqual(calls.call_count, 1)

    def test_exact_source_span_mapping_preserves_all_characters_and_source_context(self):
        texts = ['催しA\r\n火曜日 11時10分/15時40分。\n催しB\\n日曜日11:10-/15:40〜。',
                 '\n' + '長' * 1500 + '\r\n末尾', 'JSONの記載: {"text":"一行目\\n二行目"}']
        sources = [{'evidence_id': f'E{i}', 'text': text} for i, text in enumerate(texts)]
        presented, mapping = guidance._quote_sources(sources)
        self.assertEqual([''.join(part['text'] for part in source['ordered_parts']) for source in presented], texts)
        for source, output in zip(sources, presented):
            for part in output['ordered_parts']:
                self.assertEqual(mapping[part['quote_id']], {'evidence_id': source['evidence_id'], 'quote': part['text']})
                self.assertIn(part['text'], source['text'])

    def test_unknown_duplicate_or_retyped_quote_contract_is_rejected(self):
        for ids in (['Q9999'], ['Q1', 'Q1'], []):
            candidate = model_selection(self.draft)
            candidate['facts'][0]['quote_ids'] = ids
            artifact, calls = self.compose([response(candidate)])
            self.assertEqual(artifact['reason_code'], 'guidance_quote_id_invalid')
            self.assertEqual(calls.call_count, 1)
        candidate = model_selection(self.draft)
        candidate['facts'][0]['quotes'] = [{'evidence_id': 'E1', 'quote': '書き直した文字列'}]
        raw = {'done': True, 'done_reason': 'stop', 'prompt_eval_count': 2000, 'eval_count': 100,
               'message': {'content': json.dumps(candidate, ensure_ascii=False)}}
        artifact, calls = self.compose([raw])
        self.assertEqual(artifact['reason_code'], 'audit_response_schema_invalid')
        self.assertEqual(calls.call_count, 1)

    def test_original_exact_quote_validation_still_rejects_modified_text(self):
        for quote in ('火曜日と金曜日11:10', '別の情報'):
            draft = copy.deepcopy(self.draft)
            draft['facts'][0]['quotes'][0]['quote'] = quote
            with self.assertRaisesRegex(guidance.GuidanceError, 'guidance_quote_not_in_source'):
                guidance._validate_draft(draft, self.rows)

    def test_undated_question_cannot_gain_today_statement(self):
        for text in ('本日は開催日です。', '今日の開始は11:10です。', '明日は開催します。', 'It starts today.'):
            draft = copy.deepcopy(self.draft)
            draft['guidance'][0]['text'] = text
            artifact, calls = self.compose([response(draft)])
            self.assertEqual(artifact['reason_code'], 'guidance_unrequested_relative_day')
            self.assertEqual(calls.call_count, 1)
            self.assertIsNone(self.view(artifact))
        contract = copy.deepcopy(self.contract)
        contract['question'] = '2026年10月2日にはどう案内すればよいですか？'
        guidance._validate_date_scope(draft, contract)  # Explicit date still needs semantic review.

    def test_placeholder_missing_fact_reference_and_unresolved_cannot_display(self):
        cases = []
        draft = copy.deepcopy(self.draft); draft['guidance'][0]['text'] = '時間は○○です。'; cases.append(draft)
        draft = copy.deepcopy(self.draft); draft['guidance'][0]['fact_ids'] = ['MISSING']; cases.append(draft)
        draft = copy.deepcopy(self.draft); draft['unresolved'] = ['時刻不明']; cases.append(draft)
        draft = copy.deepcopy(self.draft); draft['facts'][1]['id'] = 'F1'; cases.append(draft)
        for draft in cases:
            with self.subTest(draft=draft):
                artifact, calls = self.compose([response(draft)])
                self.assertEqual(artifact['status'], 'incomplete')
                self.assertNotIn('answer', artifact)
                self.assertEqual(calls.call_count, 1)

    def test_semantic_fail_for_wrong_subject_missing_condition_or_noise_blocks_display(self):
        for key in ('support', 'same_subject', 'conditions', 'relevance'):
            with self.subTest(key=key):
                review = copy.deepcopy(self.review)
                review['checks'][2][key] = 'fail'
                artifact, calls = self.compose([response(self.draft), response(review)])
                self.assertEqual(artifact['reason_code'], 'guidance_sentence_checks_incomplete')
                self.assertEqual(calls.call_count, 2)
                self.assertIsNone(self.view(artifact))

    def test_blanket_verified_without_exact_coverage_does_not_pass(self):
        cases = []
        review = copy.deepcopy(self.review); review['checks'].pop(); cases.append(review)
        review = copy.deepcopy(self.review); review['requirements'].pop(); cases.append(review)
        review = copy.deepcopy(self.review); review['requirements'][0]['quote'] = 'not in answer'; cases.append(review)
        review = copy.deepcopy(self.review); review['requirements'][0]['status'] = 'missing'; cases.append(review)
        review = copy.deepcopy(self.review); review['requirements'].reverse(); cases.append(review)
        for review in cases:
            artifact, _ = self.compose([response(self.draft), response(review)])
            self.assertEqual(artifact['status'], 'incomplete')
            self.assertIsNone(self.view(artifact))

    def test_review_selects_exact_candidate_sentences_without_source_id_prefixes(self):
        schema = guidance._review_schema(self.draft)
        allowed = schema['properties']['requirements']['items']['properties']['quote']['enum']
        self.assertEqual(set(allowed), {''} | {item['text'] for key in ('facts', 'guidance')
                                              for item in self.draft[key]})
        self.assertTrue(guidance.guard.validate_schema(self.review, schema))
        for quote in ('Q1: 火曜日と金曜日', 'G1: ' + self.draft['guidance'][0]['text']):
            altered = copy.deepcopy(self.review)
            altered['requirements'][0]['quote'] = quote
            self.assertFalse(guidance.guard.validate_schema(altered, schema))
        missing = copy.deepcopy(self.review)
        missing['requirements'][0].update(status='missing', quote='')
        self.assertTrue(guidance.guard.validate_schema(missing, schema))
        artifact, _ = self.compose([response(self.draft), response(missing)])
        self.assertEqual(artifact['status'], 'incomplete')
        self.assertIsNone(self.view(artifact))

    def test_transport_length_usage_invalid_json_and_timeout_never_retry(self):
        cases = [response(self.draft, done_reason='length'), response(self.draft, done=False),
                 response(self.draft, prompt_eval_count=None),
                 response(self.draft, prompt_eval_count=7692),
                 response(self.draft, message={'content': '{broken'})]
        for raw in cases:
            artifact, calls = self.compose([raw])
            self.assertEqual(artifact['status'], 'incomplete')
            self.assertEqual(calls.call_count, 1)
        artifact, calls = self.compose(error=TimeoutError('secret source text'))
        self.assertNotIn('secret source text', json.dumps(artifact))
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(artifact['model_calls'][0]['stage'], 'compose')
        self.assertEqual(artifact['model_calls'][0]['context_usage']['requested_context_tokens'], 8192)

    def test_failure_in_review_never_leaks_candidate(self):
        artifact, calls = self.compose([response(self.draft), response(self.review, done_reason='length')])
        self.assertEqual(artifact['status'], 'incomplete')
        self.assertNotIn('draft', artifact)
        self.assertNotIn('answer', artifact)
        self.assertEqual(calls.call_count, 2)
        self.assertEqual(artifact['model_calls'][1]['context_usage']['done_reason'], 'length')
        self.assertIn('draft', artifact['diagnostics'])

    def test_verified_view_rechecks_record_contract_source_and_all_output_bindings(self):
        artifact, _ = self.compose()
        self.assertIsNotNone(self.view(artifact))
        for section, key in (('binding', 'record_sha256'), ('binding', 'contract_sha256'),
                             ('binding', 'sources_sha256'), ('binding', 'draft_sha256'),
                             ('binding', 'review_sha256'), ('binding', 'answer_sha256')):
            modified = copy.deepcopy(artifact); modified[section][key] = 'tampered'
            self.assertIsNone(self.view(modified))
        modified = copy.deepcopy(artifact); modified['answer'] = 'different'
        self.assertIsNone(self.view(modified))
        self.rows[0]['text'] += '更新済み'
        self.assertIsNone(self.view(artifact))

    def test_request_identity_prevents_copying_sidecar_to_other_request(self):
        self.record['request_id'] = 'request-one'
        artifact, _ = self.compose()
        self.assertIsNotNone(self.view(artifact))
        self.record['request_id'] = 'request-two'
        self.assertIsNone(self.view(artifact))

    def test_verified_view_revalidates_schema_quotes_semantics_and_usage_not_only_pass(self):
        artifact, _ = self.compose()
        for part in ('quote', 'semantic', 'usage'):
            altered = copy.deepcopy(artifact)
            if part == 'quote':
                altered['draft']['facts'][0]['quotes'][0]['quote'] = 'fabricated'
                altered['binding']['draft_sha256'] = guidance._hash(altered['draft'])
            elif part == 'semantic':
                altered['review']['checks'][0]['same_subject'] = 'fail'
                altered['binding']['review_sha256'] = guidance._hash(altered['review'])
            else:
                altered['model_calls'][0]['context_usage']['total_tokens'] = 8192
            self.assertIsNone(self.view(altered))

    def test_whole_source_budget_never_truncates_packet(self):
        self.rows[0]['text'] = '文' * 6501
        artifact, calls = self.compose()
        self.assertEqual(artifact['reason_code'], 'guidance_source_budget_exceeded')
        calls.assert_not_called()

    def test_complete_prompt_schema_and_output_reserve_are_budgeted_before_call(self):
        self.contract['goal'] += '非常に長い質問文' * 700
        self.record['query'] = guidance.intent.search_question(self.contract)
        artifact, calls = self.compose()
        self.assertEqual(artifact['reason_code'], 'guidance_prompt_budget_exceeded')
        self.assertGreater(artifact['model_calls'][0]['context_usage']['prompt_utf8_bytes'], 8192)
        calls.assert_not_called()


if __name__ == '__main__':
    unittest.main()
