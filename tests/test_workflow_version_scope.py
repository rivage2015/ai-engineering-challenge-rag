"""Registered-edition guard: synthetic snapshots only, no model or source reads."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from test_question_graph_executor import answer, ENGINE, load_module


resolver = load_module('workflow_scope_resolver_test', ENGINE / 'document_version_resolver.py')


def inventory_record(path, *, status='observed', mtime=2, token='a'):
    return {'relative_path': path, 'kind': 'file', 'read_status': status,
            'sha256': token * 64 if status == 'observed' else None,
            'size_bytes': 1, 'mtime_ns': mtime, 'birthtime_ns': 1}


class RegisteredVersionScopeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='workflow-version-scope-')
        self.addCleanup(self.temporary.cleanup)
        self.generation = Path(self.temporary.name) / ('generation-' + 'a' * 32)
        self.paths = self.generation / '01-path'
        self.paths.mkdir(parents=True)
        self.index = self.generation / 'safe-answer-index.sqlite3'
        self.index.touch()
        self.inventory = self.paths / 'path-source-inventory.jsonl'
        self.graph = self.paths / 'document-version-graph.json'
        self.decisions = self.paths / 'document-version-decisions.snapshot.json'

    def snapshot(self, inventory, decisions=None):
        self.inventory.write_text(''.join(json.dumps(item) + '\n' for item in inventory))
        self.decisions.write_text(json.dumps({'schema_version': '1.0', 'decisions': decisions or []}))
        resolver.build(self.inventory, self.graph, self.decisions)
        report = resolver.attest(self.graph, self.inventory, decision_mode='snapshot',
            decisions_path=self.decisions,
            expected_decisions_sha256=hashlib.sha256(self.decisions.read_bytes()).hexdigest())
        self.assertEqual(report['status'], 'PASS')
        return {'index_purpose': 'safe_answer', 'document_version_graph': report['document_version_graph']}

    def run_scope(self, inventory, indexed=None, query='窓口の仕事の手順を教えて', decisions=None):
        meta = self.snapshot(inventory, decisions)
        records = [{'relative_path': path} for path in
                   (indexed if indexed is not None else [inventory[0]['relative_path']])]
        with mock.patch.object(answer, 'current_tokyo_date', return_value='2026-09-19'):
            return answer.resolve_registered_version_scope(self.index, meta, records, query)

    def dated_decision(self, inventory, selected):
        candidates = [resolver.candidate(item) for item in inventory]
        key = resolver.family_key(inventory[0]['relative_path'])
        candidates.sort(key=lambda item: item['relative_path'].encode('utf-8'))
        revision = {'generation': self.generation.name, 'source_scope_sha256': 'b' * 64,
            'graph_sha256': 'c' * 64, 'graph_file_sha256': 'd' * 64,
            'inventory_sha256': 'e' * 64, 'candidate_set_sha256': resolver.candidate_set_hash(candidates),
            'decisions_sha256': None, 'resolver_version': resolver.RESOLVER_VERSION}
        chosen = next(item for item in candidates if item['relative_path'] == selected)
        return resolver.prepare_dated_consent(key, candidates, {
            'relation': 'same_work_revisions', 'selected_relative_path': selected,
            'selected_source_sha256': chosen['source_sha256'],
            'current_applicability_confirmed': True, 'allow_ingest_index_answer': True},
            displayed_revision=revision, current_revision=revision,
            actor='synthetic-human', decided_at='2026-09-19T00:00:00+09:00')

    def test_single_registered_2026_edition_is_not_claimed_latest(self):
        result = self.run_scope([inventory_record('窓口手順2026.xlsx')])
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['allowed_relative_paths'], ['窓口手順2026.xlsx'])
        self.assertEqual(result['referenced_editions'][0]['explicit_years'], [2026])
        self.assertFalse(result['latest_confirmed'])
        self.assertFalse(result['live_inventory_checked'])

    def test_explicit_requested_year_is_not_replaced(self):
        result = self.run_scope([inventory_record('窓口手順2026.xlsx')], query='2023年度の窓口手順')
        self.assertEqual(result['reason'], 'requested_edition_unavailable')
        self.assertEqual(result['allowed_relative_paths'], [])

    def test_matching_full_width_requested_year(self):
        result = self.run_scope([inventory_record('窓口手順2026.xlsx')], query='２０２６年度の窓口手順')
        self.assertEqual(result['status'], 'ready')

    def test_relative_time_and_comparison_are_not_defaulted_to_current(self):
        for query in ('昨年の窓口手順', '当時の窓口手順', '2023年と2026年を比較', '旧版の手順',
                      '2026年1月の窓口手順', '2026-01-01時点の手順'):
            with self.subTest(query=query):
                result = self.run_scope([inventory_record('窓口手順2026.xlsx')], query=query)
                self.assertEqual(result['reason'], 'explicit_temporal_scope_needs_confirmation')

    def test_new_unindexed_candidate_without_query_words_prevents_old_fallback(self):
        result = self.run_scope([inventory_record('2023/report.xlsx'), inventory_record('2026/report.xlsx')],
                                indexed=['2023/report.xlsx'])
        self.assertEqual(result['status'], 'hold')
        self.assertEqual(result['allowed_relative_paths'], [])

    def test_unreadable_candidate_is_not_lost_by_resolver_candidate_filter(self):
        result = self.run_scope([inventory_record('規程2023.xlsx'),
                                inventory_record('規程2026.xlsx', status='unresolved')])
        self.assertEqual(result['held_families'][0]['reason'], 'known_family_member_unreadable')
        self.assertEqual(result['allowed_relative_paths'], [])

    def test_old_copy_new_mtime_does_not_reverse_human_adoption(self):
        inventory = [inventory_record('規程2023.xlsx', mtime=900),
                     inventory_record('規程2026.xlsx', mtime=2, token='b')]
        decision = self.dated_decision(inventory, '規程2026.xlsx')
        result = self.run_scope(inventory, indexed=['規程2026.xlsx'], decisions=[decision])
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['allowed_relative_paths'], ['規程2026.xlsx'])

    def test_stale_human_adoption_with_new_candidate_is_held(self):
        inventory = [inventory_record('規程2023.xlsx'), inventory_record('規程2025.xlsx', token='b')]
        decision = self.dated_decision(inventory, '規程2025.xlsx')
        result = self.run_scope(inventory + [inventory_record('規程2026.xlsx', token='c')],
                                indexed=['規程2025.xlsx'], decisions=[decision])
        self.assertEqual(result['status'], 'hold')
        self.assertEqual(result['allowed_relative_paths'], [])

    def test_selected_new_edition_must_actually_be_indexed(self):
        inventory = [inventory_record('規程2023.xlsx'), inventory_record('規程2026.xlsx', token='b')]
        result = self.run_scope(inventory, indexed=['規程2023.xlsx'],
                               decisions=[self.dated_decision(inventory, '規程2026.xlsx')])
        self.assertEqual(result['held_families'][0]['reason'], 'selected_edition_not_indexed')

    def test_single_draft_historical_and_future_editions_are_not_promoted(self):
        for name in ('規程2026草稿draft.xlsx', '規程2026旧版.xlsx', '規程2027.xlsx',
                     '規程20261201.xlsx', '規程2026年12月1日.xlsx'):
            with self.subTest(name=name):
                result = self.run_scope([inventory_record(name)])
                self.assertEqual(result['status'], 'hold')

    def test_separate_older_supplement_is_not_discarded_by_global_max_year(self):
        inventory = [inventory_record('業務2026.xlsx'), inventory_record('共通補足2021.pdf')]
        result = self.run_scope(inventory, indexed=[item['relative_path'] for item in inventory])
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(set(result['allowed_relative_paths']), {'業務2026.xlsx', '共通補足2021.pdf'})

    def test_separate_safe_supplement_remains_in_trace_when_other_family_held(self):
        inventory = [inventory_record('業務2023.xlsx'), inventory_record('業務2026.xlsx', status='unresolved'),
                     inventory_record('共通補足2021.pdf')]
        result = self.run_scope(inventory, indexed=['業務2023.xlsx', '共通補足2021.pdf'])
        self.assertEqual(result['status'], 'hold')
        self.assertEqual(result['allowed_relative_paths'], ['共通補足2021.pdf'])

    def test_undated_text_is_registered_only_not_fabricated_year(self):
        result = self.run_scope([inventory_record('共通補足.txt')])
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['referenced_editions'][0]['explicit_years'], [])
        self.assertFalse(result['latest_confirmed'])

    def test_missing_binding_is_unavailable_not_latest(self):
        result = answer.resolve_registered_version_scope(self.index, {}, [], '手順は？')
        self.assertEqual(result['status'], 'unavailable')
        self.assertFalse(result['latest_confirmed'])

    def test_foreign_binding_path_is_not_followed(self):
        meta = self.snapshot([inventory_record('規程2026.xlsx')])
        meta['document_version_graph']['path'] = '/never/read/foreign.json'
        result = answer.resolve_registered_version_scope(self.index, meta, [], '手順は？')
        self.assertEqual(result['reason'], 'registered_version_binding_invalid')

    def test_changed_inventory_fails_attestation(self):
        meta = self.snapshot([inventory_record('規程2026.xlsx')])
        self.inventory.write_text(self.inventory.read_text().replace('2026', '2023'))
        result = answer.resolve_registered_version_scope(self.index, meta, [], '手順は？')
        self.assertEqual(result['reason'], 'registered_version_attestation_failed')

    def test_symlinked_artifact_is_refused(self):
        meta = self.snapshot([inventory_record('規程2026.xlsx')])
        moved = self.paths / 'other-inventory.jsonl'
        self.inventory.rename(moved)
        self.inventory.symlink_to(moved)
        result = answer.resolve_registered_version_scope(self.index, meta, [], '手順は？')
        self.assertEqual(result['reason'], 'registered_version_artifact_invalid')

    def test_indexed_path_must_belong_to_attested_inventory(self):
        result = self.run_scope([inventory_record('規程2026.xlsx')], indexed=['無関係.xlsx'])
        self.assertEqual(result['reason'], 'registered_version_index_not_in_inventory')


if __name__ == '__main__':
    unittest.main()
