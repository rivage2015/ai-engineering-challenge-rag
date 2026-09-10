from __future__ import annotations

import builtins
import contextlib
import http.client
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import quoteattr


REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "scripts"))

import adapt_layer1_to_local_memory
import build_intermediate_records as builder
import build_search_units
import probe_intermediate_records as probe
import validate_intermediate_records
import validate_intermediate_records_streaming
import validate_search_units
import validate_search_units_streaming


RUN_AT = "2026-09-09T00:00:00+00:00"
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOCUMENT_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
STRICT_MAIN = "http://purl.oclc.org/ooxml/spreadsheetml/main"
STRICT_DOCUMENT_REL = "http://purl.oclc.org/ooxml/officeDocument/relationships"
STRICT_PACKAGE_REL = "http://purl.oclc.org/ooxml/package/relationships"


def relationship(identifier="r1", target="worksheets/sheet1.xml", kind="worksheet", *,
                 namespace=DOCUMENT_REL, mode=None) -> str:
    attributes = {"Id": identifier, "Target": target, "Type": namespace + "/" + kind}
    if mode is not None:
        attributes["TargetMode"] = mode
    return "<Relationship " + " ".join(key + "=" + quoteattr(value) for key, value in attributes.items()) + "/>"


def relationships(*rows, namespace=PACKAGE_REL) -> str:
    return '<Relationships xmlns=' + quoteattr(namespace) + '>' + "".join(rows) + '</Relationships>'


def sheet(name="業務", identifier="1", relation_id="r1", *, extra="") -> str:
    return f'<sheet name={quoteattr(name)} sheetId={quoteattr(identifier)} r:id={quoteattr(relation_id)} {extra}/>'


def workbook(*sheets, namespace=MAIN, relationship_namespace=DOCUMENT_REL, body=None) -> str:
    payload = '<sheets>' + "".join(sheets or [sheet()]) + '</sheets>' if body is None else body
    return f'<workbook xmlns={quoteattr(namespace)} xmlns:r={quoteattr(relationship_namespace)}>{payload}</workbook>'


def worksheet(text="bound value", *, namespace=MAIN, body=None) -> str:
    payload = '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>' + text + '</t></is></c></row></sheetData>' if body is None else body
    return f'<worksheet xmlns={quoteattr(namespace)}>{payload}</worksheet>'


