"""Pure candidate selection gold. Fictional wording, not DAWN operational advice."""
import copy
import importlib.util
from pathlib import Path
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'distribution/macos-local-memory/engine/workflow_step_selection.py'
spec = importlib.util.spec_from_file_location('workflow_step_selection_test', MODULE)
selector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(selector)


def row(eid, locator, text, sheet='受付'):
    return {'evidence_id': eid, 'document_id': 'doc_synthetic',
            'relative_path': 'synthetic.xlsx', 'locator': {'sheet_name': sheet, **locator}, 'text': text}


def fixture():
    return [row('header', {'cell': 'C2'}, 'スクリプト'),
        row('h1', {'row_index': 5}, '受付: 1.ご挨拶'),
        row('p1', {'cell': 'C6'}, '合成例：ようこそ。'),
        row('p1b', {'cell': 'C7'}, '合成例：ご用件を確認します。'),
        row('h2', {'row_index': 9}, '受付: 2.予約確認'),
        row('p2', {'cell': 'C10'}, '合成例：予約票をご確認ください。'),
        row('condition', {'cell': 'C11'}, 'もし予約票がなければ、担当の青木に確認します。\n確認前に案内しません。'),
        row('h3', {'row_index': 13}, '受付: 3.案内'),
        row('p3', {'cell': 'C14'}, '合成例：担当者へ引き継ぎます。'),
        row('reference', {'cell': 'D11'}, '無関係な参考情報'),
        row('elsewhere', {'cell': 'C11'}, '別業務の手順', sheet='配膳')]


