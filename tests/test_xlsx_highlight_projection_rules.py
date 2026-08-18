from __future__ import annotations

import copy
import shutil
import struct
import sys
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[1]
RAG = ROOT / "rag"
if str(RAG) not in sys.path:
    sys.path.insert(0, str(RAG))

from xlsx_highlight_projection_rules import (  # noqa: E402
    decide_question,
    graph_contract_for_question,
    validate_graph_contract,
)
from answer import validate_graph_answer  # noqa: E402
from question_graph_runtime import build_graph_plan  # noqa: E402
from structured_candidate import StructuredCandidateEngine  # noqa: E402


S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
XDR_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"

LOCATION = "架空部門"
CONTAINER = "sample.xlsx"
NATIVE_SHEET = "Summary"
VECTOR_SHEET = "Visual"


def question(sheet: str, *, location: str = LOCATION, container: str = CONTAINER) -> str:
    return (
        f"{location}の{container}において、{sheet}の黄色にハイライトされたセルの"
        "抽出条件と集計内容を答えてください。"
    )


def engine_for(root: Path) -> SimpleNamespace:
    return SimpleNamespace(source_root=root.resolve(), glossary=SimpleNamespace(entries={}))


def inline_cell(reference: str, value: str, *, style: int = 0) -> str:
    style_text = f' s="{style}"' if style else ""
    return (
        f'<c r="{reference}" t="inlineStr"{style_text}>'
        f"<is><t>{escape(value)}</t></is></c>"
    )


def numeric_cell(reference: str, value: int | str, *, style: int = 0) -> str:
    style_text = f' s="{style}"' if style else ""
    return f'<c r="{reference}"{style_text}><v>{escape(str(value))}</v></c>'


def worksheet(rows: dict[int, list[str]], *, drawing: bool = False, conditional: bool = False) -> str:
    rendered_rows = "".join(
        f'<row r="{row}">{"".join(cells)}</row>' for row, cells in sorted(rows.items())
    )
    marker = '<conditionalFormatting sqref="A1"><cfRule type="expression" priority="1"><formula>1</formula></cfRule></conditionalFormatting>' if conditional else ""
    drawing_node = '<drawing r:id="rId1"/>' if drawing else ""
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="{S_NS}" xmlns:r="{R_NS}">
  <sheetData>{rendered_rows}</sheetData>{marker}{drawing_node}
