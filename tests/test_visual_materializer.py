from __future__ import annotations

import base64
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unicodedata
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from PIL import Image


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "scripts"
sys.path.insert(0, str(SCRIPTS))

import materialize_visual_assets


def image_bytes(image_format: str, size: tuple[int, int], color: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color=color).save(output, format=image_format)
    return output.getvalue()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class VisualAssetMaterializerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="visual-materializer-")
        self.root = Path(self.temporary.name)
        self.output = self.root / "materialized"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def record(
        self,
        asset_id: str,
        source_path: str,
        source_data: bytes,
        origin: dict[str, object],
    ) -> dict[str, object]:
        return {
            "asset_id": asset_id,
            "source_path": source_path,
            "source_sha256": sha256(source_data),
            "origin": origin,
            "selection_reason": "test fixture",
        }

    def test_standalone_copy_resolves_nfc_inventory_against_nfd_disk_and_resumes(self) -> None:
        disk_directory = unicodedata.normalize("NFD", "共有資料")
        disk_filename = unicodedata.normalize("NFD", "画像.png")
        source_directory = self.root / disk_directory
        source_directory.mkdir()
        data = image_bytes("PNG", (11, 7), "navy")
        (source_directory / disk_filename).write_bytes(data)
        inventory_path = unicodedata.normalize("NFC", "共有資料/画像.png")
        record = self.record(
            "asset_nfc_01", inventory_path, data, {"kind": "standalone_image"}
        )

        first = materialize_visual_assets.materialized_record(record, self.root, self.output)
        self.assertEqual(first["status"], "materialized")
        self.assertEqual(first["mime_type"], "image/png")
        self.assertEqual((first["width"], first["height"]), (11, 7))
        self.assertEqual(first["materialized_sha256"], record["source_sha256"])
        self.assertFalse(first["provenance"]["cache_hit"])
        self.assertEqual(first["provenance"]["source_path"], inventory_path)
        materialized = self.root / str(first["materialized_path"])
        self.assertEqual(materialized.read_bytes(), data)

        second = materialize_visual_assets.materialized_record(record, self.root, self.output)
        self.assertEqual(second["status"], "materialized")
        self.assertTrue(second["provenance"]["cache_hit"])
        self.assertEqual(second["materialized_sha256"], first["materialized_sha256"])

    def test_source_hash_mismatch_is_reported_before_materialization(self) -> None:
        data = image_bytes("PNG", (3, 4), "red")
        (self.root / "source.png").write_bytes(data)
        record = self.record(
            "asset_bad_hash", "source.png", data, {"kind": "standalone_image"}
        )
        record["source_sha256"] = "0" * 64

        result = materialize_visual_assets.materialized_record(record, self.root, self.output)
        self.assertEqual(result["status"], "error")
        self.assertIn("source SHA-256 mismatch", result["error"])
        self.assertFalse(self.output.exists())

    def test_office_bmp_and_emf_are_converted_to_verified_png(self) -> None:
        bmp = image_bytes("BMP", (9, 5), "green")
        office_path = self.root / "fixture.docx"
        with zipfile.ZipFile(office_path, "w") as archive:
            archive.writestr("word/media/image1.bmp", bmp)
            archive.writestr("word/media/image2.emf", b"not-raster-emf-data")
        office_data = office_path.read_bytes()

        bmp_record = self.record(
            "asset_office_bmp",
            "fixture.docx",
            office_data,
            {"kind": "office_embedded", "member_path": "word/media/image1.bmp"},
        )
        bmp_result = materialize_visual_assets.materialized_record(
            bmp_record, self.root, self.output
        )
        self.assertEqual(bmp_result["status"], "materialized")
        self.assertEqual(bmp_result["mime_type"], "image/png")
        self.assertEqual((bmp_result["width"], bmp_result["height"]), (9, 5))
        self.assertEqual(
            bmp_result["provenance"]["operation"], "office_member_convert_to_png"
        )
        converted_path = self.root / str(bmp_result["materialized_path"])
        self.assertNotEqual(converted_path.read_bytes(), bmp)
        with Image.open(converted_path) as converted:
            self.assertEqual(converted.format, "PNG")

        emf_record = self.record(
            "asset_office_emf",
            "fixture.docx",
            office_data,
            {"kind": "office_embedded", "member_path": "word/media/image2.emf"},
        )
        emf_png = image_bytes("PNG", (13, 17), "blue")
        with mock.patch.object(
            materialize_visual_assets,
            "convert_vector_office_member_to_png",
            return_value=(emf_png, "LibreOffice test|binary_sha256=" + "a" * 64),
        ) as convert:
            emf_result = materialize_visual_assets.materialized_record(
                emf_record, self.root, self.output
            )
        self.assertEqual(emf_result["status"], "materialized")
        self.assertEqual(emf_result["mime_type"], "image/png")
        self.assertEqual((emf_result["width"], emf_result["height"]), (13, 17))
        self.assertEqual(
            emf_result["provenance"]["operation"], "office_vector_convert_to_png"
        )
        convert.assert_called_once()

    def test_libreoffice_vector_conversion_requires_png_output(self) -> None:
        png = image_bytes("PNG", (4, 6), "yellow")

        def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            output = Path(command[command.index("--outdir") + 1])
            (output / "source.png").write_bytes(png)
            return subprocess.CompletedProcess(command, 0, stdout="convert source.emf as source.png")

        with mock.patch.object(
            materialize_visual_assets,
            "soffice_identity",
            return_value=("/opt/homebrew/bin/soffice", "LibreOffice test|binary_sha256=" + "b" * 64),
        ), mock.patch.object(materialize_visual_assets.subprocess, "run", side_effect=run):
            data, version = materialize_visual_assets.convert_vector_office_member_to_png(
                b"emf", ".emf", "soffice", self.output
            )
        self.assertEqual(data, png)
        self.assertIn("binary_sha256", version)

    def test_office_member_must_stay_under_known_media_directory(self) -> None:
        png = image_bytes("PNG", (2, 2), "white")
        office_path = self.root / "fixture.xlsx"
        with zipfile.ZipFile(office_path, "w") as archive:
            archive.writestr("xl/media/image1.png", png)
        office_data = office_path.read_bytes()
        record = self.record(
            "asset_zip_slip",
            "fixture.xlsx",
            office_data,
            {"kind": "office_embedded", "member_path": "xl/media/../image1.png"},
        )

        result = materialize_visual_assets.materialized_record(record, self.root, self.output)
        self.assertEqual(result["status"], "error")
        self.assertIn("unsafe Office member path", result["error"])

    def test_office_member_matches_nfd_archive_name_and_rejects_normalized_collision(self) -> None:
        png = image_bytes("PNG", (6, 3), "cyan")
        nfc_member = "word/media/café.png"
        nfd_member = unicodedata.normalize("NFD", nfc_member)
        office_path = self.root / "nfd.docx"
        with zipfile.ZipFile(office_path, "w") as archive:
            archive.writestr(nfd_member, png)
        office_data = office_path.read_bytes()
        record = self.record(
            "asset_office_nfd",
            "nfd.docx",
            office_data,
            {"kind": "office_embedded", "member_path": nfc_member},
        )

        result = materialize_visual_assets.materialized_record(record, self.root, self.output)
        self.assertEqual(result["status"], "materialized")
        self.assertEqual(result["materialized_sha256"], sha256(png))

        collision_path = self.root / "collision.docx"
        with zipfile.ZipFile(collision_path, "w") as archive:
            archive.writestr(nfc_member, png)
            archive.writestr(nfd_member, png)
        collision_data = collision_path.read_bytes()
        collision_record = self.record(
            "asset_office_nfc_collision",
            "collision.docx",
            collision_data,
            {"kind": "office_embedded", "member_path": nfc_member},
        )
        collision = materialize_visual_assets.materialized_record(
            collision_record, self.root, self.output
        )
        self.assertEqual(collision["status"], "error")
        self.assertIn("ambiguous Unicode-normalized Office member path", collision["error"])

    def test_pdf_renders_only_requested_page_and_then_uses_hash_cache(self) -> None:
        pdf_data = b"%PDF-1.4\nsynthetic-test-only\n%%EOF\n"
        (self.root / "scan.pdf").write_bytes(pdf_data)
        record = self.record(
            "asset_pdf_page_2",
            "scan.pdf",
            pdf_data,
            {"kind": "pdf_page", "page_number": 2},
        )
        rendered_png = image_bytes("PNG", (20, 30), "orange")

        def fake_pdftoppm(command, check, capture_output, text):
            self.assertFalse(check)
            self.assertTrue(capture_output)
            self.assertTrue(text)
            if command == ["pdftoppm", "-v"]:
                return subprocess.CompletedProcess(
                    command, 0, "", "pdftoppm version 25.06.0\n"
                )
            Path(str(command[-1]) + ".png").write_bytes(rendered_png)
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(
            materialize_visual_assets.subprocess, "run", side_effect=fake_pdftoppm
        ) as mocked_run:
            first = materialize_visual_assets.materialized_record(
                record, self.root, self.output, dpi=200
            )
        self.assertEqual(first["status"], "materialized")
        self.assertEqual((first["width"], first["height"]), (20, 30))
        command = next(
            call.args[0] for call in mocked_run.call_args_list if "-singlefile" in call.args[0]
        )
        self.assertEqual(command[command.index("-f") + 1], "2")
        self.assertEqual(command[command.index("-l") + 1], "2")
        self.assertEqual(command[command.index("-r") + 1], "200")
        self.assertIn("-singlefile", command)

        with mock.patch.object(
            materialize_visual_assets.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["pdftoppm", "-v"], 0, "", "pdftoppm version 25.06.0\n"
            ),
        ) as cached_run:
            second = materialize_visual_assets.materialized_record(
                record, self.root, self.output, dpi=200
            )
        self.assertEqual(cached_run.call_count, 1)
        self.assertEqual(cached_run.call_args.args[0], ["pdftoppm", "-v"])
        self.assertTrue(second["provenance"]["cache_hit"])

    def test_pdf_orphan_image_repairs_missing_metadata_after_exact_regeneration(self) -> None:
        pdf_data = b"%PDF-1.4\norphan-repair-test\n%%EOF\n"
        (self.root / "orphan.pdf").write_bytes(pdf_data)
        record = self.record(
            "asset_pdf_orphan",
            "orphan.pdf",
            pdf_data,
            {"kind": "pdf_page", "page_number": 1},
        )
        rendered_png = image_bytes("PNG", (12, 9), "lime")

        def fake_pdftoppm(command, check, capture_output, text):
            if command == ["pdftoppm", "-v"]:
                return subprocess.CompletedProcess(
                    command, 0, "", "pdftoppm version 25.06.0\n"
                )
            Path(str(command[-1]) + ".png").write_bytes(rendered_png)
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(
            materialize_visual_assets.subprocess, "run", side_effect=fake_pdftoppm
        ):
            first = materialize_visual_assets.materialized_record(
                record, self.root, self.output
            )
        target = self.root / str(first["materialized_path"])
        metadata = materialize_visual_assets.cache_metadata_path(target)
        metadata.unlink()
        self.assertTrue(target.is_file())
        self.assertFalse(metadata.exists())

        with mock.patch.object(
            materialize_visual_assets.subprocess, "run", side_effect=fake_pdftoppm
        ) as rerun:
            repaired = materialize_visual_assets.materialized_record(
                record, self.root, self.output
            )
        self.assertEqual(repaired["status"], "materialized")
        self.assertEqual(target.read_bytes(), rendered_png)
        self.assertTrue(metadata.is_file())
        self.assertTrue(any("-singlefile" in call.args[0] for call in rerun.call_args_list))

    def test_differing_orphan_image_is_not_overwritten_or_given_metadata(self) -> None:
        original = image_bytes("PNG", (5, 5), "black")
        (self.root / "orphan-source.png").write_bytes(original)
        record = self.record(
            "asset_orphan_collision",
            "orphan-source.png",
            original,
            {"kind": "standalone_image"},
        )
        first = materialize_visual_assets.materialized_record(record, self.root, self.output)
        target = self.root / str(first["materialized_path"])
        metadata = materialize_visual_assets.cache_metadata_path(target)
        metadata.unlink()
        differing = image_bytes("PNG", (5, 5), "white")
        target.write_bytes(differing)

        result = materialize_visual_assets.materialized_record(record, self.root, self.output)
        self.assertEqual(result["status"], "error")
        self.assertIn("refusing to overwrite differing file", result["error"])
        self.assertEqual(target.read_bytes(), differing)
        self.assertFalse(metadata.exists())

    def test_metadata_without_image_remains_a_hard_failure(self) -> None:
        original = image_bytes("PNG", (5, 2), "gray")
        (self.root / "missing-image-source.png").write_bytes(original)
        record = self.record(
            "asset_missing_cached_image",
            "missing-image-source.png",
            original,
            {"kind": "standalone_image"},
        )
        first = materialize_visual_assets.materialized_record(record, self.root, self.output)
        target = self.root / str(first["materialized_path"])
        metadata = materialize_visual_assets.cache_metadata_path(target)
        target.unlink()
        self.assertTrue(metadata.is_file())

        result = materialize_visual_assets.materialized_record(record, self.root, self.output)
        self.assertEqual(result["status"], "error")
        self.assertIn("incomplete materialization cache", result["error"])
        self.assertFalse(target.exists())
        self.assertTrue(metadata.is_file())

    def test_differing_existing_asset_is_never_overwritten(self) -> None:
        original = image_bytes("PNG", (4, 4), "purple")
        (self.root / "source.png").write_bytes(original)
        record = self.record(
            "asset_collision", "source.png", original, {"kind": "standalone_image"}
        )
        first = materialize_visual_assets.materialized_record(record, self.root, self.output)
        target = self.root / str(first["materialized_path"])
        changed = image_bytes("PNG", (4, 4), "yellow")
        target.write_bytes(changed)

        second = materialize_visual_assets.materialized_record(record, self.root, self.output)
        self.assertEqual(second["status"], "error")
        self.assertIn("cache hash mismatch", second["error"])
        self.assertEqual(target.read_bytes(), changed)

    def test_canonical_manifest_notebook_payload_advances_to_classification(self) -> None:
        png = image_bytes("PNG", (13, 8), "teal")
        notebook = {
            "cells": [{
                "cell_type": "code",
                "outputs": [{
                    "output_type": "display_data",
                    "data": {"image/png": base64.b64encode(png).decode("ascii")},
                }],
            }],
        }
        notebook_path = self.root / "analysis.ipynb"
        notebook_path.write_text(json.dumps(notebook), encoding="utf-8")
        notebook_data = notebook_path.read_bytes()
        discovery_provenance = {
            "builder": "visual-asset-manifest-builder",
            "question_independent": True,
        }
        record = {
            "schema_version": "0.1",
            "record_type": "visual_asset",
            "asset_id": "asset_" + "1" * 32,
            "source": {
                "relative_path": "analysis.ipynb",
                "sha256": sha256(notebook_data),
            },
            "origin": {
                "kind": "notebook_embedded_image",
                "page_number": None,
                "member_path": "cells/0/outputs/0/data/image/png",
                "member_sha256": sha256(png),
                "member_size_bytes": len(png),
                "media_type": "image/png",
            },
            "status": "pending_materialization",
            "materialized_path": None,
            "provenance": discovery_provenance,
        }

        result = materialize_visual_assets.materialized_record(
            record, self.root, self.output
        )
        self.assertEqual(result["status"], "pending_classification")
        self.assertIsNone(result["error"])
        self.assertEqual(result["provenance"], discovery_provenance)
        self.assertNotIn("materialized_sha256", result)
        materialization = result["materialization"]
        self.assertEqual(materialization["sha256"], sha256(png))
        self.assertEqual(materialization["size_bytes"], len(png))
        self.assertEqual(materialization["mime_type"], "image/png")
        self.assertEqual(
            (materialization["width_px"], materialization["height_px"]), (13, 8)
        )
        self.assertEqual(materialization["renderer"], "base64")
        self.assertEqual(materialization["renderer_version"], "RFC4648")
        self.assertIsNone(materialization["dpi"])
        self.assertFalse(materialization["cache_hit"])
        self.assertEqual(result["materialized_path"], materialization["output_path"])
        self.assertEqual(
            (self.root / result["materialized_path"]).read_bytes(), png
        )

        bad_record = {**record, "source": {**record["source"], "sha256": "0" * 64}}
        failed = materialize_visual_assets.materialized_record(
            bad_record, self.root, self.output
        )
        self.assertEqual(failed["status"], "materialization_error")
        self.assertIsNone(failed["materialized_path"])
        self.assertIsNone(failed["materialization"])
        self.assertIn("source SHA-256 mismatch", failed["error"])

    def test_notebook_attachment_matches_nfd_key_and_rejects_normalized_collision(self) -> None:
        png = image_bytes("PNG", (7, 10), "magenta")
        nfc_name = "résumé.png"
        nfd_name = unicodedata.normalize("NFD", nfc_name)

        def write_notebook(path: Path, attachment_names: list[str]) -> bytes:
            encoded = base64.b64encode(png).decode("ascii")
            notebook = {
                "cells": [{
                    "cell_type": "markdown",
                    "attachments": {
                        name: {"image/png": encoded} for name in attachment_names
                    },
                }],
            }
            path.write_text(json.dumps(notebook), encoding="utf-8")
            return path.read_bytes()

        notebook_path = self.root / "attachment.ipynb"
        notebook_data = write_notebook(notebook_path, [nfd_name])
        member_path = f"cells/0/attachments/{nfc_name}/image/png"
        record = self.record(
            "asset_notebook_nfd",
            "attachment.ipynb",
            notebook_data,
            {
                "kind": "notebook_embedded_image",
                "member_path": member_path,
                "member_sha256": sha256(png),
                "member_size_bytes": len(png),
                "media_type": "image/png",
            },
        )
        result = materialize_visual_assets.materialized_record(record, self.root, self.output)
        self.assertEqual(result["status"], "materialized")
        self.assertEqual(result["materialized_sha256"], sha256(png))

        collision_path = self.root / "attachment-collision.ipynb"
        collision_data = write_notebook(collision_path, [nfc_name, nfd_name])
        collision_record = self.record(
            "asset_notebook_nfc_collision",
            "attachment-collision.ipynb",
            collision_data,
            {
                "kind": "notebook_embedded_image",
                "member_path": member_path,
                "member_sha256": sha256(png),
                "member_size_bytes": len(png),
                "media_type": "image/png",
            },
        )
        collision = materialize_visual_assets.materialized_record(
            collision_record, self.root, self.output
        )
        self.assertEqual(collision["status"], "error")
        self.assertIn("ambiguous Unicode-normalized notebook attachment key", collision["error"])

        traversal_record = self.record(
            "asset_notebook_traversal",
            "attachment.ipynb",
            notebook_data,
            {
                "kind": "notebook_embedded_image",
                "member_path": "cells/0/attachments/../image/png",
                "member_sha256": sha256(png),
                "member_size_bytes": len(png),
                "media_type": "image/png",
            },
        )
        traversal = materialize_visual_assets.materialized_record(
            traversal_record, self.root, self.output
        )
        self.assertEqual(traversal["status"], "error")
        self.assertIn("unsafe notebook member_path", traversal["error"])


if __name__ == "__main__":
    unittest.main()
