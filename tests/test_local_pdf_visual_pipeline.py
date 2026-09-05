from __future__ import annotations

import contextlib
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import adapt_layer1_to_local_memory as adapter  # noqa: E402
import build_search_units as search_units  # noqa: E402
import local_image_ocr  # noqa: E402
import local_pdf_page_renderer as renderer  # noqa: E402
import local_visual_observation  # noqa: E402
import probe_intermediate_records as records  # noqa: E402


RUN_AT = "2031-04-01T00:00:00+00:00"
OCR_FRAME = "source_orientation_1_top_left_normalized_1000"


def png_header(width: int = 80, height: int = 40) -> bytes:
    """Return the bounded PNG header inspected by the local renderer."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", width, height)
    )


def minimal_pdf(width: int = 200, height: int = 100) -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
            "/Contents 4 0 R >>"
        ).encode("ascii"),
        b"<< /Length 0 >>\nstream\n\nendstream",
    ]
    body = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(body))
        body.extend(f"{number} 0 obj\n".encode("ascii"))
        body.extend(value)
        body.extend(b"\nendobj\n")
    xref = len(body)
    body.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    body.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    body.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(body)


class FakeMediaBox:
    width = 612.0
    height = 792.0


class FakePDFPage:
    def __init__(self, native_text: str = "") -> None:
        self.mediabox = FakeMediaBox()
        self.native_text = native_text

    def extract_text(self, *, visitor_text=None) -> str:
        if self.native_text and visitor_text is not None:
            visitor_text(
                self.native_text,
                [1.0, 0.0, 0.0, 1.0, 0.0, 0.0],
                [1.0, 0.0, 0.0, 1.0, 72.0, 720.0],
                None,
                12.0,
            )
        return self.native_text


def fake_pypdf(pages: list[FakePDFPage]) -> types.ModuleType:
    module = types.ModuleType("pypdf")
    module.PdfReader = lambda _path: types.SimpleNamespace(pages=pages)
    return module


def high_ocr_observation(image_path: Path, text: str) -> dict[str, object]:
    raw_sha256 = hashlib.sha256(image_path.read_bytes()).hexdigest()
    bbox = [100, 100, 400, 80]
    supporters = [
        {
            "pass": "apple_vision_primary",
            "engine": "apple_vision",
            "independence_group": "apple_vision",
            "line_id": "vision-1",
            "raw_text": text,
            "bbox": bbox,
            "bbox_coordinate_system": OCR_FRAME,
            "confidence": 0.95,
        },
        {
            "pass": "tesseract_psm3",
            "engine": "tesseract",
            "independence_group": "tesseract",
            "line_id": "tesseract-1",
            "raw_text": text,
            "bbox": bbox,
            "bbox_coordinate_system": OCR_FRAME,
            "confidence": 0.90,
        },
    ]
    line = {
        "text": text,
        "bbox": bbox,
        "bbox_coordinate_system": OCR_FRAME,
        "overlap": 1.0,
        "primary_confidence": 0.95,
        "audit_confidence": 0.90,
        "agreement_type": "independent_agreement",
        "quality_tier": "high",
        "provenance": {
            "primary_pass": "apple_vision_primary",
            "audit_pass": "tesseract_psm3",
            "primary_engine": "apple_vision",
            "audit_engine": "tesseract",
            "primary_independence_group": "apple_vision",
            "audit_independence_group": "tesseract",
            "primary_line_id": "vision-1",
            "audit_line_id": "tesseract-1",
            "primary_bbox_coordinate_system": OCR_FRAME,
            "audit_bbox_coordinate_system": OCR_FRAME,
            "comparison_coordinate_system": OCR_FRAME,
            "supporters": supporters,
        },
    }
    dimensions = {"width_px": 80, "height_px": 40}
    return {
        "input_sha256": raw_sha256,
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
        "ocr_input_sha256": raw_sha256,
        "ocr_input_dimensions": dimensions,
        "ocr_input_orientation": 1,
        "coordinate_frame_policy": "canonical_orientation_1",
        "engines": {
            "apple_vision_primary": {"status": "completed"},
            "tesseract_psm3": {"status": "completed"},
        },
        "independent_engines": True,
        "consensus_lines": [line],
        "read_lines": [line],
        "unlocated_transcript": None,
        "unresolved_count": 0,
    }


def provisional_chart_observation(image_path: Path) -> dict[str, object]:
    image_sha256 = records.digest_file(image_path)
    observation = {
        "visible_objects": [
            {"object_id": "o1", "kind": "chart", "description": "棒グラフ"}
        ],
        "explicit_labels": [
            {"label_id": "l1", "text": "2031年"},
            {"label_id": "l2", "text": "処理件数"},
        ],
        "explicit_relations": [
            {"source_ref": "l2", "relation": "labels", "target_ref": "o1"}
        ],
        "labeled_values": [
            {
                "value_id": "v1",
                "label_text": "2031年",
                "series_label": "処理件数",
                "value_text": "120",
                "unit_text": "件",
                "value_status": "exact_label",
                "unclear_reason": "",
            }
        ],
        "warnings": [],
    }
    return {
        "schema_version": "0.1",
        "record_type": "local_visual_observation",
        "observation_type": "whole_image_literal_visual_observation",
        "status": "provisional",
        "quality_tier": "provisional",
        "provisional_marker": records.PROVISIONAL_OCR_MARKER,
        "text": "\n".join([
            "[暫定読取] 見える対象 o1: chart; 棒グラフ",
            "[暫定読取] 明示ラベル l1: 2031年",
            "[暫定読取] 明示ラベル l2: 処理件数",
            "[暫定読取] 明示関係: l2 labels o1",
            "[暫定読取] ラベル付き明記値: 2031年 / 処理件数 = 120 件",
        ]),
        "observation": observation,
        "question_independent": True,
        "model": "gemma4:12b",
        "model_digest": "a" * 64,
        "prompt_sha256": local_visual_observation.VISUAL_OBSERVATION_PROMPT_SHA256,
        "input_image_sha256": image_sha256,
        "model_output_sha256": "b" * 64,
        "runner": "ollama_loopback_chat",
        "runner_version": local_visual_observation.VISUAL_OBSERVATION_VERSION,
        "host": "127.0.0.1",
        "port": 11434,
        "temperature": 0,
        "strict_json": True,
        "external_network_used": False,
        "downloads_performed": False,
    }
def write_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(records.canonical_json(value) + "\n" for value in values),
        encoding="utf-8",
    )


def materialize_intermediate(
    probe: records.Probe,
    source_root: Path,
    output: Path,
) -> None:
    output.mkdir()
    write_jsonl(output / "documents.jsonl", probe.documents)
    # SearchUnit reconstruction is stream-sensitive: preserve extraction order.
    write_jsonl(output / "evidence.jsonl", probe.evidence)
    write_jsonl(output / "relations.jsonl", probe.relations)
    document = probe.documents[0]
    relative_path = document["source"]["relative_path"]
    evidence_path = output / "evidence.jsonl"
    state = {
        "state_version": "1",
        "build_status": "complete",
        "source_root": str(source_root.resolve()),
        "extractor": probe.extractor,
        "extractor_version": probe.extractor_version,
        "run_at": probe.run_at,
        "input_paths": [relative_path],
        "entries": {
            relative_path: {
                "document_id": document["document_id"],
                "relative_path": relative_path,
                "source_sha256": document["source"]["sha256"],
                "status": document["extraction"]["status"],
                "shards": {
                    "evidence": {
                        "relative_path": evidence_path.name,
                        "sha256": records.digest_file(evidence_path),
                        "size_bytes": evidence_path.stat().st_size,
                        "record_count": len(probe.evidence),
                    }
                },
            }
        },
        "totals": {
            "documents": len(probe.documents),
            "evidence": len(probe.evidence),
            "relations": len(probe.relations),
        },
    }
    (output / "build-state.json").write_text(
        records.canonical_json(state) + "\n", encoding="utf-8"
    )


class LocalPDFPageRendererTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "PDFKit JXA is macOS-only")
    def test_pdfkit_jxa_binds_source_and_renders_real_png_without_clt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-pdf-render-contract-") as temporary:
            root = Path(temporary)
            source = root / "fixture.pdf"
            source.write_bytes(minimal_pdf())
            output = root / "page.png"
            inspection = renderer.inspect_pdf(source, timeout=9.0)
            result = renderer.render_pdf_page(source, 1, output, dpi=144, timeout=9.0)

            self.assertEqual(inspection["page_count"], 1)
            self.assertEqual(inspection["backend"], "apple_pdfkit_jxa")
            self.assertEqual(len(inspection["pages"]), 1)
            self.assertEqual(result["runner"], renderer.RUNNER)
            self.assertEqual(result["backend"], "apple_pdfkit_jxa")
            self.assertFalse(result["external_network_used"])
            self.assertEqual(result["page_number"], 1)
            self.assertEqual(result["dpi"], 144)
            self.assertEqual((result["width_px"], result["height_px"]), (400, 200))
            self.assertEqual(result["source_sha256"], records.digest_file(source))
            self.assertEqual(result["rendered_sha256"], records.digest_file(output))
            self.assertEqual(result["rendered_size_bytes"], output.stat().st_size)
            self.assertEqual(oct(output.stat().st_mode & 0o777), "0o600")

    def test_pdfkit_inspection_is_source_bound_and_requires_an_unlocked_pdf(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiec-pdf-inspect-contract-") as temporary:
            root = Path(temporary)
            source = root / "fixture.pdf"
            source.write_bytes(minimal_pdf())
            with renderer.snapshot_pdf(source) as snapshot:
                locked = {
                    "status": "completed",
                    "runner": renderer.SWIFT_RUNNER,
                    "runner_version": renderer.SWIFT_RUNNER_VERSION,
                    "page_count": 1,
                    "encrypted": True,
                    "locked": True,
                    "pages": [],
                    "_backend_executable_sha256": "a" * 64,
                }
                with (
                    mock.patch.object(renderer, "_run_pdfkit", return_value=locked),
                    self.assertRaisesRegex(RuntimeError, "output contract"),
                ):
                    renderer.inspect_pdf_snapshot(snapshot, timeout=9.0)

    def test_page_count_dimension_and_dangling_output_limits_fail_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "page count"):
            renderer._bounded_page_count(renderer.MAX_PDF_PAGES + 1)
        with self.assertRaisesRegex(RuntimeError, "pixel safety"):
            renderer._validate_page_geometry(100_000.0, 100_000.0, 200)
        with tempfile.TemporaryDirectory(prefix="aiec-pdf-output-contract-") as temporary:
            root = Path(temporary)
            source = root / "fixture.pdf"
            source.write_bytes(minimal_pdf())
            output = root / "page.png"
            os.symlink(root / "missing.png", output)
            with self.assertRaisesRegex(ValueError, "must not already exist"):
                renderer.render_pdf_page(source, 1, output, timeout=9.0)


class LocalPDFVisualPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aiec-pdf-visual-")
        self.work = Path(self.temporary.name)
        self.source_root = self.work / "source"
        self.source_root.mkdir()
        self.pdf_path = self.source_root / "fictional-scan.pdf"
        self.pdf_path.write_bytes(b"%PDF-1.4\n% fictional test source\n")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def render_page(
        source,
        page_number: int,
        output: Path,
        *,
        dpi: int,
        require_native_text: bool = False,
    ):
        del require_native_text
        output.write_bytes(png_header())
        rendered_sha256 = records.digest_file(output)
        source_sha256 = getattr(source, "source_sha256", None)
        if source_sha256 is None:
            source_sha256 = records.digest_file(source)
        return {
            "runner": renderer.RUNNER,
            "runner_version": renderer.RUNNER_VERSION,
            "backend": "fixture_renderer",
            "backend_version": "test",
            "backend_executable_sha256": "f" * 64,
            "external_network_used": False,
            "source_sha256": source_sha256,
            "page_number": page_number,
            "dpi": dpi,
            "rendered_sha256": rendered_sha256,
            "rendered_size_bytes": output.stat().st_size,
            "width_px": 80,
            "height_px": 40,
            "page_count": 1,
            "page_width_pt": 612.0,
            "page_height_pt": 792.0,
            "page_rotation": 0,
            "native_text": "",
        }

    def extract_with_pages(
        self,
        pages: list[FakePDFPage],
        ocr_text_by_page: dict[int, str],
        *,
        render_side_effect=None,
        visual_side_effect=None,
    ) -> records.Probe:
        probe = records.Probe(
            self.source_root,
            RUN_AT,
            None,
            diagnostic=False,
        )

        def read_rendered_page(path: Path):
            page_number = int(path.stem.rsplit("-", 1)[1])
            return high_ocr_observation(path, ocr_text_by_page[page_number])

        snapshot = types.SimpleNamespace(
            path=self.pdf_path,
            source_sha256=records.digest_file(self.pdf_path),
            source_size_bytes=self.pdf_path.stat().st_size,
            helper_path=self.pdf_path,
            helper_sha256=records.digest_file(self.pdf_path),
        )
        inspection = {
            "page_count": len(pages),
            "pages": [
                {
                    "page_number": index,
                    "page_width_pt": 612.0,
                    "page_height_pt": 792.0,
                    "page_rotation": 0,
                    "render_width_px": 1700,
                    "render_height_px": 2200,
                }
                for index in range(1, len(pages) + 1)
            ],
        }

        def read_page(snapshot_value, page_number, *, dpi):
            return {
                "runner": renderer.RUNNER,
                "runner_version": renderer.RUNNER_VERSION,
                "external_network_used": False,
                "source_sha256": snapshot_value.source_sha256,
                "page_number": page_number,
                "dpi": dpi,
                "page_count": len(pages),
                "page_width_pt": 612.0,
                "page_height_pt": 792.0,
                "page_rotation": 0,
                "native_text": pages[page_number - 1].native_text,
            }

        def render(snapshot_value, page_number, output, **kwargs):
            target = render_side_effect or self.render_page
            result = target(snapshot_value, page_number, output, **kwargs)
            result.setdefault("page_count", len(pages))
            result.setdefault("page_width_pt", 612.0)
            result.setdefault("page_height_pt", 792.0)
            result.setdefault("page_rotation", 0)
            if render_side_effect is None:
                result["native_text"] = pages[page_number - 1].native_text
            else:
                result.setdefault("native_text", pages[page_number - 1].native_text)
            return result

        visual_patch = (
            mock.patch.object(
                records.Probe, "_add_local_visual_observation", return_value=False
            )
            if visual_side_effect is None
            else mock.patch.object(
                local_visual_observation,
                "observe_path",
                side_effect=visual_side_effect,
            )
        )
        with (
            mock.patch.object(
                renderer,
                "snapshot_pdf",
                return_value=contextlib.nullcontext(snapshot),
            ),
            mock.patch.object(
                renderer, "inspect_pdf_snapshot", return_value=inspection
            ),
            mock.patch.object(
                renderer, "read_pdf_snapshot_page", side_effect=read_page
            ),
            mock.patch.object(
                renderer, "render_pdf_snapshot_page", side_effect=render
            ),
            mock.patch.object(local_image_ocr, "extract", side_effect=read_rendered_page),
            visual_patch,
        ):
            probe.extract(self.pdf_path)
        return probe

    def test_two_visual_pages_reach_page_bound_image_packets_and_semantic_evidence(self) -> None:
        texts = {
            1: "架空案件アルファの責任者は佐藤",
            2: "架空案件アルファの期限は2031年4月30日",
        }
        probe = self.extract_with_pages(
            [FakePDFPage(), FakePDFPage()], texts
        )

        self.assertEqual(probe.documents[0]["extraction"]["status"], "success")
        pages = [item for item in probe.evidence if item["evidence_type"] == "page"]
        images = [item for item in probe.evidence if item["evidence_type"] == "image"]
        ocr_lines = [item for item in probe.evidence if item["evidence_type"] == "ocr_line"]
        self.assertEqual([item["location"]["page_number"] for item in pages], [1, 2])
        self.assertEqual([item["location"]["page_number"] for item in images], [1, 2])
        self.assertEqual([item["location"]["page_number"] for item in ocr_lines], [1, 2])
        image_by_id = {item["evidence_id"]: item for item in images}
        for line in ocr_lines:
            page_number = line["location"]["page_number"]
            parent = image_by_id[line["parent_evidence_id"]]
            origin = line["native_properties"]["visual_origin"]
            self.assertEqual(parent["location"]["page_number"], page_number)
            self.assertEqual(origin["kind"], "pdf_page_image")
            self.assertEqual(origin["source_location"], parent["location"])
            self.assertEqual(
                origin, parent["native_properties"]["visual_origin"]
            )
            self.assertEqual(origin["source_relative_path"], self.pdf_path.name)
            self.assertEqual(origin["source_sha256"], records.digest_file(self.pdf_path))
            self.assertEqual(origin["materialization"]["page_number"], page_number)

        intermediate = self.work / "intermediate"
        search_output = self.work / "search"
        semantic_output = self.work / "semantic"
        materialize_intermediate(probe, self.source_root, intermediate)
        search_state = search_units.build(intermediate, search_output, 500)
        semantic_state = adapter.adapt(
            intermediate,
            self.source_root.resolve(),
            semantic_output,
            search_output,
        )

        self.assertEqual(search_state["counts_by_type"], {"image_text_packet": 2})
        packets = [
            json.loads(line)
            for line in (search_output / "search_units.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual([item["locator"]["page_number"] for item in packets], [1, 2])
        self.assertTrue(all(
            item["context"]["container_kind"] == "pdf_page_image"
            for item in packets
        ))

        semantic = [
            json.loads(line)
            for line in (semantic_output / "semantic-evidence.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        packet_evidence = [
            item
            for item in semantic
            if item.get("adapter", {}).get("unit_type") == "image_text_packet"
        ]
        self.assertEqual(len(packet_evidence), 2)
        self.assertEqual(
            [item["locator"]["page_number"] for item in packet_evidence],
            [1, 2],
        )
        for page_number, projected in enumerate(packet_evidence, 1):
            self.assertIn(texts[page_number], projected["observed_text"])
            self.assertEqual(projected["source"]["relative_path"], self.pdf_path.name)
            self.assertEqual(projected["quality_tier"], "high")
            self.assertEqual(projected["agreement_types"], ["independent_agreement"])
        self.assertEqual(
            semantic_state["search_unit_projection"]["image_quality_counts"],
            {"high": 2},
        )

    def test_pdfkit_fallback_preserves_native_page_text_without_pypdf(self) -> None:
        native_text = "Project ID: FALLBACK-31"
        probe = self.extract_with_pages(
            [FakePDFPage(native_text)], {1: native_text}
        )
        page = next(
            item for item in probe.evidence if item["evidence_type"] == "page"
        )
        self.assertEqual(page["content"]["raw_text"], native_text)
        self.assertEqual(page["location"]["page_number"], 1)
        located = [
            item for item in probe.evidence
            if item["evidence_type"] == "ocr_line"
        ]
        self.assertEqual(1, len(located))
        self.assertEqual(native_text, located[0]["content"]["raw_text"])
        self.assertEqual("high", located[0]["native_properties"]["quality_tier"])
        self.assertEqual(
            probe.documents[0]["extraction"]["parser"],
            "pdfkit-jxa+local-page-render+adaptive-local-image-reader",
        )

    def test_native_text_keeps_identical_ocr_geometry_without_duplicate_block(self) -> None:
        native_text = "Native total 24 hours"
        probe = self.extract_with_pages(
            [FakePDFPage(native_text)], {1: native_text}
        )

        self.assertEqual(probe.documents[0]["extraction"]["status"], "success")
        page = next(item for item in probe.evidence if item["evidence_type"] == "page")
        images = [item for item in probe.evidence if item["evidence_type"] == "image"]
        ocr_lines = [item for item in probe.evidence if item["evidence_type"] == "ocr_line"]
        self.assertEqual(page["content"]["raw_text"], native_text)
        self.assertFalse(any(
            item["evidence_type"] == "text_block" for item in probe.evidence
        ))
        self.assertEqual(len(images), 1)
        self.assertEqual(len(ocr_lines), 1)
        self.assertEqual(ocr_lines[0]["content"]["raw_text"], native_text)
        self.assertEqual(ocr_lines[0]["native_properties"]["quality_tier"], "high")
        self.assertEqual(ocr_lines[0]["geometry"]["coordinate_space"], "image")

    def test_chart_meaning_is_searchable_but_remains_provisional(self) -> None:
        def observe_chart(path: Path, *, expected_input_sha256: str):
            self.assertEqual(expected_input_sha256, records.digest_file(path))
            return provisional_chart_observation(path)

        probe = self.extract_with_pages(
            [FakePDFPage()],
            {1: "2031年 処理件数 120件"},
            visual_side_effect=observe_chart,
        )

        visual = next(
            item
            for item in probe.evidence
            if item.get("provenance", {}).get("extraction_method")
            == "local_vlm_visual_observation_provisional"
        )
        self.assertEqual(visual["location"]["page_number"], 1)
        self.assertEqual(
            visual["native_properties"]["quality_tier"], "provisional"
        )
        self.assertEqual(
            visual["native_properties"]["provisional_marker"],
            records.PROVISIONAL_OCR_MARKER,
        )
        self.assertEqual(
            visual["native_properties"]["visual_origin"]["kind"],
            "pdf_page_image",
        )

        intermediate = self.work / "visual-intermediate"
        search_output = self.work / "visual-search"
        semantic_output = self.work / "visual-semantic"
        materialize_intermediate(probe, self.source_root, intermediate)
        search_units.build(intermediate, search_output, 500)
        adapter.adapt(
            intermediate,
            self.source_root.resolve(),
            semantic_output,
            search_output,
        )

        search_records = [
            json.loads(line)
            for line in (search_output / "search_units.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        chart_units = [
            item
            for item in search_records
            if "ラベル付き明記値" in item["text"]["search_text"]
        ]
        self.assertEqual(1, len(chart_units))
        self.assertEqual(chart_units[0]["locator"]["page_number"], 1)
        self.assertIn("[暫定読取]", chart_units[0]["text"]["search_text"])

        semantic = [
            json.loads(line)
            for line in (semantic_output / "semantic-evidence.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        projected = [
            item
            for item in semantic
            if item.get("extraction_method")
            == "local_vlm_visual_observation_provisional"
        ]
        self.assertEqual(1, len(projected))
        self.assertEqual(projected[0]["quality_tier"], "provisional")
        self.assertEqual(
            projected[0]["provisional_marker"],
            records.PROVISIONAL_OCR_MARKER,
        )

    def test_render_failure_is_partial_and_preserves_native_page_text(self) -> None:
        native_text = "Native source remains available"

        def fail_render(*_args, **_kwargs):
            raise RuntimeError("fictional renderer failure")

        probe = self.extract_with_pages(
            [FakePDFPage(native_text)],
            {1: "must not be read"},
            render_side_effect=fail_render,
        )

        document = probe.documents[0]
        self.assertEqual(document["extraction"]["status"], "partial")
        self.assertTrue(any(
            "page 1 local visual reading unavailable" in warning
            and "fictional renderer failure" in warning
            for warning in document["extraction"]["warnings"]
        ))
        page = next(item for item in probe.evidence if item["evidence_type"] == "page")
        self.assertEqual(page["content"]["raw_text"], native_text)
        self.assertFalse(any(
            item["evidence_type"] == "text_block" for item in probe.evidence
        ))
        self.assertFalse(any(
            item["evidence_type"] in {"image", "ocr_line"}
            for item in probe.evidence
        ))

    def test_visual_budget_exhaustion_does_not_drop_later_native_pages(self) -> None:
        pages = [
            FakePDFPage("1ページ目のネイティブ文字"),
            FakePDFPage("2ページ目のネイティブ文字"),
        ]
        with mock.patch.object(renderer, "MAX_PDF_DOCUMENT_SECONDS", 0.0):
            probe = self.extract_with_pages(pages, {})

        extracted_pages = [
            item for item in probe.evidence if item["evidence_type"] == "page"
        ]
        self.assertEqual(2, len(extracted_pages))
        self.assertEqual(
            ["1ページ目のネイティブ文字", "2ページ目のネイティブ文字"],
            [item["content"]["raw_text"] for item in extracted_pages],
        )
        self.assertFalse(any(
            item["evidence_type"] in {"image", "ocr_line"}
            for item in probe.evidence
        ))
        self.assertEqual("partial", probe.documents[0]["extraction"]["status"])
        self.assertTrue(any(
            "visual reading stopped before page 1" in warning
            for warning in probe.documents[0]["extraction"]["warnings"]
        ))

    def test_deferred_store_contract_error_escapes_pdf_partial_wrapper(self) -> None:
        with mock.patch.object(
            records.Probe,
            "_project_local_image_evidence",
            side_effect=records.DeferredVisualStoreError(
                "fixture private spool contract failure"
            ),
        ):
            with self.assertRaisesRegex(
                records.DeferredVisualStoreError,
                "fixture private spool contract failure",
            ):
                self.extract_with_pages(
                    [FakePDFPage("native text remains source-bound")],
                    {1: "unused OCR"},
                )

    def test_pdf_render_materialization_mismatches_are_hard_failures(self) -> None:
        cases = (
            (
                "source",
                lambda result: result.__setitem__("source_sha256", "0" * 64),
                "rendered PDF page is not bound",
            ),
            (
                "geometry",
                lambda result: result.__setitem__("page_width_pt", 611.0),
                "PDF page changed between text read and visual render",
            ),
            (
                "native_text",
                lambda result: result.__setitem__("native_text", "changed"),
                "PDF page changed between text read and visual render",
            ),
            (
                "rendered_digest",
                lambda result: result.__setitem__("rendered_sha256", "invalid"),
                "visual materialization digest or size is invalid",
            ),
            (
                "forged_rendered_digest",
                lambda result: result.__setitem__("rendered_sha256", "a" * 64),
                "rendered PDF page differs from its materialization contract",
            ),
            (
                "forged_rendered_size",
                lambda result: result.__setitem__(
                    "rendered_size_bytes", 1024 * 1024 * 1024 + 1
                ),
                "rendered PDF page differs from its materialization contract",
            ),
        )
        for name, mutate, message in cases:
            with self.subTest(name=name):
                def invalid_render(*args, mutation=mutate, **kwargs):
                    result = self.render_page(*args, **kwargs)
                    result["native_text"] = "native text remains source-bound"
                    mutation(result)
                    return result

                with self.assertRaisesRegex(
                    records.DeferredVisualStoreError,
                    message,
                ):
                    self.extract_with_pages(
                        [FakePDFPage("native text remains source-bound")],
                        {1: "unused OCR"},
                        render_side_effect=invalid_render,
                    )


if __name__ == "__main__":
    unittest.main()
