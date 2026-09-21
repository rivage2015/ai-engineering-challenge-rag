"""Synthetic safety tests for the F2-I metadata-only document inventory."""
from __future__ import annotations

import builtins
from contextlib import contextmanager
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "inventory_local_documents", ROOT / "scripts" / "inventory_local_documents.py"
)
inventory = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = inventory
SPEC.loader.exec_module(inventory)


def metadata_with(metadata, **changes):
    values = {
        name: getattr(metadata, name)
        for name in dir(metadata)
        if name.startswith("st_")
    }
    values.update(changes)
    return SimpleNamespace(**values)


class LocalDocumentInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="inventory-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()
        self.output = self.base / "inventory"

    def make_file(self, relative, content=b"synthetic document content"):
        target = self.source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def records(self, name):
        with (self.output / name).open(encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def scan(self, **options):
        return inventory.scan(self.source, self.output, user_name="owner", **options)

    @contextmanager
    def guard_source_access(self, *, forbidden_directories=(), inaccessible=()):
        """Allow source metadata and directory fds, but never file content reads."""
        original_open = builtins.open
        original_io_open = io.open
        original_os_open = os.open
        original_scandir = os.scandir
        directory_fds = {}
        forbidden = {Path(path) for path in forbidden_directories}
        denied = {Path(path) for path in inaccessible}

        def absolute_path(path, dir_fd=None):
            if isinstance(path, int):
                return directory_fds.get(path)
            target = Path(os.fsdecode(path))
            if not target.is_absolute() and dir_fd in directory_fds:
                target = directory_fds[dir_fd] / target
            return Path(os.path.abspath(target))

        def in_source(target):
            return target is not None and target.is_relative_to(self.source)

        def check_directory(target):
            if target is None:
                return
            if any(target == item or target.is_relative_to(item) for item in forbidden):
                raise AssertionError(f"Excluded directory was traversed: {target}")
            if any(target == item or target.is_relative_to(item) for item in denied):
                raise PermissionError(f"Synthetic permission denial: {target}")

        def file_open(opener, path, *args, **kwargs):
            target = absolute_path(path)
            if in_source(target):
                raise AssertionError(f"Source content was opened: {target}")
            return opener(path, *args, **kwargs)

        def fd_open(path, flags, *args, **kwargs):
            target = absolute_path(path, kwargs.get("dir_fd"))
            if in_source(target):
                self.assertTrue(flags & os.O_DIRECTORY, f"Non-directory source open: {target}")
                check_directory(target)
            result = original_os_open(path, flags, *args, **kwargs)
            if flags & os.O_DIRECTORY:
                directory_fds[result] = target
            return result

        def scandir(path="."):
            check_directory(absolute_path(path))
            return original_scandir(path)

        with (
            patch("builtins.open", side_effect=lambda path, *a, **kw: file_open(original_open, path, *a, **kw)),
            patch("io.open", side_effect=lambda path, *a, **kw: file_open(original_io_open, path, *a, **kw)),
            patch("os.open", side_effect=fd_open),
            patch("os.scandir", side_effect=scandir),
        ):
            yield

    def test_document_and_image_candidates_are_metadata_only(self):
        paths = [
            self.make_file("documents/report.docx"),
            self.make_file("documents/note.txt"),
            self.make_file("images/photo.png"),
            self.make_file("legacy/report.doc"),
            self.make_file("legacy/table.xls"),
            self.make_file("images/photo.heic"),
        ]
        self.make_file("movie.mp4")
        expected_metadata = {str(path): path.lstat() for path in paths}

        with self.guard_source_access():
            summary = self.scan()

        records = self.records("documents.jsonl")
        self.assertEqual({row["path"] for row in records}, set(expected_metadata))
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["candidate_files"], len(paths))
        self.assertEqual(summary["bytes_total"], sum(item.st_size for item in expected_metadata.values()))
        self.assertIs(summary["content_read"], False)
        for row in records:
            target = Path(row["path"])
            self.assertEqual(row["name"], target.name)
            self.assertEqual(row["parent_path"], str(target.parent))
            self.assertEqual(row["size_bytes"], expected_metadata[str(target)].st_size)
            self.assertEqual(row["mtime_ns"], expected_metadata[str(target)].st_mtime_ns)
            self.assertEqual(row["extension"].lstrip("."), target.suffix.lstrip("."))
            self.assertEqual(row["content_status"], "unread")
        directory_paths = {row["path"] for row in self.records("directories.jsonl")}
        self.assertTrue({str(path.parent) for path in paths}.issubset(directory_paths))
        self.assertGreaterEqual(summary["directories_scanned"], 4)
        self.assertGreaterEqual(summary["entries_seen"], len(paths) + 1)
        self.assertGreaterEqual(summary["elapsed_seconds"], 0)
        saved = json.loads((self.output / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, summary)

    def test_excluded_directories_are_not_traversed_and_secrets_not_candidates(self):
        safe = self.make_file("public/guide.txt")
        excluded_names = [".git", "Library", "Applications", "System", "Caches", "Tool.app", ".Trash"]
        excluded_directories = []
        for name in excluded_names:
            self.make_file(f"{name}/private.txt")
            excluded_directories.append(self.source / name)
        self.make_file(".env")
        self.make_file("secret.txt")

        with self.guard_source_access(forbidden_directories=excluded_directories):
            summary = self.scan()

        self.assertEqual([row["path"] for row in self.records("documents.jsonl")], [str(safe)])
        omissions = self.records("omissions.jsonl")
        omitted = {row["path"] for row in omissions}
        self.assertTrue({str(path) for path in excluded_directories}.issubset(omitted))
        self.assertIn(str(self.source / ".env"), omitted)
        self.assertIn(str(self.source / "secret.txt"), omitted)
        self.assertTrue(all(row.get("reason") for row in omissions))
        self.assertTrue(summary["excluded_counts"])

    def test_symlinks_to_external_files_and_directories_are_not_followed(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "outside.txt").write_text("not a candidate", encoding="utf-8")
        (self.source / "linked").symlink_to(outside, target_is_directory=True)
        (self.source / "linked.txt").symlink_to(outside / "outside.txt")
        safe = self.make_file("safe.txt")

        with self.guard_source_access(forbidden_directories=[self.source / "linked", outside]):
            summary = self.scan()

        self.assertEqual(summary["status"], "complete")
        self.assertEqual([row["path"] for row in self.records("documents.jsonl")], [str(safe)])
        self.assertTrue(
            {str(self.source / "linked"), str(self.source / "linked.txt")}.issubset(
                {row["path"] for row in self.records("omissions.jsonl")}
            )
        )

    def test_other_users_homes_are_excluded(self):
        safe = self.make_file("Users/owner/Documents/mine.txt")
        self.make_file("Users/another_user/Documents/theirs.txt")
        other_home = self.source / "Users/another_user"

        with self.guard_source_access(forbidden_directories=[other_home]):
            self.scan()

        self.assertEqual([row["path"] for row in self.records("documents.jsonl")], [str(safe)])
        self.assertIn(str(other_home), {row["path"] for row in self.records("omissions.jsonl")})

    def test_dataless_metadata_is_excluded_before_any_content_access(self):
        target = self.make_file("cloud.txt")
        metadata = metadata_with(target.lstat(), st_flags=inventory.SF_DATALESS)
        self.assertEqual(inventory.SF_DATALESS, 0x40000000)
        self.assertTrue(inventory.exclusion_reason(Path("cloud.txt"), metadata, "owner"))
        regular_metadata = metadata_with(metadata, st_flags=0)
        self.assertIsNone(inventory.exclusion_reason(Path("cloud.txt"), regular_metadata, "owner"))

    def test_dataless_file_and_directory_are_not_read_or_traversed(self):
        placeholder = self.make_file("cloud.txt")
        self.make_file("cloud_folder/document.txt")
        cloud_folder = self.source / "cloud_folder"
        original_stat = os.stat

        def cloud_metadata(path, *args, **kwargs):
            result = original_stat(path, *args, **kwargs)
            if not isinstance(path, int) and Path(os.fsdecode(path)).name in {"cloud.txt", "cloud_folder"}:
                return metadata_with(result, st_flags=inventory.SF_DATALESS)
            return result

        with (
            self.guard_source_access(forbidden_directories=[cloud_folder]),
            patch("os.stat", side_effect=cloud_metadata),
        ):
            summary = self.scan()

        self.assertEqual(summary["status"], "complete")
        self.assertEqual(self.records("documents.jsonl"), [])
        self.assertTrue(
            {str(placeholder), str(cloud_folder)}.issubset(
                {row["path"] for row in self.records("omissions.jsonl")}
            )
        )

    def test_different_device_directory_is_not_traversed(self):
        self.make_file("other_volume/private.txt")
        other_volume = self.source / "other_volume"
        original_stat = os.stat
        original_lstat = os.lstat

        def changed_metadata(stat_function, path, *args, **kwargs):
            result = stat_function(path, *args, **kwargs)
            if not isinstance(path, int) and Path(os.fsdecode(path)).name == "other_volume":
                return metadata_with(result, st_dev=self.source.lstat().st_dev + 1)
            return result

        with (
            self.guard_source_access(forbidden_directories=[other_volume]),
            patch("os.stat", side_effect=lambda path, *a, **kw: changed_metadata(original_stat, path, *a, **kw)),
            patch("os.lstat", side_effect=lambda path, *a, **kw: changed_metadata(original_lstat, path, *a, **kw)),
        ):
            self.scan()

        self.assertEqual(self.records("documents.jsonl"), [])
        self.assertIn(str(other_volume), {row["path"] for row in self.records("omissions.jsonl")})

    def test_entry_limit_is_reported_as_partial(self):
        for number in range(20):
            self.make_file(f"document-{number}.txt")
        with self.guard_source_access():
            summary = self.scan(max_entries=1)
        self.assertEqual(summary["status"], "partial")
        self.assertTrue(summary["stopped_reason"])
        self.assertLessEqual(summary["entries_seen"], 1)
        self.assertLess(summary["candidate_files"], 20)
        self.assertIs(summary["content_read"], False)

    def test_time_limit_is_reported_as_partial(self):
        self.make_file("document.txt")
        with self.guard_source_access():
            summary = self.scan(max_seconds=0.000000001)
        self.assertEqual(summary["status"], "partial")
        self.assertTrue(summary["stopped_reason"])
        self.assertIs(summary["content_read"], False)

    def test_progress_reports_running_until_scan_finishes(self):
        self.make_file("document.txt")
        updates = []
        clock_ticks = iter(range(0, 1000, 11))
        with (
            self.guard_source_access(),
            patch.object(inventory.time, "monotonic", side_effect=lambda: next(clock_ticks)),
        ):
            summary = self.scan(progress=updates.append)
        self.assertTrue(updates, "Synthetic elapsed time must trigger a progress update")
        self.assertTrue(all(update["status"] == "running" for update in updates))
        self.assertTrue(all(update["content_read"] is False for update in updates))
        self.assertEqual(summary["status"], "complete")

    def test_entry_limit_records_each_unfinished_directory_as_omitted(self):
        self.make_file("pending/document.txt")
        with self.guard_source_access():
            summary = self.scan(max_entries=1)
        self.assertEqual(summary["status"], "partial")
        self.assertTrue(summary["stopped_reason"])
        omissions = self.records("omissions.jsonl")
        self.assertEqual(
            {row["path"] for row in omissions},
            {str(self.source), str(self.source / "pending")},
        )
        self.assertTrue(all(summary["stopped_reason"] in row["reason"] for row in omissions))
        self.assertEqual(self.records("documents.jsonl"), [])

    def test_permission_error_is_reported_as_partial_with_omission(self):
        self.make_file("restricted/private.txt")
        restricted = self.source / "restricted"
        with self.guard_source_access(inaccessible=[restricted]):
            summary = self.scan()
        self.assertEqual(summary["status"], "partial")
        self.assertTrue(summary["errors"])
        self.assertIn(str(restricted), {row["path"] for row in self.records("omissions.jsonl")})
        self.assertEqual(self.records("documents.jsonl"), [])

    def test_existing_output_files_cannot_be_overwritten(self):
        self.make_file("document.txt")
        self.output.mkdir()
        existing = self.output / "documents.jsonl"
        existing.write_text("preserve this inventory\n", encoding="utf-8")
        with self.assertRaises((FileExistsError, ValueError)):
            self.scan()
        self.assertEqual(existing.read_text(encoding="utf-8"), "preserve this inventory\n")
        self.assertEqual(set(self.output.iterdir()), {existing})

    def test_inventory_outputs_are_private(self):
        self.make_file("document.txt")
        self.scan()
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode) & 0o077, 0)
        for name in ("documents.jsonl", "directories.jsonl", "omissions.jsonl", "summary.json"):
            with self.subTest(name=name):
                self.assertEqual(stat.S_IMODE((self.output / name).stat().st_mode) & 0o077, 0)

    def test_missing_output_parents_are_created_privately_without_changing_existing_parent(self):
        self.make_file("document.txt")
        parent = self.base / "existing"
        parent.mkdir(mode=0o755)
        previous_mode = stat.S_IMODE(parent.stat().st_mode)
        self.output = parent / "new-parent" / "nested" / "inventory"
        with self.guard_source_access():
            summary = self.scan()
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(stat.S_IMODE(parent.stat().st_mode), previous_mode)
        for path in (parent / "new-parent", self.output.parent, self.output):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def test_output_ancestor_symlink_is_not_followed(self):
        self.make_file("document.txt")
        outside = self.base / "outside"
        outside.mkdir()
        alias = self.base / "output-alias"
        alias.symlink_to(outside, target_is_directory=True)
        self.output = alias / "inventory"
        with self.guard_source_access():
            with self.assertRaises(OSError):
                self.scan()
        self.assertEqual(list(outside.iterdir()), [])

    def test_output_parent_swap_at_mkdir_does_not_create_outside_directory(self):
        self.make_file("document.txt")
        parent = self.base / "output-parent"
        parent.mkdir()
        outside = self.base / "outside"
        outside.mkdir()
        self.output = parent / "inventory"
        original_mkdir = os.mkdir

        def swap_before_mkdir(name, *args, **kwargs):
            if name == "inventory":
                self.assertIn("dir_fd", kwargs)
                parent.rename(self.base / "preserved-parent")
                parent.symlink_to(outside, target_is_directory=True)
            return original_mkdir(name, *args, **kwargs)

        with self.guard_source_access(), patch.object(inventory.os, "mkdir", side_effect=swap_before_mkdir):
            with self.assertRaises(OSError):
                self.scan()
        self.assertEqual(list(outside.iterdir()), [])

    def test_output_file_symlink_inserted_before_open_cannot_overwrite_target(self):
        self.make_file("document.txt")
        outside = self.base / "outside.jsonl"
        outside.write_text("preserve outside", encoding="utf-8")
        with self.guard_source_access():
            original_open = os.open

            def insert_link(name, flags, *args, **kwargs):
                if name == "documents.jsonl":
                    self.assertTrue(flags & os.O_NOFOLLOW)
                    self.assertTrue(flags & os.O_EXCL)
                    self.assertIn("dir_fd", kwargs)
                    (self.output / name).symlink_to(outside)
                return original_open(name, flags, *args, **kwargs)

            with patch.object(inventory.os, "open", side_effect=insert_link):
                with self.assertRaises(OSError):
                    self.scan()
        self.assertEqual(outside.read_text(encoding="utf-8"), "preserve outside")

    def test_summary_write_is_anchored_if_output_directory_swapped_at_open(self):
        self.make_file("document.txt")
        outside = self.base / "outside"
        outside.mkdir()
        outside_summary = outside / "summary.json"
        outside_summary.write_text("preserve summary", encoding="utf-8")
        with self.guard_source_access():
            original_open = os.open

            def swap_before_summary(name, flags, *args, **kwargs):
                if name == "summary.json":
                    self.assertIn("dir_fd", kwargs)
                    self.output.rename(self.base / "preserved-inventory")
                    self.output.symlink_to(outside, target_is_directory=True)
                return original_open(name, flags, *args, **kwargs)

            with patch.object(inventory.os, "open", side_effect=swap_before_summary):
                with self.assertRaises(OSError):
                    self.scan()
        self.assertEqual(outside_summary.read_text(encoding="utf-8"), "preserve summary")
        self.assertEqual(list(outside.iterdir()), [outside_summary])

    def test_output_file_path_swap_during_scan_cannot_return_complete(self):
        self.make_file("document.txt")
        outside = self.base / "outside.jsonl"
        outside.write_text("preserve outside", encoding="utf-8")
        clock_ticks = iter(range(0, 1000, 11))
        swapped = []

        def replace_output(_summary):
            if not swapped:
                documents = self.output / "documents.jsonl"
                documents.rename(self.output / "preserved-documents.jsonl")
                documents.symlink_to(outside)
                swapped.append(True)

        with self.guard_source_access(), \
                patch.object(inventory.time, "monotonic", side_effect=lambda: next(clock_ticks)):
            with self.assertRaises(OSError):
                self.scan(progress=replace_output)
        self.assertTrue(swapped)
        self.assertEqual(outside.read_text(encoding="utf-8"), "preserve outside")


if __name__ == "__main__":
    unittest.main()
