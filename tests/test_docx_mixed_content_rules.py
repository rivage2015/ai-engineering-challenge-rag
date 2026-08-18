from __future__ import annotations

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

from answer import validate_graph_answer  # noqa: E402
from docx_mixed_content_rules import (  # noqa: E402
    decide_question,
    graph_contract_for_question,
    validate_graph_contract,
)
from question_graph_runtime import build_graph_plan  # noqa: E402
from structured_candidate import StructuredCandidateEngine  # noqa: E402


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
C_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

CONTENT_TYPES = b"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
</Types>
"""


def engine_for(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        source_root=root.resolve(),
        glossary=SimpleNamespace(entries={}),
    )


def write_package(path: Path, members: dict[str, bytes | str]) -> None:
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


def emf_text_record(x: int, y: int, text: str) -> bytes:
    encoded = text.encode("utf-16le")
    size = (76 + len(encoded) + 3) // 4 * 4
    record = bytearray(size)
    struct.pack_into("<II", record, 0, 84, size)
    struct.pack_into("<iiII", record, 36, x, y, len(text), 76)
    record[76 : 76 + len(encoded)] = encoded
    return bytes(record)


def emf_table_bytes(
    left_average: str = "130,000",
    right_average: str = "110,000",
    *,
    left_cell: str | None = None,
    mapping_change_after_first_text: bool = False,
) -> bytes:
    runs = [
        (100, 100, "職務タイトル"),
        (500, 100, "米国平均給与（米ドル）"),
        (900, 100, "備考"),
        (100, 200, "アルファ職"),
        (500, 200, left_cell if left_cell is not None else f"平均 {left_average}"),
        (900, 200, "A"),
        (100, 300, "ベータ職"),
        (500, 300, f"平均 {right_average}"),
        (900, 300, "B"),
    ]
    records = [emf_text_record(x, y, value) for x, y, value in runs]
    if mapping_change_after_first_text:
        records.insert(1, struct.pack("<IIii", 10, 16, 40, 20))
    eof = struct.pack("<IIIII", 14, 20, 0, 16, 20)
    header = bytearray(88)
    total_size = len(header) + sum(len(record) for record in records) + len(eof)
    struct.pack_into("<II", header, 0, 1, len(header))
    struct.pack_into(
        "<IIII",
        header,
        40,
        0x464D4520,
        0x00010000,
        total_size,
        len(records) + 2,
    )
    return b"".join([bytes(header), *records, eof])


def write_emf_docx(
    root: Path,
    *,
    left: str = "130,000",
    right: str = "110,000",
    left_cell: str | None = None,
    mapping_change_after_first_text: bool = False,
) -> Path:
    path = root / "プロジェクト" / "架空企画" / "00.提案" / "職種調査.docx"
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{W_NS}" xmlns:a="{A_NS}" xmlns:r="{R_NS}">
  <w:body><w:p><w:r><w:drawing><a:blip r:embed="rId1"/></w:drawing></w:r></w:p></w:body>
</w:document>"""
    relationships = f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{PR_NS}">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/table.emf"/>
</Relationships>"""
    write_package(
        path,
        {
            "word/document.xml": document,
            "word/_rels/document.xml.rels": relationships,
            "word/media/table.emf": emf_table_bytes(
                left,
                right,
                left_cell=left_cell,
                mapping_change_after_first_text=mapping_change_after_first_text,
            ),
        },
    )
    return path


def cell(value: str, nested: str = "") -> str:
    return f"<w:tc><w:p><w:r><w:t>{escape(value)}</w:t></w:r></w:p>{nested}</w:tc>"


def row(*values: str) -> str:
    return "<w:tr>" + "".join(cell(value) for value in values) + "</w:tr>"


def write_nested_table_docx(
    root: Path,
    *,
    upper: str = "150,000",
    baseline: str = "100,000",
    duplicate_source_header: bool = False,
    conflicting_currency: bool = False,
    cell_currency_conflict: bool = False,
) -> Path:
    path = root / "プロジェクト" / "架空企画" / "00.提案" / "給与調査.docx"
    upper_header = "上位90%（円）" if conflicting_currency else "上位90%"
    headers = ["情報源", upper_header, "中央値（米ドル）"]
    values = [
        "Omega",
        f"{upper}円" if cell_currency_conflict else upper,
        baseline,
    ]
    if duplicate_source_header:
        headers.append("調査主体")
        values.append("Omega")
    inner = "<w:tbl>" + row(*headers) + row(*values) + "</w:tbl>"
    outer = "<w:tbl><w:tr>" + cell("親表文字", inner) + "</w:tr></w:tbl>"
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{W_NS}"><w:body>
  <w:p><w:r><w:t>技術職給与</w:t></w:r></w:p>
  {outer}
</w:body></w:document>"""
    write_package(path, {"word/document.xml": document})
    return path


