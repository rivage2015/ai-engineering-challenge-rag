"""Synthetic F18 regressions: validation is not generation mutation."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "distribution/macos-local-memory/engine"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ENGINE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


validator = load("immutable_lineage_validator", "validate_adaptive_semantic_graph.py")
builder = load("immutable_lineage_builder", "build_adaptive_semantic_graph.py")


class ImmutableLineageValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="immutable-lineage-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.output = self.base / "semantic"
        self.output.mkdir()
        self.source = self.base / "source"
        self.source.mkdir()
        self.inventory = self.base / "inventory.jsonl"
        self.inventory.write_text("", encoding="utf-8")

    def snapshot(self, root=None):
        root = root or self.output
        return {
            str(path.relative_to(root)): (
                os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None,
                path.lstat().st_ino, path.lstat().st_mtime_ns, path.lstat().st_mode,
            )
            for path in (root, *root.rglob("*"))
        }

    def build_reader(self, *, initialize=True):
        path = self.source / "sample.csv"
        path.write_text("Item,Value\nalpha,13\nbeta,21\n", encoding="utf-8")
        self.inventory.write_text(json.dumps({
            "kind": "file", "relative_path": path.name, "read_status": "observed",
            "size_bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }) + "\n", encoding="utf-8")
        builder.build(self.source, self.inventory, self.output, ROOT / "scripts")
        if initialize:
            report = validator.validate(self.output, self.source, self.inventory, initialize_lineage=True)
            self.assertEqual(report["status"], "PASS")
        return path

    def test_early_validation_failure_preserves_existing_lineage(self):
        (self.output / "adaptive-reader-state.json").write_text("{}", encoding="utf-8")
        for name in (validator.LINEAGE_RELATIONS_FILE, validator.LINEAGE_VALIDATION_FILE):
            (self.output / name).write_text("existing generation sentinel\n", encoding="utf-8")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "builder_invalid"):
            validator.validate(self.output, self.source, self.inventory)
        self.assertEqual(before, self.snapshot())

    def test_initial_publisher_never_overwrites_existing_artifacts(self):
        names = (validator.LINEAGE_RELATIONS_FILE, validator.LINEAGE_VALIDATION_FILE)
        for existing in ((names[0],), (names[1],), names):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory(dir=self.base) as temporary:
                output = Path(temporary)
                for name in existing:
                    (output / name).write_text("prior payload\n", encoding="utf-8")
                before = {p.name: p.read_bytes() for p in output.iterdir()}
                with self.assertRaises((FileExistsError, ValueError)):
                    validator._publish_lineage_artifacts(output, [], {"status": "pass"})
                self.assertEqual(before, {p.name: p.read_bytes() for p in output.iterdir()})

    def test_first_creation_and_repeat_read_only_validation(self):
        self.build_reader()
        before = self.snapshot()
        for _ in range(2):
            self.assertEqual(validator.validate(self.output, self.source, self.inventory)["status"], "PASS")
            self.assertEqual(before, self.snapshot())
        with self.assertRaises(FileExistsError):
            validator.validate(self.output, self.source, self.inventory, initialize_lineage=True)
        self.assertEqual(before, self.snapshot())

    def test_default_never_initializes_missing_artifacts(self):
        self.build_reader(initialize=False)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "lineage_artifact_missing_or_unreadable"):
            validator.validate(self.output, self.source, self.inventory)
        self.assertEqual(before, self.snapshot())

    def test_read_only_directory_remains_unchanged(self):
        self.build_reader()
        self.output.chmod(0o555)
        self.addCleanup(self.output.chmod, 0o755)
        before = self.snapshot()
        with mock.patch.object(validator.os, "link", side_effect=AssertionError("validation wrote")), mock.patch.object(validator.os, "unlink", side_effect=AssertionError("validation deleted")):
            self.assertEqual(validator.validate(self.output, self.source, self.inventory)["status"], "PASS")
        self.assertEqual(before, self.snapshot())

    def test_source_and_reader_failures_preserve_lineage(self):
        source = self.build_reader()
        before = self.snapshot()
        original_source = source.read_bytes()
        source.write_bytes(original_source.replace(b"13", b"99"))
        with self.assertRaisesRegex(ValueError, "source_hash_mismatch"):
            validator.validate(self.output, self.source, self.inventory)
        self.assertEqual(before, self.snapshot())
        source.write_bytes(original_source)
        evidence = self.output / "semantic-evidence.jsonl"
        evidence.write_bytes(evidence.read_bytes() + b" ")
        tampered = self.snapshot()
        with self.assertRaisesRegex(ValueError, "semantic_output_hash_mismatch"):
            validator.validate(self.output, self.source, self.inventory)
        self.assertEqual(tampered, self.snapshot())

    def test_changed_missing_and_symlink_lineage_are_not_repaired(self):
        self.build_reader()
        canary = self.base / "unread-canary"
        canary.write_bytes(b"outside lineage canary")
        for name in (validator.LINEAGE_RELATIONS_FILE, validator.LINEAGE_VALIDATION_FILE):
            path = self.output / name
            original = path.read_bytes()
            for state in ("tampered", "missing", "symlink"):
                with self.subTest(name=name, state=state):
                    path.unlink()
                    if state == "tampered":
                        path.write_bytes(original + b" ")
                    elif state == "symlink":
                        path.symlink_to(self.base / "unread-canary")
                    before = self.snapshot()
                    with self.assertRaisesRegex(ValueError, "lineage_artifact_"):
                        validator.validate(self.output, self.source, self.inventory)
                    self.assertEqual(before, self.snapshot())
                    if state != "missing":
                        path.unlink()
                    path.write_bytes(original)
        self.assertEqual(canary.read_bytes(), b"outside lineage canary")

    def test_unexpected_version_binding_fails_without_removing_lineage(self):
        self.build_reader()
        state_path = self.output / "adaptive-reader-state.json"
        state = json.loads(state_path.read_text())
        state["document_version_graph"] = {"path": "/never-open-this-version.json"}
        state_path.write_text(json.dumps(state), encoding="utf-8")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "unexpected_document_version_graph"):
            validator.validate(self.output, self.source, self.inventory)
        self.assertEqual(before, self.snapshot())

    def test_projector_rejects_failed_revalidation_even_with_saved_pass(self):
        source = self.build_reader()
        index_builder = load("immutable_lineage_index", "build_local_semantic_index.py")
        documents = validator.read_jsonl(self.output / "semantic-documents.jsonl")
        evidence = validator.read_jsonl(self.output / "semantic-evidence.jsonl")
        relations = validator.read_jsonl(self.output / validator.LINEAGE_RELATIONS_FILE)
        source.write_bytes(source.read_bytes().replace(b"13", b"99"))
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "source_hash_mismatch"):
            index_builder._attest_lineage_context(documents, evidence, relations, {
                "output_dir": self.output, "source_root": self.source, "inventory": self.inventory,
            })
        self.assertEqual(before, self.snapshot())
        self.assertEqual(json.loads((self.output / validator.LINEAGE_VALIDATION_FILE).read_text())["status"], "pass")

    def test_published_generation_success_and_failure_leave_config_and_index_unchanged(self):
        # The production build runs inside the existing model-free E2E harness.
        path = ROOT / "distribution/macos-local-memory/tests/test_versioned_safe_index_e2e.py"
        spec = importlib.util.spec_from_file_location("immutable_lineage_e2e", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        harness = module.VersionedSafeIndexE2E()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        harness.seed()
        harness.bootstrap.build_index()
        config_bytes = harness.bootstrap.CONFIG.read_bytes()
        config = json.loads(config_bytes)
        semantic = Path(config["semantic_path"])
        generation = semantic.parent
        graph = Path(config["path_graph_path"]) / "document-version-graph.json"
        inventory = graph.parent / "path-source-inventory.jsonl"
        before = self.snapshot(generation)
        self.assertEqual(validator.validate(semantic, harness.source, inventory, graph)["status"], "PASS")
        self.assertEqual(before, self.snapshot(generation))
        (harness.source / "連絡先.txt").write_text("contact: changed desk\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            validator.validate(semantic, harness.source, inventory, graph)
        self.assertEqual(before, self.snapshot(generation))
        self.assertEqual(config_bytes, harness.bootstrap.CONFIG.read_bytes())

    def test_initial_publisher_rejects_dangling_symlink_and_directory(self):
        for kind in ("symlink", "directory"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(dir=self.base) as temporary:
                output = Path(temporary)
                target = output / validator.LINEAGE_VALIDATION_FILE
                if kind == "symlink":
                    target.symlink_to(self.base / "never-created")
                else:
                    target.mkdir()
                before = target.lstat()
                with self.assertRaises(FileExistsError):
                    validator._publish_lineage_artifacts(output, [], {"status": "pass"})
                self.assertEqual(target.lstat(), before)
                self.assertEqual({path.name for path in output.iterdir()}, {target.name})
                self.assertFalse((self.base / "never-created").exists())

    def test_initialization_write_and_second_link_failure_leave_no_own_files(self):
        actual_link = validator.os.link
        for fault in ("write", "second_link", "after_link"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=self.base) as temporary:
                output = Path(temporary)
                calls = []

                def failing_link(src, dst, **kwargs):
                    calls.append(dst)
                    if len(calls) == 2:
                        if fault == "after_link":
                            actual_link(src, dst, **kwargs)
                        raise OSError("synthetic link failure")
                    return actual_link(src, dst, **kwargs)

                if fault == "write":
                    patch = mock.patch.object(validator.os, "fsync", side_effect=OSError("synthetic fsync failure"))
                else:
                    patch = mock.patch.object(validator.os, "link", side_effect=failing_link)
                with patch, self.assertRaises(OSError):
                    validator._publish_lineage_artifacts(output, [], {"status": "pass"})
                self.assertEqual(list(output.iterdir()), [])

    def test_initialization_cleanup_preserves_concurrently_created_foreign_files(self):
        actual_link = validator.os.link
        for replace_first in (False, True):
            with self.subTest(replace_first=replace_first), tempfile.TemporaryDirectory(dir=self.base) as temporary:
                output = Path(temporary)
                foreign = output / (validator.LINEAGE_RELATIONS_FILE if replace_first else validator.LINEAGE_VALIDATION_FILE)
                calls = []

                def intervening_link(src, dst, **kwargs):
                    calls.append(dst)
                    if len(calls) == 2:
                        if replace_first:
                            foreign.unlink()
                        foreign.write_bytes(b"foreign sentinel")
                        raise OSError("synthetic concurrent failure")
                    return actual_link(src, dst, **kwargs)

                with mock.patch.object(validator.os, "link", side_effect=intervening_link), self.assertRaises(OSError):
                    validator._publish_lineage_artifacts(output, [], {"status": "pass"})
                self.assertEqual({p.name: p.read_bytes() for p in output.iterdir()}, {foreign.name: b"foreign sentinel"})


if __name__ == "__main__":
    unittest.main()
