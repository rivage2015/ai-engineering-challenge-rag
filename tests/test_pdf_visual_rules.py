from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RAG = ROOT / "rag"
if str(RAG) not in sys.path:
    sys.path.insert(0, str(RAG))

import pdf_visual_rules as rules  # noqa: E402
from answer import validate_graph_answer  # noqa: E402
from question_graph_runtime import build_graph_plan  # noqa: E402
from structured_candidate import (  # noqa: E402
    StructuredCandidateAnswer,
    StructuredCandidateDecision,
    StructuredCandidateEngine,
)


def _png_bytes(width: int = 800, height: int = 450) -> bytes:
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(output, format="PNG")
    return output.getvalue()


def _page_from_image(image: object, page_number: int = 1) -> rules._PageEvidence:
    output = io.BytesIO()
    image.save(output, format="PNG")
    data = output.getvalue()
    return rules._PageEvidence(
        page_number=page_number,
        png_bytes=data,
        image_sha256=hashlib.sha256(data).hexdigest(),
        width=image.width,
        height=image.height,
        materialized_path=None,
    )


def _word(
    text: str,
    bbox: rules._BBox,
    sequence: int,
    line: int = 1,
) -> rules._OCRWord:
    return rules._OCRWord(
        text=text,
        bbox=bbox,
        line_key=(1, 1, 1, line),
        sequence=sequence,
        confidence=0.99,
    )


def _three_row_grid_page(
    *,
    omit_middle_ocr: bool,
    omit_last_ocr: bool = False,
    open_bottom: bool = False,
) -> rules._PageEvidence:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1200, 800), "white")
    draw = ImageDraw.Draw(image)
    for x in (100, 500, 700, 1000):
        draw.line((x, 180, x, 630), fill="black", width=4)
    horizontal_lines = (180, 270, 390, 510) if open_bottom else (180, 270, 390, 510, 630)
    for y in horizontal_lines:
        draw.line((100, y, 1000, y), fill="black", width=4)
    for y, color in zip((315, 435, 555), ("green", "red", "orange")):
        draw.rectangle((570, y, 630, y + 32), fill=color)

    words = [
        _word("RiskQ", rules._BBox(150, 210, 300, 250), 1, 1),
        _word("ImpactZ", rules._BBox(540, 210, 650, 250), 2, 1),
        _word("opaque-a", rules._BBox(150, 310, 330, 350), 3, 2),
        _word("low", rules._BBox(575, 310, 625, 350), 4, 2),
    ]
    if not omit_middle_ocr:
        words.extend(
            (
                _word("opaque-b", rules._BBox(150, 430, 330, 470), 5, 3),
                _word("high", rules._BBox(575, 430, 635, 470), 6, 3),
            )
        )
    if not omit_last_ocr:
        words.extend(
            (
                _word("opaque-c", rules._BBox(150, 550, 330, 590), 7, 4),
                _word("mid", rules._BBox(575, 550, 625, 590), 8, 4),
            )
        )
    page = _page_from_image(image)
    page.ocr = rules._OCRResult(tuple(words), ())
    return page


class PDFVisualGrammarTest(unittest.TestCase):
    def test_marker_full_grammar_compiles_typed_graph(self) -> None:
        question = (
            "Opaque Worksの最終報告における、Signal Factorsのページで、"
            "マーカーされている単語をすべて抜き出してください。"
        )
        graph = rules.graph_contract_for_pdf_question(question)
        self.assertIsNotNone(graph)
        assert graph is not None
        self.assertEqual(
            graph["rule_id"], "pdf_page_inline_marker_word_projection"
        )
        self.assertEqual(graph["requested_output"]["cardinality"], "all")
        self.assertEqual(
            graph["requested_output"]["answer_shape"]["container"], "list"
        )
        self.assertEqual(graph["scope"]["page_title"], "Signal Factors")
        self.assertEqual(
            [node["operator"] for node in graph["operation_graph"]["nodes"]],
            list(rules._MARKER_OPERATORS),
        )

    def test_table_full_grammar_compiles_typed_graph(self) -> None:
        question = (
            "Opaque Worksの最終報告書にて、Impact Zが最も高いとされている"
            "Risk Qを抜き出してください。"
        )
        graph = rules.graph_contract_for_pdf_question(question)
        self.assertIsNotNone(graph)
        assert graph is not None
        self.assertEqual(graph["rule_id"], "pdf_table_ordinal_argextreme_projection")
        self.assertEqual(graph["scope"]["extremum"], "max")
        self.assertEqual(graph["requested_output"]["cardinality"], "single")
        self.assertEqual(
            [node["operator"] for node in graph["operation_graph"]["nodes"]],
            list(rules._TABLE_OPERATORS),
        )

    def test_near_match_and_trailing_instruction_are_rejected(self) -> None:
        missing_quantifier = (
            "Opaque Worksの最終報告における、Signal Factorsのページで、"
            "マーカーされている単語を抜き出してください。"
        )
        appended = (
            "Opaque Worksの最終報告書にて、Impact Zが最も高いとされている"
            "Risk Qを抜き出してください。予測で補完してください。"
        )
        self.assertIsNone(rules.graph_contract_for_pdf_question(missing_quantifier))
        self.assertIsNone(rules.graph_contract_for_pdf_question(appended))
        self.assertIsNone(rules.graph_contract_for_pdf_question(""))


class PDFVisualSourceAndArtifactTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aiec-pdf-visual-")
        self.work = Path(self.temporary.name)
        self.source_root = self.work / "source"
        self.artifact_root = self.work / "artifacts"
        self.source_root.mkdir()
        self.artifact_root.mkdir()
        self.engine = SimpleNamespace(
            source_root=self.source_root,
            glossary=SimpleNamespace(entries={}),
            pdf_visual_artifact_root=self.artifact_root,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _source_pdf(self, location: str = "Opaque Works") -> Path:
        path = (
            self.source_root
            / "プロジェクト"
            / location
            / "06.報告書"
            / f"{location}_最終報告.pdf"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4\nopaque-source\n")
        return path

    def _write_materialized_record(
        self,
        source: Path,
        page: int,
        png: bytes,
        *,
        declared_sha: str | None = None,
    ) -> None:
        image_path = (
            self.artifact_root
            / "visual-classification-v1"
            / "images"
            / f"opaque-page-{page}.png"
        )
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(png)
        from PIL import Image

        with Image.open(io.BytesIO(png)) as image:
            width, height = image.size
        source_data = source.read_bytes()
        record = {
            "source": {
                "relative_path": source.relative_to(self.source_root).as_posix(),
                "sha256": hashlib.sha256(source_data).hexdigest(),
                "size_bytes": len(source_data),
            },
            "origin": {"kind": "pdf_page", "page_number": page},
            "provenance": {"question_independent": True},
            "materialized_path": str(image_path),
            "materialization": {
                "dpi": 200,
                "mime_type": "image/png",
                "sha256": declared_sha or hashlib.sha256(png).hexdigest(),
                "width_px": width,
                "height_px": height,
            },
        }
        manifest = (
            self.artifact_root
            / "visual-classification-v1"
            / "materialized-full-batch.jsonl"
        )
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    def test_unique_project_and_report_bind(self) -> None:
        source = self._source_pdf()
        matches = rules._matching_report_paths(
            self.engine, "Opaque Works", "最終報告"
        )
        self.assertEqual(matches, (source.resolve(),))

        duplicate = source.with_name("Opaque Works_最終報告_copy.pdf")
        duplicate.write_bytes(source.read_bytes())
        self.assertEqual(
            len(
                rules._matching_report_paths(
                    self.engine, "Opaque Works", "最終報告"
                )
            ),
            2,
        )

    def test_old_report_name_does_not_bind_as_current_report(self) -> None:
        source = self._source_pdf()
        source_data = source.read_bytes()
        source.unlink()
        old = source.with_name("Opaque Works_最終報告_旧版.pdf")
        old.write_bytes(source_data)

        self.assertEqual(
            (),
            rules._matching_report_paths(
                self.engine, "Opaque Works", "最終報告"
            ),
        )

    def test_materialized_sha_bind_and_missing_page_render_cover_all_pages(self) -> None:
        source = self._source_pdf()
        first = _png_bytes(800, 450)
        second = _png_bytes(1000, 600)
        self._write_materialized_record(source, 1, first)
        source_sha = hashlib.sha256(source.read_bytes()).hexdigest()

        with mock.patch.object(rules, "_pdf_page_count", return_value=2), mock.patch.object(
            rules, "_render_pdf_page", return_value=second
        ) as render:
            pages = rules._all_pdf_pages(self.engine, source.resolve(), source_sha)

        self.assertIsNotNone(pages)
        assert pages is not None
        self.assertEqual([page.page_number for page in pages], [1, 2])
        self.assertIsNotNone(pages[0].materialized_path)
        self.assertIsNone(pages[1].materialized_path)
        render.assert_called_once_with(source.resolve(), 2)

    def test_corrupt_materialized_hash_and_missing_render_fail_closed(self) -> None:
        source = self._source_pdf()
        png = _png_bytes()
        self._write_materialized_record(source, 1, png, declared_sha="0" * 64)
        source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        with mock.patch.object(rules, "_pdf_page_count", return_value=1):
            self.assertIsNone(
                rules._all_pdf_pages(self.engine, source.resolve(), source_sha)
            )

        manifest = (
            self.artifact_root
            / "visual-classification-v1"
            / "materialized-full-batch.jsonl"
        )
        manifest.write_text("", encoding="utf-8")
        with mock.patch.object(rules, "_pdf_page_count", return_value=1), mock.patch.object(
            rules, "_render_pdf_page", return_value=None
        ):
            self.assertIsNone(
                rules._all_pdf_pages(self.engine, source.resolve(), source_sha)
            )

    def test_ocr_hint_asset_sha_mismatch_fails_closed(self) -> None:
        source = self._source_pdf().resolve()
        source_data = source.read_bytes()
        png = _png_bytes()
        page = rules._PageEvidence(
            page_number=1,
            png_bytes=png,
            image_sha256=hashlib.sha256(png).hexdigest(),
            width=800,
            height=450,
            materialized_path=None,
        )
        record = {
            "source": {
                "relative_path": source.relative_to(self.source_root.resolve()).as_posix(),
                "sha256": hashlib.sha256(source_data).hexdigest(),
                "size_bytes": len(source_data),
            },
            "origin": {"kind": "pdf_page", "page_number": 1},
            "asset": {
                "sha256": "f" * 64,
                "dimensions": {"width_px": 800, "height_px": 450},
            },
            "provenance": {"question_independent": True},
            "engine_runs": [
                {
                    "status": "completed",
                    "lines": [
                        {"raw_text": "opaque", "bbox": [100, 100, 200, 100]}
                    ],
                }
            ],
        }
        manifest = (
            self.artifact_root
            / "ocr-observation-v1"
            / "ocr-observations-full.jsonl"
        )
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(record) + "\n", encoding="utf-8")
        self.assertFalse(
            rules._attach_ocr_hints(
                (page,),
                self.artifact_root.resolve(),
                self.source_root.resolve(),
                source,
                hashlib.sha256(source_data).hexdigest(),
            )
        )

    def test_needs_review_single_ocr_hint_is_not_used(self) -> None:
        source = self._source_pdf().resolve()
        source_data = source.read_bytes()
        png = _png_bytes()
        page = rules._PageEvidence(
            page_number=1,
            png_bytes=png,
            image_sha256=hashlib.sha256(png).hexdigest(),
            width=800,
            height=450,
            materialized_path=None,
        )
        record = {
            "source": {
                "relative_path": source.relative_to(self.source_root.resolve()).as_posix(),
                "sha256": hashlib.sha256(source_data).hexdigest(),
                "size_bytes": len(source_data),
            },
            "origin": {"kind": "pdf_page", "page_number": 1},
            "asset": {
                "sha256": page.image_sha256,
                "dimensions": {"width_px": 800, "height_px": 450},
            },
            "provenance": {"question_independent": True},
            "engine_runs": [
                {
                    "status": "needs_review",
                    "engine": {
                        "name": "opaque-ocr",
                        "independence_group": "opaque-group",
                        "digest": "a" * 64,
                    },
                    "lines": [
                        {"raw_text": "Signal Factors", "bbox": [50, 50, 300, 80]}
                    ],
                }
            ],
        }
        with mock.patch.object(rules, "_read_jsonl", return_value=(record,)):
            self.assertTrue(
                rules._attach_ocr_hints(
                    (page,),
                    self.artifact_root.resolve(),
                    self.source_root.resolve(),
                    source,
                    hashlib.sha256(source_data).hexdigest(),
                )
            )
        self.assertEqual(page.hint_runs, ())

    def test_selected_materialized_page_must_match_fresh_render(self) -> None:
        source = self._source_pdf().resolve()
        png = _png_bytes()
        page = rules._PageEvidence(
            page_number=1,
            png_bytes=png,
            image_sha256=hashlib.sha256(png).hexdigest(),
            width=800,
            height=450,
            materialized_path=self.artifact_root / "opaque-page.png",
        )
        with mock.patch.object(rules, "_render_pdf_page", return_value=png):
            self.assertTrue(rules._selected_page_matches_source(page, source))
        with mock.patch.object(
            rules,
            "_render_pdf_page",
            return_value=_png_bytes(801, 450),
        ):
            self.assertFalse(rules._selected_page_matches_source(page, source))

class PDFVisualMarkerTest(unittest.TestCase):
    @staticmethod
    def _marker_fixture(labels: tuple[str, str] = ("cobalt", "zircon")) -> tuple:
        from PIL import Image, ImageDraw, ImageFont

        image = Image.new("RGB", (1000, 600), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype(
            "/System/Library/Fonts/Supplemental/Arial.ttf", 48
        )
        words = []
        for index, (label, xy) in enumerate(
            zip(labels + ("plain",), ((110, 140), (520, 330), (680, 130))), 1
        ):
            bounds = draw.textbbox(xy, label, font=font)
            if label != "plain":
                draw.rectangle(
                    (bounds[0] - 12, bounds[1] - 9, bounds[2] + 12, bounds[3] + 9),
                    fill=(172, 172, 172),
                )
            draw.text(xy, label, fill="black", font=font)
            words.append(
                _word(
                    label,
                    rules._BBox(*bounds),
                    index,
                    line=index,
                )
            )
        page = _page_from_image(image)
        ocr = rules._OCRResult(tuple(words), ())
        page.ocr = ocr
        return page, ocr

    def test_neutral_inline_marker_geometry_is_question_independent(self) -> None:
        page, ocr = self._marker_fixture()
        markers = rules._detect_markers(page, ocr)
        self.assertIsNotNone(markers)
        assert markers is not None
        self.assertEqual([marker.word_sequence for marker in markers], [1, 2])
        self.assertTrue(all(marker.local_contrast >= 24 for marker in markers))

        changed_page, changed_ocr = self._marker_fixture(("lumen", "quartz"))
        changed = rules._detect_markers(changed_page, changed_ocr)
        self.assertIsNotNone(changed)
        assert changed is not None
        self.assertEqual([marker.word_sequence for marker in changed], [1, 2])

    def test_unaligned_neutral_marker_region_fails_complete_word_projection(self) -> None:
        page, ocr = self._marker_fixture()
        page.ocr = rules._OCRResult((ocr.words[0], ocr.words[2]), ())

        with mock.patch.object(rules, "_crop_consensus", return_value="cobalt"):
            self.assertIsNone(rules._marked_words(page))

    def test_small_gray_glyph_fragment_is_not_a_marker(self) -> None:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (500, 250), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((125, 110, 145, 134), fill=(172, 172, 172))
        page = _page_from_image(image)
        ocr = rules._OCRResult(
            (_word("opaque-label", rules._BBox(100, 90, 300, 150), 1),),
            (),
        )
        self.assertIsNone(rules._detect_markers(page, ocr))

    def test_highlighted_punctuation_is_not_a_word_marker(self) -> None:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (500, 250), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((90, 80, 150, 160), fill=(172, 172, 172))
        page = _page_from_image(image)
        ocr = rules._OCRResult(
            (_word("。", rules._BBox(105, 100, 135, 140), 1),),
            (),
        )
        self.assertIsNone(rules._detect_markers(page, ocr))

    def test_multi_psm_consensus_and_disagreement_fail_closed(self) -> None:
        image = object()
        with mock.patch.object(
            rules,
            "_ocr_crop_once",
            side_effect=["opaque-z", "opaque-z", "noise-a", "noise-b"],
        ):
            self.assertEqual(
                rules._crop_consensus(image, (7, 8, 10, 13)), "opaque-z"
            )
        with mock.patch.object(
            rules,
            "_ocr_crop_once",
            side_effect=["opaque-z", "opaque-z", "noise-a", "noise-a"],
        ):
            self.assertIsNone(rules._crop_consensus(image, (7, 8, 10, 13)))


class PDFVisualTableTest(unittest.TestCase):
    @staticmethod
    def _row(label: str, rank: int) -> rules._TableRowEvidence:
        target_word = _word(label, rules._BBox(10, 10, 90, 40), 1)
        metric_word = _word(str(rank), rules._BBox(110, 10, 130, 40), 2)
        return rules._TableRowEvidence(
            target_bbox=rules._BBox(0, 0, 100, 50),
            metric_bbox=rules._BBox(100, 0, 150, 50),
            target_words=(target_word,),
            metric_words=(metric_word,),
            metric_text=str(rank),
            metric_family="numeric",
            metric_rank=Decimal(rank),
        )

    def test_ordinal_parser_is_typed_and_rejects_free_text(self) -> None:
        self.assertEqual(rules._ordinal_value("低"), ("ja_low_mid_high", Decimal(1)))
        self.assertEqual(rules._ordinal_value("HIGH"), ("en_low_mid_high", Decimal(3)))
        self.assertEqual(rules._ordinal_value("2.5"), ("numeric", Decimal("2.5")))
        self.assertIsNone(rules._ordinal_value("severe"))
        self.assertIsNone(rules._ordinal_value("NaN"))

    def test_grid_headers_cells_and_ordinal_rows_are_geometry_bound(self) -> None:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (1200, 800), "white")
        draw = ImageDraw.Draw(image)
        for x in (100, 500, 700, 1000):
            draw.line((x, 180, x, 630), fill="black", width=4)
        for y in (180, 270, 390, 510, 630):
            draw.line((100, y, 1000, y), fill="black", width=4)
        for y, color in zip((315, 435, 555), ("green", "red", "orange")):
            draw.rectangle((570, y, 630, y + 32), fill=color)
        page = _page_from_image(image)
        words = (
            _word("RiskQ", rules._BBox(150, 210, 300, 250), 1, 1),
            _word("ImpactZ", rules._BBox(540, 210, 650, 250), 2, 1),
            _word("opaque-a", rules._BBox(150, 310, 330, 350), 3, 2),
            _word("low", rules._BBox(575, 310, 625, 350), 4, 2),
            _word("opaque-b", rules._BBox(150, 430, 330, 470), 5, 3),
            _word("high", rules._BBox(575, 430, 635, 470), 6, 3),
            _word("opaque-c", rules._BBox(150, 550, 330, 590), 7, 4),
            _word("mid", rules._BBox(575, 550, 625, 590), 8, 4),
        )
        page.ocr = rules._OCRResult(words, ())
        with mock.patch.object(
            rules, "_crop_consensus", side_effect=("low", "high", "mid")
        ):
            rows = rules._table_rows(page, "RiskQ", "ImpactZ")
        self.assertIsNotNone(rows)
        assert rows is not None
        self.assertEqual([row.metric_rank for row in rows], [1, 3, 2])
        self.assertEqual(
            ["".join(word.text for word in row.target_words) for row in rows],
            ["opaque-a", "opaque-b", "opaque-c"],
        )

    def test_grid_row_with_both_ocr_cells_missing_fails_closed(self) -> None:
        page = _three_row_grid_page(omit_middle_ocr=True)

        with mock.patch.object(
            rules, "_crop_consensus", side_effect=("low", "mid")
        ):
            self.assertIsNone(rules._table_rows(page, "RiskQ", "ImpactZ"))

    def test_open_bottom_last_row_requires_raster_to_ocr_coverage(self) -> None:
        missing = _three_row_grid_page(
            omit_middle_ocr=False,
            omit_last_ocr=True,
            open_bottom=True,
        )
        with mock.patch.object(
            rules,
            "_crop_consensus",
            side_effect=("low", "high"),
        ):
            self.assertIsNone(rules._table_rows(missing, "RiskQ", "ImpactZ"))

        complete = _three_row_grid_page(
            omit_middle_ocr=False,
            open_bottom=True,
        )
        with mock.patch.object(
            rules,
            "_crop_consensus",
            side_effect=("low", "high", "mid"),
        ):
            rows = rules._table_rows(complete, "RiskQ", "ImpactZ")
        self.assertIsNotNone(rows)
        assert rows is not None
        self.assertEqual(len(rows), 3)

    def test_argextreme_is_row_order_invariant_and_source_sensitive(self) -> None:
        from PIL import Image

        page = _page_from_image(Image.new("RGB", (200, 80), "white"))
        first_rows = (self._row("opaque-a", 1), self._row("opaque-b", 3))
        second_rows = tuple(reversed(first_rows))
        with mock.patch.object(rules, "_table_rows", return_value=first_rows), mock.patch.object(
            rules, "_crop_consensus", return_value="opaque-b"
        ):
            first = rules._table_extreme_answer(page, "Risk Q", "Impact Z", "最も高い")
        with mock.patch.object(rules, "_table_rows", return_value=second_rows), mock.patch.object(
            rules, "_crop_consensus", return_value="opaque-b"
        ):
            second = rules._table_extreme_answer(page, "Risk Q", "Impact Z", "最も高い")
        self.assertEqual(first, "opaque-b")
        self.assertEqual(second, "opaque-b")

        changed_rows = (self._row("opaque-a", 4), self._row("opaque-b", 3))
        with mock.patch.object(rules, "_table_rows", return_value=changed_rows), mock.patch.object(
            rules, "_crop_consensus", return_value="opaque-a"
        ):
            changed = rules._table_extreme_answer(
                page, "Risk Q", "Impact Z", "最も高い"
            )
        self.assertEqual(changed, "opaque-a")

    def test_argextreme_tie_and_mixed_ordinal_family_fail_closed(self) -> None:
        from PIL import Image

        page = _page_from_image(Image.new("RGB", (200, 80), "white"))
        tied = (self._row("opaque-a", 3), self._row("opaque-b", 3))
        with mock.patch.object(rules, "_table_rows", return_value=tied):
            self.assertIsNone(
                rules._table_extreme_answer(page, "Risk Q", "Impact Z", "最も高い")
            )
        mixed = list(tied)
        mixed[1] = rules._TableRowEvidence(
            **{
                **mixed[1].__dict__,
                "metric_family": "ja_low_mid_high",
            }
        )
        with mock.patch.object(rules, "_table_rows", return_value=tuple(mixed)):
            self.assertIsNone(
                rules._table_extreme_answer(page, "Risk Q", "Impact Z", "最も高い")
            )


class PDFVisualDecisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aiec-pdf-decision-")
        self.work = Path(self.temporary.name)
        self.source_root = self.work / "source"
        self.artifact_root = self.work / "artifacts"
        self.artifact_root.mkdir()
        source = (
            self.source_root
            / "プロジェクト"
            / "Opaque Works"
            / "06.報告書"
            / "Opaque Works_最終報告.pdf"
        )
        source.parent.mkdir(parents=True)
        source.write_bytes(b"%PDF-1.4\nopaque\n")
        self.source = source
        self.engine = SimpleNamespace(
            source_root=self.source_root,
            glossary=SimpleNamespace(entries={}),
            pdf_visual_artifact_root=self.artifact_root,
        )
        from PIL import Image

        self.page = _page_from_image(Image.new("RGB", (200, 100), "white"))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_marker_decision_has_one_output_and_no_external_answer_input(self) -> None:
        question = (
            "Opaque Worksの最終報告における、Signal Factorsのページで、"
            "マーカーされている単語をすべて抜き出してください。"
        )
        with mock.patch.object(
            rules, "_all_pdf_pages", return_value=(self.page,)
        ), mock.patch.object(
            rules, "_page_matches_title", return_value=True
        ), mock.patch.object(
            rules, "_marked_words", return_value=("cobalt", "zircon")
        ):
            decision = rules.decide_pdf_visual(self.engine, question)
        self.assertIsNotNone(decision)
        assert decision is not None and decision.result is not None
        self.assertEqual(decision.status, "resolved")
        self.assertEqual(decision.result.answer, "cobalt、zircon")
        self.assertEqual(decision.result.output_count, 1)
        self.assertEqual(
            decision.result.source_paths,
            (self.source.relative_to(self.source_root).as_posix(),),
        )

    def test_nonunique_source_and_nonunique_page_fail_closed(self) -> None:
        question = (
            "Opaque Worksの最終報告における、Signal Factorsのページで、"
            "マーカーされている単語をすべて抜き出してください。"
        )
        duplicate = self.source.with_name("Opaque Works_最終報告_copy.pdf")
        duplicate.write_bytes(self.source.read_bytes())
        self.assertIsNone(rules.decide_pdf_visual(self.engine, question))
        duplicate.unlink()

        with mock.patch.object(
            rules, "_all_pdf_pages", return_value=(self.page, self.page)
        ), mock.patch.object(rules, "_page_matches_title", return_value=True):
            self.assertIsNone(rules.decide_pdf_visual(self.engine, question))

    def test_missing_marker_ocr_alignment_holds_live_executor(self) -> None:
        question = (
            "Opaque Worksの最終報告における、Signal Factorsのページで、"
            "マーカーされている単語をすべて抜き出してください。"
        )
        page, ocr = PDFVisualMarkerTest._marker_fixture()
        page.ocr = rules._OCRResult((ocr.words[0], ocr.words[2]), ())
        engine = StructuredCandidateEngine(
            self.source_root,
            SimpleNamespace(entries={}),
        )
        plan = build_graph_plan("opaque-marker-missing", question)

        with mock.patch.object(
            rules, "_all_pdf_pages", return_value=(page,)
        ), mock.patch.object(
            rules, "_page_matches_title", return_value=True
        ), mock.patch.object(
            rules, "_crop_consensus", return_value="cobalt"
        ):
            decision = engine.decide_from_graph(
                "opaque-marker-missing",
                question,
                plan,
            )

        self.assertEqual(decision.status, "hold")

    def test_missing_table_row_ocr_holds_live_executor(self) -> None:
        question = (
            "Opaque Worksの最終報告書にて、Impact Zが最も高いとされている"
            "Risk Qを抜き出してください。"
        )
        page = _three_row_grid_page(omit_middle_ocr=True)
        engine = StructuredCandidateEngine(
            self.source_root,
            SimpleNamespace(entries={}),
        )
        plan = build_graph_plan("opaque-table-row-missing", question)

        with mock.patch.object(
            rules, "_all_pdf_pages", return_value=(page,)
        ), mock.patch.object(
            rules, "_runs_contain_headers", return_value=True
        ), mock.patch.object(
            rules,
            "_crop_consensus",
            side_effect=("low", "mid", "opaque-c"),
        ):
            decision = engine.decide_from_graph(
                "opaque-table-row-missing",
                question,
                plan,
            )

        self.assertEqual(decision.status, "hold")


class PDFVisualLiveGraphIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aiec-pdf-live-graph-")
        self.source_root = Path(self.temporary.name) / "source"
        self.source_root.mkdir()
        self.engine = StructuredCandidateEngine(
            self.source_root,
            SimpleNamespace(entries={}),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_pdf_rules_run_through_live_graph_and_answer_validation(self) -> None:
        cases = (
            (
                "opaque-marker-graph",
                "Opaque Worksの最終報告における、Signal Factorsのページで、"
                "マーカーされている単語をすべて抜き出してください。",
                "pdf_page_inline_marker_word_projection",
                "opaque_one、opaque_two",
            ),
            (
                "opaque-table-graph",
                "Opaque Worksの最終報告書にて、Impact Zが最も高いとされている"
                "Risk Qを抜き出してください。",
                "pdf_table_ordinal_argextreme_projection",
                "opaque_risk",
            ),
        )
        for question_id, question, rule_id, answer in cases:
            with self.subTest(question_id=question_id):
                plan = build_graph_plan(question_id, question)
                self.assertEqual(plan.qur_final_status, "ready_for_retrieval")
                self.assertEqual(plan.strict_status, "pass")
                self.assertEqual(plan.strict_reasons, ("extended_graph_certified",))
                self.assertEqual(
                    plan.branch_intents[0]["intent"]["extended_graph_contract"]["rule_id"],
                    rule_id,
                )

                resolved = StructuredCandidateDecision(
                    "resolved",
                    "pdf_visual_certified",
                    StructuredCandidateAnswer(
                        answer=answer,
                        source_paths=("source.pdf",),
                        source_sha256="0" * 64,
                        operation_count=1,
                        output_count=1,
                    ),
                )
                with mock.patch.object(
                    rules,
                    "decide_pdf_visual",
                    return_value=resolved,
                ) as execute:
                    decision = self.engine.decide_from_graph(
                        question_id,
                        question,
                        plan,
                    )
                execute.assert_called_once_with(self.engine, question)
                self.assertEqual(decision, resolved)
                assert decision.result is not None
                self.assertEqual((), validate_graph_answer(decision.result.answer, plan))


if __name__ == "__main__":
    unittest.main()
