from __future__ import annotations

import builtins
import copy
import hashlib
import io
import json
import os
import platform
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from PIL import Image


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "scripts"
sys.path.insert(0, str(SCRIPTS))

import classify_visual_assets as classifier
import extract_ocr_observations as extractor
import validate_ocr_observations as validator


MODEL = {
    "requested": "gemma4:12b",
    "resolved": "gemma4:12b",
    "digest": "a" * 64,
}
STAMP = "2026-08-16T00:00:00+00:00"


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def make_png(size: tuple[int, int] = (100, 80)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, "white").save(output, format="PNG")
    return output.getvalue()


def png_skeleton_bytes(width: int, height: int) -> bytes:
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


class OCRObservationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aiec-ocr-contract-")
        self.root = Path(self.temporary.name)
        self.images = self.root / "images"
        self.images.mkdir()
        self.assets_path = self.root / "assets.jsonl"
        self.classifications_path = self.root / "classifications.jsonl"
        self.observations_path = self.root / "observations.jsonl"
        self.raw_asset, self.asset, self.classification = self._make_upstream(
            "1" * 32, "asset.png", make_png()
        )
        write_jsonl(self.assets_path, [self.raw_asset])
        write_jsonl(self.classifications_path, [self.classification])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _make_upstream(
        self,
        hex_id: str,
        filename: str,
        data: bytes,
        *,
        routes: list[str] | None = None,
        materialized_path: str | None = None,
        origin_kind: str = "standalone_image",
        processing_layers: list[str] | None = None,
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        path = self.images / filename
        if not path.exists() and materialized_path is None:
            path.write_bytes(data)
        if materialized_path is None:
            materialized_path = str(path.relative_to(self.root))
        with Image.open(io.BytesIO(data)) as image:
            width, height = int(image.width), int(image.height)
            mime_type = Image.MIME[str(image.format).upper()]
        source: dict[str, object] = {
            "relative_path": f"fixtures/{filename}",
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        if origin_kind == "pdf_page":
            source["document_type"] = "pdf"
        if processing_layers is not None:
            source["processing_layers"] = processing_layers
        raw_asset: dict[str, object] = {
            "asset_id": "asset_" + hex_id,
            "materialized_path": materialized_path,
            "sha256": hashlib.sha256(data).hexdigest(),
            "mime_type": mime_type,
            "dimensions": {"width_px": width, "height_px": height},
            "source": source,
            "origin": {
                "kind": origin_kind,
                "page_number": 1 if origin_kind == "pdf_page" else None,
            },
        }
        asset = classifier.normalize_asset(raw_asset, self.root)
        route_values = routes or ["ocr_text"]
        if route_values == ["review"]:
            classification_payload = {
                "primary_type": "unknown",
                "content_types": ["unknown"],
                "information_role": "unknown",
                "regions": [],
                "routes": route_values,
                "exactness": "unresolved",
                "warnings": ["classification requires review"],
                "status": "needs_review",
            }
        else:
            content_types = (
                ["text_document"] if "ocr_text" in route_values else ["chart"]
            )
            classification_payload = {
                "primary_type": content_types[0],
                "content_types": content_types,
                "information_role": "primary",
                "regions": [
                    {
                        "region_id": "r1",
                        "bbox": [0.0, 0.0, 1.0, 1.0],
                        "types": content_types,
                    }
                ],
                "routes": route_values,
                "exactness": "estimated",
                "warnings": [],
                "status": "classified",
            }
        signature = classifier.signature_for_asset(asset, MODEL["digest"])
        classification = classifier._record_envelope(
            asset, MODEL, signature, classification_payload
        )
        return raw_asset, asset, classification

    def _line(
        self,
        text: str,
        *,
        bbox: list[int] | None = None,
        confidence: float | None = 0.8,
        sequence: int = 1,
    ) -> dict[str, object]:
        return {
            "line_id": f"line_{sequence}",
            "sequence": sequence,
            "raw_text": text,
            "bbox": bbox or [100, 100, 600, 100],
            "confidence": confidence,
        }

    def _run(
        self,
        name: str,
        lines: list[dict[str, object]],
        *,
        status: str = "completed",
        warnings: list[str] | None = None,
        error: str | None = None,
        cache_hit: bool = False,
        asset: dict[str, object] | None = None,
    ) -> dict[str, object]:
        selected_asset = asset or self.asset
        run: dict[str, object] = {
            "run_id": "ocr_run_" + "0" * 24,
            "engine": validator.expected_engine(name),
            "config": copy.deepcopy(validator.EXPECTED_CONFIGS[name]),
            "status": status,
            "lines": copy.deepcopy(lines),
            "warnings": warnings or [],
            "error": error,
            "hashes": {
                "input_sha256": validator.engine_input_sha256(selected_asset),
                "output_sha256": "0" * 64,
                "signature_sha256": "0" * 64,
            },
            "provenance": {
                "runner": validator.ENGINE_RUNNERS[name],
                "runner_version": validator.RUNNER_VERSION,
                "generated_at": STAMP,
                "inference_generated_at": STAMP,
                "cache_hit": cache_hit,
                "question_independent": True,
            },
        }
        run["hashes"]["output_sha256"] = validator.engine_output_sha256(run)
        signature = validator.engine_signature_sha256(selected_asset, run)
        run["hashes"]["signature_sha256"] = signature
        run["run_id"] = validator.expected_run_id(signature)
        return run

    def _record(
        self,
        *,
        vision_lines: list[dict[str, object]] | None = None,
        tesseract_lines: list[dict[str, object]] | None = None,
        vision_run: dict[str, object] | None = None,
        tesseract_run: dict[str, object] | None = None,
        asset: dict[str, object] | None = None,
        classification: dict[str, object] | None = None,
    ) -> dict[str, object]:
        selected_asset = asset or self.asset
        selected_classification = classification or self.classification
        if vision_lines is None:
            vision_lines = [self._line("会議は10時に開始")]
        if tesseract_lines is None:
            tesseract_lines = [self._line("会議は10時に開始", bbox=[105, 102, 590, 98], confidence=0.7)]
        runs = [
            vision_run or self._run("apple_vision", vision_lines, asset=selected_asset),
            tesseract_run or self._run("tesseract", tesseract_lines, asset=selected_asset),
        ]
        consensus = validator.build_consensus(selected_asset["asset_id"], runs)
        exactness = validator.expected_exactness(consensus, runs)
        status = validator.expected_status(exactness, runs)
        input_sha = validator.sha256_json(
            validator.observation_input_payload(selected_asset, selected_classification)
        )
        signature = validator.observation_signature_sha256(input_sha, runs)
        record: dict[str, object] = {
            "schema_version": "0.1",
            "record_type": "ocr_observation",
            "observation_id": "ocr_" + signature[:24],
            "asset_id": selected_asset["asset_id"],
            "asset": validator.expected_asset_envelope(selected_asset),
            "source": selected_asset["source"],
            "origin": selected_asset["origin"],
            "classification_ref": validator.expected_classification_ref(selected_classification),
            "engine_runs": runs,
            "consensus": consensus,
            "exactness": exactness,
            "warnings": validator.expected_warnings(consensus, runs),
            "status": status,
            "hashes": {
                "input_sha256": input_sha,
                "output_sha256": "0" * 64,
                "signature_sha256": signature,
            },
            "provenance": {
                "observer": validator.OBSERVER,
                "observer_version": validator.OBSERVER_VERSION,
                "generated_at": STAMP,
                "cache_hit": all(run["provenance"]["cache_hit"] for run in runs),
                "question_independent": True,
                "evidence_connected": False,
                "search_unit_connected": False,
            },
        }
        record["hashes"]["output_sha256"] = validator.sha256_json(
            validator.observation_output_payload(record)
        )
        return record

    def _rehash_runtime_tamper(self, record: dict[str, object]) -> None:
        runs = record["engine_runs"]
        for run in runs:
            name = run["engine"]["name"]
            if name == "apple_vision":
                runtime = run["engine"]["runtime"]
                runtime["build_signature_sha256"] = (
                    validator.apple_vision_build_signature(runtime)
                )
            run["engine"]["digest"] = validator.expected_engine_digest(
                name, run["engine"]["runtime"]
            )
            run["hashes"]["output_sha256"] = validator.engine_output_sha256(run)
            signature = validator.engine_signature_sha256(self.asset, run)
            run["hashes"]["signature_sha256"] = signature
            run["run_id"] = validator.expected_run_id(signature)
        consensus = validator.build_consensus(record["asset_id"], runs)
        record["consensus"] = consensus
        record["exactness"] = validator.expected_exactness(consensus, runs)
        record["status"] = validator.expected_status(record["exactness"], runs)
        record["warnings"] = validator.expected_warnings(consensus, runs)
        signature = validator.observation_signature_sha256(
            record["hashes"]["input_sha256"], runs
        )
        record["hashes"]["signature_sha256"] = signature
        record["observation_id"] = "ocr_" + signature[:24]
        record["hashes"]["output_sha256"] = validator.sha256_json(
            validator.observation_output_payload(record)
        )

    def test_valid_record_uses_explicit_draft_2020_12_dual_engine_contract(self) -> None:
        record = self._record()
        self.assertEqual(validator.validate(record), [])
        schema = validator._load_published_schema()
        schema_validator, mode = validator._compile_published_schema(schema)
        self.assertEqual(mode, "jsonschema_draft202012_format")
        self.assertEqual(validator._schema_errors(record, schema_validator), [])
        self.assertEqual([run["engine"]["name"] for run in record["engine_runs"]], [
            "apple_vision", "tesseract",
        ])
        self.assertTrue(record["provenance"]["question_independent"])
        self.assertFalse(record["provenance"]["evidence_connected"])
        self.assertFalse(record["provenance"]["search_unit_connected"])

    def test_missing_jsonschema_fails_closed_instead_of_skipping_draft_validation(self) -> None:
        schema = validator._load_published_schema()
        real_import = builtins.__import__

        def import_without_jsonschema(
            name: str,
            globals: object = None,
            locals: object = None,
            fromlist: tuple[str, ...] = (),
            level: int = 0,
        ) -> object:
            if name == "jsonschema":
                raise ImportError("blocked for regression test")
            return real_import(name, globals, locals, fromlist, level)

        with (
            mock.patch.object(builtins, "__import__", side_effect=import_without_jsonschema),
            self.assertRaisesRegex(ValueError, "jsonschema is required"),
        ):
            validator._compile_published_schema(schema)

    def test_runtime_fingerprints_bind_actual_wrapper_os_binary_and_traineddata(self) -> None:
        vision = validator.current_engine_runtime("apple_vision")
        self.assertEqual(vision["architecture"], platform.machine())
        self.assertEqual(
            vision["compile_target"], f"{platform.machine()}-apple-macosx13.0"
        )
        self.assertEqual(
            vision["wrapper_sha256"],
            validator.sha256_file(REPOSITORY / vision["wrapper_path"]),
        )
        self.assertEqual(
            vision["build_signature_sha256"],
            validator.apple_vision_build_signature(vision),
        )
        self.assertTrue(vision["os_build"])
        tesseract = validator.current_engine_runtime("tesseract")
        self.assertEqual(
            tesseract["binary_sha256"],
            validator.sha256_file(Path(tesseract["executable_path"])),
        )
        for language in ("jpn", "eng"):
            entry = tesseract["traineddata"][language]
            self.assertEqual(entry["sha256"], validator.sha256_file(Path(entry["path"])))
            self.assertFalse(Path(entry["path"]).is_symlink())
        record = self._record()
        self.assertEqual(
            [run["engine"] for run in record["engine_runs"]],
            [validator.expected_engine(name) for name in validator.ENGINE_ORDER],
        )

    def test_coherently_rehashed_runtime_tampering_is_rejected_against_current_machine(self) -> None:
        base = self._record()
        mutations = []
        changed = copy.deepcopy(base)
        changed["engine_runs"][0]["engine"]["runtime"]["wrapper_sha256"] = "f" * 64
        mutations.append(changed)
        changed = copy.deepcopy(base)
        changed["engine_runs"][0]["engine"]["runtime"]["os_build"] = "FAKEBUILD"
        mutations.append(changed)
        changed = copy.deepcopy(base)
        other_arch = "x86_64" if platform.machine() == "arm64" else "arm64"
        changed["engine_runs"][0]["engine"]["runtime"]["architecture"] = other_arch
        changed["engine_runs"][0]["engine"]["runtime"]["compile_target"] = (
            f"{other_arch}-apple-macosx13.0"
        )
        mutations.append(changed)
        changed = copy.deepcopy(base)
        changed["engine_runs"][1]["engine"]["runtime"]["binary_sha256"] = "e" * 64
        mutations.append(changed)
        changed = copy.deepcopy(base)
        changed["engine_runs"][1]["engine"]["runtime"]["traineddata"]["jpn"]["path"] = (
            "/tmp/fake-jpn.traineddata"
        )
        changed["engine_runs"][1]["engine"]["runtime"]["traineddata"]["jpn"]["sha256"] = (
            "d" * 64
        )
        mutations.append(changed)
        for index, record in enumerate(mutations):
            with self.subTest(index=index):
                self._rehash_runtime_tamper(record)
                self.assertEqual(validator.validate(record), [])
                write_jsonl(self.observations_path, [record])
                with self.assertRaisesRegex(ValueError, "runtime fingerprint"):
                    validator.validate_jsonl(
                        self.observations_path,
                        self.assets_path,
                        self.classifications_path,
                        asset_root=self.root,
                        expected_count=1,
                    )

    def test_declared_engine_digest_and_malformed_lines_are_rejected_without_crash(self) -> None:
        changed = self._record()
        changed["engine_runs"][0]["engine"]["digest"] = "f" * 64
        self.assertTrue(any("digest" in error for error in validator.validate(changed)))
        malformed = self._record()
        del malformed["engine_runs"][0]["lines"][0]["raw_text"]
        errors = validator.validate(malformed)
        self.assertTrue(any("raw_text" in error for error in errors))
        self.assertTrue(any("consensus" in error for error in errors))
        bad_mime = self._record()
        bad_mime["asset"]["mime_type"] = "image/"
        self.assertTrue(any("mime_type" in error for error in validator.validate(bad_mime)))

    def test_strict_consensus_allows_only_nfc_equivalence(self) -> None:
        nfc = "café"
        nfd = "cafe\u0301"
        record = self._record(
            vision_lines=[self._line(nfc)],
            tesseract_lines=[self._line(nfd, bbox=[110, 100, 590, 100])],
        )
        self.assertEqual(record["exactness"], "observed")
        self.assertEqual(record["consensus"]["lines"][0]["text"], nfc)
        self.assertEqual(
            [reading["raw_text"] for reading in record["consensus"]["lines"][0]["readings"]],
            [nfc, nfd],
        )
        for different in ("cafe", "café。", "CAFÉ", "１件", " é"):
            with self.subTest(different=different):
                changed = self._record(
                    vision_lines=[self._line("é" if different != "１件" else "1件")],
                    tesseract_lines=[self._line(different)],
                )
                self.assertEqual(changed["exactness"], "unresolved")
                self.assertIsNone(changed["consensus"]["lines"][0]["text"])

    def test_single_reading_and_high_confidence_never_become_observed(self) -> None:
        record = self._record(
            vision_lines=[self._line("主キー", confidence=1.0)],
            tesseract_lines=[self._line("主キー", bbox=[100, 800, 600, 100], confidence=1.0)],
        )
        self.assertEqual(record["exactness"], "unresolved")
        self.assertEqual(record["status"], "needs_review")
        self.assertEqual(record["consensus"]["unresolved_count"], 2)
        self.assertTrue(all(len(line["readings"]) == 1 for line in record["consensus"]["lines"]))

    def test_needs_review_engine_blocks_exact_match_promotion(self) -> None:
        vision = self._run("apple_vision", [self._line("倒産")])
        tesseract = self._run(
            "tesseract",
            [self._line("倒産")],
            status="needs_review",
            warnings=["txt/TSV line count mismatch"],
            error="strict one-to-one alignment failed",
        )
        record = self._record(vision_run=vision, tesseract_run=tesseract)
        self.assertEqual(record["exactness"], "unresolved")
        self.assertEqual(record["status"], "needs_review")
        self.assertIn("tesseract: txt/TSV line count mismatch", record["warnings"])
        self.assertEqual(validator.validate(record), [])

    def test_empty_outputs_are_explicitly_unresolved(self) -> None:
        record = self._record(vision_lines=[], tesseract_lines=[])
        self.assertEqual(record["exactness"], "unresolved")
        self.assertEqual(record["status"], "needs_review")
        self.assertEqual(record["consensus"]["lines"], [])
        self.assertEqual(record["warnings"], ["consensus contains no lines"])
        self.assertEqual(validator.validate(record), [])

    def test_failed_engines_have_no_lines_and_make_record_failed(self) -> None:
        vision = self._run(
            "apple_vision", [], status="failed", error="Vision unavailable"
        )
        tesseract = self._run(
            "tesseract", [], status="failed", error="traineddata unavailable"
        )
        record = self._record(vision_run=vision, tesseract_run=tesseract)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["exactness"], "unresolved")
        self.assertEqual(validator.validate(record), [])

    def test_tampering_with_contract_hashes_ids_or_consensus_is_rejected(self) -> None:
        base = self._record()
        mutations = []
        changed = copy.deepcopy(base)
        changed["engine_runs"][0]["config"]["uses_language_correction"] = False
        mutations.append((changed, "config"))
        changed = copy.deepcopy(base)
        changed["engine_runs"][1]["engine"]["version"] = "tesseract-9.9.9"
        mutations.append((changed, "engine"))
        changed = copy.deepcopy(base)
        changed["engine_runs"][0]["lines"][0]["raw_text"] = "改ざん"
        mutations.append((changed, "output_sha256"))
        changed = copy.deepcopy(base)
        changed["engine_runs"][0]["run_id"] = "ocr_run_" + "f" * 24
        mutations.append((changed, "run_id"))
        changed = copy.deepcopy(base)
        changed["consensus"]["lines"][0]["text"] = "改ざん"
        mutations.append((changed, "consensus"))
        changed = copy.deepcopy(base)
        changed["hashes"]["output_sha256"] = "f" * 64
        mutations.append((changed, "output_sha256"))
        changed = copy.deepcopy(base)
        changed["observation_id"] = "ocr_" + "f" * 24
        mutations.append((changed, "observation_id"))
        for record, expected in mutations:
            with self.subTest(expected=expected):
                self.assertTrue(any(expected in error for error in validator.validate(record)))

    def test_invalid_bbox_line_order_confidence_extra_and_connections_are_rejected(self) -> None:
        base = self._record()
        cases = []
        changed = copy.deepcopy(base)
        changed["engine_runs"][0]["lines"][0]["bbox"] = [900, 0, 101, 10]
        cases.append((changed, "bbox"))
        changed = copy.deepcopy(base)
        changed["engine_runs"][0]["lines"][0]["sequence"] = 2
        cases.append((changed, "sequence"))
        changed = copy.deepcopy(base)
        changed["engine_runs"][0]["lines"][0]["confidence"] = True
        cases.append((changed, "confidence"))
        changed = copy.deepcopy(base)
        changed["unexpected_evidence"] = {}
        cases.append((changed, "unknown root keys"))
        changed = copy.deepcopy(base)
        changed["provenance"]["evidence_connected"] = True
        cases.append((changed, "evidence_connected"))
        changed = copy.deepcopy(base)
        changed["provenance"]["search_unit_connected"] = True
        cases.append((changed, "search_unit_connected"))
        for record, expected in cases:
            with self.subTest(expected=expected):
                self.assertTrue(any(expected in error for error in validator.validate(record)))

    def test_validate_jsonl_strictly_binds_asset_classification_image_and_order(self) -> None:
        record = self._record()
        write_jsonl(self.observations_path, [record])
        stats = validator.validate_jsonl(
            self.observations_path,
            self.assets_path,
            self.classifications_path,
            asset_root=self.root,
            expected_count=1,
        )
        self.assertEqual(stats["records"], 1)
        self.assertEqual(stats["eligible"], 1)
        self.assertEqual(stats["observed"], 1)
        self.assertEqual(stats["engines"], ["apple_vision", "tesseract"])

        raw2, asset2, classification2 = self._make_upstream(
            "2" * 32, "asset2.png", make_png((90, 70))
        )
        record2 = self._record(asset=asset2, classification=classification2)
        write_jsonl(self.assets_path, [self.raw_asset, raw2])
        write_jsonl(self.classifications_path, [self.classification, classification2])
        write_jsonl(self.observations_path, [record2, record])
        with self.assertRaisesRegex(ValueError, "asset_id/order mismatch"):
            validator.validate_jsonl(
                self.observations_path,
                self.assets_path,
                self.classifications_path,
                asset_root=self.root,
                expected_count=2,
            )

    def test_validate_jsonl_rejects_empty_extra_missing_and_wrong_expected_count(self) -> None:
        self.observations_path.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "contains no records"):
            validator.validate_jsonl(
                self.observations_path, self.assets_path, self.classifications_path,
                asset_root=self.root, expected_count=1,
            )
        write_jsonl(self.observations_path, [self._record(), self._record()])
        with self.assertRaisesRegex(ValueError, "record count mismatch"):
            validator.validate_jsonl(
                self.observations_path, self.assets_path, self.classifications_path,
                asset_root=self.root, expected_count=1,
            )
        write_jsonl(self.observations_path, [self._record()])
        with self.assertRaisesRegex(ValueError, "eligible OCR asset count mismatch"):
            validator.validate_jsonl(
                self.observations_path, self.assets_path, self.classifications_path,
                asset_root=self.root, expected_count=10,
            )

    def test_validate_jsonl_rejects_non_ocr_extra_record(self) -> None:
        raw2, _asset2, classification2 = self._make_upstream(
            "2" * 32,
            "chart.png",
            make_png((60, 40)),
            routes=["chart_source_recovery"],
        )
        write_jsonl(self.assets_path, [self.raw_asset, raw2])
        write_jsonl(self.classifications_path, [self.classification, classification2])
        write_jsonl(self.observations_path, [self._record(), self._record()])
        with self.assertRaisesRegex(ValueError, "record count mismatch"):
            validator.validate_jsonl(
                self.observations_path, self.assets_path, self.classifications_path,
                asset_root=self.root, expected_count=1,
            )

    def test_validate_jsonl_rejects_symlink_and_path_escape(self) -> None:
        target = self.images / "target.png"
        target.write_bytes(make_png())
        link = self.images / "link.png"
        os.symlink(target, link)
        raw, asset, classification = self._make_upstream(
            "3" * 32,
            "unused.png",
            target.read_bytes(),
            materialized_path=str(link.relative_to(self.root)),
        )
        record = self._record(asset=asset, classification=classification)
        write_jsonl(self.assets_path, [raw])
        write_jsonl(self.classifications_path, [classification])
        write_jsonl(self.observations_path, [record])
        with self.assertRaisesRegex(ValueError, "symlink"):
            validator.validate_jsonl(
                self.observations_path, self.assets_path, self.classifications_path,
                asset_root=self.root, expected_count=1,
            )

        outside = self.root.parent / (self.root.name + "-outside.png")
        try:
            outside.write_bytes(make_png())
            escaped = copy.deepcopy(raw)
            escaped["materialized_path"] = "../" + outside.name
            write_jsonl(self.assets_path, [escaped])
            with self.assertRaisesRegex(ValueError, "inside asset_root|escapes asset_root"):
                validator.validate_jsonl(
                    self.observations_path, self.assets_path, self.classifications_path,
                    asset_root=self.root, expected_count=1,
                )
        finally:
            outside.unlink(missing_ok=True)

    def test_validate_jsonl_rejects_image_hash_change_full_decode_failure_and_50mp(self) -> None:
        write_jsonl(self.observations_path, [self._record()])
        (self.images / "asset.png").write_bytes(make_png((101, 80)))
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            validator.validate_jsonl(
                self.observations_path, self.assets_path, self.classifications_path,
                asset_root=self.root, expected_count=1,
            )

        for size, expected in [((100, 80), "fully decode|verify materialized"), ((8000, 7000), "pixel safety limit")]:
            with self.subTest(size=size):
                broken = png_skeleton_bytes(*size)
                raw, asset, classification = self._make_upstream(
                    "4" * 32,
                    "broken.png",
                    broken,
                    materialized_path="images/broken.png",
                )
                (self.images / "broken.png").write_bytes(broken)
                record = self._record(asset=asset, classification=classification)
                write_jsonl(self.assets_path, [raw])
                write_jsonl(self.classifications_path, [classification])
                write_jsonl(self.observations_path, [record])
                with self.assertRaisesRegex(ValueError, expected):
                    validator.validate_jsonl(
                        self.observations_path, self.assets_path, self.classifications_path,
                        asset_root=self.root, expected_count=1,
                    )

    def test_cache_provenance_must_equal_both_engine_cache_hits(self) -> None:
        record = self._record()
        record["provenance"]["cache_hit"] = True
        self.assertTrue(any("cache_hit" in error for error in validator.validate(record)))
        vision = self._run("apple_vision", [self._line("主キー")], cache_hit=True)
        tess = self._run("tesseract", [self._line("主キー")], cache_hit=True)
        cached = self._record(vision_run=vision, tesseract_run=tess)
        self.assertTrue(cached["provenance"]["cache_hit"])
        self.assertEqual(validator.validate(cached), [])

    @staticmethod
    def _one_line_tsv(text: str = "主キー", confidence: str = "96.5") -> str:
        return (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
            "left\ttop\twidth\theight\tconf\ttext\n"
            f"5\t1\t1\t1\t1\t1\t10\t8\t50\t10\t{confidence}\t{text}\n"
        )

    def test_extractor_preserves_raw_whitespace_and_flags_tesseract_mismatch(self) -> None:
        raw = "  カテゴリ列は見つかりませんでした  "
        standardized = extractor._standard_line(1, raw, [100, 100, 500, 125], 0.9)
        self.assertEqual(standardized["raw_text"], raw)

        status, lines, warnings, error = extractor.parse_tesseract_outputs(
            raw + "\n",
            self._one_line_tsv(),
            {"width_px": 100, "height_px": 80},
        )
        self.assertEqual(status, "completed")
        self.assertEqual(lines[0]["raw_text"], raw)
        self.assertEqual(lines[0]["confidence"], 0.965)
        self.assertEqual(warnings, [])
        self.assertIsNone(error)

        mismatch = extractor.parse_tesseract_outputs(
            "主キー\n外部キー\n",
            self._one_line_tsv(),
            {"width_px": 100, "height_px": 80},
        )
        self.assertEqual(mismatch[0], "needs_review")
        self.assertEqual([line["raw_text"] for line in mismatch[1]], ["主キー"])
        self.assertTrue(any("alignment mismatch" in warning for warning in mismatch[2]))

    def test_extractor_cache_reuse_and_input_or_runtime_change_causes_miss(self) -> None:
        cache = self.root / "cache"
        cache.mkdir()
        result = ("completed", [self._line("主キー")], [], None)
        fake_vision = self.root / "vision-helper"
        fake_tesseract = self.root / "tesseract"
        with mock.patch.object(
            extractor, "run_apple_vision_raw", return_value=result
        ) as raw_runner:
            first = extractor.run_or_cache_engine(
                "apple_vision",
                self.asset,
                b"image-bytes",
                cache,
                restart=False,
                vision_binary=fake_vision,
                tesseract=fake_tesseract,
                timeout=1,
            )
            second = extractor.run_or_cache_engine(
                "apple_vision",
                self.asset,
                b"image-bytes",
                cache,
                restart=False,
                vision_binary=fake_vision,
                tesseract=fake_tesseract,
                timeout=1,
            )
            self.assertFalse(first["provenance"]["cache_hit"])
            self.assertTrue(second["provenance"]["cache_hit"])
            self.assertEqual(raw_runner.call_count, 1)

            changed_asset = copy.deepcopy(self.asset)
            changed_asset["sha256"] = "b" * 64
            extractor.run_or_cache_engine(
                "apple_vision",
                changed_asset,
                b"changed-image-bytes",
                cache,
                restart=False,
                vision_binary=fake_vision,
                tesseract=fake_tesseract,
                timeout=1,
            )
            self.assertEqual(raw_runner.call_count, 2)

            changed_runtime = copy.deepcopy(
                validator.current_engine_runtime("apple_vision")
            )
            changed_runtime["os_build"] += "-changed"
            with mock.patch.object(
                extractor.contract,
                "current_engine_runtime",
                return_value=changed_runtime,
            ):
                runtime_miss = extractor.run_or_cache_engine(
                    "apple_vision",
                    self.asset,
                    b"image-bytes",
                    cache,
                    restart=False,
                    vision_binary=fake_vision,
                    tesseract=fake_tesseract,
                    timeout=1,
                )
            self.assertFalse(runtime_miss["provenance"]["cache_hit"])
            self.assertEqual(raw_runner.call_count, 3)

    def test_extractor_rejects_engine_and_build_metadata_symlink_caches(self) -> None:
        cache = self.root / "symlink-cache"
        cache.mkdir()
        outside = self.root / "outside-engine-cache"
        outside.mkdir()
        os.symlink(outside, cache / "apple_vision")
        with self.assertRaisesRegex(ValueError, "symlink"):
            extractor.run_or_cache_engine(
                "apple_vision",
                self.asset,
                b"image-bytes",
                cache,
                restart=True,
                vision_binary=self.root / "vision-helper",
                tesseract=self.root / "tesseract",
                timeout=1,
            )

        source = extractor.VISION_SOURCE
        build = self.root / "vision-build"
        build.mkdir()
        runtime = validator.current_engine_runtime("apple_vision")
        swiftc_version = runtime["swiftc_version"]
        signature = runtime["build_signature_sha256"]
        binary = build / f"apple_vision_ocr-{signature[:24]}"
        binary.write_bytes(b"test helper")
        binary.chmod(0o755)
        external_metadata = self.root / "external-metadata.json"
        external_metadata.write_text("{}", encoding="utf-8")
        os.symlink(external_metadata, binary.with_suffix(".json"))
        version_process = mock.Mock(
            returncode=0, stdout=swiftc_version, stderr=""
        )
        with (
            mock.patch.object(extractor.platform, "system", return_value="Darwin"),
            mock.patch.object(extractor.shutil, "which", return_value="/usr/bin/xcrun"),
            mock.patch.object(extractor.subprocess, "run", return_value=version_process),
            self.assertRaisesRegex(ValueError, "symlink"),
        ):
            extractor.compile_vision_helper(source, build, timeout=1)

        metadata = binary.with_suffix(".json")
        metadata.unlink()
        original_binary_sha = hashlib.sha256(binary.read_bytes()).hexdigest()
        metadata.write_text(
            json.dumps(
                {
                    "build_signature": signature,
                    "binary_sha256": original_binary_sha,
                }
            ),
            encoding="utf-8",
        )
        binary.write_bytes(b"regular-file tampering")
        binary.chmod(0o755)
        with (
            mock.patch.object(extractor.platform, "system", return_value="Darwin"),
            mock.patch.object(extractor.shutil, "which", return_value="/usr/bin/xcrun"),
            mock.patch.object(extractor.subprocess, "run", return_value=version_process),
            self.assertRaisesRegex(ValueError, "metadata or binary hash mismatch"),
        ):
            extractor.compile_vision_helper(source, build, timeout=1)

    def test_extractor_failures_become_failed_runs_and_failed_record(self) -> None:
        cache = self.root / "failure-cache"
        cache.mkdir()
        with (
            mock.patch.object(
                extractor, "run_apple_vision_raw", side_effect=RuntimeError("Vision down")
            ),
            mock.patch.object(
                extractor, "run_tesseract_raw", side_effect=RuntimeError("Tess down")
            ),
        ):
            vision = extractor.run_or_cache_engine(
                "apple_vision",
                self.asset,
                b"image-bytes",
                cache,
                restart=False,
                vision_binary=self.root / "vision-helper",
                tesseract=self.root / "tesseract",
                timeout=1,
            )
            tesseract = extractor.run_or_cache_engine(
                "tesseract",
                self.asset,
                b"image-bytes",
                cache,
                restart=False,
                vision_binary=self.root / "vision-helper",
                tesseract=self.root / "tesseract",
                timeout=1,
            )
        self.assertEqual(vision["status"], "failed")
        self.assertEqual(tesseract["status"], "failed")
        self.assertEqual(vision["lines"], [])
        self.assertEqual(tesseract["lines"], [])
        record = extractor.observation_record(
            self.asset, self.classification, [vision, tesseract]
        )
        self.assertEqual(record["status"], "failed")
        self.assertEqual(validator.validate(record), [])
        with self.assertRaisesRegex(ValueError, "Apple Vision then Tesseract"):
            extractor.observation_record(
                self.asset, self.classification, [tesseract, vision]
            )

    def test_extractor_filters_exact_ocr_route_and_rejects_upstream_reordering(self) -> None:
        _raw2, asset2, classification2 = self._make_upstream(
            "2" * 32,
            "chart.png",
            make_png((60, 40)),
            routes=["chart_source_recovery"],
        )
        eligible = extractor.eligible_inputs(
            [self.asset, asset2], [self.classification, classification2]
        )
        self.assertEqual(
            [(asset["asset_id"], classification["asset_id"]) for asset, classification in eligible],
            [(self.asset["asset_id"], self.classification["asset_id"])],
        )
        with self.assertRaisesRegex(ValueError, "order mismatch"):
            extractor.eligible_inputs(
                [self.asset, asset2], [classification2, self.classification]
            )

    def test_pdf_ocr_required_review_fallback_is_guarded_and_valid_end_to_end(self) -> None:
        fallback_raw, fallback_asset, fallback_classification = self._make_upstream(
            "2" * 32,
            "pdf-page.png",
            make_png((70, 50)),
            routes=["review"],
            origin_kind="pdf_page",
            processing_layers=["native_text", "ocr_required"],
        )
        fallback_record = self._record(
            asset=fallback_asset, classification=fallback_classification
        )

        self.assertEqual(
            fallback_record["classification_ref"]["routes"], ["review"]
        )
        self.assertEqual(
            fallback_record["hashes"]["input_sha256"],
            validator.sha256_json(
                validator.observation_input_payload(
                    fallback_asset, fallback_classification
                )
            ),
        )
        self.assertTrue(
            validator.is_ocr_eligible(
                fallback_asset["source"],
                fallback_asset["origin"],
                fallback_classification["routes"],
            )
        )
        self.assertEqual(validator.validate(fallback_record), [])
        schema = validator._load_published_schema()
        schema_validator, mode = validator._compile_published_schema(schema)
        self.assertEqual(mode, "jsonschema_draft202012_format")
        self.assertEqual(
            validator._schema_errors(fallback_record, schema_validator), []
        )

        write_jsonl(self.assets_path, [fallback_raw])
        write_jsonl(self.classifications_path, [fallback_classification])
        write_jsonl(self.observations_path, [fallback_record])
        stats = validator.validate_jsonl(
            self.observations_path,
            self.assets_path,
            self.classifications_path,
            asset_root=self.root,
            expected_count=1,
        )
        self.assertEqual(stats["eligible"], 1)
        self.assertEqual(stats["records"], 1)

        guarded_cases = [
            (
                {"document_type": "pdf", "processing_layers": ["ocr_required"]},
                {"kind": "standalone_image", "page_number": None},
            ),
            (
                {"document_type": "pdf", "processing_layers": ["native_text"]},
                {"kind": "pdf_page", "page_number": 1},
            ),
            (
                {"document_type": "pdf", "processing_layers": "ocr_required"},
                {"kind": "pdf_page", "page_number": 1},
            ),
            (
                {"processing_layers": ["ocr_required"]},
                {"kind": "pdf_page", "page_number": 1},
            ),
            (
                {"document_type": "pdf", "processing_layers": ["ocr_required"]},
                {"kind": "pdf_page", "page_number": None},
            ),
        ]
        for source, origin in guarded_cases:
            with self.subTest(source=source, origin=origin):
                self.assertFalse(
                    validator.is_ocr_eligible(source, origin, ["review"])
                )
        self.assertFalse(
            validator.is_ocr_eligible(
                {"document_type": "pdf", "processing_layers": ["ocr_required"]},
                {"kind": "pdf_page", "page_number": 1},
                ["review", "chart_source_recovery"],
            )
        )

    def test_extractor_includes_only_guarded_review_fallback(self) -> None:
        _fallback_raw, fallback_asset, fallback_classification = self._make_upstream(
            "2" * 32,
            "fallback.png",
            make_png((70, 50)),
            routes=["review"],
            origin_kind="pdf_page",
            processing_layers=["ocr_required"],
        )
        _plain_raw, plain_asset, plain_classification = self._make_upstream(
            "3" * 32,
            "plain-review.png",
            make_png((75, 55)),
            routes=["review"],
        )
        eligible = extractor.eligible_inputs(
            [self.asset, fallback_asset, plain_asset],
            [self.classification, fallback_classification, plain_classification],
        )
        self.assertEqual(
            [asset["asset_id"] for asset, _classification in eligible],
            [self.asset["asset_id"], fallback_asset["asset_id"]],
        )
        plain_record = self._record(
            asset=plain_asset, classification=plain_classification
        )
        self.assertTrue(
            any("ocr_required" in error for error in validator.validate(plain_record))
        )
        schema = validator._load_published_schema()
        schema_validator, _mode = validator._compile_published_schema(schema)
        self.assertTrue(validator._schema_errors(plain_record, schema_validator))

    def test_production_extractor_rejects_engine_source_and_binary_overrides(self) -> None:
        output = self.root / "output.jsonl"
        alternate_source = self.root / "alternate.swift"
        alternate_source.write_text("import Vision\n", encoding="utf-8")
        cases = [
            (
                {"vision_binary": self.root / "alternate-helper"},
                "does not accept a precompiled Vision binary",
            ),
            (
                {"vision_source": alternate_source},
                "does not accept an alternate Vision source",
            ),
            (
                {"tesseract_command": "/bin/true"},
                "only the Tesseract executable resolved by PATH",
            ),
        ]
        for kwargs, expected in cases:
            with self.subTest(expected=expected), self.assertRaisesRegex(
                ValueError, expected
            ):
                extractor.extract_file(
                    self.assets_path,
                    self.classifications_path,
                    output,
                    asset_root=self.root,
                    **kwargs,
                )

    def test_raw_ocr_subprocesses_receive_only_image_bytes_and_fixed_engine_args(self) -> None:
        image_bytes = b"private image bytes only"
        dimensions = {"width_px": 100, "height_px": 80}
        vision_payload = {
            "status": "completed",
            "runner": validator.ENGINE_RUNNERS["apple_vision"],
            "runner_version": validator.RUNNER_VERSION,
            "request_revision": validator.APPLE_VISION_CONFIG["request_revision"],
            "width_px": 100,
            "height_px": 80,
            "lines": [
                {
                    "sequence": 1,
                    "raw_text": "主キー",
                    "bbox": [100, 100, 500, 125],
                    "confidence": 0.9,
                }
            ],
            "warnings": [],
        }
        vision_process = mock.Mock(
            returncode=0,
            stdout=json.dumps(vision_payload, ensure_ascii=False).encode("utf-8"),
            stderr=b"",
        )
        with mock.patch.object(
            extractor.subprocess, "run", return_value=vision_process
        ) as subprocess_run:
            extractor.run_apple_vision_raw(
                Path("/fixed/apple-vision-helper"),
                image_bytes,
                dimensions,
                timeout=1,
            )
        vision_call = subprocess_run.call_args
        self.assertEqual(vision_call.args[0], ["/fixed/apple-vision-helper"])
        self.assertEqual(vision_call.kwargs["input"], image_bytes)

        captured_tesseract: dict[str, object] = {}

        def tesseract_process(command: list[str], **kwargs: object) -> mock.Mock:
            captured_tesseract["command"] = command
            captured_tesseract["input"] = kwargs.get("input")
            output_base = Path(command[2])
            output_base.with_suffix(".txt").write_text("主キー\n", encoding="utf-8")
            output_base.with_suffix(".tsv").write_text(
                self._one_line_tsv(), encoding="utf-8"
            )
            return mock.Mock(returncode=0, stdout=b"", stderr=b"")

        with mock.patch.object(
            extractor.subprocess, "run", side_effect=tesseract_process
        ):
            extractor.run_tesseract_raw(
                Path("/fixed/tesseract"), image_bytes, dimensions, timeout=1
            )
        self.assertEqual(captured_tesseract["input"], image_bytes)
        command_text = " ".join(captured_tesseract["command"])
        self.assertIn("-l jpn+eng --oem 1 --psm 3", command_text)
        self.assertIn("preserve_interword_spaces=1", command_text)
        for forbidden in (
            self.asset["asset_id"],
            self.asset["declared_path"],
            "ocr_text",
            "question",
        ):
            self.assertNotIn(forbidden, command_text)

    def test_jsonl_and_encoded_image_size_limits_apply_before_materialization(self) -> None:
        oversized_jsonl = self.root / "oversized.jsonl"
        with oversized_jsonl.open("wb") as handle:
            handle.seek(validator.MAX_JSONL_BYTES)
            handle.write(b"\n")
        with self.assertRaisesRegex(ValueError, "byte safety limit"):
            validator.validate_jsonl(
                oversized_jsonl,
                self.assets_path,
                self.classifications_path,
                asset_root=self.root,
                expected_count=1,
            )

        huge_image = self.images / "asset.png"
        with huge_image.open("wb") as handle:
            handle.seek(validator.MAX_IMAGE_BYTES)
            handle.write(b"x")
        write_jsonl(self.observations_path, [self._record()])
        with self.assertRaisesRegex(ValueError, "encoded byte safety limit"):
            validator.validate_jsonl(
                self.observations_path,
                self.assets_path,
                self.classifications_path,
                asset_root=self.root,
                expected_count=1,
            )


if __name__ == "__main__":
    unittest.main()