class WorkflowStepSelectionTests(unittest.TestCase):
    def select(self, records=None, **overrides):
        records = fixture() if records is None else records
        scope = dict(document_id='doc_synthetic', relative_path='synthetic.xlsx', sheet_name='受付',
            start_row=5, end_row=14, content_header_id='header',
            eligible_evidence_ids={r['evidence_id'] for r in records})
        scope.update(overrides)
        return selector.select_observed_steps(records, **scope)

    def assert_hold(self, result):
        self.assertEqual(result['status'], 'hold')
        self.assertEqual(result['steps'], [])
        self.assertEqual(result['selected_evidence_ids'], [])
        self.assertTrue(result['reason'])

    def test_three_steps_all_paragraphs_condition_and_exact_source_order(self):
        result = self.select()
        self.assertEqual(result['status'], 'candidate')
        self.assertEqual(result['coverage'], 'unknown')
        self.assertEqual(result['order_kind'], 'source_order')
        self.assertEqual(result['selected_evidence_ids'],
            ['header', 'h1', 'p1', 'p1b', 'h2', 'p2', 'condition', 'h3', 'p3'])
        self.assertEqual([s['ordinal'] for s in result['steps']], [1, 2, 3])
        self.assertEqual(result['steps'][1]['paragraphs'][1], {
            'evidence_id': 'condition', 'cell': 'C11',
            'text': 'もし予約票がなければ、担当の青木に確認します。\n確認前に案内しません。'})
        self.assertEqual(result['steps'][0]['heading_text'], '受付: 1.ご挨拶')

    def test_input_shuffle_does_not_change_observed_order(self):
        result = self.select(list(reversed(fixture())))
        self.assertEqual(result['status'], 'candidate')
        self.assertEqual(result, self.select())

    def test_body_on_heading_row_is_preserved_in_its_own_step(self):
        records = fixture() + [row('same_row', {'cell': 'C9'}, '合成例：ご予約はありますか？')]
        result = self.select(records)
        self.assertEqual(result['status'], 'candidate')
        self.assertEqual([p['evidence_id'] for p in result['steps'][1]['paragraphs']],
                         ['same_row', 'p2', 'condition'])
        self.assertNotIn('same_row', [p['evidence_id'] for p in result['steps'][0]['paragraphs']])
        self.assertEqual(result, self.select(list(reversed(records))))

    def test_same_row_body_cannot_bypass_eligibility(self):
        records = fixture() + [row('same_row', {'cell': 'C9'}, '合成例：確認事項')]
        self.assert_hold(self.select(records, eligible_evidence_ids={r['evidence_id'] for r in fixture()}))

    def test_step_with_only_same_row_body_is_not_missing(self):
        records = [r for r in fixture() if r['evidence_id'] != 'p3']
        records.append(row('same_row', {'cell': 'C13'}, '合成例：引き継ぎます。'))
        result = self.select(records)
        self.assertEqual(result['status'], 'candidate')
        self.assertEqual([p['evidence_id'] for p in result['steps'][2]['paragraphs']], ['same_row'])

    def test_changed_name_and_multiline_text_are_preserved_without_interpretation(self):
        records = fixture()
        records[6]['text'] = 'もし予約票がなければ、担当の森に確認します。\n確認前に案内しません。'
        before = copy.deepcopy(records)
        result = self.select(records)
        self.assertEqual(result['status'], 'candidate')
        self.assertEqual(result['steps'][1]['paragraphs'][1]['text'], records[6]['text'])
        self.assertEqual(records, before)

    def test_gap_duplicate_and_reversed_ordinals_hold(self):
        for value in ['受付: 3.予約確認', '受付: 1.予約確認', '受付: 0.予約確認']:
            with self.subTest(value=value):
                records = fixture()
                records[4]['text'] = value
                self.assert_hold(self.select(records))

    def test_missing_step_body_does_not_return_partial_candidate(self):
        self.assert_hold(self.select([r for r in fixture() if r['evidence_id'] != 'p3']))

    def test_eligibility_is_required_for_header_heading_and_every_body(self):
        for missing in ['header', 'h2', 'condition']:
            with self.subTest(missing=missing):
                self.assert_hold(self.select(eligible_evidence_ids={r['evidence_id'] for r in fixture()} - {missing}))

    def test_duplicate_id_cell_and_row_unit_hold(self):
        for index in [0, 1, 2]:
            with self.subTest(index=index):
                records = fixture()
                records.append(copy.deepcopy(records[index]))
                self.assert_hold(self.select(records))
                records[-1]['evidence_id'] = 'duplicate_coordinate'
                self.assert_hold(self.select(records))

    def test_malformed_target_native_locators_hold(self):
        for locator in [{'cell': 'C0'}, {'cell': 'XFE6'}, {'cell': 'C1048577'},
                {'row_index': True}, {'row_index': -1}]:
            with self.subTest(locator=locator):
                records = fixture() + [row('bad', locator, 'unreadable')]
                self.assert_hold(self.select(records))

    def test_invalid_bounds_and_record_budget_hold(self):
        for bounds in [{'start_row': True}, {'end_row': 4}, {'end_row': 1048577}, {'start_row': 0}]:
            with self.subTest(bounds=bounds):
                self.assert_hold(self.select(**bounds))
        self.assert_hold(self.select(fixture() * 1000))

    def test_wrong_header_scope_and_header_after_start_hold(self):
        for overrides in [{'content_header_id': 'elsewhere'}, {'content_header_id': 'h1'},
                {'content_header_id': 'p1'}, {'sheet_name': '配膳'}, {'relative_path': 'other.xlsx'}]:
            with self.subTest(overrides=overrides):
                self.assert_hold(self.select(**overrides))

    def test_outside_bound_and_other_column_do_not_become_paragraphs(self):
        records = fixture() + [row('outside', {'cell': 'C15'}, '範囲外')]
        result = self.select(records)
        self.assertEqual(result['status'], 'candidate')
        self.assertNotIn('outside', result['selected_evidence_ids'])
        self.assertNotIn('reference', result['selected_evidence_ids'])
        self.assertNotIn('elsewhere', result['selected_evidence_ids'])

    def test_invalid_types_and_eligibility_input_hold(self):
        for overrides in [{'start_row': '5'}, {'end_row': None}, {'document_id': ''},
                {'sheet_name': []}, {'eligible_evidence_ids': None},
                {'eligible_evidence_ids': ['header']}, {'eligible_evidence_ids': {True}}]:
            with self.subTest(overrides=overrides):
                self.assert_hold(self.select(**overrides))
        self.assert_hold(self.select([{'evidence_id': 'broken'}]))

    def test_duplicate_outside_bounds_still_holds(self):
        self.assert_hold(self.select(fixture() + [row('out1', {'cell': 'C100'}, 'a'),
                                                 row('out2', {'cell': 'C100'}, 'a')]))

    def test_character_budget_boundary_never_truncates(self):
        records = fixture()
        remaining = 1000000 - sum(len(r['text']) for r in records)
        records.append(row('large_elsewhere', {'cell': 'C20'}, 'x' * remaining, sheet='別'))
        self.assertEqual(self.select(records)['status'], 'candidate')
        records[-1]['text'] += 'x'
        self.assert_hold(self.select(records))

    def test_empty_or_no_headings_hold(self):
        self.assert_hold(self.select([]))
        self.assert_hold(self.select([r for r in fixture() if not r['evidence_id'].startswith('h') or r['evidence_id'] == 'header']))


if __name__ == '__main__':
    unittest.main()
