"""Synthetic model-boundary tests. No actual inference or private documents."""
import copy
import importlib.util
import io
import json
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from test_question_graph_executor import answer
from test_workflow_bundle_connection import metadata, record
from test_ordered_section_question_graph import source_graph


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('workflow_reasoner_test',
    ROOT / 'distribution/macos-local-memory/engine/workflow_relation_reasoner.py')
reasoner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reasoner)
QUERY = '窓口対応の手順と条件を教えてください'
RAW = '窓口担当は、予約票がない場合、確認係へ連絡する。'
PLAN = {'items': [{'item_id': 'F1', 'label': '手順と条件', 'required_claim': QUERY,
                  'retrieval_query': '窓口対応 手順 条件', 'required': True}], 'answer_shape': '手順'}
PACKETS = {'E1': record('ev1', {'row_index': 6}, RAW)}
CONTEXT = '[EVIDENCE E1]\n' + RAW
GRAPH = {
    'nodes': [{'id': 'N1', 'kind': 'actor', 'packet_id': 'E1', 'quote': '窓口担当'},
              {'id': 'N2', 'kind': 'condition', 'packet_id': 'E1', 'quote': '予約票がない場合'},
              {'id': 'N3', 'kind': 'action', 'packet_id': 'E1', 'quote': '確認係へ連絡する'}],
    'edges': [{'id': 'R1', 'source': 'N1', 'target': 'N3', 'kind': 'performs',
               'support_packet_id': 'E1', 'support_quote': RAW},
              {'id': 'R2', 'source': 'N2', 'target': 'N3', 'kind': 'when',
               'support_packet_id': 'E1', 'support_quote': RAW}],
    'unresolved_packet_ids': [],
}
DRAFT = {'items': [{'item_id': 'F1', 'text': RAW, 'relation_ids': ['R1', 'R2'],
                    'supporting_packet_ids': ['E1'], 'unresolved': []}]}
REVIEW = {'items': [{'item_id': 'F1', 'verdict': 'pass', 'complete': True, 'reason': '', 'missing': []}],
          'question_requirements': [{'requirement_id': r, 'status': 'fulfilled',
                                     'item_ids': ['F1'], 'reason': ''} for r in ['Q_actions', 'Q_conditions', 'Q_order']],
          'relation_checks': [{'relation_id': r, 'verdict': 'pass', 'reason': ''} for r in ['R1', 'R2']]}


