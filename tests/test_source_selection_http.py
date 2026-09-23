"""Folder-selection HTTP path with synthetic folders and no live model/app."""
from __future__ import annotations

import contextlib
import importlib.util
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("selection_http_fixture", ROOT / "tests/test_dated_hitl_http_e2e.py")
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class SourceSelectionHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server_module = fixture.load_server()

    def setUp(self):
        self.module = self.server_module
        self.module.SOURCE_CHANGE_ACTIVE = False
        fixture.DatedHitlHttpE2ETests.setUp(self)
        self.stack = contextlib.ExitStack()
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / "新しい<資料>&"
        self.source.mkdir()
        self.config = {"source_root": str(self.base / "old"), "index_path": str(self.base / "old.sqlite3"),
                       "answer_model": "gemma4:12b", "audit_model": "gemma4:12b"}
        bootstrap = self.module.bootstrap
        for name, path in (("SUPPORT", self.base / "support"), ("CONFIG", self.base / "support/config.json"),
                           ("STATE", self.base / "support/state.json")):
            self.stack.enter_context(mock.patch.object(bootstrap, name, path))
        bootstrap.atomic_json(bootstrap.CONFIG, self.config)
        bootstrap.atomic_json(bootstrap.STATE, {"phase": "ready", "message": "old"})
        self.config_bytes = bootstrap.CONFIG.read_bytes()
        self.state_bytes = bootstrap.STATE.read_bytes()
        # Fail loudly if a test escapes the synthetic transport boundaries.
        for name in ("build_index", "ensure_models", "start_ollama", "model_names", "run", "diagnose"):
            self.stack.enter_context(mock.patch.object(bootstrap, name, side_effect=AssertionError("live call: " + name)))
        self.picker = self.stack.enter_context(mock.patch.object(self.module.source_selection, "choose_folder", return_value=self.source))
        self.httpd.source_selection = self.module.source_selection.SourceSelection(bootstrap)
        self.new_updates = mock.Mock(snapshot=lambda: {"phase": "idle"})
        self.stack.enter_context(mock.patch.object(self.module.source_updates, "SourceUpdates", return_value=self.new_updates))
        self.old_updates = mock.Mock(snapshot=lambda: {"phase": "complete", "candidates": []})
        self.httpd.source_updates = self.old_updates
        self.release = threading.Event()
        self.release.set()
        self.started = threading.Event()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def tearDown(self):
        self.release.set()
        self.wait_idle()
        self.stack.close()
        self.temporary.cleanup()
        fixture.DatedHitlHttpE2ETests.tearDown(self)
        self.module.SOURCE_CHANGE_ACTIVE = False

    def wait_idle(self):
        deadline = time.monotonic() + 3
        while self.module.SOURCE_CHANGE_ACTIVE and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertFalse(self.module.SOURCE_CHANGE_ACTIVE)
        self.assertFalse(self.module.BUILD_LOCK.locked())
        self.assertEqual(0, self.module.ACTIVE_WORK_COUNT)

    def request(self, action, fields=None, headers=None):
        data = None if fields is None else urllib.parse.urlencode(fields, doseq=True).encode()
        request = urllib.request.Request(self.base_url + action, data=data,
            headers={"Origin": self.base_url, **(headers or {})})
        try:
            response = self.opener.open(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.code, dict(response.headers), response.read().decode()

    def fields(self, **extra):
        return {self.module.UI_CSRF_FIELD: self.httpd.ui_csrf_token, **extra}

    def pick(self):
        status, _, body = self.request("/source-selection/pick", self.fields())
        self.assertEqual(200, status, body)
        return self.httpd.source_selection.snapshot()["ticket"]

    def test_home_shows_selection_without_hiding_ready_question(self):
        diagnosis = {"index_ready": True, "models": [], "warnings": [], "memory_gb": 24,
                     "free_gb": 40, "architecture": "arm64", "ollama_online": True,
                     "source_root": str(self.source)}
        with mock.patch.object(self.module.bootstrap, "diagnose", return_value=diagnosis), \
             mock.patch.object(self.module, "semantic_graph_answer_path_status", return_value={
                 "state": "ready", "show_rebuild": False, "css_class": "ok", "label": "ready"}), \
             mock.patch.object(self.module, "document_version_review_notice", return_value=""), \
             mock.patch.object(self.module, "security_exclusion_notice", return_value=""):
            body = self.module.home(csrf_token="<token>").decode()
        self.assertIn('action="/source-selection/pick"', body)
        self.assertIn('id="local-search-form"', body)
        self.assertIn('&lt;token&gt;', body)

    def test_post_guards_block_picker_and_all_actions(self):
        for action in ("pick", "build", "cancel"):
            for fields, headers, expected in (
                ({}, {}, 403), (self.fields(), {"Origin": "https://outside.invalid"}, 403),
                (self.fields(), {"Origin": "null", "Sec-Fetch-Site": "same-origin"}, 403),
                (self.fields(), {"Sec-Fetch-Site": "cross-site"}, 403),
                (self.fields(), {"Host": "outside.invalid"}, 421),
            ):
                with self.subTest(action=action, headers=headers):
                    status, _, _ = self.request("/source-selection/" + action, fields, headers)
                    self.assertEqual(expected, status)
        self.picker.assert_not_called()
        self.assertEqual(self.config_bytes, self.module.bootstrap.CONFIG.read_bytes())

    def test_fetch_context_without_referer_keeps_security_and_allows_picker(self):
        status, headers, body = self.request("/source-selection/pick", self.fields(), {
            "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "same-origin",
            "Sec-Fetch-Dest": "empty",
        })
        self.assertEqual(200, status, body)
        self.assertEqual("no-referrer", headers["Referrer-Policy"])
        self.assertIn("form-action 'self'", headers["Content-Security-Policy"])
        self.picker.assert_called_once()
        self.assertEqual(self.config_bytes, self.module.bootstrap.CONFIG.read_bytes())

    def test_get_cannot_open_native_picker(self):
        status, _, _ = self.request("/source-selection/pick")
        self.assertEqual(404, status)
        self.picker.assert_not_called()

    def test_choose_only_previews_escaped_path_and_leaves_state_unchanged(self):
        ticket = self.pick()
        self.assertTrue(ticket)
        body = self.module.source_selection_card(self.httpd.source_selection.snapshot(), "token")
        self.assertIn("新しい&lt;資料&gt;&amp;", body)
        self.assertNotIn('name="source_root"', body)
        self.assertIn('action="/source-selection/build"', body)
        self.assertEqual(self.config_bytes, self.module.bootstrap.CONFIG.read_bytes())
        self.assertEqual(self.state_bytes, self.module.bootstrap.STATE.read_bytes())

    def test_native_cancel_and_confirmation_cancel_change_nothing(self):
        self.picker.return_value = None
        status, _, body = self.request("/source-selection/pick", self.fields())
        self.assertEqual(200, status)
        self.assertIn("取り消しました", body)
        self.picker.return_value = self.source
        self.pick()
        status, _, body = self.request("/source-selection/cancel", self.fields())
        self.assertEqual(200, status)
        self.assertFalse(self.httpd.source_selection.snapshot()["ticket"])
        self.assertEqual(self.config_bytes, self.module.bootstrap.CONFIG.read_bytes())
        self.assertEqual(self.state_bytes, self.module.bootstrap.STATE.read_bytes())

    def test_confirmation_requires_single_yes_and_single_bound_ticket(self):
        for confirmation, ticket_change in ((None, None), ("no", None), (["yes", "yes"], None),
                                             ("yes", "wrong"), ("yes", ["x", "y"])):
            with self.subTest(confirmation=confirmation, ticket=ticket_change):
                ticket = self.pick()
                fields = self.fields(selection_ticket=ticket if ticket_change is None else ticket_change)
                if confirmation is not None:
                    fields["confirmed"] = confirmation
                status, _, _ = self.request("/source-selection/build", fields)
                self.assertEqual(409, status)
                self.assertEqual(self.config_bytes, self.module.bootstrap.CONFIG.read_bytes())

    def test_confirmation_starts_once_async_redirects_and_resets_update_candidates(self):
        ticket = self.pick()
        self.release.clear()
        def build(source, config, identity):
            self.assertEqual(self.source, source)
            self.assertEqual(self.config, config)
            self.assertEqual(str(self.source), identity["path"])
            self.started.set()
            if not self.release.wait(2):
                raise AssertionError("synthetic timeout")
        with mock.patch.object(self.module.bootstrap, "apply_source_selection", side_effect=build) as apply:
            status, headers, _ = self.request("/source-selection/build", self.fields(selection_ticket=ticket, confirmed="yes", source_root="/untrusted"))
            self.assertEqual(303, status)
            self.assertEqual("/", headers["Location"])
            self.assertTrue(self.started.wait(1))
            self.assertFalse(self.module._begin_active_work())
            self.assertFalse(self.module._reserve_server_shutdown())
            status, _, _ = self.request("/source-selection/build", self.fields(selection_ticket=ticket, confirmed="yes"))
            self.assertEqual(409, status)
            self.release.set()
            self.wait_idle()
            self.assertEqual("complete", self.httpd.source_selection.snapshot()["phase"])
            self.assertIs(self.new_updates, self.httpd.source_updates)
            status, _, _ = self.request("/source-selection/build", self.fields(selection_ticket=ticket, confirmed="yes"))
            self.assertEqual(409, status)
            self.assertEqual(1, apply.call_count)

    def test_active_answer_or_build_prevents_selection(self):
        self.assertTrue(self.module._begin_active_work())
        status, _, _ = self.request("/source-selection/pick", self.fields())
        self.assertEqual(409, status)
        self.module._end_active_work()
        with self.module.BUILD_LOCK:
            status, _, _ = self.request("/source-selection/pick", self.fields())
            self.assertEqual(409, status)
        self.picker.assert_not_called()

    def test_config_changed_since_preview_never_starts_real_build(self):
        ticket = self.pick()
        updated = {**self.config, "answer_model": "changed-by-other-operation"}
        self.module.bootstrap.atomic_json(self.module.bootstrap.CONFIG, updated)
        status, _, _ = self.request("/source-selection/build", self.fields(selection_ticket=ticket, confirmed="yes"))
        self.assertEqual(303, status)
        self.wait_idle()
        self.assertEqual("error", self.httpd.source_selection.snapshot()["phase"])
        self.assertEqual(updated, json.loads(self.module.bootstrap.CONFIG.read_text()))
        self.assertEqual(self.state_bytes, self.module.bootstrap.STATE.read_bytes())
        self.module.bootstrap.build_index.assert_not_called()

    def test_picker_exception_releases_reservation_without_state_change(self):
        self.picker.side_effect = RuntimeError("synthetic picker failure")
        status, _, body = self.request("/source-selection/pick", self.fields())
        self.assertEqual(409, status)
        self.assertIn("完了できませんでした", body)
        self.assertEqual(self.config_bytes, self.module.bootstrap.CONFIG.read_bytes())
        self.assertEqual(self.state_bytes, self.module.bootstrap.STATE.read_bytes())

    def test_worker_start_exception_releases_reservation(self):
        ticket = self.pick()
        with mock.patch.object(self.module, "start_selected_source_build", side_effect=RuntimeError("thread start")):
            status, _, _ = self.request("/source-selection/build", self.fields(selection_ticket=ticket, confirmed="yes"))
        self.assertEqual(409, status)
        self.assertEqual(self.config_bytes, self.module.bootstrap.CONFIG.read_bytes())

    def test_worker_failure_is_not_reported_as_complete(self):
        ticket = self.pick()
        with mock.patch.object(self.module.bootstrap, "apply_source_selection", side_effect=RuntimeError("synthetic")):
            status, _, _ = self.request("/source-selection/build", self.fields(selection_ticket=ticket, confirmed="yes"))
            self.assertEqual(303, status)
            self.wait_idle()
        self.assertEqual("error", self.httpd.source_selection.snapshot()["phase"])
        body = self.module.source_selection_card(self.httpd.source_selection.snapshot())
        self.assertIn("資料不足という判定ではありません", body)

    def test_selection_reservation_blocks_build_and_version_write(self):
        self.assertTrue(self.module._reserve_source_change())
        try:
            for action in ("/build", "/document-version-decision"):
                status, _, body = self.request(action, self.fields())
                self.assertEqual(409, status)
                self.assertIn("今回は開始・保存していません", body)
        finally:
            self.module._release_source_change()
        self.assertEqual(self.config_bytes, self.module.bootstrap.CONFIG.read_bytes())
        self.assertEqual(self.state_bytes, self.module.bootstrap.STATE.read_bytes())

    def test_already_running_version_decision_blocks_source_selection(self):
        self.release.clear()
        result = []
        def save(*_args):
            self.started.set()
            if not self.release.wait(2):
                raise AssertionError("synthetic timeout")
            return False  # No rebuild; this is a guarded decision-only test.
        with mock.patch.object(self.module, "save_dated_review_submission", side_effect=save), \
             mock.patch.object(self.module, "home", return_value=b"synthetic home"):
            request_thread = threading.Thread(target=lambda: result.append(
                self.request("/document-version-decision", self.fields(review_ticket="synthetic"))))
            request_thread.start()
            try:
                self.assertTrue(self.started.wait(1))
                status, _, _ = self.request("/source-selection/pick", self.fields())
                self.assertEqual(409, status)
                self.picker.assert_not_called()
            finally:
                self.release.set()
                request_thread.join(3)
            self.assertEqual(200, result[0][0])

    def render_home(self, current):
        diagnosis = {"index_ready": True, "models": [], "warnings": [], "memory_gb": 24,
                     "free_gb": 40, "architecture": "arm64", "ollama_online": True,
                     "source_root": str(self.source)}
        with mock.patch.object(self.module.bootstrap, "diagnose", return_value=diagnosis), \
             mock.patch.object(self.module, "state", return_value=current), \
             mock.patch.object(self.module, "semantic_graph_answer_path_status", return_value={
                 "state": "ready", "show_rebuild": False, "css_class": "ok", "label": "ready"}), \
             mock.patch.object(self.module, "document_version_review_notice", return_value=""), \
             mock.patch.object(self.module, "security_exclusion_notice", return_value=""), \
             mock.patch.object(self.module, "source_update_card", return_value="OLD_UPDATE_CANDIDATES"):
            return self.module.home(csrf_token="token").decode()

    def test_switching_hides_questions_and_previous_update_candidates(self):
        self.assertTrue(self.module._reserve_source_change())
        try:
            body = self.render_home({"phase": "ready"})
        finally:
            self.module._release_source_change()
        self.assertNotIn('id="local-search-form"', body)
        self.assertNotIn("OLD_UPDATE_CANDIDATES", body)
        self.assertIn("実行中", body)
        self.assertIn('http-equiv="refresh"', body)

    def test_error_retry_discloses_and_labels_model_download_permission(self):
        body = self.render_home({"phase": "error", "message": "合成エラー", "error": "missing model"})
        self.assertIn("公式Ollama経由で取得", body)
        self.assertIn("不足モデルの取得を許可して再実行", body)
        self.assertNotIn('id="local-search-form"', body)

    def test_storage_overlap_error_explains_narrower_folder_selection(self):
        self.picker.return_value = self.base
        status, _, body = self.request("/source-selection/pick", self.fields())
        self.assertEqual(409, status)
        self.assertIn("ホーム全体ではなく", body)
        self.assertEqual(self.config_bytes, self.module.bootstrap.CONFIG.read_bytes())


if __name__ == "__main__":
    unittest.main()
