from __future__ import annotations

import copy
import shutil
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[1]
RAG = ROOT / "rag"
if str(RAG) not in sys.path:
    sys.path.insert(0, str(RAG))

from pptx_mixed_content_rules import (  # noqa: E402
    _safe_xml,
    decide_from_graph,
    decide_question,
    graph_contract_for_question,
    validate_graph_contract,
)


P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

CONTENT_TYPES = b"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="emf" ContentType="image/x-emf"/>
</Types>
"""

AMOUNT_LOCATION_ALIAS = "北辰"
AMOUNT_LOCATION = "株式会社北辰分析"
AMOUNT_CONTAINER_ALIAS = "PP_final.pptx"
AMOUNT_CONTAINER = "提案書_final.pptx"
AMOUNT_QUESTION = (
    f"{AMOUNT_LOCATION_ALIAS}の{AMOUNT_CONTAINER_ALIAS}において、"
    "この案件にかかる金額の提示がまとまっているのは何ページですか。"
)

HIGHLIGHT_LOCATION_ALIAS = "南浜"
HIGHLIGHT_LOCATION = "株式会社南浜交通"
HIGHLIGHT_CONTAINER = "基礎分析.pptx"
HIGHLIGHT_QUESTION = (
    f"{HIGHLIGHT_LOCATION_ALIAS}の{HIGHLIGHT_CONTAINER}において、"
    "黄色ハイライトされている数値に対応するデータの"
    "抽出条件と集計内容を答えてください。"
)


def _engine(
    root: Path,
    *,
    amount_aliases: list[str] | None = None,
    filename_aliases: list[str] | None = None,
    highlight_aliases: list[str] | None = None,
) -> SimpleNamespace:
    entries: dict[str, list[str]] = {}
    if amount_aliases is not None:
        entries[AMOUNT_LOCATION_ALIAS] = amount_aliases
    if filename_aliases is not None:
        entries["PP"] = filename_aliases
    if highlight_aliases is not None:
        entries[HIGHLIGHT_LOCATION_ALIAS] = highlight_aliases
    return SimpleNamespace(
        source_root=root.resolve(),
        glossary=SimpleNamespace(entries=entries),
    )


def _graph_plan(question: str) -> SimpleNamespace:
    contract = graph_contract_for_question(question)
    assert contract is not None
    return SimpleNamespace(
        original_question=question,
        strict_status="pass",
        branch_intents=(
            {
                "status": "resolved",
                "intent": {"extended_graph_contract": contract},
            },
        ),
    )


def _shape(shape_id: int, text: str, *, hidden: bool = False) -> str:
    hidden_attribute = ' hidden="1"' if hidden else ""
    return f"""
      <p:sp>
        <p:nvSpPr>
          <p:cNvPr id="{shape_id}" name="Shape {shape_id}"{hidden_attribute}/>
          <p:cNvSpPr/><p:nvPr/>
        </p:nvSpPr>
        <p:spPr/>
        <p:txBody><a:bodyPr/><a:lstStyle/>
          <a:p><a:r><a:t>{escape(text)}</a:t></a:r></a:p>
        </p:txBody>
      </p:sp>
    """


def _picture(shape_id: int, relationship_id: str = "rIdImage") -> str:
    return f"""
      <p:pic>
        <p:nvPicPr>
          <p:cNvPr id="{shape_id}" name="Picture {shape_id}"/>
          <p:cNvPicPr/><p:nvPr/>
        </p:nvPicPr>
        <p:blipFill><a:blip r:embed="{relationship_id}"/></p:blipFill>
        <p:spPr/>
      </p:pic>
    """


def _presentation_xml(slide_count: int) -> str:
    slide_ids = "".join(
        f'<p:sldId id="{255 + index}" r:id="rId{index}"/>'
        for index in range(1, slide_count + 1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <p:presentation xmlns:p="{P_NS}" xmlns:r="{R_NS}">
      <p:sldIdLst>{slide_ids}</p:sldIdLst>
    </p:presentation>"""


def _presentation_relationships(slide_count: int, *, malformed: bool = False) -> str:
    relationships = []
    for index in range(1, slide_count + 1):
        target = f"slides/slide{index}.xml"
        target_mode = ""
        if malformed and index == 1:
            target = "https://invalid.example/slide.xml"
            target_mode = ' TargetMode="External"'
        relationships.append(
            f'<Relationship Id="rId{index}" Type="{R_NS}/slide" '
            f'Target="{target}"{target_mode}/>'
        )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="{PR_NS}">{''.join(relationships)}</Relationships>"""


def _slide_xml(shapes: list[str], *, hidden: bool = False) -> str:
    visibility = ' show="0"' if hidden else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <p:sld xmlns:p="{P_NS}" xmlns:a="{A_NS}" xmlns:r="{R_NS}"{visibility}>
      <p:cSld><p:spTree>{''.join(shapes)}</p:spTree></p:cSld>
    </p:sld>"""


