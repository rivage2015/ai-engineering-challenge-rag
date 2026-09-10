"""Actual localhost HTTP checks for dated-review and answer revision gates."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "distribution/macos-local-memory/app"


def load_server():
    prior = sys.modules.pop("bootstrap", None)
    sys.path.insert(0, str(APP))
    try:
        spec = importlib.util.spec_from_file_location(
            "dated_hitl_http_server", APP / "local_memory_server.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(APP))
        sys.modules.pop("bootstrap", None)
        if prior is not None:
            sys.modules["bootstrap"] = prior


def review_context(resolver):
    family_key = "\0手順\0.csv"
    candidates = [
        {
            "relative_path": "手順2024.csv",
            "source_sha256": "b" * 64,
            "size_bytes": 10,
            "mtime_ns": 2,
            "birthtime_ns": 1,
            "explicit_years": [2024],
            "explicit_versions": [],
            "current_markers": [],
            "historical_markers": [],
            "draft_markers": [],
        },
        {
            "relative_path": "手順2025.csv",
            "source_sha256": "c" * 64,
            "size_bytes": 10,
            "mtime_ns": 4,
            "birthtime_ns": 3,
            "explicit_years": [2025],
            "explicit_versions": [],
            "current_markers": [],
            "historical_markers": [],
            "draft_markers": [],
        },
    ]
    group_id = "version_set_" + resolver.sha256_json(
        {"family_key": family_key}
    )[:32]
    group = {
        "group_id": group_id,
        "candidate_set_sha256": resolver.candidate_set_hash(candidates),
        "status": "needs_human_review",
        "reason_code": "dated_family_requires_human_review",
        "candidates": candidates,
    }
    return {
        "graph": {"groups": [group]},
        "family_keys": {group_id: family_key},
        "dated_group_ids": [group_id],
        "base_revision": {
            "generation": "generation-" + "2" * 32,
            "source_scope_sha256": "3" * 64,
            "graph_sha256": "4" * 64,
            "graph_file_sha256": "5" * 64,
            "inventory_sha256": "6" * 64,
            "decisions_sha256": None,
            "resolver_version": "0.1.6",
        },
        "decision_store_revision": {
            "exists": False,
            "sha256": None,
            "byte_count": 0,
        },
    }


class DatedHitlHttpE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server_module = load_server()

    def setUp(self):
        module = self.server_module
        module.SERVER_SHUTDOWN_REQUESTED.clear()
        module.ACTIVE_WORK_COUNT = 0
        self.httpd = module.ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
        self.httpd.instance_id = "test-instance"
        self.httpd.shutdown_token = "test-shutdown"
        self.httpd.ui_csrf_token = "test-csrf"
        self.httpd.review_ticket_lock = threading.Lock()
        self.httpd.review_tickets = {}
        self.httpd.startup_state = "ready"
        self.log_patch = mock.patch.object(module.Handler, "log_message", lambda *_args: None)
        self.log_patch.start()
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.log_patch.stop()
        self.server_module.SERVER_SHUTDOWN_REQUESTED.clear()
        self.server_module.ACTIVE_WORK_COUNT = 0

    def post(self, path: str, fields: dict[str, str]):
        data = urllib.parse.urlencode(fields).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={"Origin": self.base_url},
            method="POST",
        )
        try:
            response = self.opener.open(request, timeout=5)
        except urllib.error.HTTPError as error:
            try:
                return error.code, error.read().decode("utf-8")
            finally:
                error.close()
        with response:
            return response.status, response.read().decode("utf-8")

    def test_positive_review_persists_and_rebuilds_once_then_replay_fails_closed(self):
        module = self.server_module
        rebuild_started = threading.Event()
        rebuild_calls = []
        with tempfile.TemporaryDirectory() as temporary:
            decisions = Path(temporary) / "decisions.json"
            engine = ROOT / "distribution/macos-local-memory/engine"
            with mock.patch.object(module.bootstrap, "ENGINE", engine):
                resolver = module.bootstrap._decision_resolver()
            current = review_context(resolver)
            with mock.patch.object(
                module.bootstrap,
                "current_document_version_review_context",
                return_value=current,
            ):
                group = current["graph"]["groups"][0]
                token = module.review_ticket_issuer(self.httpd)(group)

            def note_rebuild():
                rebuild_calls.append("rebuild")
                rebuild_started.set()

            def small_home(message="", *_args, **_kwargs):
                return module.page("<p>" + str(message) + "</p>")

            fields = {
                module.UI_CSRF_FIELD: self.httpd.ui_csrf_token,
                "review_ticket": token,
                "relation": "same_work_revisions",
                "selected_relative_path": "手順2025.csv",
                "current_confirmed": "yes",
                "use_approved": "yes",
            }
            source_rechecks = []

            def reattest(*, validate_source=False):
                source_rechecks.append(validate_source)
                return current

            with (
                mock.patch.object(module, "state", return_value={"phase": "ready"}),
                mock.patch.object(module, "home", side_effect=small_home),
                mock.patch.object(module, "build_worker", side_effect=note_rebuild),
                mock.patch.object(
                    module.bootstrap,
                    "current_document_version_review_context",
                    side_effect=reattest,
                ),
                mock.patch.object(module.bootstrap, "ENGINE", engine),
                mock.patch.object(module.bootstrap, "DOCUMENT_VERSION_DECISIONS", decisions),
            ):
                status, body = self.post("/document-version-decision", fields)
                self.assertEqual(status, 200)
                self.assertIn("索引の再構築を開始", body)
                self.assertTrue(rebuild_started.wait(2))
                first_bytes = decisions.read_bytes()
                replay_status, replay_body = self.post(
                    "/document-version-decision", fields
                )
            self.assertEqual(replay_status, 409)
            self.assertIn("保存しませんでした", replay_body)
            self.assertEqual(decisions.read_bytes(), first_bytes)
            self.assertEqual(rebuild_calls, ["rebuild"])
            self.assertEqual(source_rechecks, [True])
            saved = json.loads(first_bytes)["decisions"][0]
            self.assertEqual(saved["selected_relative_path"], "手順2025.csv")
            self.assertTrue(saved["current_applicability_confirmed"])
            self.assertTrue(saved["allow_ingest_index_answer"])

    def test_bad_csrf_is_rejected_before_save_or_rebuild(self):
        module = self.server_module
        with tempfile.TemporaryDirectory() as temporary:
            decisions = Path(temporary) / "decisions.json"
            with (
                mock.patch.object(module, "state", return_value={"phase": "ready"}),
                mock.patch.object(module.bootstrap, "DOCUMENT_VERSION_DECISIONS", decisions),
                mock.patch.object(module, "build_worker") as rebuild,
            ):
                status, _body = self.post(
                    "/document-version-decision",
                    {
                        module.UI_CSRF_FIELD: "wrong",
                        "review_ticket": "unused",
                        "relation": "defer",
                    },
                )
            self.assertEqual(status, 403)
            self.assertFalse(decisions.exists())
            rebuild.assert_not_called()

    def test_ask_rejects_stale_revision_before_answer_generation(self):
        module = self.server_module
        with (
            mock.patch.object(module, "state", return_value={"phase": "ready"}),
            mock.patch.object(module, "home", side_effect=lambda message="", *_a, **_k: module.page(str(message))),
            mock.patch.object(
                module.bootstrap,
                "active_answer_revision_identity",
                return_value=(False, "decision_revision_changed", None),
            ),
            mock.patch.object(module, "answer_query") as answer,
        ):
            status, body = self.post(
                "/ask",
                {module.UI_CSRF_FIELD: self.httpd.ui_csrf_token, "query": "受付は？"},
            )
        self.assertEqual(status, 409)
        self.assertIn("対応する索引がまだ完成していない", body)
        answer.assert_not_called()

    def test_ask_hides_generated_text_if_revision_changes_during_generation(self):
        module = self.server_module
        sentinel = "THIS_GENERATED_TEXT_MUST_NOT_BE_DISCLOSED"
        with (
            mock.patch.object(module, "state", return_value={"phase": "ready"}),
            mock.patch.object(module, "home", side_effect=lambda message="", *_a, **_k: module.page(str(message))),
            mock.patch.object(
                module.bootstrap,
                "active_answer_revision_identity",
                side_effect=[
                    (True, "current", {
                        "generation": "generation-" + "0" * 32,
                        "generation_path": "/synthetic/g0",
                        "decision_snapshot_sha256": "a" * 64,
                        "config_sha256": "b" * 64,
                    }),
                    (False, "decision_revision_changed", None),
                ],
            ),
            mock.patch.object(module, "answer_query", return_value={"answer": {"answer": sentinel}}) as answer,
        ):
            status, body = self.post(
                "/ask",
                {module.UI_CSRF_FIELD: self.httpd.ui_csrf_token, "query": "受付は？"},
            )
        self.assertEqual(status, 409)
        self.assertIn("回答作成中に資料の判断が変わった", body)
        self.assertNotIn(sentinel, body)
        answer.assert_called_once_with(
            "受付は？",
            expected_active_revision={
                "generation": "generation-" + "0" * 32,
                "generation_path": "/synthetic/g0",
                "decision_snapshot_sha256": "a" * 64,
                "config_sha256": "b" * 64,
            },
        )

    def test_ask_hides_old_answer_when_both_revisions_are_current_but_different(self):
        module = self.server_module
        sentinel = "OLD_GENERATION_ANSWER_MUST_NOT_BE_DISCLOSED"
        old_identity = {
            "generation": "generation-" + "0" * 32,
            "generation_path": "/synthetic/g0",
            "decision_snapshot_sha256": "a" * 64,
            "config_sha256": "b" * 64,
        }
        new_identity = {
            "generation": "generation-" + "1" * 32,
            "generation_path": "/synthetic/g1",
            "decision_snapshot_sha256": "c" * 64,
            "config_sha256": "d" * 64,
        }
        with (
            mock.patch.object(module, "state", return_value={"phase": "ready"}),
            mock.patch.object(
                module,
                "home",
                side_effect=lambda message="", *_a, **_k: module.page(str(message)),
            ),
            mock.patch.object(
                module.bootstrap,
                "active_answer_revision_identity",
                side_effect=[
                    (True, "current", old_identity),
                    (True, "current", new_identity),
                ],
            ),
            mock.patch.object(
                module,
                "answer_query",
                return_value={"answer": {"answer": sentinel, "answer_mode": "supported"}},
            ),
        ):
            status, body = self.post(
                "/ask",
                {module.UI_CSRF_FIELD: self.httpd.ui_csrf_token, "query": "受付は？"},
            )
        self.assertEqual(status, 409)
        self.assertNotIn(sentinel, body)

    def test_answer_query_rejects_captured_config_mismatch_before_model_start(self):
        module = self.server_module
        expected = {
            "generation": "generation-" + "0" * 32,
            "generation_path": "/synthetic/g0",
            "decision_snapshot_sha256": "a" * 64,
            "config_sha256": "b" * 64,
        }
        changed_config = {
            "active_generation": "generation-" + "1" * 32,
            "workspace": "/synthetic",
        }
        with (
            mock.patch.object(
                module.bootstrap,
                "load_config_snapshot",
                return_value=(True, changed_config),
            ),
            mock.patch.object(module.bootstrap, "start_ollama") as model_start,
        ):
            with self.assertRaisesRegex(
                RuntimeError, "answer_revision_changed_before_query"
            ):
                module.answer_query(
                    "受付は？", expected_active_revision=expected
                )
        model_start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
