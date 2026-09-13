import json
import unittest
from test_question_graph_executor import answer, evidence


class CredentialBoundaryTests(unittest.TestCase):
    def test_plain_and_json_encoded_credentials_never_enter_context(self):
        for raw in ['PASS: synthetic-secret', '説明\npassword: synthetic-secret']:
            for value in [raw, json.dumps(raw)]:
                with self.subTest(value=value):
                    context, ids = answer.compact_context([evidence('secret', value)])
                    self.assertEqual(context, '')
                    self.assertEqual(ids, {})
                    with self.assertRaises(ValueError):
                        answer.require_graph_primary_coverage(
                            {'graph_primary_evidence_ids': ['secret']}, ids)

    def test_business_caution_is_preserved(self):
        context, ids = answer.compact_context([
            evidence('safe', json.dumps('確認は担当スタッフに引き継ぐ。'))])
        self.assertEqual(ids, {'E1': 'safe'})
        self.assertTrue(context)
