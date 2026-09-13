import unittest
from unittest import mock
from test_dated_hitl_http_e2e import load_server


class HomeFormOrderTests(unittest.TestCase):
    def test_input_precedes_diagnostics_and_keeps_csrf(self):
        server = load_server()
        diagnosis = dict(index_ready=True, models=[], warnings=['warning-marker'],
                         memory_gb=24, free_gb=100, architecture='arm64',
                         ollama_online=True, source_root='/synthetic')
        with mock.patch.object(server.bootstrap, 'diagnose', return_value=diagnosis), \
             mock.patch.object(server, 'state', return_value={'phase': 'ready_with_limits', 'message': 'error-marker'}), \
             mock.patch.object(server, 'semantic_graph_answer_path_status', return_value={'state': 'ready', 'show_rebuild': False, 'css_class': 'ok', 'label': 'ready'}), \
             mock.patch.object(server, 'document_version_review_notice', return_value=''), \
             mock.patch.object(server, 'security_exclusion_notice', return_value=''), \
             mock.patch.object(server, '_semantic_graph_observer_pending', return_value=False):
            rendered = server.home(csrf_token='synthetic-token').decode()
        self.assertEqual(rendered.count('id="local-search-form"'), 1)
        for marker in ['warning-marker', 'SYSTEM STATUS', 'error-marker']:
            self.assertLess(rendered.index('id="local-search-form"'), rendered.index(marker))
        self.assertIn('synthetic-token', rendered)
        self.assertIn('action="/intent-dialog"', rendered)