class ReasonerTests(unittest.TestCase):
    def run_case(self, replies=None, **kwargs):
        replies = copy.deepcopy([GRAPH, DRAFT, REVIEW] if replies is None else replies)
        calls = []

        def post(url, payload, timeout):
            calls.append({'url': url, 'payload': copy.deepcopy(payload), 'timeout': timeout})
            value = replies.pop(0)
            if isinstance(value, Exception):
                raise value
            return {'message': {'content': json.dumps(value, ensure_ascii=False)},
                    'done_reason': 'stop', 'prompt_eval_count': 100, 'eval_count': 50}

        args = dict(model='gemma-test', query=QUERY, plan=copy.deepcopy(PLAN),
            packet_sources=copy.deepcopy(PACKETS), context=CONTEXT, post_json=post,
            chat_url='http://127.0.0.1:11434/api/chat', escape=answer.base.escape_evidence_quotation)
        args.update(kwargs)
        return reasoner.run(**args), calls

    def test_graph_and_raw_reach_explanation_and_check(self):
        result, calls = self.run_case()
        self.assertEqual(result['status'], 'checked')
        self.assertTrue(result['complete'])
        self.assertTrue(result['graph_delivered'])
        self.assertEqual(result['used_relation_ids'], ['R1', 'R2'])
        self.assertEqual(len(calls), 3)
        # Source extraction is not conditioned on a desired answer/question.
        self.assertNotIn(QUERY, calls[0]['payload']['messages'][-1]['content'])
        for call in calls[1:]:
            content = call['payload']['messages'][-1]['content']
            self.assertIn(RAW, content)
            self.assertIn('Q_request', content)
            self.assertIn('R2', content)
            self.assertIn('N3', content)
        self.assertEqual(result['audits'][0]['supporting_packet_ids'], ['ev1'])
        self.assertEqual(result['semantic_validation'], 'same_model_separate_context')
        self.assertEqual(result['model_calls'][1]['edge_ids'], ['R1', 'R2'])

    def test_compact_model_output_restores_exact_source_provenance(self):
        wire = {'n': [{'i': n['id'], 'k': n['kind'], 'p': n['packet_id'], 'q': n['quote']}
                      for n in GRAPH['nodes']],
                'e': [{'i': e['id'], 's': e['source'], 't': e['target'],
                       'k': e['kind'], 'p': e['support_packet_id']} for e in GRAPH['edges']], 'u': []}
        result, _ = self.run_case([wire, DRAFT, REVIEW])
        self.assertTrue(result['complete'])
        self.assertEqual(result['source_graph']['edges'][0]['support_quote'], RAW)

    def test_compact_input_support_spans_resolve_without_losing_relations(self):
        normalized = reasoner.graph.normalize_source_graph(GRAPH, PACKETS)
        qg = reasoner.graph.build_question_graph(QUERY, PLAN)
        matches = reasoner.graph.build_relation_matches(qg, normalized)
        data = json.loads(reasoner.render_model_graph(qg, normalized, matches, PACKETS))
        self.assertEqual([e[0] for e in data['source_edges']], ['R1', 'R2'])
        for packet_id, start, end in data['support_spans'].values():
            self.assertEqual(json.loads(PACKETS[packet_id]['text'])[start:end], RAW)

    def test_reversed_semantics_rejected_by_separate_check(self):
        check = copy.deepcopy(REVIEW)
        check['relation_checks'][1].update(verdict='fail', reason='条件の意味が逆')
        for row in check['question_requirements']:
            row.update(status='unresolved', reason='関係を支持できない')
        result, _ = self.run_case([GRAPH, DRAFT, check])
        self.assertFalse(result['complete'])
        self.assertEqual(result['audits'][0]['verdict'], 'insufficient')
        self.assertEqual(result['used_relation_ids'], [])

    def test_missing_steps_not_complete_even_if_text_supported(self):
        check = copy.deepcopy(REVIEW)
        check['items'][0].update(complete=False, missing=['次の対応が未確認'])
        for row in check['question_requirements']:
            row.update(status='unresolved', reason='次の対応が未確認')
        result, _ = self.run_case([GRAPH, DRAFT, check])
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['audits'][0]['verdict'], 'insufficient')
        self.assertEqual(result['partial_explanations'][0]['text'], RAW)

    def test_unknown_relation_id_never_reaches_reviewer(self):
        draft = copy.deepcopy(DRAFT); draft['items'][0]['relation_ids'].append('fake')
        result, calls = self.run_case([GRAPH, draft])
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['status'], 'error')

    def test_unknown_evidence_cannot_support_explanation(self):
        draft = copy.deepcopy(DRAFT); draft['items'][0]['supporting_packet_ids'] = ['E999']
        result, calls = self.run_case([GRAPH, draft])
        self.assertFalse(result['complete'])
        self.assertEqual(len(calls), 2)

    def test_relation_requires_both_endpoints_and_support_packet(self):
        source = copy.deepcopy(PACKETS)
        source['E2'] = record('ev2', {'cell': 'B6'}, '窓口担当')
        graph = copy.deepcopy(GRAPH); graph['nodes'][0]['packet_id'] = 'E2'
        result, calls = self.run_case([graph, DRAFT], packet_sources=source,
                                     context=CONTEXT + '\n[EVIDENCE E2]\n窓口担当')
        self.assertEqual(result['status'], 'error')
        self.assertIn('relation_evidence_missing', result['error'])
        self.assertEqual(len(calls), 2)

    def test_each_adopted_edge_must_be_reviewed_exactly_once(self):
        for checks in [[], [REVIEW['relation_checks'][0]] * 2]:
            check = copy.deepcopy(REVIEW); check['relation_checks'] = checks
            result, _ = self.run_case([GRAPH, DRAFT, check])
            self.assertEqual(result['status'], 'error')

    def test_model_or_json_failure_stops_no_silent_fallback(self):
        result, calls = self.run_case([RuntimeError('simulated HTTP 500')])
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['audits'][0]['verdict'], 'insufficient')

    def test_graph_delivery_is_not_conflated_with_json_validity(self):
        replies = [{'message': {'content': json.dumps(GRAPH)}}, {'message': {'content': '{"items":'}}]
        for reply in replies:
            reply.update(done_reason='stop', prompt_eval_count=100, eval_count=50)
        result, _ = self.run_case(post_json=lambda *_: replies.pop(0))
        self.assertTrue(result['graph_delivered'])
        self.assertTrue(result['model_calls'][1]['response_received'])
        self.assertEqual(result['model_calls'][1]['status'], 'error')
        self.assertFalse(result['complete'])

    def test_empty_plan_rejected_before_inference(self):
        result, calls = self.run_case(plan={'items': []})
        self.assertEqual(calls, [])
        self.assertFalse(result['complete'])
        self.assertIn('plan_invalid', result['error'])

    def test_each_question_requirement_needs_explicit_review(self):
        check = copy.deepcopy(REVIEW); check['question_requirements'].pop()
        result, _ = self.run_case([GRAPH, DRAFT, check])
        self.assertEqual(result['status'], 'error')
        self.assertIn('question_coverage_missing', result['error'])

    def test_fulfilled_requirement_cannot_point_at_failed_item(self):
        check = copy.deepcopy(REVIEW)
        check['items'][0].update(verdict='fail', complete=False, reason='支持されない')
        result, _ = self.run_case([GRAPH, DRAFT, check])
        self.assertEqual(result['status'], 'error')
        self.assertIn('coverage_failed_item', result['error'])

    def test_optional_planner_field_cannot_hide_unresolved_question(self):
        projected = {'answer_mode': 'grounded', 'answer': '一部の回答', 'uncertainties': []}
        reasoner.apply_completion_guard(projected, {'status': 'incomplete', 'complete': False})
        self.assertEqual(projected['answer_mode'], 'qualified')
        self.assertTrue(projected['uncertainties'])

    def test_completion_guard_preserves_partial_answer_prohibition(self):
        for mode in ('grounded', 'qualified'):
            projected = {'answer_status': 'answered', 'answer_mode': mode,
                         'answer': '一部の回答', 'evidence_ids': ['ev1'], 'uncertainties': []}
            reasoner.apply_completion_guard(projected, {'status': 'incomplete', 'complete': False},
                                            partial_answer_allowed=False)
            self.assertEqual(projected['answer_mode'], 'insufficient')
            self.assertEqual(projected['answer_status'], 'insufficient')
            self.assertEqual(projected['answer'], 'わかりません')
            self.assertEqual(projected['evidence_ids'], [])
            self.assertEqual(projected['diagnostic_evidence_ids'], ['ev1'])
            self.assertEqual(projected['non_answer_reason']['code'], 'coverage_unknown')

    def test_planner_omission_cannot_remove_question_requirement(self):
        plan = copy.deepcopy(PLAN); plan['items'][0]['required_claim'] = '窓口担当は誰'
        check = copy.deepcopy(REVIEW)
        check['question_requirements'][1].update(status='unresolved', item_ids=[], reason='条件の説明がない')
        result, _ = self.run_case([GRAPH, DRAFT, check], plan=plan)
        self.assertFalse(result['complete'])
        self.assertEqual(result['audits'][0]['verdict'], 'insufficient')

    def test_invalid_graph_never_promoted(self):
        result, calls = self.run_case([{'nodes': [], 'edges': [], 'unresolved_packet_ids': ['E1']}])
        self.assertEqual(len(calls), 1)
        self.assertFalse(result['graph_delivered'])

    def test_budget_or_deadline_stops_without_call(self):
        for kw in [{'context': 'x' * 12001}, {'deadline': time.monotonic() - 1}]:
            result, calls = self.run_case(**kw)
            self.assertEqual(calls, [])
            self.assertEqual(result['status'], 'error')

    def test_model_endpoint_is_local_only(self):
        result, calls = self.run_case(chat_url='https://example.invalid/api/chat')
        self.assertEqual(calls, [])
        self.assertIn('local_model_endpoint', result['error'])

    def test_incomplete_check_cannot_claim_complete(self):
        check = copy.deepcopy(REVIEW); check['items'][0]['missing'] = ['不足']
        result, _ = self.run_case([GRAPH, DRAFT, check])
        self.assertFalse(result['complete'])
        self.assertEqual(result['status'], 'error')


