"""Real deterministic QEG, synthetic data; no model or installed app used."""

import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

from test_intent_requirement_graph import graph
from test_record_lookup_question_graph import qeg, evidence_fixture, source_graph
from test_answerability_integration import engine, audit, FIELDS, fixture, run_main


class IntentLookupDispatchTests(unittest.TestCase):
    def setUp(self):
        self.contract = {'version': 1,
            'question': 'For the finalized Project Atlas record, give the Owner and Unit Cost.',
            'goal': 'Provide both values and explain the conditions.',
            'requirements': ['Owner and Unit Cost, including conditions.'],
            'revision': {'generation': 'synthetic'}, 'expires_at': int(time.time()) + 1800}
        self.plan = graph.plan_from_contract(self.contract)
        self.rows = evidence_fixture()
        self.source = source_graph(self.rows)
        self.reference = '2026-09-23'
        self.addCleanup(setattr, engine, 'ACTIVE_INTENT_PLAN', engine.ACTIVE_INTENT_PLAN)
        self.addCleanup(setattr, engine, 'ACTIVE_READING_SNAPSHOT', engine.ACTIVE_READING_SNAPSHOT)
        engine.ACTIVE_INTENT_PLAN = None
        engine.ACTIVE_READING_SNAPSHOT = None

    def dispatch(self, plan=None, rows=None, source=None):
        return graph.dispatch_lookup_candidates(
            self.plan if plan is None else plan, self.rows if rows is None else rows,
            self.source if source is None else source, self.reference, qeg)

    def record(self):
        dispatch = self.dispatch()
        ids = dispatch['selected_by_item']['F1']
        return {'confirmed_intent': self.contract, 'query': self.contract['question'],
            'question_plan': self.plan, 'intent_requirement_graph': self.plan['intent_graph'],
            'question_reference_date': self.reference,
            'intent_requirement_trace': {'work_mapping': graph.build_work_mapping(
                self.plan, qeg.RECORD_LOOKUP_FIELD_ALIASES)},
            'intent_lookup_dispatch': dispatch,
            'field_runs': [{'item': self.plan['items'][0],
                'intent_lookup_candidate_evidence_ids': ids,
                'retrieved_evidence_ids': ids,
                'audit': {'verdict': 'supported'},
                'model_context_attempts': [{'evidence_ids': ids,
                    'delivery_status': 'response_received', 'intent_capacity_status': 'observed'}]}]}

    def test_compound_lookup_ready_without_changing_whole_requirement(self):
        before = copy.deepcopy(self.plan)
        result = self.dispatch()
        self.assertEqual(result['status'], 'candidate_ready')
        self.assertEqual([b['value'] for b in result['graph']['branches']], ['Maya Chen', '1250'])
        self.assertEqual(self.plan, before)
        self.assertFalse(result['requirements_checked'])
        self.assertFalse(result['relations_adopted'])
        graph.validate_lookup_dispatch(self.plan, result, self.rows, self.source, self.reference, qeg)

    def test_unknown_relation_stays_whole_and_skips_specialist_without_model(self):
        plan = graph.plan_from_contract({**self.contract, 'requirements': ['必要な条件と例外を説明する']})
        with mock.patch.object(qeg, 'build_question_evidence_graph') as build:
            result = self.dispatch(plan=plan)
        build.assert_not_called()
        self.assertEqual(result['status'], 'not_applicable')
        self.assertEqual(result['selected_by_item'], {'F1': []})

    def test_different_requirements_same_field_not_merged(self):
        plan = graph.plan_from_contract({**self.contract, 'requirements': ['Owner of A', 'Owner of B']})
        result = self.dispatch(plan=plan)
        self.assertEqual(len(result['lookup_plan']['items']), 2)
        self.assertEqual(result['status'], 'held')
        self.assertEqual(result['selected_by_item'], {'F1': [], 'F2': []})

    def test_ungrounded_field_cannot_be_added_by_rewriting_question(self):
        plan = graph.plan_from_contract({**self.contract, 'question': 'Project Atlasは何ですか？',
                                        'requirements': ['Owner']})
        result = self.dispatch(plan=plan)
        self.assertEqual(result['status'], 'held')
        self.assertEqual(result['selected_by_item'], {'F1': []})

    def test_relative_time_without_typed_scope_is_held(self):
        plan = graph.plan_from_contract({**self.contract,
            'question': '5年前の Project Atlas の担当者は誰ですか？', 'requirements': ['担当者']})
        result = self.dispatch(plan=plan)
        self.assertEqual(result['status'], 'held')
        self.assertIn('temporal', result['reason'])

    def test_competing_records_are_not_overridden(self):
        rows = evidence_fixture(second_approved=True)
        result = self.dispatch(rows=rows, source=source_graph(rows))
        self.assertEqual(result['status'], 'held')
        self.assertEqual(result['selected_by_item'], {'F1': []})

    def test_missing_source_graph_not_accepted(self):
        result = self.dispatch(source={})
        self.assertNotEqual(result['status'], 'candidate_ready')

    def test_augmented_candidates_keep_existing_order_and_avoid_duplicates(self):
        dispatch = self.dispatch()
        by_id = {r['evidence_id']: r for r in self.rows}
        existing = [by_id['ev_unrelated_note'], by_id['ev_row_approved']]
        result, ids = engine.augment_intent_lookup_candidates(
            existing, by_id, dispatch, 'F1', {'status': 'disabled'})
        self.assertEqual(result[:2], existing)
        self.assertEqual(len({r['evidence_id'] for r in result}), len(result))
        self.assertTrue(set(ids) <= {r['evidence_id'] for r in result})

    def test_no_candidate_can_cross_version_filter(self):
        with mock.patch.object(engine, 'restrict_version_paths', return_value=[]), self.assertRaisesRegex(
                ValueError, 'version_scope_mismatch'):
            engine.augment_intent_lookup_candidates([], {r['evidence_id']: r for r in self.rows},
                self.dispatch(), 'F1', {})

    def test_context_omission_is_rejected_before_model_call(self):
        dispatch = self.dispatch()
        field = {'retrieved': [],
                 'intent_lookup_candidate_evidence_ids': dispatch['selected_by_item']['F1']}
        with self.assertRaisesRegex(ValueError, 'context_missing_candidate'):
            engine.require_graph_primary_coverage(field, {'E1': 'ev_row_approved'})

    def test_total_candidate_budget_includes_ordinary_hits_before_any_model_call(self):
        engine.ACTIVE_INTENT_PLAN = self.plan
        rows = [{**self.rows[-1], 'evidence_id': f'candidate-{n}'} for n in range(13)]
        field = {'item': self.plan['items'][0], 'retrieved': rows}
        with mock.patch.object(engine.base, 'post_json') as call, self.assertRaisesRegex(
                ValueError, 'block_budget_exceeded'):
            engine.audit_fields_batched('unused', [field], 1)
        call.assert_not_called()

    def test_character_packing_cannot_silently_drop_appended_lookup(self):
        engine.ACTIVE_INTENT_PLAN = self.plan
        rows = [{**self.rows[-1], 'evidence_id': f'long-{n}', 'text': '原文' * 600}
                for n in range(5)]
        field = {'item': self.plan['items'][0], 'retrieved': rows,
                 'intent_lookup_candidate_evidence_ids': ['long-4']}
        with mock.patch.object(engine.base, 'post_json') as call, self.assertRaisesRegex(
                ValueError, 'context_missing_candidate'):
            engine.audit_fields_batched('unused', [field], 1)
        call.assert_not_called()

    def test_final_rebuild_checks_mapping_source_and_delivery(self):
        record = self.record()
        result = audit.validate_intent_lookup_binding(record, self.rows, self.source)
        self.assertEqual(result['status'], 'pass')
        self.assertFalse(result['requirements_checked'])
        mutations = [lambda r: r.pop('intent_lookup_dispatch'),
            lambda r: r['intent_lookup_dispatch']['selected_by_item'].update(F1=[]),
            lambda r: r['intent_requirement_trace']['work_mapping']['links'].pop(),
            lambda r: r['field_runs'][0].update(intent_lookup_candidate_evidence_ids=[]),
            lambda r: r['field_runs'][0].update(retrieved_evidence_ids=[]),
            lambda r: r['field_runs'][0]['model_context_attempts'][0].update(evidence_ids=[]),
            lambda r: r['field_runs'][0]['model_context_attempts'][0].update(delivery_status='packed_not_sent'),
            lambda r: r.update(registered_version_scope={'status': 'ready', 'allowed_relative_paths': []}),
            lambda r: r.update(question_reference_date='2026-09-24')]
        for mutate in mutations:
            bad = copy.deepcopy(record)
            mutate(bad)
            with self.assertRaises(ValueError):
                audit.validate_intent_lookup_binding(bad, self.rows, self.source)
        changed_rows = copy.deepcopy(self.rows)
        next(r for r in changed_rows if r['evidence_id'] == 'ev_v_b_approved')['text'] = 'Other Owner'
        with self.assertRaises(ValueError):
            audit.validate_intent_lookup_binding(record, changed_rows, self.source)

    def test_unreceived_candidate_is_not_observed_or_completed(self):
        record = self.record()
        record['field_runs'][0]['audit']['verdict'] = 'insufficient'
        record['field_runs'][0]['model_context_attempts'] = []
        result = audit.validate_intent_lookup_binding(record, self.rows, self.source)
        self.assertEqual(result['delivery'][0]['delivery'], 'not_observed')
        self.assertFalse(result['requirements_checked'])

    def test_new_schema_requires_dispatch_check_even_if_both_receipts_deleted(self):
        record, packets = fixture()
        record['schema_version'] = '0.4-intent-candidate-dispatch'
        with mock.patch.object(audit, 'validate_intent_lookup_binding',
                               side_effect=ValueError('required_dispatch_check')) as check:
            with self.assertRaisesRegex(ValueError, 'required_dispatch_check'):
                run_main(record, packets)
        check.assert_called_once()

    def test_real_qeg_cli_dispatch_delivers_candidates_but_does_not_bypass_primary_hold(self):
        metadata = {**{k: 'synthetic-' + k for k in FIELDS}, 'model': 'fake-embed'}
        output = io.StringIO()
        outer = {'message': {'content': json.dumps({'audits': [{
            'item_id': 'F1', 'verdict': 'supported', 'supported_value': 'Maya Chen',
            'supporting_packet_ids': ['E4'], 'competing_packet_ids': [],
            'reason_code': 'none', 'defect': '', 'missing_information': []}]})},
            'done': True, 'done_reason': 'stop', 'prompt_eval_count': 800, 'eval_count': 130}
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            index = Path(directory) / 'synthetic.sqlite'
            index.touch()
            stack.enter_context(mock.patch.object(sys, 'argv', ['engine', self.contract['question'],
                '--index', str(index), '--audit-mode', 'batched', '--intent-contract-stdin',
                '--no-workflow-bundle', '--json']))
            stack.enter_context(mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(self.contract))))
            stack.enter_context(mock.patch.object(engine, 'index_metadata', return_value=metadata))
            stack.enter_context(mock.patch.object(engine, 'load_index_evidence_graph',
                return_value=(self.rows, {r['evidence_id']: r for r in self.rows}, self.source)))
            ordinary = {**self.rows[-1], **{key: 1.0 for key in (
                'score', 'rerank_score', 'document_support_bonus',
                'semantic_score', 'lexical_score', 'token_score')}}
            stack.enter_context(mock.patch.object(engine, 'retrieve_versioned',
                return_value=(metadata, [ordinary])))
            stack.enter_context(mock.patch.object(engine, 'workflow_source_context', return_value=None))
            planner = stack.enter_context(mock.patch.object(engine, 'plan_question'))
            llm = stack.enter_context(mock.patch.object(engine.base, 'post_json', return_value=outer))
            stack.enter_context(contextlib.redirect_stdout(output))
            engine.main()
        record = json.loads(output.getvalue())
        planner.assert_not_called()
        self.assertEqual(llm.call_count, 1)
        self.assertEqual(record['intent_lookup_dispatch']['status'], 'candidate_ready')
        self.assertNotEqual(record['question_evidence_graph']['status'], 'ready')
        self.assertEqual(record['field_runs'][0]['audit']['verdict'], 'insufficient')
        result = audit.validate_intent_lookup_binding(record, self.rows, self.source)
        self.assertEqual(result['delivery'][0]['delivery'], 'observed')
        self.assertIn('Maya Chen', llm.call_args.args[1]['messages'][1]['content'])
        self.assertIn('1250', llm.call_args.args[1]['messages'][1]['content'])
        self.assertIn('including conditions.', llm.call_args.args[1]['messages'][1]['content'])


if __name__ == '__main__':
    unittest.main()
