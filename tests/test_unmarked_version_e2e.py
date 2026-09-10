"""F03a synthetic application wiring, not a real-model or release test.

Reuses the already reviewed F01 CLI harness. Only its external inference and
runtime metadata boundaries are stubbed; version/Reader/index APIs are real.
Run with design/.../f03a-root-run.v1.py after side-effect preflight.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "distribution/macos-local-memory"
SPEC = importlib.util.spec_from_file_location(
    "f03a_existing_e2e", PACKAGE / "tests/test_versioned_safe_index_e2e.py",
)
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)
builder = types.SimpleNamespace()  # The outer guarded dispatcher slot; unused.


class UnmarkedVersionApplicationTests(unittest.TestCase):
    def setUp(self):
        self.h = fixture.VersionedSafeIndexE2E()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def seed(self, files):
        self.assertLessEqual(sum(len(text.encode()) for text in files.values()), 16384)
        for name, text in files.items():
            path = self.h.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def originals(self):
        return {
            str(path.relative_to(self.h.source)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.h.source.rglob("*") if path.is_file()
        }

    def current(self):
        config = self.h.bootstrap.load_json(self.h.bootstrap.CONFIG)
        graph_path = Path(config["path_graph_path"]) / "document-version-graph.json"
        graph = self.h.bootstrap.load_json(graph_path)
        with contextlib.closing(sqlite3.connect(config["index_path"])) as db:
            paths = {row[0] for row in db.execute("SELECT relative_path FROM evidence")}
            binding = json.loads(db.execute(
                "SELECT value FROM metadata WHERE key='document_version_graph'",
            ).fetchone()[0])
        self.assertEqual(graph["graph_sha256"], binding["graph_sha256"])
        self.assertEqual("current", self.h.bootstrap.reader_generation_contract_status(config)["state"])
        return config, graph_path, graph, paths

    def assert_held(self, graph, candidates, reason):
        self.assertEqual({"groups": 1, "resolved": 0, "needs_human_review": 1}, graph["counts"])
        group = graph["groups"][0]
        self.assertEqual(set(candidates), {c["relative_path"] for c in group["candidates"]})
        self.assertEqual(len(candidates), len(group["candidates"]))
        self.assertEqual(reason, group["reason_code"])
        self.assertIsNone(group["selected_relative_path"])
        self.assertIsNone(group["resolution_basis"])
        self.assertTrue(all(c["disposition"] == "needs_human_review" for c in group["candidates"]))
        self.assertFalse(any(e["edge_type"] == "active_version" for e in graph["edges"]))

    def test_year_and_unmarked_hold_without_suppressing_unrelated_contact(self):
        self.seed({
            "手順2024.csv": "task,owner\nold,alice\n",
            "手順.csv": "task,owner\nother,bob\n",
            "連絡先.txt": "contact: front desk\n",
        })
        before = self.originals()
        self.h.bootstrap.build_index()
        config, _graph_path, graph, paths = self.current()
        self.assert_held(graph, {"手順2024.csv", "手順.csv"}, "unmarked_candidate_requires_human_review")
        self.assertEqual({"連絡先.txt"}, paths)
        reader = self.h.bootstrap.load_json(Path(config["semantic_path"]) / "adaptive-reader-state.json")
        self.assertEqual(2, reader["limitations"]["version_files_needing_human_review"])
        self.assertEqual(0, reader["limitations"]["historical_version_files_held"])
        self.assertEqual(before, self.originals())

    def test_full_set_human_choice_can_publish_unmarked_member(self):
        self.seed({
            "current/Guide.csv": "task,owner\nmarked,alice\n",
            "Guide.csv": "task,owner\nunmarked,bob\n",
            "Contact.txt": "contact: front desk\n",
        })
        before = self.originals()
        self.h.bootstrap.build_index()
        first, graph_path, graph, paths = self.current()
        self.assert_held(graph, {"current/Guide.csv", "Guide.csv"}, "unmarked_candidate_requires_human_review")
        self.assertEqual({"Contact.txt"}, paths)
        resolver = self.h.module(PACKAGE / "engine/document_version_resolver.py")
        resolver.record_decision(
            graph_path, self.h.bootstrap.DOCUMENT_VERSION_DECISIONS,
            graph["groups"][0]["group_id"], "Guide.csv", "synthetic-human",
        )
        self.h.bootstrap.build_index()
        second, _path, chosen, paths = self.current()
        group = chosen["groups"][0]
        self.assertEqual("human", group["resolution_basis"])
        self.assertEqual("Guide.csv", group["selected_relative_path"])
        self.assertEqual({"Guide.csv", "Contact.txt"}, paths)
        self.assertEqual("human_confirmed_active", group["reason_code"])
        self.assertNotEqual(first["active_generation"], second["active_generation"])
        self.assertEqual(before, self.originals())

    def test_unmarked_addition_invalidates_prior_bound_marked_choice(self):
        self.seed({
            "Guide_ver1.csv": "task,owner\nold,alice\n",
            "Guide_ver2.csv": "task,owner\nnew,bob\n",
            "Contact.txt": "contact: front desk\n",
        })
        self.h.bootstrap.build_index()
        _first, graph_path, graph, _paths = self.current()
        resolver = self.h.module(PACKAGE / "engine/document_version_resolver.py")
        resolver.record_decision(
            graph_path, self.h.bootstrap.DOCUMENT_VERSION_DECISIONS,
            graph["groups"][0]["group_id"], "Guide_ver2.csv", "synthetic-human",
        )
        self.h.bootstrap.build_index()
        chosen_config, _graph_path, chosen, paths = self.current()
        self.assertEqual("human", chosen["groups"][0]["resolution_basis"])
        self.assertEqual({"Guide_ver2.csv", "Contact.txt"}, paths)
        self.seed({"Guide.csv": "task,owner\nunknown,cara\n"})
        before = self.originals()
        self.h.bootstrap.build_index()
        changed, _path, held, paths = self.current()
        self.assert_held(held, {"Guide_ver1.csv", "Guide_ver2.csv", "Guide.csv"}, "stale_human_decision")
        self.assertEqual(["candidate_set_changed"], held["groups"][0]["conflicts"])
        self.assertEqual(chosen["groups"][0]["group_id"], held["groups"][0]["group_id"])
        self.assertNotEqual(chosen["groups"][0]["candidate_set_sha256"], held["groups"][0]["candidate_set_sha256"])
        self.assertNotEqual(chosen_config["active_generation"], changed["active_generation"])
        self.assertEqual({"Contact.txt"}, paths)
        self.assertEqual(before, self.originals())

    def test_all_mixed_candidates_held_does_not_replace_prior_config_or_index(self):
        self.h.seed()
        self.h.bootstrap.build_index()
        config_before = self.h.bootstrap.CONFIG.read_bytes()
        index = Path(self.h.bootstrap.load_json(self.h.bootstrap.CONFIG)["index_path"])
        index_before = hashlib.sha256(index.read_bytes()).hexdigest()
        (self.h.source / "業務内容_ver1.csv").rename(self.h.source / "業務内容.csv")
        contact = self.h.base / "saved-contact.txt"
        (self.h.source / "連絡先.txt").rename(contact)
        before = self.originals()
        contact_before = contact.read_bytes()
        command_count = len(self.h.commands)
        with self.assertRaisesRegex(SystemExit, "ValueError:adaptive_reader_no_supported_files"):
            self.h.bootstrap.build_index()
        self.assertEqual(config_before, self.h.bootstrap.CONFIG.read_bytes())
        self.assertEqual(index_before, hashlib.sha256(index.read_bytes()).hexdigest())
        self.assertEqual(before, self.originals())
        self.assertEqual(contact_before, contact.read_bytes())
        self.assertFalse(any(Path(c[1]).name == "build_local_semantic_index.py" for c in self.h.commands[command_count:]))
        pending = self.h.bootstrap.load_json(self.h.bootstrap.DOCUMENT_VERSION_REVIEW)
        self.assert_held(pending, {"業務内容.csv", "業務内容_ver2.csv"}, "unmarked_candidate_requires_human_review")
        # F06 review generation and F13 freshness of the retained old index
        # remain open. Preserved bytes are not a current-source attestation.


if __name__ == "__main__":
    unittest.main(verbosity=2)