</worksheet>'''


def emf_text_record(x: int, y: int, text: str) -> bytes:
    encoded = text.encode("utf-16le")
    size = (76 + len(encoded) + 3) // 4 * 4
    record = bytearray(size)
    struct.pack_into("<II", record, 0, 84, size)
    struct.pack_into("<iiII", record, 36, x, y, len(text), 76)
    record[76 : 76 + len(encoded)] = encoded
    return bytes(record)


def emf_brush(handle: int, colorref: int) -> bytes:
    return struct.pack("<IIIIII", 39, 24, handle, 0, colorref, 0)


def emf_select(handle: int) -> bytes:
    return struct.pack("<III", 37, 12, handle)


def emf_bitblt(x: int, y: int, cx: int, cy: int) -> bytes:
    record = bytearray(100)
    struct.pack_into("<II", record, 0, 76, 100)
    struct.pack_into("<iiiii", record, 24, x, y, cx, cy, 0x00F00021)
    return bytes(record)


def emf_rectangle(left: int, top: int, right: int, bottom: int) -> bytes:
    return struct.pack("<IIiiii", 43, 24, left, top, right, bottom)


def emf_bytes(
    *,
    category: str = "Alpha",
    phase: str = "2",
    role: str = "Engineer",
    aggregate: str = "2",
    duplicate_marker: bool = False,
    unsupported_transform: bool = False,
    extra_brush_rectangle: bool = False,
    unsupported_record: bool = False,
    mapping_origin: tuple[int, int] | None = None,
    ambiguous_hierarchy: bool = False,
) -> bytes:
    records = []
    if mapping_origin is not None:
        records.append(struct.pack("<IIii", 10, 16, *mapping_origin))
    records.extend([
        emf_brush(1, 0x00FFFFFF),
        emf_select(1),
        emf_brush(2, 0x0000FFFF),
        emf_select(2),
        emf_bitblt(300, 200, 100, 30),
    ])
    if duplicate_marker:
        records.append(emf_bitblt(300, 240, 100, 30))
    if extra_brush_rectangle:
        # EMR_RECTANGLE consumes the currently selected solid yellow brush.
        # It must never be ignored as a second visual yellow marker.
        records.append(emf_rectangle(300, 240, 400, 270))
    if unsupported_transform:
        records.append(struct.pack("<II", 35, 8))
    if unsupported_record:
        records.append(struct.pack("<II", 200, 8))
    records.extend(
        [
            emf_text_record(5, 10, "segment"),
            emf_text_record(105, 10, "phase"),
            emf_text_record(205, 10, "role"),
            emf_text_record(305, 10, "個数 / record_id"),
            emf_text_record(5, 80, category),
            emf_text_record(105, 130, phase),
            emf_text_record(205, 204, role),
            emf_text_record(360, 204, aggregate),
        ]
    )
    if ambiguous_hierarchy:
        records.append(emf_text_record(240, 204, "conflict"))
    eof = struct.pack("<IIIII", 14, 20, 0, 16, 20)
    header = bytearray(88)
    total = len(header) + sum(len(record) for record in records) + len(eof)
    struct.pack_into("<II", header, 0, 1, len(header))
    struct.pack_into("<IIII", header, 40, 0x464D4520, 0x00010000, total, len(records) + 2)
    return b"".join([bytes(header), *records, eof])


def workbook_members(
    *,
    native_category: str = "Alpha",
    native_phase: str = "2",
    native_zone: str = "East",
    native_aggregate: int = 2,
    vector_category: str = "Alpha",
    vector_phase: str = "2",
    vector_role: str = "Engineer",
    vector_aggregate: int = 2,
    duplicate_native_marker: bool = False,
    duplicate_vector_marker: bool = False,
    unsupported_transform: bool = False,
    ambiguous_vector_hierarchy: bool = False,
    raw_mismatch: bool = False,
    duplicate_raw: bool = False,
    conditional_native: bool = False,
    native_apply_fill: str = "1",
    extra_brush_rectangle: bool = False,
    unsupported_emf_record: bool = False,
    emf_mapping_origin: tuple[int, int] | None = None,
) -> dict[str, bytes | str]:
    native_rows = {
        1: [
            inline_cell("A1", "segment"),
            inline_cell("B1", "phase"),
            inline_cell("C1", "zone"),
            inline_cell("D1", "個数"),
        ],
        2: [inline_cell("A2", native_category)],
        3: [numeric_cell("B3", native_phase)],
        4: [
            inline_cell("C4", native_zone),
            numeric_cell("D4", native_aggregate, style=1),
        ],
    }
    if duplicate_native_marker:
        native_rows[5] = [numeric_cell("D5", native_aggregate, style=1)]

    raw_rows = {
        1: [
            inline_cell("A1", "record_id"),
            inline_cell("B1", "segment"),
            inline_cell("C1", "phase"),
            inline_cell("D1", "zone"),
            inline_cell("E1", "role"),
        ],
        2: [
            inline_cell("A2", "r-a"),
            inline_cell("B2", "Alpha"),
            numeric_cell("C2", 2),
            inline_cell("D2", "East"),
            inline_cell("E2", "Engineer"),
        ],
        3: [
            inline_cell("A3", "r-b"),
            inline_cell("B3", "Alpha"),
            numeric_cell("C3", 2),
            inline_cell("D3", "East"),
            inline_cell("E3", "Engineer"),
        ],
        4: [
            inline_cell("A4", "r-c"),
            inline_cell("B4", "Beta"),
            numeric_cell("C4", 7),
            inline_cell("D4", "West"),
            inline_cell("E4", "Analyst"),
        ],
    }
    if raw_mismatch:
        raw_rows.pop(3)

    workbook = f'''<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="{S_NS}" xmlns:r="{R_NS}"><sheets>
  <sheet name="{NATIVE_SHEET}" sheetId="1" r:id="rId1"/>
  <sheet name="{VECTOR_SHEET}" sheetId="2" r:id="rId2"/>
  <sheet name="Raw" sheetId="3" r:id="rId3"/>
  {('<sheet name="RawCopy" sheetId="4" r:id="rId4"/>' if duplicate_raw else '')}
