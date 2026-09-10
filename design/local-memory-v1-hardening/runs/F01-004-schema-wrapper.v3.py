"""Bound the 20-case schema regression with network/model/process guards."""
from __future__ import annotations

import argparse
import contextlib
import http.client
import importlib.util
import re
import subprocess
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    if not args.worker:
        if not args.run_id or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", args.run_id):
            parser.error("a unique bounded --run-id is required")
        runner = load("schema_bounded_runner", ROOT / "scripts/run_local_memory_hardening_tests.py")
        result = runner.run_bounded(
            [sys.executable, "-B", str(Path(__file__).resolve()), "--worker"],
            ROOT / "artifacts/local-memory-v1-hardening/runs" / args.run_id,
            cwd=ROOT, timeout_seconds=30, max_log_bytes=1048576,
        )
        print(result)
        return int(result["status"] != "passed")
    module = load("f01_schema_regression", ROOT / "tests/test_local_graph_index_schema.py")
    with contextlib.ExitStack() as guards:
        for target, attribute in (
            (http.client.HTTPConnection, "connect"),
            (urllib.request.OpenerDirector, "open"),
            (subprocess, "Popen"),
            (module.index_builder, "embed"),
        ):
            guards.enter_context(mock.patch.object(target, attribute, side_effect=AssertionError("unexpected external call")))
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(module))
    return int(not result.wasSuccessful())


if __name__ == "__main__":
    raise SystemExit(main())
