from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "scripts"
sys.path.insert(0, str(SCRIPTS))

import classify_visual_assets
import validate_visual_classifications


MODEL = {
    "requested": "gemma4:12b",
    "resolved": "gemma4:12b",
    "digest": "a" * 64,
}


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def png_bytes(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + (b"\x20\x40\x60" * width)
    pixels = row * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def png_skeleton_bytes(width: int, height: int) -> bytes:
    """Return a structurally framed PNG without enough decoded pixel data."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b""))
        + chunk(b"IEND", b"")
    )


class VisualClassifierTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aiec-visual-classifier-")
        self.work = Path(self.temporary.name)
        self.image = self.work / "asset.png"
        self.image.write_bytes(png_bytes(100, 80))
        self.asset_sha = hashlib.sha256(self.image.read_bytes()).hexdigest()
        self.input_path = self.work / "assets.jsonl"
        write_jsonl(self.input_path, [{
            "asset_id": "asset_001",
            "materialized_path": self.image.name,
            "sha256": self.asset_sha,
            "mime_type": "image/png",
            "dimensions": {"width_px": 100, "height_px": 80},
            "source": {"path": "scan.pdf"},
            "origin": {"page": 3},
        }])
        self.output_path = self.work / "classifications.jsonl"
        self.cache_dir = self.work / "cache"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, response: dict[str, object]) -> tuple[dict[str, int], mock.Mock]:
        request = mock.Mock(return_value={
            "message": {"content": "```json\n" + json.dumps(response) + "\n```"}
        })
        with (
            mock.patch.object(classify_visual_assets, "model_info", return_value=MODEL),
            mock.patch.object(classify_visual_assets, "request_json", request),
        ):
            stats = classify_visual_assets.classify_file(
                self.input_path,
                self.output_path,
                cache_dir=self.cache_dir,
                timeout=5,
            )
        return stats, request

    def test_request_is_fixed_and_output_is_normalized(self) -> None:
        stats, request = self._run({
            "primary_type": "chart",
            "content_types": ["chart", "text_document"],
            "information_role": "primary",
            "regions": [
                {"region_id": "plot", "bbox": [50, 100, 800, 700], "types": ["chart"]},
                {
                    "region_id": "caption",
                    "bbox": [0.05, 0.82, 0.9, 0.1],
                    "types": ["text_document"],
                },
            ],
            "warnings": [],
        })
        self.assertEqual(stats["classified"], 1)
        self.assertEqual(request.call_count, 1)
        base_url, endpoint, payload, timeout = request.call_args.args
        self.assertEqual(base_url, "http://127.0.0.1:11434")
        self.assertEqual(endpoint, "/api/chat")
        self.assertEqual(timeout, 5)
        self.assertIs(payload["think"], False)
        self.assertEqual(payload["format"], "json")
        self.assertEqual(payload["options"]["temperature"], 0)
        self.assertEqual(payload["options"]["seed"], 42)
        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["messages"][0]["content"], classify_visual_assets.CLASSIFICATION_PROMPT)
        self.assertNotIn("scan.pdf", payload["messages"][0]["content"])
        self.assertNotIn("asset_001", payload["messages"][0]["content"])
        self.assertTrue(payload["messages"][0]["images"])

        record = json.loads(self.output_path.read_text(encoding="utf-8"))
        self.assertEqual(record["content_types"], ["chart", "text_document"])
        self.assertEqual(record["exactness"], "estimated")
        self.assertEqual(record["warnings"], [])
        self.assertIn("chart_source_recovery", record["routes"])
        self.assertIn("ocr_text", record["routes"])
        self.assertAlmostEqual(record["regions"][0]["bbox"][0], 0.05)
        self.assertTrue(record["prompt"]["question_independent"])
        self.assertEqual(validate_visual_classifications.validate(record), [])

    def test_second_run_reuses_signature_cache(self) -> None:
        response = {
            "primary_type": "table",
            "content_types": ["table"],
            "information_role": "primary",
            "regions": [{"region_id": "r1", "bbox": [0, 0, 1, 1], "types": ["table"]}],
            "warnings": [],
        }
        first_stats, first_request = self._run(response)
        self.assertEqual(first_stats["classified"], 1)
        self.assertEqual(first_request.call_count, 1)
        first_record = json.loads(self.output_path.read_text(encoding="utf-8"))
        self.assertFalse(first_record["provenance"]["cache_hit"])
        self.assertEqual(
            first_record["provenance"]["generated_at"],
            first_record["provenance"]["inference_generated_at"],
        )
        second_stats, second_request = self._run(response)
        self.assertEqual(second_stats["cached"], 1)
        self.assertEqual(second_request.call_count, 0)
        second_record = json.loads(self.output_path.read_text(encoding="utf-8"))
        self.assertTrue(second_record["provenance"]["cache_hit"])
        self.assertEqual(
            second_record["provenance"]["inference_generated_at"],
            first_record["provenance"]["inference_generated_at"],
        )
        self.assertGreaterEqual(
            second_record["provenance"]["generated_at"],
            second_record["provenance"]["inference_generated_at"],
        )

        third_stats, third_request = self._run(response)
        self.assertEqual(third_stats["cached"], 1)
        self.assertEqual(third_request.call_count, 0)
        third_record = json.loads(self.output_path.read_text(encoding="utf-8"))
        self.assertEqual(
            third_record["provenance"]["inference_generated_at"],
            first_record["provenance"]["inference_generated_at"],
        )

    def test_cache_is_not_used_after_image_is_deleted_or_changed(self) -> None:
        response = {
            "primary_type": "table",
            "content_types": ["table"],
            "information_role": "primary",
            "regions": [],
            "warnings": [],
        }
        first, first_request = self._run(response)
        self.assertEqual(first["classified"], 1)
        self.assertEqual(first_request.call_count, 1)

        self.image.unlink()
        deleted, deleted_request = self._run(response)
        self.assertEqual(deleted["failed"], 1)
        self.assertEqual(deleted["cached"], 0)
        self.assertEqual(deleted_request.call_count, 0)

        self.image.write_bytes(b"changed-after-cache")
        changed, changed_request = self._run(response)
        self.assertEqual(changed["failed"], 1)
        self.assertEqual(changed["cached"], 0)
        self.assertEqual(changed_request.call_count, 0)

    def test_cached_classification_is_rewrapped_with_current_envelope(self) -> None:
        response = {
            "primary_type": "photo",
            "content_types": ["photo"],
            "information_role": "supporting",
            "regions": [],
            "warnings": [],
        }
        first, request = self._run(response)
        self.assertEqual(first["classified"], 1)
        self.assertEqual(request.call_count, 1)

        relocated = self.work / "relocated.png"
        relocated.write_bytes(self.image.read_bytes())
        changed_input = {
            "asset_id": "asset_001",
            "materialized_path": relocated.name,
            "sha256": self.asset_sha,
            "mime_type": "image/png",
            "dimensions": {"width_px": 100, "height_px": 80},
            "source": {"path": "new-source.pdf", "sha256": "c" * 64},
            "origin": {"page": 9},
        }
        write_jsonl(self.input_path, [changed_input])
        second, second_request = self._run(response)
        self.assertEqual(second["cached"], 1)
        self.assertEqual(second_request.call_count, 0)
        current = json.loads(self.output_path.read_text(encoding="utf-8"))
        self.assertEqual(current["asset"]["materialized_path"], relocated.name)
        self.assertEqual(current["source"], changed_input["source"])
        self.assertEqual(current["origin"], changed_input["origin"])
        normalized = classify_visual_assets.normalize_asset(changed_input, self.work)
        self.assertEqual(current["hashes"]["input_sha256"], normalized["input_sha256"])

    def test_invalid_cached_envelope_forces_model_request(self) -> None:
        response = {
            "primary_type": "illustration",
            "content_types": ["illustration"],
            "information_role": "primary",
            "regions": [],
            "warnings": [],
        }
        self._run(response)
        paths = [self.output_path, *self.cache_dir.glob("*.json")]
        self.assertEqual(len(paths), 2)
        for path in paths:
            record = json.loads(path.read_text(encoding="utf-8"))
            record["warnings"] = ["tampered cache"]
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        stats, request = self._run(response)
        self.assertEqual(stats["classified"], 1)
        self.assertEqual(stats["cached"], 0)
        self.assertEqual(request.call_count, 1)

    def test_legacy_cache_without_inference_provenance_is_not_reused(self) -> None:
        response = {
            "primary_type": "photo",
            "content_types": ["photo"],
            "information_role": "primary",
            "regions": [],
            "warnings": [],
        }
        self._run(response)
        paths = [self.output_path, *self.cache_dir.glob("*.json")]
        self.assertEqual(len(paths), 2)
        for path in paths:
            record = json.loads(path.read_text(encoding="utf-8"))
            record["provenance"].pop("cache_hit")
            record["provenance"].pop("inference_generated_at")
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")

        stats, request = self._run(response)
        self.assertEqual(stats["classified"], 1)
        self.assertEqual(stats["cached"], 0)
        self.assertEqual(request.call_count, 1)

    def test_materialized_path_must_stay_inside_asset_root(self) -> None:
        base = json.loads(self.input_path.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="aiec-visual-outside-") as temporary:
            outside = Path(temporary) / "outside.png"
            outside.write_bytes(self.image.read_bytes())
            cases = [str(outside), os.path.relpath(outside, self.work)]
            link = self.work / "outside-link.png"
            try:
                link.symlink_to(outside)
            except OSError:
                pass
            else:
                cases.append(link.name)
            for raw_path in cases:
                with self.subTest(raw_path=raw_path):
                    record = copy.deepcopy(base)
                    record["materialized_path"] = raw_path
                    with self.assertRaisesRegex(ValueError, "inside asset_root"):
                        classify_visual_assets.normalize_asset(record, self.work)

    def test_volatile_materialization_metadata_does_not_change_signature(self) -> None:
        base_record = {
            "asset_id": "asset_stable_signature",
            "materialization": {
                "output_path": self.image.name,
                "sha256": self.asset_sha,
                "mime_type": "image/png",
                "width_px": 100,
                "height_px": 80,
                "generated_at": "2026-08-15T00:00:00+00:00",
                "cache_hit": False,
            },
            "source": {"relative_path": "scan.pdf", "sha256": "b" * 64},
            "origin": {"kind": "pdf_page", "page_number": 3},
        }
        write_jsonl(self.input_path, [base_record])
        response = {
            "primary_type": "diagram",
            "content_types": ["diagram", "illustration"],
            "information_role": "primary",
            "regions": [],
            "warnings": [],
        }
        first_stats, first_request = self._run(response)
        first_record = json.loads(self.output_path.read_text(encoding="utf-8"))
        self.assertEqual(first_stats["classified"], 1)
        self.assertEqual(first_request.call_count, 1)

        changed_record = copy.deepcopy(base_record)
        changed_record["materialization"]["generated_at"] = "2026-08-16T12:34:56+00:00"
        changed_record["materialization"]["cache_hit"] = True
        write_jsonl(self.input_path, [changed_record])
        second_stats, second_request = self._run(response)
        second_record = json.loads(self.output_path.read_text(encoding="utf-8"))
        self.assertEqual(second_stats["cached"], 1)
        self.assertEqual(second_request.call_count, 0)
        self.assertEqual(
            first_record["hashes"]["input_sha256"],
            second_record["hashes"]["input_sha256"],
        )
        self.assertEqual(
            first_record["hashes"]["signature_sha256"],
            second_record["hashes"]["signature_sha256"],
        )

    def test_canonical_nested_materialization_is_accepted(self) -> None:
        write_jsonl(self.input_path, [{
            "asset_id": "asset_nested",
            "materialization": {
                "output_path": self.image.name,
                "sha256": self.asset_sha,
                "mime_type": "image/png",
                "width_px": 100,
                "height_px": 80,
            },
            "source": {"relative_path": "scan.pdf", "sha256": "b" * 64},
            "origin": {"kind": "pdf_page", "page": 3},
        }])
        stats, request = self._run({
            "primary_type": "text_document",
            "content_types": ["text_document"],
            "information_role": "primary",
            "regions": [],
            "warnings": [],
        })
        self.assertEqual(stats["classified"], 1)
        self.assertEqual(request.call_count, 1)
        record = json.loads(self.output_path.read_text(encoding="utf-8"))
        self.assertEqual(record["asset_id"], "asset_nested")
        self.assertEqual(record["asset"]["sha256"], self.asset_sha)
        self.assertEqual(record["source"]["relative_path"], "scan.pdf")

    def test_malformed_model_fields_are_normalized_but_require_review(self) -> None:
        stats, request = self._run({
            "primary_type": "Graph",
            "content_types": "chart",
            "information_role": "primary",
            "regions": [{
                "region_id": "r1",
                "bbox": {"x": 0, "y": 0, "width": 1, "height": 1},
                "types": ["chart", "alien_label"],
                "extra": True,
            }],
            "warnings": "not-an-array",
            "exactness": "exact",
        })
        self.assertEqual(request.call_count, 1)
        self.assertEqual(stats["needs_review"], 1)
        record = json.loads(self.output_path.read_text(encoding="utf-8"))
        self.assertEqual(record["primary_type"], "chart")
        self.assertEqual(record["content_types"], ["chart"])
        self.assertEqual(record["exactness"], "estimated")
        self.assertIn("review", record["routes"])
        self.assertTrue(any("unexpected fields" in item for item in record["warnings"]))
        self.assertTrue(any("unknown label" in item for item in record["warnings"]))
        self.assertTrue(any("coerced" in item for item in record["warnings"]))
        self.assertEqual(validate_visual_classifications.validate(record), [])

    def test_failed_request_is_persisted_and_retried(self) -> None:
        with (
            mock.patch.object(classify_visual_assets, "model_info", return_value=MODEL),
            mock.patch.object(
                classify_visual_assets,
                "request_json",
                side_effect=RuntimeError("local test failure"),
            ),
        ):
            first = classify_visual_assets.classify_file(
                self.input_path, self.output_path, cache_dir=self.cache_dir, timeout=5
            )
        self.assertEqual(first["failed"], 1)
        failed_record = json.loads(self.output_path.read_text(encoding="utf-8"))
        self.assertEqual(failed_record["primary_type"], "unknown")
        self.assertEqual(validate_visual_classifications.validate(failed_record), [])

        response = {
            "primary_type": "decoration",
            "content_types": ["decoration"],
            "information_role": "decorative",
            "regions": [],
            "warnings": [],
        }
        second, request = self._run(response)
        self.assertEqual(second["classified"], 1)
        self.assertEqual(second["cached"], 0)
        self.assertEqual(request.call_count, 1)

    def test_validator_rejects_bbox_routes_exactness_and_hash_tampering(self) -> None:
        self._run({
            "primary_type": "table",
            "content_types": ["table"],
            "information_role": "primary",
            "regions": [{"region_id": "r1", "bbox": [0, 0, 1, 1], "types": ["table"]}],
            "warnings": [],
        })
        valid = json.loads(self.output_path.read_text(encoding="utf-8"))

        malformed = copy.deepcopy(valid)
        malformed["regions"][0]["bbox"] = [0.8, 0.8, 0.5, 0.5]
        malformed["routes"] = ["image_description"]
        malformed["exactness"] = "exact"
        malformed["hashes"]["output_sha256"] = "0" * 64
        errors = validate_visual_classifications.validate(malformed)
        self.assertTrue(any("normalized image bounds" in error for error in errors))
        self.assertTrue(any("table requires route table_structure" in error for error in errors))
        self.assertTrue(any("must never have exact" in error for error in errors))
        self.assertTrue(any("output_sha256" in error for error in errors))

    def test_validator_enforces_status_in_both_directions(self) -> None:
        self._run({
            "primary_type": "chart",
            "content_types": ["chart"],
            "information_role": "primary",
            "regions": [],
            "warnings": [],
        })
        valid = json.loads(self.output_path.read_text(encoding="utf-8"))

        warning_but_classified = copy.deepcopy(valid)
        warning_but_classified["warnings"] = ["requires review"]
        warning_but_classified["routes"].append("review")
        warning_but_classified["hashes"]["output_sha256"] = classify_visual_assets.sha256_json(
            classify_visual_assets.classification_payload(warning_but_classified)
        )
        errors = validate_visual_classifications.validate(warning_but_classified)
        self.assertTrue(any("classified status requires" in error for error in errors))

        clean_but_review = copy.deepcopy(valid)
        clean_but_review["status"] = "needs_review"
        clean_but_review["routes"].append("review")
        clean_but_review["hashes"]["output_sha256"] = classify_visual_assets.sha256_json(
            classify_visual_assets.classification_payload(clean_but_review)
        )
        errors = validate_visual_classifications.validate(clean_but_review)
        self.assertTrue(any("must have classified status" in error for error in errors))

    def test_batch_validator_checks_assets_hashes_and_envelope(self) -> None:
        self._run({
            "primary_type": "table",
            "content_types": ["table"],
            "information_role": "primary",
            "regions": [],
            "warnings": [],
        })
        stats = validate_visual_classifications.validate_jsonl(
            self.output_path, self.input_path, asset_root=self.work
        )
        self.assertEqual(stats["records"], 1)
        self.assertIn(
            stats["schema_validation"],
            {"strict_manual_fallback", "jsonschema_draft202012_format"},
        )
        valid = json.loads(self.output_path.read_text(encoding="utf-8"))

        mutations = {
            "source": lambda record: record.__setitem__("source", {"path": "stale.pdf"}),
            "origin": lambda record: record.__setitem__("origin", {"page": 999}),
            "asset": lambda record: record["asset"].__setitem__("materialized_path", "stale.png"),
            "input_hash": lambda record: record["hashes"].__setitem__("input_sha256", "0" * 64),
            "signature": lambda record: record["hashes"].__setitem__("signature_sha256", "0" * 64),
            "classification_id": lambda record: record.__setitem__("classification_id", "vc_" + "0" * 24),
            "output_hash": lambda record: record["hashes"].__setitem__("output_sha256", "0" * 64),
            "model": lambda record: record["model"].__setitem__("digest", "b" * 64),
            "model_name": lambda record: record["model"].__setitem__("requested", "other:1b"),
            "prompt": lambda record: record["prompt"].__setitem__("version", "stale-prompt"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                malformed = copy.deepcopy(valid)
                mutate(malformed)
                write_jsonl(self.output_path, [malformed])
                with self.assertRaises(ValueError):
                    validate_visual_classifications.validate_jsonl(
                        self.output_path, self.input_path, asset_root=self.work
                    )
        write_jsonl(self.output_path, [valid])

        wrong_assets = json.loads(self.input_path.read_text(encoding="utf-8"))
        wrong_assets["dimensions"] = {"width_px": 101, "height_px": 80}
        wrong_assets_path = self.work / "wrong-assets.jsonl"
        write_jsonl(wrong_assets_path, [wrong_assets])
        with self.assertRaisesRegex(ValueError, "dimensions mismatch"):
            validate_visual_classifications.validate_jsonl(
                self.output_path, wrong_assets_path, asset_root=self.work
            )

    def test_model_digest_requires_string_and_signature_is_type_safe(self) -> None:
        numeric_digest = 1111111111111111
        normalized = classify_visual_assets.normalize_asset(
            json.loads(self.input_path.read_text(encoding="utf-8")), self.work
        )
        with self.assertRaisesRegex(ValueError, "model digest"):
            classify_visual_assets.signature_for_asset(normalized, numeric_digest)  # type: ignore[arg-type]

        request = mock.Mock()
        bad_model = {**MODEL, "digest": numeric_digest}
        with (
            mock.patch.object(classify_visual_assets, "model_info", return_value=bad_model),
            mock.patch.object(classify_visual_assets, "request_json", request),
        ):
            with self.assertRaisesRegex(ValueError, "model.digest"):
                classify_visual_assets.classify_file(
                    self.input_path,
                    self.output_path,
                    cache_dir=self.cache_dir,
                    timeout=5,
                )
        request.assert_not_called()

        self._run({
            "primary_type": "chart",
            "content_types": ["chart"],
            "information_role": "primary",
            "regions": [],
            "warnings": [],
        })
        malformed = json.loads(self.output_path.read_text(encoding="utf-8"))
        malformed["model"]["digest"] = numeric_digest
        errors = validate_visual_classifications.validate(malformed)
        self.assertTrue(any("model fields" in error for error in errors))
        write_jsonl(self.output_path, [malformed])
        with self.assertRaisesRegex(ValueError, "model digest cannot be verified"):
            validate_visual_classifications.validate_jsonl(
                self.output_path, self.input_path, asset_root=self.work
            )

    def test_asset_bound_decoder_rejects_large_and_truncated_images(self) -> None:
        oversized = png_skeleton_bytes(10_001, 5_000)
        with self.assertRaisesRegex(ValueError, "50000000 pixel safety limit"):
            validate_visual_classifications._actual_image_metadata(
                oversized, self.work / "oversized.png"
            )

        from PIL import Image

        jpeg = io.BytesIO()
        Image.new("RGB", (4, 3), color=(20, 40, 60)).save(jpeg, format="JPEG")
        with self.assertRaisesRegex(ValueError, "terminal EOI marker"):
            validate_visual_classifications._actual_image_metadata(
                jpeg.getvalue()[:-2], self.work / "truncated.jpg"
            )

        with mock.patch.object(Image.Image, "load", side_effect=OSError("full-load-marker")):
            with self.assertRaisesRegex(ValueError, "full-load-marker"):
                validate_visual_classifications._actual_image_metadata(
                    self.image.read_bytes(), self.image
                )

    def test_schema_engine_state_is_explicit_in_manual_fallback(self) -> None:
        schema = validate_visual_classifications._load_published_schema()
        with mock.patch.dict(sys.modules, {"jsonschema": None}):
            validator, state = validate_visual_classifications._compile_published_schema(schema)
        self.assertIsNone(validator)
        self.assertEqual(state, "strict_manual_fallback")

        self._run({
            "primary_type": "text_document",
            "content_types": ["text_document"],
            "information_role": "primary",
            "regions": [],
            "warnings": [],
        })
        with mock.patch.object(
            validate_visual_classifications,
            "_compile_published_schema",
            return_value=(None, "strict_manual_fallback"),
        ):
            stats = validate_visual_classifications.validate_jsonl(
                self.output_path, self.input_path, asset_root=self.work
            )
        self.assertEqual(stats["schema_validation"], "strict_manual_fallback")

    def test_batch_validator_rejects_empty_count_and_order_mismatch(self) -> None:
        response = {
            "primary_type": "text_document",
            "content_types": ["text_document"],
            "information_role": "primary",
            "regions": [],
            "warnings": [],
        }
        self._run(response)
        valid_line = self.output_path.read_text(encoding="utf-8")
        empty = self.work / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "contains no records"):
            validate_visual_classifications.validate_jsonl(
                empty, self.input_path, asset_root=self.work
            )
        with self.assertRaisesRegex(ValueError, "contains no records"):
            validate_visual_classifications.validate_jsonl(
                self.output_path, empty, asset_root=self.work
            )

        first_asset = json.loads(self.input_path.read_text(encoding="utf-8"))
        second_asset = copy.deepcopy(first_asset)
        second_asset["asset_id"] = "asset_002"
        second_asset["materialized_path"] = "asset-2.png"
        (self.work / "asset-2.png").write_bytes(self.image.read_bytes())
        write_jsonl(self.input_path, [first_asset, second_asset])
        with self.assertRaisesRegex(ValueError, "record count mismatch"):
            validate_visual_classifications.validate_jsonl(
                self.output_path, self.input_path, asset_root=self.work
            )

        stats, request = self._run(response)
        self.assertEqual(stats["classified"], 2)
        self.assertEqual(request.call_count, 1)
        records = [json.loads(line) for line in self.output_path.read_text().splitlines()]
        write_jsonl(self.output_path, list(reversed(records)))
        with self.assertRaisesRegex(ValueError, "asset_id/order mismatch"):
            validate_visual_classifications.validate_jsonl(
                self.output_path, self.input_path, asset_root=self.work
            )
        self.output_path.write_text(valid_line, encoding="utf-8")

    def test_robust_json_parser_accepts_surrounding_text_and_nested_value(self) -> None:
        parsed = classify_visual_assets.parse_model_json({
            "message": {"content": "prefix {\"classification\": {\"primary_type\": \"photo\"}} suffix"}
        })
        self.assertEqual(parsed, {"primary_type": "photo"})


if __name__ == "__main__":
    unittest.main()
