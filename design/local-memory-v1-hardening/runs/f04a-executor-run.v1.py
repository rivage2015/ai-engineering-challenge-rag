"""Scoped RED/regression runner using the reviewed F01 synthetic dispatcher.

Only the supervisor creates a Python test process. Worker HTTP, socket and
process launches are denied, including Reader runtime-metadata probes. Fixture
source writes are cumulatively limited to 1 MiB; logs and elapsed time are
bounded by the existing supervisor. This is not an OS security sandbox.
"""
from __future__ import annotations

import contextlib
import http.client
import importlib.util
import json
import mimetypes
import os
from pathlib import Path
import socket
import site
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"
TESTS = ROOT / "distribution/macos-local-memory/tests"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def worker(mode):
    sys.path[:0] = [str(TESTS), str(ROOT / "scripts")]
    # Use Python's built-in MIME table, not host Apache configuration.
    with mock.patch.object(mimetypes, "knownfiles", []):
        mimetypes.init()
    fixture_root = tempfile.TemporaryDirectory(prefix="f04a-fixtures-", dir="/private/tmp")
    tempfile.tempdir = fixture_root.name
    fixture_path = os.path.abspath(fixture_root.name)
    fixture_ancestors = {str(path) for path in Path(fixture_path).parents}
    reads = tuple(os.path.abspath(str(path)) for path in (
        ROOT / "scripts", ROOT / "schemas", ROOT / "tests", TESTS,
        ROOT / "distribution/macos-local-memory/engine",
        ROOT / "distribution/macos-local-memory/app", RUNS,
        Path(sys.prefix), Path(sys.base_prefix), Path(sys.prefix).resolve(),
        Path(sys.base_prefix).resolve(), *map(Path, site.getsitepackages()),
    ))
    special_read = {
        "/System/Library/CoreServices/SystemVersion.plist",
        "/usr/share/zoneinfo/Asia/Tokyo",
        str(Path(sys.executable).resolve()),
        str(ROOT / "distribution/macos-local-memory/paddleocr-model-manifest.json"),
        str(ROOT / "distribution/macos-local-memory/paddleocr-requirements.lock.txt"),
    }
    descriptor_paths = {}
    audited_open_path = None
    original_os_open = os.open

    def inside(path, roots):
        return any(path == item or path.startswith(item + os.sep) for item in roots)

    def audit(event, args):
        if event.startswith(("socket.", "subprocess.")) or event in {
            "os.system", "os.fork", "os.forkpty", "os.posix_spawn", "os.exec",
        }:
            raise AssertionError("F04a forbidden network/process: " + event)
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = audited_open_path or os.path.abspath(os.fsdecode(args[0]))
            mode_value, flags = args[1], args[2]
            writing = (isinstance(mode_value, str) and any(c in mode_value for c in "wax+")) or (isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)))
            if writing and not inside(path, (fixture_path,)):
                raise AssertionError("F04a write outside fixtures: " + path)
            directory_anchor = isinstance(flags, int) and bool(flags & os.O_DIRECTORY) and path in fixture_ancestors
            if not writing and not (inside(path, (*reads, fixture_path)) or path in special_read or directory_anchor):
                raise AssertionError("F04a read outside reviewed code/runtime/fixtures: " + path)

    def bounded_os_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal audited_open_path
        decoded = os.fsdecode(path)
        if dir_fd is not None and not os.path.isabs(decoded):
            if dir_fd not in descriptor_paths:
                raise AssertionError("F04a unknown directory descriptor")
            effective = os.path.abspath(os.path.join(descriptor_paths[dir_fd], decoded))
        else:
            effective = os.path.abspath(decoded)
        previous = audited_open_path
        audited_open_path = effective
        try:
            descriptor = original_os_open(path, flags, mode, dir_fd=dir_fd)
            descriptor_paths[descriptor] = effective
            return descriptor
        finally:
            audited_open_path = previous

    sys.addaudithook(audit)
    os.open = bounded_os_open
    if original_os_open in os.supports_dir_fd:
        # Preserve the actual primitive's advertised capability through the
        # guard; bounded_os_open forwards dir_fd unchanged to that primitive.
        os.supports_dir_fd = {*os.supports_dir_fd, bounded_os_open}
    source_bytes = 0
    original_write = Path.write_text

    def bounded_write(path, data, *args, **kwargs):
        nonlocal source_bytes
        if inside(os.path.abspath(path), (fixture_path,)) and "source" in path.parts:
            source_bytes += len(data.encode(kwargs.get("encoding") or "utf-8"))
            if source_bytes > 1048576:
                raise AssertionError("F04a cumulative fixture source limit")
        return original_write(path, data, *args, **kwargs)

    harness = None
    try:
        with contextlib.ExitStack() as guards:
            guards.enter_context(mock.patch.object(socket.socket, "connect", side_effect=AssertionError("real socket forbidden")))
            guards.enter_context(mock.patch.object(socket.socket, "connect_ex", side_effect=AssertionError("real socket forbidden")))
            guards.enter_context(mock.patch.object(http.client.HTTPConnection, "connect", side_effect=AssertionError("real HTTP forbidden")))
            guards.enter_context(mock.patch.object(subprocess, "Popen", side_effect=AssertionError("real subprocess forbidden")))
            guards.enter_context(mock.patch.object(Path, "write_text", bounded_write))
            isolated = load(TESTS / "test_versioned_safe_index_e2e.py", "f04a_isolated")
            if mode == "e2e":
                suite = unittest.defaultTestLoader.loadTestsFromModule(isolated)
            else:
                # Same reviewed dispatch stubs OCR/Paddle/PDF/VLM identities and
                # password discovery before build_intermediate_records.main().
                harness = isolated.VersionedSafeIndexE2E()
                harness.setUp()
                target = load(TESTS / "test_document_version_resolver.py", "f04a_resolver_tests")
                target.builder.run_tool = lambda _label, command, _tools, _log: harness.run_cli(command)
                if mode == "red":
                    suite = unittest.TestSuite((target.DocumentVersionResolverTests("test_current_marker_conflicts_with_higher_numeric_version"),))
                else:
                    suite = unittest.defaultTestLoader.loadTestsFromModule(target)
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            return 0 if result.wasSuccessful() else 1
    finally:
        if harness is not None:
            harness.doCleanups()
        fixture_root.cleanup()


def main():
    mode, run_id = sys.argv[1:3]
    if mode not in {"red", "resolver", "e2e"} or not run_id.startswith("f04a-"):
        raise SystemExit("invalid fixed-scope run")
    if "--worker" in sys.argv:
        return worker(mode)
    supervisor = load(ROOT / "scripts/run_local_memory_hardening_tests.py", "f04a_supervisor")
    result = supervisor.run_bounded(
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), mode, run_id, "--worker"],
        RUNS / run_id, cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
