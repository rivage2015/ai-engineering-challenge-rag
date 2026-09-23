"""Routing hints never discharge the user's complete requirement."""

import copy
import unittest

from test_intent_requirement_graph import graph
from test_record_lookup_question_graph import qeg


class IntentWorkMappingTests(unittest.TestCase):
    def plan(self, requirements):
        return graph.plan_from_contract({
            'version': 1,
            'question': '2026年度の資料で案内してください。',
            'goal': '対象を一つに確定し、条件と例外も説明する。',
            'requirements': requirements,
            'revision': {'generation': 'frozen-test'},
        })

    def build(self, plan):
        return graph.build_work_mapping(plan, qeg.RECORD_LOOKUP_FIELD_ALIASES)

    def test_compound_requirement_keeps_whole_and_two_candidates(self):
        plan = self.plan(['担当者と単価を確認し、お客様への案内も説明する'])
        before = copy.deepcopy(plan)
        mapping = self.build(plan)
        self.assertEqual(plan, before)
        self.assertEqual([j['work_id'] for j in mapping['jobs']],
                         ['R1:whole', 'R1:field:owner', 'R1:field:unit_cost'])
        self.assertEqual(mapping['jobs'][0]['text'], plan['intent_graph']['requirements'][0])
        self.assertTrue(mapping['jobs'][0]['required'])
        self.assertTrue(all(j['satisfies_requirement'] is False for j in mapping['jobs'][1:]))
        self.assertFalse(mapping['requirements_checked'])

    def test_unknown_relation_not_dropped_or_forced_into_actor(self):
        mapping = self.build(self.plan(['開始時刻と案内文を知りたい']))
        self.assertEqual(len(mapping['jobs']), 1)
        self.assertEqual(mapping['jobs'][0]['status'], 'requires_semantic_matching')

    def test_negated_field_is_only_hint_not_answer_requirement(self):
        mapping = self.build(self.plan(['担当者ではなく注意事項を知りたい']))
        hint = mapping['jobs'][1]
        self.assertEqual(hint['status'], 'candidate_not_semantically_validated')
        self.assertFalse(hint['satisfies_requirement'])
        self.assertNotIn('required', hint)

    def test_shared_field_keeps_distinct_subject_requirements(self):
        mapping = self.build(self.plan(['Aの担当者', 'Bの担当者']))
        hints = [j for j in mapping['jobs'] if j['kind'] == 'record_field_candidate']
        self.assertEqual([j['requirement_id'] for j in hints], ['R1', 'R2'])
        self.assertEqual([j['source_text'] for j in hints], ['Aの担当者', 'Bの担当者'])
        self.assertEqual([j['source_pointer'] for j in hints],
                         ['/requirements/0', '/requirements/1'])

    def test_scope_revision_uniqueness_and_relative_time_preserved(self):
        plan = self.plan(['5年前の担当者と当時の単価'])
        mapping = self.build(plan)
        self.assertEqual(mapping['scope']['question'], plan['intent_graph']['question'])
        self.assertEqual(mapping['scope']['goal'], plan['intent_graph']['goal'])
        self.assertFalse(mapping['scope']['partial_answer_allowed'])
        self.assertEqual(mapping['scope']['revision'], {'generation': 'frozen-test'})
        self.assertIn('5年前', mapping['jobs'][0]['text'])
        self.assertNotIn('as_of', mapping['scope'])  # No invented date resolution.

    def test_all_eight_requirements_survive_even_with_duplicate_text(self):
        mapping = self.build(self.plan(['担当者と単価'] * 8))
        self.assertEqual(len(mapping['jobs']), 24)
        self.assertEqual(len({j['work_id'] for j in mapping['jobs']}), 24)

    def test_long_requirement_not_limited_by_display_label(self):
        plan = self.plan(['あ' * 100 + '単価を知りたい'])
        self.assertEqual(self.build(plan)['jobs'][1]['field_name'], 'unit_cost')

    def test_english_word_boundaries_and_unicode(self):
        self.assertEqual(len(self.build(self.plan(['homeowner ownership']))['jobs']), 1)
        self.assertEqual(self.build(self.plan(['ＵＮＩＴ＿ＣＯＳＴ']))['jobs'][1]['field_name'],
                         'unit_cost')

    def test_rebuild_rejects_tampering_and_alias_drift(self):
        plan = self.plan(['担当者と単価'])
        original = self.build(plan)
        graph.validate_work_mapping(plan, original, qeg.RECORD_LOOKUP_FIELD_ALIASES)
        mutations = [lambda m: m['jobs'].pop(),
                     lambda m: m['links'].pop(),
                     lambda m: m['jobs'][1].update(satisfies_requirement=True),
                     lambda m: m['scope'].update(partial_answer_allowed=True),
                     lambda m: m.update(requirements_checked=True),
                     lambda m: m.update(execution_status='executed')]
        for mutate in mutations:
            changed = copy.deepcopy(original)
            mutate(changed)
            with self.assertRaisesRegex(ValueError, 'intent_work_mapping_mismatch'):
                graph.validate_work_mapping(plan, changed, qeg.RECORD_LOOKUP_FIELD_ALIASES)
        with self.assertRaises(ValueError):
            graph.validate_work_mapping(plan, original, {'owner': ['担当者']})

    def test_deterministic_independent_and_no_dispatch(self):
        plan = self.plan(['担当者'])
        mapping = self.build(plan)
        self.assertEqual(mapping, self.build(plan))
        self.assertEqual(mapping['execution_status'], 'mapping_only_not_dispatched')
        mapping['scope']['revision']['generation'] = 'changed'
        self.assertEqual(plan['intent_graph']['revision']['generation'], 'frozen-test')


if __name__ == '__main__':
    unittest.main()
