from __future__ import annotations

import base64
import csv
import io
import json
import subprocess
import sys
import tempfile
import types
import unicodedata
import unittest
import zipfile
from collections import Counter
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_visual_asset_manifest
import validate_visual_asset_manifest
from materialize_visual_assets import materialization_signature


RUN_AT = "2026-08-15T10:00:00+00:00"
INVENTORY_FIELDS = [
    "file_id", "file_path", "file_name", "extension", "file_size", "source_sha256",
    "modified_at", "document_type", "processing_layer", "text_extractable",
    "page_count", "sheet_count", "slide_count", "extraction_status", "notes",
]


def png_bytes(width: int = 13, height: int = 8) -> bytes:
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (width, height), color=(31, 97, 173)).save(output, format="PNG")
    return output.getvalue()


class VisualAssetManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aiec-visual-manifest-")
        self.work = Path(self.temporary.name)
        self.root = self.work / "source"
        self.root.mkdir()
        self.inventory = self.work / "text_inventory.csv"
        self.rows: list[dict[str, str]] = []
        self.sample_png = png_bytes()

        self.add_bytes(
            "プロジェクト/かえで病院/会議録.pdf",
            b"%PDF-1.4\nscanned-page-fixture\n",
            document_type="pdf",
            processing_layer="native_text;ocr_required",
            page_count="3",
            notes="OCR deferred for pages [2, 3]",
        )
        self.add_office(
            "プロジェクト/東都人材/調査.pptx",
            {"ppt/media/image1.png": png_bytes(14, 8)},
            processing_layer="native_text;ocr_required",
        )
        self.add_bytes(
            "プロジェクト/青潮モビリティ/reports/figures/trend.png",
            self.sample_png,
            document_type="image",
            processing_layer="graph_required",
        )
        notebook = {
            "cells": [{
                "cell_type": "code",
                "outputs": [{
                    "data": {"image/png": base64.b64encode(self.sample_png).decode("ascii")}
                }],
            }],
        }
        self.add_bytes(
            "プロジェクト/青潮モビリティ/notebooks/eda.ipynb",
            (json.dumps(notebook) + "\n").encode("utf-8"),
            document_type="notebook",
            processing_layer="native_text;graph_required",
            notes="1 embedded image(s) recorded",
        )
        self.add_office(
            "プロジェクト/青葉与信/最終報告.pptx",
            {},
            processing_layer="native_text;graph_required",
        )
        self.write_inventory()
        self.manifest = self.work / "visual-assets.jsonl"
        self.batch = self.work / "representative-batch.jsonl"
        self.materializable = self.work / "materializable-batch.jsonl"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def actual_path(self, relative_path: str) -> Path:
        # macOS commonly stores Japanese paths in NFD while the inventory is NFC.
        actual = self.root / unicodedata.normalize("NFD", relative_path)
        actual.parent.mkdir(parents=True, exist_ok=True)
        return actual

    def inventory_row(
        self, relative_path: str, path: Path, *, document_type: str,
        processing_layer: str, page_count: str = "", notes: str = "",
    ) -> dict[str, str]:
        normalized = unicodedata.normalize("NFC", relative_path)
        source_sha256 = build_visual_asset_manifest.digest_file(path)
        return {
            "file_id": build_visual_asset_manifest.stable_id(
                "file", {"relative_path": normalized, "source_sha256": source_sha256}
            ),
            "file_path": normalized,
            "file_name": unicodedata.normalize("NFC", Path(relative_path).name),
            "extension": Path(relative_path).suffix.lower().lstrip("."),
            "file_size": str(path.stat().st_size),
            "source_sha256": source_sha256,
            "modified_at": RUN_AT,
            "document_type": document_type,
            "processing_layer": processing_layer,
            "text_extractable": "false",
            "page_count": page_count,
            "sheet_count": "",
            "slide_count": "",
            "extraction_status": "deferred",
            "notes": notes,
        }

    def add_bytes(
        self, relative_path: str, data: bytes, *, document_type: str,
        processing_layer: str, page_count: str = "", notes: str = "",
    ) -> None:
        path = self.actual_path(relative_path)
        path.write_bytes(data)
        self.rows.append(self.inventory_row(
            relative_path, path,
            document_type=document_type,
            processing_layer=processing_layer,
            page_count=page_count,
            notes=notes,
        ))

    def add_office(
        self, relative_path: str, members: dict[str, bytes], *, processing_layer: str,
    ) -> None:
        path = self.actual_path(relative_path)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            for member_path, data in members.items():
                archive.writestr(member_path, data)
        self.rows.append(self.inventory_row(
            relative_path, path,
            document_type="presentation",
            processing_layer=processing_layer,
            notes=(f"{len(members)} embedded image(s) recorded" if members else ""),
        ))

    def write_inventory(self) -> None:
        with self.inventory.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=INVENTORY_FIELDS)
            writer.writeheader()
            writer.writerows(reversed(self.rows))

    def build(self, **kwargs: object) -> dict[str, object]:
        return build_visual_asset_manifest.build(
            inventory=self.inventory,
            root=self.root,
            output=self.manifest,
            batch_output=self.batch,
            materializable_output=self.materializable,
            batch_size=3,
            run_at=RUN_AT,
            **kwargs,
        )

    def materialized_records(
        self, selected: list[dict[str, object]], directory_name: str = "images",
    ) -> list[dict[str, object]]:
        image_directory = self.work / directory_name
        image_directory.mkdir(exist_ok=True)
        records = json.loads(json.dumps(selected))
        rendered_png = png_bytes(10, 10)
        for record in records:
            output = image_directory / f"{record['asset_id']}.png"
            output.write_bytes(rendered_png)
            output_sha256 = build_visual_asset_manifest.digest_file(output)
            renderer = "fixture-renderer"
            renderer_version = "1"
            dpi = 200 if record["origin"]["kind"] == "pdf_page" else None
            signature = materialization_signature(
                record["source"]["sha256"],
                record["origin"],
                dpi or 0,
                renderer,
                renderer_version,
            )
            record["status"] = "pending_classification"
            record["materialized_path"] = str(output)
            record["materialization"] = {
                "output_path": str(output),
                "sha256": output_sha256,
                "size_bytes": output.stat().st_size,
                "mime_type": "image/png",
                "width_px": 10,
                "height_px": 10,
                "renderer": renderer,
                "renderer_version": renderer_version,
                "dpi": dpi,
                "signature": signature,
                "cache_hit": False,
                "generated_at": RUN_AT,
            }
        return records

    def test_question_independent_discovery_selection_and_deduplication(self) -> None:
        summary = self.build()
        self.assertEqual(summary["records"], 7)
        self.assertEqual(summary["selected"], 3)
        self.assertEqual(summary["duplicates"], 1)
        records = build_visual_asset_manifest.read_jsonl(self.manifest)
        self.assertEqual(
            Counter(record["origin"]["kind"] for record in records),
            Counter({
                "pdf_page": 2,
                "office_embedded_image": 1,
                "standalone_image": 1,
                "notebook_embedded_image": 1,
                "visual_container": 2,
            }),
        )
        self.assertTrue(all(
            unicodedata.is_normalized("NFC", record["source"]["relative_path"])
            for record in records
        ))
        notebook = next(
            record for record in records if record["origin"]["kind"] == "notebook_embedded_image"
        )
        standalone = next(
            record for record in records if record["selection"]["stratum"] == "standalone_graph"
        )
        self.assertEqual(notebook["origin"]["member_path"], "cells/0/outputs/0/data/image/png")
        self.assertEqual(notebook["origin"]["member_sha256"], standalone["source"]["sha256"])
        self.assertEqual(notebook["duplicate_of_asset_id"], standalone["asset_id"])
        self.assertFalse(notebook["selection"]["selected_for_batch"])

        selected = build_visual_asset_manifest.read_jsonl(self.batch)
        materializable = build_visual_asset_manifest.read_jsonl(self.materializable)
        self.assertEqual(len(materializable), 5)
        self.assertTrue(all(
            record["origin"]["kind"]
            in build_visual_asset_manifest.MATERIALIZABLE_ORIGIN_KINDS
            for record in materializable
        ))
        self.assertEqual(
            [record["selection"]["stratum"] for record in selected],
            ["scanned_pdf_page", "office_embedded_image", "standalone_graph"],
        )
        self.assertEqual([record["selection"]["batch_rank"] for record in selected], [1, 2, 3])
        self.assertTrue(all(build_visual_asset_manifest.selection_eligible(record) for record in selected))
        self.assertTrue(all(record["provenance"]["question_independent"] for record in records))
        expected_limits = build_visual_asset_manifest.office_zip_limits()
        self.assertTrue(all(
            record["provenance"]["office_zip_limits"] == expected_limits
            for record in records
        ))
        self.assertTrue(all(record["materialization"] is None for record in records))
        self.assertTrue(all(record["error"] is None for record in records))

        validation = validate_visual_asset_manifest.validate(
            self.manifest,
            self.inventory,
            self.root,
            batch_size=3,
            batch=self.batch,
            materializable_batch=self.materializable,
        )
        self.assertEqual(validation["records"], 7)
        self.assertEqual(validation["selected"], 3)
        self.assertEqual(validation["duplicates"], 1)
        self.assertIn(
            validation["schema_validation"],
            {"strict_manual_fallback", "jsonschema_draft202012_format"},
        )

        materialized_batch = self.work / "materialized-batch.jsonl"
        materialized_records = self.materialized_records(selected)
        build_visual_asset_manifest.atomic_jsonl(materialized_batch, materialized_records)
        materialized_validation = validate_visual_asset_manifest.validate(
            self.manifest,
            self.inventory,
            self.root,
            batch_size=3,
            batch=self.batch,
            materialized_batch=materialized_batch,
        )
        self.assertEqual(materialized_validation["materialized_selected"], 3)

    def test_materialized_image_bytes_metadata_limits_and_signature_are_verified(self) -> None:
        self.build()
        selected = build_visual_asset_manifest.read_jsonl(self.batch)
        materialized_batch = self.work / "materialized-batch.jsonl"
        records = self.materialized_records(selected, "validation-images")
        build_visual_asset_manifest.atomic_jsonl(materialized_batch, records)

        first = records[0]
        output = Path(first["materialization"]["output_path"])
        output.write_bytes(b"not really a PNG")
        first["materialization"]["sha256"] = build_visual_asset_manifest.digest_file(output)
        first["materialization"]["size_bytes"] = output.stat().st_size
        build_visual_asset_manifest.atomic_jsonl(materialized_batch, records)
        with self.assertRaisesRegex(ValueError, "not a decodable image"):
            validate_visual_asset_manifest.validate(
                self.manifest, self.inventory, self.root,
                batch_size=3, batch=self.batch, materialized_batch=materialized_batch,
            )

        output.write_bytes(png_bytes(10, 10))
        first["materialization"]["sha256"] = build_visual_asset_manifest.digest_file(output)
        first["materialization"]["size_bytes"] = output.stat().st_size
        first["materialization"]["mime_type"] = "image/jpeg"
        build_visual_asset_manifest.atomic_jsonl(materialized_batch, records)
        with self.assertRaisesRegex(ValueError, "MIME mismatch"):
            validate_visual_asset_manifest.validate(
                self.manifest, self.inventory, self.root,
                batch_size=3, batch=self.batch, materialized_batch=materialized_batch,
            )

        first["materialization"]["mime_type"] = "image/png"
        first["materialization"]["width_px"] = 11
        build_visual_asset_manifest.atomic_jsonl(materialized_batch, records)
        with self.assertRaisesRegex(ValueError, "dimensions mismatch"):
            validate_visual_asset_manifest.validate(
                self.manifest, self.inventory, self.root,
                batch_size=3, batch=self.batch, materialized_batch=materialized_batch,
            )

        first["materialization"]["width_px"] = 10
        first["materialization"]["signature"] = "0" * 64
        build_visual_asset_manifest.atomic_jsonl(materialized_batch, records)
        with self.assertRaisesRegex(ValueError, "signature mismatch"):
            validate_visual_asset_manifest.validate(
                self.manifest, self.inventory, self.root,
                batch_size=3, batch=self.batch, materialized_batch=materialized_batch,
            )

        first["materialization"]["signature"] = materialization_signature(
            first["source"]["sha256"],
            first["origin"],
            first["materialization"]["dpi"] or 0,
            first["materialization"]["renderer"],
            first["materialization"]["renderer_version"],
        )
        build_visual_asset_manifest.atomic_jsonl(materialized_batch, records)
        with (
            mock.patch.object(validate_visual_asset_manifest, "MAX_MATERIALIZED_PIXELS", 99),
            self.assertRaisesRegex(ValueError, "maximum pixel count"),
        ):
            validate_visual_asset_manifest.validate(
                self.manifest, self.inventory, self.root,
                batch_size=3, batch=self.batch, materialized_batch=materialized_batch,
            )

    def test_symlinks_and_resolved_paths_outside_root_are_rejected(self) -> None:
        outside = self.work / "outside.png"
        outside.write_bytes(self.sample_png)
        linked = self.root / "linked.png"
        linked.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlinks are not allowed"):
            build_visual_asset_manifest.source_file_index(self.root)

        linked.unlink()
        standalone_row = next(row for row in self.rows if row["document_type"] == "image")
        with self.assertRaisesRegex(ValueError, "resolves outside --root"):
            build_visual_asset_manifest.resolve_source(
                standalone_row,
                {standalone_row["file_path"]: outside},
                self.root,
            )

        linked_root = self.work / "linked-root"
        linked_root.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "source root must not be a symlink"):
            build_visual_asset_manifest.source_file_index(linked_root)

    def test_normalized_office_and_notebook_member_collisions_are_rejected(self) -> None:
        composed = "café.png"
        decomposed = unicodedata.normalize("NFD", composed)
        office = self.work / "collision.pptx"
        with zipfile.ZipFile(office, "w") as archive:
            archive.writestr(f"ppt/media/{composed}", self.sample_png)
            archive.writestr(f"ppt/media/{decomposed}", self.sample_png)
        with self.assertRaisesRegex(ValueError, "NFC member collision in Office"):
            build_visual_asset_manifest.office_media(office, "pptx")

        notebook = self.work / "collision.ipynb"
        encoded = base64.b64encode(self.sample_png).decode("ascii")
        notebook.write_text(json.dumps({
            "cells": [{
                "attachments": {
                    composed: {"image/png": encoded},
                    decomposed: {"image/png": encoded},
                },
                "outputs": [],
            }],
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "NFC member collision in notebook"):
            build_visual_asset_manifest.notebook_media(notebook)

    def test_office_zip_streaming_and_expansion_limits_reject_oversize_archives(self) -> None:
        office = self.work / "bounded.pptx"
        first_payload = b"A" * 64
        second_payload = b"B" * 64
        with zipfile.ZipFile(office, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("ppt/media/first.png", first_payload)
            archive.writestr("ppt/media/second.png", second_payload)

        with mock.patch.object(
            zipfile.ZipFile,
            "read",
            side_effect=AssertionError("office_media must stream via ZipFile.open"),
        ):
            members = build_visual_asset_manifest.office_media(office, "pptx")
        self.assertEqual(
            members,
            [
                (
                    "ppt/media/first.png",
                    build_visual_asset_manifest.digest_bytes(first_payload),
                    len(first_payload),
                ),
                (
                    "ppt/media/second.png",
                    build_visual_asset_manifest.digest_bytes(second_payload),
                    len(second_payload),
                ),
            ],
        )

        with self.assertRaisesRegex(ValueError, "entry count exceeds safety limit"):
            build_visual_asset_manifest.office_media(
                office, "pptx", max_archive_entries=2,
            )
        with self.assertRaisesRegex(ValueError, "uncompressed-size safety limit"):
            build_visual_asset_manifest.office_media(
                office, "pptx", max_member_uncompressed_bytes=63,
            )
        with self.assertRaisesRegex(ValueError, "total exceeds uncompressed-size"):
            build_visual_asset_manifest.office_media(
                office, "pptx", max_total_uncompressed_bytes=127,
            )

        compressed = self.work / "ratio-bomb.pptx"
        with zipfile.ZipFile(compressed, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("ppt/media/bomb.png", b"Z" * 10_000)
        with self.assertRaisesRegex(ValueError, "compression-ratio safety limit"):
            build_visual_asset_manifest.office_media(
                compressed, "pptx", max_compression_ratio=2.0,
            )

    def test_status_forgery_cannot_bypass_materialization_requirements(self) -> None:
        self.build()
        records = build_visual_asset_manifest.read_jsonl(self.manifest)
        records[0]["status"] = "classified"
        self.assertIsNone(records[0]["materialization"])
        build_visual_asset_manifest.atomic_jsonl(self.manifest, records)
        with self.assertRaisesRegex(ValueError, "schema violation|invalid status"):
            validate_visual_asset_manifest.validate(
                self.manifest, self.inventory, self.root, batch_size=3, batch=self.batch,
            )
        with self.assertRaisesRegex(ValueError, "cannot resume: invalid status"):
            self.build(resume=True)

        records[0]["status"] = "pending_classification"
        build_visual_asset_manifest.atomic_jsonl(self.manifest, records)
        with self.assertRaisesRegex(
            ValueError, "schema violation|requires materialization metadata"
        ):
            validate_visual_asset_manifest.validate(
                self.manifest, self.inventory, self.root, batch_size=3, batch=self.batch,
            )
        with self.assertRaisesRegex(ValueError, "materialized asset lacks metadata"):
            self.build(resume=True)

    def test_office_zip_limits_are_deterministic_provenance(self) -> None:
        custom_limits = {
            "max_office_archive_entries": 100,
            "max_office_member_bytes": 1_000_000,
            "max_office_total_bytes": 2_000_000,
            "max_office_compression_ratio": 50.0,
        }
        self.build(**custom_limits)
        records = build_visual_asset_manifest.read_jsonl(self.manifest)
        expected = {
            "max_archive_entries": 100,
            "max_member_uncompressed_bytes": 1_000_000,
            "max_total_uncompressed_bytes": 2_000_000,
            "max_compression_ratio": 50.0,
        }
        self.assertTrue(all(
            record["provenance"]["office_zip_limits"] == expected
            for record in records
        ))
        result = validate_visual_asset_manifest.validate(
            self.manifest,
            self.inventory,
            self.root,
            batch_size=3,
            batch=self.batch,
            **custom_limits,
        )
        self.assertEqual(result["records"], 7)

        records[0]["provenance"]["office_zip_limits"]["max_archive_entries"] = 101
        build_visual_asset_manifest.atomic_jsonl(self.manifest, records)
        with self.assertRaisesRegex(ValueError, "safety limits do not match"):
            validate_visual_asset_manifest.validate(
                self.manifest,
                self.inventory,
                self.root,
                batch_size=3,
                batch=self.batch,
                **custom_limits,
            )
        with self.assertRaisesRegex(ValueError, "Office ZIP safety limits changed"):
            self.build(resume=True, **custom_limits)

    def test_jsonschema_draft_202012_uses_format_checker_when_available(self) -> None:
        calls: dict[str, object] = {}

        class FakeValidator:
            @staticmethod
            def check_schema(schema: dict[str, object]) -> None:
                calls["checked"] = schema["$schema"]

            def __init__(self, schema: dict[str, object], format_checker: object) -> None:
                calls["format_checker"] = format_checker

            def iter_errors(self, _record: dict[str, object]) -> list[object]:
                return []

        fake_jsonschema = types.SimpleNamespace(
            Draft202012Validator=FakeValidator,
            FormatChecker=lambda: "format-checker",
        )
        schema = validate_visual_asset_manifest.validate_schema(
            REPOSITORY / "schemas" / "visual-asset.schema.json"
        )
        with mock.patch.dict(sys.modules, {"jsonschema": fake_jsonschema}):
            validator, mode = validate_visual_asset_manifest.compile_published_schema(schema)
        self.assertIsInstance(validator, FakeValidator)
        self.assertEqual(mode, "jsonschema_draft202012_format")
        self.assertEqual(calls["checked"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(calls["format_checker"], "format-checker")

    def test_overwrite_refusal_resume_and_reset_are_explicit(self) -> None:
        self.build()
        with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
            self.build()

        records = build_visual_asset_manifest.read_jsonl(self.manifest)
        container = next(record for record in records if record["origin"]["kind"] == "visual_container")
        container["status"] = "unsupported_media"
        container["error"] = "container renderer is intentionally unavailable in this fixture"
        build_visual_asset_manifest.atomic_jsonl(self.manifest, records)
        resumed = self.build(resume=True)
        self.assertEqual(resumed["records"], 7)
        resumed_records = build_visual_asset_manifest.read_jsonl(self.manifest)
        resumed_container = next(
            record for record in resumed_records if record["asset_id"] == container["asset_id"]
        )
        self.assertEqual(resumed_container["status"], "unsupported_media")
        self.assertIsNotNone(resumed_container["error"])

        self.build(overwrite=True)
        reset = next(
            record for record in build_visual_asset_manifest.read_jsonl(self.manifest)
            if record["asset_id"] == container["asset_id"]
        )
        self.assertEqual(reset["status"], "pending_materialization")
        self.assertIsNone(reset["error"])
        with self.assertRaisesRegex(ValueError, "must differ"):
            build_visual_asset_manifest.build(
                inventory=self.inventory,
                root=self.root,
                output=self.manifest,
                batch_output=self.manifest,
                overwrite=True,
            )

    def test_schema_and_cli_help_publish_the_contract(self) -> None:
        schema = json.loads(
            (REPOSITORY / "schemas" / "visual-asset.schema.json").read_text(encoding="utf-8")
        )
        self.assertTrue(
            {"duplicate_of_asset_id", "materialization", "error"} <= set(schema["required"])
        )
        self.assertTrue(
            {"classified", "needs_review", "verified", "unresolved", "skipped"}.isdisjoint(
                schema["properties"]["status"]["enum"]
            )
        )
        for script, expected_flags in (
            (
                "build_visual_asset_manifest.py",
                ("--batch-out", "--max-office-member-bytes"),
            ),
            (
                "validate_visual_asset_manifest.py",
                ("--materialized-batch", "--max-office-member-bytes"),
            ),
        ):
            completed = subprocess.run(
                [sys.executable, str(SCRIPTS / script), "--help"],
                check=True,
                capture_output=True,
                text=True,
            )
            for expected in expected_flags:
                self.assertIn(expected, completed.stdout)


if __name__ == "__main__":
    unittest.main()
