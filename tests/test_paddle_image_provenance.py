from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_intermediate_records as builder
import local_image_ocr
import probe_intermediate_records as probe_records


RUN_AT = "2026-09-03T00:00:00+00:00"
COORDINATE_SYSTEM = "source_orientation_1_top_left_normalized_1000"


class PaddleImageProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="aiec-paddle-provenance-"
        )
        self.root = Path(self.temporary.name)
        self.image = self.root / "sample.png"
        self.image.write_bytes(b"stable-image-source")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def honest_line() -> dict[str, object]:
        return {
            "text": "中野",
            "bbox": [100, 100, 400, 80],
            "bbox_coordinate_system": COORDINATE_SYSTEM,
            "overlap": 1.0,
            "primary_confidence": 0.95,
            "audit_confidence": 0.85,
            "agreement_type": "independent_agreement",
            "quality_tier": "high",
            "provenance": {
                "primary_pass": "paddleocr_primary",
                "audit_pass": "tesseract_psm3",
                "primary_engine": "paddleocr",
                "audit_engine": "tesseract",
                "primary_independence_group": "paddleocr",
                "audit_independence_group": "tesseract",
                "primary_line_id": "paddle-1",
                "audit_line_id": "psm3-1",
                "primary_bbox_coordinate_system": COORDINATE_SYSTEM,
                "audit_bbox_coordinate_system": COORDINATE_SYSTEM,
                "comparison_coordinate_system": COORDINATE_SYSTEM,
                "supporters": [
                    {
                        "pass": "paddleocr_primary",
                        "engine": "paddleocr",
                        "independence_group": "paddleocr",
                        "line_id": "paddle-1",
                        "raw_text": "中野",
                        "bbox": [100, 100, 400, 80],
                        "bbox_coordinate_system": COORDINATE_SYSTEM,
                        "confidence": 0.95,
                    },
                    {
                        "pass": "tesseract_psm3",
                        "engine": "tesseract",
                        "independence_group": "tesseract",
                        "line_id": "psm3-1",
                        "raw_text": "中野",
                        "bbox": [100, 100, 400, 80],
                        "bbox_coordinate_system": COORDINATE_SYSTEM,
                        "confidence": 0.85,
                    },
                ],
            },
        }

    def observation(self, line: dict[str, object]) -> dict[str, object]:
        dimensions = {"width_px": 800, "height_px": 600}
        source_sha256 = hashlib.sha256(self.image.read_bytes()).hexdigest()
        return {
            "input_sha256": source_sha256,
            "source_dimensions": dimensions,
            "dimensions": dimensions,
            "image_format": "PNG",
            "orientation": 1,
            "orientation_source": "imageio_canonicalizer",
            "canonicalization": {
                "status": "completed",
                "canonical_dimensions": dimensions,
                "canonical_orientation": 1,
            },
            "ocr_input_sha256": source_sha256,
            "ocr_input_dimensions": dimensions,
            "ocr_input_orientation": 1,
            "coordinate_frame_policy": "canonical_orientation_1",
            "engines": {},
            "independent_engines": True,
            "consensus_lines": [line],
            "read_lines": [line],
            "unlocated_transcript": None,
            "unresolved_count": 0,
        }

    def extract_observation(self, line: dict[str, object]) -> probe_records.Probe:
        probe = probe_records.Probe(
            self.root,
            RUN_AT,
            None,
            diagnostic=False,
        )
        with mock.patch.object(
            local_image_ocr,
            "extract",
            return_value=self.observation(line),
        ):
            probe.extract(self.image)
        return probe

    def test_registered_paddle_pass_is_an_independent_engine_group(self) -> None:
        self.assertEqual(
            probe_records.ocr_engine("paddleocr_primary"), "paddleocr"
        )
        self.assertEqual(
            probe_records.ocr_independence_group("paddleocr_primary"),
            "paddleocr",
        )

    def test_honest_paddle_provenance_uses_reader_v070(self) -> None:
        probe = self.extract_observation(self.honest_line())

        self.assertEqual(
            probe.documents[0]["extraction"]["parser"],
            "adaptive-local-image-reader-v0.7.0",
        )
        ocr_line = next(
            item for item in probe.evidence if item["evidence_type"] == "ocr_line"
        )
        self.assertEqual(
            ocr_line["native_properties"]["observation_provenance"][
                "primary_independence_group"
            ],
            "paddleocr",
        )

    def test_forged_paddle_pass_is_rejected(self) -> None:
        forged = self.honest_line()
        forged["provenance"]["primary_pass"] = "paddleocr_secondary"

        with self.assertRaisesRegex(ValueError, "unsupported OCR provenance pass"):
            self.extract_observation(forged)

    def test_forged_paddle_engine_is_rejected(self) -> None:
        forged = self.honest_line()
        forged["provenance"]["primary_engine"] = "apple_vision"

        with self.assertRaisesRegex(ValueError, "primary engine is invalid"):
            self.extract_observation(forged)

    def test_forged_paddle_independence_group_is_rejected(self) -> None:
        forged = self.honest_line()
        forged["provenance"]["primary_independence_group"] = "tesseract"

        with self.assertRaisesRegex(
            ValueError, "primary independence group is invalid"
        ):
            self.extract_observation(forged)

    def test_forged_raw_supporter_text_is_rejected(self) -> None:
        forged = self.honest_line()
        forged["provenance"]["supporters"][1]["raw_text"] = "新宿"

        with self.assertRaisesRegex(
            ValueError, "supporter text does not reproduce"
        ):
            self.extract_observation(forged)

    def test_previous_builder_version_cannot_resume_new_shards(self) -> None:
        output = self.root / "intermediate"
        output.mkdir()
        state = {
            "state_version": builder.STATE_VERSION,
            "build_status": "in_progress",
            "source_root": probe_records.nfc_path(self.root),
            "extractor": builder.EXTRACTOR,
            "extractor_version": "0.8.0",
            "run_at": RUN_AT,
            "input_paths": [],
            "entries": {},
        }
        (output / builder.STATE_FILE).write_text(
            json.dumps(state) + "\n", encoding="utf-8"
        )

        self.assertEqual(probe_records.EXTRACTOR_VERSION, "0.7.1")
        self.assertEqual(builder.EXTRACTOR_VERSION, "0.11.0")
        with self.assertRaisesRegex(ValueError, "resume mismatch for extractor_version"):
            builder.load_state(output, self.root, None)


if __name__ == "__main__":
    unittest.main()
