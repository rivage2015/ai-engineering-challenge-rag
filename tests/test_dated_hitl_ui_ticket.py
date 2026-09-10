"""Synthetic UI ticket tests; no real config, source documents, or app."""
from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import threading
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "distribution/macos-local-memory/app"


def load_server():
    prior = sys.modules.pop("bootstrap", None)
    sys.path.insert(0, str(APP))
    try:
        spec = importlib.util.spec_from_file_location("dated_ticket_server", APP / "local_memory_server.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(APP))
        sys.modules.pop("bootstrap", None)
        if prior is not None:
            sys.modules["bootstrap"] = prior


class FakeServer:
    def __init__(self):
        self.review_ticket_lock = threading.Lock()
        self.review_tickets = {}


def context():
    group_id = "version_set_" + "1" * 32
    set_hash = "a" * 64
    group = {
        "group_id": group_id, "candidate_set_sha256": set_hash,
        "status": "needs_human_review", "reason_code": "dated_family_requires_human_review",
        "candidates": [
            {"relative_path": "手順2024.csv", "source_sha256": "b" * 64,
             "size_bytes": 10, "mtime_ns": 2, "birthtime_ns": 1,
             "explicit_years": [2024], "explicit_versions": [], "current_markers": [],
             "historical_markers": [], "draft_markers": []},
            {"relative_path": "手順2025.csv", "source_sha256": "c" * 64,
             "size_bytes": 10, "mtime_ns": 4, "birthtime_ns": 3,
             "explicit_years": [2025], "explicit_versions": [], "current_markers": [],
             "historical_markers": [], "draft_markers": []},
        ],
    }
    base = {
        "generation": "generation-" + "2" * 32,
        "source_scope_sha256": "3" * 64, "graph_sha256": "4" * 64,
        "graph_file_sha256": "5" * 64, "inventory_sha256": "6" * 64,
        "decisions_sha256": None, "resolver_version": "0.1.6",
    }
    return {
        "graph": {"groups": [group]}, "family_keys": {group_id: "\0手順\0.csv"},
        "dated_group_ids": [group_id], "base_revision": base,
        "decision_store_revision": {"exists": False, "sha256": None, "byte_count": 0},
    }


class DatedHitlUiTicketTests(unittest.TestCase):
    def setUp(self):
        self.server_module = load_server()
        self.server = FakeServer()

    def test_render_issues_opaque_ticket_without_exposing_revision(self):
        current = context()
        core = {"groups": current["graph"]["groups"]}
        raw = json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        graph = {**core, "graph_sha256": hashlib.sha256(raw.encode()).hexdigest()}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "review.json"
            path.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
            with (
                mock.patch.object(
                    self.server_module.bootstrap, "current_document_version_review_context",
                    return_value=current,
                ),
                mock.patch.object(self.server_module.bootstrap, "DOCUMENT_VERSION_REVIEW", path),
            ):
                issuer = self.server_module.review_ticket_issuer(self.server)
                rendered = self.server_module.document_version_review_notice("csrf", issuer)
        self.assertIn("手順2024.csv", rendered)
        self.assertIn("手順2025.csv", rendered)
        self.assertIn('name="review_ticket"', rendered)
        self.assertIn("independent_records", rendered)
        self.assertIn("今は判断しない", rendered)
        self.assertNotIn(current["base_revision"]["graph_sha256"], rendered)
        self.assertEqual(len(self.server.review_tickets), 1)

    def test_temporal_ticket_uses_full_consent_form_even_without_valid_year(self):
        current = context()
        for index, item in enumerate(current["graph"]["groups"][0]["candidates"]):
            item["relative_path"] = f"手順-invalid-20{25 + index}1340.csv"
            item["explicit_years"] = []
        core = {"groups": current["graph"]["groups"]}
        raw = json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        graph = {**core, "graph_sha256": hashlib.sha256(raw.encode()).hexdigest()}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "review.json"
            path.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
            with (
                mock.patch.object(
                    self.server_module.bootstrap,
                    "current_document_version_review_context",
                    return_value=current,
                ),
                mock.patch.object(
                    self.server_module.bootstrap, "DOCUMENT_VERSION_REVIEW", path
                ),
            ):
                rendered = self.server_module.document_version_review_notice(
                    "csrf", self.server_module.review_ticket_issuer(self.server)
                )
        self.assertIn('name="review_ticket"', rendered)
        self.assertIn('name="relation" value="same_work_revisions"', rendered)
        self.assertNotIn('name="group_id"', rendered)

    def test_ticket_is_single_use(self):
        current = context()
        with mock.patch.object(
            self.server_module.bootstrap, "current_document_version_review_context",
            return_value=current,
        ):
            token = self.server_module.review_ticket_issuer(self.server)(current["graph"]["groups"][0])
        self.server_module.consume_review_ticket(self.server, token)
        with self.assertRaisesRegex(ValueError, "review_ticket_invalid"):
            self.server_module.consume_review_ticket(self.server, token)

    def test_submit_reattests_source_and_passes_displayed_store_revision_to_cas(self):
        current = context()
        with mock.patch.object(
            self.server_module.bootstrap, "current_document_version_review_context",
            return_value=current,
        ):
            token = self.server_module.review_ticket_issuer(self.server)(current["graph"]["groups"][0])
        resolver = mock.Mock()
        resolver.prepare_dated_consent.return_value = {"record": "prepared"}
        with (
            mock.patch.object(
                self.server_module.bootstrap, "current_document_version_review_context",
                return_value=current,
            ) as review,
            mock.patch.object(self.server_module.bootstrap, "_decision_resolver", return_value=resolver),
        ):
            rebuild = self.server_module.save_dated_review_submission(self.server, {
                "review_ticket": [token], "relation": ["same_work_revisions"],
                "selected_relative_path": ["手順2025.csv"],
                "current_confirmed": ["yes"], "use_approved": ["yes"],
            })
        self.assertTrue(rebuild)
        review.assert_called_once_with(validate_source=True)
        resolver.record_dated_consent_cas.assert_called_once()
        self.assertIsNone(resolver.record_dated_consent_cas.call_args.args[2])

    def test_real_prepare_and_cas_persist_only_selected_current_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            decisions = Path(temporary) / "decisions.json"
            engine = ROOT / "distribution/macos-local-memory/engine"
            with mock.patch.object(self.server_module.bootstrap, "ENGINE", engine):
                resolver = self.server_module.bootstrap._decision_resolver()
            current = context()
            key = "\0手順\0.csv"
            group = current["graph"]["groups"][0]
            group_id = "version_set_" + resolver.sha256_json({"family_key": key})[:32]
            group["group_id"] = group_id
            group["candidate_set_sha256"] = resolver.candidate_set_hash(group["candidates"])
            current["family_keys"] = {group_id: key}
            current["dated_group_ids"] = [group_id]
            with mock.patch.object(
                self.server_module.bootstrap, "current_document_version_review_context",
                return_value=current,
            ):
                token = self.server_module.review_ticket_issuer(self.server)(group)
            with (
                mock.patch.object(
                    self.server_module.bootstrap, "current_document_version_review_context",
                    return_value=current,
                ),
                mock.patch.object(self.server_module.bootstrap, "ENGINE", engine),
                mock.patch.object(
                    self.server_module.bootstrap, "DOCUMENT_VERSION_DECISIONS", decisions,
                ),
            ):
                self.assertTrue(self.server_module.save_dated_review_submission(self.server, {
                    "review_ticket": [token], "relation": ["same_work_revisions"],
                    "selected_relative_path": ["手順2025.csv"],
                    "current_confirmed": ["yes"], "use_approved": ["yes"],
                }))
            saved = json.loads(decisions.read_text(encoding="utf-8"))["decisions"][0]
            self.assertEqual(saved["selected_relative_path"], "手順2025.csv")
            self.assertTrue(saved["current_applicability_confirmed"])
            self.assertTrue(saved["allow_ingest_index_answer"])


if __name__ == "__main__":
    unittest.main()
