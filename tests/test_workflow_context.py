"""Bounded context capacity checks using only synthetic responses and logs."""
import copy
import io
import json
import unittest
from unittest import mock

import test_workflow_relation_reasoner as relations
import test_workflow_final_audit as final_tests


def usage_response(**overrides):
    response = {'done_reason': 'stop', 'prompt_eval_count': 100, 'eval_count': 50}
    response.update(overrides)
    return response


def log_slice(context=8192, prompt=100, task=42, release_task=None, truncated=0):
    release_task = task if release_task is None else release_task
    return (
        f'slot   operator(): id  0 | task {task} | new prompt, n_ctx_slot = {context}, '
        f'n_keep = 4, task.n_tokens = {prompt}\n'
        f'slot      release: id  0 | task {release_task} | stop processing: '
        f'n_tokens = 149, truncated = {truncated}\n'
    )


class ContextUsageTests(unittest.TestCase):
    def test_complete_usage_is_observed_not_proof_of_full_input(self):
        result = relations.reasoner.context_usage(usage_response(), 8192, 4000)
        self.assertEqual(result['status'], 'observed')
        self.assertEqual(result['remaining_tokens'], 8042)
        self.assertEqual(result['output_budget_headroom'], 4092)

    def test_missing_invalid_negative_or_zero_input_usage_is_unverified(self):
        responses = [{}, {'done_reason': 'stop'},
                     usage_response(prompt_eval_count=0), usage_response(prompt_eval_count=-1),
                     usage_response(eval_count=-1), usage_response(prompt_eval_count='100'),
                     usage_response(eval_count=50.0), usage_response(prompt_eval_count=True),
                     usage_response(eval_count=False), usage_response(done_reason=None),
                     usage_response(done_reason='unknown')]
        for response in responses:
            with self.subTest(response=response):
                result = relations.reasoner.context_usage(response, 8192, 4000)
                self.assertEqual(result['status'], 'unverified')

    def test_valid_json_output_does_not_override_exhaustion(self):
        for response in (usage_response(done_reason='length'),
                         usage_response(prompt_eval_count=8191, eval_count=1),
                         usage_response(prompt_eval_count=8192, eval_count=50)):
            with self.subTest(response=response):
                response['message'] = {'content': '{"complete":true}'}
                self.assertEqual(relations.reasoner.context_usage(response, 8192, 4000)['status'],
                                 'exhausted')

    def test_usage_does_not_mutate_response(self):
        response = usage_response()
        original = copy.deepcopy(response)
        relations.reasoner.context_usage(response, 8192, 4000)
        self.assertEqual(response, original)


class ContextLogTests(unittest.TestCase):
    def test_single_matching_runner_task_verifies_log_slice(self):
        for context in (8192, 16384):
            with self.subTest(context=context):
                report = relations.reasoner.context_log_assessment(log_slice(context=context), context, 100)
                self.assertEqual(report['status'], 'verified', report)

    def test_input_truncation_or_context_shift_is_not_verified(self):
        warnings = (
            'time=2026-09-19 level=WARN msg="truncating input messages which exceed context length"',
            'time=2026-09-19 level=WARN msg="truncating native chat messages which exceed context length"',
            'time=2026-09-19 level=WARN msg="truncating input prompt"',
            'slot   operator(): id  0 | task 42 | slot context shift, n_keep = 4, n_left = 20, n_discard = 10',
        )
        for warning in warnings:
            with self.subTest(warning=warning):
                result = relations.reasoner.context_log_assessment(warning + '\n' + log_slice(), 8192, 100)
                self.assertNotEqual(result['status'], 'verified', result)

    def test_runner_truncation_not_hidden_by_valid_usage(self):
        result = relations.reasoner.context_log_assessment(log_slice(truncated=1), 8192, 100)
        self.assertNotEqual(result['status'], 'verified', result)

    def test_missing_mixed_or_mismatched_log_stays_unverified(self):
        samples = ('', 'unrelated runner output',
                   log_slice().splitlines()[0], log_slice().splitlines()[1],
                   log_slice(context=4096), log_slice(prompt=99), log_slice(release_task=43),
                   log_slice() + log_slice(task=43),
                   log_slice() + 'slot print_timing: id  1 | task 43 | n_gen = 100\n',
                   log_slice() + log_slice().splitlines()[1] + '\n')
        for log in samples:
            with self.subTest(log=log):
                result = relations.reasoner.context_log_assessment(log, 8192, 100)
                self.assertEqual(result['status'], 'unverified', result)


