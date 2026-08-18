from __future__ import annotations

import copy
import math
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[1]
RAG = ROOT / "rag"
if str(RAG) not in sys.path:
    sys.path.insert(0, str(RAG))

import xlsx_histogram_rules as rules  # noqa: E402
from answer import validate_graph_answer  # noqa: E402
from question_graph_runtime import build_graph_plan  # noqa: E402
from structured_candidate import StructuredCandidateEngine  # noqa: E402


S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
XDR_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
C_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"

LOCATION = "架空解析部"
CONTAINER = "observations.xlsx"
MEASURE = "signal_ratio"


def max_question(*, location: str = LOCATION, container: str = CONTAINER) -> str:
    return f"{location}の{container}において、{MEASURE}のヒストグラムで最も多いカウント数はいくつですか。"


def rank_question(rank: int, precision: int = 6) -> str:
    return (
        f"{LOCATION}の{CONTAINER}内の{MEASURE}のヒストグラムで、{rank}番目に"
        f"カウント数が多いビンの範囲を小数第{precision}位までで答えてください。"
    )


def engine_for(root: Path) -> SimpleNamespace:
    return SimpleNamespace(source_root=root.resolve(), glossary=SimpleNamespace(entries={}))


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def histogram_png(counts: tuple[int, ...]) -> bytes:
    slot = 14
    margin = 12
    width = margin * 2 + slot * len(counts)
    height = 150
    baseline = 112
    maximum = max(counts)
    pixels = [[(226, 226, 226) for _ in range(width)] for _ in range(height)]
    for index, count in enumerate(counts):
        bar_height = max(1, round(90 * count / maximum)) if count else 0
        left = margin + index * slot
        for y in range(baseline - bar_height + 1, baseline + 1):
            for x in range(left, left + slot - 1):
                pixels[y][x] = (21, 96, 130)
    raw = b"".join(
        b"\x00" + bytes(channel for pixel in row for channel in pixel)
        for row in pixels
    )
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", ihdr)
        + png_chunk(b"IDAT", zlib.compress(raw))
        + png_chunk(b"IEND", b"")
    )


def numeric_cell(reference: str, value: Decimal | int | str, *, formula: bool = False) -> str:
    marker = "<f>1+1</f>" if formula else ""
    return f'<c r="{reference}">{marker}<v>{escape(str(value))}</v></c>'


def inline_cell(reference: str, value: str) -> str:
    return f'<c r="{reference}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'


def raw_sheet(values: tuple[Decimal, ...], *, duplicate_header: bool = False, formula: bool = False) -> str:
    headers = [inline_cell("A1", MEASURE)]
    if duplicate_header:
        headers.append(inline_cell("B1", MEASURE))
    rows = [f'<row r="1">{"".join(headers)}</row>']
    for index, value in enumerate(values, 2):
        cells = [numeric_cell(f"A{index}", value, formula=formula and index == 2)]
        if duplicate_header:
            cells.append(numeric_cell(f"B{index}", value))
        rows.append(f'<row r="{index}">{"".join(cells)}</row>')
    return f'<?xml version="1.0"?><worksheet xmlns="{S_NS}"><sheetData>{"".join(rows)}</sheetData></worksheet>'


def chart_sheet(*, native_chart: bool = False) -> str:
    return (
        f'<?xml version="1.0"?><worksheet xmlns="{S_NS}" xmlns:r="{R_NS}">'
        '<sheetData/><drawing r:id="rId1"/></worksheet>'
    )


def chart_xml(counts: tuple[int, ...], *, title: str = MEASURE) -> str:
    points = "".join(
        f'<c:pt idx="{index}"><c:v>{count}</c:v></c:pt>'
        for index, count in enumerate(counts)
    )
    return f'''<?xml version="1.0"?>
<c:chartSpace xmlns:c="{C_NS}" xmlns:a="{A_NS}">
  <c:chart><c:title><c:tx><c:rich><a:p><a:r><a:t>{escape(title)}</a:t></a:r></a:p></c:rich></c:tx></c:title>
  <c:plotArea><c:barChart><c:ser><c:val><c:numRef><c:numCache>{points}</c:numCache></c:numRef></c:val></c:ser></c:barChart></c:plotArea></c:chart>
</c:chartSpace>'''


