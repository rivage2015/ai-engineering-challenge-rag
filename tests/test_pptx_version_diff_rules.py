from __future__ import annotations

import copy
import shutil
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

from answer import validate_graph_answer  # noqa: E402
from pptx_version_diff_rules import (  # noqa: E402
    decide_from_graph,
    decide_question,
    graph_contract_for_question,
    validate_graph_contract,
)
from question_graph_runtime import build_graph_plan  # noqa: E402
from structured_candidate import StructuredCandidateEngine  # noqa: E402


P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
C_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"

CONTENT_TYPES = b"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
</Types>
"""


def engine_for(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        source_root=root.resolve(),
        glossary=SimpleNamespace(entries={}),
    )


def explicit_question(
    *,
    location: str = "架空部門",
    before: str = "plan_v1.pptx",
    after: str = "plan_v2.pptx",
) -> str:
    return (
        f"{location}の{before}から{after}に修正されたもののうち、"
        "案件遂行に関連する変更を挙げてください。"
    )


def report_question(*, location: str = "架空医療室") -> str:
    return (
        f"{location}の最終報告書old版と最新版を比較したとき、"
        "案件遂行に関連する実質的な変更を挙げてください。"
    )


def _shape(
    shape_id: int,
    paragraphs: tuple[str, ...] | list[str],
    *,
    title: bool = False,
    hidden: bool = False,
) -> str:
    hidden_attribute = ' hidden="1"' if hidden else ""
    placeholder = '<p:ph type="title"/>' if title else ""
    body = "".join(
        f"<a:p><a:r><a:t>{escape(value)}</a:t></a:r></a:p>"
        for value in paragraphs
    )
    return f"""
      <p:sp>
        <p:nvSpPr>
          <p:cNvPr id="{shape_id}" name="Shape {shape_id}"{hidden_attribute}/>
          <p:cNvSpPr/><p:nvPr>{placeholder}</p:nvPr>
        </p:nvSpPr>
        <p:spPr/>
        <p:txBody><a:bodyPr/><a:lstStyle/>{body}</p:txBody>
      </p:sp>
    """


def _picture(shape_id: int) -> str:
    return f"""
      <p:pic>
        <p:nvPicPr>
          <p:cNvPr id="{shape_id}" name="Picture {shape_id}"/>
          <p:cNvPicPr/><p:nvPr/>
        </p:nvPicPr>
        <p:blipFill><a:blip r:embed="rIdImage"/></p:blipFill>
        <p:spPr/>
      </p:pic>
    """


def _table(shape_id: int, rows: tuple[tuple[str, ...], ...]) -> str:
    row_xml = []
    for row in rows:
        cells = "".join(
            "<a:tc><a:txBody><a:bodyPr/><a:lstStyle/>"
            f"<a:p><a:r><a:t>{escape(value)}</a:t></a:r></a:p>"
            "</a:txBody><a:tcPr/></a:tc>"
            for value in row
        )
        row_xml.append(f'<a:tr h="1">{cells}</a:tr>')
    return f"""
      <p:graphicFrame>
        <p:nvGraphicFramePr>
          <p:cNvPr id="{shape_id}" name="Table {shape_id}"/>
          <p:cNvGraphicFramePr/><p:nvPr/>
        </p:nvGraphicFramePr>
        <p:xfrm/>
        <a:graphic><a:graphicData uri="{A_NS}/table">
          <a:tbl><a:tblPr/><a:tblGrid/>{''.join(row_xml)}</a:tbl>
        </a:graphicData></a:graphic>
      </p:graphicFrame>
    """


def _chart_frame(shape_id: int, relation_id: str = "rIdChart") -> str:
    return f"""
      <p:graphicFrame>
        <p:nvGraphicFramePr>
          <p:cNvPr id="{shape_id}" name="Chart {shape_id}"/>
          <p:cNvGraphicFramePr/><p:nvPr/>
        </p:nvGraphicFramePr>
        <p:xfrm/>
        <a:graphic><a:graphicData uri="{C_NS}">
          <c:chart r:id="{relation_id}"/>
        </a:graphicData></a:graphic>
      </p:graphicFrame>
    """


def _chart_xml(
    categories: tuple[str, ...] = ("Model A", "Model B"),
    values: tuple[str, ...] = ("0.8", "0.9"),
) -> str:
    category_points = "".join(
        f'<c:pt idx="{index}"><c:v>{escape(value)}</c:v></c:pt>'
        for index, value in enumerate(categories)
    )
    value_points = "".join(
        f'<c:pt idx="{index}"><c:v>{escape(value)}</c:v></c:pt>'
        for index, value in enumerate(values)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <c:chartSpace xmlns:c="{C_NS}" xmlns:a="{A_NS}">
      <c:chart><c:plotArea><c:barChart><c:ser>
        <c:tx><c:v>Accuracy</c:v></c:tx>
        <c:cat><c:strLit>{category_points}</c:strLit></c:cat>
        <c:val><c:numLit>{value_points}</c:numLit></c:val>
      </c:ser></c:barChart></c:plotArea></c:chart>
    </c:chartSpace>"""


