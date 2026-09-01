from __future__ import annotations

import inspect
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import local_image_ocr as reader  # noqa: E402
import build_search_units as search_units  # noqa: E402
import probe_intermediate_records as probe_records  # noqa: E402


def png_header(width: int = 80, height: int = 40) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


def located_line(
    text: str = "作業報告",
    bbox: list[int] | None = None,
    confidence: float = 0.95,
) -> dict[str, object]:
    return {
        "line_id": "line-000001",
        "sequence": 1,
        "raw_text": text,
        "bbox": bbox or [100, 100, 400, 80],
        "confidence": confidence,
    }


def completed(lines: list[dict[str, object]]):
    return "completed", lines, [], None


def vision_completed(
    lines: list[dict[str, object]],
    *,
    orientation: int = 1,
):
    return "completed", lines, [], None, orientation


def unlocated_transcript(text: str = "項目A\t24\n項目B\t完了") -> dict[str, object]:
    return {
        "text": text,
        "location_status": "unlocated",
        "quality_tier": "provisional",
        "provisional_marker": reader.PROVISIONAL_MARKER,
        "transcript_type": "whole_image_faithful_transcript",
        "question_independent": True,
        "model": "gemma4:12b",
        "model_digest": "a" * 64,
        "prompt_sha256": reader.UNLOCATED_TRANSCRIPT_PROMPT_SHA256,
        "runner": "ollama_loopback_chat",
        "host": "127.0.0.1",
        "temperature": 0,
        "num_predict": reader.MAX_UNLOCATED_TRANSCRIPT_TOKENS,
    }


class AdaptiveLocalImageOCRTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.image_path = Path(self.temporary.name) / "sample.png"
        self.image_path.write_bytes(png_header())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_stdlib_header_inspection_and_no_pillow_import(self) -> None:
        metadata = reader.inspect_image_bytes(png_header(321, 123))

        self.assertEqual(
            metadata["dimensions"], {"width_px": 321, "height_px": 123}
        )
        self.assertEqual(metadata["image_format"], "PNG")
        self.assertNotIn("from PIL", inspect.getsource(reader))

    def test_oversized_image_is_rejected_before_opening_the_file(self) -> None:
        with (
            mock.patch.object(type(self.image_path), "is_symlink", return_value=False),
            mock.patch.object(type(self.image_path), "stat") as path_stat,
            mock.patch.object(type(self.image_path), "open") as path_open,
        ):
            path_stat.return_value.st_mode = 0o100600
            path_stat.return_value.st_size = reader.MAX_IMAGE_BYTES + 1
            with self.assertRaisesRegex(ValueError, "safety limit"):
                reader.read_checked_image_bytes(self.image_path)
        path_open.assert_not_called()

    def test_vision_primary_and_tesseract_are_independent_agreement(self) -> None:
        apple = located_line(confidence=0.96)
        tesseract = located_line(bbox=[105, 102, 395, 78], confidence=0.88)

        with (
            mock.patch.object(
                reader.ocr, "resolve_vision_binary", return_value=Path("/vision")
            ),
            mock.patch.object(
                reader, "_run_vision_pass", return_value=vision_completed([apple])
            ) as vision_run,
            mock.patch.object(
                reader.ocr, "verify_tesseract", return_value=Path("/tesseract")
            ),
            mock.patch.object(
                reader, "_run_tesseract_psm", return_value=completed([tesseract])
            ) as tesseract_run,
            mock.patch.object(
                reader,
                "run_unlocated_transcript_fallback",
                side_effect=AssertionError("located OCR must not invoke the VLM fallback"),
            ) as fallback,
        ):
            result = reader.extract(self.image_path)

        self.assertEqual(result["agreement_counts"]["independent_agreement"], 1)
        self.assertEqual(
            result["consensus_lines"][0]["agreement_type"],
            "independent_agreement",
        )
        self.assertEqual(result["consensus_lines"][0]["quality_tier"], "high")
        self.assertNotIn("provisional_marker", result["consensus_lines"][0])
        self.assertEqual(
            result["consensus_lines"][0]["provenance"]["primary_pass"],
            "apple_vision_primary",
        )
        self.assertEqual(result["orientation"], 1)
        self.assertTrue(result["cross_engine_spatial_comparison"])
        self.assertEqual(
            result["consensus_lines"][0]["bbox_coordinate_system"],
            reader.ORIENTATION_1_COORDINATE_SYSTEM,
        )
        self.assertTrue(result["independent_engines"])
        self.assertEqual(vision_run.call_args.kwargs["pass_name"], "primary")
        self.assertEqual(tesseract_run.call_args.kwargs["psm"], 3)
        fallback.assert_not_called()
        self.assertFalse(result["external_network_used"])
        self.assertFalse(result["downloads_performed"])

    def test_retry_cannot_reuse_one_tesseract_line_as_two_independent_supports(self) -> None:
        primary_a = located_line(text="A", bbox=[100, 100, 200, 60])
        primary_a["line_id"] = "vision-primary-a"
        primary_b = located_line(text="B", bbox=[100, 300, 200, 60])
        primary_b["line_id"] = "vision-primary-b"
        retry_a = located_line(text="A", bbox=[110, 102, 195, 58])
        retry_a["line_id"] = "vision-retry-a"
        retry_b = located_line(text="B", bbox=[110, 302, 195, 58])
        retry_b["line_id"] = "vision-retry-b"
        tesseract_a = located_line(text="A", bbox=[105, 101, 198, 59])
        tesseract_a["line_id"] = "tesseract-a"

        def vision_result(*args, pass_name: str, **kwargs):
            return vision_completed(
                [primary_a, primary_b]
                if pass_name == "primary" else [retry_a, retry_b]
            )

        def tesseract_result(*args, psm: int, **kwargs):
            return completed([tesseract_a] if psm == 3 else [])

        with (
            mock.patch.object(
                reader.ocr, "resolve_vision_binary", return_value=Path("/vision")
            ),
            mock.patch.object(
                reader, "_run_vision_pass", side_effect=vision_result
            ),
            mock.patch.object(
                reader.ocr, "verify_tesseract", return_value=Path("/tesseract")
            ),
            mock.patch.object(
                reader, "_run_tesseract_psm", side_effect=tesseract_result
            ),
        ):
            result = reader.extract(self.image_path)

        self.assertEqual(result["agreement_counts"]["independent_agreement"], 1)
        self.assertEqual(
            [line["provenance"]["audit_line_id"] for line in result["consensus_lines"]],
            ["tesseract-a"],
        )
        self.assertEqual(
            result["agreement_counts"]["same_engine_agreement"], 1
        )

    def test_nearby_retry_pass_pairs_form_one_independent_consensus(self) -> None:
        primary_reading = located_line(
            text="同じ行", bbox=[100, 100, 240, 60]
        )
        primary_reading["line_id"] = "vision-primary-reading"
        unmatched_primary = located_line(
            text="未一致", bbox=[100, 400, 240, 60]
        )
        unmatched_primary["line_id"] = "vision-primary-unmatched"
        retry_reading = located_line(
            text="同じ行", bbox=[110, 105, 230, 56]
        )
        retry_reading["line_id"] = "vision-retry-reading"
        psm3_reading = located_line(
            text="同じ行", bbox=[104, 102, 236, 58]
        )
        psm3_reading["line_id"] = "tesseract-psm3-reading"
        psm6_reading = located_line(
            text="同じ行", bbox=[115, 108, 225, 54]
        )
        psm6_reading["line_id"] = "tesseract-psm6-reading"

        def vision_result(*args, pass_name: str, **kwargs):
            return vision_completed(
                [primary_reading, unmatched_primary]
                if pass_name == "primary"
                else [retry_reading]
            )

        def tesseract_result(*args, psm: int, **kwargs):
            return completed([psm3_reading] if psm == 3 else [psm6_reading])

        with (
            mock.patch.object(
                reader.ocr, "resolve_vision_binary", return_value=Path("/vision")
            ),
            mock.patch.object(
                reader, "_run_vision_pass", side_effect=vision_result
            ),
            mock.patch.object(
                reader.ocr, "verify_tesseract", return_value=Path("/tesseract")
            ),
            mock.patch.object(
                reader, "_run_tesseract_psm", side_effect=tesseract_result
            ),
        ):
            result = reader.extract(self.image_path)

        self.assertEqual(result["agreement_counts"]["independent_agreement"], 1)
        self.assertEqual(
            result["consensus_lines"][0]["provenance"]["primary_pass"],
            "apple_vision_primary",
        )
        self.assertEqual(
            result["consensus_lines"][0]["provenance"]["audit_pass"],
            "tesseract_psm3",
        )

    def test_distant_identical_text_remains_two_independent_consensus_lines(self) -> None:
        primary_reading = located_line(
            text="同じ行", bbox=[100, 100, 240, 60]
        )
        primary_reading["line_id"] = "vision-primary-near"
        unmatched_primary = located_line(
            text="未一致", bbox=[100, 400, 240, 60]
        )
        unmatched_primary["line_id"] = "vision-primary-unmatched"
        retry_reading = located_line(
            text="同じ行", bbox=[100, 700, 240, 60]
        )
        retry_reading["line_id"] = "vision-retry-far"
        psm3_reading = located_line(
            text="同じ行", bbox=[104, 102, 236, 58]
        )
        psm3_reading["line_id"] = "tesseract-psm3-near"
        psm6_reading = located_line(
            text="同じ行", bbox=[104, 702, 236, 58]
        )
        psm6_reading["line_id"] = "tesseract-psm6-far"

        def vision_result(*args, pass_name: str, **kwargs):
            return vision_completed(
                [primary_reading, unmatched_primary]
                if pass_name == "primary"
                else [retry_reading]
            )

        def tesseract_result(*args, psm: int, **kwargs):
            return completed([psm3_reading] if psm == 3 else [psm6_reading])

        with (
            mock.patch.object(
                reader.ocr, "resolve_vision_binary", return_value=Path("/vision")
            ),
            mock.patch.object(
                reader, "_run_vision_pass", side_effect=vision_result
            ),
            mock.patch.object(
                reader.ocr, "verify_tesseract", return_value=Path("/tesseract")
            ),
            mock.patch.object(
                reader, "_run_tesseract_psm", side_effect=tesseract_result
            ),
        ):
            result = reader.extract(self.image_path)

        self.assertEqual(result["agreement_counts"]["independent_agreement"], 2)
        self.assertEqual(
            [line["bbox"][1] for line in result["consensus_lines"]],
            [100, 700],
        )

    def test_no_located_reading_routes_to_unlocated_transcript_fallback(self) -> None:
        fallback_result = unlocated_transcript()
        with (
            mock.patch.object(
                reader.ocr,
                "resolve_vision_binary",
                side_effect=RuntimeError("Vision unavailable"),
            ),
            mock.patch.object(
                reader.ocr,
                "verify_tesseract",
                side_effect=FileNotFoundError("Tesseract unavailable"),
            ),
            mock.patch.object(
                reader,
                "run_unlocated_transcript_fallback",
                return_value=fallback_result,
            ) as fallback,
        ):
            result = reader.extract(self.image_path)

        fallback.assert_called_once()
        self.assertEqual(result["read_lines"], [])
        self.assertEqual(result["unlocated_transcript"], fallback_result)
        self.assertEqual(result["agreement_counts"]["unlocated_transcript"], 1)
        self.assertEqual(
            result["engines"]["gemma4_unlocated_transcript"]["status"],
            "completed",
        )
        self.assertFalse(result["external_network_used"])
        self.assertFalse(result["downloads_performed"])

    def test_unlocated_transcript_uses_installed_digest_and_strict_json(self) -> None:
        calls: list[tuple[str, str]] = []

        def local_response(method, path, *, payload, timeout):
            calls.append((method, path))
            if path == "/api/tags":
                self.assertIsNone(payload)
                return {
                    "models": [{
                        "name": "gemma4:12b",
                        "model": "gemma4:12b",
                        "digest": "a" * 64,
                    }]
                }
            self.assertEqual(payload["model"], "gemma4:12b")
            self.assertEqual(payload["format"], reader.UNLOCATED_TRANSCRIPT_SCHEMA)
            self.assertEqual(payload["options"]["temperature"], 0)
            self.assertEqual(
                payload["options"]["num_predict"],
                reader.MAX_UNLOCATED_TRANSCRIPT_TOKENS,
            )
            self.assertNotIn("tools", payload)
            self.assertEqual(len(payload["messages"][-1]["images"]), 1)
            return {
                "model": "gemma4:12b",
                "message": {
                    "content": json.dumps(
                        {"transcript": "作業報告\n合計 24時間"},
                        ensure_ascii=False,
                    )
                },
            }

        with mock.patch.object(reader, "_ollama_json", side_effect=local_response):
            result = reader.run_unlocated_transcript_fallback(
                png_header(), timeout=999
            )

        self.assertEqual(calls, [("GET", "/api/tags"), ("POST", "/api/chat")])
        self.assertEqual(result["model_digest"], "a" * 64)
        self.assertEqual(
            result["prompt_sha256"], reader.UNLOCATED_TRANSCRIPT_PROMPT_SHA256
        )
        self.assertEqual(result["location_status"], "unlocated")
        self.assertNotIn("bbox", result)

    def test_uninstalled_model_skips_without_chat_or_download(self) -> None:
        calls: list[tuple[str, str]] = []

        def no_model(method, path, *, payload, timeout):
            calls.append((method, path))
            return {"models": []}

        with mock.patch.object(reader, "_ollama_json", side_effect=no_model):
            with self.assertRaisesRegex(RuntimeError, "download is forbidden"):
                reader.run_unlocated_transcript_fallback(
                    png_header(), timeout=10
                )

        self.assertEqual(calls, [("GET", "/api/tags")])

    def test_probe_retains_unlocated_transcript_as_searchable_provisional_text(self) -> None:
        observation = {
            "input_sha256": "b" * 64,
            "dimensions": {"width_px": 80, "height_px": 40},
            "image_format": "PNG",
            "orientation": 1,
            "engines": {},
            "independent_engines": False,
            "consensus_lines": [],
            "read_lines": [],
            "unlocated_transcript": unlocated_transcript(
                "作業報告書\n総作業時間\t24時間"
            ),
            "unresolved_count": 1,
        }
        probe = probe_records.Probe(
            self.image_path.parent,
            "2026-09-01T00:00:00+00:00",
            None,
            diagnostic=False,
        )
        with mock.patch.object(reader, "extract", return_value=observation):
            probe.extract(self.image_path)

        image = next(
            item for item in probe.evidence if item["evidence_type"] == "image"
        )
        transcript = next(
            item
            for item in probe.evidence
            if item["evidence_type"] == "text_block"
        )
        self.assertEqual(transcript["parent_evidence_id"], image["evidence_id"])
        self.assertEqual(
            transcript["native_properties"]["location_status"], "unlocated"
        )
        self.assertEqual(
            transcript["native_properties"]["quality_tier"], "provisional"
        )
        self.assertTrue(
            transcript["content"]["raw_text"].startswith(
                f"{reader.PROVISIONAL_MARKER}\n"
            )
        )

        emitted: list[dict[str, object]] = []
        deriver = search_units.DocumentDeriver(
            probe.documents[0]["document_id"],
            "2026-09-01T00:00:00+00:00",
            emitted.append,
            500,
        )
        for record in probe.evidence:
            deriver.consume(record)
        self.assertEqual(deriver.finish(), {"text_chunk": 1})
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0]["unit_type"], "text_chunk")
        self.assertIn("総作業時間", emitted[0]["text"]["search_text"])
        self.assertIn(
            reader.PROVISIONAL_MARKER, emitted[0]["text"]["search_text"]
        )

    def test_long_unlocated_transcript_is_exactly_sharded_and_all_chunks_are_provisional(self) -> None:
        transcript_text = "先頭\n" + ("A" * 5000) + "\n末尾の質問根拠"
        observation = {
            "input_sha256": "b" * 64,
            "dimensions": {"width_px": 80, "height_px": 40},
            "image_format": "PNG",
            "orientation": 1,
            "engines": {},
            "independent_engines": False,
            "consensus_lines": [],
            "read_lines": [],
            "unlocated_transcript": unlocated_transcript(transcript_text),
            "unresolved_count": 1,
        }
        probe = probe_records.Probe(
            self.image_path.parent,
            "2026-09-01T00:00:00+00:00",
            None,
            diagnostic=False,
        )
        with mock.patch.object(reader, "extract", return_value=observation):
            probe.extract(self.image_path)

        chunks = [
            item for item in probe.evidence
            if item["evidence_type"] == "text_block"
        ]
        prefix = f"{reader.PROVISIONAL_MARKER}\n"
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(
            item["content"]["raw_text"].startswith(prefix) for item in chunks
        ))
        self.assertTrue(all(
            len(item["content"]["raw_text"])
            <= probe_records.MAX_QUESTION_EVIDENCE_CHARS
            for item in chunks
        ))
        self.assertEqual(
            "".join(item["content"]["raw_text"][len(prefix):] for item in chunks),
            transcript_text,
        )
        self.assertTrue(all(
            item["native_properties"]["transcript_chunk_count"] == len(chunks)
            for item in chunks
        ))

        emitted: list[dict[str, object]] = []
        deriver = search_units.DocumentDeriver(
            probe.documents[0]["document_id"],
            "2026-09-01T00:00:00+00:00",
            emitted.append,
            500,
        )
        for record in probe.evidence:
            deriver.consume(record)
        self.assertEqual(deriver.finish(), {"text_chunk": len(chunks)})
        self.assertEqual(len(emitted), len(chunks))
        self.assertIn("末尾の質問根拠", emitted[-1]["text"]["search_text"])
        self.assertTrue(all(
            reader.PROVISIONAL_MARKER in item["text"]["search_text"]
            for item in emitted
        ))

    def test_tesseract_unavailable_keeps_vision_reading_provisional(self) -> None:
        apple = located_line()

        def vision_result(*args, pass_name: str, **kwargs):
            return vision_completed([apple] if pass_name == "primary" else [])

        with (
            mock.patch.object(
                reader.ocr, "resolve_vision_binary", return_value=Path("/vision")
            ),
            mock.patch.object(
                reader, "_run_vision_pass", side_effect=vision_result
            ),
            mock.patch.object(
                reader.ocr,
                "verify_tesseract",
                side_effect=FileNotFoundError("tesseract is not installed"),
            ),
        ):
            result = reader.extract(self.image_path)

        self.assertFalse(result["independent_engines"])
        self.assertEqual(result["consensus_lines"], [])
        self.assertEqual(result["agreement_counts"]["provisional_single_pass"], 1)
        self.assertEqual(
            result["provisional_lines"][0]["agreement_type"],
            "provisional_single_pass",
        )
        self.assertEqual(
            result["provisional_lines"][0]["quality_tier"],
            "provisional",
        )
        self.assertEqual(
            result["provisional_lines"][0]["provisional_marker"],
            "[暫定読取]",
        )
        self.assertEqual(
            result["provisional_lines"][0]["provenance"]["primary_pass"],
            "apple_vision_primary",
        )
        self.assertEqual(result["engines"]["tesseract_psm3"]["status"], "unavailable")

    def test_empty_primary_branches_to_fast_sparse(self) -> None:
        sparse = located_line(text="小さな文字")
        passes: list[str] = []

        def vision_result(*args, pass_name: str, **kwargs):
            passes.append(pass_name)
            return vision_completed(
                [sparse] if pass_name == "fast_sparse" else []
            )

        with (
            mock.patch.object(
                reader.ocr, "resolve_vision_binary", return_value=Path("/vision")
            ),
            mock.patch.object(
                reader, "_run_vision_pass", side_effect=vision_result
            ),
            mock.patch.object(
                reader.ocr, "verify_tesseract", side_effect=FileNotFoundError()
            ),
        ):
            result = reader.extract(self.image_path)

        self.assertEqual(passes, ["primary", "fast_sparse"])
        self.assertIn("apple_vision_fast_sparse", result["engines"])
        self.assertEqual(
            result["provisional_lines"][0]["provenance"]["primary_pass"],
            "apple_vision_fast_sparse",
        )

    def test_vision_unavailable_compares_tesseract_psm3_and_psm6(self) -> None:
        line_psm3 = located_line(confidence=0.87)
        line_psm6 = located_line(bbox=[102, 101, 398, 79], confidence=0.83)
        passes: list[int] = []

        def tesseract_result(*args, psm: int, **kwargs):
            passes.append(psm)
            return completed([line_psm3] if psm == 3 else [line_psm6])

        with (
            mock.patch.object(
                reader.ocr,
                "resolve_vision_binary",
                side_effect=RuntimeError("Vision unavailable"),
            ),
            mock.patch.object(
                reader.ocr, "verify_tesseract", return_value=Path("/tesseract")
            ),
            mock.patch.object(
                reader, "_run_tesseract_psm", side_effect=tesseract_result
            ),
        ):
            result = reader.extract(self.image_path)

        self.assertEqual(passes, [3, 6])
        self.assertFalse(result["independent_engines"])
        self.assertEqual(result["consensus_lines"], [])
        self.assertEqual(result["agreement_counts"]["same_engine_agreement"], 1)
        self.assertEqual(
            result["same_engine_lines"][0]["agreement_type"],
            "same_engine_agreement",
        )
        self.assertEqual(
            result["same_engine_lines"][0]["quality_tier"],
            "provisional",
        )
        self.assertEqual(
            result["same_engine_lines"][0]["provisional_marker"],
            "[暫定読取]",
        )
        self.assertEqual(
            result["same_engine_lines"][0]["provenance"]["primary_pass"],
            "tesseract_psm3",
        )

    def test_exif_orientation_keeps_both_coordinate_frames_out_of_high_tier(self) -> None:
        apple = located_line()
        tesseract = located_line()

        def vision_result(*args, **kwargs):
            return vision_completed([apple], orientation=6)

        with (
            mock.patch.object(
                reader.ocr, "resolve_vision_binary", return_value=Path("/vision")
            ),
            mock.patch.object(
                reader, "_run_vision_pass", side_effect=vision_result
            ),
            mock.patch.object(
                reader.ocr, "verify_tesseract", return_value=Path("/tesseract")
            ),
            mock.patch.object(
                reader, "_run_tesseract_psm", return_value=completed([tesseract])
            ),
        ):
            result = reader.extract(self.image_path)

        self.assertEqual(result["orientation"], 6)
        self.assertEqual(result["orientation_source"], "apple_vision_imageio")
        self.assertFalse(result["cross_engine_spatial_comparison"])
        self.assertEqual(result["consensus_lines"], [])
        self.assertEqual(result["agreement_counts"]["independent_agreement"], 0)
        self.assertEqual(result["agreement_counts"]["same_engine_agreement"], 2)
        self.assertEqual(
            {line["bbox_coordinate_system"] for line in result["same_engine_lines"]},
            {
                reader.VISION_BBOX_COORDINATE_SYSTEM,
                reader.RAW_BBOX_COORDINATE_SYSTEM,
            },
        )
        self.assertTrue(
            any("cross-engine spatial agreement is disabled" in warning
                for warning in result["warnings"])
        )

    def test_vision_orientation_must_remain_stable_across_passes(self) -> None:
        apple = located_line()

        def vision_result(*args, pass_name: str, **kwargs):
            orientation = 6 if pass_name == "primary" else 3
            return vision_completed([apple], orientation=orientation)

        with (
            mock.patch.object(
                reader.ocr, "resolve_vision_binary", return_value=Path("/vision")
            ),
            mock.patch.object(
                reader, "_run_vision_pass", side_effect=vision_result
            ),
            mock.patch.object(
                reader.ocr, "verify_tesseract", side_effect=FileNotFoundError()
            ),
        ):
            result = reader.extract(self.image_path)

        self.assertEqual(result["orientation"], 6)
        self.assertEqual(result["engines"]["apple_vision_literal"]["status"], "failed")
        self.assertTrue(
            any("changed across OCR passes" in warning
                for warning in result["engines"]["apple_vision_literal"]["warnings"])
        )

    def test_vision_payload_orientation_and_coordinate_system_are_validated(self) -> None:
        base_payload = {
            "status": "completed",
            "runner": reader.ocr.contract.ENGINE_RUNNERS["apple_vision"],
            "runner_version": reader.ocr.contract.RUNNER_VERSION,
            "request_revision": reader.ocr.contract.APPLE_VISION_CONFIG[
                "request_revision"
            ],
            "pass_name": "primary",
            "width_px": 80,
            "height_px": 40,
            "source_orientation": 1,
            "bbox_coordinate_system": reader.VISION_BBOX_COORDINATE_SYSTEM,
            "lines": [],
            "warnings": [],
            "error": None,
        }

        for field, invalid_value, message in (
            ("source_orientation", 9, "source orientation"),
            ("source_orientation", True, "source orientation"),
            ("bbox_coordinate_system", "raw", "bbox coordinate system"),
        ):
            with self.subTest(field=field, invalid_value=invalid_value):
                payload = {**base_payload, field: invalid_value}
                process = mock.Mock(
                    returncode=0,
                    stdout=json.dumps(payload).encode("utf-8"),
                    stderr=b"",
                )
                with mock.patch.object(reader.subprocess, "run", return_value=process):
                    with self.assertRaisesRegex(RuntimeError, message):
                        reader._run_vision_pass(
                            Path("/vision"),
                            png_header(),
                            {"width_px": 80, "height_px": 40},
                            pass_name="primary",
                            timeout=1,
                        )


if __name__ == "__main__":
    unittest.main()