def members_for(
    values: tuple[Decimal, ...],
    *,
    picture_counts: tuple[int, ...] | None = None,
    duplicate_header: bool = False,
    formula: bool = False,
    duplicate_picture: bool = False,
    native_chart: bool = False,
) -> dict[str, bytes | str]:
    histogram = rules._histogram(values)
    counts = picture_counts or histogram.counts
    workbook = f'''<?xml version="1.0"?>
<workbook xmlns="{S_NS}" xmlns:r="{R_NS}"><sheets>
  <sheet name="Visual" sheetId="1" r:id="rId1"/>
  <sheet name="Raw" sheetId="2" r:id="rId2"/>
</sheets></workbook>'''
    workbook_rels = f'''<?xml version="1.0"?>
<Relationships xmlns="{PR_NS}">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
</Relationships>'''
    sheet_rels = f'''<?xml version="1.0"?>
<Relationships xmlns="{PR_NS}"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" Target="../drawings/drawing1.xml"/></Relationships>'''
    if native_chart:
        drawing_item = (
            f'<xdr:graphicFrame><a:graphic><a:graphicData><c:chart xmlns:c="{C_NS}" r:id="rId1"/>'
            '</a:graphicData></a:graphic></xdr:graphicFrame>'
        )
        drawing_rels = f'''<?xml version="1.0"?><Relationships xmlns="{PR_NS}">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart" Target="../charts/chart1.xml"/>
</Relationships>'''
    else:
        pictures = ['<xdr:pic><xdr:blipFill><a:blip r:embed="rId1"/></xdr:blipFill></xdr:pic>']
        if duplicate_picture:
            pictures.append('<xdr:pic><xdr:blipFill><a:blip r:embed="rId2"/></xdr:blipFill></xdr:pic>')
        drawing_item = "".join(pictures)
        second = (
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/image2.png"/>'
            if duplicate_picture
            else ""
        )
        drawing_rels = f'''<?xml version="1.0"?><Relationships xmlns="{PR_NS}">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/image1.png"/>{second}
</Relationships>'''
    drawing = f'''<?xml version="1.0"?><xdr:wsDr xmlns:xdr="{XDR_NS}" xmlns:a="{A_NS}" xmlns:r="{R_NS}">{drawing_item}</xdr:wsDr>'''
    result: dict[str, bytes | str] = {
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels": workbook_rels,
        "xl/worksheets/sheet1.xml": chart_sheet(native_chart=native_chart),
        "xl/worksheets/sheet2.xml": raw_sheet(values, duplicate_header=duplicate_header, formula=formula),
        "xl/worksheets/_rels/sheet1.xml.rels": sheet_rels,
        "xl/drawings/drawing1.xml": drawing,
        "xl/drawings/_rels/drawing1.xml.rels": drawing_rels,
    }
    if native_chart:
        result["xl/charts/chart1.xml"] = chart_xml(counts)
    else:
        result["xl/media/image1.png"] = histogram_png(counts)
        if duplicate_picture:
            result["xl/media/image2.png"] = histogram_png(counts)
    return result


