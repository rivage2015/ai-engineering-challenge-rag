"""Synthetic end-to-end final auditing; no private data or real model calls."""
import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from test_final_answer_graph_audit import final_audit
from test_workflow_relation_reasoner import answer, PLAN, QUERY, RAW, GRAPH, REVIEW
from test_workflow_bundle_connection import metadata, record
from test_ordered_section_question_graph import source_graph


PARAPHRASE = '予約票をお持ちでない場合は、窓口担当から確認係に連絡します。'


def fixture(text=PARAPHRASE, *, context_tokens=8192):
    rows = [record('ev1', {'row_index': 6}, RAW)]
    graph = source_graph(rows)
    draft = {'items': [{'item_id': 'F1', 'text': text, 'relation_ids': ['R1', 'R2'],
                        'supporting_packet_ids': ['E1'], 'unresolved': []}]}
    replies = copy.deepcopy([GRAPH, draft, REVIEW])
    def post(*args):
        return {'message': {'content': json.dumps(replies.pop(0), ensure_ascii=False)},
                'done_reason': 'stop', 'prompt_eval_count': 100, 'eval_count': 50}
    with mock.patch.object(sys, 'argv', [answer.__file__, QUERY, '--index', answer.__file__,
            '--workflow-relations', '--workflow-context-tokens', str(context_tokens), '--json']), mock.patch.object(
            answer, 'index_metadata', return_value=metadata()), mock.patch.object(
            answer, 'plan_question', return_value=copy.deepcopy(PLAN)), mock.patch.object(
            answer, 'load_index_evidence_graph', return_value=(rows, {'ev1': rows[0]}, graph)), mock.patch.object(
            answer, 'retrieve_hybrid', return_value=(metadata(), [])), mock.patch.object(
            answer.base, 'post_json', side_effect=post), redirect_stdout(io.StringIO()) as output:
        assert answer.main() == 0
    result = json.loads(output.getvalue())
    meta = metadata()
    policy = {'eligible_evidence_ids': {'ev1'}, 'metadata': meta, 'source_graph': graph,
              'graph_sha256': meta['graph_sha256'], 'partition_sha256': meta['graph_security_partition_sha256'],
              'eligible_evidence_set_sha256': meta['graph_retrievable_evidence_set_sha256']}
    packets = [{'evidence_id': r['evidence_id'], 'document_id': r['document_id'],
                'path': r['relative_path'], 'locator': r['locator'], 'text': r['text']} for r in rows]
    return result, rows, policy, packets


def review_for(rec):
    return {'verdict': 'verified', 'reason': '原文から支持', 'unsupported_claims': [],
        'explanation_checks': [{'claim_id': 'C1', 'verdict': 'pass', 'reason': ''}],
        'relation_checks': [{'relation_id': r, 'verdict': 'pass', 'reason': ''} for r in ['R1', 'R2']],
        'question_requirement_checks': [{'requirement_id': q['id'], 'verdict': 'pass', 'reason': ''}
                                       for q in rec['workflow_reasoning']['question_graph']['requirements']]}