class XlsxFallbackBindingHardeningTests(unittest.TestCase):
    """F10: synthetic <=1 MiB ZIPs, no runtime/model/credential interaction."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="lms-f10-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "source"
        self.root.mkdir()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(builder, "_ollama_json", side_effect=AssertionError("network forbidden")))
        stack.enter_context(mock.patch.object(http.client.HTTPConnection, "connect", side_effect=AssertionError("network forbidden")))

    def source(self, name="fixture.xlsx", *, workbook_xml=None, workbook_rels=None,
               root_rels=None, parts=None, omit=()) -> Path:
        entries = {
            "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/></Types>',
            "_rels/.rels": root_rels if root_rels is not None else relationships(relationship("root", "xl/workbook.xml", "officeDocument")),
            "xl/workbook.xml": workbook_xml if workbook_xml is not None else workbook(),
            "xl/_rels/workbook.xml.rels": workbook_rels if workbook_rels is not None else relationships(relationship()),
            "xl/worksheets/sheet1.xml": worksheet(),
        }
        entries.update(parts or {})
        overrides = '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        overrides += "".join(
            '<Override PartName=' + quoteattr('/' + member) + ' ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for member in entries if member.startswith("xl/worksheets/")
        )
        entries["[Content_Types].xml"] = entries["[Content_Types].xml"].replace('</Types>', overrides + '</Types>')
        path = self.root / name
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            for member, value in entries.items():
                if member not in omit:
                    archive.writestr(member, value.encode("utf-8"))
        self.assertLessEqual(path.stat().st_size, 1024 * 1024)
        return path

    def reader(self) -> probe.Probe:
        return probe.Probe(self.root, RUN_AT, None, diagnostic=False, visual_observation_mode="suppressed")

    def extract(self, path) -> probe.Probe:
        reader = self.reader()
        # Select only the standard-library path, irrespective of installed extras.
        with mock.patch.object(probe.Probe, "extract_xlsx", probe.Probe.extract_xlsx_ooxml):
            reader.extract(path)
        return reader

    def assert_binding_rejected(self, path) -> None:
        reader = self.reader()
        with self.assertRaises(ValueError):
            reader.extract_xlsx_ooxml(path)
        # A malformed binding must be discovered before any cell, parent or
        # document-containment Evidence/Relation enters even the in-memory sink.
        self.assertEqual(reader.evidence, [])
        self.assertEqual(reader.relations, [])

    def test_valid_direct_binding_preserves_order_names_members_and_state(self) -> None:
        source = self.source(
            workbook_xml=workbook(sheet("後の部品が先", "8", "last", extra='state="hidden"'), sheet("次", "2", "first")),
            workbook_rels=relationships(relationship("first"), relationship("last", "/xl/worksheets/custom.xml")),
            parts={"xl/worksheets/custom.xml": worksheet("first in workbook"), "xl/worksheets/orphan.xml": worksheet("unreachable canary")},
        )
        before = probe.digest_file(source)
        reader = self.extract(source)
        sheets = [item for item in reader.evidence if item["evidence_type"] == "worksheet"]
        self.assertEqual([item["location"]["sheet_name"] for item in sheets], ["後の部品が先", "次"])
        self.assertEqual([item["ordinal"] for item in sheets], [1, 2])
        self.assertEqual([item["native_properties"]["source_member"] for item in sheets], ["xl/worksheets/custom.xml", "xl/worksheets/sheet1.xml"])
        self.assertEqual(sheets[0]["native_properties"]["state"], "hidden")
        cells = [item for item in reader.evidence if item["evidence_type"] == "table_cell"]
        self.assertEqual([item["content"]["raw_value"] for item in cells], ["first in workbook", "bound value"])
        self.assertEqual([item["parent_evidence_id"] for item in cells], [item["evidence_id"] for item in sheets])
        self.assertEqual(reader.documents[0]["extraction"]["status"], "partial")
        self.assertEqual(probe.digest_file(source), before)

    def test_strict_namespace_direct_binding_is_supported(self) -> None:
        source = self.source(
            workbook_xml=workbook(namespace=STRICT_MAIN, relationship_namespace=STRICT_DOCUMENT_REL),
            workbook_rels=relationships(relationship(namespace=STRICT_DOCUMENT_REL), namespace=STRICT_PACKAGE_REL),
            root_rels=relationships(relationship("root", "xl/workbook.xml", "officeDocument", namespace=STRICT_DOCUMENT_REL), namespace=STRICT_PACKAGE_REL),
            parts={"xl/worksheets/sheet1.xml": worksheet("strict value", namespace=STRICT_MAIN)},
        )
        reader = self.extract(source)
        self.assertEqual([item["content"]["raw_value"] for item in reader.evidence if item["evidence_type"] == "table_cell"], ["strict value"])

    def test_normal_dispatch_selects_fallback_when_openpyxl_is_unavailable(self) -> None:
        real_import = builtins.__import__

        def without_openpyxl(name, *args, **kwargs):
            if name == "openpyxl" or name.startswith("openpyxl."):
                raise ImportError("synthetic optional dependency absence")
            return real_import(name, *args, **kwargs)

        reader = self.reader()
        with mock.patch.object(builtins, "__import__", side_effect=without_openpyxl):
            reader.extract(self.source())
        self.assertEqual(reader.documents[0]["extraction"]["parser"], "ooxml-stdlib-xlsx-fallback")
        self.assertEqual(reader.documents[0]["extraction"]["status"], "partial")
        self.assertEqual([item["content"]["raw_value"] for item in reader.evidence if item["evidence_type"] == "table_cell"], ["bound value"])

    def test_valid_binding_retains_exact_numeric_lexemes_and_unrecalculated_formulas(self) -> None:
        exact = "0.123456789012345678901234567890"
        body = '<sheetData><row r="1"><c r="A1"><v>' + exact + '</v></c><c r="B1"><f>SUM(A2:A3)</f><v>0</v></c></row></sheetData>'
        source = self.source(parts={"xl/worksheets/sheet1.xml": worksheet(body=body)})
        reader = self.extract(source)
        cells = {item["location"]["cell"]: item for item in reader.evidence if item["evidence_type"] == "table_cell"}
        self.assertEqual(cells["A1"]["content"]["raw_value"], exact)
        self.assertEqual(cells["A1"]["native_properties"]["raw_lexeme"], exact)
        self.assertEqual(cells["B1"]["content"]["raw_value"], 0)
        formula = next(item for item in reader.evidence if item["evidence_type"] == "formula")
        self.assertEqual(formula["content"]["raw_text"], "=SUM(A2:A3)")
        self.assertEqual(formula["native_properties"]["cached_value_status"], "stored_in_file_not_recalculated")
        self.assertTrue(formula["native_properties"]["cached_value_available"])
        units = []
        deriver = build_search_units.DocumentDeriver(reader.documents[0]["document_id"], RUN_AT, units.append, 1200)
        for item in reader.evidence:
            deriver.consume(item)
        deriver.finish()
        self.assertTrue(any("保存値（ファイル保存時・未再計算）: 0" in item["text"]["search_text"] for item in units))

    def test_worksheet_requires_one_direct_sheet_data_but_allows_empty_data(self) -> None:
        for index, body in enumerate(("", '<extension><sheetData/></extension>', '<sheetData xmlns="urn:fake"/>', '<sheetData/><sheetData/>')):
            with self.subTest(index=index):
                self.assert_binding_rejected(self.source(f"data-{index}.xlsx", parts={"xl/worksheets/sheet1.xml": worksheet(body=body)}))
        reader = self.extract(self.source("empty.xlsx", parts={"xl/worksheets/sheet1.xml": worksheet(body='<sheetData/>')}))
        self.assertEqual([item["evidence_type"] for item in reader.evidence], ["worksheet"])
        self.assertEqual(reader.documents[0]["extraction"]["status"], "partial")

    def test_package_must_explicitly_bind_the_main_workbook(self) -> None:
        cases = [
            {"omit": ("_rels/.rels",)},
            {"root_rels": relationships()},
            {"root_rels": relationships(relationship("root", "xl/workbook.xml", "worksheet"))},
            {"root_rels": relationships(relationship("root", "xl/workbook.xml", "officeDocument", mode="External"))},
            {"root_rels": relationships(relationship("root", "xl/orphan.xml", "officeDocument"))},
            {"root_rels": relationships(relationship("a", "xl/workbook.xml", "officeDocument"), relationship("b", "xl/workbook.xml", "officeDocument"))},
        ]
        for index, kwargs in enumerate(cases):
            with self.subTest(index=index):
                self.assert_binding_rejected(self.source(f"root-{index}.xlsx", **kwargs))

    def test_workbook_root_must_have_its_canonical_namespace_and_name(self) -> None:
        for index, value in enumerate((workbook(namespace="urn:fake"), workbook().replace("workbook", "notWorkbook"), workbook(namespace=""))):
            with self.subTest(index=index):
                self.assert_binding_rejected(self.source(f"workbook-{index}.xlsx", workbook_xml=value))

    def test_sheet_list_is_direct_canonical_and_unambiguous(self) -> None:
        cases = [
            '', '<sheets/>', '<extension><sheets>' + sheet() + '</sheets></extension>',
            '<sheets xmlns="urn:fake">' + sheet() + '</sheets>',
            '<sheets><sheet xmlns="urn:fake" name="fake" sheetId="1" r:id="r1"/></sheets>',
            '<sheets>' + sheet() + '</sheets><sheets>' + sheet("duplicate", "2") + '</sheets>',
            '<sheets><extension>' + sheet() + '</extension></sheets>',
        ]
        for index, body in enumerate(cases):
            with self.subTest(index=index):
                self.assert_binding_rejected(self.source(f"sheets-{index}.xlsx", workbook_xml=workbook(body=body)))

    def test_relationship_id_attribute_must_be_explicit_and_unambiguous(self) -> None:
        cases = [
            '<sheet name="fake" sheetId="1" id="r1"/>',
            '<sheet name="fake" sheetId="1" xmlns:q="urn:fake" q:id="r1"/>',
            f'<sheet name="fake" sheetId="1" r:id="r1" xmlns:q="{STRICT_DOCUMENT_REL}" q:id="r1"/>',
            '<sheet name="fake" sheetId="1"/>',
        ]
        for index, value in enumerate(cases):
            with self.subTest(index=index):
                self.assert_binding_rejected(self.source(f"attr-{index}.xlsx", workbook_xml=workbook(body="<sheets>" + value + "</sheets>")))

    def test_relationship_xml_must_have_direct_canonical_elements(self) -> None:
        cases = [
            relationships(relationship(), namespace="urn:fake"),
            relationships(relationship().replace("<Relationship ", '<Relationship xmlns="urn:fake" ')),
            relationships('<container>' + relationship() + '</container>'),
            relationships(relationship()).replace("Relationships", "FakeRelationships"),
        ]
        for index, value in enumerate(cases):
            with self.subTest(index=index):
                self.assert_binding_rejected(self.source(f"rels-{index}.xlsx", workbook_rels=value))

    def test_wrong_missing_external_and_unsafe_worksheet_relationships_are_rejected(self) -> None:
        rows = [
            relationship(kind="chart"), relationship(namespace="urn:fake"),
            '<Relationship Id="r1" Target="worksheets/sheet1.xml"/>',
            relationship(mode="External"), relationship(mode="external"), relationship(mode="unknown"),
            relationship(target="https://invalid.example/worksheet.xml"),
            relationship(target="../../outside.xml"), relationship(target="worksheets/missing.xml"),
            relationship(target=""), relationship(identifier="other"),
        ]
        for index, row in enumerate(rows):
            with self.subTest(index=index):
                self.assert_binding_rejected(self.source(f"target-{index}.xlsx", workbook_rels=relationships(row)))

    def test_duplicate_relationship_ids_are_never_last_wins(self) -> None:
        for index, last in enumerate((relationship(), relationship(target="worksheets/forged.xml"))):
            with self.subTest(index=index):
                self.assert_binding_rejected(self.source(f"duplicate-rel-{index}.xlsx", workbook_rels=relationships(relationship(), last), parts={"xl/worksheets/forged.xml": worksheet("forged canary")}))

    def test_sheet_identity_and_target_duplicates_are_rejected(self) -> None:
        cases = [
            (sheet("one", "1", "r1"), sheet("two", "1", "r2")),
            (sheet("same", "1", "r1"), sheet("SAME", "2", "r2")),
            (sheet("one", "1", "r1"), sheet("two", "2", "r1")),
            (sheet("one", "1", "r1"), sheet("two", "2", "r2")),
            (sheet("", "1", "r1"),), (sheet("one", "", "r1"),),
        ]
        for index, sheets in enumerate(cases):
            with self.subTest(index=index):
                # r1 and r2 deliberately point to the same existing part.
                self.assert_binding_rejected(self.source(f"duplicate-sheet-{index}.xlsx", workbook_xml=workbook(*sheets), workbook_rels=relationships(relationship("r1"), relationship("r2"))))

    def test_worksheet_part_requires_exact_root_not_merely_cell_like_descendants(self) -> None:
        for index, value in enumerate((worksheet(namespace="urn:fake"), worksheet().replace("worksheet", "chart"), worksheet(namespace=""))):
            with self.subTest(index=index):
                self.assert_binding_rejected(self.source(f"part-{index}.xlsx", parts={"xl/worksheets/sheet1.xml": value}))

    def test_unresolved_binding_does_not_fall_back_to_numbered_orphan_sheet(self) -> None:
        self.assert_binding_rejected(self.source(workbook_rels=relationships(), parts={"xl/worksheets/sheet1.xml": worksheet("orphan canary")}))

    def test_uri_targets_do_not_bind_to_existing_posix_lookalike_members(self) -> None:
        cases = (
            ("https://invalid.example/worksheet.xml", "xl/https:/invalid.example/worksheet.xml"),
            ("custom+scheme:payload.xml", "xl/custom+scheme:payload.xml"),
            ("//xl/worksheets/sheet1.xml", "xl/worksheets/sheet1.xml"),
            ("///xl/worksheets/sheet1.xml", "xl/worksheets/sheet1.xml"),
            ("worksheets/part.xml?query", "xl/worksheets/part.xml?query"),
            ("worksheets/part.xml#fragment", "xl/worksheets/part.xml#fragment"),
            ("worksheets/%2e%2e/part.xml", "xl/worksheets/%2e%2e/part.xml"),
            ("worksheets/part%2Fother.xml", "xl/worksheets/part%2Fother.xml"),
        )
        for index, (target, member) in enumerate(cases):
            with self.subTest(target=target):
                self.assert_binding_rejected(self.source(f"uri-lookalike-{index}.xlsx", workbook_rels=relationships(relationship(target=target)), parts={member: worksheet("forged target canary")}))

    def test_shared_package_target_resolver_preserves_valid_path_forms_without_decoding(self) -> None:
        cases = (
            ("", "xl/workbook.xml", "xl/workbook.xml"),
            ("", "/word/document.xml", "word/document.xml"),
            ("xl/workbook.xml", "worksheets/sheet1.xml", "xl/worksheets/sheet1.xml"),
            ("xl/workbook.xml", "/xl/worksheets/sheet1.xml", "xl/worksheets/sheet1.xml"),
            ("xl/workbook.xml", "./worksheets/sheet1.xml", "xl/worksheets/sheet1.xml"),
            ("xl/workbook.xml", "worksheets/../worksheets/sheet1.xml", "xl/worksheets/sheet1.xml"),
            ("xl/worksheets/sheet1.xml", "../drawings/drawing1.xml", "xl/drawings/drawing1.xml"),
            ("ppt/slides/slide1.xml", "../media/image1.png", "ppt/media/image1.png"),
            ("word/document.xml", "media/image1.png", "word/media/image1.png"),
            ("xl/workbook.xml", "worksheets/sheet%201.xml", "xl/worksheets/sheet%201.xml"),
            ("xl/workbook.xml", "worksheets/literal%23name.xml", "xl/worksheets/literal%23name.xml"),
            ("xl/workbook.xml", "worksheets/literal%3Fname.xml", "xl/worksheets/literal%3Fname.xml"),
            ("xl/workbook.xml", "worksheets/literal%252f.xml", "xl/worksheets/literal%252f.xml"),
            ("xl/workbook.xml", "worksheets/part:name.xml", "xl/worksheets/part:name.xml"),
            ("xl/workbook.xml", "worksheets/日本語.xml", "xl/worksheets/日本語.xml"),
        )
        for source_part, target, expected in cases:
            with self.subTest(source_part=source_part, target=target):
                self.assertEqual(probe._resolve_ooxml_target(source_part, target), expected)
        reader = self.extract(self.source(workbook_rels=relationships(relationship(target="worksheets/sheet%201.xml")), parts={
            "xl/worksheets/sheet%201.xml": worksheet("escaped exact part"),
            "xl/worksheets/sheet 1.xml": worksheet("different unescaped canary"),
        }))
        self.assertEqual([item["content"]["raw_value"] for item in reader.evidence if item["evidence_type"] == "table_cell"], ["escaped exact part"])

    def test_shared_package_target_resolver_rejects_non_part_uri_and_escape_aliases(self) -> None:
        targets = (
            "https://invalid.example/a.xml", "HTTP:a.xml", "file:///a.xml", "mailto:a.xml",
            "urn:part", "C:/a.xml", "1invalid:part.xml", ":part.xml",
            "//authority/a.xml", "///a.xml", "a.xml?", "a.xml?query", "a.xml#", "a.xml#part",
            " a.xml", "a\tb.xml", "a\nb.xml", "a\rb.xml", "a\x00b.xml", "a\x7fb.xml", "a\\b.xml",
            "a%00.xml", "a%09.xml", "a%7f.xml", "a%2Fb.xml", "a%5cb.xml", "%2e%2e/a.xml",
            "%61.xml", "a%2Exml", "a%.xml", "a%2.xml", "a%GG.xml", "../../outside.xml", "",
            "a//b.xml", "worksheets/sheet1.xml/",
        )
        for target in targets:
            with self.subTest(target=target):
                self.assertIsNone(probe._resolve_ooxml_target("xl/workbook.xml", target))

    def test_later_invalid_sheet_data_does_not_leave_first_sheet_evidence(self) -> None:
        for index, body in enumerate(("", '<extension><sheetData/></extension>', '<sheetData xmlns="urn:fake"/>', '<sheetData/><sheetData/>')):
            with self.subTest(index=index):
                self.assert_binding_rejected(self.source(
                    f"later-data-{index}.xlsx",
                    workbook_xml=workbook(sheet(), sheet("bad second", "2", "r2")),
                    workbook_rels=relationships(relationship(), relationship("r2", "worksheets/second.xml")),
                    parts={"xl/worksheets/second.xml": worksheet(body=body)},
                ))

    def test_terminal_dot_directory_references_do_not_bind_existing_file_parts(self) -> None:
        for index, suffix in enumerate(("/.", "/child/..", "/child/../.", "/child/grandchild/../..")):
            for absolute in (False, True):
                with self.subTest(suffix=suffix, absolute=absolute):
                    root_target = ("/" if absolute else "") + "xl/workbook.xml" + suffix
                    sheet_target = ("/xl/" if absolute else "") + "worksheets/sheet1.xml" + suffix
                    self.assertIsNone(probe._resolve_ooxml_target("", root_target))
                    self.assertIsNone(probe._resolve_ooxml_target("xl/workbook.xml", sheet_target))
                    self.assert_binding_rejected(self.source(
                        f"root-dot-{index}-{absolute}.xlsx",
                        root_rels=relationships(relationship("root", root_target, "officeDocument")),
                    ))
                    self.assert_binding_rejected(self.source(
                        f"sheet-dot-{index}-{absolute}.xlsx",
                        workbook_rels=relationships(relationship(target=sheet_target)),
                    ))

    def test_nonterminal_dot_paths_keep_valid_root_and_worksheet_bindings(self) -> None:
        cases = (
            ("./xl/workbook.xml", "./worksheets/sheet1.xml", "xl/worksheets/sheet1.xml"),
            ("xl/../xl/workbook.xml", "worksheets/../worksheets/sheet1.xml", "xl/worksheets/sheet1.xml"),
            ("/xl/./workbook.xml", "/xl/./worksheets/sheet1.xml", "xl/worksheets/sheet1.xml"),
            ("/xl/child/../workbook.xml", "./part:name.xml", "xl/part:name.xml"),
        )
        for index, (root_target, sheet_target, member) in enumerate(cases):
            with self.subTest(index=index):
                path = self.source(
                    f"normal-dot-{index}.xlsx",
                    root_rels=relationships(relationship("root", root_target, "officeDocument")),
                    workbook_rels=relationships(relationship(target=sheet_target)),
                    parts={member: worksheet("normal relative binding")},
                )
                before = probe.digest_file(path)
                reader = self.extract(path)
                self.assertEqual([item["content"]["raw_value"] for item in reader.evidence if item["evidence_type"] == "table_cell"], ["normal relative binding"])
                self.assertEqual(probe.digest_file(path), before)

    def test_only_direct_canonical_cells_are_evidence(self) -> None:
        normal = '<c r="A1" t="inlineStr"><is><t>real cell</t></is></c>'
        fake = '<c r="B1" t="inlineStr"><is><t>forged cell</t></is></c>'
        body = '<sheetData><row r="1">' + normal + fake.replace('<c ', '<c xmlns="urn:fake" ') + '</row><extension><row r="2">' + fake + '</row></extension></sheetData><extension><sheetData><row r="3">' + fake + '</row></sheetData></extension>'
        source = self.source(parts={"xl/worksheets/sheet1.xml": worksheet(body=body)})
        reader = self.extract(source)
        self.assertEqual([item["content"]["raw_value"] for item in reader.evidence if item["evidence_type"] == "table_cell"], ["real cell"])

    def test_valid_neighbor_survives_rejected_bindings_through_real_projection(self) -> None:
        sources = [
            self.source("good.xlsx"),
            self.source("orphan.xlsx", workbook_rels=relationships()),
            self.source("wrong-kind.xlsx", workbook_rels=relationships(relationship(kind="chart"))),
            self.source("wrong-root.xlsx", parts={"xl/worksheets/sheet1.xml": worksheet(namespace="urn:fake")}),
            self.source("duplicate.xlsx", workbook_rels=relationships(relationship(), relationship())),
            self.source("second-bad.xlsx", workbook_xml=workbook(sheet(), sheet("bad second", "2", "r2")), workbook_rels=relationships(relationship(), relationship("r2", "worksheets/second.xml")), parts={"xl/worksheets/second.xml": worksheet(namespace="urn:fake")}),
            self.source("uri-lookalike.xlsx", workbook_rels=relationships(relationship(target="https://invalid.example/worksheet.xml")), parts={"xl/https:/invalid.example/worksheet.xml": worksheet("URI canary")}),
            self.source("later-sheet-data.xlsx", workbook_xml=workbook(sheet(), sheet("bad second", "2", "r2")), workbook_rels=relationships(relationship(), relationship("r2", "worksheets/second.xml")), parts={"xl/worksheets/second.xml": worksheet(body='<extension><sheetData/></extension>')}),
        ]
        neighbor = self.root / "neighbor.txt"
        neighbor.write_text("valid neighbor", encoding="utf-8")
        sources.append(neighbor)
        before = {path: probe.digest_file(path) for path in sources}
        output = self.base / "intermediate"
        actual_probe = probe.Probe

        def suppressed_probe(*args, **kwargs):
            kwargs["visual_observation_mode"] = "suppressed"
            return actual_probe(*args, **kwargs)

        payload = {"test": "f10-synthetic-no-models"}
        fingerprint = {"version": builder.PROCESSING_FINGERPRINT_VERSION, "sha256": probe.digest_value(payload), "payload": payload}
        with (
            mock.patch.object(sys, "argv", ["build_intermediate_records.py", "--root", str(self.root), "--out", str(output), "--run-at", RUN_AT]),
            mock.patch.object(builder, "Probe", side_effect=suppressed_probe),
            mock.patch.object(probe.Probe, "extract_xlsx", probe.Probe.extract_xlsx_ooxml),
            mock.patch.object(builder, "processing_fingerprint", return_value=fingerprint),
            mock.patch.object(builder, "discover_password_candidates", return_value=()),
            mock.patch.object(builder, "paddle_build_session", side_effect=contextlib.nullcontext),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            builder.main()
        state = json.loads((output / "build-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["build_status"], "complete_with_failures")
        failed_ids = set()
        for name in ("orphan.xlsx", "wrong-kind.xlsx", "wrong-root.xlsx", "duplicate.xlsx", "second-bad.xlsx", "uri-lookalike.xlsx", "later-sheet-data.xlsx"):
            entry = state["entries"][name]
            self.assertEqual(entry["status"], "failed", name)
            self.assertEqual(entry["shards"]["evidence"]["record_count"], 0, name)
            self.assertEqual(entry["shards"]["relations"]["record_count"], 0, name)
            failed_ids.add(entry["document_id"])
        self.assertEqual(state["entries"]["good.xlsx"]["status"], "partial")
        self.assertEqual(state["entries"]["neighbor.txt"]["status"], "success")
        self.assertEqual(validate_intermediate_records.validate(output, self.root)["document"], len(sources))
        self.assertEqual(validate_intermediate_records_streaming.validate(output, self.root)["document"], len(sources))
        search = self.base / "search"
        build_search_units.build(output, search, 1200)
        expected = validate_search_units.validate(search, [output])
        self.assertEqual(validate_search_units_streaming.validate(search, [output]), expected)
        units = [json.loads(line) for line in (search / "search_units.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertFalse(failed_ids.intersection(unit["document_id"] for unit in units))
        self.assertTrue(any("bound value" in unit["text"]["search_text"] for unit in units))
        self.assertTrue(any("valid neighbor" in unit["text"]["search_text"] for unit in units))
        adapter = self.base / "adapter"
        result = adapt_layer1_to_local_memory.adapt(output, self.root.resolve(), adapter, search)
        self.assertEqual(result["layer1_status_counts"], {"failed": 7, "partial": 1, "success": 1})
        documents = [json.loads(line) for line in (adapter / "semantic-documents.jsonl").read_text(encoding="utf-8").splitlines()]
        evidence = [json.loads(line) for line in (adapter / "semantic-evidence.jsonl").read_text(encoding="utf-8").splitlines()]
        for document in documents:
            if document["document_id"] in failed_ids:
                self.assertEqual(document["status"], "extraction_failed")
                self.assertEqual(document["evidence_ids"], [])
        self.assertFalse(failed_ids.intersection(item["document_id"] for item in evidence))
        self.assertEqual({path: probe.digest_file(path) for path in sources}, before)


if __name__ == "__main__":
    unittest.main()
