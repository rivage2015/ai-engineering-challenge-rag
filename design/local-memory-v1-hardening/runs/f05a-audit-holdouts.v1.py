"""Independent F05a boundary holdouts, run only in the reviewed audit guard.

No real source documents or app/model imports. Every serialized fixture write
is bounded: <=16 inventory records, 16 KiB inventory, 8 KiB decisions, 64 KiB
graphs and 1 MiB cumulative for this process. Mutations are attacker fixtures.
"""
from __future__ import annotations
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "distribution/macos-local-memory/engine/document_version_resolver.py"
spec = importlib.util.spec_from_file_location("f05a_independent_resolver", SOURCE)
resolver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resolver)
SERIALIZED_BYTES = 0


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def record(path, salt):
    return {"kind": "file", "read_status": "observed", "relative_path": path,
            "sha256": sha(salt.encode()), "size_bytes": 13, "mtime_ns": 21, "birthtime_ns": 8}


class IndependentReconstructionHoldouts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="f05a-independent-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.inventory = self.base / "inventory.jsonl"
        self.graph = self.base / "graph.json"
        self.decisions = self.base / "decisions.json"
        patch = mock.patch.object(resolver, "atomic_json", side_effect=self.atomic)
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, path, raw, cap):
        global SERIALIZED_BYTES
        raw = raw.encode("utf-8") if isinstance(raw, str) else raw
        self.assertLessEqual(len(raw), cap)
        SERIALIZED_BYTES += len(raw)
        self.assertLessEqual(SERIALIZED_BYTES, 1048576)
        path.write_bytes(raw)

    def atomic(self, path, value):
        self.write(path, canonical(value), 8192 if path == self.decisions else 65536)

    def fixture(self, records=None, *, decisions=None, newline="\n"):
        records = records if records is not None else [record("Memo_ver1.csv", "marked"), record("Memo.csv", "plain")]
        self.assertLessEqual(len(records), 16)
        self.write(self.inventory, "".join(canonical(item) + newline for item in records), 16384)
        return resolver.build(self.inventory, self.graph, decisions)

    def seal(self, graph):
        value = copy.deepcopy(graph)
        value["graph_sha256"] = sha(canonical({k: v for k, v in value.items() if k != "graph_sha256"}).encode())
        self.atomic(self.graph, value)
        return value

    def validate(self, decision_path=None):
        before = {path: path.read_bytes() for path in (self.graph, self.inventory, self.decisions) if path.exists()}
        with contextlib.ExitStack() as stack:
            for name in ("build", "atomic_json", "record_decision", "load_inventory", "load_decisions", "sha256_file"):
                stack.enter_context(mock.patch.object(resolver, name, side_effect=AssertionError("validation used disallowed API: " + name)))
            result = resolver.validate(self.graph, self.inventory, decisions_path=decision_path)
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertEqual({"status", "errors"}, set(result))
        self.assertIsInstance(result["errors"], list)
        return result

    def fails(self, decision_path=None, error=None):
        result = self.validate(decision_path)
        self.assertEqual("FAIL", result["status"], result)
        self.assertTrue(result["errors"])
        if error:
            self.assertTrue(any(error in item for item in result["errors"]), result)

    def succeeds(self, decision_path=None):
        self.assertEqual({"status": "PASS", "errors": []}, self.validate(decision_path))

    def choose(self, graph):
        resolver.record_decision(self.graph, self.decisions, graph["groups"][0]["group_id"], "Memo.csv", "independent-synthetic-human")
        result = resolver.build(self.inventory, self.graph, self.decisions)
        self.assertEqual("Memo.csv", result["groups"][0]["selected_relative_path"])
        self.assertEqual("human", result["groups"][0]["resolution_basis"])
        return result

    def test_valid_unicode_line_separators_and_universal_newlines(self):
        records = [record("台帳\u2028_ver1.csv", "a"), record("台帳\u2028.csv", "b")]
        for newline in ("\n", "\r\n", "\r"):
            with self.subTest(newline=repr(newline)):
                graph = self.fixture(records, newline=newline)
                self.assertEqual("unmarked_candidate_requires_human_review", graph["groups"][0]["reason_code"])
                self.assertEqual({r["relative_path"] for r in records}, {c["relative_path"] for c in graph["groups"][0]["candidates"]})
                self.succeeds()

    def two_families(self):
        records = [record("Memo_ver1.csv", "a"), record("Memo.csv", "b"),
                   record("Plan_ver2.csv", "c"), record("Plan_ver3.csv", "d")]
        graph = self.fixture(records)
        self.assertEqual({"groups": 2, "resolved": 1, "needs_human_review": 1}, graph["counts"])
        self.assertEqual({None, "Plan_ver3.csv"}, {g["selected_relative_path"] for g in graph["groups"]})
        self.succeeds()
        return graph

    def test_reordered_complete_semantic_collections_fail(self):
        original = self.two_families()
        for field in ("groups", "nodes", "edges"):
            graph = copy.deepcopy(original)
            graph[field].reverse()
            self.seal(graph)
            self.fails(error="version_graph_" + field + "_mismatch")

    def test_whole_family_omission_cannot_be_resealed_away(self):
        graph = self.two_families()
        removed = next(g for g in graph["groups"] if g["status"] == "resolved")
        removed_paths = {c["relative_path"] for c in removed["candidates"]}
        removed_ids = {n["node_id"] for n in graph["nodes"] if n.get("relative_path") in removed_paths}
        removed_ids.add(removed["group_id"])
        graph["groups"] = [g for g in graph["groups"] if g is not removed]
        graph["nodes"] = [n for n in graph["nodes"] if n["node_id"] not in removed_ids]
        graph["edges"] = [e for e in graph["edges"] if e["from_node_id"] not in removed_ids and e["to_node_id"] not in removed_ids]
        graph["counts"] = {"groups": 1, "resolved": 0, "needs_human_review": 1}
        self.seal(graph)
        self.fails(error="version_graph_groups_mismatch")

    def test_nested_duplicate_keys_in_every_snapshot_fail(self):
        for kind in ("graph", "inventory", "decisions"):
            graph = self.fixture()
            if kind == "graph":
                self.write(self.graph, canonical(graph)[:-1] + ',"unused":{"k":1,"k":2}}', 65536)
                decision_path = None
            elif kind == "inventory":
                lines = self.inventory.read_text().splitlines()
                lines[0] = lines[0][:-1] + ',"unused":{"k":1,"k":2}}'
                raw = ("\n".join(lines) + "\n").encode()
                self.write(self.inventory, raw, 16384)
                graph["source"]["inventory_sha256"] = sha(raw)
                self.seal(graph)
                decision_path = None
            else:
                raw = b'{"decisions":[],"unused":{"k":1,"k":2}}'
                self.write(self.decisions, raw, 8192)
                graph["source"]["decisions_sha256"] = sha(raw)
                self.seal(graph)
                decision_path = self.decisions
            self.fails(decision_path, "duplicate_json_key")

    def test_nonfinite_values_in_unused_input_fields_fail(self):
        for kind in ("graph", "inventory", "decisions"):
            for number in ("NaN", "Infinity", "-Infinity", "1e999", "-1e999"):
                with self.subTest(kind=kind, number=number):
                    graph = self.fixture()
                    addition = ',"unused":' + number + '}'
                    if kind == "graph":
                        self.write(self.graph, canonical(graph)[:-1] + addition, 65536)
                        decision_path = None
                    elif kind == "inventory":
                        lines = self.inventory.read_text().splitlines()
                        lines[0] = lines[0][:-1] + addition
                        raw = ("\n".join(lines) + "\n").encode()
                        self.write(self.inventory, raw, 16384)
                        graph["source"]["inventory_sha256"] = sha(raw)
                        self.seal(graph)
                        decision_path = None
                    else:
                        raw = ('{"decisions":[]' + addition).encode()
                        self.write(self.decisions, raw, 8192)
                        graph["source"]["decisions_sha256"] = sha(raw)
                        self.seal(graph)
                        decision_path = self.decisions
                    self.fails(decision_path, "nonfinite_json_")

    def test_numeric_types_are_not_coerced(self):
        initial = self.fixture()
        for field, value in (("size_bytes", 13.0), ("mtime_ns", 21.0), ("birthtime_ns", 8.0), ("explicit_versions", [[True]])):
            graph = copy.deepcopy(initial)
            marked = next(c for c in graph["groups"][0]["candidates"] if c["relative_path"] == "Memo_ver1.csv")
            marked[field] = value
            self.seal(graph)
            self.fails(error="version_graph_groups_mismatch")
        graph = copy.deepcopy(initial)
        graph["counts"]["groups"] = 1.0
        self.seal(graph)
        self.fails(error="version_graph_counts_mismatch")

    def test_explicit_snapshot_canaries_and_single_reads(self):
        graph = self.choose(self.fixture())
        graph["source"]["inventory_path"] = "/f05a-never-follow/inventory.jsonl"
        graph["source"]["decisions_path"] = "/f05a-never-follow/decisions.json"
        self.seal(graph)
        snapshots = {p: p.read_bytes() for p in (self.graph, self.inventory, self.decisions)}
        calls = []

        def read_snapshot(path):
            self.assertIn(path, snapshots)
            self.assertNotIn(path, calls)
            calls.append(path)
            return snapshots[path]

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(Path, "read_bytes", read_snapshot))
            for name in ("open", "read_text", "stat", "lstat", "exists", "resolve"):
                stack.enter_context(mock.patch.object(Path, name, side_effect=AssertionError("unexpected path IO: " + name)))
            for name in ("build", "atomic_json", "record_decision", "load_inventory", "load_decisions", "sha256_file"):
                stack.enter_context(mock.patch.object(resolver, name, side_effect=AssertionError("unexpected resolver IO: " + name)))
            result = resolver.validate(self.graph, self.inventory, decisions_path=self.decisions)
        self.assertEqual({"status": "PASS", "errors": []}, result)
        self.assertCountEqual(list(snapshots), calls)
        self.assertEqual(snapshots, {p: p.read_bytes() for p in snapshots})
        self.fails(error="decision_authority_required")

    def test_mutation_after_inventory_read_uses_snapshot_then_next_validation_fails(self):
        self.fixture()
        original_read = Path.read_bytes
        graph_before = self.graph.read_bytes()
        changed = (canonical(record("Different.csv", "different")) + "\n").encode()
        calls = []

        def racing_read(path):
            calls.append(path)
            raw = original_read(path)
            if path == self.inventory:
                # Test-controlled mutation after returning the captured bytes;
                # this is not an assertion that source freshness is protected.
                self.write(self.inventory, changed, 16384)
            return raw

        with mock.patch.object(Path, "read_bytes", racing_read), mock.patch.object(resolver, "atomic_json", side_effect=AssertionError("validator write")), mock.patch.object(resolver, "build", side_effect=AssertionError("validator build")):
            result = resolver.validate(self.graph, self.inventory)
        self.assertEqual({"status": "PASS", "errors": []}, result)
        self.assertEqual([self.graph, self.inventory], calls)
        self.assertEqual(graph_before, self.graph.read_bytes())
        self.assertEqual(changed, self.inventory.read_bytes())
        self.fails(error="inventory_changed")

    def test_unreadable_optional_authority_fails_and_guard_assertion_propagates(self):
        self.fixture()
        original_read = Path.read_bytes
        for failure in (PermissionError("synthetic denied"), IsADirectoryError("synthetic directory")):
            def fail_decision(path):
                if path == self.decisions:
                    raise failure
                return original_read(path)
            with mock.patch.object(Path, "read_bytes", fail_decision):
                result = resolver.validate(self.graph, self.inventory, self.decisions)
            self.assertEqual("FAIL", result["status"])
            self.assertTrue(any(type(failure).__name__ in e for e in result["errors"]))
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("independent guard sentinel")):
            with self.assertRaisesRegex(AssertionError, "independent guard sentinel"):
                resolver.validate(self.graph, self.inventory)

    def test_cli_missing_optional_decision_and_deleted_recorded_authority(self):
        missing = self.base / "never-created.json"
        self.fixture(decisions=missing)
        argv = ["resolver", "validate", "--graph", str(self.graph), "--inventory", str(self.inventory), "--decisions", str(missing)]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, resolver.main())
        self.assertEqual({"status": "PASS", "errors": []}, json.loads(output.getvalue()))
        self.choose(self.fixture())
        self.decisions.unlink()
        self.fails(self.decisions, "decisions_changed")
        self.fails(error="decision_authority_required")

    def test_duplicate_ineligible_inventory_and_unused_decision_ids_fail(self):
        records = [record("Memo_ver1.csv", "m"), record("Memo.csv", "u")]
        self.fixture([*records, {**records[0], "kind": "directory"}])
        self.fails(error="duplicate_inventory_relative_path")
        graph = self.fixture()
        raw = b'{"decisions":[{"group_id":"unused"},{"group_id":"unused"}]}'
        self.write(self.decisions, raw, 8192)
        graph["source"]["decisions_sha256"] = sha(raw)
        self.seal(graph)
        self.fails(self.decisions, "duplicate_decision_group_id")


if __name__ == "__main__":
    raise SystemExit("Run using f05a-audit-run.v1.py holdout under its guard")