class WorkflowFinalAuditTests(unittest.TestCase):
    def run_final(self, rec, rows, policy, response=None, *, done_reason='stop', usage=None):
        reply = review_for(rec) if response is None else response
        calls = []
        def open_request(request, timeout):
            calls.append(json.loads(request.data))
            model_response = {'message': {'content': json.dumps(reply, ensure_ascii=False)},
                              'done_reason': done_reason}
            model_response.update({'prompt_eval_count': 100, 'eval_count': 50} if usage is None else usage)
            return io.BytesIO(json.dumps(model_response, ensure_ascii=False).encode())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'record.json'
            path.write_text(json.dumps(rec, ensure_ascii=False))
            with mock.patch.object(sys, 'argv', [final_audit.__file__, '--record', str(path),
                    '--index', str(Path(directory) / 'fake.sqlite3')]), mock.patch.object(
                    final_audit.answer_engine, 'load_answer_evidence_records', return_value=(rows, policy)), mock.patch.object(
                    final_audit.LOCAL_HTTP_OPENER, 'open', side_effect=open_request), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(final_audit.main(), 0)
            return json.loads(output.getvalue()), calls

    def test_paraphrase_reaches_real_final_audit_code_with_raw_and_relations(self):
        rec, rows, policy, packets = fixture()
        _, claims, validation = final_audit.claim_validator.build_and_validate(rec, packets)
        self.assertEqual(validation['status'], 'pass', validation)
        self.assertEqual(claims['claims'][0]['claim_kind'], 'grounded_explanation')
        result, calls = self.run_final(rec, rows, policy)
        self.assertEqual(len(calls), 1, result)
        self.assertEqual(result['orchestration_decision']['status'], 'accepted', result)
        prompt = calls[0]['messages'][1]['content']
        for text in (RAW, PARAPHRASE, 'R1', 'R2', 'Q_conditions', 'E1', 'ev1'):
            self.assertIn(text, prompt)
        self.assertIn('explanation_checks', calls[0]['format']['required'])

    def test_plain_literal_mode_still_rejects_unbound_paraphrase(self):
        rec, _, _, packets = fixture()
        rec.pop('workflow_reasoning')
        _, _, validation = final_audit.claim_validator.build_and_validate(rec, packets)
        self.assertIn('value_not_in_evidence', {f['code'] for f in validation['failures']})

    def test_fake_literal_speech_rejected_before_model(self):
        rec, rows, policy, _ = fixture('窓口では「予約不要です」と案内します。')
        result, calls = self.run_final(rec, rows, policy)
        self.assertEqual(calls, [])
        self.assertEqual(result['orchestration_decision']['status'], 'rejected')

    def test_exact_literal_quote_allowed_with_paraphrase(self):
        rec, rows, policy, _ = fixture('「予約票がない場合」は、窓口担当から確認係に連絡します。')
        result, calls = self.run_final(rec, rows, policy)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['orchestration_decision']['status'], 'accepted', result)

    def test_graph_or_quote_or_draft_tampering_blocks_before_model(self):
        for kind in ('quote', 'edge', 'draft', 'query', 'evidence', 'self_pass_only', 'input_hash', 'node_ids'):
            with self.subTest(kind=kind):
                rec, rows, policy, _ = fixture()
                trace = rec['workflow_reasoning']
                if kind == 'quote': trace['source_graph']['nodes'][0]['quote'] = '不存在の担当'
                elif kind == 'edge': trace['source_graph']['edges'][0]['target'] = 'N2'
                elif kind == 'draft': trace['draft']['items'][0]['text'] = '別の説明'
                elif kind == 'query': rec['query'] += ' 2025年度版について'
                elif kind == 'evidence': trace['draft']['items'][0]['supporting_packet_ids'] = ['E999']
                elif kind == 'self_pass_only': trace.pop('review')
                elif kind == 'input_hash': trace['model_calls'][1]['input_sha256'] = 'fake'
                else: trace['model_calls'][1]['node_ids'] = []
                result, calls = self.run_final(rec, rows, policy)
                self.assertEqual(calls, [], kind)
                self.assertEqual(result['orchestration_decision']['status'], 'rejected')

    def test_omitted_fresh_source_cannot_escape_by_declared_bundle(self):
        rec, rows, policy, _ = fixture()
        rows.append(record('ev2', {'row_index': 7}, '例外の場合は必ず停止する。'))
        policy['eligible_evidence_ids'].add('ev2')
        policy['source_graph'] = source_graph(rows)
        result, calls = self.run_final(rec, rows, policy)
        self.assertEqual(calls, [])
        self.assertEqual(result['workflow_retrieval_validation']['status'], 'blocked')

    def test_latest_year_cannot_be_forged_in_question_graph(self):
        rec, rows, policy, _ = fixture()
        rec['query'] += ' 2025年度'
        helper = final_audit.claim_validator.load_workflow_contract()
        trace = rec['workflow_reasoning']
        trace['question_graph'] = helper.graph.build_question_graph(rec['query'], rec['question_plan'])
        trace['matches'] = helper.graph.build_relation_matches(trace['question_graph'], trace['source_graph'])
        result, calls = self.run_final(rec, rows, policy)
        self.assertEqual(calls, [])
        self.assertEqual(result['workflow_retrieval_validation']['status'], 'blocked')

    def test_per_relation_rejection_overrides_blanket_verified(self):
        rec, rows, policy, _ = fixture('予約票がある場合に連絡します。')
        check = review_for(rec)
        check['relation_checks'][1].update(verdict='fail', reason='条件が逆転しています')
        result, calls = self.run_final(rec, rows, policy, check)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['answer']['answer_mode'], 'insufficient')
        self.assertEqual(result['orchestration_decision']['status'], 'rejected')

    def test_missing_explanation_relation_or_requirement_check_fails_closed(self):
        for key in ('explanation_checks', 'relation_checks', 'question_requirement_checks'):
            rec, rows, policy, _ = fixture()
            check = review_for(rec); check[key].pop()
            result, calls = self.run_final(rec, rows, policy, check)
            self.assertEqual(len(calls), 1)
            self.assertEqual(result['answer']['answer_mode'], 'insufficient', key)

    def test_unfulfilled_original_requirement_rejects_complete_answer(self):
        rec, rows, policy, _ = fixture()
        check = review_for(rec)
        check['question_requirement_checks'][0].update(verdict='fail', reason='手順が抜けています')
        result, _ = self.run_final(rec, rows, policy, check)
        self.assertEqual(result['answer']['answer_status'], 'insufficient')

    def test_truncated_but_valid_json_never_passes(self):
        rec, rows, policy, _ = fixture()
        result, calls = self.run_final(rec, rows, policy, done_reason='length')
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['answer']['answer_status'], 'insufficient')

    def test_provisional_source_cannot_use_explanation_exception(self):
        rec, rows, policy, packets = fixture()
        packets[0]['text'] = '[暫定読取]' + packets[0]['text']
        _, _, validation = final_audit.claim_validator.build_and_validate(rec, packets)
        self.assertEqual(validation['status'], 'blocked')


if __name__ == '__main__':
    unittest.main()
