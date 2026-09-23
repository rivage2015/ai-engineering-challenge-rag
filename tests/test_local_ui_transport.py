"""Run the shipped UI transport in a small, offline Node DOM test harness.

This is a JavaScript behavior test, not a substitute for the actual browser
Origin/Referrer-Policy check. No app module is imported and no server is started.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "distribution/macos-local-memory/app/local_memory_server.py"
HARNESS = Path(__file__).with_name("local_ui_transport.test.js")


def ui_script() -> str:
    """Extract the real constant without importing runtime/config dependencies."""
    tree = ast.parse(SERVER.read_text(encoding="utf-8"), filename=str(SERVER))
    assignments = [
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "UI_SCRIPT"
                for target in node.targets)
    ]
    if len(assignments) != 1:
        raise AssertionError("Expected exactly one UI_SCRIPT assignment")
    expression = assignments[0].value
    if (not isinstance(expression, ast.Call)
            or not isinstance(expression.func, ast.Attribute)
            or expression.func.attr != "encode"
            or [ast.literal_eval(arg) for arg in expression.args] != ["utf-8"]):
        raise AssertionError("UI_SCRIPT must remain a literal encoded as UTF-8")
    source = ast.literal_eval(expression.func.value)
    if not isinstance(source, str):
        raise AssertionError("UI_SCRIPT must contain JavaScript text")
    return source


def node_binary() -> str:
    configured = os.environ.get("LOCAL_MEMORY_TEST_NODE")
    if configured:
        return configured
    installed = shutil.which("node")
    if installed:
        return installed
    bundled = (Path.home() / ".cache/codex-runtimes/codex-primary-runtime"
               / "dependencies/node/bin/node")
    if bundled.is_file():
        return str(bundled)
    raise AssertionError(
        "Node.js is required: put node on PATH or set LOCAL_MEMORY_TEST_NODE"
    )


class LocalUiTransportTests(unittest.TestCase):
    def test_shipped_ui_script_transport_contract(self):
        completed = subprocess.run(
            [node_binary(), str(HARNESS)], input=ui_script(), text=True,
            capture_output=True, timeout=20, check=False,
        )
        self.assertTrue(completed.stdout, completed.stderr)
        results = json.loads(completed.stdout)
        self.assertGreaterEqual(len(results), 16)
        for result in results:
            with self.subTest(case=result["name"]):
                self.assertTrue(result["ok"], result.get("error", ""))
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
