"""Audit finding regression: multiline native headings must not merge steps."""
import unittest
from test_workflow_step_selection import fixture, selector


class WorkflowMultilineHeadingTests(unittest.TestCase):
    def select(self, records):
        return selector.select_observed_steps(records, document_id='doc_synthetic',
            relative_path='synthetic.xlsx', sheet_name='受付', start_row=5, end_row=14,
            content_header_id='header', eligible_evidence_ids={r['evidence_id'] for r in records})

    def test_last_heading_newline_does_not_absorb_third_step_into_second(self):
        records = fixture()
        records[7]['text'] = '受付: 3.案内\n補足'
        result = self.select(records)
        self.assertEqual(result['status'], 'candidate')
        self.assertEqual([s['ordinal'] for s in result['steps']], [1, 2, 3])
        self.assertEqual(result['steps'][2]['heading_text'], records[7]['text'])
        self.assertEqual([p['evidence_id'] for p in result['steps'][1]['paragraphs']], ['p2', 'condition'])
        self.assertEqual([p['evidence_id'] for p in result['steps'][2]['paragraphs']], ['p3'])

    def test_middle_heading_newline_does_not_create_false_number_gap(self):
        records = fixture()
        records[4]['text'] = '受付: 2.予約確認\r\n補足'
        result = self.select(records)
        self.assertEqual(result['status'], 'candidate')
        self.assertEqual([s['ordinal'] for s in result['steps']], [1, 2, 3])
        self.assertEqual(result['steps'][1]['heading_text'], records[4]['text'])
        self.assertEqual([p['evidence_id'] for p in result['steps'][1]['paragraphs']], ['p2', 'condition'])


if __name__ == '__main__':
    unittest.main()
