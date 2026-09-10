"""Independent synthetic F03a holdouts; execute only through the audit guard.

Fixtures and exact membership/hash oracles are independent of executor tests.
Real resolver file APIs are exercised in fresh temporary directories. The only
app control reuses the reviewed stub-inference fixture with fresh source bytes.
"""
from __future__ import annotations
import contextlib
import copy
import hashlib
import importlib.util
import itertools
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"
PACKAGE = ROOT / "distribution/macos-local-memory"
builder = types.SimpleNamespace()
SIGNALS = ("explicit_years", "explicit_versions", "current_markers", "historical_markers", "draft_markers")


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


resolver = load(PACKAGE / "engine/document_version_resolver.py", "f03a_audit_resolver")
old = load(RUNS / "f03a-before-resolver.v1.py", "f03a_audit_old_resolver")


def record(path, salt):
    return {"relative_path": path, "kind": "file", "read_status": "observed",
            "sha256": hashlib.sha256(salt.encode()).hexdigest(),
            "size_bytes": 13, "mtime_ns": 21, "birthtime_ns": 8}


class IndependentF03aHoldouts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="f03a-independent-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.sequence = 0
        self.fixture_bytes = 0

    def build(self, records, decisions=None):
        self.sequence += 1
        case = self.base / f"case-{self.sequence}"
        case.mkdir()
        inventory, graph = case / "inventory.jsonl", case / "graph.json"
        text = "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in records)
        self.fixture_bytes += len(text.encode())
        self.assertLessEqual(self.fixture_bytes, 1048576)
        inventory.write_text(text, encoding="utf-8")
        choice_path = None
        if decisions is not None:
            choice_path = case / "decision.json"
            choice_path.write_text(json.dumps({"decisions": decisions}), encoding="utf-8")
        before = inventory.read_bytes()
        value = resolver.build(inventory, graph, choice_path)
        self.assertEqual(before, inventory.read_bytes())
        self.assertEqual(value, json.loads(graph.read_text()))
        self.assertEqual("PASS", resolver.validate(graph, inventory)["status"])
        return value, graph

    def held(self, result, records, reason="unmarked_candidate_requires_human_review", conflict=None):
        self.assertEqual({"groups": 1, "resolved": 0, "needs_human_review": 1}, result["counts"])
        group = result["groups"][0]
        expected = {item["relative_path"]: item["sha256"] for item in records}
        self.assertEqual(expected, {item["relative_path"]: item["source_sha256"] for item in group["candidates"]})
        self.assertEqual(len(records), len(group["candidates"]))
        self.assertEqual(sorted(expected, key=lambda p: p.encode()), [item["relative_path"] for item in group["candidates"]])
        self.assertEqual(reason, group["reason_code"])
        self.assertIsNone(group["selected_relative_path"])
        self.assertIsNone(group["resolution_basis"])
        self.assertTrue(all(item["disposition"] == "needs_human_review" for item in group["candidates"]))
        if conflict is not None:
            self.assertEqual([conflict], group["conflicts"])
        self.assertEqual(len(records), len(result["edges"]))
        self.assertTrue(all(edge["edge_type"] == "candidate_for" and edge["basis"] == "normalized_filename_family" and edge["status"] == "candidate" for edge in result["edges"]))
        return group

    def choose(self, graph, group, selected):
        target = graph.with_name("human.json")
        graph_before = graph.read_bytes()
        result = resolver.record_decision(graph, target, group["group_id"], selected, "independent-synthetic-human")
        self.assertEqual(graph_before, graph.read_bytes())
        self.assertEqual([result], json.loads(target.read_text())["decisions"])
        return result

    def test_every_allowed_suffix_and_five_signal_categories_hold_plain_peer(self):
        suffixes = (".csv", ".doc", ".docx", ".ods", ".pdf", ".ppt", ".pptx", ".tsv", ".xls", ".xlsx")
        for suffix, category in itertools.product(suffixes, range(5)):
            stem = "帳票" if category % 2 else "Ledger"
            marked = (f"2031/{stem}{suffix}", f"{stem}_VERSION_9.2{suffix}",
                      f"final/{stem}{suffix}", f"archive/{stem}{suffix}", f"{stem}_draft{suffix}")[category]
            records = [record(marked, "marked"), record(stem + suffix.upper(), "plain")]
            with self.subTest(suffix=suffix, category=category):
                value, _ = self.build(records)
                group = self.held(value, records)
                for item in group["candidates"]:
                    if item["relative_path"] == marked:
                        self.assertEqual(old.candidate(records[0]), {k: v for k, v in item.items() if k != "disposition"})
                    else:
                        self.assertTrue(all(item[key] == [] for key in SIGNALS))

    def test_multiple_families_and_input_order_have_exact_stable_membership(self):
        paths = ("部門/ＦＯＲＭ2028.csv", "部門/form.csv", "手引_ver4.pdf", "手引.pdf", "独立.csv", "独立コピー.csv")
        records = [record(path, str(i)) for i, path in enumerate(paths)]
        expected = {frozenset(paths[:2]), frozenset(paths[2:4])}
        baseline, _ = self.build(records)
        self.assertEqual(expected, {frozenset(c["relative_path"] for c in group["candidates"]) for group in baseline["groups"]})
        self.assertEqual({"groups": 2, "resolved": 0, "needs_human_review": 2}, baseline["counts"])
        for ordered in (records[::-1], records[2:] + records[:2], records[::2] + records[1::2]):
            value, _ = self.build(ordered)
            for key in ("groups", "nodes", "edges"):
                self.assertEqual(baseline[key], value[key])

    def test_removing_selected_or_unselected_plain_member_stales_formed_group(self):
        records = [record("approved/Checklist.csv", "m"), record("Checklist.csv", "u"), record("Checklist copy.csv", "x")]
        initial, graph = self.build(records)
        group = self.held(initial, records)
        choice = self.choose(graph, group, "Checklist.csv")
        for removed in (1, 2):
            remaining = [item for i, item in enumerate(records) if i != removed]
            value, _ = self.build(remaining, [choice])
            self.held(value, remaining, "stale_human_decision", "candidate_set_changed")

    def test_same_set_wrong_source_or_missing_selection_cannot_activate(self):
        records = [record("Log_ver8.csv", "m"), record("Log.csv", "u")]
        initial, graph = self.build(records)
        group = self.held(initial, records)
        choice = self.choose(graph, group, "Log.csv")
        mutations = (("selected_source_sha256", "0" * 64, "selected_source_changed"),
                     ("selected_relative_path", "absent.csv", "selected_candidate_missing"))
        for field, changed, conflict in mutations:
            value, _ = self.build(records, [{**choice, field: changed}])
            self.held(value, records, "stale_human_decision", conflict)

    def test_plain_to_marked_rename_stales_choice_before_numeric_automatic_selection(self):
        records = [record("Checklist_ver1.csv", "m"), record("Checklist.csv", "u")]
        initial, graph = self.build(records)
        choice = self.choose(graph, initial["groups"][0], "Checklist.csv")
        changed = [records[0], {**records[1], "relative_path": "Checklist_ver2.csv"}]
        without, _ = self.build(changed)
        self.assertEqual("Checklist_ver2.csv", without["groups"][0]["selected_relative_path"])
        with_choice, _ = self.build(changed, [choice])
        self.held(with_choice, changed, "stale_human_decision", "candidate_set_changed")

    def test_existing_metadata_hash_representation_and_plain_decision_are_preserved(self):
        records = [record("Log_ver1.csv", "m"), record("Log.csv", "u")]
        initial, graph = self.build(records)
        group = self.held(initial, records)
        choice = self.choose(graph, group, "Log.csv")
        for field in ("mtime_ns", "birthtime_ns"):
            changed = [records[0], {**records[1], field: records[1][field] + 1}]
            value, _ = self.build(changed, [choice])
            self.held(value, changed, "stale_human_decision", "candidate_set_changed")
        # size_bytes is deliberately absent from the preexisting set hash.
        changed_size = [records[0], {**records[1], "size_bytes": 99}]
        value, _ = self.build(changed_size, [choice])
        self.assertEqual(group["candidate_set_sha256"], value["groups"][0]["candidate_set_sha256"])
        self.assertEqual("Log.csv", value["groups"][0]["selected_relative_path"])
        self.assertEqual("human", value["groups"][0]["resolution_basis"])

    def test_ineligible_plain_records_do_not_poison_numeric_selection(self):
        base = [record("Checklist_ver1.csv", "a"), record("Checklist_ver2.csv", "b")]
        valid, _ = self.build(base)
        for patch in ({"kind": "symlink"}, {"read_status": "unreadable"}, {"sha256": None}, {"relative_path": "Checklist.exe"}):
            invalid = {**record("Checklist.csv", "invalid"), **patch}
            value, _ = self.build([*base, invalid])
            self.assertEqual(valid["groups"], value["groups"])
        missing = record("Checklist.csv", "missing")
        del missing["sha256"]
        value, _ = self.build([*base, missing])
        self.assertEqual(valid["groups"], value["groups"])

    def test_all_marked_differential_corpus_matches_saved_before_semantics(self):
        paths = ("Guide2024.csv", "Guide2025.csv", "Guide_ver1.csv", "Guide_ver1.2.csv", "Guide_ver1.10.csv",
                 "current/Guide.csv", "archive/Guide.csv", "draft/Guide.csv", "current/Guide_ver1.csv",
                 "Guide_ver2_draft.csv", "Guide2026_ver2.csv", "approved/Guide2025.csv")
        for left, right in itertools.combinations(paths, 2):
            records = [record(left, left), record(right, right)]
            before_candidates = [old.candidate(item) for item in records]
            after_candidates = [resolver.candidate(item) for item in records]
            self.assertEqual(before_candidates, after_candidates)
            key = old.family_key(left)
            self.assertEqual(key, old.family_key(right))
            self.assertEqual(key, resolver.family_key(left))
            before = old.resolve_group(key, copy.deepcopy(before_candidates), None)
            after = resolver.resolve_group(key, copy.deepcopy(after_candidates), None)
            self.assertEqual(before, after)
            self.assertEqual(old.graph_projection([before]), resolver.graph_projection([after]))

    def test_all_unmarked_same_family_still_reaches_real_reader_and_safe_index(self):
        fixture = load(PACKAGE / "tests/test_versioned_safe_index_e2e.py", "f03a_audit_app_fixture")
        harness = fixture.VersionedSafeIndexE2E()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        files = {"Ledger.csv": "item,value\none,11\n", "Ledger copy.csv": "item,value\ntwo,22\n"}
        for name, text in files.items():
            (harness.source / name).write_text(text, encoding="utf-8")
        originals = {p.name: p.read_bytes() for p in harness.source.iterdir()}
        harness.bootstrap.build_index()
        config = harness.bootstrap.load_json(harness.bootstrap.CONFIG)
        graph = harness.bootstrap.load_json(Path(config["path_graph_path"]) / "document-version-graph.json")
        self.assertEqual([], graph["groups"])
        self.assertEqual("current", harness.bootstrap.reader_generation_contract_status(config)["state"])
        with contextlib.closing(sqlite3.connect(config["index_path"])) as connection:
            self.assertEqual(set(files), {row[0] for row in connection.execute("SELECT relative_path FROM evidence")})
            binding = json.loads(connection.execute("SELECT value FROM metadata WHERE key='document_version_graph'").fetchone()[0])
            self.assertEqual(graph["graph_sha256"], binding["graph_sha256"])
        self.assertEqual(originals, {p.name: p.read_bytes() for p in harness.source.iterdir()})


if __name__ == "__main__":
    raise SystemExit("Use f03a-audit-run.v1.py holdout with its reviewed guard")
