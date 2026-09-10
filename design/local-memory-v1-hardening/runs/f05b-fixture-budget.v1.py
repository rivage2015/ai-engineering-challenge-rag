"""Count only explicit test-authored Path writes, not all product artifacts.

The existing guard confines actual writes to tmp. This additional budget is
not a whole-process disk/RAM cap or OS sandbox. It counts writes directly
authored by the frozen test/fixture files, through the known Path wrappers.
"""
from contextlib import contextmanager, ExitStack
import inspect
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "design/local-memory-v1-hardening/runs"
AUTHORS = {str(ROOT / path) for path in (
    "tests/test_decision_snapshot_e2e.py", "tests/test_decision_snapshot_controls.py",
    "tests/test_decision_snapshot_attestation.py",
    "distribution/macos-local-memory/tests/test_versioned_safe_index_e2e.py",
    "design/local-memory-v1-hardening/runs/f05b-audit-holdouts.v1.py",
)}
WRAPPERS = {str(Path(__file__).resolve()), str(RUNS / "f04a-executor-run.v1.py")}


@contextmanager
def enforce():
    counters = {"explicit_test_bytes": 0, "explicit_test_writes": 0}
    sources = {}
    original_text, original_bytes = Path.write_text, Path.write_bytes
    original_stop = unittest.TextTestResult.stopTestRun

    def authored():
        frame = inspect.currentframe().f_back
        try:
            while frame is not None:
                name = os.path.abspath(frame.f_code.co_filename)
                if name not in WRAPPERS:
                    return name in AUTHORS
                frame = frame.f_back
            return False
        finally:
            del frame

    def account(path, size):
        if not authored():
            return
        counters["explicit_test_writes"] += 1
        counters["explicit_test_bytes"] += size
        if counters["explicit_test_bytes"] > 1048576:
            raise AssertionError("F05b explicit fixture cumulative byte limit")
        name = path.name
        cap = 8192 if "decision" in name and name.endswith(".json") else 16384 if "inventory" in name else 65536 if "graph" in name and name.endswith(".json") else None
        if cap is not None and size > cap:
            raise AssertionError("F05b explicit fixture individual limit: " + name)
        parts = path.parts
        if "source" in parts:
            key = str(Path(*parts[:parts.index("source") + 1]))
            sources[key] = sources.get(key, 0) + size
            if sources[key] > 16384:
                raise AssertionError("F05b explicit source fixture per-case limit")

    def write_text(path, data, *args, **kwargs):
        encoding = kwargs.get("encoding") or (args[0] if args else None) or "utf-8"
        errors = kwargs.get("errors") or (args[1] if len(args) > 1 else None) or "strict"
        account(path, len(data.encode(encoding, errors)))
        return original_text(path, data, *args, **kwargs)

    def write_bytes(path, data):
        account(path, memoryview(data).nbytes)
        return original_bytes(path, data)

    def stop(result):
        original_stop(result)
        result.stream.writeln("F05b explicit fixture budget: " + json.dumps({**counters, "source_fixture_count": len(sources), "max_source_fixture_bytes": max(sources.values(), default=0)}, sort_keys=True))

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(Path, "write_text", write_text))
        stack.enter_context(mock.patch.object(Path, "write_bytes", write_bytes))
        stack.enter_context(mock.patch.object(unittest.TextTestResult, "stopTestRun", stop))
        yield counters