</sheets></workbook>'''
    workbook_rels = f'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{PR_NS}">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet3.xml"/>
  {('<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet4.xml"/>' if duplicate_raw else '')}
</Relationships>'''
    styles = f'''<?xml version="1.0" encoding="UTF-8"?>
<styleSheet xmlns="{S_NS}">
  <fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFFFFF00"/></patternFill></fill></fills>
  <cellXfs count="2"><xf fillId="0"/><xf fillId="1" applyFill="{escape(native_apply_fill)}"/></cellXfs>
</styleSheet>'''
    visual_rels = f'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{PR_NS}"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" Target="../drawings/drawing1.xml"/></Relationships>'''
    drawing = f'''<?xml version="1.0" encoding="UTF-8"?>
<xdr:wsDr xmlns:xdr="{XDR_NS}" xmlns:a="{A_NS}" xmlns:r="{R_NS}">
  <xdr:twoCellAnchor><xdr:from><xdr:col>0</xdr:col><xdr:row>0</xdr:row></xdr:from><xdr:to><xdr:col>5</xdr:col><xdr:row>20</xdr:row></xdr:to>
    <xdr:pic><xdr:blipFill><a:blip r:embed="rId1"/></xdr:blipFill></xdr:pic><xdr:clientData/>
  </xdr:twoCellAnchor>
</xdr:wsDr>'''
    drawing_rels = f'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{PR_NS}"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/table.emf"/></Relationships>'''
    content_types = '''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>'''
    members: dict[str, bytes | str] = {
        "[Content_Types].xml": content_types,
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels": workbook_rels,
        "xl/styles.xml": styles,
        "xl/worksheets/sheet1.xml": worksheet(native_rows, conditional=conditional_native),
        "xl/worksheets/sheet2.xml": worksheet({}, drawing=True),
        "xl/worksheets/_rels/sheet2.xml.rels": visual_rels,
        "xl/worksheets/sheet3.xml": worksheet(raw_rows),
        "xl/drawings/drawing1.xml": drawing,
        "xl/drawings/_rels/drawing1.xml.rels": drawing_rels,
        "xl/media/table.emf": emf_bytes(
            category=vector_category,
            phase=vector_phase,
            role=vector_role,
            aggregate=str(vector_aggregate),
            duplicate_marker=duplicate_vector_marker,
            unsupported_transform=unsupported_transform,
            extra_brush_rectangle=extra_brush_rectangle,
            unsupported_record=unsupported_emf_record,
            mapping_origin=emf_mapping_origin,
            ambiguous_hierarchy=ambiguous_vector_hierarchy,
        ),
    }
    if duplicate_raw:
        members["xl/worksheets/sheet4.xml"] = worksheet(raw_rows)
    return members


def write_workbook(
    root: Path,
    *,
    members: dict[str, bytes | str] | None = None,
    location: str = LOCATION,
    container: str = CONTAINER,
) -> Path:
    path = root / "projects" / location / container
    path.parent.mkdir(parents=True, exist_ok=True)
    payloads = members if members is not None else workbook_members()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in sorted(payloads):
            value = payloads[name]
            archive.writestr(name, value.encode("utf-8") if isinstance(value, str) else value)
    return path


class XlsxHighlightProjectionRulesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = Path(tempfile.mkdtemp(prefix="xlsx-highlight-rules-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tempdir)

    def decision(self, sheet: str, *, members: dict[str, bytes | str] | None = None):
        write_workbook(self.tempdir, members=members)
        return decide_question(engine_for(self.tempdir), question(sheet))

    def test_graph_contract_is_typed_exact_and_question_complete(self) -> None:
        prompt = question(NATIVE_SHEET)
        contract = graph_contract_for_question(prompt)
        self.assertIsNotNone(contract)
        assert contract is not None
        self.assertTrue(contract["graph_contract_id"].startswith("xlsx_highlight_"))
        self.assertEqual(contract["requested_output"]["answer_shape"]["value_type"], "string")
        self.assertEqual(contract["requested_output"]["answer_shape"]["container"], "key_value")
        self.assertEqual(contract["requested_output"]["cardinality"], "multiple")
        self.assertTrue(validate_graph_contract(prompt, contract))
        mutated = copy.deepcopy(contract)
        mutated["scope"]["sheet"] = "different"
        self.assertFalse(validate_graph_contract(prompt, mutated))
        self.assertIsNone(graph_contract_for_question(prompt + "追記"))
        self.assertIsNone(
            graph_contract_for_question(prompt.replace("黄色", "青色"))
        )

    def test_native_unique_yellow_sparse_hierarchy_and_raw_recompute(self) -> None:
        decision = self.decision(NATIVE_SHEET)
        self.assertEqual(decision.status, "resolved")
        assert decision.result is not None
        self.assertEqual(
            decision.result.answer,
            "segment=Alpha、phase=2、zone=Eastで抽出されたデータに対する個数",
        )
        self.assertEqual(decision.result.operation_count, 9)
        self.assertEqual(decision.result.output_count, 1)

    def test_vector_unique_yellow_patcopy_utf16_and_raw_recompute(self) -> None:
        decision = self.decision(VECTOR_SHEET)
        self.assertEqual(decision.status, "resolved")
        assert decision.result is not None
        self.assertEqual(
            decision.result.answer,
            "segment=Alpha、phase=2、role=Engineerで抽出されたデータに対する個数 / record_id",
        )

    def test_live_graphplan_and_output_validator(self) -> None:
        write_workbook(self.tempdir)
        prompt = question(NATIVE_SHEET)
        plan = build_graph_plan("opaque-xlsx-highlight", prompt, fast_advisory=True)
        engine = StructuredCandidateEngine(
            self.tempdir.resolve(), SimpleNamespace(entries={})
        )
        decision = engine.decide_from_graph("opaque-xlsx-highlight", prompt, plan)
        self.assertEqual(decision.status, "resolved")
        assert decision.result is not None
        self.assertEqual(validate_graph_answer(decision.result.answer, plan), ())

        missing = engine.decide_from_graph("opaque-xlsx-highlight", prompt, None)
        self.assertEqual(
            (missing.status, missing.reason),
            ("hold", "extended_graph_plan_not_certified"),
        )

        branches = copy.deepcopy(plan.branch_intents)
        branches[0]["intent"]["extended_graph_contract"]["scope"]["sheet"] = "wrong"
        mismatched = replace(plan, branch_intents=branches)
        held = engine.decide_from_graph("opaque-xlsx-highlight", prompt, mismatched)
        self.assertEqual(
            (held.status, held.reason),
            ("hold", "extended_graph_plan_contract_mismatch"),
        )

    def test_native_mutation_follows_authored_marker_and_raw_rows(self) -> None:
        members = workbook_members(
            native_category="Beta",
            native_phase="7",
            native_zone="West",
            native_aggregate=1,
        )
        decision = self.decision(NATIVE_SHEET, members=members)
        self.assertEqual(decision.status, "resolved")
        assert decision.result is not None
        self.assertEqual(
            decision.result.answer,
            "segment=Beta、phase=7、zone=Westで抽出されたデータに対する個数",
        )

    def test_vector_mutation_follows_authored_marker_and_raw_rows(self) -> None:
        members = workbook_members(
            vector_category="Beta",
            vector_phase="7",
            vector_role="Analyst",
            vector_aggregate=1,
        )
        decision = self.decision(VECTOR_SHEET, members=members)
        self.assertEqual(decision.status, "resolved")
        assert decision.result is not None
        self.assertEqual(
            decision.result.answer,
            "segment=Beta、phase=7、role=Analystで抽出されたデータに対する個数 / record_id",
        )

    def test_duplicate_source_holds(self) -> None:
        write_workbook(self.tempdir)
        write_workbook(self.tempdir, location="mirror/" + LOCATION)
        decision = decide_question(engine_for(self.tempdir), question(NATIVE_SHEET))
        self.assertEqual(decision.status, "hold")
        self.assertEqual(decision.reason, "xlsx_highlight_workbook_not_unique")

    def test_duplicate_native_marker_holds(self) -> None:
        decision = self.decision(
            NATIVE_SHEET, members=workbook_members(duplicate_native_marker=True)
        )
        self.assertEqual(decision.status, "hold")

    def test_conditional_formatting_is_not_silently_treated_as_direct_fill(self) -> None:
        decision = self.decision(
            NATIVE_SHEET, members=workbook_members(conditional_native=True)
        )
        self.assertEqual(decision.status, "hold")

    def test_native_apply_fill_false_is_not_a_visible_yellow_marker(self) -> None:
        decision = self.decision(
            NATIVE_SHEET, members=workbook_members(native_apply_fill="0")
        )
        self.assertEqual(decision.status, "hold")
        self.assertEqual(decision.reason, "xlsx_highlight_marker_not_unique")

    def test_duplicate_vector_marker_holds(self) -> None:
        decision = self.decision(
            VECTOR_SHEET, members=workbook_members(duplicate_vector_marker=True)
        )
        self.assertEqual(decision.status, "hold")

    def test_vector_hierarchy_ambiguity_holds(self) -> None:
        decision = self.decision(
            VECTOR_SHEET, members=workbook_members(ambiguous_vector_hierarchy=True)
        )
        self.assertEqual(decision.status, "hold")

    def test_raw_mismatch_holds(self) -> None:
        decision = self.decision(
            NATIVE_SHEET, members=workbook_members(raw_mismatch=True)
        )
        self.assertEqual(decision.status, "hold")
        self.assertEqual(decision.reason, "xlsx_highlight_raw_mismatch")

    def test_duplicate_raw_candidate_holds(self) -> None:
        decision = self.decision(
            VECTOR_SHEET, members=workbook_members(duplicate_raw=True)
        )
        self.assertEqual(decision.status, "hold")
        self.assertEqual(decision.reason, "xlsx_highlight_raw_mismatch")

    def test_unsupported_emf_transform_holds(self) -> None:
        decision = self.decision(
            VECTOR_SHEET, members=workbook_members(unsupported_transform=True)
        )
        self.assertEqual(decision.status, "hold")
        self.assertEqual(decision.reason, "xlsx_highlight_source_invalid")

    def test_yellow_brush_rectangle_primitive_holds(self) -> None:
        decision = self.decision(
            VECTOR_SHEET, members=workbook_members(extra_brush_rectangle=True)
        )
        self.assertEqual(decision.status, "hold")
        self.assertEqual(decision.reason, "xlsx_highlight_source_invalid")

    def test_unrecognized_emf_record_type_holds(self) -> None:
        decision = self.decision(
            VECTOR_SHEET, members=workbook_members(unsupported_emf_record=True)
        )
        self.assertEqual(decision.status, "hold")
        self.assertEqual(decision.reason, "xlsx_highlight_source_invalid")

    def test_window_origin_is_validated_before_geometry(self) -> None:
        valid = self.decision(
            VECTOR_SHEET, members=workbook_members(emf_mapping_origin=(0, 0))
        )
        self.assertEqual(valid.status, "resolved")

        for origin in ((-1, 0), (0, -1), (1, 0), (0, 1)):
            with self.subTest(origin=origin):
                root = self.tempdir / f"case-{origin[0]}-{origin[1]}"
                write_workbook(
                    root,
                    members=workbook_members(emf_mapping_origin=origin),
                )
                decision = decide_question(engine_for(root), question(VECTOR_SHEET))
                self.assertEqual(decision.status, "hold")
                self.assertEqual(decision.reason, "xlsx_highlight_source_invalid")

    def test_malformed_and_unsafe_archives_hold(self) -> None:
        path = self.tempdir / "projects" / LOCATION / CONTAINER
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a zip")
        decision = decide_question(engine_for(self.tempdir), question(NATIVE_SHEET))
        self.assertEqual(decision.status, "hold")
        self.assertEqual(decision.reason, "xlsx_highlight_source_invalid")

        path.unlink()
        members = workbook_members()
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, value in members.items():
                archive.writestr(name, value.encode("utf-8") if isinstance(value, str) else value)
            archive.writestr("../escape.xml", "<x/>")
        decision = decide_question(engine_for(self.tempdir), question(NATIVE_SHEET))
        self.assertEqual(decision.status, "hold")
        self.assertEqual(decision.reason, "xlsx_highlight_source_invalid")


if __name__ == "__main__":
    unittest.main()
