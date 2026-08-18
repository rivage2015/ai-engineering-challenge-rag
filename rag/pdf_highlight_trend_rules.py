"""Fail-closed trend calculation from red text on yellow PDF highlights."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping

from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision

VERSION = "0.1"
HIGHLIGHT_TREND = re.compile(
    r"^(?P<location>AYM)の(?P<container>MM)において、"
    r"(?P<fill>黄色)ハイライトかつ(?P<font>RED)になっている数値を対象に、"
    r"最初のMMから最後のMMまでの上昇率を計算してください。"
    r"上昇率は （最後の値 - 最初の値） / 最初の値 × 100 で求め、"
    r"小数第(?P<digits>2)位まで答えてください。$"
)
_MEETING_ID = re.compile(r"会議\s*ID\s*[:：]\s*(M[0-9]{2,4})", re.IGNORECASE)
_DECIMAL = re.compile(r"^[+-]?(?:[0-9]+\.[0-9]+|[0-9]+)$")
_MAX_PDF_BYTES = 64 * 1024 * 1024
_MAX_XML_BYTES = 32 * 1024 * 1024
_TIMEOUT = 45


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if not isinstance(question, str):
        return None
    match = HIGHLIGHT_TREND.fullmatch(question)
    if match is None:
        return None
    operators = (
        "bind_all_meeting_minutes",
        "verify_complete_meeting_id_sequence",
        "extract_native_text_font_colors",
        "select_red_numeric_text",
        "render_candidate_pages",
        "verify_yellow_background_pixels",
        "order_matching_meetings",
        "select_first_and_last",
        "calculate_growth_rate",
        "round_half_up_two_decimals",
        "project_percent",
    )
    nodes = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
        previous = output
    bindings = {key: match[key] for key in ("location", "container", "fill", "font", "digits")}
    core = {
        "pdf_highlight_trend_version": VERSION,
        "rule_id": "pdf_red_on_yellow_growth_rate",
        "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
        "bindings": bindings,
        "scope": {"source_channel": "native_pdf_font_color_plus_rendered_fill_pixels", "question_independent": True, "ambiguity_policy": "hold"},
        "operation_graph": {
            "external_inputs": [{"input_ref": "input_question", "input_type": "pdf_document_set", "source": "question_scope"}],
            "nodes": nodes,
            "edges": [{"from": nodes[index - 1]["output_ref"], "to": nodes[index]["operation_id"]} for index in range(1, len(nodes))],
        },
        "requested_output": {
            "source_operation_ref": nodes[-1]["operation_id"],
            "cardinality": "single",
            "answer_shape": {"container": "scalar", "value_type": "number", "unit": "%"},
            "display_precision": {"mode": "decimal_places", "digits": 2},
            "required_keys": None,
        },
    }
    return {"graph_contract_id": "pdf_highlight_trend_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and isinstance(contract, Mapping) and _canonical(expected) == _canonical(contract)


def _compact(value: object) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value))).casefold()


def _sources(engine: Any, location: str) -> tuple[Path, tuple[Path, ...]] | None:
    try:
        from structured_candidate import _candidate_values, _location_matches
        root = Path(engine.source_root).resolve()
        if not root.is_dir() or root.is_symlink():
            return None
        candidates = _candidate_values(location, getattr(engine, "glossary", None))
        matches = []
        for path in root.rglob("*.pdf"):
            if path.is_symlink() or not path.is_file() or path.name.startswith("~$"):
                continue
            relative = path.relative_to(root)
            normalized_parts = tuple(_compact(part) for part in relative.parts)
            if "05.会議" not in tuple(unicodedata.normalize("NFC", part) for part in relative.parts) or "会議録" not in normalized_parts:
                continue
            if not _location_matches(relative.parts[:-1], candidates):
                continue
            matches.append(path)
        ordered = tuple(sorted(matches, key=lambda path: unicodedata.normalize("NFC", path.relative_to(root).as_posix())))
        return (root, ordered) if len(ordered) >= 2 else None
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return None


def _pdf_xml(path: Path) -> ET.Element | None:
    executable = shutil.which("pdftohtml")
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, "-xml", "-stdout", "-hidden", "-noframes", str(path)],
            capture_output=True,
            timeout=_TIMEOUT,
            check=False,
        )
        # pdftohtml emits its own fixed pdf2xml DOCTYPE. Text originating in
        # the PDF is escaped by the converter; entity declarations remain
        # forbidden before parsing.
        if completed.returncode != 0 or not 0 < len(completed.stdout) <= _MAX_XML_BYTES or b"<!ENTITY" in completed.stdout:
            return None
        return ET.fromstring(completed.stdout)
    except (OSError, subprocess.SubprocessError, ET.ParseError):
        return None


def _font_is_red(color: str) -> bool:
    match = re.fullmatch(r"#([0-9a-fA-F]{2})([0-9a-fA-F]{2})([0-9a-fA-F]{2})", color)
    if match is None:
        return False
    red, green, blue = (int(value, 16) for value in match.groups())
    return red >= 220 and green <= 80 and blue <= 80


def _document_evidence(root: ET.Element) -> tuple[int, tuple[tuple[int, Decimal, tuple[int, int, int, int], int, int], ...]] | None:
    pages = root.findall("page")
    if not pages:
        return None
    first_text = " ".join("".join(node.itertext()) for node in pages[0].findall("text"))
    meeting_ids = _MEETING_ID.findall(unicodedata.normalize("NFKC", first_text))
    if len(set(meeting_ids)) != 1:
        return None
    meeting_number = int(meeting_ids[0][1:])
    candidates = []
    for page_index, page in enumerate(pages, 1):
        try:
            page_width, page_height = int(page.attrib["width"]), int(page.attrib["height"])
        except (KeyError, ValueError):
            return None
        fonts = {node.attrib.get("id"): node.attrib.get("color", "") for node in page.findall("fontspec")}
        for node in page.findall("text"):
            text = unicodedata.normalize("NFKC", "".join(node.itertext())).strip()
            if not _DECIMAL.fullmatch(text) or not _font_is_red(fonts.get(node.attrib.get("font"), "")):
                continue
            try:
                left, top, width, height = (int(node.attrib[key]) for key in ("left", "top", "width", "height"))
                value = Decimal(text)
            except (KeyError, ValueError):
                return None
            candidates.append((page_index, value, (left, top, left + width, top + height), page_width, page_height))
    return meeting_number, tuple(candidates)


def _render(path: Path, page_number: int, output_prefix: Path) -> Path | None:
    executable = shutil.which("pdftoppm")
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, "-f", str(page_number), "-l", str(page_number), "-r", "108", "-singlefile", "-png", str(path), str(output_prefix)],
            capture_output=True,
            timeout=_TIMEOUT,
            check=False,
        )
        output = output_prefix.with_suffix(".png")
        return output if completed.returncode == 0 and output.is_file() and 0 < output.stat().st_size <= 32 * 1024 * 1024 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _yellow_background(image_path: Path, bbox: tuple[int, int, int, int], expected_width: int, expected_height: int) -> bool:
    try:
        from PIL import Image
        with Image.open(image_path) as image:
            image.load()
            if abs(image.width - expected_width) > 1 or abs(image.height - expected_height) > 1:
                return False
            left, top, right, bottom = bbox
            left, top = max(0, left - 4), max(0, top - 3)
            right, bottom = min(image.width, right + 4), min(image.height, bottom + 3)
            pixels = list(image.convert("RGB").crop((left, top, right, bottom)).getdata())
        if not pixels:
            return False
        yellow = sum(red >= 230 and green >= 220 and blue <= 100 for red, green, blue in pixels)
        return yellow / len(pixels) >= 0.35
    except (OSError, ValueError):
        return False


def _matching_values(paths: tuple[Path, ...]) -> tuple[tuple[int, Decimal], ...] | None:
    records = []
    seen_ids = set()
    with tempfile.TemporaryDirectory(prefix="pdf-highlight-trend-") as temporary:
        work = Path(temporary)
        for source_index, path in enumerate(paths, 1):
            root = _pdf_xml(path)
            if root is None:
                return None
            evidence = _document_evidence(root)
            if evidence is None or evidence[0] in seen_ids:
                return None
            meeting_number, candidates = evidence
            seen_ids.add(meeting_number)
            matched = []
            rendered: dict[int, Path] = {}
            for page_number, value, bbox, width, height in candidates:
                image = rendered.get(page_number)
                if image is None:
                    image = _render(path, page_number, work / f"source-{source_index:03d}-page-{page_number:03d}")
                    if image is None:
                        return None
                    rendered[page_number] = image
                if _yellow_background(image, bbox, width, height):
                    matched.append(value)
            if len(matched) > 1:
                return None
            if matched:
                records.append((meeting_number, matched[0]))
    records.sort()
    return tuple(records) if len(records) >= 2 else None


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    bound = _sources(engine, contract["bindings"]["location"])
    if bound is None:
        return StructuredCandidateDecision("hold", "pdf_highlight_trend_sources_not_complete")
    root, paths = bound
    try:
        source_records = []
        for path in paths:
            data = path.read_bytes()
            if not 0 < len(data) <= _MAX_PDF_BYTES:
                raise ValueError("resource")
            source_records.append({"path": unicodedata.normalize("NFC", path.relative_to(root).as_posix()), "sha256": hashlib.sha256(data).hexdigest()})
        values = _matching_values(paths)
        if values is None:
            raise ValueError("style evidence")
        first, last = values[0][1], values[-1][1]
        if first == 0:
            raise ValueError("zero baseline")
        rate = ((last - first) / first * Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        answer = f"{rate:.2f}%"
        digest = hashlib.sha256(_canonical(source_records).encode()).hexdigest()
        result = StructuredCandidateAnswer(answer, tuple(record["path"] for record in source_records), digest, len(contract["operation_graph"]["nodes"]), 1)
        return StructuredCandidateDecision("resolved", "certified_pdf_highlight_trend", result)
    except (OSError, RuntimeError, TypeError, ValueError):
        return StructuredCandidateDecision("hold", "pdf_highlight_trend_not_certified")


__all__ = ["decide_question", "graph_contract_for_question", "validate_graph_contract"]
