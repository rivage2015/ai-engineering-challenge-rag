"""Audit counterexamples: changes invisible in the projected step text."""
import copy
import unittest
from test_workflow_step_selection import fixture, selector


class WorkflowInputBindingTests(unittest.TestCase):
    def setUp(self):
        self.records = fixture()
        self.scope = dict(document_id='doc_synthetic', relative_path='synthetic.xlsx',
            sheet_name='受付', start_row=5, end_row=14, content_header_id='header',
            eligible_evidence_ids={r['evidence_id'] for r in self.records})
        self.candidate = selector.select_observed_steps(self.records, **self.scope)

    def test_header_only_text_change_invalidates_candidate(self):
        changed = copy.deepcopy(self.records)
        changed[0]['text'] = '別の内容列'
        actual = selector.validate_observed_steps(self.candidate,changed,**self.scope)
        self.assertEqual(actual['status'],'fail')

    def test_empty_scope_extension_invalidates_candidate(self):
        actual = selector.validate_observed_steps(self.candidate,self.records,
            **{**self.scope,'end_row':15})
        self.assertEqual(actual['status'],'fail')


if __name__ == '__main__':
    unittest.main()
