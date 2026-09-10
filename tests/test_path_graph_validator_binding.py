"""Bounded synthetic Path Validator tests; no application/model/source data."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ENGINE = Path(__file__).resolve().parents[1] / "distribution/macos-local-memory/engine"


def load(name, path):
    specification = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


builder = load("validator_fixture_builder", ENGINE / "build_path_graph.py")
validator = load("source_bound_path_validator", ENGINE / "validate_path_graph.py")


class PathValidatorBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="lms-path-validator-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "source"
        self.root.mkdir()
        self.leaf = self.root / "memo.txt"
        self.leaf.write_bytes(b"SOURCE")
        self.outside = self.base / "outside"
        self.outside.mkdir()
        self.canary = self.outside / "canary.txt"
        self.canary.write_bytes(b"OUTSIDE")
        self.graph_path = self.base / "graph.json"
        self.inventory_path = self.base / "inventory.jsonl"
        self.snapshot()
        for target, name in ((http.client.HTTPConnection, "connect"), (subprocess, "Popen")):
            patcher = mock.patch.object(target, name, side_effect=AssertionError("external IO forbidden"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def snapshot(self):
        entries, errors = builder.enumerate_entries(self.root)
        self.assertEqual(errors, [])
        self.records = builder.inventory_records(entries)
        payload = builder.inventory_jsonl(self.records)
        self.graph = builder.make_graph(self.root, entries, errors, builder.sha256_bytes(payload))
        self.save()

    def save(self):
        payload = builder.inventory_jsonl(self.records)
        self.graph["integrity"]["source_inventory_sha256"] = builder.sha256_bytes(payload)
        self.graph["integrity"]["graph_content_sha256"] = None
        self.graph["integrity"]["graph_content_sha256"] = builder.sha256_bytes(builder.canonical(self.graph))
        self.inventory_path.write_bytes(payload)
        self.graph_path.write_bytes(builder.canonical(self.graph))

    def check(self, root=None):
        return validator.validate(self.graph_path, self.inventory_path, source_root=self.root if root is None else root)

    @contextlib.contextmanager
    def source_reads_forbidden(self):
        real_path_open = Path.open

        def descriptor_open(path, *args, **kwargs):
            self.fail("source descriptor opened before input/root validation")

        def path_open(path, *args, **kwargs):
            self.assertIn(path, {self.graph_path, self.inventory_path})
            return real_path_open(path, *args, **kwargs)

        with mock.patch.object(os, "open", descriptor_open), mock.patch.object(Path, "open", path_open):
            yield

    @contextlib.contextmanager
    def canary_reads_forbidden(self):
        identity = self.canary.stat().st_dev, self.canary.stat().st_ino
        real_read, real_path_open = os.read, Path.open

        def guard(fd):
            info = os.fstat(fd)
            self.assertNotEqual((info.st_dev, info.st_ino), identity, "outside source read")

        def read(fd, count):
            guard(fd)
            return real_read(fd, count)

        def path_open(path, *args, **kwargs):
            handle = real_path_open(path, *args, **kwargs)
            try:
                if handle.readable():
                    guard(handle.fileno())
            except BaseException:
                handle.close()
                raise
            return handle

        with mock.patch.object(os, "read", read), mock.patch.object(Path, "open", path_open):
            yield

    def test_valid_root_and_complete_inventory_are_accepted_without_mutation(self):
        before = [path.read_bytes() for path in (self.graph_path, self.inventory_path, self.leaf)]
        result = self.check()
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(before, [path.read_bytes() for path in (self.graph_path, self.inventory_path, self.leaf)])

    def test_missing_external_root_fails_before_source_io(self):
        with self.source_reads_forbidden():
            result = validator.validate(self.graph_path, self.inventory_path)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("source_root_required", result["errors"])

    def test_wrong_or_relative_external_root_fails_before_source_io(self):
        for root in (self.outside, Path("source")):
            with self.subTest(root=str(root)), self.source_reads_forbidden():
                self.assertEqual(self.check(root)["status"], "FAIL")

    def test_forged_graph_scope_does_not_choose_source_authority(self):
        self.graph["source_universe"]["scope"] = str(self.outside)
        self.save()
        with self.source_reads_forbidden():
            self.assertEqual(self.check()["status"], "FAIL")

    def test_unsafe_inventory_paths_fail_before_source_io(self):
        original = copy.deepcopy(self.records)
        for value in (str(self.canary), "../outside/canary.txt", "a/../memo.txt", "a//memo.txt", "./memo.txt", ".", "", "a\x00b"):
            with self.subTest(path=value):
                self.records = copy.deepcopy(original)
                self.records[0]["relative_path"] = value
                self.save()
                with self.source_reads_forbidden():
                    self.assertEqual(self.check()["status"], "FAIL")

    def test_duplicate_inventory_is_an_error_not_last_wins(self):
        self.records.append(copy.deepcopy(self.records[0]))
        self.save()
        with self.source_reads_forbidden():
            result = self.check()
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("duplicate_inventory_relative_path", result["errors"])

    def test_added_file_or_empty_directory_invalidates_old_inventory(self):
        added = self.root / "new"
        added.write_bytes(b"NEW")
        self.assertEqual(self.check()["status"], "FAIL")
        added.unlink()
        added.mkdir()
        self.assertEqual(self.check()["status"], "FAIL")

    def test_missing_or_same_size_changed_source_fails(self):
        self.leaf.write_bytes(b"CHANGE")
        self.assertEqual(self.check()["status"], "FAIL")
        self.leaf.unlink()
        self.assertEqual(self.check()["status"], "FAIL")

    def test_symlink_replacement_cannot_read_outside_canary(self):
        self.leaf.unlink()
        self.leaf.symlink_to(self.canary)
        with self.canary_reads_forbidden():
            self.assertEqual(self.check()["status"], "FAIL")

    def test_intermediate_directory_symlink_is_not_traversed(self):
        bucket = self.root / "bucket"
        bucket.mkdir()
        (bucket / "canary.txt").write_bytes(b"INSIDE")
        self.snapshot()
        bucket.rename(self.base / "detached")
        bucket.symlink_to(self.outside, target_is_directory=True)
        with self.canary_reads_forbidden():
            self.assertEqual(self.check()["status"], "FAIL")

    def test_root_alias_is_rejected_even_when_graph_scope_matches_alias(self):
        alias = self.base / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.graph["source_universe"]["scope"] = str(alias)
        self.graph["nodes"][0]["raw_value"]["absolute_path"] = str(alias)
        self.graph["nodes"][0]["source"]["path"] = str(alias)
        for node in self.graph["nodes"][1:]:
            node["source"]["path"] = str(alias / node["raw_value"]["relative_path"])
        for edge in self.graph["edges"]:
            edge["scope"]["root"] = str(alias)
        self.save()
        self.assertEqual(self.check(alias)["status"], "FAIL")

    def test_replacement_after_leaf_stat_fails_without_canary_read(self):
        original_stat = os.stat
        fired = []

        def observed(path, *args, **kwargs):
            result = original_stat(path, *args, **kwargs)
            if not isinstance(path, int) and Path(path).name == "memo.txt" and kwargs.get("follow_symlinks") is False and not fired:
                fired.append(True)
                self.leaf.unlink()
                self.leaf.symlink_to(self.canary)
            return result

        with self.canary_reads_forbidden(), mock.patch.object(os, "stat", observed):
            self.assertEqual(self.check()["status"], "FAIL")
        self.assertTrue(fired)

    def test_rehashed_forged_node_is_not_attested_by_valid_inventory(self):
        node = self.graph["nodes"][1]
        node["source"]["sha256"] = "f" * 64
        self.save()
        self.assertEqual(self.check()["status"], "FAIL")

    def test_valid_symlink_metadata_is_not_followed(self):
        link = self.root / "reference"
        link.symlink_to(self.canary)
        self.snapshot()
        with self.canary_reads_forbidden():
            result = self.check()
        self.assertEqual(result["status"], "PASS", result)

    def test_same_inode_mutation_during_read_is_not_attested(self):
        info = self.leaf.stat()
        real_read = os.read
        fired = []

        def read(fd, count):
            value = real_read(fd, count)
            if os.fstat(fd).st_ino == info.st_ino and not fired:
                fired.append(True)
                self.leaf.write_bytes(b"CHANGE")
                os.utime(self.leaf, ns=(info.st_atime_ns, info.st_mtime_ns))
            return value

        with mock.patch.object(os, "read", read):
            self.assertEqual(self.check()["status"], "FAIL")
        self.assertTrue(fired)

    def test_malformed_records_and_graphs_fail_without_source_reads(self):
        for record in (None, {}, {**self.records[0], "size_bytes": True}, {**self.records[0], "kind": []}):
            with self.subTest(record=record):
                self.inventory_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
                with self.source_reads_forbidden():
                    self.assertEqual(self.check()["status"], "FAIL")
        self.save()
        for graph in (None, {}, {**self.graph, "nodes": None}):
            self.graph_path.write_text(json.dumps(graph), encoding="utf-8")
            with self.source_reads_forbidden():
                self.assertEqual(self.check()["status"], "FAIL")

    def test_cli_requires_external_root_and_accepts_canonical_scope(self):
        arguments = ["validate_path_graph.py", str(self.graph_path), str(self.inventory_path)]
        with mock.patch.object(sys, "argv", arguments), contextlib.redirect_stderr(io.StringIO()), self.source_reads_forbidden():
            with self.assertRaises(SystemExit) as failure:
                validator.main()
        self.assertEqual(failure.exception.code, 2)
        output = io.StringIO()
        with mock.patch.object(sys, "argv", [*arguments, "--source-root", str(self.root)]), contextlib.redirect_stdout(output):
            self.assertEqual(validator.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "PASS")

    def test_reader_resource_identity_contains_actual_path_code_bytes(self):
        bootstrap = load("path_resource_bootstrap", ENGINE.parent / "app/bootstrap.py")
        bootstrap.ENGINE = ENGINE
        resources = bootstrap._current_reader_resource_contract()
        for key, name in (("path_builder", "build_path_graph.py"), ("path_validator", "validate_path_graph.py")):
            self.assertEqual(resources[key]["sha256"], hashlib.sha256((ENGINE / name).read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
