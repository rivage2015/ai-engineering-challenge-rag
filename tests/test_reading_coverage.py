from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "scripts"))

import validate_reading_coverage


class ReadingCoverageTest(unittest.TestCase):
    def test_direct_ocr_and_native_container_form_exact_total(self) -> None:
        with tempfile.TemporaryDirectory(prefix="reading-coverage-") as temporary:
            root = Path(temporary)
            paths = {
                name: root / name
                for name in (
                    "manifest.jsonl", "materializable.jsonl", "materialized.jsonl",
                    "classifications.jsonl", "observations.jsonl", "native.jsonl",
                )
            }
            direct = {
                "asset_id": "asset_direct", "origin": {"kind": "standalone_image"},
                "source": {"relative_path": "image.png"},
            }
            container = {
                "asset_id": "asset_container", "origin": {"kind": "visual_container"},
                "source": {"relative_path": "deck.pptx"},
            }
            fixtures = {
                "manifest.jsonl": [direct, container],
                "materializable.jsonl": [direct],
                "materialized.jsonl": [direct],
                "classifications.jsonl": [{"asset_id": "asset_direct", "routes": ["ocr_text"]}],
                "observations.jsonl": [{"asset_id": "asset_direct", "status": "needs_review"}],
                "native.jsonl": [{"source_path": "deck.pptx"}],
            }
            for name, records in fixtures.items():
                paths[name].write_text(
                    "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
                )
            inventory = root / "inventory.csv"
            with inventory.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["file_path", "extraction_status", "text_extractable"],
                )
                writer.writeheader()
                writer.writerow({
                    "file_path": "deck.pptx", "extraction_status": "success",
                    "text_extractable": "true",
                })
            with mock.patch.object(
                validate_reading_coverage.manifest_validator, "validate"
            ), mock.patch.object(
                validate_reading_coverage.classification_validator, "validate_jsonl"
            ), mock.patch.object(
                validate_reading_coverage.ocr_validator, "validate_jsonl"
            ):
                result = validate_reading_coverage.validate(
                    manifest=paths["manifest.jsonl"],
                    materializable=paths["materializable.jsonl"],
                    materialized=paths["materialized.jsonl"],
                    classifications=paths["classifications.jsonl"],
                    observations=paths["observations.jsonl"],
                    inventory=inventory,
                    native_raw=paths["native.jsonl"],
                    source_root=root,
                    asset_root=root,
                )
            self.assertEqual(result["records"], 2)
            self.assertEqual(result["uncovered"], 0)
            self.assertEqual(result["coverage"]["dual_ocr_needs_review"], 1)
            self.assertEqual(result["coverage"]["native_container_text"], 1)


if __name__ == "__main__":
    unittest.main()
