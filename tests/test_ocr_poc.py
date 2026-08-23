from __future__ import annotations

import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_ocr_poc_manifest as builder
import evaluate_ocr_poc as evaluator
import merge_ocr_poc_runs as merger
import ocr_poc_adapters as adapters
import ocr_poc_contract as contract
import run_ocr_poc as runner


STAMP = "2026-08-17T00:00:00+00:00"


class FakeAdapter:
    name = "fake_ocr"

    def fingerprint(self) -> dict[str, object]:
        runtime = {"implementation": "test-only"}
        return {
            "name": self.name,
            "version": "0.1",
            "fingerprint_sha256": contract.sha256_json(runtime),
            "runtime": runtime,
        }


class OCRPoCTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aiec-ocr-poc-")
        self.repository = Path(self.temporary.name)
        self.image_dir = self.repository / "artifacts" / "images"
        self.image_dir.mkdir(parents=True)
        self.image_path = self.image_dir / "fixture.png"
        image = Image.new("RGB", (200, 100), "white")
        image.save(self.image_path)
        image_bytes = self.image_path.read_bytes()
        self.observation = {
            "asset_id": "asset_" + "1" * 32,
            "asset": {
                "materialized_path": str(self.image_path),
                "sha256": hashlib.sha256(image_bytes).hexdigest(),
                "mime_type": "image/png",
                "dimensions": {"width_px": 200, "height_px": 100},
            },
            "source": {
                "relative_path": "opaque/document.pdf",
                "sha256": "2" * 64,
            },
            "origin": {"kind": "pdf_page", "page_number": 3},
        }
        self.selection = {
            "asset_id": self.observation["asset_id"],
            "crop": {
                "bbox": [100, 200, 600, 500],
                "purpose": "printed_line",
                "writing_mode": "horizontal",
            },
            "strata": {
                "document_family": "scan_pdf",
                "difficulty": "medium",
                "routes": ["ocr_text"],
            },
            "reference": {
                "status": "verified",
                "raw_text": "会議は10時に開始",
                "important_spans": ["10時"],
                "verification_method": "human_visual_transcription",
                "reviewer_count": 1,
                "notes": ["opaque fixture"],
            },
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def fixture(self) -> dict[str, object]:
        original_root = builder.ROOT
        try:
            builder.ROOT = self.repository
            return builder.build_fixture(
                self.observation, self.selection, created_at=STAMP
            )
        finally:
            builder.ROOT = original_root

    def test_fixture_is_closed_hashed_and_bound_to_image(self) -> None:
        fixture = self.fixture()
        self.assertEqual([], contract.validate_fixture(fixture, repository_root=self.repository))
        self.assertTrue(fixture["fixture_id"].startswith("ocrfx_"))
        changed = copy.deepcopy(fixture)
        changed["crop"]["purpose"] = "chart_label"
        errors = contract.validate_fixture(changed, repository_root=self.repository)
        self.assertTrue(any("signature" in error for error in errors))
        unknown = copy.deepcopy(fixture)
        unknown["asset_ref"]["question_id"] = "q1"
        errors = contract.validate_fixture(unknown)
        self.assertTrue(any("question_id" in error for error in errors))

    def test_pending_fixture_cannot_carry_reference_text(self) -> None:
        fixture = self.fixture()
        fixture["reference"] = {
            "status": "pending",
            "raw_text": "not allowed",
            "important_spans": [],
            "verification_method": "pending_human_review",
            "reviewer_count": 0,
            "notes": [],
        }
        signature = contract.expected_fixture_signature(fixture)
        fixture["hashes"]["signature_sha256"] = signature
        fixture["fixture_id"] = contract.expected_fixture_id(signature)
        errors = contract.validate_fixture(fixture)
        self.assertTrue(any("raw_text" in error for error in errors))

    def test_important_span_must_exist_in_verified_reference(self) -> None:
        fixture = self.fixture()
        fixture["reference"]["important_spans"] = ["存在しない"]
        signature = contract.expected_fixture_signature(fixture)
        fixture["hashes"]["signature_sha256"] = signature
        fixture["fixture_id"] = contract.expected_fixture_id(signature)
        errors = contract.validate_fixture(fixture)
        self.assertTrue(any("span is not in raw_text" in error for error in errors))

    def test_crop_input_uses_normalized_bbox(self) -> None:
        fixture = self.fixture()
        value = adapters.crop_input(fixture, self.repository)
        self.assertEqual((120, 50), (value.width_px, value.height_px))
        with Image.open(io.BytesIO(value.image_bytes)) as image:
            self.assertEqual((120, 50), image.size)

    def test_metrics_preserve_raw_text_and_count_edits(self) -> None:
        exact = contract.fixture_metrics("ABC 123", "ABC 123", ["123"])
        self.assertTrue(exact["exact_match"])
        self.assertEqual(0, exact["edit_distance"])
        changed = contract.fixture_metrics("ABC", "ADCX", ["ABC"])
        self.assertEqual(2, changed["edit_distance"])
        self.assertEqual(1, changed["substitutions"])
        self.assertEqual(1, changed["insertions"])
        self.assertEqual(0.0, changed["important_span_recall"])
        spaced = contract.fixture_metrics("BMI\nfloat64", "BMI     float64", ["BMI"])
        self.assertGreater(spaced["cer"], 0)
        self.assertEqual(0.0, spaced["whitespace_collapsed_cer"])
        self.assertTrue(spaced["whitespace_collapsed_exact_match"])
        with self.assertRaisesRegex(ValueError, "non-whitespace"):
            contract.fixture_metrics("   ", "", [])
        with self.assertRaisesRegex(ValueError, "controlled limit"):
            contract.levenshtein_counts("A" * 3000, "B" * 3000)

    def test_generated_run_is_stable_across_timing(self) -> None:
        fixture = self.fixture()
        value = adapters.crop_input(fixture, self.repository)
        result = adapters.AdapterResult(
            status="completed",
            lines=[{
                "line_id": "line_1",
                "sequence": 1,
                "raw_text": "会議は10時に開始",
                "bbox": [10, 10, 900, 200],
                "confidence": 0.9,
            }],
            warnings=[],
            error=None,
            setup_ms=1.0,
            inference_ms=2.0,
        )
        first = runner.make_run(fixture, FakeAdapter(), value, result)
        second_result = copy.deepcopy(result)
        object.__setattr__(second_result, "inference_ms", 99.0)
        second = runner.make_run(fixture, FakeAdapter(), value, second_result)
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertNotEqual(
            first["hashes"]["record_sha256"],
            second["hashes"]["record_sha256"],
        )
        self.assertEqual([], contract.validate_run(first))
        tampered = copy.deepcopy(first)
        tampered["timing"]["inference_ms"] = 999999.0
        self.assertTrue(
            any("record_sha256" in error for error in contract.validate_run(tampered))
        )

    def test_evaluation_requires_complete_engine_matrix(self) -> None:
        fixture = self.fixture()
        value = adapters.crop_input(fixture, self.repository)
        result = adapters.AdapterResult(
            status="completed",
            lines=[{
                "line_id": "line_1",
                "sequence": 1,
                "raw_text": "会議は10時に開始",
                "bbox": [10, 10, 900, 200],
                "confidence": None,
            }],
            warnings=[],
            error=None,
            setup_ms=0.0,
            inference_ms=3.0,
        )
        run = runner.make_run(fixture, FakeAdapter(), value, result)
        fixture_path = self.repository / "fixtures.jsonl"
        run_path = self.repository / "runs.jsonl"
        contract.write_jsonl(fixture_path, [fixture])
        contract.write_jsonl(run_path, [run])
        expected = {"fake_ocr": run["engine"]["fingerprint_sha256"]}
        report = evaluator.build_report(
            [fixture], [run], fixture_path, run_path, expected
        )
        overall = report["engines"]["fake_ocr"]["overall"]
        self.assertEqual(1, overall["exact_match_count"])
        self.assertEqual(0.0, overall["micro_cer"])
        self.assertEqual(
            {
                "diagnostic_fixture_selection",
                "no_handwriting_photo_vertical",
                "region_text_only",
                "raw_and_collapsed_metrics",
            },
            {item["code"] for item in report["limitations"]},
        )
        duplicate = copy.deepcopy(run)
        with self.assertRaisesRegex(ValueError, "duplicate fixture/engine"):
            evaluator.build_report(
                [fixture], [run, duplicate], fixture_path, run_path, expected
            )
        with self.assertRaisesRegex(ValueError, "expected engine is missing"):
            evaluator.build_report(
                [fixture],
                [run],
                fixture_path,
                run_path,
                {"fake_ocr": expected["fake_ocr"], "missing_engine": "a" * 64},
            )

    def test_merge_runs_is_stable_and_rejects_duplicate_pairs(self) -> None:
        fixture = self.fixture()
        value = adapters.crop_input(fixture, self.repository)
        result = adapters.AdapterResult(
            status="completed",
            lines=[{
                "line_id": "line_1",
                "sequence": 1,
                "raw_text": "会議は10時に開始",
                "bbox": [10, 10, 900, 200],
                "confidence": None,
            }],
            warnings=[],
            error=None,
            setup_ms=0.0,
            inference_ms=3.0,
        )
        first = runner.make_run(fixture, FakeAdapter(), value, result)
        first_path = self.repository / "first.jsonl"
        contract.write_jsonl(first_path, [first])
        self.assertEqual([first], merger.merge_runs([first_path]))
        with self.assertRaisesRegex(ValueError, "duplicate fixture/engine"):
            merger.merge_runs([first_path, first_path])

    def test_engine_identity_drift_is_rejected(self) -> None:
        fixture = self.fixture()
        value = adapters.crop_input(fixture, self.repository)
        result = adapters.AdapterResult(
            status="completed",
            lines=[{
                "line_id": "line_1",
                "sequence": 1,
                "raw_text": "text",
                "bbox": [1, 1, 10, 10],
                "confidence": None,
            }],
            warnings=[],
            error=None,
            setup_ms=0.0,
            inference_ms=1.0,
        )
        first = runner.make_run(fixture, FakeAdapter(), value, result)
        changed = copy.deepcopy(first)
        changed["engine"]["version"] = "0.2"
        changed["engine"]["fingerprint_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "engine identity drift"):
            contract.consistent_engine_identities([first, changed])

    def test_jsonl_loader_rejects_duplicate_keys_and_blank_lines(self) -> None:
        duplicate = self.repository / "duplicate.jsonl"
        duplicate.write_text('{"a":1,"a":2}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            contract.load_jsonl(duplicate)
        blank = self.repository / "blank.jsonl"
        blank.write_text("{}\n\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "blank JSONL line"):
            contract.load_jsonl(blank)


if __name__ == "__main__":
    unittest.main()
