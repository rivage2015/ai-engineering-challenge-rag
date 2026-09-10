"""Auditor guards for the bounded terminal-dot diagnostic only."""
import contextlib
import http.client
import runpy
import socket
import subprocess
import sys
from pathlib import Path
from unittest import mock

target = Path(__file__).with_name("f10-adversarial-dot-cases.v1.py")
sys.argv = [str(target), "-v"]
with contextlib.ExitStack() as guards:
    guards.enter_context(mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden by auditor")))
    guards.enter_context(mock.patch.object(socket.socket, "connect_ex", side_effect=AssertionError("network forbidden by auditor")))
    guards.enter_context(mock.patch.object(http.client.HTTPConnection, "connect", side_effect=AssertionError("HTTP forbidden by auditor")))
    guards.enter_context(mock.patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess forbidden by auditor")))
    runpy.run_path(str(target), run_name="__main__")
