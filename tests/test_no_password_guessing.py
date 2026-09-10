"""Read actual function AST without loading OCR/model dependencies."""
import ast
import io
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TREE = ast.parse((ROOT / "scripts/probe_intermediate_records.py").read_text())


def load(name):
    nodes = list(TREE.body)
    for node in TREE.body:
        if isinstance(node, ast.ClassDef) and node.name == "Probe":
            nodes += node.body
    node = next(n for n in nodes if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = {"Path": Path, "io": io, "zipfile": mock.Mock()}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "actual-reader-function", "exec"), ns)
    return ns[name], ns


class PasswordSafetyTests(unittest.TestCase):
    def test_discovery_never_inspects_root(self):
        method, _ = load("discover_password_candidates")
        root = mock.Mock()
        root.rglob.side_effect = AssertionError("must not scan")
        self.assertEqual(method(root), ())
        self.assertEqual(root.mock_calls, [])

    def test_non_zip_never_tries_batch_secrets(self):
        method, ns = load("office_source")
        ns["zipfile"].is_zipfile.return_value = False
        instance = mock.Mock(password_candidates=("test-secret-never-log",))
        with self.assertRaises(ValueError) as result:
            method(instance, Path("synthetic.docx"))
        self.assertIn("requires_human_review", str(result.exception))
        self.assertNotIn("test-secret-never-log", str(result.exception))
        self.assertEqual(instance.mock_calls, [])

    def test_plain_zip_still_uses_original_path(self):
        method, ns = load("office_source")
        ns["zipfile"].is_zipfile.return_value = True
        self.assertEqual(method(mock.Mock(), Path("synthetic.xlsx")), ("synthetic.xlsx", False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
