import copy
import json
import unittest
from test_question_graph_executor import answer
from test_workflow_step_selection import fixture
from test_ordered_section_question_graph import source_graph


class WorkflowSourceContextTests(unittest.TestCase):
    def records(self):
        rows = fixture()
        for r in rows:
            r['relative_path'] = 'synthetic2023.xlsx'
        return rows

    def test_scoped_graph_bound_rows_and_safe_cell_fallback(self):
        rows = self.records()
        mixed = copy.deepcopy(rows[1])
        mixed.update(evidence_id='mixed', text=json.dumps('PASS: synthetic-secret'))
        mixed['locator'] = {'sheet_name': '受付', 'row_index': 6}
        rows.append(mixed)
        result = answer.workflow_source_context('2023年の受付の流れ', rows, source_graph(rows))
        ids = [r['evidence_id'] for r in result]
        self.assertIn('p1', ids)
        self.assertNotIn('mixed', ids)
        self.assertNotIn('elsewhere', ids)
        self.assertTrue(all(r['retrieval_source'] == 'validated_workflow_source_order' for r in result))

    def test_no_explicit_year_does_not_assume_latest(self):
        rows = self.records()
        self.assertIsNone(answer.workflow_source_context('受付の流れ', rows, source_graph(rows)))

    def test_ambiguous_document_requires_confirmation(self):
        rows = self.records()
        other = copy.deepcopy(rows)
        for r in other:
            r['document_id'] += '_other'
            r['evidence_id'] += '_other'
        rows += other
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            answer.workflow_source_context('2023年の受付の流れ', rows, source_graph(rows))

    def test_missing_graph_path_is_not_silently_accepted(self):
        rows = self.records()
        graph = source_graph(rows)
        graph['edges'] = []
        with self.assertRaisesRegex(ValueError, 'path_missing'):
            answer.workflow_source_context('2023年の受付の流れ', rows, graph)
