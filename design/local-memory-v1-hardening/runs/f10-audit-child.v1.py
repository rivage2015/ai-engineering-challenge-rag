"""Auditor wrapper: preflight-reviewed synthetic suites only, no child processes/I/O."""
from __future__ import annotations

import contextlib
import http.client
import runpy
import socket
import subprocess
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
TARGETS = {
    "focused": ROOT / "tests/test_xlsx_fallback_binding_hardening.py",
    "regression": ROOT / "tests/test_generic_structured_reader_hardening.py",
    "legacy": ROOT / "design/local-memory-v1-hardening/runs/f10-legacy-fixture-check.v2.py",
    "adversarial": ROOT / "design/local-memory-v1-hardening/runs/f10-adversarial-cases.v1.py",
}
target = TARGETS[sys.argv[1]]
sys.path.insert(0, str(ROOT / "scripts"))
sys.argv = [str(target), "-v"]
with contextlib.ExitStack() as guards:
    guards.enter_context(mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden by auditor")))
    guards.enter_context(mock.patch.object(socket.socket, "connect_ex", side_effect=AssertionError("network forbidden by auditor")))
    guards.enter_context(mock.patch.object(http.client.HTTPConnection, "connect", side_effect=AssertionError("HTTP forbidden by auditor")))
    guards.enter_context(mock.patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess forbidden by auditor")))
    runpy.run_path(str(target), run_name="__main__")