class MainConnectionTests(unittest.TestCase):
    def test_opt_in_preserves_existing_qeg_and_records_real_boundary(self):
        rows = [record('ev1', {'row_index': 6}, RAW)]
        graph = source_graph(rows)
        replies = copy.deepcopy([GRAPH, DRAFT, REVIEW])

        def post(*_args):
            return {'message': {'content': json.dumps(replies.pop(0), ensure_ascii=False)},
                    'done_reason': 'stop', 'prompt_eval_count': 100, 'eval_count': 50}

        with mock.patch.object(sys, 'argv', [answer.__file__, QUERY, '--index', answer.__file__,
                '--workflow-relations', '--json']), mock.patch.object(
                answer, 'index_metadata', return_value=metadata()), mock.patch.object(
                answer, 'plan_question', return_value=copy.deepcopy(PLAN)), mock.patch.object(
                answer, 'load_index_evidence_graph', return_value=(rows, {'ev1': rows[0]}, graph)), mock.patch.object(
                answer, 'retrieve_hybrid', return_value=(metadata(), [])), mock.patch.object(
                answer.base, 'post_json', side_effect=post), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(answer.main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(replies, [])
        self.assertEqual(result['question_evidence_graph']['intent']['operation'], 'unknown')
        self.assertFalse(result['graph_route']['used'])
        self.assertTrue(result['workflow_reasoning']['graph_delivered'])
        self.assertEqual(result['workflow_reasoning']['used_relation_ids'], ['R1', 'R2'])
        self.assertIn(RAW, result['answer']['answer'])
        self.assertEqual(result['answer']['evidence_ids'], ['ev1'])


if __name__ == '__main__':
    unittest.main()
