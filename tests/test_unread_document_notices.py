"""Pure display-contract tests: do not start the server or read user data."""
import ast
import html
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def function(path, name):
    tree = ast.parse((ROOT / path).read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    module = ast.Module(body=[node], type_ignores=[])
    namespace = {"html": html, "Any": object}
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[name]


collect = function("distribution/macos-local-memory/engine/build_adaptive_semantic_graph.py", "unread_document_notices")
render = function("distribution/macos-local-memory/app/local_memory_server.py", "unread_document_notice")


class UnreadDocumentNoticeTests(unittest.TestCase):
    def test_partial_names_file_without_inventing_page_or_exclusion(self):
        report = collect([{"source": {"relative_path": "DAWN.pdf"},
                           "extraction": {"status": "partial", "warnings": ["unresolved layout"]}}])
        output = render(report)
        self.assertIn("一部を読めませんでした", output)
        self.assertIn("DAWN.pdf", output)
        self.assertIn("場所を特定できませんでした", output)
        self.assertNotIn("回答の根拠に使っていません", output)

    def test_failed_and_deferred_and_unknown_reason(self):
        for status in ("failed", "deferred"):
            output = render(collect([{"extraction": {"status": status}}]))
            self.assertIn("理由を特定できませんでした", output)

    def test_escaped_file_and_error_are_never_html(self):
        output = render(collect([{"source": {"relative_path": "<script>.pdf"},
                                 "extraction": {"status": "failed", "errors": ["<img onerror=x>"]}}]))
        self.assertNotIn("<script>", output)
        self.assertNotIn("<img", output)
        self.assertIn("&lt;script&gt;", output)

    def test_success_is_not_a_read_failure(self):
        self.assertEqual(collect([{"extraction": {"status": "success"}}])["total"], 0)

    def test_display_limit_is_explicit(self):
        report = collect([{"extraction": {"status": "partial"}}] * 102)
        self.assertEqual(len(report["items"]), 100)
        self.assertEqual(report["omitted"], 2)
        self.assertIn("ほか2件", render(report))

    def test_legacy_state_has_no_false_complete_claim(self):
        self.assertIn("詳細記録はありません", render(None))

    def test_propagation_wiring_exists(self):
        bridge = (ROOT / "distribution/macos-local-memory/engine/build_adaptive_semantic_graph.py").read_text()
        bootstrap = (ROOT / "distribution/macos-local-memory/app/bootstrap.py").read_text()
        server = (ROOT / "distribution/macos-local-memory/app/local_memory_server.py").read_text()
        self.assertIn('"unread_document_notices": unread_document_notices(', bridge)
        self.assertIn('"unread_document_notices": reader_state.get(', bootstrap)
        self.assertIn('setup += unread_document_notice(current.get(', server)


if __name__ == "__main__":
    unittest.main(verbosity=2)
