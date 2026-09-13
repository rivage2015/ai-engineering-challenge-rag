import json
import unittest
from unittest import mock
from test_question_graph_executor import answer


class WorkflowAuditGuidanceTests(unittest.TestCase):
    def test_workflow_budget_and_explicit_condition_instructions(self):
        for claim, budget in [('受付の手順', 1600), ('担当者の名前', 450)]:
            item = {'item_id': 'F1', 'label': claim, 'required_claim': claim}
            value = {'item_id': 'F1', 'verdict': 'supported', 'supported_value': '合成の値',
                     'supporting_packet_ids': ['E1'], 'competing_packet_ids': [],
                     'reason_code': 'none', 'defect': '', 'missing_information': []}
            with mock.patch.object(answer.base, 'post_json', return_value={
                    'message': {'content': json.dumps(value)}}) as call:
                answer.audit_field('unused', item, 'synthetic', {f'E{i}': f'ev_{i}' for i in range(1, 9)}, 1)
            payload = call.call_args.args[1]
            self.assertEqual(payload['options']['num_predict'], budget)
            self.assertEqual(payload['format']['properties']['supporting_packet_ids']['maxItems'],
                             8 if claim == '受付の手順' else 4)
            self.assertIn('条件ごとに転記', payload['messages'][0]['content'])
            self.assertIn('出典メタデータ', payload['messages'][0]['content'])