def _presentation_xml(slide_count: int, *, unsafe: bool = False) -> str:
    slide_ids = "".join(
        f'<p:sldId id="{255 + index}" r:id="rId{index}"/>'
        for index in range(1, slide_count + 1)
    )
    doctype = "<!DOCTYPE unsafe>" if unsafe else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>{doctype}
    <p:presentation xmlns:p="{P_NS}" xmlns:r="{R_NS}">
      <p:sldIdLst>{slide_ids}</p:sldIdLst>
    </p:presentation>"""


def _presentation_relationships(slide_count: int) -> str:
    relationships = "".join(
        f'<Relationship Id="rId{index}" Type="{R_NS}/slide" '
        f'Target="slides/slide{index}.xml"/>'
        for index in range(1, slide_count + 1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="{PR_NS}">{relationships}</Relationships>"""


def _slide_xml(elements: list[str], *, hidden: bool = False) -> str:
    visibility = ' show="0"' if hidden else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <p:sld xmlns:p="{P_NS}" xmlns:a="{A_NS}" xmlns:r="{R_NS}" xmlns:c="{C_NS}"{visibility}>
      <p:cSld><p:spTree>{''.join(elements)}</p:spTree></p:cSld>
    </p:sld>"""


def _slide_relationships(*, chart: bool = False) -> str:
    relationship = (
        f'<Relationship Id="rIdChart" Type="{R_NS}/chart" '
        'Target="../charts/chart1.xml"/>'
        if chart
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="{PR_NS}">{relationship}</Relationships>"""


def write_pptx(
    path: Path,
    slides: list[list[str]],
    *,
    chart_slides: set[int] | None = None,
    chart_values: tuple[str, ...] = ("0.8", "0.9"),
    hidden_slides: set[int] | None = None,
    unsafe: bool = False,
) -> Path:
    chart_slides = chart_slides or set()
    hidden_slides = hidden_slides or set()
    members: dict[str, bytes | str] = {
        "[Content_Types].xml": CONTENT_TYPES,
        "ppt/presentation.xml": _presentation_xml(len(slides), unsafe=unsafe),
        "ppt/_rels/presentation.xml.rels": _presentation_relationships(len(slides)),
    }
    for index, elements in enumerate(slides, 1):
        members[f"ppt/slides/slide{index}.xml"] = _slide_xml(
            elements, hidden=index in hidden_slides
        )
        members[f"ppt/slides/_rels/slide{index}.xml.rels"] = _slide_relationships(
            chart=index in chart_slides
        )
        if index in chart_slides:
            members["ppt/charts/chart1.xml"] = _chart_xml(values=chart_values)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, value in members.items():
            archive.writestr(
                name,
                value.encode("utf-8") if isinstance(value, str) else value,
            )
    return path


def _role_slide(person: str, *, hidden_note: str | None = None) -> list[str]:
    elements = [
        _shape(1, ("2. 実施体制",), title=True),
        _shape(2, ("品質レビューア", person, "成果物レビュー・報告")),
    ]
    if hidden_note is not None:
        elements.append(_shape(3, (hidden_note,), hidden=True))
    return elements


def _identifier_slide(values: tuple[str, ...]) -> list[str]:
    return [
        _shape(1, ("4. 分析アプローチ ― 実施手順",), title=True),
        _shape(2, values),
    ]


class PptxVersionDiffRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.location = self.root / "架空部門" / "00.提案"
        self.before = self.location / "plan_v1.pptx"
        self.after = self.location / "plan_v2.pptx"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_contract_is_complete_deterministic_and_tamper_evident(self) -> None:
        question = explicit_question()
        first = graph_contract_for_question(question)
        self.assertEqual(first, graph_contract_for_question(question))
        self.assertIsNotNone(first)
        self.assertTrue(first["graph_contract_id"].startswith("pptx_version_diff_"))
        self.assertEqual("multiple", first["requested_output"]["cardinality"])
        self.assertEqual("list", first["requested_output"]["answer_shape"]["container"])
        self.assertTrue(validate_graph_contract(question, first))

        tampered = copy.deepcopy(first)
        tampered["scope"]["noise_policy"] = "ignore_all_content"
        self.assertFalse(validate_graph_contract(question, tampered))
        self.assertIsNone(graph_contract_for_question(question + "図形も列挙してください。"))
        self.assertIsNone(
            graph_contract_for_question(
                explicit_question(before="plan_v2.pptx", after="plan_v1.pptx")
            )
        )

    def test_personnel_change_is_source_derived_and_metamorphic(self) -> None:
        write_pptx(self.before, [_role_slide("水野 葵")])
        write_pptx(self.after, [_role_slide("相川 蓮")])
        question = explicit_question()
        first = decide_question(engine_for(self.root), question)
        self.assertEqual("resolved", first.status)
        self.assertEqual(
            "役割「品質レビューア」の担当者: 水野 葵→相川 蓮",
            first.result.answer,
        )

        write_pptx(self.after, [_role_slide("森川 凛")])
        changed = decide_question(engine_for(self.root), question)
        self.assertEqual("resolved", changed.status)
        self.assertEqual(
            "役割「品質レビューア」の担当者: 水野 葵→森川 凛",
            changed.result.answer,
        )
        self.assertNotEqual(first.result.source_sha256, changed.result.source_sha256)

    def test_identifier_notation_changes_preserve_source_order(self) -> None:
        write_pptx(
            self.before,
            [_identifier_slide(("customer status", "risk score"))],
        )
        write_pptx(
            self.after,
            [_identifier_slide(("customer_status", "risk_score"))],
        )
        decision = decide_question(engine_for(self.root), explicit_question())
        self.assertEqual("resolved", decision.status)
        self.assertEqual(
            "カラム表記: customer status→customer_status、"
            "カラム表記: risk score→risk_score",
            decision.result.answer,
        )

    def test_layout_split_numbering_and_table_to_chart_are_noise(self) -> None:
        before_slides = [
            [
                _shape(1, ("1. モデル比較結果",), title=True),
                _table(2, (("モデル", "Accuracy"), ("Model A", "0.8"), ("Model B", "0.9"))),
            ],
            [
                _shape(1, ("3. 実施方法",), title=True),
                _shape(2, ("主な作業フロー", "1. 受付", "2. 検証")),
            ],
        ]
        after_slides = [
            [
                _shape(1, ("1. モデル比較結果",), title=True),
                _chart_frame(2),
            ],
            [
                _shape(1, ("3. 実施方法",), title=True),
                _shape(2, ("主な作業フロー",)),
                _shape(3, ("1",)),
                _shape(4, ("受付",)),
                _shape(5, ("2",)),
                _shape(6, ("検証",)),
            ],
        ]
        write_pptx(self.before, before_slides)
        write_pptx(self.after, after_slides, chart_slides={1})
        decision = decide_question(engine_for(self.root), explicit_question())
        self.assertEqual("resolved", decision.status)
        self.assertEqual(
            "案件遂行に関連する実質的な変更はありません",
            decision.result.answer,
        )

        # This makes the table/chart assertion non-vacuous: changing one
        # chart cache value must break the normalized fact equality even
        # though the slide title is outside the execution-title regex.
        write_pptx(
            self.after,
            after_slides,
            chart_slides={1},
            chart_values=("0.8", "0.91"),
        )
        changed = decide_question(engine_for(self.root), explicit_question())
        self.assertEqual("hold", changed.status)
        self.assertEqual("pptx_version_diff_source_not_certified", changed.reason)

    def test_old_to_latest_report_binding_preserves_provenance_direction(self) -> None:
        directory = self.root / "架空医療室" / "06.報告書"
        old = directory / "架空医療室_最終報告_old.pptx"
        latest = directory / "架空医療室_最終報告.pptx"
        workflow = [[
            _shape(1, ("5. 実施計画",), title=True),
            _shape(2, ("月次レビュー",)),
        ]]
        write_pptx(old, workflow)
        write_pptx(latest, workflow)
        decision = decide_question(engine_for(self.root), report_question())
        self.assertEqual("resolved", decision.status)
        self.assertTrue(decision.result.source_paths[0].endswith("_old.pptx"))
        self.assertTrue(decision.result.source_paths[1].endswith("_最終報告.pptx"))

    def test_hidden_shape_is_ignored_but_hidden_slide_or_visible_change_holds(self) -> None:
        write_pptx(self.before, [_role_slide("水野 葵", hidden_note="旧注記")])
        write_pptx(self.after, [_role_slide("水野 葵", hidden_note="新注記")])
        hidden = decide_question(engine_for(self.root), explicit_question())
        self.assertEqual("resolved", hidden.status)

        write_pptx(
            self.before,
            [_role_slide("水野 葵")],
            hidden_slides={1},
        )
        write_pptx(
            self.after,
            [_role_slide("水野 葵")],
            hidden_slides={1},
        )
        hidden_slide = decide_question(engine_for(self.root), explicit_question())
        self.assertEqual("hold", hidden_slide.status)

        write_pptx(
            self.before,
            [[
                _shape(1, ("5. 実施計画",), title=True),
                _shape(2, ("監査頻度 月次",)),
            ]],
        )
        write_pptx(
            self.after,
            [[
                _shape(1, ("5. 実施計画",), title=True),
                _shape(2, ("監査頻度 週次",)),
            ]],
        )
        changed = decide_question(engine_for(self.root), explicit_question())
        self.assertEqual("hold", changed.status)
        self.assertEqual("pptx_version_diff_source_not_certified", changed.reason)

    def test_layout_split_is_allowed_but_subject_object_swap_holds(self) -> None:
        write_pptx(
            self.before,
            [[
                _shape(1, ("5. 実施計画",), title=True),
                _shape(2, ("甲野が乙川を承認",)),
            ]],
        )
        write_pptx(
            self.after,
            [[
                _shape(1, ("5. 実施計画",), title=True),
                _shape(2, ("甲野が",)),
                _shape(3, ("乙川を承認",)),
            ]],
        )
        split = decide_question(engine_for(self.root), explicit_question())
        self.assertEqual("resolved", split.status)

        write_pptx(
            self.after,
            [[
                _shape(1, ("5. 実施計画",), title=True),
                _shape(2, ("乙川が甲野を承認",)),
            ]],
        )
        swapped = decide_question(engine_for(self.root), explicit_question())
        self.assertEqual("hold", swapped.status)
        self.assertEqual("pptx_version_diff_source_not_certified", swapped.reason)

    def test_unsafe_visible_or_ambiguous_sources_fail_closed(self) -> None:
        write_pptx(self.before, [_role_slide("水野 葵")])
        write_pptx(self.after, [_role_slide("相川 蓮")], unsafe=True)
        unsafe = decide_question(engine_for(self.root), explicit_question())
        self.assertEqual("hold", unsafe.status)

        write_pptx(self.after, [[
            _shape(1, ("2. 実施体制",), title=True),
            _picture(2),
        ]])
        picture = decide_question(engine_for(self.root), explicit_question())
        self.assertEqual("hold", picture.status)

        write_pptx(self.after, [_role_slide("相川 蓮")])
        duplicate = self.root / "架空部門" / "nested" / self.before.name
        duplicate.parent.mkdir(parents=True)
        shutil.copy2(self.before, duplicate)
        ambiguous = decide_question(engine_for(self.root), explicit_question())
        self.assertEqual("hold", ambiguous.status)
        self.assertEqual("pptx_version_diff_pair_not_unique", ambiguous.reason)

    def test_live_graph_plan_and_terminal_answer_contract_are_mandatory(self) -> None:
        write_pptx(self.before, [_role_slide("水野 葵")])
        write_pptx(self.after, [_role_slide("相川 蓮")])
        question = explicit_question()
        plan = build_graph_plan("opaque-pptx-diff", question, fast_advisory=True)
        self.assertEqual("pass", plan.strict_status)

        decision = decide_from_graph(engine_for(self.root), question, plan)
        self.assertEqual("resolved", decision.status)
        self.assertEqual((), validate_graph_answer(decision.result.answer, plan))

        live_engine = StructuredCandidateEngine(
            self.root.resolve(), SimpleNamespace(entries={})
        )
        live = live_engine.decide_from_graph("opaque-pptx-diff", question, plan)
        self.assertEqual("resolved", live.status)
        self.assertEqual((), validate_graph_answer(live.result.answer, plan))

        self.assertEqual(
            "pptx_version_diff_graph_plan_not_certified",
            decide_from_graph(engine_for(self.root), question, None).reason,
        )
        branches = copy.deepcopy(plan.branch_intents)
        branches[0]["intent"]["extended_graph_contract"]["scope"][
            "noise_policy"
        ] = "ignore_semantic_changes"
        tampered = replace(plan, branch_intents=branches)
        mismatch = decide_from_graph(engine_for(self.root), question, tampered)
        self.assertEqual("hold", mismatch.status)
        self.assertEqual("pptx_version_diff_graph_plan_contract_mismatch", mismatch.reason)


if __name__ == "__main__":
    unittest.main()
