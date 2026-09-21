"""Finite localhost UI checks with a fake update service and no live state."""
from __future__ import annotations

import contextlib
import copy
import html
import importlib.util
import io
import json
import sys
import threading
import time
import types
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "source_updates_http_fixture", ROOT / "tests/test_dated_hitl_http_e2e.py"
)
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


def candidate_view():
    return {
        "phase": "partial", "summary": {"errors": 1, "candidate_files": 2},
        "candidates": [{
            "ticket": 'ticket-"<&', "indexed_path": '/synthetic/<old>.csv',
            "record": {"path": '/synthetic/<img src=x onerror=alert(1)>.csv',
                       "name": '<img src=x onerror=alert(1)>.csv',
                       "mtime_ns": 1_000_000_000, "size_bytes": 12},
        }],
    }


class FakeUpdates:
    """No filesystem or models; reservations and workers are observable."""
    def __init__(self):
        self.view = candidate_view()
        self.calls = []
        self.confirmations = []
        self.release = threading.Event()
        self.release.set()
        self.started = threading.Event()
        self.finished = threading.Event()
        self.failure = None

    def snapshot(self):
        return copy.deepcopy(self.view)

    def reserve_scan(self):
        self.calls.append("reserve_scan")
        self.view["phase"] = "scanning"

    def reserve_adoption(self, ticket, *, confirmed):
        self.confirmations.append(confirmed)
        if confirmed is not True:
            raise ValueError("update_confirmation_required")
        self.calls.append(("reserve_adoption", ticket))
        self.view["phase"] = "adopting"
        return {"ticket": ticket}

    def _work(self, action):
        self.calls.append(action)
        self.started.set()
        if not self.release.wait(2):
            raise RuntimeError("synthetic_worker_timeout")
        if self.failure is not None:
            raise self.failure
        self.view["phase"] = "complete" if action == "scan" else "applied"
        self.finished.set()

    def scan_reserved(self):
        self._work("scan")

    def adopt_reserved(self, item):
        self.calls.append(("adopt_item", item))
        self._work("adopt")

    def dismiss(self, ticket):
        self.calls.append(("dismiss", ticket))
        self.view["candidates"] = []

    def fail(self, error):
        self.calls.append(("fail", str(error)))
        self.view.update(phase="error", error=str(error))
        self.finished.set()


class SourceUpdatesHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server_module = fixture.load_server()

    def setUp(self):
        fixture.DatedHitlHttpE2ETests.setUp(self)
        self.service = FakeUpdates()
        self.httpd.source_updates = self.service
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        module = self.server_module
        self.stack.enter_context(mock.patch.object(module, "state", return_value={"phase": "ready"}))
        self.stack.enter_context(mock.patch.object(module, "home", side_effect=lambda message="", csrf_token="", **kw:
            module.page("<p>" + html.escape(message) + "</p>" + module.source_update_card(kw.get("source_update_state"), csrf_token))))
        for name in ("load_config_snapshot", "load_json", "atomic_json", "start_ollama", "ensure_models", "apply_source_update", "build_index"):
            self.stack.enter_context(mock.patch.object(module.bootstrap, name, side_effect=AssertionError("live bootstrap forbidden: " + name)))
        self.stack.enter_context(mock.patch.object(module.source_updates, "SourceUpdates", side_effect=AssertionError("real scanner service forbidden")))

    def tearDown(self):
        self.service.release.set()
        deadline = time.monotonic() + 2
        while self.server_module.ACTIVE_WORK_COUNT and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(0, self.server_module.ACTIVE_WORK_COUNT)
        self.assertFalse(self.server_module.BUILD_LOCK.locked())
        fixture.DatedHitlHttpE2ETests.tearDown(self)

    def request(self, path, fields=None, *, headers=None):
        request_headers = {"Origin": self.base_url, **(headers or {})}
        data = None if fields is None else urllib.parse.urlencode(fields, doseq=True).encode()
        request = urllib.request.Request(self.base_url + path, data=data, headers=request_headers)
        try:
            response = self.opener.open(request, timeout=3)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read().decode()
        with response:
            return response.status, response.read().decode()

    def fields(self, **extra):
        return {self.server_module.UI_CSRF_FIELD: self.httpd.ui_csrf_token, **extra}

    def test_csrf_origin_and_host_guards_prevent_service_actions(self):
        for fields, headers, expected in (
            (self.fields(**{self.server_module.UI_CSRF_FIELD: "bad"}), {}, 403),
            (self.fields(), {"Origin": "https://foreign.invalid"}, 403),
            (self.fields(), {"Host": "foreign.invalid"}, 421),
        ):
            for action in ("scan", "adopt", "dismiss"):
                with self.subTest(action=action, headers=headers, fields=fields):
                    status, _body = self.request("/source-updates/" + action, fields, headers=headers)
                    self.assertEqual(expected, status)
                    self.assertEqual([], self.service.calls)
                    self.assertEqual([], self.service.confirmations)

    def test_adoption_requires_exactly_one_yes_confirmation(self):
        for confirmation in (None, "false", "true", "no", ["yes", "yes"], ["yes", "no"]):
            with self.subTest(confirmation=confirmation):
                fields = self.fields(candidate_ticket="candidate-1")
                if confirmation is not None:
                    fields["confirmed"] = confirmation
                status, _body = self.request("/source-updates/adopt", fields)
                self.assertEqual(409, status)
                self.assertEqual([], self.service.calls)
        self.assertEqual([False] * 6, self.service.confirmations)

    def test_scan_posts_only_metadata_worker_and_returns_before_completion(self):
        self.service.release.clear()
        status, body = self.request("/source-updates/scan", self.fields())
        self.assertEqual(200, status)
        self.assertTrue(self.service.started.wait(1))
        self.assertEqual(["reserve_scan", "scan"], self.service.calls)
        self.assertFalse(self.service.finished.is_set())
        self.assertIn("本文は読みません", body)
        self.assertIn('data-busy="true"', body)
        self.assertEqual(1, self.server_module.ACTIVE_WORK_COUNT)

    def test_adopt_posts_only_confirmed_reservation_and_async_worker(self):
        self.service.release.clear()
        status, body = self.request("/source-updates/adopt", self.fields(candidate_ticket="candidate-1", confirmed="yes"))
        self.assertEqual(200, status)
        self.assertTrue(self.service.started.wait(1))
        self.assertEqual([True], self.service.confirmations)
        self.assertEqual([("reserve_adoption", "candidate-1"), ("adopt_item", {"ticket": "candidate-1"}), "adopt"], self.service.calls)
        self.assertFalse(self.service.finished.is_set())
        self.assertIn("安全検査と索引構築", body)

    def test_dismiss_never_adopts_or_starts_background_work(self):
        status, _body = self.request("/source-updates/dismiss", self.fields(candidate_ticket="candidate-1"))
        self.assertEqual(200, status)
        self.assertEqual([("dismiss", "candidate-1")], self.service.calls)
        self.assertFalse(self.service.started.is_set())
        self.assertEqual(0, self.server_module.ACTIVE_WORK_COUNT)

    def test_get_status_is_read_only_and_escapes_candidate_paths(self):
        status, body = self.request("/source-updates/status")
        self.assertEqual(200, status)
        value = json.loads(body)
        self.assertFalse(value["busy"])
        self.assertEqual([], self.service.calls)
        self.assertIn("一部の場所・候補が未確認", value["html"])
        self.assertIn("更新なし", value["html"])
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;.csv", value["html"])
        self.assertIn("&lt;old&gt;.csv", value["html"])
        self.assertIn("ticket-&quot;&lt;&amp;", value["html"])
        self.assertNotIn("<img", value["html"])

    def test_service_unavailable_startup_and_foreign_get_fail_before_snapshot(self):
        with mock.patch.object(self.service, "snapshot", side_effect=AssertionError("snapshot must not run")):
            self.httpd.startup_state = "recovering"
            self.assertEqual(503, self.request("/source-updates/status")[0])
            self.httpd.startup_state = "ready"
            self.assertEqual(421, self.request("/source-updates/status", headers={"Host": "foreign.invalid"})[0])
            del self.httpd.source_updates
            self.assertEqual(503, self.request("/source-updates/status")[0])
            self.assertEqual(503, self.request("/source-updates/scan", self.fields())[0])

    def test_busy_and_shutdown_reject_all_update_mutations(self):
        module = self.server_module
        for condition in ("busy", "shutdown"):
            with self.subTest(condition=condition):
                if condition == "busy":
                    module.BUILD_LOCK.acquire()
                else:
                    module.SERVER_SHUTDOWN_REQUESTED.set()
                try:
                    for action in ("scan", "adopt", "dismiss"):
                        status, _body = self.request("/source-updates/" + action, self.fields(candidate_ticket="candidate-1", confirmed="yes"))
                        self.assertEqual(409, status)
                finally:
                    if condition == "busy":
                        module.BUILD_LOCK.release()
                    else:
                        module.SERVER_SHUTDOWN_REQUESTED.clear()
                self.assertEqual([], self.service.calls)
                self.assertEqual(0, module.ACTIVE_WORK_COUNT)


class SourceUpdatesWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = fixture.load_server()

    def setUp(self):
        self.module.SERVER_SHUTDOWN_REQUESTED.clear()
        self.module.ACTIVE_WORK_COUNT = 0
        self.service = FakeUpdates()
        self.server = types.SimpleNamespace(source_updates=self.service)

    def tearDown(self):
        self.assertEqual(0, self.module.ACTIVE_WORK_COUNT)
        self.assertFalse(self.module.BUILD_LOCK.locked())
        self.module.SERVER_SHUTDOWN_REQUESTED.clear()

    def test_reservation_is_complete_before_worker_starts_and_holds_locks(self):
        for action in ("scan", "adopt"):
            with self.subTest(action=action):
                self.service.calls.clear()
                pending = []

                def thread_factory(*, target, **_kwargs):
                    self.assertEqual(1, len(self.service.calls))
                    self.assertEqual(1, self.module.ACTIVE_WORK_COUNT)
                    self.assertTrue(self.module.BUILD_LOCK.locked())
                    self.assertFalse(self.module._reserve_server_shutdown())
                    pending.append(target)
                    return types.SimpleNamespace(start=lambda: None)

                with mock.patch.object(self.module.threading, "Thread", side_effect=thread_factory):
                    self.module.start_source_update(self.server, action, "candidate-1", confirmed=True)
                self.assertEqual(1, len(pending))
                self.assertEqual(1, self.module.ACTIVE_WORK_COUNT)
                pending.pop()()
                self.assertEqual(0, self.module.ACTIVE_WORK_COUNT)
                self.assertFalse(self.module.BUILD_LOCK.locked())

    def test_worker_failure_reports_error_and_releases_both_reservations(self):
        self.service.failure = ValueError("synthetic_worker_failure")
        with mock.patch.object(self.module.threading, "Thread", side_effect=lambda *, target, **kw: types.SimpleNamespace(start=target)):
            self.module.start_source_update(self.server, "scan")
        self.assertIn(("fail", "synthetic_worker_failure"), self.service.calls)
        self.assertEqual("error", self.service.view["phase"])

    def test_thread_start_failure_releases_both_reservations(self):
        with mock.patch.object(self.module.threading, "Thread") as worker:
            worker.return_value.start.side_effect = RuntimeError("thread_start_failure")
            with self.assertRaisesRegex(RuntimeError, "thread_start_failure"):
                self.module.start_source_update(self.server, "scan")
        self.assertEqual(["reserve_scan", ("fail", "thread_start_failure")], self.service.calls)
        self.assertEqual("error", self.service.view["phase"])

    def test_reservation_failure_never_starts_thread_and_releases_locks(self):
        with mock.patch.object(self.module.threading, "Thread") as worker:
            with self.assertRaisesRegex(ValueError, "update_confirmation_required"):
                self.module.start_source_update(self.server, "adopt", "candidate-1", confirmed=False)
        worker.assert_not_called()

    def test_send_adds_pending_warning_to_answer_without_hiding_answer(self):
        for view in (candidate_view(), {"phase": "partial", "candidates": []}, {"phase": "scanning", "candidates": []}, {"phase": "error", "candidates": []}, {"phase": "applied", "candidates": []}, {"phase": "idle", "candidates": []}):
            with self.subTest(phase=view["phase"]):
                self.service.view = view
                output = io.BytesIO()
                handler = types.SimpleNamespace(server=self.server, wfile=output,
                    send_response=mock.Mock(), send_header=mock.Mock(),
                    send_local_security_headers=mock.Mock(), end_headers=mock.Mock())
                self.module.Handler.send(handler, self.module.page("<p>SYNTHETIC ANSWER</p>"))
                body = output.getvalue().decode()
                self.assertIn("SYNTHETIC ANSWER", body)
                self.assertIn("最新版の保証ではありません", body)

    def test_home_card_prevents_duplicate_pending_warning(self):
        output = io.BytesIO()
        handler = types.SimpleNamespace(server=self.server, wfile=output,
            send_response=mock.Mock(), send_header=mock.Mock(),
            send_local_security_headers=mock.Mock(), end_headers=mock.Mock())
        self.module.Handler.send(handler, self.module.page(self.module.source_update_card(self.service.snapshot(), "csrf")))
        self.assertNotIn("この回答は現在の索引に基づき", output.getvalue().decode())

    def test_missing_model_error_explains_no_download(self):
        # SourceUpdates.fail exposes a bounded code, omitting model/path text.
        content = self.module.source_update_card({"phase": "error", "error": "model_downloads_disabled_missing:", "candidates": []})
        self.assertIn("必要なローカルモデルが不足", content)
        self.assertIn("勝手なダウンロードはしていません", content)

    def test_startup_scans_only_after_successful_recovery_with_all_boundaries_faked(self):
        module = self.module
        for outcome, count in (("ready", 1), ("failed", 0)):
            with self.subTest(outcome=outcome):
                self.service.calls.clear()
                fake_httpd = mock.MagicMock()
                fake_httpd.__enter__.return_value = fake_httpd
                events = []

                def recovery():
                    events.append("recovery")
                    self.assertEqual(1, module.ACTIVE_WORK_COUNT)
                    return outcome

                def start(server, action):
                    self.assertEqual(["recovery"], events)
                    self.assertEqual(0, module.ACTIVE_WORK_COUNT)
                    self.assertEqual("ready", server.startup_state)
                    self.assertEqual("scan", action)

                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.object(sys, "argv", ["synthetic-server", "--port", "8765"]))
                    stack.enter_context(mock.patch.object(module, "ThreadingHTTPServer", return_value=fake_httpd))
                    factory = stack.enter_context(mock.patch.object(module.source_updates, "SourceUpdates", return_value=self.service))
                    stack.enter_context(mock.patch.object(module, "_publish_server_identity"))
                    stack.enter_context(mock.patch.object(module, "_remove_server_identity"))
                    stack.enter_context(mock.patch.object(module, "_startup_recovery_outcome", side_effect=recovery))
                    scan = stack.enter_context(mock.patch.object(module, "start_source_update", side_effect=start))
                    stack.enter_context(mock.patch.object(module.threading, "Thread", side_effect=lambda *, target, **kw: types.SimpleNamespace(start=target)))
                    for name in ("load_json", "atomic_json", "load_config_snapshot", "recover_interrupted_build", "start_ollama", "build_index"):
                        stack.enter_context(mock.patch.object(module.bootstrap, name, side_effect=AssertionError("live startup forbidden")))
                    self.assertEqual(0, module.main())
                    factory.assert_called_once_with(module.bootstrap)
                    self.assertEqual(count, scan.call_count)
                self.assertEqual(outcome, fake_httpd.startup_state)
                fake_httpd.serve_forever.assert_called_once()
                self.assertEqual([], self.service.calls)


if __name__ == "__main__":
    unittest.main()