def write_book(root: Path, members: dict[str, bytes | str], *, location: str = LOCATION) -> Path:
    directory = root / location
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / CONTAINER
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def synthetic_values() -> tuple[Decimal, ...]:
    return tuple(
        Decimal(i % 19) / Decimal(10)
        + Decimal((i // 19) % 4) / Decimal(100)
        for i in range(420)
    )


class XlsxHistogramRulesTests(unittest.TestCase):
    def test_full_grammar_and_contract_tamper(self) -> None:
        maximum = rules.graph_contract_for_question(max_question())
        ranked = rules.graph_contract_for_question(rank_question(3))
        self.assertIsNotNone(maximum)
        self.assertIsNotNone(ranked)
        assert maximum is not None and ranked is not None
        self.assertTrue(maximum["graph_contract_id"].startswith("xlsx_histogram_"))
        self.assertEqual(maximum["requested_output"]["answer_shape"]["value_type"], "integer")
        self.assertEqual(ranked["requested_output"]["display_precision"]["digits"], 6)
        changed = copy.deepcopy(ranked)
        changed["bindings"]["rank"] = 4
        self.assertFalse(rules.validate_graph_contract(rank_question(3), changed))
        self.assertIsNone(rules.graph_contract_for_question("histogram please"))
        self.assertIsNone(rules.graph_contract_for_question(rank_question(0)))

    def test_scott_width_is_sample_sd_rounded_to_two_significant_digits(self) -> None:
        values = synthetic_values()
        histogram = rules._histogram(values)
        floats = [float(value) for value in values]
        mean = math.fsum(floats) / len(floats)
        variance = math.fsum((value - mean) ** 2 for value in floats) / (len(floats) - 1)
        raw = Decimal(str(3.5 * math.sqrt(variance) / math.pow(len(floats), 1 / 3)))
        quantum = Decimal(1).scaleb(raw.adjusted() - 1)
        self.assertEqual(histogram.width, raw.quantize(quantum, rounding=ROUND_HALF_UP))
        self.assertEqual(sum(histogram.counts), len(values))

    def test_png_profile_and_native_chart_cache_both_verify(self) -> None:
        histogram = rules._histogram(synthetic_values())
        self.assertTrue(rules._png_profile_matches(histogram_png(histogram.counts), histogram.counts))
        self.assertFalse(rules._png_profile_matches(histogram_png(tuple(reversed(histogram.counts))), histogram.counts))
        self.assertTrue(rules._chart_cache_matches(chart_xml(histogram.counts), MEASURE, histogram.counts))
        self.assertFalse(rules._chart_cache_matches(chart_xml(histogram.counts, title="other"), MEASURE, histogram.counts))

    def test_png_dimension_and_profile_work_are_bounded(self) -> None:
        ihdr = struct.pack(">IIBBBBB", rules._MAX_PNG_DIMENSION + 1, 1, 8, 2, 0, 0, 0)
        oversized = (
            b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", ihdr)
            + png_chunk(b"IDAT", zlib.compress(b"\x00"))
            + png_chunk(b"IEND", b"")
        )
        with self.assertRaises(rules._InvalidSource):
            rules._png_rows(oversized)
        histogram = rules._histogram(synthetic_values())
        self.assertFalse(
            rules._png_profile_matches(
                histogram_png(histogram.counts),
                tuple(1 for _ in range(rules._MAX_PICTURE_BINS + 1)),
            )
        )

    def test_picture_channel_resolves_maximum_source_only(self) -> None:
        values = synthetic_values()
        expected = str(max(rules._histogram(values).counts))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_book(root, members_for(values))
            with mock.patch.object(rules, "_ocr_title", return_value=MEASURE):
                decision = rules.decide_question(engine_for(root), max_question())
        self.assertIsNotNone(decision)
        assert decision is not None and decision.result is not None
        self.assertEqual((decision.status, decision.result.answer), ("resolved", expected))

    def test_ranked_picture_question_resolves_unique_rank(self) -> None:
        values = synthetic_values()
        histogram = rules._histogram(values)
        expected = rules._answer(
            histogram,
            {"mode": "ranked_bin_range", "rank": 2, "precision": 6},
        )
        self.assertIsNotNone(expected)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_book(root, members_for(values))
            with mock.patch.object(rules, "_ocr_title", return_value=MEASURE):
                decision = rules.decide_question(engine_for(root), rank_question(2))
        self.assertIsNotNone(decision)
        assert decision is not None and decision.result is not None
        self.assertEqual((decision.status, decision.result.answer), ("resolved", expected))

    def test_translation_metamorphic_series_preserves_counts_without_fixed_answer(self) -> None:
        original = synthetic_values()
        transformed = tuple(value + Decimal("7") for value in original)
        self.assertEqual(rules._histogram(original).counts, rules._histogram(transformed).counts)
        answers: list[str] = []
        hashes: list[str] = []
        for values in (original, transformed):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                write_book(root, members_for(values))
                with mock.patch.object(rules, "_ocr_title", return_value=MEASURE):
                    decision = rules.decide_question(engine_for(root), max_question())
                self.assertIsNotNone(decision)
                assert decision is not None and decision.result is not None
                answers.append(decision.result.answer)
                hashes.append(decision.result.source_sha256)
        self.assertEqual(answers[0], answers[1])
        self.assertNotEqual(hashes[0], hashes[1])

    def test_native_chart_cache_resolves_without_ocr(self) -> None:
        values = synthetic_values()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_book(root, members_for(values, native_chart=True))
            with mock.patch.object(rules, "_ocr_title", side_effect=AssertionError("unused")):
                decision = rules.decide_question(engine_for(root), max_question())
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.status, "resolved")

    def test_ranked_interval_is_right_closed_and_exact_precision(self) -> None:
        histogram = rules._Histogram(Decimal("0.125"), Decimal("0.25"), (3, 8, 5))
        bindings = {"mode": "ranked_bin_range", "rank": 2, "precision": 6}
        self.assertEqual(rules._answer(histogram, bindings), "(0.625000, 0.875000]")
        first = rules._Histogram(Decimal("0.125"), Decimal("0.25"), (9, 4, 2))
        bindings["rank"] = 1
        self.assertEqual(rules._answer(first, bindings), "[0.125000, 0.375000]")

    def test_rank_tie_and_rounding_collapse_hold(self) -> None:
        tie = rules._Histogram(Decimal("0"), Decimal("1"), (9, 5, 5))
        self.assertIsNone(rules._answer(tie, {"mode": "ranked_bin_range", "rank": 2, "precision": 6}))
        tiny = rules._Histogram(Decimal("0"), Decimal("0.0000001"), (9, 5))
        self.assertIsNone(rules._answer(tiny, {"mode": "ranked_bin_range", "rank": 1, "precision": 6}))

    def test_mismatch_duplicate_visual_duplicate_header_and_formula_hold(self) -> None:
        values = synthetic_values()
        histogram = rules._histogram(values)
        variants = (
            members_for(values, picture_counts=tuple(reversed(histogram.counts))),
            members_for(values, duplicate_picture=True),
            members_for(values, duplicate_header=True),
            members_for(values, formula=True),
        )
        for members in variants:
            with self.subTest(member_count=len(members)):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    write_book(root, members)
                    with mock.patch.object(rules, "_ocr_title", return_value=MEASURE):
                        decision = rules.decide_question(engine_for(root), max_question())
                self.assertIsNotNone(decision)
                assert decision is not None
                self.assertEqual(decision.status, "hold")

    def test_unbound_picture_title_and_missing_ocr_hold(self) -> None:
        values = synthetic_values()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_book(root, members_for(values))
            for title in ("different_measure", None):
                with self.subTest(title=title):
                    with mock.patch.object(rules, "_ocr_title", return_value=title):
                        decision = rules.decide_question(engine_for(root), max_question())
                    self.assertIsNotNone(decision)
                    assert decision is not None
                    self.assertEqual(
                        (decision.status, decision.reason),
                        ("hold", "xlsx_histogram_visual_mismatch"),
                    )

    def test_workbook_binding_is_unique_and_archives_are_ignored(self) -> None:
        values = synthetic_values()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            members = members_for(values)
            write_book(root, members)
            write_book(root, members, location=LOCATION + " copy")
            with mock.patch.object(rules, "_ocr_title", return_value=MEASURE):
                decision = rules.decide_question(engine_for(root), max_question())
            self.assertIsNotNone(decision)
            assert decision is not None
            self.assertEqual(decision.status, "resolved")
            write_book(root, members, location=LOCATION + " extra")
            ambiguous = max_question(location=LOCATION + " extra")
            with mock.patch.object(rules, "_ocr_title", return_value=MEASURE):
                exact = rules.decide_question(engine_for(root), ambiguous)
            self.assertIsNotNone(exact)

    def test_unsafe_archive_member_holds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / LOCATION / CONTAINER
            path.parent.mkdir(parents=True)
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("../escape.xml", "bad")
            decision = rules.decide_question(engine_for(root), max_question())
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual((decision.status, decision.reason), ("hold", "xlsx_histogram_source_invalid"))

    def test_live_graph_plan_and_contract_mismatch(self) -> None:
        values = synthetic_values()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_book(root, members_for(values))
            engine = StructuredCandidateEngine(root, SimpleNamespace(entries={}))
            question = max_question()
            plan = build_graph_plan("opaque-hist", question, fast_advisory=True)
            with mock.patch.object(rules, "_ocr_title", return_value=MEASURE):
                decision = engine.decide_from_graph("opaque-hist", question, plan)
            self.assertIsNotNone(decision)
            assert decision is not None and decision.result is not None
            self.assertEqual(decision.status, "resolved")
            self.assertEqual(validate_graph_answer(decision.result.answer, plan), ())
            tampered = copy.deepcopy(plan.branch_intents[0])
            tampered["intent"]["extended_graph_contract"]["bindings"]["measure"] = "other"
            bad_plan = SimpleNamespace(
                original_question=question,
                strict_status="pass",
                branch_intents=(tampered,),
            )
            rejected = rules.decide_from_graph(engine_for(root), question, bad_plan)
            self.assertIsNotNone(rejected)
            assert rejected is not None
            self.assertEqual(rejected.reason, "xlsx_histogram_graph_plan_contract_mismatch")


if __name__ == "__main__":
    unittest.main()
