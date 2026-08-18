from __future__ import annotations

import copy
import csv
import hashlib
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

import build_pdf_page_observations as builder


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def two_page_pdf() -> bytes:
    content = b"BT /F1 18 Tf 72 720 Td (Native PDF text 123) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n"
        + content
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("ascii"))
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


class PDFPageObservationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aiec-pdf-page-")
        self.work = Path(self.temporary.name)
        self.row = {
            "file_id": "file_" + "1" * 32,
            "document_id": "doc_" + "1" * 32,
            "relative_path": "fixtures/sample.pdf",
            "source_sha256": SHA_A,
            "size_bytes": 1234,
            "page_count": 2,
        }
        self.native_page = {
            "page_number": 1,
            "width_pt": 612.0,
            "height_pt": 792.0,
            "page_output_sha256": SHA_B,
            "words": [
                {
                    "word_id": "word_000001",
                    "reading_order": 1,
                    "block_index": 1,
                    "line_index": 1,
                    "word_index": 1,
                    "raw_text": "ＡＢＣ",
                    "bbox": [72.0, 80.0, 40.0, 12.0],
                }
            ],
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build_record(
        self,
        page_data: dict[str, object],
        *,
        page_number: int = 1,
        manifests: list[dict[str, object]] | None = None,
        observations: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        return builder.build_record(
            repository_root=REPOSITORY,
            row=self.row,
            page_number=page_number,
            page_data=page_data,
            pdfinfo_data={
                "width_pt": page_data["width_pt"],
                "height_pt": page_data["height_pt"],
                "rotation_degrees": 0,
            },
            pdfinfo_output_sha256=SHA_C,
            pdftotext_output_sha256=SHA_D,
            pdftotext_version="pdftotext version fixture",
            pdftotext_binary_sha256=SHA_A,
            inventory_sha256=SHA_B,
            visual_assets_sha256=SHA_C,
            ocr_observations_sha256=SHA_D,
            pdfinfo_version="pdfinfo version fixture",
            pdfinfo_binary_sha256=SHA_B,
            manifests=manifests or [],
            observations=observations or [],
        )

    def test_bbox_parser_retains_raw_text_order_and_blank_pages(self) -> None:
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <html xmlns="http://www.w3.org/1999/xhtml"><body><doc>
          <page width="612.0" height="792.0">
            <flow><block><line>
              <word xMin="72" yMin="80" xMax="112" yMax="92">ABC</word>
              <word xMin="120" yMin="80" xMax="160" yMax="92">123</word>
            </line></block></flow>
          </page>
          <page width="612.0" height="792.0"/>
        </doc></body></html>"""
        pages = builder.parse_bbox_pages(xml, expected_pages=2)
        self.assertEqual([page["page_number"] for page in pages], [1, 2])
        self.assertEqual(
            [word["raw_text"] for word in pages[0]["words"]], ["ABC", "123"]
        )
        self.assertEqual(
            [word["reading_order"] for word in pages[0]["words"]], [1, 2]
        )
        self.assertEqual(pages[1]["words"], [])

    def test_pdfinfo_parser_binds_every_page_and_rejects_encryption(self) -> None:
        output = b"""Pages:           2
Encrypted:       no
Page    1 size:  612 x 792 pts (letter)
Page    1 rot:   0
Page    2 size:  792 x 612 pts (letter)
Page    2 rot:   90
"""
        pages, digest = builder.parse_pdfinfo(output, expected_pages=2)
        self.assertEqual(pages[1]["rotation_degrees"], 0)
        self.assertEqual(pages[2]["rotation_degrees"], 90)
        self.assertEqual(digest, builder.sha256_bytes(output))
        encrypted = output.replace(b"Encrypted:       no", b"Encrypted:       yes")
        with self.assertRaisesRegex(builder.PDFObservationError, "encrypted"):
            builder.parse_pdfinfo(encrypted, expected_pages=2)

    def test_native_route_is_closed_deterministic_and_never_mixes_ocr(self) -> None:
        ignored_ocr = [{"this": "must not be read for a native page"}]
        first = self.build_record(
            self.native_page,
            manifests=ignored_ocr,
            observations=ignored_ocr,
        )
        second = self.build_record(
            copy.deepcopy(self.native_page),
            manifests=ignored_ocr,
            observations=ignored_ocr,
        )
        self.assertEqual(first, second)
        self.assertEqual(first["extraction"]["route"], "native_bbox")
        self.assertEqual(first["native"]["words"][0]["raw_text"], "ＡＢＣ")
        self.assertIsNone(first["ocr"])
        self.assertEqual(first["conflicts"], [])
        self.assertEqual(first["unresolved"], [])
        self.assertEqual(first["status"], "observed")
        self.assertEqual(builder.validate_observation(first), [])
        self.assertTrue(first["provenance"]["shadow_only"])
        self.assertFalse(first["provenance"]["evidence_connected"])
        self.assertFalse(first["provenance"]["search_unit_connected"])

    def test_empty_native_page_without_ocr_is_explicitly_unresolved(self) -> None:
        blank_page = {
            "page_number": 2,
            "width_pt": 612.0,
            "height_pt": 792.0,
            "page_output_sha256": SHA_B,
            "words": [],
        }
        record = self.build_record(blank_page, page_number=2)
        self.assertEqual(record["extraction"]["route"], "unresolved")
        self.assertEqual(
            record["extraction"]["route_reason"], "missing_materialized_page"
        )
        self.assertEqual(record["status"], "needs_review")
        self.assertEqual(record["unresolved"][0]["reason"], "missing_materialized_page")
        self.assertEqual(builder.validate_observation(record), [])

    def test_ocr_route_retains_both_raw_readings_and_disagreement(self) -> None:
        blank_page = {
            "page_number": 2,
            "width_pt": 612.0,
            "height_pt": 792.0,
            "page_output_sha256": SHA_B,
            "words": [],
        }
        manifest = {
            "asset_id": "asset_" + "2" * 32,
            "materialized_path": str(REPOSITORY / "artifacts" / "fixture.png"),
            "materialization": {
                "sha256": SHA_C,
                "mime_type": "image/png",
                "width_px": 100,
                "height_px": 200,
            },
        }
        run_one = "ocr_run_" + "3" * 24
        run_two = "ocr_run_" + "4" * 24
        observation = {
            "observation_id": "ocr_" + "5" * 24,
            "status": "needs_review",
            "exactness": "unresolved",
            "hashes": {"signature_sha256": SHA_D},
            "engine_runs": [
                {
                    "run_id": run_one,
                    "engine": {
                        "name": "engine-a",
                        "version": "1",
                        "digest": SHA_A,
                        "independence_group": "a",
                    },
                    "status": "completed",
                    "lines": [
                        {
                            "line_id": "line_1",
                            "sequence": 1,
                            "raw_text": "金額 100",
                            "bbox": [100, 100, 300, 50],
                            "confidence": 0.9,
                        }
                    ],
                    "warnings": [],
                    "error": None,
                    "hashes": {"output_sha256": SHA_B},
                },
                {
                    "run_id": run_two,
                    "engine": {
                        "name": "engine-b",
                        "version": "2",
                        "digest": SHA_B,
                        "independence_group": "b",
                    },
                    "status": "completed",
                    "lines": [
                        {
                            "line_id": "line_1",
                            "sequence": 1,
                            "raw_text": "金額 1OO",
                            "bbox": [102, 101, 298, 49],
                            "confidence": 0.8,
                        }
                    ],
                    "warnings": [],
                    "error": None,
                    "hashes": {"output_sha256": SHA_C},
                },
            ],
            "consensus": {
                "lines": [
                    {
                        "consensus_line_id": "ocr_line_" + "6" * 16,
                        "exactness": "unresolved",
                        "bbox": [100, 100, 300, 50],
                        "text": None,
                        "readings": [
                            {
                                "run_id": run_one,
                                "line_id": "line_1",
                                "raw_text": "金額 100",
                            },
                            {
                                "run_id": run_two,
                                "line_id": "line_1",
                                "raw_text": "金額 1OO",
                            },
                        ],
                    }
                ]
            },
        }
        fake_image = REPOSITORY / "artifacts" / "fixture.png"
        with (
            mock.patch.object(builder, "_manifest_binding_errors", return_value=[]),
            mock.patch.object(builder, "_ocr_binding_errors", return_value=[]),
            mock.patch.object(
                builder,
                "_verify_materialized_image",
                return_value=(fake_image, {"width_px": 100, "height_px": 200}),
            ),
        ):
            record = self.build_record(
                blank_page,
                page_number=2,
                manifests=[manifest],
                observations=[observation],
            )
        self.assertEqual(record["extraction"]["route"], "ocr_raw")
        self.assertIsNone(record["native"])
        self.assertEqual(
            [
                run["lines"][0]["raw_text"]
                for run in record["ocr"]["raw_runs"]
            ],
            ["金額 100", "金額 1OO"],
        )
        self.assertEqual(len(record["conflicts"]), 1)
        self.assertEqual(len(record["unresolved"]), 1)
        self.assertEqual(record["status"], "needs_review")
        self.assertEqual(builder.validate_observation(record), [])

    def test_semantic_validator_rejects_out_of_page_bbox_after_rehash(self) -> None:
        record = self.build_record(self.native_page)
        tampered = copy.deepcopy(record)
        tampered["native"]["words"][0]["bbox"] = [600.0, 780.0, 20.0, 20.0]
        tampered = builder.rehash_record(tampered)
        errors = builder.validate_observation(tampered)
        self.assertTrue(any("outside" in error for error in errors), errors)

    def test_schema_is_closed_even_when_hashes_are_recomputed(self) -> None:
        record = self.build_record(self.native_page)
        tampered = copy.deepcopy(record)
        tampered["native"]["words"][0]["normalized_text"] = "ABC"
        tampered = builder.rehash_record(tampered)
        errors = builder.validate_observation(tampered)
        self.assertTrue(any("Additional properties" in error for error in errors), errors)

    def test_atomic_writer_preserves_existing_output_without_overwrite(self) -> None:
        record = self.build_record(self.native_page)
        output = self.work / "observations.jsonl"
        builder.atomic_write_jsonl(output, [record], overwrite=False)
        original = output.read_bytes()
        with self.assertRaises(FileExistsError):
            builder.atomic_write_jsonl(output, [record], overwrite=False)
        self.assertEqual(output.read_bytes(), original)
        builder.atomic_write_jsonl(output, [record, record], overwrite=True)
        self.assertEqual(len(output.read_text(encoding="utf-8").splitlines()), 2)
        self.assertEqual(list(self.work.glob("*.tmp")), [])

    def test_jsonl_reader_rejects_duplicate_keys_blank_lines_and_nonobjects(self) -> None:
        duplicate = self.work / "duplicate.jsonl"
        duplicate.write_text('{"a":1,"a":2}\n', encoding="utf-8")
        with self.assertRaisesRegex(builder.PDFObservationError, "duplicate JSON key"):
            builder.load_jsonl(duplicate, "fixture")
        blank = self.work / "blank.jsonl"
        blank.write_text("{}\n\n", encoding="utf-8")
        with self.assertRaisesRegex(builder.PDFObservationError, "blank JSONL"):
            builder.load_jsonl(blank, "fixture")
        nonobject = self.work / "nonobject.jsonl"
        nonobject.write_text(json.dumps([1, 2]) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(builder.PDFObservationError, "must be an object"):
            builder.load_jsonl(nonobject, "fixture")

    def test_record_index_distinguishes_identical_bytes_at_different_paths(self) -> None:
        first = {
            "source": {"relative_path": "a/copy.pdf", "sha256": SHA_A},
            "origin": {"kind": "pdf_page", "page_number": 1},
        }
        second = {
            "source": {"relative_path": "b/copy.pdf", "sha256": SHA_A},
            "origin": {"kind": "pdf_page", "page_number": 1},
        }
        index = builder._index_pdf_records([first, second], label="fixture")
        self.assertEqual(len(index), 2)
        self.assertEqual(
            index[("a/copy.pdf", SHA_A, 1)][0]["source"]["relative_path"],
            "a/copy.pdf",
        )
        self.assertEqual(
            index[("b/copy.pdf", SHA_A, 1)][0]["source"]["relative_path"],
            "b/copy.pdf",
        )

    @unittest.skipUnless(
        shutil.which("pdfinfo") and shutil.which("pdftotext"),
        "Poppler is required for the full builder regression",
    )
    def test_full_builder_covers_every_page_and_is_byte_deterministic(self) -> None:
        artifacts = REPOSITORY / "artifacts"
        artifacts.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="aiec-pdf-builder-", dir=artifacts
        ) as directory:
            root = Path(directory)
            source_root = root / "source"
            source_root.mkdir()
            pdf_path = source_root / "sample.pdf"
            pdf_data = two_page_pdf()
            pdf_path.write_bytes(pdf_data)
            inventory = root / "text_inventory.csv"
            fields = [
                "file_id",
                "file_path",
                "extension",
                "file_size",
                "source_sha256",
                "document_type",
                "page_count",
            ]
            with inventory.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow(
                    {
                        "file_id": "file_" + "7" * 32,
                        "file_path": "sample.pdf",
                        "extension": "pdf",
                        "file_size": len(pdf_data),
                        "source_sha256": hashlib.sha256(pdf_data).hexdigest(),
                        "document_type": "pdf",
                        "page_count": 2,
                    }
                )
            first = root / "first.jsonl"
            second = root / "second.jsonl"
            first_summary = builder.build(
                repository_root=REPOSITORY,
                source_root=source_root,
                inventory_path=inventory,
                output_path=first,
            )
            second_summary = builder.build(
                repository_root=REPOSITORY,
                source_root=source_root,
                inventory_path=inventory,
                output_path=second,
            )
            self.assertEqual(first_summary["pages"], 2)
            self.assertEqual(
                first_summary["routes"], {"native_bbox": 1, "unresolved": 1}
            )
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(
                first_summary["output_sha256"], second_summary["output_sha256"]
            )
            records = [
                json.loads(line)
                for line in first.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [record["page"]["page_number"] for record in records], [1, 2]
            )
            self.assertTrue(
                all(builder.validate_observation(record) == [] for record in records)
            )


if __name__ == "__main__":
    unittest.main()
