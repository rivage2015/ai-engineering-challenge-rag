from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_pdf_page_evidence as adapter
import build_pdf_page_observations as pdf_observations
import build_search_units as search_units
from probe_intermediate_records import canonical_json, content, digest_file, stable_id
import validate_pdf_page_evidence as overlay_validator


RUN_AT = "2026-08-15T00:00:00+00:00"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(canonical_json(record) + "\n" for record in records),
        encoding="utf-8",
    )


def shard_metadata(path: Path, base: Path, count: int) -> dict[str, object]:
    return {
        "relative_path": path.relative_to(base).as_posix(),
        "sha256": digest_file(path),
        "size_bytes": path.stat().st_size,
        "record_count": count,
    }


class PDFPageEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary_root = Path(tempfile.gettempdir()).resolve()
        self.temporary = tempfile.TemporaryDirectory(
            prefix="aiec-pdf-evidence-", dir=temporary_root
        )
        self.work = Path(self.temporary.name).resolve()
        self.relative_path = "fixtures/sample.pdf"
        self.source_sha = SHA_A
        self.document_id = stable_id(
            "doc",
            {
                "relative_path": self.relative_path,
                "source_sha256": self.source_sha,
            },
        )
        self.row = {
            "file_id": "file_" + "1" * 32,
            "document_id": self.document_id,
            "relative_path": self.relative_path,
            "source_sha256": self.source_sha,
            "size_bytes": 1234,
            "page_count": 3,
        }
        self.observations = [
            self._native_record(),
            self._ocr_record(
                page_number=2,
                texts=("確認済み", "確認済み"),
                exact=True,
            ),
            self._ocr_record(
                page_number=3,
                texts=("金額 100", "金額 1OO"),
                exact=False,
            ),
        ]
        self.observations_path = self.work / "pdf-page-observations.jsonl"
        write_jsonl(self.observations_path, self.observations)
        self.base = self.work / "base-intermediate"
        self._write_base_intermediate()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _build_record(
        self,
        page_number: int,
        words: list[dict[str, object]],
        *,
        manifests: list[dict[str, object]] | None = None,
        observations: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        return pdf_observations.build_record(
            repository_root=REPOSITORY,
            row=self.row,
            page_number=page_number,
            page_data={
                "page_number": page_number,
                "width_pt": 612.0,
                "height_pt": 792.0,
                "page_output_sha256": SHA_B,
                "words": words,
            },
            pdfinfo_data={
                "width_pt": 612.0,
                "height_pt": 792.0,
                "rotation_degrees": 0,
            },
            pdfinfo_output_sha256=SHA_C,
            pdftotext_output_sha256=SHA_D,
            pdftotext_version="pdftotext fixture",
            pdftotext_binary_sha256=SHA_A,
            inventory_sha256=SHA_B,
            visual_assets_sha256=SHA_C,
            ocr_observations_sha256=SHA_D,
            pdfinfo_version="pdfinfo fixture",
            pdfinfo_binary_sha256=SHA_B,
            manifests=manifests or [],
            observations=observations or [],
        )

    def _native_record(self) -> dict[str, object]:
        return self._build_record(
            1,
            [
                {
                    "word_id": "word_000001",
                    "reading_order": 1,
                    "block_index": 1,
                    "line_index": 1,
                    "word_index": 1,
                    "raw_text": "原文ＡＢＣ",
                    "bbox": [72.0, 80.0, 60.0, 12.0],
                }
            ],
        )

    def _ocr_record(
        self,
        *,
        page_number: int,
        texts: tuple[str, str],
        exact: bool,
    ) -> dict[str, object]:
        asset_digit = str(page_number)
        manifest = {
            "asset_id": "asset_" + asset_digit * 32,
            "materialized_path": f"artifacts/fixture-{page_number}.png",
            "materialization": {
                "sha256": SHA_C,
                "mime_type": "image/png",
                "width_px": 100,
                "height_px": 200,
            },
        }
        run_ids = (
            "ocr_run_" + str(page_number * 2) * 24,
            "ocr_run_" + str(page_number * 2 + 1) * 24,
        )
        readings: list[dict[str, object]] = []
        engine_runs: list[dict[str, object]] = []
        for index, (run_id, raw_text) in enumerate(zip(run_ids, texts), start=1):
            readings.append(
                {"run_id": run_id, "line_id": "line_1", "raw_text": raw_text}
            )
            engine_runs.append(
                {
                    "run_id": run_id,
                    "engine": {
                        "name": f"engine-{index}",
                        "version": "1",
                        "digest": SHA_A if index == 1 else SHA_B,
                        "independence_group": f"group-{index}",
                    },
                    "status": "completed",
                    "lines": [
                        {
                            "line_id": "line_1",
                            "sequence": 1,
                            "raw_text": raw_text,
                            "bbox": [100, 100, 300, 50],
                            "confidence": 0.9 if index == 1 else 0.8,
                        }
                    ],
                    "warnings": [],
                    "error": None,
                    "hashes": {
                        "output_sha256": SHA_B if index == 1 else SHA_C
                    },
                }
            )
        upstream = {
            "observation_id": "ocr_" + str(page_number + 4) * 24,
            "status": "observed" if exact else "needs_review",
            "exactness": "observed" if exact else "unresolved",
            "hashes": {"signature_sha256": SHA_D},
            "engine_runs": engine_runs,
            "consensus": {
                "lines": [
                    {
                        "consensus_line_id": "ocr_line_" + str(page_number) * 16,
                        "exactness": "observed" if exact else "unresolved",
                        "bbox": [100, 100, 300, 50],
                        "text": texts[0] if exact else None,
                        "readings": readings,
                    }
                ]
            },
        }
        fake_image = REPOSITORY / "artifacts" / f"fixture-{page_number}.png"
        with (
            mock.patch.object(
                pdf_observations, "_manifest_binding_errors", return_value=[]
            ),
            mock.patch.object(
                pdf_observations, "_ocr_binding_errors", return_value=[]
            ),
            mock.patch.object(
                pdf_observations,
                "_verify_materialized_image",
                return_value=(fake_image, {"width_px": 100, "height_px": 200}),
            ),
        ):
            return self._build_record(
                page_number,
                [],
                manifests=[manifest],
                observations=[upstream],
            )

    def _base_page_evidence(self, page_number: int) -> dict[str, object]:
        location = {"page_number": page_number}
        item_content = (
            content(raw_text="原文ＡＢＣ")
            if page_number == 1
            else content(content_ref=f"{self.relative_path}#page={page_number}")
        )
        evidence_id = stable_id(
            "ev",
            {
                "document_id": self.document_id,
                "evidence_type": "page",
                "location": location,
                "content_sha256": item_content["sha256"],
            },
        )
        return {
            "schema_version": "0.1",
            "record_type": "evidence",
            "evidence_id": evidence_id,
            "document_id": self.document_id,
            "evidence_type": "page",
            "location": location,
            "content": item_content,
            "geometry": {
                "coordinate_space": "page",
                "unit": "pt",
                "x": 0,
                "y": 0,
                "width": 612.0,
                "height": 792.0,
            },
            "ordinal": page_number,
            "native_properties": {"text_layer_present": page_number == 1},
            "provenance": {
                "extraction_method": "native_parser",
                "extractor": "intermediate-record-extractor",
                "extractor_version": "0.5.0",
                "extracted_at": RUN_AT,
                "deterministic": True,
                "confidence": 1.0,
                "warnings": [] if page_number == 1 else ["OCR deferred"],
            },
        }

    def _write_base_intermediate(self) -> None:
        shards = self.base / "shards"
        shards.mkdir(parents=True, exist_ok=True)
        document = {
            "schema_version": "0.1",
            "record_type": "document",
            "document_id": self.document_id,
            "source": {
                "relative_path": self.relative_path,
                "file_name": "sample.pdf",
                "extension": "pdf",
                "media_type": "application/pdf",
                "size_bytes": 1234,
                "sha256": self.source_sha,
                "modified_at": RUN_AT,
            },
            "extraction": {
                "status": "partial",
                "parser": "pypdf",
                "parser_version": "0.5.0",
                "extracted_at": RUN_AT,
                "warnings": ["OCR deferred"],
                "errors": [],
            },
        }
        evidence = [self._base_page_evidence(number) for number in range(1, 4)]
        document_path = shards / f"{self.document_id}.documents.jsonl"
        evidence_path = shards / f"{self.document_id}.evidence.jsonl"
        write_jsonl(document_path, [document])
        write_jsonl(evidence_path, evidence)
        entry_shards = {
            "documents": shard_metadata(document_path, self.base, 1),
            "evidence": shard_metadata(evidence_path, self.base, 3),
        }
        state = {
            "state_version": "1",
            "build_status": "complete",
            "source_root": "/fixture/source",
            "extractor": "intermediate-record-extractor",
            "extractor_version": "0.5.0",
            "run_at": RUN_AT,
            "input_paths": [self.relative_path],
            "entries": {
                self.relative_path: {
                    "document_id": self.document_id,
                    "relative_path": self.relative_path,
                    "source_sha256": self.source_sha,
                    "status": "partial",
                    "shards": entry_shards,
                }
            },
            "totals": {"documents": 1, "evidence": 3, "relations": 0},
        }
        (self.base / "build-state.json").write_text(
            canonical_json(state) + "\n", encoding="utf-8"
        )

    def _read_overlay(self, output: Path) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in (output / adapter.EVIDENCE_FILE)
            .read_text(encoding="utf-8")
            .splitlines()
        ]

    def _update_output_state(self, output: Path) -> None:
        evidence_path = output / adapter.EVIDENCE_FILE
        state_path = output / adapter.STATE_FILE
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["output"]["sha256"] = digest_file(evidence_path)
        state["output"]["size_bytes"] = evidence_path.stat().st_size
        state_path.write_text(canonical_json(state) + "\n", encoding="utf-8")

    def test_build_validate_is_lossless_deterministic_and_nonsearchable(self) -> None:
        base_before = {
            path.relative_to(self.base).as_posix(): digest_file(path)
            for path in self.base.rglob("*")
            if path.is_file()
        }
        first = self.work / "first-overlay"
        second = self.work / "second-overlay"
        first_result = adapter.build(
            observations_path=self.observations_path,
            base_intermediate=self.base,
            output=first,
        )
        second_result = adapter.build(
            observations_path=self.observations_path,
            base_intermediate=self.base,
            output=second,
        )
        self.assertEqual(
            (first / adapter.EVIDENCE_FILE).read_bytes(),
            (second / adapter.EVIDENCE_FILE).read_bytes(),
        )
        self.assertEqual(
            (first / adapter.STATE_FILE).read_bytes(),
            (second / adapter.STATE_FILE).read_bytes(),
        )
        self.assertEqual(
            first_result["hashes"]["evidence_sha256"],
            second_result["hashes"]["evidence_sha256"],
        )
        validated = overlay_validator.validate(
            observations_path=self.observations_path,
            base_intermediate=self.base,
            overlay_dir=first,
            expected_count=3,
        )
        self.assertEqual(validated["counts"]["evidence_types"], {"other": 3})
        records = self._read_overlay(first)
        by_observation = {
            item["native_properties"]["pdf_page_observation_id"]: item
            for item in records
        }
        for observation in self.observations:
            evidence = by_observation[observation["observation_id"]]
            self.assertEqual(evidence["evidence_type"], "other")
            self.assertEqual(
                evidence["content"]["raw_value"],
                adapter._shadow_content(observation),
            )
            self.assertEqual(
                evidence["parent_evidence_id"],
                self._base_page_evidence(observation["page"]["page_number"])[
                    "evidence_id"
                ],
            )
        exact = records[1]["content"]["raw_value"]["ocr"]["raw_runs"]
        conflict = records[2]["content"]["raw_value"]
        self.assertEqual(
            [run["lines"][0]["raw_text"] for run in exact],
            ["確認済み", "確認済み"],
        )
        self.assertEqual(
            [run["lines"][0]["raw_text"] for run in conflict["ocr"]["raw_runs"]],
            ["金額 100", "金額 1OO"],
        )
        self.assertEqual(len(conflict["conflicts"]), 1)
        self.assertEqual(len(conflict["unresolved"]), 1)
        emitted: list[dict[str, object]] = []
        deriver = search_units.DocumentDeriver(
            self.document_id, RUN_AT, emitted.append, 500
        )
        for record in records:
            deriver.consume(record)
        self.assertEqual(deriver.finish(), {})
        self.assertEqual(emitted, [])
        with self.assertRaisesRegex(ValueError, "build-state"):
            search_units.build(first, self.work / "search", 500)
        for forbidden in adapter.FORBIDDEN_OUTPUT_NAMES:
            self.assertFalse((first / forbidden).exists())
        self.assertEqual(
            base_before,
            {
                path.relative_to(self.base).as_posix(): digest_file(path)
                for path in self.base.rglob("*")
                if path.is_file()
            },
        )

    def test_rejects_observation_hash_tamper_and_base_source_mismatch(self) -> None:
        tampered = copy.deepcopy(self.observations)
        tampered[0]["native"]["words"][0]["raw_text"] = "改ざん"
        write_jsonl(self.observations_path, tampered)
        with self.assertRaisesRegex(adapter.PDFPageEvidenceError, "invalid"):
            adapter.build(
                observations_path=self.observations_path,
                base_intermediate=self.base,
                output=self.work / "tampered-output",
            )
        self.assertFalse((self.work / "tampered-output").exists())
        mismatched = copy.deepcopy(self.observations)
        for record in mismatched:
            record["source"]["size_bytes"] = 999
            updated = pdf_observations.rehash_record(record)
            record.clear()
            record.update(updated)
            self.assertEqual(pdf_observations.validate_observation(record), [])
        write_jsonl(self.observations_path, mismatched)
        with self.assertRaisesRegex(adapter.PDFPageEvidenceError, "source binding"):
            adapter.build(
                observations_path=self.observations_path,
                base_intermediate=self.base,
                output=self.work / "mismatched-output",
            )

    def test_rejects_duplicate_base_page_even_with_coherent_shard_metadata(self) -> None:
        state_path = self.base / "build-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        metadata = state["entries"][self.relative_path]["shards"]["evidence"]
        evidence_path = self.base / metadata["relative_path"]
        records = [json.loads(line) for line in evidence_path.read_text().splitlines()]
        records.append(copy.deepcopy(records[0]))
        write_jsonl(evidence_path, records)
        state["entries"][self.relative_path]["shards"]["evidence"] = shard_metadata(
            evidence_path, self.base, 4
        )
        state_path.write_text(canonical_json(state) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(adapter.PDFPageEvidenceError, "duplicate"):
            adapter.build(
                observations_path=self.observations_path,
                base_intermediate=self.base,
                output=self.work / "duplicate-output",
            )

    def test_no_overwrite_and_validator_rejects_promotion_and_question_metadata(self) -> None:
        output = self.work / "overlay"
        adapter.build(
            observations_path=self.observations_path,
            base_intermediate=self.base,
            output=output,
        )
        original = {
            name: (output / name).read_bytes()
            for name in (adapter.EVIDENCE_FILE, adapter.STATE_FILE)
        }
        with self.assertRaisesRegex(adapter.PDFPageEvidenceError, "already exists"):
            adapter.build(
                observations_path=self.observations_path,
                base_intermediate=self.base,
                output=output,
            )
        self.assertEqual(
            original,
            {
                name: (output / name).read_bytes()
                for name in (adapter.EVIDENCE_FILE, adapter.STATE_FILE)
            },
        )
        self.assertEqual(list(output.glob("*.tmp")), [])

        promoted = self.work / "promoted-overlay"
        adapter.build(
            observations_path=self.observations_path,
            base_intermediate=self.base,
            output=promoted,
        )
        promoted_records = self._read_overlay(promoted)
        promoted_records[0]["evidence_type"] = "page"
        write_jsonl(promoted / adapter.EVIDENCE_FILE, promoted_records)
        self._update_output_state(promoted)
        with self.assertRaisesRegex(
            overlay_validator.PDFPageEvidenceValidationError,
            "evidence_type must remain 'other'",
        ):
            overlay_validator.validate(
                observations_path=self.observations_path,
                base_intermediate=self.base,
                overlay_dir=promoted,
                expected_count=3,
            )

        leaked = self.work / "leaked-overlay"
        adapter.build(
            observations_path=self.observations_path,
            base_intermediate=self.base,
            output=leaked,
        )
        leaked_records = self._read_overlay(leaked)
        leaked_records[0]["native_properties"]["question_id"] = "Q001"
        write_jsonl(leaked / adapter.EVIDENCE_FILE, leaked_records)
        self._update_output_state(leaked)
        with self.assertRaisesRegex(
            overlay_validator.PDFPageEvidenceValidationError,
            "question-layer data is forbidden",
        ):
            overlay_validator.validate(
                observations_path=self.observations_path,
                base_intermediate=self.base,
                overlay_dir=leaked,
                expected_count=3,
            )

    def test_shuffled_input_round_trips_but_coherent_raw_value_tamper_fails(self) -> None:
        shuffled_path = self.work / "shuffled-observations.jsonl"
        write_jsonl(shuffled_path, list(reversed(self.observations)))
        output = self.work / "shuffled-overlay"
        adapter.build(
            observations_path=shuffled_path,
            base_intermediate=self.base,
            output=output,
        )
        overlay_validator.validate(
            observations_path=shuffled_path,
            base_intermediate=self.base,
            overlay_dir=output,
            expected_count=3,
        )

        records = self._read_overlay(output)
        record = records[0]
        raw_value = copy.deepcopy(record["content"]["raw_value"])
        raw_value["extraction"]["selector"] = "tampered-selector"
        record["content"] = content(raw_value=raw_value)
        record["evidence_id"] = stable_id(
            "ev",
            {
                "document_id": record["document_id"],
                "evidence_type": record["evidence_type"],
                "location": record["location"],
                "content_sha256": record["content"]["sha256"],
            },
        )
        write_jsonl(output / adapter.EVIDENCE_FILE, records)
        self._update_output_state(output)
        with self.assertRaisesRegex(
            overlay_validator.PDFPageEvidenceValidationError,
            "must exactly preserve",
        ):
            overlay_validator.validate(
                observations_path=shuffled_path,
                base_intermediate=self.base,
                overlay_dir=output,
                expected_count=3,
            )

    def test_rejects_alias_bundle_and_forbidden_query_source(self) -> None:
        output = self.work / "alias-overlay"
        adapter.build(
            observations_path=self.observations_path,
            base_intermediate=self.base,
            output=output,
        )
        (output / adapter.EVIDENCE_FILE).rename(output / "evidence.jsonl")
        with self.assertRaisesRegex(
            overlay_validator.PDFPageEvidenceValidationError,
            "expected exactly one",
        ):
            overlay_validator.validate(
                observations_path=self.observations_path,
                base_intermediate=self.base,
                overlay_dir=output,
                expected_count=3,
            )

        forbidden = copy.deepcopy(self.observations)
        forbidden_path = "fixtures/questions.pdf"
        forbidden_document_id = stable_id(
            "doc",
            {
                "relative_path": forbidden_path,
                "source_sha256": self.source_sha,
            },
        )
        for record in forbidden:
            record["source"]["relative_path"] = forbidden_path
            record["document_id"] = forbidden_document_id
            updated = pdf_observations.rehash_record(record)
            record.clear()
            record.update(updated)
            self.assertEqual(pdf_observations.validate_observation(record), [])
        forbidden_observations = self.work / "source-fixture.jsonl"
        write_jsonl(forbidden_observations, forbidden)
        with self.assertRaisesRegex(
            adapter.PDFPageEvidenceError, "forbidden query-data component"
        ):
            adapter.build(
                observations_path=forbidden_observations,
                base_intermediate=self.base,
                output=self.work / "forbidden-output",
            )
        base_state_path = self.base / "build-state.json"
        base_state = json.loads(base_state_path.read_text(encoding="utf-8"))
        base_state["question_id"] = "Q001"
        base_state_path.write_text(
            canonical_json(base_state) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(
            adapter.PDFPageEvidenceError, "query-layer metadata"
        ):
            adapter.build(
                observations_path=self.observations_path,
                base_intermediate=self.base,
                output=self.work / "query-state-output",
            )

    def test_rejects_base_other_id_collision_and_publish_failure_is_invisible(self) -> None:
        base_state_path = self.base / "build-state.json"
        base_state_sha = digest_file(base_state_path)
        colliding = adapter.make_evidence(
            self.observations[0],
            self._base_page_evidence(1),
            observations_sha256=digest_file(self.observations_path),
            base_state_sha256=base_state_sha,
            run_at=RUN_AT,
        )
        state = json.loads(base_state_path.read_text(encoding="utf-8"))
        metadata = state["entries"][self.relative_path]["shards"]["evidence"]
        evidence_path = self.base / metadata["relative_path"]
        records = [json.loads(line) for line in evidence_path.read_text().splitlines()]
        records.append(colliding)
        write_jsonl(evidence_path, records)
        state["entries"][self.relative_path]["shards"]["evidence"] = shard_metadata(
            evidence_path, self.base, 4
        )
        base_state_path.write_text(canonical_json(state) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(adapter.PDFPageEvidenceError, "collides with base"):
            adapter.build(
                observations_path=self.observations_path,
                base_intermediate=self.base,
                output=self.work / "collision-output",
            )

        self._write_base_intermediate()
        interrupted = self.work / "interrupted-overlay"
        with (
            mock.patch.object(
                adapter, "_rename_noreplace", side_effect=OSError("stop")
            ),
            self.assertRaisesRegex(OSError, "stop"),
        ):
            adapter.build(
                observations_path=self.observations_path,
                base_intermediate=self.base,
                output=interrupted,
            )
        self.assertFalse(interrupted.exists())
        self.assertFalse(interrupted.with_name(f".{interrupted.name}.lock").exists())
        self.assertEqual(
            list(interrupted.parent.glob(f".{interrupted.name}.*")),
            [],
        )
        staging = self.work / "race-staging"
        target = self.work / "race-target"
        staging.mkdir()
        target.mkdir()
        (target / "sentinel").write_text("preserve", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            adapter._rename_noreplace(staging, target)
        self.assertTrue(staging.is_dir())
        self.assertEqual((target / "sentinel").read_text(encoding="utf-8"), "preserve")

    def test_rejects_sensitive_physical_paths_parent_symlinks_and_traversal(self) -> None:
        normal_output = self.work / "normal-overlay"
        adapter.build(
            observations_path=self.observations_path,
            base_intermediate=self.base,
            output=normal_output,
        )

        sensitive_root = self.work / "questions_valid"
        sensitive_root.mkdir()
        sensitive_observations = sensitive_root / self.observations_path.name
        shutil.copy2(self.observations_path, sensitive_observations)
        rejected_output = self.work / "rejected-sensitive-input"
        with self.assertRaisesRegex(
            adapter.PDFPageEvidenceError, "forbidden data component"
        ):
            adapter.build(
                observations_path=sensitive_observations,
                base_intermediate=self.base,
                output=rejected_output,
            )
        self.assertFalse(rejected_output.exists())
        with self.assertRaisesRegex(
            overlay_validator.PDFPageEvidenceValidationError,
            "physical path boundary",
        ):
            overlay_validator.validate(
                observations_path=sensitive_observations,
                base_intermediate=self.base,
                overlay_dir=normal_output,
                expected_count=3,
            )

        sensitive_base = sensitive_root / "base-intermediate"
        shutil.copytree(self.base, sensitive_base)
        with self.assertRaisesRegex(
            overlay_validator.PDFPageEvidenceValidationError,
            "physical path boundary",
        ):
            overlay_validator.validate(
                observations_path=self.observations_path,
                base_intermediate=sensitive_base,
                overlay_dir=normal_output,
                expected_count=3,
            )

        sensitive_overlay = sensitive_root / "overlay"
        shutil.copytree(normal_output, sensitive_overlay)
        with self.assertRaisesRegex(
            overlay_validator.PDFPageEvidenceValidationError,
            "physical path boundary",
        ):
            overlay_validator.validate(
                observations_path=self.observations_path,
                base_intermediate=self.base,
                overlay_dir=sensitive_overlay,
                expected_count=3,
            )

        sensitive_explicit = self.work / "questions_test"
        sensitive_explicit.mkdir()
        explicit_evidence = sensitive_explicit / adapter.EVIDENCE_FILE
        explicit_state = sensitive_explicit / adapter.STATE_FILE
        shutil.copy2(normal_output / adapter.EVIDENCE_FILE, explicit_evidence)
        shutil.copy2(normal_output / adapter.STATE_FILE, explicit_state)
        for evidence_path, state_path in (
            (explicit_evidence, normal_output / adapter.STATE_FILE),
            (normal_output / adapter.EVIDENCE_FILE, explicit_state),
        ):
            with self.assertRaisesRegex(
                overlay_validator.PDFPageEvidenceValidationError,
                "physical path boundary",
            ):
                overlay_validator.validate(
                    observations_path=self.observations_path,
                    base_intermediate=self.base,
                    evidence_path=evidence_path,
                    state_path=state_path,
                    expected_count=3,
                )

        safe_input_link = self.work / "safe-inputs"
        safe_input_link.symlink_to(sensitive_root, target_is_directory=True)
        linked_observations = safe_input_link / self.observations_path.name
        with self.assertRaisesRegex(
            adapter.PDFPageEvidenceError, "symlink component"
        ):
            adapter.build(
                observations_path=linked_observations,
                base_intermediate=self.base,
                output=self.work / "linked-input-output",
            )
        with self.assertRaisesRegex(
            overlay_validator.PDFPageEvidenceValidationError,
            "symlink component",
        ):
            overlay_validator.validate(
                observations_path=linked_observations,
                base_intermediate=self.base,
                overlay_dir=normal_output,
                expected_count=3,
            )

        safe_target = self.work / "safe-output-target"
        safe_target.mkdir()
        safe_output_link = self.work / "safe-output-parent"
        safe_output_link.symlink_to(safe_target, target_is_directory=True)
        with self.assertRaisesRegex(
            adapter.PDFPageEvidenceError, "symlink component"
        ):
            adapter.build(
                observations_path=self.observations_path,
                base_intermediate=self.base,
                output=safe_output_link / "bundle",
            )
        self.assertFalse((safe_target / "bundle").exists())

        broken_output = self.work / "broken-output"
        broken_output.symlink_to(self.work / "missing-target", target_is_directory=True)
        with self.assertRaisesRegex(
            adapter.PDFPageEvidenceError, "symlink component"
        ):
            adapter.build(
                observations_path=self.observations_path,
                base_intermediate=self.base,
                output=broken_output,
            )
        self.assertTrue(broken_output.is_symlink())

        nested = self.work / "nested"
        nested.mkdir()
        traversed_observations = nested / ".." / self.observations_path.name
        with self.assertRaisesRegex(
            adapter.PDFPageEvidenceError, "parent traversal"
        ):
            adapter.build(
                observations_path=traversed_observations,
                base_intermediate=self.base,
                output=self.work / "traversal-output",
            )


if __name__ == "__main__":
    unittest.main()
