"""No sockets, models or live settings: test the real POST handler in memory."""
import io
import types
import unittest
import urllib.parse
from unittest import mock

from test_dated_hitl_http_e2e import load_server


class IntentTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_server()

    def dispatch(self, route, fields=None, body=None, authorized=True):
        module = self.module
        request = object.__new__(module.Handler)
        request.path = route
        body = urllib.parse.urlencode(fields).encode() if body is None else body
        request.headers = {'Content-Length': str(len(body)), 'Host': '127.0.0.1:8765'}
        request.rfile = mock.Mock(wraps=io.BytesIO(body))
        request.server = types.SimpleNamespace(startup_state='ready', ui_csrf_token='synthetic-csrf', server_address=('127.0.0.1', 8765))
        request.send = mock.Mock()
        request.send_json = mock.Mock()
        with mock.patch.object(module, 'state', return_value={'phase': 'ready'}), mock.patch.object(
                module.bootstrap, 'active_answer_revision_identity', return_value=(True, 'current', {'generation': 'synthetic'})), mock.patch.object(
                module, '_local_ui_post_is_authorized', return_value=authorized), mock.patch.object(
                module, '_local_ui_post_has_stale_csrf', return_value=False), mock.patch.object(
                module, 'answer_query', side_effect=AssertionError('must not search')) as answer:
            module.Handler.do_POST(request)
            answer.assert_not_called()
        return request

    def test_maximum_unicode_review_and_edit(self):
        module = self.module
        for character in ['受', '\U00020BB7']:
            with self.subTest(character=character):
                query, goal = character * 2000, character * 3000
                requirements = '\n'.join(character * 300 for _ in range(8))
                fields = {'query': query, 'goal': goal, 'requirements': requirements,
                    module.UI_CSRF_FIELD: 'synthetic-csrf'}
                contract = module.intent_contract.make_contract(query, goal, requirements, {'generation': 'synthetic'})
                payload, signature = module.intent_contract.seal(contract, module.intent_contract.SIGNING_KEY)
                edit = {'query': query, 'intent_payload': payload, 'intent_signature': signature,
                    'intent_action': 'edit', module.UI_CSRF_FIELD: 'synthetic-csrf'}
                for route, form in [('/intent-preview', fields), ('/local-search-answer', edit)]:
                    with self.subTest(route=route):
                        size = len(urllib.parse.urlencode(form).encode())
                        self.assertLess(size, 128 * 1024)
                        request = self.dispatch(route, form)
                        request.send_json.assert_not_called()
                        request.send.assert_called_once()
                        self.assertEqual(request.send.call_args.args[1:] or (200,), (200,))
                        self.assertIn(goal, request.send.call_args.args[0].decode())

    def test_route_caps_and_overflow_are_checked_before_read(self):
        for route, cap in [('/intent-preview', 128*1024), ('/local-search-answer', 128*1024),
                ('/ask', 64*1024), ('/build', 64*1024), ('/intent-dialog', 64*1024)]:
            for delta in [0, 1]:
                with self.subTest(route=route, delta=delta):
                    request = self.dispatch(route, body=b'x' * (cap + delta), authorized=False)
                    if delta:
                        request.rfile.read.assert_not_called()
                        self.assertEqual(request.send_json.call_args.args[1], 413)
                    else:
                        request.rfile.read.assert_called_once_with(cap)
                        self.assertEqual(request.send_json.call_args.args[1], 403)


if __name__ == '__main__':
    unittest.main()
