"""Independent small binding counterexamples; rejection assertions may fail."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("f10_fixture_helpers", ROOT / "tests/test_xlsx_fallback_binding_hardening.py")
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


class AuditorCounterexamples(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.XlsxFallbackBindingHardeningTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_absolute_url_cannot_bind_even_if_zip_has_posix_lookalike(self):
        path = self.fixture.source(
            workbook_rels=fixtures.relationships(fixtures.relationship(target="https://invalid.example/worksheet.xml")),
            parts={"xl/https:/invalid.example/worksheet.xml": fixtures.worksheet("forged URI canary")},
        )
        self.fixture.assert_binding_rejected(path)

    def test_late_missing_direct_sheet_data_leaves_no_reader_evidence(self):
        path = self.fixture.source(
            workbook_xml=fixtures.workbook(fixtures.sheet(), fixtures.sheet("bad second", "2", "r2")),
            workbook_rels=fixtures.relationships(fixtures.relationship(), fixtures.relationship("r2", "worksheets/second.xml")),
            parts={"xl/worksheets/second.xml": fixtures.worksheet(body="<extension><sheetData/></extension>")},
        )
        self.fixture.assert_binding_rejected(path)


if __name__ == "__main__":
    unittest.main()
