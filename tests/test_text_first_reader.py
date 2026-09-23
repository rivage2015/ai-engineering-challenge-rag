from __future__ import annotations

import base64
import contextlib
import hashlib
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import local_image_ocr as image_reader  # noqa: E402
import local_pdf_page_renderer as pdf_reader  # noqa: E402
import local_visual_observation as visual_reader  # noqa: E402
import probe_intermediate_records as records  # noqa: E402


RUN_AT = "2031-04-01T00:00:00+00:00"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
XDR_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4"
    "2mP8/x8AAusB9Y9ZK7sAAAAASUVORK5CYII="
)
PNG_SHA256 = hashlib.sha256(PNG_BYTES).hexdigest()


def relationships(*items: tuple[str, str, str]) -> str:
    return f'<Relationships xmlns="{PR_NS}">' + "".join(
        f'<Relationship Id="{identity}" Type="{R_NS}/{kind}" Target="{target}"/>'
        for identity, kind, target in items
    ) + "</Relationships>"


def write_office_fixture(
    path: Path, *, include_image: bool = True, image_occurrences: int = 1,
) -> None:
    """Small synthetic OOXML containers; never use personal documents."""
    content_types = (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="png" ContentType="image/png"/></Types>'
    )
    members: dict[str, str | bytes] = {"[Content_Types].xml": content_types}
    if path.suffix == ".xlsx":
        members.update({
            "_rels/.rels": relationships(("rIdWorkbook", "officeDocument", "xl/workbook.xml")),
            "xl/workbook.xml": (
                f'<workbook xmlns="{S_NS}" xmlns:r="{R_NS}"><sheets>'
                '<sheet name="架空の予定" sheetId="1" r:id="rIdSheet"/></sheets></workbook>'
            ),
            "xl/_rels/workbook.xml.rels": relationships(("rIdSheet", "worksheet", "worksheets/sheet1.xml")),
            "xl/worksheets/sheet1.xml": (
                f'<worksheet xmlns="{S_NS}" xmlns:r="{R_NS}"><sheetData>'
                '<row r="1"><c r="A1" t="inlineStr"><is><t>対象と条件</t></is></c></row>'
                '<row r="2"><c r="A2" t="inlineStr"><is><t>火曜日のみ</t></is></c>'
                '<c r="B2"><v>15</v></c><c r="C2"><f>B2*2</f><v>30</v></c></row>'
                '</sheetData><mergeCells count="1"><mergeCell ref="A1:C1"/></mergeCells>'
                + ('<drawing r:id="rIdDrawing"/>' if include_image else "")
                + '</worksheet>'
            ),
        })
        if include_image:
            members.update({
                "xl/worksheets/_rels/sheet1.xml.rels": relationships(("rIdDrawing", "drawing", "../drawings/drawing1.xml")),
                "xl/drawings/drawing1.xml": (
                    f'<xdr:wsDr xmlns:xdr="{XDR_NS}" xmlns:a="{A_NS}" xmlns:r="{R_NS}">'
                    '<xdr:oneCellAnchor><xdr:from><xdr:col>0</xdr:col><xdr:colOff>0</xdr:colOff>'
                    '<xdr:row>3</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>'
                    '<xdr:ext cx="914400" cy="914400"/><xdr:pic><xdr:nvPicPr>'
                    '<xdr:cNvPr id="1" name="図"/><xdr:cNvPicPr/></xdr:nvPicPr>'
                    '<xdr:blipFill><a:blip r:embed="rIdImage"/></xdr:blipFill>'
                    '<xdr:spPr/></xdr:pic><xdr:clientData/></xdr:oneCellAnchor></xdr:wsDr>'
                ),
                "xl/drawings/_rels/drawing1.xml.rels": relationships(("rIdImage", "image", "../media/image1.png")),
                "xl/media/image1.png": PNG_BYTES,
            })
    elif path.suffix == ".docx":
        members.update({
            "_rels/.rels": relationships(("rIdDocument", "officeDocument", "word/document.xml")),
            "word/document.xml": (
                f'<w:document xmlns:w="{W_NS}" xmlns:a="{A_NS}" xmlns:r="{R_NS}"><w:body>'
                '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>対象と条件</w:t></w:r></w:p>'
                '<w:p><w:r><w:t>火曜日のみ実施します。</w:t></w:r></w:p>'
                '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>数量</w:t></w:r></w:p></w:tc>'
                '<w:tc><w:p><w:r><w:t>15個</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
                + ('<w:p><w:r><w:drawing><a:blip r:embed="rIdImage"/></w:drawing></w:r></w:p>' * image_occurrences if include_image else "")
                + '</w:body></w:document>'
            ),
        })
        if include_image:
            members.update({
                "word/_rels/document.xml.rels": relationships(("rIdImage", "image", "media/image1.png")),
                "word/media/image1.png": PNG_BYTES,
            })
    elif path.suffix == ".pptx":
        picture = (
            '<p:pic><p:nvPicPr><p:cNvPr id="3" name="図"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
            '<p:blipFill><a:blip r:embed="rIdImage"/></p:blipFill>'
            '<p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="914400" cy="914400"/></a:xfrm></p:spPr></p:pic>'
        ) if include_image else ""
        members.update({
            "_rels/.rels": relationships(("rIdPresentation", "officeDocument", "ppt/presentation.xml")),
            "ppt/presentation.xml": (
                f'<p:presentation xmlns:p="{P_NS}" xmlns:r="{R_NS}"><p:sldIdLst>'
                '<p:sldId id="256" r:id="rIdSlide"/></p:sldIdLst>'
                '<p:sldSz cx="9144000" cy="6858000"/></p:presentation>'
            ),
            "ppt/_rels/presentation.xml.rels": relationships(("rIdSlide", "slide", "slides/slide1.xml")),
            "ppt/slides/slide1.xml": (
                f'<p:sld xmlns:p="{P_NS}" xmlns:a="{A_NS}" xmlns:r="{R_NS}"><p:cSld><p:spTree>'
                '<p:sp><p:nvSpPr><p:cNvPr id="2" name="本文"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
                '<p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>火曜日のみ15個</a:t></a:r></a:p></p:txBody></p:sp>'
                + picture + '</p:spTree></p:cSld></p:sld>'
            ),
        })
        if include_image:
            members.update({
                "ppt/slides/_rels/slide1.xml.rels": relationships(("rIdImage", "image", "../media/image1.png")),
                "ppt/media/image1.png": PNG_BYTES,
            })
    else:
        raise AssertionError(f"unsupported synthetic fixture: {path.suffix}")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, value in sorted(members.items()):
            archive.writestr(name, value)


class TextFirstReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="lms-text-first-reader-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # An attempted expensive call is a failure even if Reader catches its
        # exception and labels the document partial. Check call counts as well.
        self.expensive_calls = [self.enterContext(mock.patch.object(
            owner, name, side_effect=AssertionError(f"{name} forbidden in text-first test")
        )) for owner, name in (
            (image_reader, "extract"),
            (visual_reader, "observe_path"),
            (visual_reader, "_ollama_json"),
            (visual_reader, "_run_isolated_task"),
            (pdf_reader, "render_pdf_snapshot_page"),
        )]

    def tearDown(self) -> None:
        for guarded in self.expensive_calls:
            guarded.assert_not_called()

    def reader(self, policy: str = "text_first_v1") -> records.Probe:
        return records.Probe(self.root, RUN_AT, None, diagnostic=False, reading_policy=policy)

    def extract_office(self, path: Path, policy: str) -> records.Probe:
        reader = self.reader(policy)
        # Force the supported stdlib readers so this fixture does not depend on
        # a test runner's optional Office libraries. In the full-policy control
        # only the already-covered visual expansion is replaced with a no-op.
        with mock.patch.dict(sys.modules, {"docx": None, "openpyxl": None, "pptx": None}):
            if policy == "full":
                with mock.patch.object(reader, "_project_embedded_image_bytes", return_value=0):
                    reader.extract(path)
            else:
                reader.extract(path)
        return reader

    def assert_pending_bound(self, reader: records.Probe, kind: str, count: int) -> list[dict]:
        document = reader.documents[0]
        self.assertEqual(document["extraction"]["reading_policy"], "text_first_v1")
        coverage = document["extraction"]["visual_coverage"]
        self.assertEqual(coverage["status"], "pending")
        pending = coverage["pending"]
        self.assertEqual(len(pending), count)
        self.assertEqual(document["extraction"]["status"], "partial")
        by_id = {item["evidence_id"]: item for item in reader.evidence}
        for item in pending:
            self.assertEqual(item["kind"], kind)
            self.assertEqual(item["reason"], "deferred_by_reading_policy")
            self.assertEqual(item["source_sha256"], document["source"]["sha256"])
            evidence = by_id[item["evidence_id"]]
            self.assertEqual(item["location"], evidence["location"])
            self.assertEqual(evidence["document_id"], document["document_id"])
            if kind == "pdf_page":
                self.assertNotIn("image_sha256", item)
            else:
                self.assertEqual(item["image_sha256"], PNG_SHA256)
                self.assertEqual(evidence["evidence_type"], "image")
                self.assertIn("content_ref", evidence["content"])
                self.assertNotIn("raw_text", evidence["content"])
                self.assertNotIn("raw_value", evidence["content"])
        return pending

    def test_office_native_payload_and_relations_match_full_policy(self) -> None:
        for suffix in (".xlsx", ".docx", ".pptx"):
            with self.subTest(suffix=suffix):
                path = self.root / ("fictional" + suffix)
                write_office_fixture(path)
                original = path.read_bytes()
                full = self.extract_office(path, "full")
                candidate = self.extract_office(path, "text_first_v1")
                full_native = [item for item in full.evidence if item["evidence_type"] != "image"]
                candidate_native = [item for item in candidate.evidence if item["evidence_type"] != "image"]
                self.assertTrue(full_native)
                self.assertEqual(candidate_native, full_native)
                self.assertEqual(candidate.relations, full.relations)
                self.assertEqual(path.read_bytes(), original)
                self.assert_pending_bound(candidate, "embedded_image", 1)
                self.assertNotIn("reading_policy", full.documents[0]["extraction"])
                self.assertNotIn("visual_coverage", full.documents[0]["extraction"])

    def test_xlsx_keeps_cell_formula_cached_value_merge_and_sheet_location(self) -> None:
        path = self.root / "cells.xlsx"
        write_office_fixture(path)
        reader = self.extract_office(path, "text_first_v1")
        cells = {item["location"]["cell"]: item for item in reader.evidence if item["evidence_type"] == "table_cell"}
        self.assertEqual(cells["A2"]["content"]["raw_value"], "火曜日のみ")
        self.assertEqual(cells["B2"]["content"]["raw_value"], 15)
        self.assertEqual(cells["C2"]["content"]["raw_value"], 30)
        self.assertEqual(cells["C2"]["native_properties"]["cached_value_status"], records.FORMULA_CACHED_VALUE_STATUS)
        self.assertTrue(all(item["location"]["sheet_name"] == "架空の予定" for item in cells.values()))
        formulas = [item for item in reader.evidence if item["evidence_type"] == "formula"]
        self.assertEqual(len(formulas), 1)
        self.assertEqual(formulas[0]["content"]["raw_text"], "=B2*2")
        self.assertEqual(formulas[0]["parent_evidence_id"], cells["C2"]["evidence_id"])
        merges = [item for item in reader.evidence if item["evidence_type"] == "merged_range"]
        self.assertEqual(merges[0]["location"]["range"], "A1:C1")
        pending = self.assert_pending_bound(reader, "embedded_image", 1)
        self.assertEqual(pending[0]["location"]["sheet_name"], "架空の予定")
        self.assertEqual(pending[0]["location"]["source_member"], "xl/media/image1.png")

    def test_no_image_does_not_erase_existing_native_fallback_limit(self) -> None:
        path = self.root / "native-only.xlsx"
        write_office_fixture(path, include_image=False)
        full = self.extract_office(path, "full")
        candidate = self.extract_office(path, "text_first_v1")
        extraction = candidate.documents[0]["extraction"]
        self.assertEqual(extraction["visual_coverage"], {"status": "none_pending", "pending": []})
        self.assertEqual(extraction["status"], "partial")
        self.assertEqual(extraction["warnings"], full.documents[0]["extraction"]["warnings"])
        self.assertTrue(any("fallback" in item for item in extraction["warnings"]))

    def test_same_embedded_bytes_at_two_locations_keep_both_pending_occurrences(self) -> None:
        path = self.root / "two-placements.docx"
        write_office_fixture(path, image_occurrences=2)
        reader = self.extract_office(path, "text_first_v1")
        pending = self.assert_pending_bound(reader, "embedded_image", 2)
        self.assertEqual(pending[0]["image_sha256"], pending[1]["image_sha256"])
        self.assertNotEqual(pending[0]["evidence_id"], pending[1]["evidence_id"])
        self.assertNotEqual(pending[0]["location"], pending[1]["location"])

    def test_plain_text_is_identical_and_no_visual_pending_is_not_partial(self) -> None:
        path = self.root / "notes.txt"
        path.write_text("対象と条件\n火曜日のみ15個\n", encoding="utf-8")
        original = path.read_bytes()
        full = records.Probe(self.root, RUN_AT, None, diagnostic=False)
        full.extract(path)
        candidate = self.reader()
        candidate.extract(path)
        self.assertEqual(candidate.evidence, full.evidence)
        self.assertEqual(candidate.relations, full.relations)
        self.assertEqual(candidate.documents[0]["extraction"]["status"], "success")
        self.assertEqual(candidate.documents[0]["extraction"]["visual_coverage"], {"status": "none_pending", "pending": []})
        self.assertEqual(path.read_bytes(), original)

    def test_structured_text_sources_keep_native_payload_and_provenance(self) -> None:
        fixtures = {
            ".csv": "曜日,数量\n火曜日,15\n",
            ".tsv": "曜日\t数量\n火曜日\t15\n",
            ".json": '{"曜日":"火曜日","数量":15}',
            ".xml": "<予定><曜日>火曜日</曜日><数量>15</数量></予定>",
        }
        for suffix, text in fixtures.items():
            with self.subTest(suffix=suffix):
                path = self.root / ("structured" + suffix)
                path.write_text(text, encoding="utf-8")
                full = records.Probe(self.root, RUN_AT, None, diagnostic=False)
                full.extract(path)
                candidate = self.reader()
                candidate.extract(path)
                self.assertTrue(candidate.evidence)
                self.assertEqual(candidate.evidence, full.evidence)
                self.assertEqual(candidate.relations, full.relations)
                self.assertEqual(candidate.documents[0]["extraction"]["status"], full.documents[0]["extraction"]["status"])
                self.assertEqual(candidate.documents[0]["extraction"]["visual_coverage"], {"status": "none_pending", "pending": []})
                self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_standalone_image_is_bound_binary_not_made_up_text(self) -> None:
        path = self.root / "figure.png"
        path.write_bytes(PNG_BYTES)
        reader = self.reader()
        reader.extract(path)
        pending = self.assert_pending_bound(reader, "standalone_image", 1)
        self.assertEqual(pending[0]["source_sha256"], PNG_SHA256)
        self.assertEqual(len(reader.evidence), 1)
        self.assertEqual(reader.documents[0]["extraction"]["errors"], [])
        self.assertEqual(path.read_bytes(), PNG_BYTES)

    def extract_pdf(self, pages: list[str | Exception]) -> records.Probe:
        path = self.root / "fictional.pdf"
        path.write_bytes(b"%PDF-1.4\nsynthetic source identity, native reader is mocked\n")
        source_hash = records.digest_file(path)
        snapshot = types.SimpleNamespace(
            path=path, source_sha256=source_hash, source_size_bytes=path.stat().st_size,
        )
        planned = [{"page_number": number, "page_width_pt": 612.0, "page_height_pt": 792.0,
                    "page_rotation": 0, "render_width_px": 1700, "render_height_px": 2200}
                   for number in range(1, len(pages) + 1)]

        def native_page(_snapshot, page_number, *, dpi):
            value = pages[page_number - 1]
            if isinstance(value, Exception):
                raise value
            return {"source_sha256": source_hash, "page_number": page_number,
                    "page_count": len(pages), "dpi": dpi, "page_width_pt": 612.0,
                    "page_height_pt": 792.0, "page_rotation": 0, "native_text": value}

        reader = self.reader()
        with (
            mock.patch.object(pdf_reader, "snapshot_pdf", return_value=contextlib.nullcontext(snapshot)),
            mock.patch.object(pdf_reader, "inspect_pdf_snapshot", return_value={"page_count": len(pages), "pages": planned}),
            mock.patch.object(pdf_reader, "read_pdf_snapshot_page", side_effect=native_page) as native,
        ):
            reader.extract(path)
        self.assertEqual(native.call_count, len(pages))
        self.assertEqual(records.digest_file(path), source_hash)
        return reader

    def test_pdf_keeps_every_native_page_and_marks_even_text_pages_visual_pending(self) -> None:
        reader = self.extract_pdf(["見出し\n火曜日のみ", "", "数量15個"])
        pages = [item for item in reader.evidence if item["evidence_type"] == "page"]
        self.assertEqual([item["location"]["page_number"] for item in pages], [1, 2, 3])
        self.assertEqual(pages[0]["content"]["raw_text"], "見出し\n火曜日のみ")
        self.assertNotIn("raw_text", pages[1]["content"])
        self.assertIn("content_ref", pages[1]["content"])
        self.assertEqual(pages[2]["content"]["raw_text"], "数量15個")
        self.assertFalse(any(item["evidence_type"] in {"ocr_line", "image"} for item in reader.evidence))
        pending = self.assert_pending_bound(reader, "pdf_page", 3)
        self.assertEqual([item["location"]["page_number"] for item in pending], [1, 2, 3])
        self.assertEqual(reader.documents[0]["extraction"]["errors"], [])

    def test_pdf_native_page_failure_stays_explicit_and_other_pages_survive(self) -> None:
        reader = self.extract_pdf(["前のページ", RuntimeError("synthetic native failure"), "後のページ"])
        pages = [item for item in reader.evidence if item["evidence_type"] == "page"]
        self.assertEqual([item["location"]["page_number"] for item in pages], [1, 2, 3])
        self.assertEqual(pages[0]["content"]["raw_text"], "前のページ")
        self.assertEqual(pages[2]["content"]["raw_text"], "後のページ")
        self.assertNotIn("raw_text", pages[1]["content"])
        extraction = reader.documents[0]["extraction"]
        self.assertEqual(extraction["status"], "partial")
        self.assertTrue(any("synthetic native failure" in str(item) for item in extraction["warnings"] + extraction["errors"]))
        self.assert_pending_bound(reader, "pdf_page", 3)

    def test_streaming_record_sink_also_receives_coverage_with_bound_evidence(self) -> None:
        path = self.root / "streamed.png"
        path.write_bytes(PNG_BYTES)
        emitted: dict[str, list[dict]] = {"documents": [], "evidence": [], "relations": []}
        reader = records.Probe(
            self.root, RUN_AT, None, diagnostic=False, reading_policy="text_first_v1",
            retain_records=False, record_sink=lambda kind, value: emitted[kind].append(value),
        )
        reader.extract(path)
        self.assertEqual(reader.documents, [])
        self.assertEqual(reader.evidence, [])
        self.assertEqual(len(emitted["documents"]), 1)
        self.assertEqual(len(emitted["evidence"]), 1)
        pending = emitted["documents"][0]["extraction"]["visual_coverage"]["pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["evidence_id"], emitted["evidence"][0]["evidence_id"])
        self.assertEqual(pending[0]["image_sha256"], PNG_SHA256)

    def test_unsupported_policy_is_rejected_before_documents_exist(self) -> None:
        for policy in ("", "text_first", "FULL", None):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                self.reader(policy)

    def test_unsupported_format_is_not_silently_declared_text_first(self) -> None:
        path = self.root / "legacy.doc"
        path.write_bytes(b"not a supported text-first container")
        reader = self.reader()
        with self.assertRaises(ValueError):
            reader.extract(path)
        self.assertEqual(reader.documents, [])
        self.assertEqual(reader.evidence, [])

    def test_damaged_office_is_not_reclassified_as_deferred_image(self) -> None:
        path = self.root / "damaged.xlsx"
        path.write_bytes(b"invalid Office container")
        reader = self.reader()
        with self.assertRaisesRegex(ValueError, "office_source_requires_human_review"):
            with mock.patch.dict(sys.modules, {"openpyxl": None}):
                reader.extract(path)
        self.assertEqual(reader.evidence, [])


if __name__ == "__main__":
    unittest.main()