def points(values: tuple[str, ...]) -> str:
    rendered = "".join(
        f'<c:pt idx="{index}"><c:v>{escape(value)}</c:v></c:pt>'
        for index, value in enumerate(values)
    )
    return f'<c:ptCount val="{len(values)}"/>{rendered}'


def series(index: int, theme_color: str, values: tuple[str, ...]) -> str:
    return f"""
<c:ser>
  <c:idx val="{index}"/><c:order val="{index}"/>
  <c:spPr><a:ln><a:solidFill><a:schemeClr val="{theme_color}"/></a:solidFill></a:ln></c:spPr>
  <c:val><c:numRef><c:numCache>{points(values)}</c:numCache></c:numRef></c:val>
</c:ser>"""


def chart_xml(title: str, groups: tuple[tuple[str, tuple[str, ...]], ...]) -> str:
    line_charts = "".join(
        f"<c:lineChart>{series(index, color, values)}</c:lineChart>"
        for index, (color, values) in enumerate(groups)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<c:chartSpace xmlns:c="{C_NS}" xmlns:a="{A_NS}">
  <c:chart>
    <c:title><c:tx><c:rich><a:p><a:r><a:t>{escape(title)}</a:t></a:r></a:p></c:rich></c:tx></c:title>
    <c:plotArea>{line_charts}</c:plotArea>
  </c:chart>
</c:chartSpace>"""


def write_chart_docx(
    root: Path,
    *,
    blue_values: tuple[str, ...] = ("0.10", "0.23456", "0.30"),
    duplicate_blue: bool = False,
) -> Path:
    path = root / "プロジェクト" / "架空企画" / "05.会議" / "図表.docx"
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{W_NS}" xmlns:c="{C_NS}" xmlns:r="{R_NS}">
  <w:body>
    <w:p><w:r><w:drawing><c:chart r:id="rId1"/></w:drawing></w:r></w:p>
    <w:p><w:r><w:drawing><c:chart r:id="rId2"/></w:drawing></w:r></w:p>
  </w:body>
</w:document>"""
    relationships = f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{PR_NS}">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart" Target="charts/chart1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart" Target="charts/chart2.xml"/>
  <Relationship Id="rIdTheme" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme2.xml"/>
</Relationships>"""
    referenced_theme = f"""<?xml version="1.0" encoding="UTF-8"?>
<a:theme xmlns:a="{A_NS}" name="opaque"><a:themeElements><a:clrScheme name="opaque">
  <a:accent1><a:srgbClr val="156082"/></a:accent1>
  <a:accent2><a:srgbClr val="E97132"/></a:accent2>
</a:clrScheme></a:themeElements></a:theme>"""
    decoy_theme = f"""<?xml version="1.0" encoding="UTF-8"?>
<a:theme xmlns:a="{A_NS}" name="decoy"><a:themeElements><a:clrScheme name="decoy">
  <a:accent1><a:srgbClr val="E97132"/></a:accent1>
  <a:accent2><a:srgbClr val="156082"/></a:accent2>
</a:clrScheme></a:themeElements></a:theme>"""
    chart_relationships = f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{PR_NS}">
  <Relationship Id="rIdExternal" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/package" Target="file:///untrusted/external.xlsx" TargetMode="External"/>
</Relationships>"""
    second_color = "accent1" if duplicate_blue else "accent2"
    write_package(
        path,
        {
            "word/document.xml": document,
            "word/_rels/document.xml.rels": relationships,
            "word/theme/theme1.xml": decoy_theme,
            "word/theme/theme2.xml": referenced_theme,
            "word/charts/_rels/chart2.xml.rels": chart_relationships,
            "word/charts/chart1.xml": chart_xml(
                "グラフ1", (("accent1", ("1.11", "2.22", "3.334")),)
            ),
            "word/charts/chart2.xml": chart_xml(
                "グラフ2",
                (
                    (second_color, ("10", "20", "30")),
                    ("accent1", blue_values),
                ),
            ),
        },
    )
    return path


def write_comment_docx(
    root: Path,
    anchor: str = "原文アンカー",
    *,
    with_controls: bool = False,
    cross_paragraph: bool = False,
) -> Path:
    path = (
        root
        / "プロジェクト"
        / "架空企画"
        / "05.会議"
        / "会議録"
        / "会議録_2030-01-01.docx"
    )
    anchor_xml = f'<w:r><w:t xml:space="preserve">{escape(anchor)}</w:t></w:r>'
    if with_controls:
        anchor_xml += (
            '<w:r><w:tab/><w:t>続き</w:t><w:br/>'
            '<w:t xml:space="preserve">末尾  </w:t></w:r>'
        )
    if cross_paragraph:
        anchor_body = f"""<w:p><w:commentRangeStart w:id="0"/>{anchor_xml}</w:p>
<w:p><w:r><w:t>第二段落</w:t></w:r><w:commentRangeEnd w:id="0"/>
<w:r><w:commentReference w:id="0"/></w:r></w:p>"""
    else:
        anchor_body = f"""<w:p><w:commentRangeStart w:id="0"/>{anchor_xml}
<w:commentRangeEnd w:id="0"/><w:r><w:commentReference w:id="0"/></w:r></w:p>"""
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{W_NS}"><w:body>{anchor_body}</w:body></w:document>"""
    comments = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:comments xmlns:w="{W_NS}"><w:comment w:id="0"><w:p><w:r><w:t>確認</w:t></w:r></w:p></w:comment></w:comments>"""
    relationships = f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{PR_NS}">
  <Relationship Id="rIdC" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>
</Relationships>"""
    write_package(
        path,
        {
            "word/document.xml": document,
            "word/comments.xml": comments,
            "word/_rels/document.xml.rels": relationships,
        },
    )
    return path


EMF_QUESTION = (
    "架空企画の職種調査資料において、米国平均給与における"
    "アルファ職とベータ職の差はいくらですか。"
)
NESTED_QUESTION = (
    "架空企画の給与調査において、Omegaが公表している技術職給与について、"
    "上位90%の層と中央値の差はいくらですか。"
)
BLUE_CHART_QUESTION = (
    "架空企画の図表.docxのグラフ2で、x=2のときの"
    "青色の折れ線のyの値を小数第3位で答えてください。"
)
SINGLE_CHART_QUESTION = (
    "架空企画の図表.docxのグラフ1で、x=3のときのyの値を"
    "小数第2位で答えてください。"
)
COMMENT_QUESTION = (
    "架空企画の会議録において、コメントがついている部分を"
    "そのまま抽出してください。"
)


class DocxMixedContentRulesTest(unittest.TestCase):
    def test_contract_and_graphplan_execute_emf_without_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            write_emf_docx(root)
            contract = graph_contract_for_question(EMF_QUESTION)
            self.assertIsNotNone(contract)
            assert contract is not None
            self.assertTrue(validate_graph_contract(EMF_QUESTION, contract))
            self.assertEqual(contract["rule_id"], "docx_emf_table_average_difference")
            self.assertEqual(contract["scope"]["source_channel"], "embedded_emf_unicode_text")

            plan = build_graph_plan("opaque-emf", EMF_QUESTION, fast_advisory=True)
            decision = StructuredCandidateEngine(root, SimpleNamespace(entries={})).decide_from_graph(
                "opaque-emf", EMF_QUESTION, plan
            )
            self.assertEqual(decision.status, "resolved")
            assert decision.result is not None
            self.assertEqual(decision.result.answer, "20,000ドル")
            self.assertEqual(validate_graph_answer(decision.result.answer, plan), ())

            missing_plan = StructuredCandidateEngine(
                root, SimpleNamespace(entries={})
            ).decide_from_graph("opaque-emf", EMF_QUESTION, None)
            self.assertEqual(
                (missing_plan.status, missing_plan.reason),
                ("hold", "extended_graph_plan_not_certified"),
            )

            write_emf_docx(root, right="105,000")
            mutated = decide_question(engine_for(root), EMF_QUESTION)
            self.assertIsNotNone(mutated)
            assert mutated is not None and mutated.result is not None
            self.assertEqual(mutated.result.answer, "25,000ドル")

    def test_emf_duplicate_source_and_truncated_record_hold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            original = write_emf_docx(root)
            duplicate = root / "プロジェクト" / "架空企画" / "別置" / original.name
            duplicate.parent.mkdir(parents=True)
            shutil.copyfile(original, duplicate)
            decision = decide_question(engine_for(root), EMF_QUESTION)
            self.assertIsNotNone(decision)
            assert decision is not None
            self.assertEqual((decision.status, decision.reason), ("hold", "docx_source_not_unique"))

            duplicate.unlink()
            write_emf_docx(root)
            with zipfile.ZipFile(original) as archive:
                members = {
                    info.filename: archive.read(info.filename)
                    for info in archive.infolist()
                    if info.filename != "[Content_Types].xml"
                }
            members["word/media/table.emf"] = members["word/media/table.emf"][:-1]
            write_package(original, members)
            malformed = decide_question(engine_for(root), EMF_QUESTION)
            self.assertIsNotNone(malformed)
            assert malformed is not None
            self.assertEqual((malformed.status, malformed.reason), ("hold", "emf_table_not_found"))

            write_emf_docx(root, left_cell="平均約 44,000 ～ 161,000")
            ranged = decide_question(engine_for(root), EMF_QUESTION)
            self.assertIsNotNone(ranged)
            assert ranged is not None
            self.assertEqual(
                (ranged.status, ranged.reason),
                ("hold", "emf_table_semantics_not_unique"),
            )

            write_emf_docx(root, left_cell="平均 130,000円")
            currency_conflict = decide_question(engine_for(root), EMF_QUESTION)
            self.assertIsNotNone(currency_conflict)
            assert currency_conflict is not None
            self.assertEqual(
                (currency_conflict.status, currency_conflict.reason),
                ("hold", "emf_table_semantics_not_unique"),
            )

            write_emf_docx(root, mapping_change_after_first_text=True)
            remapped = decide_question(engine_for(root), EMF_QUESTION)
            self.assertIsNotNone(remapped)
            assert remapped is not None
            self.assertEqual(
                (remapped.status, remapped.reason),
                ("hold", "emf_table_not_found"),
            )

    def test_nested_leaf_table_and_header_ambiguity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            write_nested_table_docx(root)
            decision = decide_question(engine_for(root), NESTED_QUESTION)
            self.assertIsNotNone(decision)
            assert decision is not None and decision.result is not None
            self.assertEqual(decision.result.answer, "50,000ドル")

            write_nested_table_docx(root, upper="163,000")
            mutated = decide_question(engine_for(root), NESTED_QUESTION)
            self.assertIsNotNone(mutated)
            assert mutated is not None and mutated.result is not None
            self.assertEqual(mutated.result.answer, "63,000ドル")

            write_nested_table_docx(root, duplicate_source_header=True)
            ambiguous = decide_question(engine_for(root), NESTED_QUESTION)
            self.assertIsNotNone(ambiguous)
            assert ambiguous is not None
            self.assertEqual(
                (ambiguous.status, ambiguous.reason),
                ("hold", "nested_table_semantics_not_unique"),
            )

            write_nested_table_docx(root, conflicting_currency=True)
            conflict = decide_question(engine_for(root), NESTED_QUESTION)
            self.assertIsNotNone(conflict)
            assert conflict is not None
            self.assertEqual(
                (conflict.status, conflict.reason),
                ("hold", "nested_table_semantics_not_unique"),
            )

            write_nested_table_docx(root, cell_currency_conflict=True)
            cell_conflict = decide_question(engine_for(root), NESTED_QUESTION)
            self.assertIsNotNone(cell_conflict)
            assert cell_conflict is not None
            self.assertEqual(
                (cell_conflict.status, cell_conflict.reason),
                ("hold", "nested_table_semantics_not_unique"),
            )

            write_nested_table_docx(root, upper="9" * 5_000)
            oversized = decide_question(engine_for(root), NESTED_QUESTION)
            self.assertIsNotNone(oversized)
            assert oversized is not None
            self.assertEqual(
                (oversized.status, oversized.reason),
                ("hold", "nested_table_difference_not_integral"),
            )

    def test_native_chart_cache_color_order_rounding_and_ambiguity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            write_chart_docx(root)
            blue = decide_question(engine_for(root), BLUE_CHART_QUESTION)
            self.assertIsNotNone(blue)
            assert blue is not None and blue.result is not None
            self.assertEqual(blue.result.answer, "0.235")

            single = decide_question(engine_for(root), SINGLE_CHART_QUESTION)
            self.assertIsNotNone(single)
            assert single is not None and single.result is not None
            self.assertEqual(single.result.answer, "3.33")

            write_chart_docx(root, blue_values=("0.10", "0.87654", "0.30"))
            mutated = decide_question(engine_for(root), BLUE_CHART_QUESTION)
            self.assertIsNotNone(mutated)
            assert mutated is not None and mutated.result is not None
            self.assertEqual(mutated.result.answer, "0.877")

            write_chart_docx(root, duplicate_blue=True)
            ambiguous = decide_question(engine_for(root), BLUE_CHART_QUESTION)
            self.assertIsNotNone(ambiguous)
            assert ambiguous is not None
            self.assertEqual(
                (ambiguous.status, ambiguous.reason),
                ("hold", "chart_series_not_unique"),
            )

            write_chart_docx(root, blue_values=("0.10", "1E+1000", "0.30"))
            extreme = decide_question(engine_for(root), BLUE_CHART_QUESTION)
            self.assertIsNotNone(extreme)
            assert extreme is not None
            self.assertEqual(
                (extreme.status, extreme.reason),
                ("hold", "chart_value_not_roundable"),
            )

            chart_path = write_chart_docx(root)
            with zipfile.ZipFile(chart_path) as archive:
                members = {
                    info.filename: archive.read(info.filename)
                    for info in archive.infolist()
                    if info.filename != "[Content_Types].xml"
                }
            members["word/charts/chart2.xml"] = members["word/charts/chart2.xml"].replace(
                b'ptCount val="3"', b'ptCount val="100000001"'
            )
            write_package(chart_path, members)
            oversized = decide_question(engine_for(root), BLUE_CHART_QUESTION)
            self.assertIsNotNone(oversized)
            assert oversized is not None
            self.assertEqual(
                (oversized.status, oversized.reason),
                ("hold", "chart_cache_incomplete"),
            )

    def test_comment_range_projects_exact_text_and_requires_uniqueness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            first = write_comment_docx(root, "  生の原文  A-17", with_controls=True)
            decision = decide_question(engine_for(root), COMMENT_QUESTION)
            self.assertIsNotNone(decision)
            assert decision is not None and decision.result is not None
            self.assertEqual(decision.result.answer, "  生の原文  A-17\t続き\n末尾  ")

            write_comment_docx(root, "第一段落", cross_paragraph=True)
            cross_paragraph = decide_question(engine_for(root), COMMENT_QUESTION)
            self.assertIsNotNone(cross_paragraph)
            assert cross_paragraph is not None and cross_paragraph.result is not None
            self.assertEqual(cross_paragraph.result.answer, "第一段落\n第二段落")

            second = (
                root
                / "プロジェクト"
                / "架空企画"
                / "05.会議"
                / "会議録"
                / "会議録_2030-02-01.docx"
            )
            second.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(first, second)
            ambiguous = decide_question(engine_for(root), COMMENT_QUESTION)
            self.assertIsNotNone(ambiguous)
            assert ambiguous is not None
            self.assertEqual(
                (ambiguous.status, ambiguous.reason),
                ("hold", "comment_anchor_not_unique"),
            )

    def test_every_selector_is_bound_and_all_routes_use_live_graph_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            write_emf_docx(root)
            write_nested_table_docx(root)
            write_chart_docx(root)
            write_comment_docx(root)
            engine = StructuredCandidateEngine(root, SimpleNamespace(entries={}))
            expected = {
                EMF_QUESTION: "20,000ドル",
                NESTED_QUESTION: "50,000ドル",
                BLUE_CHART_QUESTION: "0.235",
                COMMENT_QUESTION: "原文アンカー",
            }
            for ordinal, (question, answer) in enumerate(expected.items(), 1):
                with self.subTest(route=ordinal):
                    plan = build_graph_plan(f"opaque-{ordinal}", question, fast_advisory=True)
                    decision = engine.decide_from_graph(f"opaque-{ordinal}", question, plan)
                    self.assertEqual(decision.status, "resolved")
                    assert decision.result is not None
                    self.assertEqual(decision.result.answer, answer)
                    self.assertEqual(validate_graph_answer(answer, plan), ())

            mutations = {
                EMF_QUESTION.replace("アルファ職", "ガンマ職"),
                NESTED_QUESTION.replace("技術職給与", "果物価格"),
                BLUE_CHART_QUESTION.replace("青色", "赤色"),
                COMMENT_QUESTION.replace("架空企画", "別企画"),
            }
            for question in mutations:
                with self.subTest(mutated_question=question):
                    plan = build_graph_plan("opaque-hold", question, fast_advisory=True)
                    decision = engine.decide_from_graph("opaque-hold", question, plan)
                    self.assertEqual(decision.status, "hold")


if __name__ == "__main__":
    unittest.main()
