"""Repair1 URI terminal-dot counterexamples and supported-path controls."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("f10_fixture_helpers", ROOT / "tests/test_xlsx_fallback_binding_hardening.py")
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


class AuditorUriTerminalDotTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.XlsxFallbackBindingHardeningTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_worksheet_directory_dot_reference_cannot_bind_existing_file_part(self):
        for index, suffix in enumerate(("/.", "/child/..")):
            target = "worksheets/sheet1.xml" + suffix
            with self.subTest(target=target):
                # Pure stdlib URI composition, no HTTP operation.
                self.assertTrue(urljoin("https://package.invalid/xl/workbook.xml", target).endswith("/"))
                path = self.fixture.source(
                    "sheet-dot-" + str(index) + ".xlsx",
                    workbook_rels=fixtures.relationships(fixtures.relationship(target=target)),
                )
                self.fixture.assert_binding_rejected(path)

    def test_package_root_directory_dot_reference_cannot_bind_workbook_file(self):
        for index, suffix in enumerate(("/.", "/child/..")):
            target = "xl/workbook.xml" + suffix
            with self.subTest(target=target):
                self.assertTrue(urljoin("https://package.invalid/", target).endswith("/"))
                path = self.fixture.source(
                    "root-dot-" + str(index) + ".xlsx",
                    root_rels=fixtures.relationships(fixtures.relationship("root", target, "officeDocument")),
                )
                self.fixture.assert_binding_rejected(path)

    def test_nonterminal_dot_paths_and_relative_colon_path_still_bind(self):
        cases = (
            ("./worksheets/sheet1.xml", "xl/worksheets/sheet1.xml"),
            ("worksheets/../worksheets/sheet1.xml", "xl/worksheets/sheet1.xml"),
            ("/xl/./worksheets/sheet1.xml", "xl/worksheets/sheet1.xml"),
            ("./part:name.xml", "xl/part:name.xml"),
        )
        for index, (target, member) in enumerate(cases):
            with self.subTest(target=target):
                path = self.fixture.source(
                    "normal-" + str(index) + ".xlsx",
                    workbook_rels=fixtures.relationships(fixtures.relationship(target=target)),
                    parts={member: fixtures.worksheet("valid internal target")},
                )
                before = fixtures.probe.digest_file(path)
                reader = self.fixture.extract(path)
                self.assertEqual([item["content"]["raw_value"] for item in reader.evidence if item["evidence_type"] == "table_cell"], ["valid internal target"])
                self.assertEqual(fixtures.probe.digest_file(path), before)


if __name__ == "__main__":
    unittest.main()
