from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "evidence.schema.json"


class EvidenceSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.evidence_types = cls.schema["properties"]["evidence_type"]["enum"]
        cls.geometry = cls.schema["properties"]["geometry"]

    def test_image_ocr_line_evidence_type_is_officially_supported(self) -> None:
        self.assertIn("ocr_line", self.evidence_types)

    def test_normalized_1000_geometry_is_officially_supported(self) -> None:
        self.assertIn("normalized_1000", self.geometry["properties"]["unit"]["enum"])
        self.assertEqual(
            self.geometry["properties"]["coordinate_origin"]["enum"],
            ["top_left"],
        )

    def test_coordinate_origin_is_additive_and_remains_optional(self) -> None:
        self.assertEqual(self.geometry["required"], ["coordinate_space", "unit"])
        self.assertNotIn("coordinate_origin", self.geometry["required"])

    def test_every_preexisting_geometry_unit_remains_supported(self) -> None:
        units = set(self.geometry["properties"]["unit"]["enum"])
        self.assertTrue({"pt", "emu", "px", "cell", "other"} <= units)
        self.assertNotIn("percentage", units)

    def test_every_preexisting_evidence_type_remains_supported(self) -> None:
        preexisting = {
            "page", "slide", "worksheet", "header", "footer", "speaker_note",
            "text_block", "paragraph", "heading", "table", "table_row",
            "table_cell", "merged_range", "formula", "filter", "pivot_table",
            "data_validation", "defined_name", "chart", "chart_series", "image",
            "shape", "connector", "comment", "style_span", "hyperlink", "field",
            "tracked_change", "code_block", "notebook_cell", "metadata", "other",
        }
        self.assertTrue(preexisting <= set(self.evidence_types))
        self.assertNotIn("ocr_guess", self.evidence_types)

    def test_geometry_remains_closed_to_unknown_properties(self) -> None:
        self.assertIs(self.geometry["additionalProperties"], False)
        self.assertNotIn("unpublished_coordinate_hint", self.geometry["properties"])


if __name__ == "__main__":
    unittest.main()