def _write_package(path: Path, members: dict[str, bytes | str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payloads: dict[str, bytes] = {"[Content_Types].xml": CONTENT_TYPES}
    payloads.update(
        {
            name: value.encode("utf-8") if isinstance(value, str) else value
            for name, value in members.items()
        }
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in sorted(payloads):
            archive.writestr(name, payloads[name])
    return path


def _amount_summary_shapes(
    *,
    net: str = "4,200,000",
    tax: str = "420,000",
    gross: str = "4,620,000",
) -> list[str]:
    return [
        _shape(1, "8. 費用見積"),
        _shape(2, f"契約金額（税抜） ¥{net}"),
        _shape(3, f"消費税額 ¥{tax}"),
        _shape(4, f"契約金額（税込） ¥{gross}"),
        _shape(5, "契約形態: 固定価格契約"),
        _shape(6, "支払条件 着手金 50% 検収金 50%"),
    ]


def write_amount_pptx(
    root: Path,
    *,
    summary_slides: set[int] | None = None,
    slide_count: int = 3,
    hidden_slides: set[int] | None = None,
    amounts: tuple[str, str, str] = ("4,200,000", "420,000", "4,620,000"),
    malformed_relationship: bool = False,
) -> Path:
    summary_slides = summary_slides or {2}
    hidden_slides = hidden_slides or set()
    members: dict[str, bytes | str] = {
        "ppt/presentation.xml": _presentation_xml(slide_count),
        "ppt/_rels/presentation.xml.rels": _presentation_relationships(
            slide_count, malformed=malformed_relationship
        ),
    }
    for index in range(1, slide_count + 1):
        if index in summary_slides:
            shapes = _amount_summary_shapes(
                net=amounts[0], tax=amounts[1], gross=amounts[2]
            )
        elif index == slide_count:
            shapes = [
                _shape(1, "ご検討のほどよろしくお願いします"),
                _shape(2, f"費用: ¥{amounts[2]}（税込・固定価格）"),
            ]
        else:
            shapes = [_shape(1, f"背景と分析方針 {index}")]
        members[f"ppt/slides/slide{index}.xml"] = _slide_xml(
            shapes, hidden=index in hidden_slides
        )
    return _write_package(
        root / "プロジェクト" / AMOUNT_LOCATION / "00.提案" / AMOUNT_CONTAINER,
        members,
    )


def _emf_text(x: int, y: int, value: str) -> bytes:
    encoded = value.encode("utf-16le")
    size = (76 + len(encoded) + 3) // 4 * 4
    record = bytearray(size)
    struct.pack_into("<II", record, 0, 84, size)
    struct.pack_into("<iiII", record, 36, x, y, len(value), 76)
    record[76 : 76 + len(encoded)] = encoded
    return bytes(record)


def _emf_brush(handle: int, rgb: tuple[int, int, int]) -> bytes:
    red, green, blue = rgb
    color_ref = red | (green << 8) | (blue << 16)
    return struct.pack("<IIIIII", 39, 24, handle, 0, color_ref, 0)


def _emf_select(handle: int) -> bytes:
    return struct.pack("<III", 37, 12, handle)


def _emf_fill(x: int, y: int, width: int, height: int) -> bytes:
    record = bytearray(100)
    struct.pack_into("<II", record, 0, 76, len(record))
    struct.pack_into("<iiiiI", record, 24, x, y, width, height, 0x00F00021)
    return bytes(record)


def emf_table(
    *,
    selected_row: int = 2,
    selected_column: int = 2,
    duplicate_marker: bool = False,
    mapping_change: bool = False,
    bad_restore: bool = False,
    extra_paint_primitive: bool = False,
    unknown_record: bool = False,
    duplicate_header: bool = False,
    initial_origin: tuple[int, int] = (0, 0),
) -> bytes:
    row_values = ("R0", "R1", "R2")
    column_values = ("C0", "C1", "C2")
    matrix = (
        ("0.60", "0.61", "0.62"),
        ("0.70", "0.71", "0.72"),
        ("0.80", "0.81", "0.82"),
    )
    xs = (160, 270, 380)
    ys = (120, 220, 320)
    marker_x = xs[selected_column] - 20
    marker_y = ys[selected_row] - 5
    records = [
        struct.pack("<IIii", 10, 16, *initial_origin),
        _emf_brush(1, (255, 255, 0)),
        _emf_select(1),
        _emf_fill(0, 70, 1, 300),
        _emf_fill(0, 70, 600, 1),
        _emf_fill(marker_x, marker_y, 80, 30),
    ]
    if duplicate_marker:
        records.append(_emf_fill(130, 115, 80, 30))
    if extra_paint_primitive:
        records.append(struct.pack("<IIiiii", 43, 24, 130, 115, 210, 145))
    if unknown_record:
        records.append(struct.pack("<II", 0x7FFFFFFE, 8))
    if duplicate_header:
        duplicate = bytearray(88)
        struct.pack_into("<II", duplicate, 0, 1, 88)
        records.append(bytes(duplicate))
    records.extend(
        [
            _emf_text(100, 5, "列項目"),
            _emf_text(5, 50, "行項目"),
            *(
                _emf_text(x, 50, value)
                for x, value in zip(xs, column_values)
            ),
        ]
    )
    for y, row_value, row in zip(ys, row_values, matrix):
        records.append(_emf_text(5, y, row_value))
        records.extend(_emf_text(x, y, value) for x, value in zip(xs, row))
    if mapping_change:
        records.insert(-1, struct.pack("<IIii", 10, 16, 40, 20))
    if bad_restore:
        records.insert(-1, struct.pack("<IIi", 34, 12, -1))
    eof = struct.pack("<IIIII", 14, 20, 0, 16, 20)
    header = bytearray(88)
    record_count = 1 + len(records) + 1
    total_size = len(header) + sum(len(record) for record in records) + len(eof)
    struct.pack_into("<II", header, 0, 1, len(header))
    struct.pack_into(
        "<IIII", header, 40, 0x464D4520, 0x00010000, total_size, record_count
    )
    return bytes(header) + b"".join(records) + eof


def write_highlight_pptx(
    root: Path,
    *,
    selected_row: int = 2,
    selected_column: int = 2,
    duplicate_marker: bool = False,
    mapping_change: bool = False,
    bad_restore: bool = False,
    extra_paint_primitive: bool = False,
    unknown_record: bool = False,
    duplicate_header: bool = False,
    initial_origin: tuple[int, int] = (0, 0),
    hidden: bool = False,
    truncate_emf: bool = False,
    external_image: bool = False,
) -> Path:
    image = emf_table(
        selected_row=selected_row,
        selected_column=selected_column,
        duplicate_marker=duplicate_marker,
        mapping_change=mapping_change,
        bad_restore=bad_restore,
        extra_paint_primitive=extra_paint_primitive,
        unknown_record=unknown_record,
        duplicate_header=duplicate_header,
        initial_origin=initial_origin,
    )
    if truncate_emf:
        image = image[:-1]
    image_target = "https://invalid.example/table.emf" if external_image else "../media/table.emf"
    target_mode = ' TargetMode="External"' if external_image else ""
    slide_relationships = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="{PR_NS}">
      <Relationship Id="rIdImage" Type="{R_NS}/image" Target="{image_target}"{target_mode}/>
    </Relationships>"""
    members: dict[str, bytes | str] = {
        "ppt/presentation.xml": _presentation_xml(1),
        "ppt/_rels/presentation.xml.rels": _presentation_relationships(1),
        "ppt/slides/slide1.xml": _slide_xml([_picture(1)], hidden=hidden),
        "ppt/slides/_rels/slide1.xml.rels": slide_relationships,
        "ppt/media/table.emf": image,
    }
    return _write_package(
        root / "プロジェクト" / HIGHLIGHT_LOCATION / "05.会議" / HIGHLIGHT_CONTAINER,
        members,
    )


class PptxMixedContentRulesTest(unittest.TestCase):
    def test_amount_contract_alias_resolution_graph_gate_and_metamorphic_page(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            write_amount_pptx(root)
            engine = _engine(
                root,
                amount_aliases=[AMOUNT_LOCATION],
                filename_aliases=["提案書"],
            )
            contract = graph_contract_for_question(AMOUNT_QUESTION)
            self.assertIsNotNone(contract)
            assert contract is not None
            self.assertTrue(validate_graph_contract(AMOUNT_QUESTION, contract))
            self.assertTrue(contract["graph_contract_id"].startswith("pptx_mixed_"))
            self.assertEqual(contract["rule_id"], "pptx_amount_summary_page")
            self.assertEqual(
                contract["requested_output"]["answer_shape"],
                {"container": "scalar", "value_type": "integer", "unit": "ページ"},
            )

            plan = _graph_plan(AMOUNT_QUESTION)
            resolved = decide_from_graph(engine, AMOUNT_QUESTION, plan)
            self.assertIsNotNone(resolved)
            assert resolved is not None and resolved.result is not None
            self.assertEqual((resolved.status, resolved.result.answer), ("resolved", "2ページ"))

            missing = decide_from_graph(engine, AMOUNT_QUESTION, None)
            self.assertIsNotNone(missing)
            assert missing is not None
            self.assertEqual(
                (missing.status, missing.reason),
                ("hold", "pptx_mixed_graph_plan_not_certified"),
            )
            changed_plan = _graph_plan(AMOUNT_QUESTION)
            changed_plan.branch_intents[0]["intent"]["extended_graph_contract"][
                "scope"
            ]["container"] = "changed.pptx"
            mismatch = decide_from_graph(engine, AMOUNT_QUESTION, changed_plan)
            self.assertIsNotNone(mismatch)
            assert mismatch is not None
            self.assertEqual(
                (mismatch.status, mismatch.reason),
                ("hold", "pptx_mixed_graph_plan_contract_mismatch"),
            )

            write_amount_pptx(
                root,
                summary_slides={3},
                slide_count=4,
                amounts=("7,100,000", "710,000", "7,810,000"),
            )
            moved = decide_question(engine, AMOUNT_QUESTION)
            self.assertIsNotNone(moved)
            assert moved is not None and moved.result is not None
            self.assertEqual(moved.result.answer, "3ページ")

    def test_amount_duplicate_hidden_ordinal_source_and_alias_ambiguity_hold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            write_amount_pptx(
                root,
                summary_slides={2},
                hidden_slides={1},
            )
            engine = _engine(
                root,
                amount_aliases=[AMOUNT_LOCATION],
                filename_aliases=["提案書"],
            )
            ordinal = decide_question(engine, AMOUNT_QUESTION)
            self.assertIsNotNone(ordinal)
            assert ordinal is not None and ordinal.result is not None
            self.assertEqual(ordinal.result.answer, "2ページ")

            write_amount_pptx(root, summary_slides={1, 2})
            duplicated_summary = decide_question(engine, AMOUNT_QUESTION)
            self.assertIsNotNone(duplicated_summary)
            assert duplicated_summary is not None
            self.assertEqual(
                (duplicated_summary.status, duplicated_summary.reason),
                ("hold", "pptx_amount_summary_not_unique"),
            )

            write_amount_pptx(root)
            source = root / "プロジェクト" / AMOUNT_LOCATION / "00.提案" / AMOUNT_CONTAINER
            duplicate = root / "プロジェクト" / AMOUNT_LOCATION / "別置" / AMOUNT_CONTAINER
            duplicate.parent.mkdir(parents=True)
            shutil.copyfile(source, duplicate)
            duplicated_source = decide_question(engine, AMOUNT_QUESTION)
            self.assertIsNotNone(duplicated_source)
            assert duplicated_source is not None
            self.assertEqual(
                (duplicated_source.status, duplicated_source.reason),
                ("hold", "pptx_source_not_unique"),
            )
            duplicate.unlink()

            ambiguous_engine = _engine(
                root,
                amount_aliases=[AMOUNT_LOCATION],
                filename_aliases=["提案書", "見積書"],
            )
            ambiguous = decide_question(ambiguous_engine, AMOUNT_QUESTION)
            self.assertIsNotNone(ambiguous)
            assert ambiguous is not None
            self.assertEqual(
                (ambiguous.status, ambiguous.reason),
                ("hold", "pptx_alias_or_root_ambiguous"),
            )

    def test_amount_malformed_relationship_hidden_only_and_near_miss_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            write_amount_pptx(root, malformed_relationship=True)
            engine = _engine(
                root,
                amount_aliases=[AMOUNT_LOCATION],
                filename_aliases=["提案書"],
            )
            malformed = decide_question(engine, AMOUNT_QUESTION)
            self.assertIsNotNone(malformed)
            assert malformed is not None
            self.assertEqual(
                (malformed.status, malformed.reason),
                ("hold", "pptx_slide_order_invalid"),
            )

            write_amount_pptx(root, summary_slides={2}, hidden_slides={2})
            hidden = decide_question(engine, AMOUNT_QUESTION)
            self.assertIsNotNone(hidden)
            assert hidden is not None
            self.assertEqual(
                (hidden.status, hidden.reason),
                ("hold", "pptx_amount_summary_not_unique"),
            )
            self.assertIsNone(
                graph_contract_for_question(
                    AMOUNT_QUESTION.replace("何ページですか", "どこですか")
                )
            )
            tampered = copy.deepcopy(graph_contract_for_question(AMOUNT_QUESTION))
            assert tampered is not None
            tampered["requested_output"]["answer_shape"]["unit"] = "枚"
            self.assertFalse(validate_graph_contract(AMOUNT_QUESTION, tampered))

            write_amount_pptx(
                root,
                amounts=("1,000", "999,000", "3,000"),
            )
            inconsistent = decide_question(engine, AMOUNT_QUESTION)
            self.assertIsNotNone(inconsistent)
            assert inconsistent is not None
            self.assertEqual(
                (inconsistent.status, inconsistent.reason),
                ("hold", "pptx_amount_summary_not_unique"),
            )

    def test_xml_declaration_tokens_are_rejected_anywhere_in_payload(self) -> None:
        payload = (
            b'<?xml version="1.0"?>'
            + b"<!--"
            + (b"x" * 5000)
            + b'--><!DOCTYPE root [<!ENTITY injected "value">]>'
            + b"<root>&injected;</root>"
        )
        self.assertIsNone(_safe_xml(payload))

    def test_emf_contract_graph_gate_gridline_filter_and_metamorphic_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            write_highlight_pptx(root)
            engine = _engine(root, highlight_aliases=[HIGHLIGHT_LOCATION])
            contract = graph_contract_for_question(HIGHLIGHT_QUESTION)
            self.assertIsNotNone(contract)
            assert contract is not None
            self.assertTrue(validate_graph_contract(HIGHLIGHT_QUESTION, contract))
            self.assertEqual(contract["rule_id"], "pptx_emf_highlighted_table_value")
            self.assertEqual(
                contract["requested_output"]["answer_shape"]["container"],
                "key_value",
            )
            self.assertIsNone(contract["requested_output"]["required_keys"])

            resolved = decide_from_graph(
                engine, HIGHLIGHT_QUESTION, _graph_plan(HIGHLIGHT_QUESTION)
            )
            self.assertIsNotNone(resolved)
            assert resolved is not None and resolved.result is not None
            self.assertEqual(
                resolved.result.answer,
                "行条件: R2（行項目）、列条件: C2（列項目）、集計内容: 0.82",
            )

            write_highlight_pptx(root, selected_row=1, selected_column=0)
            moved = decide_question(engine, HIGHLIGHT_QUESTION)
            self.assertIsNotNone(moved)
            assert moved is not None and moved.result is not None
            self.assertEqual(
                moved.result.answer,
                "行条件: R1（行項目）、列条件: C0（列項目）、集計内容: 0.70",
            )

    def test_emf_duplicate_malformed_state_hidden_and_alias_ambiguity_hold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            engine = _engine(root, highlight_aliases=[HIGHLIGHT_LOCATION])
            cases = (
                ({"duplicate_marker": True}, "pptx_highlighted_cell_not_unique"),
                ({"truncate_emf": True}, "pptx_emf_invalid"),
                ({"mapping_change": True}, "pptx_emf_invalid"),
                ({"bad_restore": True}, "pptx_emf_invalid"),
                ({"hidden": True}, "pptx_highlighted_cell_not_unique"),
                ({"external_image": True}, "pptx_picture_relationship_invalid"),
                ({"extra_paint_primitive": True}, "pptx_emf_invalid"),
                ({"unknown_record": True}, "pptx_emf_invalid"),
                ({"duplicate_header": True}, "pptx_emf_invalid"),
                ({"initial_origin": (-1, 0)}, "pptx_emf_invalid"),
                ({"initial_origin": (1, 0)}, "pptx_emf_invalid"),
            )
            for kwargs, reason in cases:
                with self.subTest(kwargs=kwargs):
                    write_highlight_pptx(root, **kwargs)
                    decision = decide_question(engine, HIGHLIGHT_QUESTION)
                    self.assertIsNotNone(decision)
                    assert decision is not None
                    self.assertEqual((decision.status, decision.reason), ("hold", reason))

            write_highlight_pptx(root)
            ambiguous_engine = _engine(
                root,
                highlight_aliases=[HIGHLIGHT_LOCATION, "株式会社別の交通"],
            )
            ambiguous = decide_question(ambiguous_engine, HIGHLIGHT_QUESTION)
            self.assertIsNotNone(ambiguous)
            assert ambiguous is not None
            self.assertEqual(
                (ambiguous.status, ambiguous.reason),
                ("hold", "pptx_alias_or_root_ambiguous"),
            )


if __name__ == "__main__":
    unittest.main()
