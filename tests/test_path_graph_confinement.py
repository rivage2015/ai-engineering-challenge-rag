"""Tiny descriptor-bound filesystem race fixtures; no personal roots or models."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ENGINE = Path(__file__).resolve().parents[1] / "distribution/macos-local-memory/engine"
SPEC = importlib.util.spec_from_file_location("f16_path_builder", ENGINE / "build_path_graph.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)
VALIDATOR_SPEC = importlib.util.spec_from_file_location("f16_path_validator", ENGINE / "validate_path_graph.py")
validator = importlib.util.module_from_spec(VALIDATOR_SPEC)
VALIDATOR_SPEC.loader.exec_module(validator)


@contextlib.contextmanager
def after_entry_stat(name, action):
    """Schedule a single replacement just after the real no-follow observation.

    Both DirEntry.stat (the old implementation) and os.stat(dir_fd=...) (the
    hardened implementation) are covered by the same deterministic schedule.
    """
    real_scandir, real_stat = os.scandir, os.stat
    fired = []

    def maybe_replace(entry_name):
        if entry_name == name and not fired:
            fired.append(True)
            action()

    class Entry:
        def __init__(self, entry):
            self.entry = entry
            self.name, self.path = entry.name, entry.path

        def stat(self, *args, **kwargs):
            value = self.entry.stat(*args, **kwargs)
            maybe_replace(self.name)
            return value

    class Scan:
        def __init__(self, *args, **kwargs):
            self.iterator = real_scandir(*args, **kwargs)

        def __iter__(self):
            return (Entry(entry) for entry in self.iterator)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.iterator.close()

    def observed_stat(path, *args, **kwargs):
        value = real_stat(path, *args, **kwargs)
        if not isinstance(path, int) and kwargs.get("follow_symlinks") is False:
            maybe_replace(Path(path).name)
        return value

    with mock.patch.object(os, "scandir", Scan), mock.patch.object(os, "stat", observed_stat):
        yield fired


class PathGraphConfinementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="lms-f16-")
        self.addCleanup(self.temporary.cleanup)
        # macOS /var is an alias; the explicit task scope is its canonical root.
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "source"
        self.root.mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()
        self.canary = self.outside / "canary.txt"
        self.canary.write_bytes(b"OUTSIDE-CANARY")

    def assert_incomplete(self, entries, errors):
        self.assertTrue(errors)
        graph = builder.make_graph(self.root, entries, errors, builder.sha256_bytes(builder.inventory_jsonl(builder.inventory_records(entries))))
        self.assertEqual(graph["source_universe"]["coverage_status"], "incomplete")
        self.assertEqual(graph["answer_projection"]["status"], "blocked")
        self.assertEqual(graph["duplicate_groups"], [])

    @contextlib.contextmanager
    def guard_canary_reads(self):
        identity = (self.canary.stat().st_dev, self.canary.stat().st_ino)
        real_read, real_path_open = os.read, Path.open
        reads = []

        def guard(descriptor):
            metadata = os.fstat(descriptor)
            self.assertNotEqual((metadata.st_dev, metadata.st_ino), identity, "outside canary was opened for reading")
            reads.append((metadata.st_dev, metadata.st_ino))

        def observed_read(descriptor, count):
            guard(descriptor)
            return real_read(descriptor, count)

        def observed_open(path, *args, **kwargs):
            handle = real_path_open(path, *args, **kwargs)
            try:
                if handle.readable():
                    guard(handle.fileno())
            except BaseException:
                handle.close()
                raise
            return handle

        with mock.patch.object(os, "read", observed_read), mock.patch.object(Path, "open", observed_open):
            yield reads

    def test_leaf_replaced_by_symlink_after_lstat_is_not_hashed(self):
        leaf = self.root / "leaf.txt"
        leaf.write_bytes(b"INSIDE")

        def replace():
            leaf.unlink()
            leaf.symlink_to(self.canary)

        with self.guard_canary_reads(), after_entry_stat("leaf.txt", replace) as fired:
            entries, errors = builder.enumerate_entries(self.root)
        self.assertTrue(fired)
        self.assert_incomplete(entries, errors)
        self.assertFalse(any(item["sha256"] == hashlib.sha256(b"OUTSIDE-CANARY").hexdigest() for item in entries))
        self.assertTrue(all(item["sha256"] is None for item in entries if item["kind"] == "file"))

    def test_transient_supplied_ancestor_change_invalidates_prior_hashes_immediately(self):
        # Swap an ancestor above the selected root, then restore it immediately
        # after its no-follow stat. This uses real inode values, not forged stat
        # data; the next verification sees the restored bindings.
        holder = self.base / "holder"
        holder.mkdir()
        self.root = holder / "source"
        self.root.mkdir()
        for name in ("a.txt", "b.txt", "c.txt"):
            (self.root / name).write_bytes(b"SAME")
        trigger = (self.root / "c.txt").stat().st_ino
        real_read, real_stat = os.read, os.stat
        armed, fired = [], []

        def observed_read(fd, count):
            value = real_read(fd, count)
            if os.fstat(fd).st_ino == trigger:
                armed.append(True)
            return value

        def observed_stat(path, *args, **kwargs):
            if armed and not fired and not isinstance(path, int) and Path(path).name == "holder" and kwargs.get("follow_symlinks") is False:
                fired.append(True)
                detached = self.base / "detached-holder"
                holder.rename(detached)
                try:
                    holder.mkdir()
                    try:
                        return real_stat(path, *args, **kwargs)
                    finally:
                        holder.rmdir()
                finally:
                    detached.rename(holder)
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(os, "read", observed_read), mock.patch.object(os, "stat", observed_stat):
            entries, errors = builder.enumerate_entries(self.root)
        self.assertTrue(fired)
        self.assertTrue(errors)
        self.assertTrue(all(item["sha256"] is None and item["read_status"] == "unresolved" for item in entries))
        self.assert_incomplete(entries, errors)

    def test_directory_replaced_by_symlink_after_lstat_is_not_traversed(self):
        directory = self.root / "bucket"
        directory.mkdir()
        (directory / "inside.txt").write_bytes(b"INSIDE")

        def replace():
            directory.rename(self.base / "detached")
            directory.symlink_to(self.outside, target_is_directory=True)

        with self.guard_canary_reads(), after_entry_stat("bucket", replace) as fired:
            entries, errors = builder.enumerate_entries(self.root)
        self.assertTrue(fired)
        self.assert_incomplete(entries, errors)
        self.assertNotIn("bucket/canary.txt", [item["relative_path"] for item in entries])

    def test_same_size_regular_replacement_does_not_inherit_observed_hash(self):
        leaf = self.root / "leaf.txt"
        leaf.write_bytes(b"BEFORE")
        replacement = self.base / "replacement.txt"
        replacement.write_bytes(b"AFTER!")
        with after_entry_stat("leaf.txt", lambda: os.replace(replacement, leaf)):
            entries, errors = builder.enumerate_entries(self.root)
        self.assert_incomplete(entries, errors)
        self.assertTrue(all(item["sha256"] is None for item in entries if item["kind"] == "file"))

    def test_cli_rejects_symlink_root_without_resolving_it_away(self):
        alias = self.base / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        output = self.base / "output"
        with mock.patch("sys.argv", ["build_path_graph.py", str(alias), "--output-dir", str(output)]), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises((SystemExit, OSError)):
                builder.main()
        self.assertFalse((output / "path-source-inventory.jsonl").exists())

    def test_ancestor_alias_and_parent_components_are_rejected(self):
        alias = self.base / "alias"
        alias.symlink_to(self.base, target_is_directory=True)
        for path in (alias / "source", self.root / ".." / "source"):
            with self.subTest(path=path), self.assertRaises(OSError):
                builder.enumerate_entries(path)

    def test_leaf_rebound_after_open_is_rejected_before_read(self):
        leaf = self.root / "leaf.txt"
        leaf.write_bytes(b"INSIDE")
        real_open = os.open
        fired = []

        def replace_after_open(path, flags, *args, **kwargs):
            descriptor = real_open(path, flags, *args, **kwargs)
            if path == "leaf.txt" and not fired:
                fired.append(True)
                leaf.unlink()
                leaf.symlink_to(self.canary)
            return descriptor

        with self.guard_canary_reads() as reads, mock.patch.object(os, "open", replace_after_open):
            entries, errors = builder.enumerate_entries(self.root)
        self.assertTrue(fired)
        self.assertEqual(reads, [])
        self.assert_incomplete(entries, errors)

    def test_directory_rebound_after_open_does_not_read_replacement(self):
        directory = self.root / "bucket"
        directory.mkdir()
        (directory / "inside.txt").write_bytes(b"INSIDE")
        real_open = builder._open_verified_directory
        fired = []

        def replace_after_open(parent_fd, name, expected):
            descriptor = real_open(parent_fd, name, expected)
            if name == "bucket" and not fired:
                fired.append(True)
                directory.rename(self.base / "detached")
                directory.symlink_to(self.outside, target_is_directory=True)
            return descriptor

        with self.guard_canary_reads() as reads, mock.patch.object(builder, "_open_verified_directory", replace_after_open):
            entries, errors = builder.enumerate_entries(self.root)
        self.assertTrue(fired)
        self.assertEqual(reads, [])
        self.assert_incomplete(entries, errors)
        self.assertNotIn("bucket/canary.txt", [item["relative_path"] for item in entries])

    def test_root_rebound_after_open_does_not_scan_replacement(self):
        (self.root / "inside.txt").write_bytes(b"INSIDE")
        real_open = builder._open_verified_directory
        fired = []

        def replace_after_open(parent_fd, name, expected):
            descriptor = real_open(parent_fd, name, expected)
            if name == "source" and not fired:
                fired.append(True)
                self.root.rename(self.base / "detached-root")
                self.root.symlink_to(self.outside, target_is_directory=True)
            return descriptor

        with self.guard_canary_reads() as reads, mock.patch.object(builder, "_open_verified_directory", replace_after_open):
            entries, errors = builder.enumerate_entries(self.root)
        self.assertTrue(fired)
        self.assertEqual(reads, [])
        self.assertEqual(entries, [])
        self.assert_incomplete(entries, errors)

    def test_same_inode_content_change_during_read_is_unresolved_even_if_mtime_restored(self):
        leaf = self.root / "leaf.txt"
        leaf.write_bytes(b"BEFORE")
        previous = leaf.stat()
        real_read = os.read
        fired = []

        def mutate_after_read(descriptor, count):
            data = real_read(descriptor, count)
            if not fired:
                fired.append(True)
                leaf.write_bytes(b"AFTER!")
                os.utime(leaf, ns=(previous.st_atime_ns, previous.st_mtime_ns))
            return data

        with mock.patch.object(os, "read", mutate_after_read):
            entries, errors = builder.enumerate_entries(self.root)
        self.assertTrue(fired)
        self.assert_incomplete(entries, errors)
        self.assertIsNone(entries[0]["sha256"])
        self.assertEqual(entries[0]["read_status"], "unresolved")

    def test_growing_file_is_not_followed_past_observed_size(self):
        leaf = self.root / "leaf.txt"
        leaf.write_bytes(b"small")
        real_read = os.read
        requests = []

        def append_after_read(descriptor, count):
            requests.append(count)
            data = real_read(descriptor, count)
            with leaf.open("ab") as handle:
                handle.write(b"growth")
            return data

        with mock.patch.object(os, "read", append_after_read):
            entries, errors = builder.enumerate_entries(self.root)
        self.assertEqual(requests, [5])
        self.assert_incomplete(entries, errors)
        self.assertIsNone(entries[0]["sha256"])

    def test_new_directory_member_invalidates_prior_hashes(self):
        (self.root / "a.txt").write_bytes(b"one")
        (self.root / "b.txt").write_bytes(b"two")
        real_hash = builder.sha256_file
        fired = []

        def add_after_hash(*args, **kwargs):
            digest = real_hash(*args, **kwargs)
            if args[0].name == "b.txt" and not fired:
                fired.append(True)
                (self.root / "new.txt").write_bytes(b"new")
            return digest

        with mock.patch.object(builder, "sha256_file", add_after_hash):
            entries, errors = builder.enumerate_entries(self.root)
        self.assertTrue(fired)
        self.assert_incomplete(entries, errors)
        self.assertTrue(all(item["sha256"] is None and item["read_status"] == "unresolved" for item in entries))

    def test_fifo_replacement_does_not_block_or_read_special_file(self):
        leaf = self.root / "leaf.txt"
        leaf.write_bytes(b"INSIDE")

        def replace():
            leaf.unlink()
            os.mkfifo(leaf)

        with self.guard_canary_reads() as reads, after_entry_stat("leaf.txt", replace):
            entries, errors = builder.enumerate_entries(self.root)
        self.assertEqual(reads, [])
        self.assert_incomplete(entries, errors)

    def test_unavailable_no_follow_capability_fails_closed(self):
        with mock.patch.object(builder, "_HAS_FD_OPERATIONS", False), mock.patch.object(os, "open", side_effect=AssertionError("must not attempt fallback")):
            with self.assertRaisesRegex(OSError, "unavailable"):
                builder.enumerate_entries(self.root)

    def test_descriptors_closed_on_success_read_error_and_unexpected_exception(self):
        directory = self.root / "bucket"
        directory.mkdir()
        (directory / "file.txt").write_bytes(b"inside")
        for failure in (None, PermissionError("synthetic unreadable"), RuntimeError("synthetic interruption")):
            with self.subTest(failure=type(failure).__name__):
                real_open, real_read = os.open, os.read
                opened = []

                def observe_open(*args, **kwargs):
                    descriptor = real_open(*args, **kwargs)
                    opened.append(descriptor)
                    return descriptor

                def maybe_fail(*args):
                    if failure is not None:
                        raise failure
                    return real_read(*args)

                with mock.patch.object(os, "open", observe_open), mock.patch.object(os, "read", maybe_fail):
                    if isinstance(failure, RuntimeError):
                        with self.assertRaises(RuntimeError):
                            builder.enumerate_entries(self.root)
                    else:
                        entries, errors = builder.enumerate_entries(self.root)
                        self.assertEqual(bool(errors), failure is not None)
                self.assertTrue(opened)
                for descriptor in set(opened):
                    with self.assertRaises(OSError):
                        os.fstat(descriptor)

    def test_standalone_hash_has_same_no_follow_contract(self):
        (self.root / "leaf.txt").write_bytes(b"inside")
        self.assertEqual(builder.sha256_file(self.root / "leaf.txt"), hashlib.sha256(b"inside").hexdigest())
        (self.root / "link.txt").symlink_to(self.canary)
        with self.guard_canary_reads(), self.assertRaises(OSError):
            builder.sha256_file(self.root / "link.txt")

    def test_cli_stable_tree_publishes_compatible_artifacts(self):
        original = self.root / "source.txt"
        original.write_bytes(b"synthetic original")
        output = self.base / "output"
        stdout = io.StringIO()
        with mock.patch("sys.argv", ["build_path_graph.py", str(self.root), "--output-dir", str(output)]), contextlib.redirect_stdout(stdout):
            self.assertEqual(builder.main(), 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["coverage"], "complete")
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["file_count"], 1)
        self.assertEqual(validator.validate(output / "path-evidence-graph.json", output / "path-source-inventory.jsonl", source_root=self.root)["status"], "PASS")
        self.assertEqual(original.read_bytes(), b"synthetic original")

    def test_scandir_failure_stays_explicit_with_no_directory_source_hash(self):
        (self.root / "bucket").mkdir()
        real_scan = builder._directory_names
        directory_inode = (self.root / "bucket").stat().st_ino

        def deny_child_scan(descriptor):
            if os.fstat(descriptor).st_ino == directory_inode:
                raise PermissionError("synthetic unreadable directory")
            return real_scan(descriptor)

        with mock.patch.object(builder, "_directory_names", deny_child_scan):
            entries, errors = builder.enumerate_entries(self.root)
        self.assert_incomplete(entries, errors)
        graph = builder.make_graph(self.root, entries, errors, builder.sha256_bytes(builder.inventory_jsonl(builder.inventory_records(entries))))
        directory = next(node for node in graph["nodes"] if node["node_type"] == "filesystem_directory")
        self.assertEqual(directory["status"], "unresolved")
        self.assertIsNone(directory["source"]["sha256"])

    def test_stable_tree_schema_inventory_order_symlink_and_duplicate_contract(self):
        directory = self.root / "folder"
        directory.mkdir()
        (directory / "b.txt").write_bytes(b"same")
        (self.root / "a.txt").write_bytes(b"same")
        (self.root / "alias.txt").symlink_to(self.canary)
        entries, errors = builder.enumerate_entries(self.root)
        self.assertEqual(errors, [])
        self.assertEqual([item["relative_path"] for item in entries], ["a.txt", "alias.txt", "folder", "folder/b.txt"])
        link = next(item for item in entries if item["kind"] == "symlink")
        self.assertEqual(link["symlink_target"], str(self.canary))
        inventory = builder.inventory_jsonl(builder.inventory_records(entries))
        graph = builder.make_graph(self.root, entries, errors, builder.sha256_bytes(inventory))
        self.assertEqual(graph["schema_version"], "1.1")
        self.assertEqual(graph["source_universe"]["coverage_status"], "complete")
        self.assertEqual(graph["duplicate_groups"][0]["paths"], ["a.txt", "folder/b.txt"])
        inventory_file, graph_file = self.base / "inventory.jsonl", self.base / "graph.json"
        inventory_file.write_bytes(inventory)
        graph_file.write_text(json.dumps(graph), encoding="utf-8")
        # Stable synthetic files only. This validator's independent pathname
        # race is outside F16 builder ownership and is not security-certified.
        self.assertEqual(validator.validate(graph_file, inventory_file, source_root=self.root)["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
