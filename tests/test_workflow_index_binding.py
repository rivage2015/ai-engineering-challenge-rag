"""No real index, model or network: exercise index-loader/traversal wiring."""
import copy
import hashlib
import importlib.util
from pathlib import Path
import unittest
from unittest import mock
from test_workflow_step_selection import fixture
from test_ordered_section_question_graph import source_graph

path = Path(__file__).resolve().parents[1]/'distribution/macos-local-memory/engine/workflow_index_binding.py'
spec = importlib.util.spec_from_file_location('workflow_binding_test',path)
binding = importlib.util.module_from_spec(spec)
spec.loader.exec_module(binding)


class WorkflowIndexBindingTests(unittest.TestCase):
    def setUp(self):
        self.records = fixture()
        for r in self.records:
            r['observed_sha256'] = hashlib.sha256(r['text'].encode()).hexdigest()
        self.graph = source_graph(self.records)
        self.policy = {'source_graph':self.graph,
            'eligible_evidence_ids':frozenset(r['evidence_id'] for r in self.records)}
        self.scope = dict(document_id='doc_synthetic',relative_path='synthetic.xlsx',
            sheet_name='受付',start_row=5,end_row=14,content_header_id='header')
        self.revision = dict(generation='generation-'+'1'*32,
            generation_path='/synthetic/generation-'+'1'*32,
            decision_snapshot_sha256='a'*64,config_sha256='b'*64)
        self.index = Path(self.revision['generation_path'])/'safe-answer-index.sqlite3'
        self.reader = mock.Mock(return_value=(True,'current',self.revision))
        self.loader = mock.patch.object(binding.base,'load_answer_evidence_records',
            return_value=(self.records,self.policy)).start()
        self.addCleanup(mock.patch.stopall)

    def build(self, **kw):
        return binding.load_bound_workflow(self.index,scope=self.scope,
            expected_revision=self.revision,read_revision=self.reader,**kw)

    def test_full_three_steps_and_all_paths_then_revalidation(self):
        c = self.build()
        self.assertEqual(c['status'],'candidate',c)
        self.assertEqual([s['ordinal'] for s in c['selection']['steps']],[1,2,3])
        paths = c['stored_graph_binding']['evidence_paths']
        self.assertEqual(len(paths),9)
        self.assertTrue(all(p['root_document_id']=='doc_synthetic' for p in paths))
        self.assertIn('確認前に案内しません。',c['selection']['steps'][1]['paragraphs'][1]['text'])
        result = binding.validate_bound_workflow(c,self.index,scope=self.scope,
            expected_revision=self.revision,read_revision=self.reader)
        self.assertEqual(result['status'],'pass')

    def test_pre_read_stale_revision_never_reads_index(self):
        self.reader.return_value=(True,'current',{**self.revision,'config_sha256':'c'*64})
        self.assertEqual(self.build()['status'],'hold')
        self.loader.assert_not_called()

    def test_mid_read_revision_change_holds(self):
        self.reader.side_effect=[(True,'current',self.revision),(False,'changed',None)]
        self.assertEqual(self.build()['status'],'hold')

    def test_index_from_another_generation_is_not_read(self):
        self.index = Path('/synthetic/generation-'+'2'*32)/'safe-answer-index.sqlite3'
        self.assertEqual(self.build()['status'],'hold')
        self.loader.assert_not_called()

    def test_loader_failure_does_not_fallback(self):
        self.loader.side_effect=ValueError('invalid indexed graph')
        self.assertEqual(self.build()['status'],'hold')
        self.loader.assert_called_once()

    def test_missing_or_wrong_document_path_holds(self):
        for wrong in [False,True]:
            with self.subTest(wrong=wrong):
                self.graph['edges'] = source_graph(self.records)['edges']
                edge = next(e for e in self.graph['edges'] if e['to_node_id']=='condition')
                if wrong:
                    self.graph['nodes'].append(dict(node_id='doc_other',node_type='document',status='observed',record_sha256='f'*64))
                    edge['from_node_id']='doc_other'
                else:
                    self.graph['edges'].remove(edge)
                self.assertEqual(self.build()['status'],'hold')

    def test_excluded_condition_cannot_disappear_silently(self):
        self.records[:] = [r for r in self.records if r['evidence_id']!='condition']
        self.graph['eligible_evidence_ids'].remove('condition')
        self.policy['eligible_evidence_ids'] -= {'condition'}
        next(n for n in self.graph['nodes'] if n['node_id']=='condition')['status']='unresolved'
        self.assertEqual(self.build()['status'],'hold')

    def test_observed_text_hash_mismatch_holds(self):
        self.records[6]['text']='changed'
        self.assertEqual(self.build()['status'],'hold')

    def test_candidate_tampering_is_rejected_against_external_scope(self):
        c=self.build()
        self.assertEqual(c['status'],'candidate')
        altered=copy.deepcopy(c)
        altered['selection']['steps'].pop()
        result=binding.validate_bound_workflow(altered,self.index,scope=self.scope,
            expected_revision=self.revision,read_revision=self.reader)
        self.assertEqual(result['status'],'fail')


if __name__=='__main__':
    unittest.main()
