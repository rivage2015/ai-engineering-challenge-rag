from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_ocr_poc_ndlocr as ndlocr_runner


class NDLOCRPoCRunnerTest(unittest.TestCase):
    def test_line_conversion_retains_order_text_and_confidence(self) -> None:
        raw = [
            {
                "text": "  会議録 ",
                "confidence": 0.8125,
                "boundingBox": [[10, 5], [10, 25], [90, 5], [90, 25]],
            },
            {
                "text": "Action",
                "confidence": None,
                "boundingBox": [[0, 0], [0, 10], [100, 0], [100, 10]],
            },
        ]
        lines = ndlocr_runner.convert_engine_lines(raw, width=100, height=50)
        self.assertEqual(["  会議録 ", "Action"], [line["raw_text"] for line in lines])
        self.assertEqual([1, 2], [line["sequence"] for line in lines])
        self.assertEqual(0.8125, lines[0]["confidence"])
        self.assertIsNone(lines[1]["confidence"])
        self.assertEqual([100, 100, 800, 400], lines[0]["bbox"])
        self.assertEqual([0, 0, 1000, 200], lines[1]["bbox"])

    def test_line_conversion_rejects_unrepresentable_empty_text(self) -> None:
        raw = [{
            "text": "",
            "confidence": 0.5,
            "boundingBox": [[0, 0], [0, 10], [10, 0], [10, 10]],
        }]
        with self.assertRaisesRegex(ValueError, "empty text"):
            ndlocr_runner.convert_engine_lines(raw, width=10, height=10)

    def test_bbox_conversion_rejects_out_of_crop_coordinates(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside the crop"):
            ndlocr_runner._normalized_bbox(
                [[-1, 0], [-1, 10], [10, 0], [10, 10]], width=10, height=10
            )

    def test_checkout_integrity_rejects_dirty_checkout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-ndlocr-dirty-") as raw:
            checkout = Path(raw)
            with mock.patch.object(
                ndlocr_runner, "_git_status", return_value=" M src/ocr.py"
            ):
                with self.assertRaisesRegex(ValueError, "checkout is dirty"):
                    ndlocr_runner.verify_checkout_integrity(checkout)

    def test_checkout_integrity_rejects_tracked_source_byte_change(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-ndlocr-source-") as raw:
            checkout = Path(raw)
            source = checkout / "src" / "ocr.py"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"working tree bytes\n")
            with mock.patch.object(ndlocr_runner, "_git_status", return_value=""):
                with mock.patch.object(
                    ndlocr_runner,
                    "_tracked_execution_source_paths",
                    return_value=["src/ocr.py"],
                ):
                    with mock.patch.object(
                        ndlocr_runner,
                        "_git_blob_sha256",
                        return_value="0" * 64,
                    ):
                        with self.assertRaisesRegex(ValueError, "source byte mismatch"):
                            ndlocr_runner.verify_checkout_integrity(checkout)


if __name__ == "__main__":
    unittest.main()