class ContextPropagationTests(unittest.TestCase):
    def test_8k_and_16k_reach_all_three_calls_and_trace(self):
        for context in (8192, 16384):
            with self.subTest(context=context):
                result, calls = relations.ReasonerTests().run_case(context_tokens=context)
                self.assertTrue(result['complete'], result)
                self.assertEqual(result['context_tokens'], context)
                self.assertEqual(len(calls), 3)
                for emitted, recorded, prediction in zip(calls, result['model_calls'], (4000, 2800, 2000)):
                    self.assertEqual(emitted['payload']['options']['num_ctx'], context)
                    self.assertEqual(recorded['num_ctx'], context)
                    self.assertEqual(recorded['num_predict'], prediction)
                    self.assertLessEqual(emitted['timeout'], 180)

    def test_other_context_sizes_rejected_without_model_call(self):
        for context in (4096, 32768, 0, -1, '8192', True, 8192.0):
            with self.subTest(context=context):
                result, calls = relations.ReasonerTests().run_case(context_tokens=context)
                self.assertEqual(calls, [])
                self.assertEqual(result['status'], 'error')
                self.assertFalse(result['complete'])

    def test_unverified_or_exhausted_usage_stops_at_each_stage(self):
        for stage in range(3):
            for metrics in ({}, usage_response(done_reason='length'),
                            usage_response(prompt_eval_count=8191, eval_count=1)):
                with self.subTest(stage=stage, metrics=metrics):
                    replies = copy.deepcopy([relations.GRAPH, relations.DRAFT, relations.REVIEW])
                    seen = []

                    def post(*args):
                        number = len(seen)
                        seen.append(args)
                        response = {'message': {'content': json.dumps(replies[number], ensure_ascii=False)}}
                        response.update(metrics if number == stage else usage_response())
                        return response

                    result, _ = relations.ReasonerTests().run_case(post_json=post)
                    self.assertEqual(len(seen), stage + 1)
                    self.assertEqual(result['status'], 'error')
                    self.assertFalse(result['complete'])
                    self.assertTrue(all(a['verdict'] == 'insufficient' for a in result['audits']))

    def test_workflow_context_reaches_cli_and_final_audit(self):
        for context in (8192, 16384):
            with self.subTest(context=context):
                rec, rows, policy, _ = final_tests.fixture(context_tokens=context)
                result, calls = final_tests.WorkflowFinalAuditTests().run_final(rec, rows, policy)
                self.assertEqual(result['orchestration_decision']['status'], 'accepted', result)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]['options']['num_ctx'], context)

    def test_final_audit_without_usable_usage_fails_closed(self):
        for metrics in ({}, {'prompt_eval_count': '100', 'eval_count': 50},
                        {'prompt_eval_count': 8191, 'eval_count': 1}):
            with self.subTest(metrics=metrics):
                rec, rows, policy, _ = final_tests.fixture()
                result, calls = final_tests.WorkflowFinalAuditTests().run_final(rec, rows, policy, usage=metrics)
                self.assertEqual(len(calls), 1)
                self.assertEqual(result['orchestration_decision']['status'], 'rejected', result)
                self.assertEqual(result['answer']['answer_status'], 'insufficient')

    def test_legacy_audit_does_not_set_context_or_require_usage(self):
        captured = []

        def open_request(request, timeout):
            captured.append(json.loads(request.data))
            return io.BytesIO(json.dumps({'message': {'content': json.dumps({
                'verdict': 'verified', 'reason': '合成監査', 'unsupported_claims': []})}}).encode())

        with mock.patch.object(final_tests.final_audit.LOCAL_HTTP_OPENER, 'open', side_effect=open_request):
            result, _ = final_tests.final_audit.audit('gemma-test', '単純な質問', {'answer': '回答'}, [], 5)
        self.assertEqual(result['verdict'], 'verified')
        self.assertNotIn('num_ctx', captured[0]['options'])


if __name__ == '__main__':
    unittest.main()
