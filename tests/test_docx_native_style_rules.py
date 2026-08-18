from __future__ import annotations

import copy
import sys
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[1]
RAG = ROOT / "rag"
if str(RAG) not in sys.path:
    sys.path.insert(0, str(RAG))

from answer import validate_graph_answer  # noqa: E402
import docx_native_style_rules as native_rules  # noqa: E402
from docx_native_style_rules import (  # noqa: E402
    decide_question,
    graph_contract_for_question,
    validate_graph_contract,
)
from question_graph_runtime import build_graph_plan  # noqa: E402
from structured_candidate import StructuredCandidateEngine  # noqa: E402
from glossary import build_glossary  # noqa: E402


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES = b"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
</Types>
"""

HIGHLIGHT_QUESTION = (
    "架空与信の中間報告資料にて、黄色ハイライトかつ"
    "赤字となっている部分を抜き出してください。"
)
BOLD_QUESTION = (
    "架空契約との契約書において、"
    "太字で記載されている部分を抽出してください。"
)
MEETING_STYLE_QUESTION = (
    "架空資産の会議録の中で、"
    "太字、下線、イタリックのすべてに該当する箇所を"
    "抽出してください。"
)
Q003_VARIANT = (
    "架空契約の契約書において、"
    "太字で記載されている箇所のうち、"
    "日付以外のものをすべて抽出してください。"
)
TRIPLE_STYLE = (
    '<w:b/><w:bCs/><w:i/><w:iCs/><w:u w:val="single"/>'
)


def engine_for(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        source_root=root.resolve(),
        glossary=SimpleNamespace(entries={}),
    )


def write_package(
    path: Path,
    document: str | bytes,
    styles: str | bytes,
    *,
    extra: dict[str, str | bytes] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    members: dict[str, str | bytes] = {
        "[Content_Types].xml": CONTENT_TYPES,
        "word/document.xml": document,
        "word/styles.xml": styles,
    }
    members.update(extra or {})
    members.setdefault(
        "_rels/.rels",
        f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{PR_NS}">
  <Relationship Id="rIdMain" Type="{R_NS}/officeDocument" Target="word/document.xml"/>
</Relationships>""",
    )
    related = [
        f'<Relationship Id="rIdStyles" Type="{R_NS}/styles" Target="styles.xml"/>'
    ]
    if "word/theme/theme1.xml" in members:
        related.append(
            f'<Relationship Id="rIdTheme" Type="{R_NS}/theme" Target="theme/theme1.xml"/>'
        )
    if "word/numbering.xml" in members:
        related.append(
            f'<Relationship Id="rIdNumbering" Type="{R_NS}/numbering" Target="numbering.xml"/>'
        )
    if "word/footnotes.xml" in members:
        related.append(
            f'<Relationship Id="rIdFootnotes" Type="{R_NS}/footnotes" Target="footnotes.xml"/>'
        )
    members.setdefault(
        "word/_rels/document.xml.rels",
        f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{PR_NS}">{''.join(related)}</Relationships>""",
    )
    if not (extra and "[Content_Types].xml" in extra):
        content_types = {
            "word/document.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        }
        for name in members:
            basename = name.rsplit("/", 1)[-1]
            if name.startswith("word/") and basename.startswith("styles") and name.endswith(".xml"):
                content_types[name] = "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"
            elif name.startswith("word/theme/") and name.endswith(".xml"):
                content_types[name] = "application/vnd.openxmlformats-officedocument.theme+xml"
            elif name.startswith("word/") and basename.startswith("numbering") and name.endswith(".xml"):
                content_types[name] = "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"
            elif name.startswith("word/header") and name.endswith(".xml"):
                content_types[name] = "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"
            elif name.startswith("word/footer") and name.endswith(".xml"):
                content_types[name] = "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"
            elif name.startswith("word/footnotes") and name.endswith(".xml"):
                content_types[name] = "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"
            elif name.startswith("word/endnotes") and name.endswith(".xml"):
                content_types[name] = "application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml"
        overrides = "".join(
            f'<Override PartName="/{name}" ContentType="{content_type}"/>'
            for name, content_type in sorted(content_types.items())
        )
        members["[Content_Types].xml"] = f"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  {overrides}
</Types>"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, value in members.items():
            archive.writestr(
                name,
                value.encode("utf-8") if isinstance(value, str) else value,
            )


def styles_xml(*, doc_defaults: str = "", definitions: str = "") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="{W_NS}">
  <w:docDefaults><w:rPrDefault><w:rPr>{doc_defaults}</w:rPr></w:rPrDefault></w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"/>
  {definitions}
</w:styles>"""


def theme_xml(accent1: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="opaque">
  <a:themeElements><a:clrScheme name="opaque">
    <a:dk1><a:srgbClr val="000000"/></a:dk1>
    <a:lt1><a:srgbClr val="FFFFFF"/></a:lt1>
    <a:accent1><a:srgbClr val="{accent1}"/></a:accent1>
  </a:clrScheme></a:themeElements>
</a:theme>"""


def main_relationships(*records: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{PR_NS}">{''.join(records)}</Relationships>"""


def relationship(
    relation_id: str,
    kind: str,
    target: str,
    *,
    external: bool = False,
) -> str:
    mode = ' TargetMode="External"' if external else ""
    return (
        f'<Relationship Id="{relation_id}" Type="{R_NS}/{kind}" '
        f'Target="{target}"{mode}/>'
    )


def run(
    text: str,
    *,
    properties: str = "",
    character_style: str | None = None,
) -> str:
    style = (
        f'<w:rStyle w:val="{escape(character_style)}"/>'
        if character_style is not None
        else ""
    )
    rpr = f"<w:rPr>{style}{properties}</w:rPr>" if style or properties else ""
    return f"<w:r>{rpr}<w:t>{escape(text)}</w:t></w:r>"


def paragraph(content: str, *, paragraph_style: str | None = None) -> str:
    ppr = (
        f'<w:pPr><w:pStyle w:val="{escape(paragraph_style)}"/></w:pPr>'
        if paragraph_style is not None
        else ""
    )
    return f"<w:p>{ppr}{content}</w:p>"


def document_xml(*paragraphs: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{W_NS}"><w:body>{''.join(paragraphs)}</w:body></w:document>"""


def report_path(root: Path, name: str = "報告資料_2030-02-01.docx") -> Path:
    return (
        root
        / "プロジェクト"
        / "架空与信株式会社"
        / "05.会議"
        / "報告資料"
        / name
    )


def contract_path(
    root: Path,
    directory: str = "01.契約",
) -> Path:
    return (
        root
        / "プロジェクト"
        / "架空契約株式会社"
        / directory
        / "契約書.docx"
    )


def meeting_minutes_path(
    root: Path,
    name: str = "会議録_2030-01-01.docx",
) -> Path:
    return (
        root
        / "プロジェクト"
        / "架空資産株式会社"
        / "05.会議"
        / "会議録"
        / name
    )


def write_meeting_minutes(
    root: Path,
    name: str,
    *paragraphs: str,
    styles: str | None = None,
) -> Path:
    path = meeting_minutes_path(root, name)
    write_package(path, document_xml(*paragraphs), styles or styles_xml())
    return path


def direct_highlight_run(value: str) -> str:
    return run(
        value,
        properties='<w:highlight w:val="yellow"/><w:color w:val="D00000"/>',
    )


def write_middle_report(
    root: Path,
    value: str,
    *,
    name: str = "報告資料_2030-02-01.docx",
    field_result: bool = False,
    second_match: str | None = None,
) -> Path:
    styled = direct_highlight_run(value)
    if field_result:
        styled = f'<w:fldSimple w:instr=" MERGEFIELD score ">{styled}</w:fldSimple>'
    paragraphs = [
        paragraph(run("本資料は中間分析報告です。")),
        paragraph(
            run("前", properties='<w:highlight w:val="yellow"/>')
            + styled
            + run(
                "後",
                properties=(
                    '<w:highlight w:val="yellow"/>'
                    '<w:color w:val="000000"/>'
                ),
            )
        ),
    ]
    if second_match is not None:
        paragraphs.append(paragraph(direct_highlight_run(second_match)))
    path = report_path(root, name)
    write_package(path, document_xml(*paragraphs), styles_xml())
    return path


def write_decoy_report(root: Path) -> Path:
    path = report_path(root, "報告資料_2030-01-01.docx")
    write_package(
        path,
        document_xml(
            paragraph(run("本資料はキックオフ報告です。中間レビュー準備は次工程です。")),
            paragraph(direct_highlight_run("9.999")),
        ),
        styles_xml(),
    )
    return path


def write_contract(
    root: Path,
    body: str,
    *,
    styles: str | None = None,
    directory: str = "01.契約",
) -> Path:
    path = contract_path(root, directory)
    write_package(path, document_xml(paragraph(body)), styles or styles_xml())
    return path


class DocxNativeStyleRulesTest(unittest.TestCase):
    def test_contract_uses_full_question_and_typed_style_scope(self) -> None:
        highlight = graph_contract_for_question(HIGHLIGHT_QUESTION)
        bold = graph_contract_for_question(BOLD_QUESTION)
        meeting = graph_contract_for_question(MEETING_STYLE_QUESTION)
        self.assertIsNotNone(highlight)
        self.assertIsNotNone(bold)
        self.assertIsNotNone(meeting)
        assert highlight is not None and bold is not None and meeting is not None
        self.assertTrue(validate_graph_contract(HIGHLIGHT_QUESTION, highlight))
        self.assertTrue(validate_graph_contract(BOLD_QUESTION, bold))
        self.assertTrue(
            validate_graph_contract(MEETING_STYLE_QUESTION, meeting)
        )
        self.assertTrue(
            highlight["graph_contract_id"].startswith("docx_mixed_native_style_")
        )
        self.assertEqual(
            [
                {"property": "highlight", "operator": "eq", "value": "yellow"},
                {"property": "font_color", "operator": "is_hue", "value": "red"},
            ],
            highlight["scope"]["style_predicates"],
        )
        self.assertEqual(
            {"container": "scalar", "value_type": "string", "unit": None},
            bold["requested_output"]["answer_shape"],
        )
        self.assertEqual(
            "docx_effective_bold_underline_italic_intersection",
            meeting["rule_id"],
        )
        self.assertEqual(
            [
                {"property": "bold", "operator": "eq", "value": True},
                {"property": "underline", "operator": "eq", "value": True},
                {"property": "italic", "operator": "eq", "value": True},
            ],
            meeting["scope"]["style_predicates"],
        )
        self.assertEqual(
            "05.会議/会議録/*.docx",
            meeting["scope"]["container"],
        )
        excluding_dates = graph_contract_for_question(Q003_VARIANT)
        self.assertIsNotNone(excluding_dates)
        self.assertEqual(
            "multiple", excluding_dates["requested_output"]["cardinality"]
        )

        tampered = copy.deepcopy(highlight)
        tampered["scope"]["style_predicates"][0]["value"] = "green"
        self.assertFalse(validate_graph_contract(HIGHLIGHT_QUESTION, tampered))
        for changed in (
            HIGHLIGHT_QUESTION + "suffix",
            HIGHLIGHT_QUESTION.replace("の中間報告", "を中間報告"),
            HIGHLIGHT_QUESTION.replace("黄色", "緑色"),
            BOLD_QUESTION.replace("太字", "斜体"),
            MEETING_STYLE_QUESTION + "追記",
            MEETING_STYLE_QUESTION.replace("太字、下線", "下線、太字"),
        ):
            self.assertIsNone(graph_contract_for_question(changed))

    def test_semantic_binding_precedes_style_and_field_mutation_follows_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            write_decoy_report(root)
            write_middle_report(root, "0.321", field_result=True)
            decision = decide_question(engine_for(root), HIGHLIGHT_QUESTION)
            self.assertIsNotNone(decision)
            assert decision is not None and decision.result is not None
            self.assertEqual((decision.status, decision.result.answer), ("resolved", "0.321"))
            self.assertEqual(1, len(decision.result.source_paths))
            self.assertIn("2030-02-01", decision.result.source_paths[0])

            write_middle_report(root, "0.654", field_result=True)
            mutated = decide_question(engine_for(root), HIGHLIGHT_QUESTION)
            self.assertIsNotNone(mutated)
            assert mutated is not None and mutated.result is not None
            self.assertEqual("0.654", mutated.result.answer)
            self.assertNotEqual(
                decision.result.source_sha256,
                mutated.result.source_sha256,
            )

    def test_hidden_effective_semantic_marker_does_not_bind_visible_styled_decoy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            hidden_styles = styles_xml(
                definitions=f"""
<w:style w:type="character" w:styleId="OpaqueHidden">
  <w:rPr><w:vanish/></w:rPr>
</w:style>""",
            )
            write_package(
                report_path(root),
                document_xml(
                    paragraph(
                        run(
                            "本資料は中間分析報告です。",
                            character_style="OpaqueHidden",
                        )
                    ),
                    paragraph(direct_highlight_run("8.888")),
                ),
                hidden_styles,
            )
            decision = decide_question(engine_for(root), HIGHLIGHT_QUESTION)
            self.assertIsNotNone(decision)
            assert decision is not None
            self.assertEqual(
                ("hold", "docx_semantic_source_not_unique"),
                (decision.status, decision.reason),
            )

    def test_nested_textbox_run_does_not_inherit_outer_run_formatting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            inner = paragraph(run("内側の非太字"))
            outer = (
                "<w:r><w:rPr><w:b/></w:rPr><w:t>外側の太字</w:t>"
                f"<w:drawing><w:txbxContent>{inner}</w:txbxContent></w:drawing>"
                "</w:r>"
            )
            write_package(
                contract_path(root),
                document_xml(paragraph(outer)),
                styles_xml(),
            )
            resolved = decide_question(engine_for(root), BOLD_QUESTION)
            self.assertIsNotNone(resolved)
            assert resolved is not None and resolved.result is not None
            self.assertEqual("外側の太字", resolved.result.answer)

            outer_without_own_text = (
                "<w:r><w:rPr><w:b/></w:rPr>"
                f"<w:drawing><w:txbxContent>{inner}</w:txbxContent></w:drawing>"
                "</w:r>"
            )
            write_package(
                contract_path(root),
                document_xml(paragraph(outer_without_own_text)),
                styles_xml(),
            )
            held = decide_question(engine_for(root), BOLD_QUESTION)
            self.assertIsNotNone(held)
            assert held is not None
            self.assertEqual(
                ("hold", "docx_style_match_not_unique"),
                (held.status, held.reason),
            )

    def test_effective_cascade_supports_docdefaults_style_chains_and_direct_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            inherited_styles = styles_xml(
                doc_defaults='<w:highlight w:val="yellow"/>',
                definitions=f"""
<w:style w:type="character" w:styleId="RedBase">
  <w:rPr><w:color w:val="D00000"/></w:rPr>
</w:style>
<w:style w:type="character" w:styleId="RedChild">
  <w:basedOn w:val="RedBase"/>
</w:style>""",
            )
            report = report_path(root)
            write_package(
                report,
                document_xml(
                    paragraph(run("これは中間分析報告です。")),
                    paragraph(run("0.777", character_style="RedChild")),
                ),
                inherited_styles,
            )
            highlighted = decide_question(engine_for(root), HIGHLIGHT_QUESTION)
            self.assertIsNotNone(highlighted)
            assert highlighted is not None and highlighted.result is not None
            self.assertEqual("0.777", highlighted.result.answer)

            bold_styles = styles_xml(
                definitions=f"""
<w:style w:type="paragraph" w:styleId="BoldBase">
  <w:rPr><w:b/></w:rPr>
</w:style>
<w:style w:type="paragraph" w:styleId="BoldChild">
  <w:basedOn w:val="BoldBase"/>
</w:style>"""
            )
            write_package(
                contract_path(root),
                document_xml(
                    paragraph(
                        run("締結日：")
                        + run("2030-04-05")
                        + run("非対象", properties='<w:b w:val="0"/>'),
                        paragraph_style="BoldChild",
                    )
                ),
                bold_styles,
            )
            bold = decide_question(engine_for(root), BOLD_QUESTION)
            self.assertIsNotNone(bold)
            assert bold is not None and bold.result is not None
            self.assertEqual("締結日：2030-04-05", bold.result.answer)

    def test_meeting_minutes_scan_is_complete_and_excludes_hidden_and_deleted_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            write_meeting_minutes(
                root,
                "会議録_2030-01-01.docx",
                paragraph(run("4,250,000円", properties=TRIPLE_STYLE)),
            )
            write_meeting_minutes(
                root,
                "会議録_2030-01-15.docx",
                paragraph(
                    run(
                        "下線なし",
                        properties="<w:b/><w:bCs/><w:i/><w:iCs/>",
                    )
                ),
            )
            third = write_meeting_minutes(
                root,
                "会議録_2030-02-01.docx",
                paragraph(
                    run(
                        "非表示の三重一致",
                        properties=TRIPLE_STYLE + "<w:vanish/>",
                    )
                    + "<w:del>"
                    + run("削除済みの三重一致", properties=TRIPLE_STYLE)
                    + "</w:del>"
                ),
            )

            decision = decide_question(engine_for(root), MEETING_STYLE_QUESTION)
            self.assertIsNotNone(decision)
            assert decision is not None and decision.result is not None
            self.assertEqual(
                ("resolved", "4,250,000円"),
                (decision.status, decision.result.answer),
            )
            self.assertEqual(3, len(decision.result.source_paths))
            self.assertEqual(
                [
                    "会議録_2030-01-01.docx",
                    "会議録_2030-01-15.docx",
                    "会議録_2030-02-01.docx",
                ],
                [Path(path).name for path in decision.result.source_paths],
            )
            first_digest = decision.result.source_sha256

            write_package(
                third,
                document_xml(paragraph(run("非対象資料の更新"))),
                styles_xml(),
            )
            mutated = decide_question(engine_for(root), MEETING_STYLE_QUESTION)
            self.assertIsNotNone(mutated)
            assert mutated is not None and mutated.result is not None
            self.assertEqual("4,250,000円", mutated.result.answer)
            self.assertNotEqual(first_digest, mutated.result.source_sha256)

    def test_meeting_minutes_effective_italic_underline_cascade_and_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            inherited = styles_xml(
                doc_defaults='<w:u w:val="single"/>',
                definitions="""
<w:style w:type="paragraph" w:styleId="TripleBase">
  <w:rPr><w:b/><w:bCs/><w:i/><w:iCs/></w:rPr>
</w:style>
<w:style w:type="paragraph" w:styleId="TripleChild">
  <w:basedOn w:val="TripleBase"/>
</w:style>""",
            )
            write_meeting_minutes(
                root,
                "会議録_2030-01-01.docx",
                paragraph(run("継承三重一致"), paragraph_style="TripleChild"),
                paragraph(
                    run("下線解除", properties='<w:u w:val="none"/>'),
                    paragraph_style="TripleChild",
                ),
                paragraph(
                    run(
                        "斜体解除",
                        properties='<w:i w:val="0"/><w:iCs w:val="0"/>',
                    ),
                    paragraph_style="TripleChild",
                ),
                styles=inherited,
            )
            resolved = decide_question(engine_for(root), MEETING_STYLE_QUESTION)
            self.assertIsNotNone(resolved)
            assert resolved is not None and resolved.result is not None
            self.assertEqual("継承三重一致", resolved.result.answer)

    def test_meeting_minutes_zero_multiple_and_invalid_sources_hold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            write_meeting_minutes(
                root,
                "会議録_2030-01-01.docx",
                paragraph(run("下線なし", properties="<w:b/><w:i/>")),
            )
            zero = decide_question(engine_for(root), MEETING_STYLE_QUESTION)
            self.assertIsNotNone(zero)
            assert zero is not None
            self.assertEqual(
                ("hold", "docx_style_match_not_unique"),
                (zero.status, zero.reason),
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            for index in (1, 2):
                write_meeting_minutes(
                    root,
                    f"会議録_2030-01-0{index}.docx",
                    paragraph(run(f"三重一致{index}", properties=TRIPLE_STYLE)),
                )
            multiple = decide_question(engine_for(root), MEETING_STYLE_QUESTION)
            self.assertIsNotNone(multiple)
            assert multiple is not None
            self.assertEqual(
                ("hold", "docx_style_match_not_unique"),
                (multiple.status, multiple.reason),
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            write_meeting_minutes(
                root,
                "会議録_2030-01-01.docx",
                paragraph(run("三重一致", properties=TRIPLE_STYLE)),
            )
            write_package(
                meeting_minutes_path(root, "会議録_2030-01-02.docx"),
                "<broken",
                styles_xml(),
            )
            invalid = decide_question(engine_for(root), MEETING_STYLE_QUESTION)
            self.assertIsNotNone(invalid)
            assert invalid is not None
            self.assertEqual(
                ("hold", "docx_xml_malformed"),
                (invalid.status, invalid.reason),
            )

    def test_meeting_minutes_style_ambiguity_and_invalid_underline_hold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            write_meeting_minutes(
                root,
                "会議録_2030-01-01.docx",
                paragraph(
                    run(
                        "Latinعربي",
                        properties=(
                            '<w:b/><w:bCs/><w:i/><w:iCs w:val="0"/>'
                            '<w:u w:val="single"/>'
                        ),
                    )
                ),
            )
            ambiguous = decide_question(engine_for(root), MEETING_STYLE_QUESTION)
            self.assertIsNotNone(ambiguous)
            assert ambiguous is not None
            self.assertEqual(
                ("hold", "docx_italic_script_ambiguous"),
                (ambiguous.status, ambiguous.reason),
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            write_meeting_minutes(
                root,
                "会議録_2030-01-01.docx",
                paragraph(
                    run(
                        "不明な下線",
                        properties=(
                            '<w:b/><w:bCs/><w:i/><w:iCs/>'
                            '<w:u w:val="sparkle"/>'
                        ),
                    )
                ),
            )
            invalid = decide_question(engine_for(root), MEETING_STYLE_QUESTION)
            self.assertIsNotNone(invalid)
            assert invalid is not None
            self.assertEqual(
                ("hold", "docx_style_value_invalid"),
                (invalid.status, invalid.reason),
            )

    def test_duplicate_sources_multiple_matches_and_style_conflicts_hold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            write_middle_report(root, "0.111", second_match="0.222")
            multiple = decide_question(engine_for(root), HIGHLIGHT_QUESTION)
            self.assertIsNotNone(multiple)
            assert multiple is not None
            self.assertEqual(
                ("hold", "docx_style_match_not_unique"),
                (multiple.status, multiple.reason),
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            write_middle_report(root, "0.111")
            write_middle_report(root, "0.111", name="報告資料_2030-02-02.docx")
            duplicate_report = decide_question(engine_for(root), HIGHLIGHT_QUESTION)
            self.assertIsNotNone(duplicate_report)
            assert duplicate_report is not None
            self.assertEqual(
                ("hold", "docx_semantic_source_not_unique"),
                (duplicate_report.status, duplicate_report.reason),
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            write_contract(root, run("締結", properties="<w:b/>"))
            write_contract(
                root,
                run("締結", properties="<w:b/>"),
                directory="別契約",
            )
            duplicate_contract = decide_question(engine_for(root), BOLD_QUESTION)
            self.assertIsNotNone(duplicate_contract)
            assert duplicate_contract is not None
            self.assertEqual(
                ("hold", "docx_source_not_unique"),
                (duplicate_contract.status, duplicate_contract.reason),
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            write_contract(
                root,
                run(
                    "締結",
                    properties='<w:b/><w:b w:val="0"/>',
                ),
            )
            conflict = decide_question(engine_for(root), BOLD_QUESTION)
            self.assertIsNotNone(conflict)
            assert conflict is not None
            self.assertEqual(
                ("hold", "docx_style_conflict"),
                (conflict.status, conflict.reason),
            )

    def test_malformed_xml_and_unsafe_zip_hold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            path = contract_path(root)
            write_package(path, "<broken", styles_xml())
            malformed = decide_question(engine_for(root), BOLD_QUESTION)
            self.assertIsNotNone(malformed)
            assert malformed is not None
            self.assertEqual(
                ("hold", "docx_xml_malformed"),
                (malformed.status, malformed.reason),
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            path = contract_path(root)
            write_package(
                path,
                document_xml(paragraph(run("締結", properties="<w:b/>"))),
                styles_xml(),
                extra={"../escape.xml": "<x/>"},
            )
            unsafe = decide_question(engine_for(root), BOLD_QUESTION)
            self.assertIsNotNone(unsafe)
            assert unsafe is not None
            self.assertEqual(
                ("hold", "docx_archive_invalid"),
                (unsafe.status, unsafe.reason),
            )

    def test_relationships_are_authoritative_for_styles_and_theme(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            referenced_styles = styles_xml(
                definitions="""
<w:style w:type="character" w:styleId="RelBold">
  <w:rPr><w:b/></w:rPr>
</w:style>"""
            )
            write_package(
                contract_path(root),
                document_xml(
                    paragraph(run("関係参照の太字", character_style="RelBold"))
                ),
                styles_xml(),
                extra={
                    "word/styles2.xml": referenced_styles,
                    "word/_rels/document.xml.rels": main_relationships(
                        relationship("rIdStyles", "styles", "styles2.xml")
                    ),
                },
            )
            related = decide_question(engine_for(root), BOLD_QUESTION)
            self.assertIsNotNone(related)
            assert related is not None and related.result is not None
            self.assertEqual("関係参照の太字", related.result.answer)

            write_package(
                contract_path(root),
                document_xml(paragraph(run("本文", properties="<w:b/>"))),
                styles_xml(),
                extra={
                    "word/styles2.xml": styles_xml(),
                    "word/_rels/document.xml.rels": main_relationships(
                        relationship("rIdStyles1", "styles", "styles.xml"),
                        relationship("rIdStyles2", "styles", "styles2.xml"),
                    ),
                },
            )
            duplicate = decide_question(engine_for(root), BOLD_QUESTION)
            self.assertIsNotNone(duplicate)
            assert duplicate is not None
            self.assertEqual(
                ("hold", "docx_relationship_not_unique"),
                (duplicate.status, duplicate.reason),
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            path = report_path(root)
            themed = run(
                "0.888",
                properties=(
                    '<w:highlight w:val="yellow"/>'
                    '<w:color w:val="000000" w:themeColor="accent1"/>'
                ),
            )
            write_package(
                path,
                document_xml(
                    paragraph(run("本資料は中間分析報告です。")),
                    paragraph(themed),
                ),
                styles_xml(),
                extra={
                    "word/theme/theme1.xml": theme_xml("000000"),
                    "word/theme/theme2.xml": theme_xml("D00000"),
                    "word/_rels/document.xml.rels": main_relationships(
                        relationship("rIdStyles", "styles", "styles.xml"),
                        relationship("rIdTheme", "theme", "theme/theme2.xml"),
                    ),
                },
            )
            theme_related = decide_question(engine_for(root), HIGHLIGHT_QUESTION)
            self.assertIsNotNone(theme_related)
            assert theme_related is not None and theme_related.result is not None
            self.assertEqual("0.888", theme_related.result.answer)

    def test_utf16_entity_canonical_part_collision_and_deep_xml_hold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            dangerous = (
                '<?xml version="1.0" encoding="UTF-16"?>'
                + (" " * 4096)
                + '<!DOCTYPE w:document [<!ENTITY payload "偽太字">]>'
                + f'<w:document xmlns:w="{W_NS}"><w:body><w:p><w:r>'
                + '<w:rPr><w:b/></w:rPr><w:t>&payload;</w:t>'
                + '</w:r></w:p></w:body></w:document>'
            ).encode("utf-16")
            write_package(contract_path(root), dangerous, styles_xml())
            entity = decide_question(engine_for(root), BOLD_QUESTION)
            self.assertIsNotNone(entity)
            assert entity is not None
            self.assertEqual(
                ("hold", "docx_xml_encoding_unsupported"),
                (entity.status, entity.reason),
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            write_package(
                contract_path(root),
                document_xml(paragraph(run("本文", properties="<w:b/>"))),
                styles_xml(),
                extra={"word/Styles.xml": styles_xml()},
            )
            collision = decide_question(engine_for(root), BOLD_QUESTION)
            self.assertIsNotNone(collision)
            assert collision is not None
            self.assertEqual(
                ("hold", "docx_archive_invalid"),
                (collision.status, collision.reason),
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            inner = run("深層", properties="<w:b/>")
            for _ in range(300):
                inner = f"<w:sdt><w:sdtContent>{inner}</w:sdtContent></w:sdt>"
            write_package(
                contract_path(root),
                document_xml(paragraph(inner)),
                styles_xml(),
            )
            deep = decide_question(engine_for(root), BOLD_QUESTION)
            self.assertIsNotNone(deep)
            assert deep is not None
            self.assertEqual(
                ("hold", "docx_xml_resource_limit"),
                (deep.status, deep.reason),
            )

    def test_only_relationship_referenced_visible_stories_are_scanned(self) -> None:
        footnotes = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:footnotes xmlns:w="{W_NS}">
  <w:footnote w:id="7"><w:p>{run('未参照脚注', properties='<w:b/>')}</w:p></w:footnote>
</w:footnotes>"""
        header = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:hdr xmlns:w="{W_NS}"><w:p>{run('未参照ヘッダー', properties='<w:b/>')}</w:p></w:hdr>"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            write_package(
                contract_path(root),
                document_xml(paragraph(run("可視本文", properties="<w:b/>"))),
                styles_xml(),
                extra={
                    "word/header1.xml": header,
                    "word/footnotes.xml": footnotes,
                },
            )
            unreferenced = decide_question(engine_for(root), BOLD_QUESTION)
            self.assertIsNotNone(unreferenced)
            assert unreferenced is not None and unreferenced.result is not None
            self.assertEqual("可視本文", unreferenced.result.answer)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            document = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{W_NS}" xmlns:r="{R_NS}"><w:body>
  {paragraph(run('可視本文', properties='<w:b/>'))}
  <w:sectPr><w:headerReference w:type="default" r:id="rIdHeader"/></w:sectPr>
</w:body></w:document>"""
            write_package(
                contract_path(root),
                document,
                styles_xml(),
                extra={
                    "word/header1.xml": header,
                    "word/_rels/document.xml.rels": main_relationships(
                        relationship("rIdStyles", "styles", "styles.xml"),
                        relationship("rIdHeader", "header", "header1.xml"),
                    ),
                },
            )
            referenced = decide_question(engine_for(root), BOLD_QUESTION)
            self.assertIsNotNone(referenced)
            assert referenced is not None
            self.assertEqual(
                ("hold", "docx_style_match_not_unique"),
                (referenced.status, referenced.reason),
            )

    def test_script_and_color_ambiguity_and_oversized_duplicate_hold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            write_contract(
                root,
                run(
                    "Latinعربي",
                    properties='<w:b/><w:bCs w:val="0"/>',
                ),
            )
            script = decide_question(engine_for(root), BOLD_QUESTION)
            self.assertIsNotNone(script)
            assert script is not None
            self.assertEqual(
                ("hold", "docx_bold_script_ambiguous"),
                (script.status, script.reason),
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            write_package(
                report_path(root),
                document_xml(
                    paragraph(run("本資料は中間分析報告です。")),
                    paragraph(
                        run(
                            "0.444",
                            properties=(
                                '<w:highlight w:val="yellow"/>'
                                '<w:color w:val="D00000" w:themeColor="accent1" '
                                'w:themeTint="80"/>'
                            ),
                        )
                    ),
                ),
                styles_xml(),
                extra={
                    "word/theme/theme1.xml": theme_xml("D00000"),
                },
            )
            color = decide_question(engine_for(root), HIGHLIGHT_QUESTION)
            self.assertIsNotNone(color)
            assert color is not None
            self.assertEqual(
                ("hold", "docx_font_color_unresolved"),
                (color.status, color.reason),
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            first = write_middle_report(root, "0.111")
            second = report_path(root, "報告資料_2030-02-02.docx")
            write_package(
                second,
                document_xml(
                    paragraph(run("本資料は中間分析報告です。")),
                    paragraph(direct_highlight_run("0.222")),
                ),
                styles_xml(),
                extra={"word/media/padding.bin": b"x" * 20_000},
            )
            limit = first.stat().st_size + 100
            self.assertGreater(second.stat().st_size, limit)
            with patch.object(native_rules, "_MAX_DOCX_BYTES", limit):
                oversized = decide_question(engine_for(root), HIGHLIGHT_QUESTION)
            self.assertIsNotNone(oversized)
            assert oversized is not None
            self.assertEqual(
                ("hold", "docx_source_resource_limit"),
                (oversized.status, oversized.reason),
            )

    def test_live_graphplan_is_required_and_contract_mismatch_holds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "共有ドライブ"
            write_contract(
                root,
                run("締結日：2030-04-05", properties="<w:b/>"),
            )
            plan = build_graph_plan("opaque-style", BOLD_QUESTION, fast_advisory=True)
            engine = StructuredCandidateEngine(root, SimpleNamespace(entries={}))
            decision = engine.decide_from_graph("opaque-style", BOLD_QUESTION, plan)
            self.assertEqual("resolved", decision.status)
            assert decision.result is not None
            self.assertEqual("締結日：2030-04-05", decision.result.answer)
            self.assertEqual((), validate_graph_answer(decision.result.answer, plan))

            write_meeting_minutes(
                root,
                "会議録_2030-01-01.docx",
                paragraph(run("4,250,000円", properties=TRIPLE_STYLE)),
            )
            meeting_plan = build_graph_plan(
                "opaque-meeting-style",
                MEETING_STYLE_QUESTION,
                fast_advisory=True,
            )
            self.assertEqual("pass", meeting_plan.strict_status)
            meeting = engine.decide_from_graph(
                "opaque-meeting-style",
                MEETING_STYLE_QUESTION,
                meeting_plan,
            )
            self.assertEqual("resolved", meeting.status)
            assert meeting.result is not None
            self.assertEqual("4,250,000円", meeting.result.answer)
            self.assertEqual(
                (),
                validate_graph_answer(meeting.result.answer, meeting_plan),
            )

            missing = engine.decide_from_graph("opaque-style", BOLD_QUESTION, None)
            self.assertEqual(
                ("hold", "extended_graph_plan_not_certified"),
                (missing.status, missing.reason),
            )

            branches = copy.deepcopy(plan.branch_intents)
            branches[0]["intent"]["extended_graph_contract"]["scope"][
                "location"
            ] = "tampered"
            mismatched_plan = replace(plan, branch_intents=branches)
            mismatched = engine.decide_from_graph(
                "opaque-style", BOLD_QUESTION, mismatched_plan
            )
            self.assertEqual(
                ("hold", "extended_graph_plan_contract_mismatch"),
                (mismatched.status, mismatched.reason),
            )

    def test_actual_q003_decrypts_in_memory_and_excludes_dates(self) -> None:
        source_root = ROOT / "share" / "共有ドライブ"
        engine = StructuredCandidateEngine(
            source_root, build_glossary(source_root)
        )
        question = (
            "恒一会 かえで総合病院の契約書において、"
            "太字で記載されている箇所のうち、"
            "日付以外のものをすべて抽出してください。"
        )
        decision = decide_question(engine, question)
        self.assertEqual("resolved", decision.status)
        self.assertEqual(
            "「time_and_materials」、"
            "「実績工数に基づき、案件完了後に最終成果物の検収を経て一括精算する。」、"
            "「30分単位」、「25,000円／時間」",
            decision.result.answer,
        )
        self.assertEqual(1, len(decision.result.source_paths))


if __name__ == "__main__":
    unittest.main()
