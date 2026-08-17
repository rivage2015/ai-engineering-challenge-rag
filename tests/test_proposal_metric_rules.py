from __future__ import annotations

import copy
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
RAG = ROOT / "rag"
if str(RAG) not in sys.path:
    sys.path.insert(0, str(RAG))

from glossary import Glossary  # noqa: E402
from proposal_metric_rules import (  # noqa: E402
    decide_extended,
    graph_contract_for_question,
    validate_graph_contract,
)
from score_candidate_rules import (  # noqa: E402
    decide_extended as decide_from_main_graph,
    graph_contract_for_question as main_graph_contract,
    validate_graph_contract as validate_main_graph_contract,
)


PROJECT = "架空北斗研究所"
QUESTION = (
    f"{PROJECT}の提案書内で、"
    "重視するとされている評価指標を答えてください。"
)


def _xml_text(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _shape_xml(shape_id: int, runs: list[str], *, hidden: bool = False) -> str:
    hidden_attribute = ' hidden="1"' if hidden else ""
    paragraphs = "".join(
        "<a:p><a:r><a:rPr/><a:t>"
        + _xml_text(run)
        + "</a:t></a:r><a:endParaRPr/></a:p>"
        for run in runs
    )
    return f"""
      <p:sp>
        <p:nvSpPr>
          <p:cNvPr id="{shape_id}" name="Shape {shape_id}"{hidden_attribute}/>
          <p:cNvSpPr/><p:nvPr/>
        </p:nvSpPr>
        <p:spPr/>
        <p:txBody><a:bodyPr/><a:lstStyle/>{paragraphs}</p:txBody>
      </p:sp>
    """


def write_proposal(
    path: Path,
    slides: list[list[tuple[list[str], bool]]],
    *,
    hidden_slides: set[int] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    hidden_slides = hidden_slides or set()
    slide_ids = "".join(
        f'<p:sldId id="{255 + number}" r:id="rId{number}"/>'
        for number in range(1, len(slides) + 1)
    )
    presentation = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <p:presentation
      xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
      xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
      <p:sldIdLst>{slide_ids}</p:sldIdLst>
    </p:presentation>"""
    relationships = "".join(
        f'<Relationship Id="rId{number}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
        f'Target="slides/slide{number}.xml"/>'
        for number in range(1, len(slides) + 1)
    )
    relationship_document = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      {relationships}
    </Relationships>"""
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
    <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
      <Default Extension="xml" ContentType="application/xml"/>
    </Types>"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("ppt/presentation.xml", presentation)
        archive.writestr(
            "ppt/_rels/presentation.xml.rels", relationship_document
        )
        for slide_number, shapes in enumerate(slides, 1):
            shape_document = "".join(
                _shape_xml(shape_number, runs, hidden=hidden)
                for shape_number, (runs, hidden) in enumerate(shapes, 1)
            )
            slide_visibility = ' show="0"' if slide_number in hidden_slides else ""
            slide = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
            <p:sld{slide_visibility}
              xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
              xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
              xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
              <p:cSld><p:spTree>{shape_document}</p:spTree></p:cSld>
            </p:sld>"""
            archive.writestr(f"ppt/slides/slide{slide_number}.xml", slide)


def engine_for(root: Path, *, alias: str | None = None) -> SimpleNamespace:
    glossary = Glossary()
    if alias is not None:
        glossary.add(alias, PROJECT)
    return SimpleNamespace(source_root=root.resolve(), glossary=glossary)


class ProposalMetricGraphContractTest(unittest.TestCase):
    def test_full_grammar_builds_stable_self_validating_contract(self) -> None:
        contract = graph_contract_for_question(QUESTION)
        self.assertIsNotNone(contract)
        assert contract is not None
        self.assertEqual(contract["rule_id"], "proposal_explicit_priority_metric")
        self.assertEqual(contract["bindings"], {"location": PROJECT})
        self.assertEqual(
            [node["operator"] for node in contract["operation_graph"]["nodes"]],
            [
                "retrieve",
                "select_authoritative",
                "parse_visible_text_runs",
                "filter_explicit_priority_marker",
                "verify_unique",
                "project_metric_token",
            ],
        )
        self.assertTrue(validate_graph_contract(QUESTION, contract))

        mutated = copy.deepcopy(contract)
        mutated["scope"]["version_state"] = "caller_claimed"
        self.assertFalse(validate_graph_contract(QUESTION, mutated))
        unserializable = copy.deepcopy(contract)
        unserializable["scope"]["unexpected"] = object()
        self.assertFalse(validate_graph_contract(QUESTION, unserializable))
        self.assertIsNone(graph_contract_for_question(QUESTION + " suffix"))
        self.assertIsNone(
            graph_contract_for_question(
                f"{PROJECT}の提案書内で、評価指標を答えてください。"
            )
        )

    def test_main_graph_gate_routes_the_independent_proposal_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = (
                root / "プロジェクト" / PROJECT / "00.提案" / "提案書.pptx"
            )
            write_proposal(source, [[(["OrbitYield ◆最重要"], False)]])

            contract = main_graph_contract(QUESTION)
            self.assertIsNotNone(contract)
            assert contract is not None
            self.assertEqual(contract["rule_id"], "proposal_explicit_priority_metric")
            self.assertTrue(validate_main_graph_contract(QUESTION, contract))

            decision = decide_from_main_graph(engine_for(root), "opaque", QUESTION)
            self.assertEqual(decision.status, "resolved")
            assert decision.result is not None
            self.assertEqual(decision.result.answer, "OrbitYield")


class ProposalMetricDecisionTest(unittest.TestCase):
    def test_unique_decorated_metric_resolves_and_metamorphoses(self) -> None:
        for metric in ("VectorFlux", "OrbitYield"):
            with self.subTest(metric=metric), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = (
                    root
                    / "プロジェクト"
                    / PROJECT
                    / "00.提案"
                    / "提案書.pptx"
                )
                write_proposal(
                    source,
                    [
                        [
                            ([f"{metric} ★重視"], False),
                            (
                                ["別の値も評価するが、運用上の安定性を重視する。"],
                                False,
                            ),
                        ]
                    ],
                )

                decision = decide_extended(engine_for(root), "opaque", QUESTION)
                self.assertEqual(decision.status, "resolved")
                self.assertIsNotNone(decision.result)
                assert decision.result is not None
                self.assertEqual(decision.result.answer, metric)
                self.assertEqual(decision.result.operation_count, 6)
                self.assertEqual(decision.result.output_count, 1)
                self.assertEqual(
                    decision.result.source_paths,
                    ("プロジェクト/架空北斗研究所/00.提案/提案書.pptx",),
                )

    def test_glossary_alias_scopes_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = (
                root / "プロジェクト" / PROJECT / "00.提案" / "提案書.pptx"
            )
            write_proposal(source, [[(["OpaqueRate ◆最重要"], False)]])
            alias = "HBX"
            question = (
                f"{alias}の提案書内で、"
                "重視するとされている評価指標を答えてください。"
            )

            decision = decide_extended(engine_for(root, alias=alias), "opaque", question)

            self.assertEqual(decision.status, "resolved")
            assert decision.result is not None
            self.assertEqual(decision.result.answer, "OpaqueRate")

    def test_plain_narrative_use_of_priority_word_does_not_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = (
                root / "プロジェクト" / PROJECT / "00.提案" / "提案書.pptx"
            )
            write_proposal(
                source,
                [[(["運用ではNovaScoreを重視する。"], False)]],
            )

            decision = decide_extended(engine_for(root), "opaque", QUESTION)

            self.assertEqual(decision.status, "hold")
            self.assertEqual(decision.reason, "priority_marker_not_unique")

    def test_zero_or_multiple_marked_runs_hold(self) -> None:
        cases = {
            "zero": [(["NovaScore"], False)],
            "multiple": [
                (["NovaScore ★重視"], False),
                (["QuasarLift ◎重点"], False),
            ],
        }
        for label, shapes in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = (
                    root
                    / "プロジェクト"
                    / PROJECT
                    / "00.提案"
                    / "提案書.pptx"
                )
                write_proposal(source, [shapes])

                decision = decide_extended(engine_for(root), "opaque", QUESTION)

                self.assertEqual(decision.status, "hold")
                self.assertEqual(decision.reason, "priority_marker_not_unique")

    def test_ambiguous_or_marker_only_run_holds(self) -> None:
        for marked_run in (
            "★重視",
            "NovaScoreとQuasarLift ★重視",
            "NovaScore QuasarLift ★重視",
        ):
            with self.subTest(marked_run=marked_run), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = (
                    root
                    / "プロジェクト"
                    / PROJECT
                    / "00.提案"
                    / "提案書.pptx"
                )
                write_proposal(source, [[([marked_run], False)]])

                decision = decide_extended(engine_for(root), "opaque", QUESTION)

                self.assertEqual(decision.status, "hold")
                self.assertEqual(decision.reason, "priority_metric_ambiguous")

    def test_duplicate_current_sources_hold_but_archives_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            proposal = root / "プロジェクト" / PROJECT / "00.提案"
            write_proposal(
                proposal / "提案書.pptx", [[(["NovaScore ★重視"], False)]]
            )
            write_proposal(
                proposal / "old" / "提案書.pptx",
                [[(["ArchiveValue ★重視"], False)]],
            )
            write_proposal(
                proposal / "提案書old.pptx",
                [[(["BackupValue ★重視"], False)]],
            )

            unique = decide_extended(engine_for(root), "opaque", QUESTION)
            self.assertEqual(unique.status, "resolved")
            assert unique.result is not None
            self.assertEqual(unique.result.answer, "NovaScore")

            write_proposal(
                proposal / "提案書_v2.pptx",
                [[(["QuasarLift ★重視"], False)]],
            )
            duplicate = decide_extended(engine_for(root), "opaque", QUESTION)
            self.assertEqual(duplicate.status, "hold")
            self.assertEqual(duplicate.reason, "proposal_source_not_unique")

    def test_hidden_shape_and_hidden_slide_do_not_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = (
                root / "プロジェクト" / PROJECT / "00.提案" / "提案書.pptx"
            )
            write_proposal(
                source,
                [
                    [
                        (["NovaScore ★重視"], False),
                        (["HiddenShape ★重視"], True),
                    ],
                    [(["HiddenSlide ★重視"], False)],
                ],
                hidden_slides={2},
            )

            decision = decide_extended(engine_for(root), "opaque", QUESTION)

            self.assertEqual(decision.status, "resolved")
            assert decision.result is not None
            self.assertEqual(decision.result.answer, "NovaScore")

    def test_missing_or_corrupt_source_holds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            missing = decide_extended(engine_for(root), "opaque", QUESTION)
            self.assertEqual(missing.status, "hold")
            self.assertEqual(missing.reason, "proposal_source_not_unique")

            source = (
                root / "プロジェクト" / PROJECT / "00.提案" / "提案書.pptx"
            )
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"not-an-office-package")
            corrupt = decide_extended(engine_for(root), "opaque", QUESTION)
            self.assertEqual(corrupt.status, "hold")
            self.assertEqual(corrupt.reason, "proposal_source_invalid")

    def test_unsupported_question_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            decision = decide_extended(
                engine_for(Path(temp)),
                "opaque",
                f"{PROJECT}の提案書について教えてください。",
            )
        self.assertIsNone(decision)


if __name__ == "__main__":
    unittest.main()
