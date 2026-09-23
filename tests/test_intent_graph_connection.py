"""Request transport tests; no real documents, models, indexes or app settings."""
import contextlib
import copy
import importlib.util
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from test_answerability_integration import engine, audit, FIELDS, fixture, run_main


def contract(count=1):
    return {'version': 1, 'question': '開店までの手順は？',
            'goal': '開店前の点検までを知りたい。閉店作業は含めない。',
            'requirements': ['知りたいことに対する、資料で裏付けられる具体的な回答']
                if count == 1 else [f'確認したい項目{i}' for i in range(1, count + 1)],
            'revision': {'generation': 'synthetic'}, 'expires_at': int(time.time()) + 1800}


def field_result(item_id, packet_id='E1'):
    return {'item_id': item_id, 'verdict': 'supported', 'supported_value': '青',
            'supporting_packet_ids': [packet_id], 'competing_packet_ids': [],
            'reason_code': 'none', 'defect': '', 'missing_information': []}


class IntentGraphConnectionTests(unittest.TestCase):
    def setUp(self):
        self.contract = contract()
        self.plan = engine.intent_requirements.plan_from_contract(self.contract)
        self.addCleanup(setattr, engine, 'ACTIVE_INTENT_PLAN', engine.ACTIVE_INTENT_PLAN)
        self.addCleanup(setattr, engine, 'ACTIVE_READING_SNAPSHOT', engine.ACTIVE_READING_SNAPSHOT)
        engine.ACTIVE_INTENT_PLAN = self.plan
        engine.ACTIVE_READING_SNAPSHOT = None
        _, packets = fixture()
        self.row = {**packets[0], **{key: 1.0 for key in
            ('score', 'rerank_score', 'document_support_bonus', 'semantic_score', 'lexical_score', 'token_score')}}

    def outer(self, results, batch=True):
        return {'message': {'content': json.dumps({'audits': results} if batch else results[0])},
                'done': True, 'done_reason': 'stop', 'prompt_eval_count': 800, 'eval_count': 130}

    def test_single_field_receives_original_question_goal_and_requirement_id(self):
        item = self.plan['items'][0]
        with mock.patch.object(engine.base, 'post_json', return_value=self.outer([field_result('F1')], False)) as call:
            engine.audit_field('fake-model', item, '青', {'E1': 'source-1'}, 1)
        user = call.call_args.args[1]['messages'][1]['content']
        for value in (self.contract['question'], self.contract['goal'], 'R1', 'expresses_goal', 'requires'):
            self.assertIn(value, user)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args[1]['options']['num_ctx'], 8192)

    def test_capacity_or_truncation_failure_cannot_be_treated_as_delivery(self):
        fields = [{'item': self.plan['items'][0], 'retrieved': [self.row]}]
        for change in ({'done_reason': 'length'}, {'prompt_eval_count': 8190},
                       {'eval_count': None}, {'done': False}):
            raw = {**self.outer([field_result('F1')]), **change}
            with self.subTest(change=change), \
                 mock.patch.object(engine.base, 'post_json', return_value=raw), self.assertRaises(ValueError):
                engine.audit_fields_batched('fake-model', fields, 1)
            self.assertNotEqual(fields[0]['model_context_attempts'][-1].get('intent_capacity_status'), 'observed')

    def test_batch_eight_requirements_reach_one_call_and_delivery_is_observed(self):
        value = contract(8)
        engine.ACTIVE_INTENT_PLAN = plan = engine.intent_requirements.plan_from_contract(value)
        engine.validate_plan(plan)
        fields = [{'item': item, 'retrieved': [self.row]} for item in plan['items']]
        results = [field_result(item['item_id']) for item in plan['items']]
        with mock.patch.object(engine.base, 'post_json', return_value=self.outer(results)) as call:
            audits = engine.audit_fields_batched('fake-model', fields, 1)
        user = call.call_args.args[1]['messages'][1]['content']
        self.assertEqual(user.count(value['goal']), 1)
        self.assertEqual(call.call_count, 1)
        self.assertEqual([r['item_id'] for r in audits], [f'F{i}' for i in range(1, 9)])
        self.assertIn('R8', user)
        self.assertEqual(call.call_args.args[1]['format']['properties']['audits']['maxItems'], 8)
        self.assertTrue(all(f['model_context_attempts'][-1]['delivery_status'] == 'response_received' for f in fields))
        self.assertEqual(len({f['model_context_attempts'][-1]['intent_input_sha256'] for f in fields}), 1)

    def test_original_requirement_is_not_removed_by_auxiliary_filter(self):
        value = contract()
        value['requirements'] = [engine.AUXILIARY_ITEM_MARKERS[0]]
        plan = engine.intent_requirements.plan_from_contract(value)
        engine.validate_plan(plan)
        self.assertEqual(plan['items'][0]['required_claim'], value['requirements'][0])

    def test_cache_identity_separates_intent_and_revision(self):
        metadata = {key: 'synthetic-' + key for key in FIELDS}
        base = engine.answer_cache_key('same question', metadata, 'model', 5, 'batched',
                                       confirmed_intent=self.contract)
        for changed in [{**self.contract, 'goal': '別の目的'},
                        {**self.contract, 'revision': {'generation': 'new'}},
                        {**self.contract, 'requirements': ['別の内容']}]:
            self.assertNotEqual(base, engine.answer_cache_key('same question', metadata, 'model', 5,
                                'batched', confirmed_intent=changed))

    def test_answer_binding_rejects_changed_dropped_or_reordered_requirements(self):
        value = contract(2)
        plan = engine.intent_requirements.plan_from_contract(value)
        record = {'query': value['question'], 'confirmed_intent': value,
                  'intent_requirement_graph': plan['intent_graph'], 'question_plan': plan,
                  'field_runs': [{'item': item} for item in plan['items']]}
        audit.intent_contract.validate_record_binding(value, record)
        for bad in [dict(record, query='別質問'), dict(record, field_runs=record['field_runs'][:1]),
                    dict(record, field_runs=list(reversed(record['field_runs']))),
                    dict(record, confirmed_intent=dict(value, goal='別の目的'))]:
            with self.assertRaises(ValueError):
                audit.intent_contract.validate_record_binding(value, bad)

    def run_engine(self, value, failure=False):
        metadata = {**{key: 'synthetic-' + key for key in FIELDS}, 'model': 'fake-embed'}
        qeg = {'status': 'unsupported', 'intent': {'operation': 'unknown'}, 'reason': 'unknown'}
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            index = Path(directory) / 'unused.sqlite'
            index.touch()
            stack.enter_context(mock.patch.object(sys, 'argv', ['engine', value['question'],
                '--index', str(index), '--audit-mode', 'batched', '--intent-contract-stdin', '--json']))
            stack.enter_context(mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(value))))
            stack.enter_context(mock.patch.object(engine, 'index_metadata', return_value=metadata))
            stack.enter_context(mock.patch.object(engine, 'load_index_evidence_graph',
                return_value=([self.row], {self.row['evidence_id']: self.row}, {})))
            stack.enter_context(mock.patch.object(engine.question_graph, 'build_question_evidence_graph', return_value=qeg))
            stack.enter_context(mock.patch.object(engine.question_graph, 'validate_question_evidence_graph',
                return_value={'status': 'not_applicable', 'failures': []}))
            stack.enter_context(mock.patch.object(engine, 'build_workflow_source_bundle',
                return_value=([], {'status': 'disabled', 'excluded_evidence': []})))
            stack.enter_context(mock.patch.object(engine, 'workflow_source_context', return_value=None))
            retrieve = stack.enter_context(mock.patch.object(engine, 'retrieve_versioned', return_value=(metadata, [self.row])))
            planner = stack.enter_context(mock.patch.object(engine, 'plan_question', side_effect=AssertionError('no replanning')))
            single = stack.enter_context(mock.patch.object(engine, 'audit_input', side_effect=AssertionError('no retry')))
            results = [field_result(f'F{i}') for i in range(1, len(value['requirements']) + 1)]
            call = stack.enter_context(mock.patch.object(engine.base, 'post_json',
                return_value=self.outer(results), side_effect=TimeoutError('synthetic') if failure else None))
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            engine.main()
            planner.assert_not_called()
            single.assert_not_called()
            self.assertEqual(call.call_count, 1)
            self.assertIn(value['goal'], retrieve.call_args.args[1])
            return json.loads(output.getvalue())

    def test_cli_connection_keeps_all_ids_and_never_claims_source_semantics(self):
        value = contract(8)
        record = self.run_engine(value)
        audit.intent_contract.validate_record_binding(value, record)
        trace = record['intent_requirement_trace']
        self.assertTrue(trace['graph_built'])
        self.assertTrue(trace['graph_delivered'])
        self.assertFalse(trace['relations_adopted'])
        self.assertFalse(trace['requirements_checked'])
        engine.intent_requirements.validate_work_mapping(
            record['question_plan'], trace['work_mapping'],
            engine.question_graph.RECORD_LOOKUP_FIELD_ALIASES)
        self.assertEqual(trace['work_mapping']['execution_status'],
                         'mapping_only_not_dispatched')
        self.assertEqual(len(trace['requirements']), 8)
        self.assertEqual(record['performance']['planning_mode'], 'confirmed_intent_deterministic')

    def test_failed_batch_does_not_spawn_eight_individual_calls(self):
        record = self.run_engine(contract(8), failure=True)
        self.assertEqual(len(record['field_runs']), 8)
        self.assertTrue(all(r['audit']['verdict'] == 'insufficient' for r in record['field_runs']))
        self.assertFalse(record['intent_requirement_trace']['graph_delivered'])

    def test_expired_or_mismatched_contract_stops_before_index_or_models(self):
        for value in [dict(self.contract, expires_at=0), dict(self.contract, question='別質問')]:
            with mock.patch.object(sys, 'argv', ['engine', self.contract['question'], '--index', 'unused',
                    '--audit-mode', 'batched', '--intent-contract-stdin']), \
                 mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(value))), \
                 mock.patch.object(engine, 'index_metadata') as read_index, \
                 self.assertRaises(ValueError):
                engine.main()
            read_index.assert_not_called()

    def test_final_audit_preserves_contract_and_rejects_modified_field_mapping(self):
        record, packets = fixture()
        value = contract(3)
        value['question'] = record['query']
        value['requirements'] = [r['item']['required_claim'] for r in record['field_runs']]
        plan = engine.intent_requirements.plan_from_contract(value)
        for run, item in zip(record['field_runs'], plan['items']):
            run['item'] = item
        record.update(confirmed_intent=value, question_plan=plan, intent_requirement_graph=plan['intent_graph'])
        result, llm = run_main(record, packets)
        audit.intent_contract.validate_record_binding(value, result)
        self.assertEqual(llm.call_args.args[5]['intent_requirement_graph'], plan['intent_graph'])
        bad = copy.deepcopy(record)
        bad['field_runs'][0]['item']['intent_requirement_id'] = 'R3'
        with self.assertRaises(ValueError):
            run_main(bad, packets)


if __name__ == '__main__':
    unittest.main()
