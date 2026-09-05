from __future__ import annotations

import inspect
import hashlib
import json
import os
import stat
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
import local_visual_observation as visual  # noqa: E402
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


def paddle_completed(lines: list[dict[str, object]]):
    return (
        "completed" if lines else "needs_review",
        lines,
        [] if lines else ["PaddleOCR returned no OCR lines"],
        None,
        {
            "name": reader.PADDLE_ENGINE_NAME,
            "version": reader.PADDLE_ENGINE_VERSION,
            "pass": reader.PADDLE_PASS,
            "independence_group": reader.PADDLE_INDEPENDENCE_GROUP,
            "fingerprint_sha256": "d" * 64,
        },
        {"setup_ms": 1.0, "inference_ms": 2.0},
    )


def paddle_worker_payload(raw: bytes) -> dict[str, object]:
    engine: dict[str, object] = {
        "name": reader.PADDLE_ENGINE_NAME,
        "version": reader.PADDLE_ENGINE_VERSION,
        "pass": reader.PADDLE_PASS,
        "independence_group": reader.PADDLE_INDEPENDENCE_GROUP,
        "packages": {
            name: {"version": version}
            for name, version in reader.PADDLE_PACKAGE_VERSIONS.items()
        },
        "runtime_lock": {
            "sha256": reader.PADDLE_RUNTIME_LOCK_SHA256,
            "package_count": 72,
            "fully_matched": True,
        },
        "models": reader.PADDLE_MODEL_CONTRACTS,
        "runtime": {
            "settings": dict(reader.PADDLE_RUNTIME_SETTINGS),
            "offline_environment": {
                "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "1",
                "HF_HUB_OFFLINE": "1",
            },
            "network_guard": "python_af_inet_and_af_inet6_denied",
            "model_download_permitted": False,
        },
    }
    engine["fingerprint_sha256"] = hashlib.sha256(
        json.dumps(
            engine,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": reader.PADDLE_WORKER_SCHEMA_VERSION,
        "runner": reader.PADDLE_WORKER_RUNNER,
        "runner_version": reader.PADDLE_WORKER_VERSION,
        "status": "completed",
        "input": {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "width_px": 80,
            "height_px": 40,
        },
        "engine": engine,
        "lines": [located_line(text="中野")],
        "warnings": [],
        "error": None,
        "timing": {"setup_ms": 1.0, "inference_ms": 2.0},
        "external_network_used": False,
        "downloads_performed": False,
    }


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


def image_observation_metadata() -> dict[str, object]:
    dimensions = {"width_px": 80, "height_px": 40}
    return {
        "input_sha256": "b" * 64,
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
        "ocr_input_sha256": "c" * 64,
        "ocr_input_dimensions": dimensions,
        "ocr_input_orientation": 1,
        "coordinate_frame_policy": "canonical_orientation_1",
    }


class AdaptiveLocalImageOCRTests(unittest.TestCase):
    def setUp(self) -> None:
        reader._LOCAL_MODEL_TIMEOUT_LATCH.clear()
        reader._UNREAPED_PADDLE_PROCESSES.clear()
        self.temporary = tempfile.TemporaryDirectory()
        self.image_path = Path(self.temporary.name) / "sample.png"
        self.image_path.write_bytes(png_header())
        self.paddle_runtime_patch = mock.patch.object(
            reader,
            "resolve_paddle_runtime",
            side_effect=FileNotFoundError("PaddleOCR unavailable in phase-1 unit tests"),
        )
        self.paddle_runtime = self.paddle_runtime_patch.start()

    def tearDown(self) -> None:
        self.paddle_runtime_patch.stop()
        reader._LOCAL_MODEL_TIMEOUT_LATCH.clear()
        reader._UNREAPED_PADDLE_PROCESSES.clear()
        self.temporary.cleanup()

    def test_stdlib_header_inspection_and_no_pillow_import(self) -> None:
        metadata = reader.inspect_image_bytes(png_header(321, 123))

        self.assertEqual(
            metadata["dimensions"], {"width_px": 321, "height_px": 123}
        )
        self.assertEqual(metadata["image_format"], "PNG")
        self.assertNotIn("from PIL", inspect.getsource(reader))

    def test_oversized_image_is_rejected_before_reading_the_file(self) -> None:
        descriptor = os.open(self.image_path, os.O_RDONLY)
        oversized = mock.Mock(
            st_mode=stat.S_IFREG | 0o600,
            st_size=reader.MAX_IMAGE_BYTES + 1,
        )
        with (
            mock.patch.object(reader.os, "open", return_value=descriptor),
            mock.patch.object(reader.os, "fstat", return_value=oversized) as fstat,
            mock.patch.object(type(self.image_path), "open") as path_open,
        ):
            with self.assertRaisesRegex(ValueError, "safety limit"):
                reader.read_checked_image_bytes(self.image_path)
        fstat.assert_called_once()
        path_open.assert_not_called()

    def test_checked_image_reader_rejects_a_final_symlink(self) -> None:
        alias = self.image_path.with_name("alias.png")
        alias.symlink_to(self.image_path)
        with self.assertRaisesRegex(ValueError, "opened safely"):
            reader.read_checked_image_bytes(alias)

    def test_path_replacement_after_open_cannot_change_read_bytes(self) -> None:
        original = self.image_path.read_bytes()
        replacement = png_header(81, 41)
        real_open = os.open

        def open_then_replace(path: Path, flags: int) -> int:
            descriptor = real_open(path, flags)
            self.image_path.unlink()
            self.image_path.write_bytes(replacement)
            return descriptor

        with mock.patch.object(reader.os, "open", side_effect=open_then_replace):
            observed = reader.read_checked_image_bytes(self.image_path)

        self.assertEqual(observed, original)
        self.assertEqual(self.image_path.read_bytes(), replacement)

    def test_paddle_runtime_preserves_the_venv_launcher_path(self) -> None:
        runtime_root = Path(self.temporary.name) / "runtime"
        launcher = runtime_root / "venv" / "bin" / "python"
        launcher.parent.mkdir(parents=True)
        launcher.symlink_to(Path(sys.executable))
        model_root = runtime_root / "models"
        model_root.mkdir()

        self.paddle_runtime_patch.stop()
        try:
            with mock.patch.object(
                reader,
                "_paddle_runtime_candidates",
                return_value=[("test", launcher, model_root)],
            ):
                runtime = reader.resolve_paddle_runtime()
        finally:
            self.paddle_runtime = self.paddle_runtime_patch.start()

        self.assertEqual(runtime["python"], launcher.absolute())
        self.assertTrue(runtime["python"].is_symlink())
        self.assertEqual(runtime["python_target"], Path(sys.executable).resolve())

    def test_paddle_parent_enforces_os_sandbox_and_full_engine_contract(self) -> None:
        raw = png_header()
        payload = paddle_worker_payload(raw)
        commands: list[list[str]] = []

        def completed_process(command, **_kwargs):
            commands.append(command)
            output = Path(command[command.index("--output") + 1])
            output.write_text(json.dumps(payload), encoding="utf-8")
            process = mock.Mock(returncode=0)
            process.wait.return_value = 0
            return process

        runtime = {
            "python": Path("/runtime/venv/bin/python"),
            "worker": Path("/runtime/local_paddle_ocr.py"),
            "model_root": Path("/runtime/models"),
            "runtime_lock": Path("/runtime/paddle.lock"),
            "network_sandbox": reader.PADDLE_NETWORK_SANDBOX,
            "network_profile": reader.PADDLE_NETWORK_PROFILE,
        }
        with mock.patch.object(
            reader.subprocess, "Popen", side_effect=completed_process
        ):
            status, lines, *_ = reader._run_paddle_ocr(
                runtime,
                raw,
                {"width_px": 80, "height_px": 40},
                timeout=10,
            )

        self.assertEqual(status, "completed")
        self.assertEqual(lines[0]["raw_text"], "中野")
        self.assertEqual(commands[0][:3], [
            str(reader.PADDLE_NETWORK_SANDBOX),
            "-p",
            reader.PADDLE_NETWORK_PROFILE,
        ])
        self.assertIn("--runtime-lock", commands[0])

    def test_paddle_parent_rejects_self_consistent_unapproved_packages(self) -> None:
        raw = png_header()
        payload = paddle_worker_payload(raw)
        engine = payload["engine"]
        engine["packages"]["paddleocr"]["version"] = "9.9.9"
        fingerprint_payload = dict(engine)
        fingerprint_payload.pop("fingerprint_sha256")
        engine["fingerprint_sha256"] = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        def completed_process(command, **_kwargs):
            output = Path(command[command.index("--output") + 1])
            output.write_text(json.dumps(payload), encoding="utf-8")
            process = mock.Mock(returncode=0)
            process.wait.return_value = 0
            return process

        runtime = {
            "python": Path("/runtime/venv/bin/python"),
            "worker": Path("/runtime/local_paddle_ocr.py"),
            "model_root": Path("/runtime/models"),
            "runtime_lock": Path("/runtime/paddle.lock"),
            "network_sandbox": reader.PADDLE_NETWORK_SANDBOX,
            "network_profile": reader.PADDLE_NETWORK_PROFILE,
        }
        with mock.patch.object(
            reader.subprocess, "Popen", side_effect=completed_process
        ):
            with self.assertRaisesRegex(RuntimeError, "engine contract"):
                reader._run_paddle_ocr(
                    runtime,
                    raw,
                    {"width_px": 80, "height_px": 40},
                    timeout=10,
                )

    def test_paddle_one_shot_refuses_start_after_local_model_timeout(self) -> None:
        runtime = {
            "python": Path("/runtime/venv/bin/python"),
            "worker": Path("/runtime/local_paddle_ocr.py"),
            "model_root": Path("/runtime/models"),
            "runtime_lock": Path("/runtime/paddle.lock"),
            "network_sandbox": reader.PADDLE_NETWORK_SANDBOX,
            "network_profile": reader.PADDLE_NETWORK_PROFILE,
        }
        reader.latch_local_model_timeout()
        with (
            mock.patch.object(reader.subprocess, "Popen") as popen,
            self.assertRaisesRegex(RuntimeError, "restart is disabled"),
        ):
            reader._run_paddle_ocr(
                runtime,
                png_header(),
                {"width_px": 80, "height_px": 40},
                timeout=10,
            )
        popen.assert_not_called()

    def test_paddle_one_shot_keyboard_interrupt_reaps_and_latches(self) -> None:
        runtime = {
            "python": Path("/runtime/venv/bin/python"),
            "worker": Path("/runtime/local_paddle_ocr.py"),
            "model_root": Path("/runtime/models"),
            "runtime_lock": Path("/runtime/paddle.lock"),
            "network_sandbox": reader.PADDLE_NETWORK_SANDBOX,
            "network_profile": reader.PADDLE_NETWORK_PROFILE,
        }
        process = mock.Mock()
        process.pid = 424248
        process.poll.return_value = None
        process.wait.side_effect = [KeyboardInterrupt(), 0]
        with (
            mock.patch.object(reader.subprocess, "Popen", return_value=process),
            mock.patch.object(reader.os, "killpg") as killpg,
            self.assertRaises(KeyboardInterrupt),
        ):
            reader._run_paddle_ocr(
                runtime,
                png_header(),
                {"width_px": 80, "height_px": 40},
                timeout=10,
            )

        self.assertTrue(reader.local_model_timeout_latched())
        killpg.assert_called_once_with(process.pid, reader.signal.SIGTERM)

    def test_paddle_one_shot_popen_interrupt_poison_without_a_handle(self) -> None:
        runtime = {
            "python": Path("/runtime/venv/bin/python"),
            "worker": Path("/runtime/local_paddle_ocr.py"),
            "model_root": Path("/runtime/models"),
            "runtime_lock": Path("/runtime/paddle.lock"),
            "network_sandbox": reader.PADDLE_NETWORK_SANDBOX,
            "network_profile": reader.PADDLE_NETWORK_PROFILE,
        }
        with (
            mock.patch.object(
                reader.subprocess, "Popen", side_effect=KeyboardInterrupt
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            reader._run_paddle_ocr(
                runtime,
                png_header(),
                {"width_px": 80, "height_px": 40},
                timeout=10,
            )

        self.assertTrue(reader.local_model_timeout_latched())

    def test_paddle_cleanup_interrupt_retains_unreaped_handle(self) -> None:
        process = mock.Mock()
        process.pid = 424250
        process.poll.return_value = None
        process.wait.side_effect = [
            KeyboardInterrupt(),
            reader.subprocess.TimeoutExpired("kill", 2),
        ]
        with (
            mock.patch.object(reader.os, "killpg"),
            self.assertRaises(KeyboardInterrupt),
        ):
            reader._terminate_paddle_process(process)

        self.assertTrue(reader.local_model_timeout_latched())
        self.assertTrue(
            any(item is process for item in reader._UNREAPED_PADDLE_PROCESSES)
        )

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
            result = reader.extract(self.image_path, canonicalize=False)

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
        self.assertEqual(tesseract_run.call_count, 1)
        self.assertNotIn("tesseract_psm11", result["engines"])
        self.assertEqual(
            result["consensus_lines"][0]["provenance"][
                "primary_independence_group"
            ],
            "apple_vision",
        )
        self.assertEqual(
            result["consensus_lines"][0]["provenance"][
                "audit_independence_group"
            ],
            "tesseract",
        )
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
            if psm == 3:
                return completed([tesseract_a])
            if psm in {6, 11}:
                return completed([])
            raise AssertionError(f"unexpected PSM: {psm}")

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
            result = reader.extract(self.image_path, canonicalize=False)

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
            if psm == 3:
                return completed([psm3_reading])
            if psm == 6:
                return completed([psm6_reading])
            if psm == 11:
                return completed([])
            raise AssertionError(f"unexpected PSM: {psm}")

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
            result = reader.extract(self.image_path, canonicalize=False)

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
            if psm == 3:
                return completed([psm3_reading])
            if psm == 6:
                return completed([psm6_reading])
            if psm == 11:
                return completed([])
            raise AssertionError(f"unexpected PSM: {psm}")

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
            result = reader.extract(self.image_path, canonicalize=False)

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
            result = reader.extract(self.image_path, canonicalize=False)

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

    def test_ocr_only_mode_never_invokes_the_local_vlm_api(self) -> None:
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
                reader, "_ollama_json", side_effect=AssertionError("must not call Ollama")
            ) as ollama,
        ):
            result = reader.extract(
                self.image_path,
                canonicalize=False,
                allow_vlm=False,
            )

        ollama.assert_not_called()
        self.assertEqual(result["read_lines"], [])
        self.assertFalse(result["vlm_allowed"])
        self.assertEqual(
            result["engines"]["gemma4_unlocated_transcript"]["status"],
            "disabled",
        )
        self.assertEqual(
            result["engines"]["gemma4_unlocated_transcript"]["trigger"],
            "ocr_only_mode",
        )

    def test_canonical_image_bytes_are_shared_by_vision_and_tesseract(self) -> None:
        original = self.image_path.read_bytes()
        canonical = b"canonical-png-bytes"
        canonicalization = {
            "status": "completed",
            "method": "coregraphics_imageio_exif_srgb_png",
            "runner": reader.CANONICALIZER_RUNNER,
            "runner_version": reader.CANONICALIZER_VERSION,
            "source_orientation": 6,
            "source_dimensions": {"width_px": 80, "height_px": 40},
            "canonical_orientation": 1,
            "canonical_dimensions": {"width_px": 40, "height_px": 80},
            "canonical_sha256": hashlib.sha256(canonical).hexdigest(),
            "format": "PNG",
            "color_space": "sRGB",
            "pixel_format": "RGBA8",
            "alpha_policy": "flattened_on_white",
            "source_sha256": hashlib.sha256(original).hexdigest(),
            "build": {},
        }
        apple = located_line(text="Nakano")
        tesseract = located_line(text="Nakano", bbox=[102, 101, 398, 79])
        seen: list[tuple[str, bytes, dict[str, int]]] = []

        def vision_result(_binary, raw, dimensions, *, pass_name, timeout):
            seen.append((f"vision:{pass_name}", raw, dimensions))
            return vision_completed([apple], orientation=1)

        def tesseract_result(_binary, raw, dimensions, *, psm, timeout):
            seen.append((f"tesseract:{psm}", raw, dimensions))
            return completed([tesseract])

        with (
            mock.patch.object(
                reader,
                "canonicalize_image_bytes",
                return_value=(canonical, canonicalization),
            ),
            mock.patch.object(
                reader.ocr, "resolve_vision_binary", return_value=Path("/vision")
            ),
            mock.patch.object(reader, "_run_vision_pass", side_effect=vision_result),
            mock.patch.object(
                reader.ocr, "verify_tesseract", return_value=Path("/tesseract")
            ),
            mock.patch.object(
                reader, "_run_tesseract_psm", side_effect=tesseract_result
            ),
        ):
            result = reader.extract(self.image_path, allow_vlm=False)

        self.assertEqual([name for name, _, _ in seen], ["vision:primary", "tesseract:3"])
        self.assertTrue(all(raw == canonical for _, raw, _ in seen))
        self.assertTrue(all(
            dimensions == {"width_px": 40, "height_px": 80}
            for _, _, dimensions in seen
        ))
        self.assertEqual(self.image_path.read_bytes(), original)
        self.assertEqual(result["orientation"], 6)
        self.assertEqual(result["orientation_source"], "imageio_canonicalizer")
        self.assertEqual(result["source_dimensions"], {"width_px": 80, "height_px": 40})
        self.assertEqual(result["dimensions"], {"width_px": 40, "height_px": 80})
        self.assertEqual(result["ocr_input_sha256"], canonicalization["canonical_sha256"])
        self.assertTrue(result["cross_engine_spatial_comparison"])
        self.assertEqual(result["consensus_lines"][0]["quality_tier"], "high")

    def test_canonical_image_is_shared_with_paddle_and_tesseract(self) -> None:
        original = self.image_path.read_bytes()
        canonical = b"canonical-png-for-paddle"
        canonical_dimensions = {"width_px": 40, "height_px": 80}
        canonicalization = {
            "status": "completed",
            "method": "coregraphics_imageio_exif_srgb_png",
            "runner": reader.CANONICALIZER_RUNNER,
            "runner_version": reader.CANONICALIZER_VERSION,
            "source_orientation": 6,
            "source_dimensions": {"width_px": 80, "height_px": 40},
            "canonical_orientation": 1,
            "canonical_dimensions": canonical_dimensions,
            "canonical_sha256": hashlib.sha256(canonical).hexdigest(),
            "format": "PNG",
            "color_space": "sRGB",
            "pixel_format": "RGBA8",
            "alpha_policy": "flattened_on_white",
            "source_sha256": hashlib.sha256(original).hexdigest(),
            "build": {},
        }
        tesseract = located_line(text="中野")
        paddle = located_line(text="中野", bbox=[102, 101, 398, 79])

        def vision_result(*args, pass_name: str, **kwargs):
            return vision_completed([], orientation=1)

        def tesseract_result(_binary, raw, dimensions, *, psm, timeout):
            self.assertEqual(raw, canonical)
            self.assertEqual(dimensions, canonical_dimensions)
            return completed([tesseract] if psm == 3 else [])

        self.paddle_runtime.side_effect = None
        self.paddle_runtime.return_value = {
            "source": "test",
            "python": Path("/paddle-python"),
            "worker": Path("/paddle-worker"),
            "model_root": Path("/paddle-models"),
        }
        with (
            mock.patch.object(
                reader,
                "canonicalize_image_bytes",
                return_value=(canonical, canonicalization),
            ),
            mock.patch.object(
                reader.ocr, "resolve_vision_binary", return_value=Path("/vision")
            ),
            mock.patch.object(reader, "_run_vision_pass", side_effect=vision_result),
            mock.patch.object(
                reader.ocr, "verify_tesseract", return_value=Path("/tesseract")
            ),
            mock.patch.object(
                reader, "_run_tesseract_psm", side_effect=tesseract_result
            ),
            mock.patch.object(
                reader, "_run_paddle_ocr", return_value=paddle_completed([paddle])
            ) as paddle_run,
        ):
            result = reader.extract(self.image_path, allow_vlm=False)

        self.assertEqual(paddle_run.call_args.args[1], canonical)
        self.assertEqual(paddle_run.call_args.args[2], canonical_dimensions)
        self.assertEqual(result["agreement_counts"]["independent_agreement"], 1)
        line = result["consensus_lines"][0]
        self.assertEqual(line["quality_tier"], "high")
        self.assertEqual(line["provenance"]["primary_pass"], "tesseract_psm3")
        self.assertEqual(line["provenance"]["audit_pass"], reader.PADDLE_PASS)
        self.assertEqual(
            line["provenance"]["audit_independence_group"], "paddleocr"
        )
        self.assertTrue(result["independent_engines"])
        self.assertEqual(result["engines"]["paddleocr"]["input_sha256"], hashlib.sha256(canonical).hexdigest())

    def test_paddle_only_reading_stays_provisional(self) -> None:
        paddle = located_line(text="Nakano")
        self.paddle_runtime.side_effect = None
        self.paddle_runtime.return_value = {
            "source": "test",
            "python": Path("/paddle-python"),
            "worker": Path("/paddle-worker"),
            "model_root": Path("/paddle-models"),
        }
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
                reader, "_run_paddle_ocr", return_value=paddle_completed([paddle])
            ),
        ):
            result = reader.extract(
                self.image_path,
                canonicalize=False,
                allow_vlm=False,
            )

        self.assertEqual(result["consensus_lines"], [])
        self.assertEqual(result["agreement_counts"]["provisional_single_pass"], 1)
        self.assertEqual(result["provisional_lines"][0]["text"], "Nakano")
        self.assertEqual(
            result["provisional_lines"][0]["provenance"]["primary_pass"],
            reader.PADDLE_PASS,
        )
        self.assertFalse(result["independent_engines"])

    def test_one_paddle_line_cannot_certify_two_tesseract_passes(self) -> None:
        tesseract_psm3 = located_line(text="中野", bbox=[100, 100, 400, 80])
        tesseract_psm6 = located_line(text="中野", bbox=[101, 101, 399, 79])
        paddle = located_line(text="中野", bbox=[102, 102, 398, 78])

        def vision_result(*args, pass_name: str, **kwargs):
            return vision_completed([], orientation=1)

        def tesseract_result(*args, psm: int, **kwargs):
            if psm == 3:
                return completed([tesseract_psm3])
            if psm in {6, 11}:
                return completed([tesseract_psm6])
            raise AssertionError(f"unexpected PSM: {psm}")

        self.paddle_runtime.side_effect = None
        self.paddle_runtime.return_value = {
            "source": "test",
            "python": Path("/paddle-python"),
            "worker": Path("/paddle-worker"),
            "model_root": Path("/paddle-models"),
        }
        with (
            mock.patch.object(
                reader.ocr, "resolve_vision_binary", return_value=Path("/vision")
            ),
            mock.patch.object(reader, "_run_vision_pass", side_effect=vision_result),
            mock.patch.object(
                reader.ocr, "verify_tesseract", return_value=Path("/tesseract")
            ),
            mock.patch.object(
                reader, "_run_tesseract_psm", side_effect=tesseract_result
            ),
            mock.patch.object(
                reader, "_run_paddle_ocr", return_value=paddle_completed([paddle])
            ),
        ):
            result = reader.extract(
                self.image_path,
                canonicalize=False,
                allow_vlm=False,
            )

        self.assertEqual(result["agreement_counts"]["independent_agreement"], 1)
        self.assertEqual(
            sum(
                line["provenance"].get("audit_line_id") == paddle["line_id"]
                for line in result["consensus_lines"]
            ),
            1,
        )

    def test_canonicalization_failure_cannot_promote_matching_raw_readings(self) -> None:
        apple = located_line(text="Nakano")
        tesseract = located_line(text="Nakano", bbox=[102, 101, 398, 79])

        def vision_result(*args, pass_name: str, **kwargs):
            return vision_completed([apple] if pass_name == "primary" else [])

        def tesseract_result(*args, psm: int, **kwargs):
            if psm == 3:
                return completed([tesseract])
            if psm in {6, 11}:
                return completed([])
            raise AssertionError(f"unexpected PSM: {psm}")

        with (
            mock.patch.object(
                reader,
                "canonicalize_image_bytes",
                side_effect=RuntimeError("canonicalizer failed"),
            ),
            mock.patch.object(
                reader.ocr, "resolve_vision_binary", return_value=Path("/vision")
            ),
            mock.patch.object(reader, "_run_vision_pass", side_effect=vision_result),
            mock.patch.object(
                reader.ocr, "verify_tesseract", return_value=Path("/tesseract")
            ),
            mock.patch.object(
                reader, "_run_tesseract_psm", side_effect=tesseract_result
            ),
        ):
            result = reader.extract(self.image_path, allow_vlm=False)

        self.assertEqual(result["canonicalization"]["status"], "failed")
        self.assertFalse(result["cross_engine_spatial_comparison"])
        self.assertEqual(result["consensus_lines"], [])
        self.assertTrue(all(
            line["quality_tier"] == "provisional" for line in result["read_lines"]
        ))
        self.assertTrue(any(
            "canonicalization failed" in warning for warning in result["warnings"]
        ))

    def test_canonicalization_failure_cannot_promote_paddle_and_tesseract(self) -> None:
        tesseract = located_line(text="Nakano")
        paddle = located_line(text="Nakano", bbox=[102, 101, 398, 79])

        def tesseract_result(*args, psm: int, **kwargs):
            return completed([tesseract] if psm == 3 else [])

        self.paddle_runtime.side_effect = None
        self.paddle_runtime.return_value = {
            "source": "test",
            "python": Path("/paddle-python"),
            "worker": Path("/paddle-worker"),
            "model_root": Path("/paddle-models"),
        }
        with (
            mock.patch.object(
                reader,
                "canonicalize_image_bytes",
                side_effect=RuntimeError("canonicalizer failed"),
            ),
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
            mock.patch.object(
                reader, "_run_paddle_ocr", return_value=paddle_completed([paddle])
            ),
        ):
            result = reader.extract(self.image_path, allow_vlm=False)

        self.assertEqual(result["canonicalization"]["status"], "failed")
        self.assertFalse(result["cross_engine_spatial_comparison"])
        self.assertEqual(result["consensus_lines"], [])
        self.assertFalse(result["independent_engines"])
        self.assertTrue(result["multiple_engine_groups_observed"])
        self.assertTrue(all(
            line["quality_tier"] == "provisional" for line in result["read_lines"]
        ))

    def test_psm11_recovers_an_unmatched_vision_line(self) -> None:
        apple = located_line(text="Nakano")
        sparse = located_line(text="Nakano", bbox=[102, 101, 398, 79])
        passes: list[int] = []

        def vision_result(*args, pass_name: str, **kwargs):
            return vision_completed([apple] if pass_name == "primary" else [])

        def tesseract_result(*args, psm: int, **kwargs):
            passes.append(psm)
            if psm in {3, 6}:
                return completed([])
            if psm == 11:
                return completed([sparse])
            raise AssertionError(f"unexpected PSM: {psm}")

        with (
            mock.patch.object(
                reader.ocr, "resolve_vision_binary", return_value=Path("/vision")
            ),
            mock.patch.object(reader, "_run_vision_pass", side_effect=vision_result),
            mock.patch.object(
                reader.ocr, "verify_tesseract", return_value=Path("/tesseract")
            ),
            mock.patch.object(
                reader, "_run_tesseract_psm", side_effect=tesseract_result
            ),
        ):
            result = reader.extract(
                self.image_path,
                canonicalize=False,
                allow_vlm=False,
            )

        self.assertEqual(passes, [3, 6, 11])
        self.assertEqual(result["agreement_counts"]["independent_agreement"], 1)
        self.assertEqual(
            result["consensus_lines"][0]["provenance"]["audit_pass"],
            "tesseract_psm11",
        )
        self.assertEqual(
            result["engines"]["tesseract_psm11"]["trigger"],
            "no_independent_agreement",
        )

    def test_psm11_recovers_a_second_line_after_existing_consensus(self) -> None:
        first_vision = located_line(text="Nakano", bbox=[100, 100, 400, 80])
        second_vision = located_line(text="中野", bbox=[100, 300, 400, 80])
        first_psm3 = located_line(text="Nakano", bbox=[102, 101, 398, 79])
        second_psm11 = located_line(text="中野", bbox=[101, 302, 399, 78])
        passes: list[int] = []

        def vision_result(*args, pass_name: str, **kwargs):
            return vision_completed(
                [first_vision, second_vision] if pass_name == "primary" else []
            )

        def tesseract_result(*args, psm: int, **kwargs):
            passes.append(psm)
            if psm == 3:
                return completed([first_psm3])
            if psm == 6:
                return completed([])
            if psm == 11:
                return completed([second_psm11])
            raise AssertionError(f"unexpected PSM: {psm}")

        with (
            mock.patch.object(
                reader.ocr, "resolve_vision_binary", return_value=Path("/vision")
            ),
            mock.patch.object(reader, "_run_vision_pass", side_effect=vision_result),
            mock.patch.object(
                reader.ocr, "verify_tesseract", return_value=Path("/tesseract")
            ),
            mock.patch.object(
                reader, "_run_tesseract_psm", side_effect=tesseract_result
            ),
        ):
            result = reader.extract(
                self.image_path,
                canonicalize=False,
                allow_vlm=False,
            )

        self.assertEqual(passes, [3, 6, 11])
        self.assertEqual(
            {line["text"] for line in result["consensus_lines"]},
            {"Nakano", "中野"},
        )
        self.assertEqual(
            result["engines"]["tesseract_psm11"]["trigger"],
            "unmatched_located_readings",
        )

    def test_psm11_failure_preserves_earlier_tesseract_readings(self) -> None:
        psm3 = located_line(text="中野", confidence=0.87)
        psm6 = located_line(text="中野", bbox=[102, 101, 398, 79], confidence=0.83)

        def tesseract_result(*args, psm: int, **kwargs):
            if psm == 3:
                return completed([psm3])
            if psm == 6:
                return completed([psm6])
            if psm == 11:
                raise TimeoutError("PSM11 timed out")
            raise AssertionError(f"unexpected PSM: {psm}")

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
            result = reader.extract(
                self.image_path,
                canonicalize=False,
                allow_vlm=False,
            )

        self.assertEqual(result["engines"]["tesseract_psm11"]["status"], "failed")
        self.assertEqual(result["agreement_counts"]["same_engine_agreement"], 1)
        self.assertEqual(result["same_engine_lines"][0]["text"], "中野")
        self.assertEqual(result["same_engine_lines"][0]["quality_tier"], "provisional")

    def test_unlocated_transcript_uses_installed_digest_and_strict_json(self) -> None:
        calls: list[tuple[str, str]] = []
        deadlines: list[float] = []

        def local_response(method, path, *, payload, deadline_at):
            calls.append((method, path))
            deadlines.append(deadline_at)
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
                    "role": "assistant",
                    "content": json.dumps(
                        {"transcript": "作業報告\n合計 24時間"},
                        ensure_ascii=False,
                    )
                },
                "done": True,
            }

        with mock.patch.object(reader, "_ollama_json", side_effect=local_response):
            result = reader._run_unlocated_transcript_inline(
                png_header(), timeout=999
            )

        self.assertEqual(calls, [
            ("GET", "/api/tags"),
            ("POST", "/api/chat"),
            ("GET", "/api/tags"),
        ])
        self.assertEqual(len(set(deadlines)), 1)
        self.assertEqual(result["model_digest"], "a" * 64)
        self.assertEqual(
            result["prompt_sha256"], reader.UNLOCATED_TRANSCRIPT_PROMPT_SHA256
        )
        self.assertEqual(result["location_status"], "unlocated")
        self.assertNotIn("bbox", result)

    def test_unlocated_transport_integration_rejects_invalid_json_and_requests(self) -> None:
        class FakeResponse:
            status = 200

            def __init__(self, raw: bytes) -> None:
                self.raw = raw
                self.finished = False

            def read1(self, _limit: int) -> bytes:
                if self.finished:
                    return b""
                self.finished = True
                return self.raw

        class FakeConnection:
            raw = b""

            def __init__(self, _host: str, _port: int, *, timeout: float) -> None:
                self.timeout = timeout
                self.sock = None

            def request(self, *_args: object, **_kwargs: object) -> None:
                return None

            def getresponse(self) -> FakeResponse:
                return FakeResponse(self.raw)

            def close(self) -> None:
                return None

        invalid_responses = (
            (b'{"models":[],"models":[]}', "duplicate key"),
            (b'{"models":[],"unexpected":NaN}', "non-JSON constant"),
        )
        for raw_response, message in invalid_responses:
            with self.subTest(response=raw_response):
                FakeConnection.raw = raw_response
                with (
                    mock.patch.object(
                        visual.http.client, "HTTPConnection", FakeConnection
                    ),
                    self.assertRaisesRegex(RuntimeError, message),
                ):
                    reader._ollama_json(
                        "GET", "/api/tags", payload=None, timeout=5
                    )

        with mock.patch.object(
            visual.http.client,
            "HTTPConnection",
            side_effect=AssertionError("invalid request must not connect"),
        ) as constructor:
            for method, path, payload in (
                ("GET", "/api/chat", None),
                ("POST", "/api/tags", {}),
                ("GET", "/api/tags", {}),
                ("POST", "/api/chat", None),
            ):
                with self.subTest(method=method, path=path, payload=payload):
                    with self.assertRaises(ValueError):
                        reader._ollama_json(
                            method, path, payload=payload, timeout=5
                        )
            with self.assertRaises(ValueError):
                reader._ollama_json(
                    "POST",
                    "/api/chat",
                    payload={"value": float("nan")},
                    timeout=5,
                )
        constructor.assert_not_called()

    def test_unlocated_base64_and_call_sequence_share_the_absolute_deadline(self) -> None:
        now = [100.0]
        digest = "a" * 64
        tags = {
            "models": [{
                "name": reader.UNLOCATED_TRANSCRIPT_MODEL,
                "model": reader.UNLOCATED_TRANSCRIPT_MODEL,
                "digest": digest,
            }]
        }
        real_b64encode = reader.base64.b64encode
        common_transport = mock.Mock(return_value=tags)

        def expired_base64(raw: bytes) -> bytes:
            encoded = real_b64encode(raw)
            now[0] = 106.0
            return encoded

        with (
            mock.patch.object(reader.time, "monotonic", side_effect=lambda: now[0]),
            mock.patch.object(visual, "_ollama_json", common_transport),
            mock.patch.object(
                reader.base64, "b64encode", side_effect=expired_base64
            ),
            self.assertRaisesRegex(TimeoutError, "absolute deadline"),
        ):
            reader._run_unlocated_transcript_inline(png_header(), timeout=5)

        common_transport.assert_called_once()
        self.assertEqual(common_transport.call_args.args, ("GET", "/api/tags"))
        self.assertEqual(common_transport.call_args.kwargs["deadline_at"], 105.0)

        now[0] = 100.0
        calls: list[tuple[str, str]] = []

        def successful_transport(method, path, *, payload, deadline_at):
            calls.append((method, path))
            self.assertEqual(deadline_at, 105.0)
            if path == "/api/tags":
                return tags
            return {
                "model": reader.UNLOCATED_TRANSCRIPT_MODEL,
                "message": {
                    "role": "assistant",
                    "content": '{"transcript":"ok"}',
                },
                "done": True,
            }

        def expired_content(_content: str) -> dict[str, str]:
            now[0] = 106.0
            return {"transcript": "ok"}

        with (
            mock.patch.object(reader.time, "monotonic", side_effect=lambda: now[0]),
            mock.patch.object(
                visual, "_ollama_json", side_effect=successful_transport
            ),
            mock.patch.object(
                reader, "_strict_unlocated_content", side_effect=expired_content
            ),
            self.assertRaisesRegex(TimeoutError, "absolute deadline"),
        ):
            reader._run_unlocated_transcript_inline(png_header(), timeout=5)
        self.assertEqual(calls, [("GET", "/api/tags"), ("POST", "/api/chat")])

    def test_unlocated_transcript_rejects_unsafe_envelopes_and_model_changes(self) -> None:
        def tags(*digests: str) -> dict[str, object]:
            return {
                "models": [
                    {
                        "name": reader.UNLOCATED_TRANSCRIPT_MODEL,
                        "model": reader.UNLOCATED_TRANSCRIPT_MODEL,
                        "digest": digest,
                    }
                    for digest in digests
                ]
            }

        def chat(content: str = '{"transcript":"ok"}') -> dict[str, object]:
            return {
                "model": reader.UNLOCATED_TRANSCRIPT_MODEL,
                "message": {"role": "assistant", "content": content},
                "done": True,
            }

        unsafe_cases = []
        duplicate = chat('{"transcript":"first","transcript":"second"}')
        unsafe_cases.append(("duplicate", duplicate, "not strict JSON"))
        non_json = chat('{"transcript":NaN}')
        unsafe_cases.append(("non_json", non_json, "not strict JSON"))
        incomplete = chat()
        incomplete["done"] = False
        unsafe_cases.append(("incomplete", incomplete, "message is invalid"))
        wrong_role = chat()
        wrong_role["message"]["role"] = "user"
        unsafe_cases.append(("wrong_role", wrong_role, "message is invalid"))
        tools = chat()
        tools["message"]["tool_calls"] = [{"function": {"name": "unsafe"}}]
        unsafe_cases.append(("tool_calls", tools, "message is invalid"))
        truncated = chat()
        truncated["done_reason"] = "length"
        unsafe_cases.append(("truncated", truncated, "message is invalid"))
        unknown = chat()
        unknown["unexpected"] = True
        unsafe_cases.append(("unknown", unknown, "error or unknown field"))

        for name, response, message in unsafe_cases:
            with self.subTest(name=name):
                with (
                    mock.patch.object(
                        reader,
                        "_ollama_json",
                        side_effect=[tags("a" * 64), response],
                    ),
                    self.assertRaisesRegex(RuntimeError, message),
                ):
                    reader._run_unlocated_transcript_inline(
                        png_header(), timeout=10
                    )

        with (
            mock.patch.object(
                reader,
                "_ollama_json",
                return_value=tags("a" * 64, "b" * 64),
            ),
            self.assertRaisesRegex(RuntimeError, "conflicting digests"),
        ):
            reader._run_unlocated_transcript_inline(png_header(), timeout=10)

        non_string_digest = tags("a" * 64)
        non_string_digest["models"][0]["digest"] = 123
        with (
            mock.patch.object(
                reader, "_ollama_json", return_value=non_string_digest
            ),
            self.assertRaisesRegex(RuntimeError, "digest is invalid"),
        ):
            reader._run_unlocated_transcript_inline(png_header(), timeout=10)

        with (
            mock.patch.object(
                reader,
                "_ollama_json",
                side_effect=[tags("a" * 64), chat(), tags("b" * 64)],
            ),
            self.assertRaisesRegex(RuntimeError, "changed during transcription"),
        ):
            reader._run_unlocated_transcript_inline(png_header(), timeout=10)

        for invalid_timeout in (0, -1, float("nan"), True, "10"):
            with self.subTest(timeout=invalid_timeout):
                with (
                    mock.patch.object(
                        reader,
                        "_ollama_json",
                        side_effect=AssertionError("invalid timeout must stop first"),
                    ) as request,
                    self.assertRaisesRegex(ValueError, "positive finite"),
                ):
                    reader._run_unlocated_transcript_inline(
                        png_header(), timeout=invalid_timeout
                    )
                request.assert_not_called()

    def test_public_unlocated_transcript_revalidates_isolated_result(self) -> None:
        expected = unlocated_transcript("作業報告\n合計 24時間")
        with mock.patch(
            "local_visual_observation.run_unlocated_transcript_isolated",
            return_value=expected,
        ) as isolated:
            result = reader.run_unlocated_transcript_fallback(
                png_header(), timeout=12
            )
        self.assertEqual(result, expected)
        isolated.assert_called_once_with(
            png_header(),
            prompt_sha256=reader.UNLOCATED_TRANSCRIPT_PROMPT_SHA256,
            timeout=12,
        )

        forged = dict(expected)
        forged["model_digest"] = "invalid"
        with (
            mock.patch(
                "local_visual_observation.run_unlocated_transcript_isolated",
                return_value=forged,
            ),
            self.assertRaisesRegex(RuntimeError, "result contract is invalid"),
        ):
            reader.run_unlocated_transcript_fallback(png_header(), timeout=12)

    def test_uninstalled_model_skips_without_chat_or_download(self) -> None:
        calls: list[tuple[str, str]] = []

        def no_model(method, path, *, payload, deadline_at):
            calls.append((method, path))
            return {"models": []}

        with mock.patch.object(reader, "_ollama_json", side_effect=no_model):
            with self.assertRaisesRegex(RuntimeError, "download is forbidden"):
                reader._run_unlocated_transcript_inline(
                    png_header(), timeout=10
                )

        self.assertEqual(calls, [("GET", "/api/tags")])

    def test_probe_retains_unlocated_transcript_as_searchable_provisional_text(self) -> None:
        observation = {
            **image_observation_metadata(),
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

    def test_probe_rejects_same_engine_line_forged_as_high(self) -> None:
        forged = {
            "text": "中野",
            "bbox": [100, 100, 400, 80],
            "bbox_coordinate_system": reader.ORIENTATION_1_COORDINATE_SYSTEM,
            "overlap": 1.0,
            "primary_confidence": 0.9,
            "audit_confidence": 0.8,
            "agreement_type": "independent_agreement",
            "quality_tier": "high",
            "provenance": {
                "primary_pass": "tesseract_psm3",
                "audit_pass": "tesseract_psm11",
                "primary_engine": "tesseract",
                "audit_engine": "tesseract",
                "primary_independence_group": "tesseract",
                "audit_independence_group": "tesseract",
                "primary_line_id": "psm3-1",
                "audit_line_id": "psm11-1",
                "primary_bbox_coordinate_system": reader.ORIENTATION_1_COORDINATE_SYSTEM,
                "audit_bbox_coordinate_system": reader.ORIENTATION_1_COORDINATE_SYSTEM,
                "comparison_coordinate_system": reader.ORIENTATION_1_COORDINATE_SYSTEM,
            },
        }
        observation = {
            **image_observation_metadata(),
            "engines": {},
            "independent_engines": True,
            "consensus_lines": [forged],
            "read_lines": [forged],
            "unlocated_transcript": None,
            "unresolved_count": 0,
        }
        probe = probe_records.Probe(
            self.image_path.parent,
            "2026-09-01T00:00:00+00:00",
            None,
            diagnostic=False,
        )
        with mock.patch.object(reader, "extract", return_value=observation):
            with self.assertRaisesRegex(
                ValueError, "distinct line-level engine groups"
            ):
                probe.extract(self.image_path)

    def test_unknown_prefixed_passes_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown OCR pass engine"):
            reader._pass_engine("apple_vision_fake")
        with self.assertRaisesRegex(ValueError, "unsupported OCR provenance pass"):
            probe_records.ocr_engine("tesseract_fake")

    def test_probe_rejects_engine_label_that_disagrees_with_pass(self) -> None:
        forged = {
            "text": "中野",
            "bbox": [100, 100, 400, 80],
            "bbox_coordinate_system": reader.ORIENTATION_1_COORDINATE_SYSTEM,
            "overlap": 1.0,
            "primary_confidence": 0.9,
            "audit_confidence": 0.8,
            "agreement_type": "independent_agreement",
            "quality_tier": "high",
            "provenance": {
                "primary_pass": "apple_vision_primary",
                "audit_pass": "tesseract_psm3",
                "primary_engine": "tesseract",
                "audit_engine": "tesseract",
                "primary_independence_group": "apple_vision",
                "audit_independence_group": "tesseract",
                "primary_line_id": "vision-1",
                "audit_line_id": "psm3-1",
                "primary_bbox_coordinate_system": reader.ORIENTATION_1_COORDINATE_SYSTEM,
                "audit_bbox_coordinate_system": reader.ORIENTATION_1_COORDINATE_SYSTEM,
                "comparison_coordinate_system": reader.ORIENTATION_1_COORDINATE_SYSTEM,
            },
        }
        observation = {
            **image_observation_metadata(),
            "engines": {},
            "independent_engines": True,
            "consensus_lines": [forged],
            "read_lines": [forged],
            "unlocated_transcript": None,
            "unresolved_count": 0,
        }
        probe = probe_records.Probe(
            self.image_path.parent,
            "2026-09-01T00:00:00+00:00",
            None,
            diagnostic=False,
        )
        with mock.patch.object(reader, "extract", return_value=observation):
            with self.assertRaisesRegex(ValueError, "primary engine is invalid"):
                probe.extract(self.image_path)

    def test_long_unlocated_transcript_is_exactly_sharded_and_all_chunks_are_provisional(self) -> None:
        transcript_text = "先頭\n" + ("A" * 5000) + "\n末尾の質問根拠"
        observation = {
            **image_observation_metadata(),
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
            result = reader.extract(self.image_path, canonicalize=False)

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
            result = reader.extract(self.image_path, canonicalize=False)

        self.assertEqual(passes, ["primary", "fast_sparse"])
        self.assertIn("apple_vision_fast_sparse", result["engines"])
        self.assertEqual(
            result["provisional_lines"][0]["provenance"]["primary_pass"],
            "apple_vision_fast_sparse",
        )

    def test_vision_unavailable_compares_all_tesseract_layout_passes(self) -> None:
        line_psm3 = located_line(confidence=0.87)
        line_psm6 = located_line(bbox=[102, 101, 398, 79], confidence=0.83)
        passes: list[int] = []

        def tesseract_result(*args, psm: int, **kwargs):
            passes.append(psm)
            if psm == 3:
                return completed([line_psm3])
            if psm in {6, 11}:
                return completed([line_psm6])
            raise AssertionError(f"unexpected PSM: {psm}")

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
            result = reader.extract(self.image_path, canonicalize=False)

        self.assertEqual(passes, [3, 6, 11])
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
        self.assertEqual(
            result["engines"]["tesseract_psm11"]["independence_group"],
            "tesseract",
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
            result = reader.extract(self.image_path, canonicalize=False)

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
            result = reader.extract(self.image_path, canonicalize=False)

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
