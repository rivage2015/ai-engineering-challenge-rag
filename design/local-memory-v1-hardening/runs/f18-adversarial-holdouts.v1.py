"""Independent F18 round-0 holdouts; synthetic fixtures and reviewed guard only.

Preflight: stdlib launcher; application imports happen only inside the frozen
F04a read/write/network/process guard. Reuses the F18 v2 dispatcher and bounded
supervisor. Fixtures use one small CSV and at most 64 KiB artificial payload;
no personal data, models, package build, native applications or new permissions.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if __name__ != "__main__":
    fixture_module = load(ROOT / "tests/test_immutable_lineage_validation.py", "f18_holdout_fixture")
    validator = fixture_module.validator
    builder = fixture_module.builder


class F18IndependentHoldouts(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.ImmutableLineageValidationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.output = self.fixture.output

    def snapshot(self):
        return self.fixture.snapshot()

    def initialize_small(self):
        validator._publish_lineage_artifacts(self.output, [], {"status": "pass"})

    def test_malformed_and_partial_payloads_preserve_inspected_generation(self):
        self.fixture.build_reader()
        for name in (validator.LINEAGE_RELATIONS_FILE, validator.LINEAGE_VALIDATION_FILE):
            path = self.output / name
            original = path.read_bytes()
            for payload in (b"", b"{", b"null\n", original[:-1], b"\xef\xbb\xbf" + original):
                with self.subTest(name=name, payload_length=len(payload)):
                    path.write_bytes(payload)
                    before = self.snapshot()
                    with self.assertRaisesRegex(ValueError, "lineage_artifact_payload_mismatch"):
                        validator.validate(self.output, self.fixture.source, self.fixture.inventory)
                    self.assertEqual(before, self.snapshot())
            path.write_bytes(original)

    def test_fifo_and_directory_are_rejected_without_blocking_or_mutation(self):
        self.fixture.build_reader()
        for name in (validator.LINEAGE_RELATIONS_FILE, validator.LINEAGE_VALIDATION_FILE):
            path = self.output / name
            original = path.read_bytes()
            for kind in ("fifo", "directory"):
                with self.subTest(name=name, kind=kind):
                    path.unlink()
                    os.mkfifo(path) if kind == "fifo" else path.mkdir()
                    before = self.snapshot()
                    with self.assertRaises((ValueError, OSError)):
                        validator.validate(self.output, self.fixture.source, self.fixture.inventory)
                    self.assertEqual(before, self.snapshot())
                    path.unlink() if kind == "fifo" else path.rmdir()
                    path.write_bytes(original)

    def test_compare_requests_only_expected_length_plus_one(self):
        self.initialize_small()
        (self.output / validator.LINEAGE_RELATIONS_FILE).write_bytes(b"x" * 65536)
        before = self.snapshot()
        actual_fdopen = validator.os.fdopen
        requested = []

        class Reader:
            def __init__(self, handle):
                self.handle = handle
            def __enter__(self):
                self.handle.__enter__()
                return self
            def __exit__(self, *args):
                return self.handle.__exit__(*args)
            def fileno(self):
                return self.handle.fileno()
            def read(self, amount):
                requested.append(amount)
                return self.handle.read(amount)

        with mock.patch.object(validator.os, "fdopen", side_effect=lambda fd, mode: Reader(actual_fdopen(fd, mode))):
            with self.assertRaisesRegex(ValueError, "lineage_artifact_payload_mismatch"):
                validator._compare_lineage_artifacts(self.output, [], {"status": "pass"})
        self.assertEqual(requested, [1])
        self.assertEqual(before, self.snapshot())

    def test_read_detects_same_bytes_replaced_inode(self):
        self.initialize_small()
        actual_fdopen = validator.os.fdopen
        observed_after_external_swap = []
        target = self.output / validator.LINEAGE_RELATIONS_FILE
        owner = self

        class Reader:
            def __init__(self, handle):
                self.handle = handle
            def __enter__(self):
                self.handle.__enter__()
                return self
            def __exit__(self, *args):
                return self.handle.__exit__(*args)
            def fileno(self):
                return self.handle.fileno()
            def read(self, amount):
                result = self.handle.read(amount)
                target.unlink()
                target.write_bytes(result)
                observed_after_external_swap.append(owner.snapshot())
                return result

        with mock.patch.object(validator.os, "fdopen", side_effect=lambda fd, mode: Reader(actual_fdopen(fd, mode))):
            with self.assertRaisesRegex(ValueError, "lineage_artifact_changed_during_read"):
                validator._compare_lineage_artifacts(self.output, [], {"status": "pass"})
        self.assertEqual(observed_after_external_swap, [self.snapshot()])

    def test_second_payload_write_failure_cleans_only_created_files(self):
        actual_fdopen = validator.os.fdopen
        writes = []

        class Writer:
            def __init__(self, handle):
                self.handle = handle
            def __enter__(self):
                self.handle.__enter__()
                return self
            def __exit__(self, *args):
                return self.handle.__exit__(*args)
            def fileno(self):
                return self.handle.fileno()
            def flush(self):
                return self.handle.flush()
            def write(self, payload):
                writes.append(len(payload))
                if len(writes) == 2:
                    raise OSError("independent second payload write failure")
                return self.handle.write(payload)

        with mock.patch.object(validator.os, "fdopen", side_effect=lambda fd, mode: Writer(actual_fdopen(fd, mode))):
            with self.assertRaisesRegex(OSError, "second payload write failure"):
                self.initialize_small()
        self.assertEqual(len(writes), 2)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_real_no_replace_collision_preserves_foreign_regular_file(self):
        actual_link = validator.os.link
        for collision_number in (1, 2):
            with self.subTest(collision_number=collision_number), tempfile.TemporaryDirectory(dir=self.fixture.base) as temporary:
                output = Path(temporary)
                calls = []
                foreign = []

                def collide(src, dst, **kwargs):
                    calls.append(dst)
                    if len(calls) == collision_number:
                        path = output / dst
                        path.write_bytes(b"independent foreign sentinel")
                        foreign.append((path, path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns))
                    return actual_link(src, dst, **kwargs)

                with mock.patch.object(validator.os, "link", side_effect=collide), self.assertRaises(FileExistsError):
                    validator._publish_lineage_artifacts(output, [], {"status": "pass"})
                path, payload, inode, mtime = foreign[0]
                self.assertEqual(list(output.iterdir()), [path])
                self.assertEqual((path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns), (payload, inode, mtime))

    def test_foreign_symlink_collision_is_preserved_without_following(self):
        actual_link = validator.os.link
        canary = self.fixture.base / "canary"
        canary.write_bytes(b"unmodified canary")
        calls = []
        foreign = self.output / validator.LINEAGE_VALIDATION_FILE

        def collide(src, dst, **kwargs):
            calls.append(dst)
            if len(calls) == 2:
                foreign.symlink_to(canary)
            return actual_link(src, dst, **kwargs)

        with mock.patch.object(validator.os, "link", side_effect=collide), self.assertRaises(FileExistsError):
            self.initialize_small()
        self.assertEqual(list(self.output.iterdir()), [foreign])
        self.assertTrue(foreign.is_symlink())
        self.assertEqual(os.readlink(foreign), str(canary))
        self.assertEqual(canary.read_bytes(), b"unmodified canary")

    def test_keyboard_interrupt_immediately_after_link_cleans_owned_inodes(self):
        actual_link = validator.os.link
        for interrupt_number in (1, 2):
            with self.subTest(interrupt_number=interrupt_number), tempfile.TemporaryDirectory(dir=self.fixture.base) as temporary:
                output = Path(temporary)
                calls = []

                def interrupted(src, dst, **kwargs):
                    calls.append(dst)
                    actual_link(src, dst, **kwargs)
                    if len(calls) == interrupt_number:
                        raise KeyboardInterrupt("independent controlled interruption")

                with mock.patch.object(validator.os, "link", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
                    validator._publish_lineage_artifacts(output, [], {"status": "pass"})
                self.assertEqual(list(output.iterdir()), [])


def worker():
    runner = load(RUNS / "f18-test-run.v2.py", "f18_holdout_runner")
    original_load = runner.load

    def load_guard(path, name):
        module = original_load(path, name)
        if path.name == "f04a-executor-run.v1.py":
            original_guard_load = module.load

            def load_target(target, target_name):
                if target.name == "test_immutable_lineage_validation.py":
                    return original_guard_load(Path(__file__), "f18_independent_holdouts")
                return original_guard_load(target, target_name)

            module.load = load_target
        return module

    runner.load = load_guard
    return runner.worker("focused")


def main():
    if "--worker" in sys.argv:
        return worker()
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f18_holdout_supervisor")
    result = supervisor.run_bounded(
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--worker"],
        RUNS / "f18-adversarial-001-holdouts", cwd=ROOT,
        timeout_seconds=30, max_log_bytes=1048576,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
