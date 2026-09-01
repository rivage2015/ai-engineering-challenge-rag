from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "evaluate_private_xlsx_route.py"
SPEC = importlib.util.spec_from_file_location("evaluate_private_xlsx_route", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise ImportError(f"cannot load {SCRIPT}")
route = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = route
SPEC.loader.exec_module(route)


class PrivateXlsxRouteTest(unittest.TestCase):
    def test_query_fields_keeps_only_label_value_lines(self) -> None:
        self.assertEqual(
            route.query_fields("Name: alpha\nignored\nHours: 7\n: blank"),
            ["Name alpha", "Hours 7"],
        )

    def test_select_cases_is_bounded_per_sheet(self) -> None:
        records = []
        for sheet in ("A", "B"):
            for index in range(3):
                records.append({
                    "evidence_id": f"{sheet}-{index}",
                    "locator": {"sheet_name": sheet, "row_index": index + 1},
                    "observed_text": f"Key: {sheet}{index}\nValue: {index}",
                    "adapter": {"unit_type": "table_row"},
                })
        cases = route.select_cases(records, per_sheet=2)
        self.assertEqual(len(cases), 4)
        self.assertEqual([item["evidence_id"] for item in cases], ["A-0", "A-1", "B-0", "B-1"])

    def test_select_cases_excludes_unusable_or_non_row_records(self) -> None:
        records = [
            {
                "evidence_id": "cell",
                "locator": {"sheet_name": "A"},
                "observed_text": "Key: value\nOther: value",
                "adapter": {"source_record_type": "table_cell"},
            },
            {
                "evidence_id": "short",
                "locator": {"sheet_name": "A"},
                "observed_text": "Key: value",
                "adapter": {"unit_type": "table_row"},
            },
        ]
        self.assertEqual(route.select_cases(records, per_sheet=2), [])


if __name__ == "__main__":
    unittest.main()
