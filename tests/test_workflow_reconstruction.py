"""Synthetic reconstruction boundary, not source authority or runtime E2E."""
import copy
import unittest
from test_workflow_step_selection import fixture, selector


class WorkflowReconstructionTests(unittest.TestCase):
    def setUp(self):
        self.records = fixture()
        self.scope = dict(document_id='doc_synthetic', relative_path='synthetic.xlsx',
            sheet_name='受付', start_row=5, end_row=14, content_header_id='header',
            eligible_evidence_ids={r['evidence_id'] for r in self.records})
        self.candidate = selector.select_observed_steps(self.records, **self.scope)

    def check(self, candidate=None, records=None, **scope):
        return selector.validate_observed_steps(
            self.candidate if candidate is None else candidate,
            self.records if records is None else records, **{**self.scope, **scope})

    def assert_failure(self, result):
        self.assertEqual(result['status'], 'fail')
        self.assertEqual(result['coverage'], 'unknown')
        self.assertTrue(result['reason'])

    def test_normal_full_sequence_and_condition_pass_without_mutation(self):
        before = copy.deepcopy((self.candidate, self.records, self.scope))
        self.assertEqual(self.check(), {'status':'pass',
            'reason':'selection_reconstruction_match', 'coverage':'unknown'})
        self.assertEqual([s['ordinal'] for s in self.candidate['steps']], [1,2,3])
        self.assertIn('確認前に案内しません。', self.candidate['steps'][1]['paragraphs'][1]['text'])
        self.assertEqual(before, (self.candidate, self.records, self.scope))

    def test_reordered_records_and_json_key_order_are_equivalent(self):
        reordered = dict(reversed(list(self.candidate.items())))
        self.assertEqual(self.check(reordered, list(reversed(self.records)))['status'], 'pass')

    def test_step_deletion_and_reordering_fail(self):
        for action in ['delete', 'reverse']:
            c = copy.deepcopy(self.candidate)
            if action == 'delete':
                del c['steps'][1]
                c['selected_evidence_ids'] = ['header','h1','p1','p1b','h3','p3']
            else:
                c['steps'].reverse()
            self.assert_failure(self.check(c))

    def test_condition_text_id_and_position_tampering_fail(self):
        for key,value in [('text','先に案内します。'),('evidence_id','p2'),('cell','D11')]:
            c = copy.deepcopy(self.candidate)
            c['steps'][1]['paragraphs'][1][key] = value
            self.assert_failure(self.check(c))
        c = copy.deepcopy(self.candidate)
        c['steps'][1]['paragraphs'].pop()
        c['selected_evidence_ids'].remove('condition')
        self.assert_failure(self.check(c))

    def test_status_coverage_extra_field_and_numeric_type_tampering_fail(self):
        for key,value in [('status','ready'),('coverage','complete'),('extra','accepted')]:
            c = copy.deepcopy(self.candidate)
            c[key] = value
            self.assert_failure(self.check(c))
        for value in [True,1.0]:
            c = copy.deepcopy(self.candidate)
            c['steps'][0]['ordinal'] = value
            self.assert_failure(self.check(c))

    def test_shrunk_candidate_must_use_external_original_scope(self):
        c = selector.select_observed_steps(self.records, **{**self.scope,'end_row':11})
        self.assertEqual(c['status'],'candidate')
        self.assertEqual(len(c['steps']),2)
        self.assert_failure(self.check(c))
        self.assert_failure(self.check(sheet_name='配膳'))
        self.assert_failure(self.check(content_header_id='reference'))

    def test_source_text_change_or_ineligible_condition_fails(self):
        r = copy.deepcopy(self.records)
        r[6]['text'] += '\n新しい条件'
        self.assert_failure(self.check(records=r))
        self.assert_failure(self.check(eligible_evidence_ids=self.scope['eligible_evidence_ids']-{'condition'}))

    def test_hold_is_not_verified(self):
        r = [x for x in self.records if x['evidence_id'] != 'p3']
        held = selector.select_observed_steps(r, **self.scope)
        self.assertEqual(held['status'],'hold')
        self.assert_failure(self.check(held,r))

    def test_malformed_non_json_and_cyclic_candidates_fail(self):
        for c in [[], 'candidate', 1, True, {'x':float('nan')}, {1:'x'}, {'x':(1,2)}]:
            self.assert_failure(self.check(c))
        self.assert_failure(selector.validate_observed_steps(None,self.records,**self.scope))
        c = {}; c['x'] = c
        self.assert_failure(self.check(c))
        c = {}; cursor = c
        for _ in range(40):
            cursor['x'] = {}; cursor = cursor['x']
        self.assert_failure(self.check(c))
        self.assert_failure(self.check({'x':[0]*100001}))
        self.assert_failure(self.check({'x':'x'*4000001}))


if __name__ == '__main__':
    unittest.main()
