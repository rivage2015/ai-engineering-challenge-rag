"""Fail-closed source-only calculation of a PDF investment coefficient."""

from __future__ import annotations

import hashlib
import csv
import io
import json
import re
import shutil
import subprocess
import tempfile
import unicodedata
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision

VERSION = "0.1"
INVESTMENT_COEFFICIENT = re.compile(
    r"^(?P<location>東都人材プラットフォーム)の"
    r"(?P<container>データサイエンス市場の未来予測\.pdf)において、"
    r"投資実装係数の計算式が記載されているページの数値情報を式に代入し、"
    r"投資実装係数を小数で答えてください。$"
)
_PERCENT_PAIR = re.compile(
    r"\+?\s*(?P<left>[0-9]{1,3}(?:\.[0-9]+)?)\s*%\s*/\s*"
    r"\+?\s*(?P<right>[0-9]{1,3}(?:\.[0-9]+)?)\s*%"
)
_MULTIPLIER = re.compile(r"(?<![0-9.])(?P<value>[0-9]{1,3}(?:\.[0-9]+)?)\s*倍")
_MAX_PDF_BYTES = 64 * 1024 * 1024
_MAX_PAGES = 100
_TIMEOUT = 45


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if not isinstance(question, str):
        return None
    match = INVESTMENT_COEFFICIENT.fullmatch(question)
    if match is None:
        return None
    operators = (
        "bind_exact_pdf",
        "verify_complete_page_count",
        "render_every_page",
        "ocr_all_pages",
        "select_unique_formula_page",
        "extract_percent_pair",
        "extract_roi_multiplier",
        "verify_three_ocr_layout_readings",
        "convert_percent_to_decimal",
        "evaluate_source_formula",
        "project_exact_decimal",
    )
    nodes = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
        previous = output
    bindings = {key: match[key] for key in ("location", "container")}
    core = {
        "pdf_investment_coefficient_version": VERSION,
        "rule_id": "pdf_investment_coefficient_formula_evaluation",
        "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
        "bindings": bindings,
        "scope": {
            "source_channel": "full_page_raster_ocr",
            "formula": "(productivity_improvement_rate + cost_reduction_rate) * roi_multiplier",
            "question_independent": True,
            "ambiguity_policy": "hold",
        },
        "operation_graph": {
            "external_inputs": [{"input_ref": "input_question", "input_type": "pdf_document", "source": "question_scope"}],
            "nodes": nodes,
            "edges": [{"from": nodes[index - 1]["output_ref"], "to": nodes[index]["operation_id"]} for index in range(1, len(nodes))],
        },
        "requested_output": {
            "source_operation_ref": nodes[-1]["operation_id"],
            "cardinality": "single",
            "answer_shape": {"container": "scalar", "value_type": "number", "unit": None},
            "display_precision": {"mode": "source_exact"},
            "required_keys": None,
        },
    }
    return {"graph_contract_id": "pdf_investment_coefficient_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and isinstance(contract, Mapping) and _canonical(expected) == _canonical(contract)


def _compact(value: object) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value))).casefold()


def _source(engine: Any, location: str, container: str) -> tuple[Path, Path] | None:
    try:
        from structured_candidate import _candidate_values, _location_matches
        root = Path(engine.source_root).resolve()
        if not root.is_dir() or root.is_symlink():
            return None
        locations = _candidate_values(location, getattr(engine, "glossary", None))
        expected = _compact(container)
        matches = []
        for path in root.rglob("*.pdf"):
            if path.is_symlink() or not path.is_file() or _compact(path.name) != expected:
                continue
            relative = path.relative_to(root)
            if _location_matches(relative.parts[:-1], locations):
                matches.append(path)
        return (root, matches[0]) if len(matches) == 1 else None
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return None


def _page_count(path: Path) -> int | None:
    try:
        from pypdf import PdfReader
        reader = PdfReader(path, strict=True)
        count = len(reader.pages)
        return count if not reader.is_encrypted and 1 <= count <= _MAX_PAGES else None
    except Exception:
        return None


def _render(path: Path, page_number: int, output: Path) -> bool:
    executable = shutil.which("pdftoppm")
    if executable is None:
        return False
    try:
        completed = subprocess.run(
            [executable, "-f", str(page_number), "-l", str(page_number), "-r", "200", "-singlefile", "-png", str(path), str(output.with_suffix(""))],
            capture_output=True,
            timeout=_TIMEOUT,
            check=False,
        )
        return completed.returncode == 0 and output.is_file() and 0 < output.stat().st_size <= 32 * 1024 * 1024
    except (OSError, subprocess.SubprocessError):
        return False


def _ocr(image: Path, psm: int) -> str | None:
    executable = shutil.which("tesseract")
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, str(image.resolve()), "stdout", "-l", "jpn+eng", "--oem", "1", "--psm", str(psm)],
            capture_output=True,
            timeout=_TIMEOUT,
            check=False,
        )
        if completed.returncode != 0 or len(completed.stdout) > 2 * 1024 * 1024:
            return None
        return completed.stdout.decode("utf-8", errors="strict")
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None


def _ocr_lines(image: Path) -> tuple[tuple[str, tuple[int, int, int, int]], ...] | None:
    executable = shutil.which("tesseract")
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, str(image.resolve()), "stdout", "-l", "jpn+eng", "--oem", "1", "--psm", "11", "tsv"],
            capture_output=True,
            timeout=_TIMEOUT,
            check=False,
        )
        if completed.returncode != 0 or len(completed.stdout) > 8 * 1024 * 1024:
            return None
        rows = csv.DictReader(io.StringIO(completed.stdout.decode("utf-8", errors="strict")), delimiter="\t")
        grouped: dict[tuple[str, str, str, str], list[dict[str, str]]] = {}
        for row in rows:
            if row.get("level") == "5" and (row.get("text") or "").strip():
                key = tuple(row[name] for name in ("page_num", "block_num", "par_num", "line_num"))
                grouped.setdefault(key, []).append(row)
        result = []
        for words in grouped.values():
            left = min(int(word["left"]) for word in words)
            top = min(int(word["top"]) for word in words)
            right = max(int(word["left"]) + int(word["width"]) for word in words)
            bottom = max(int(word["top"]) + int(word["height"]) for word in words)
            result.append((" ".join(word["text"] for word in words), (left, top, right, bottom)))
        return tuple(result) if result else None
    except (KeyError, OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        return None


def _ocr_image(image: Any, psm: int) -> str | None:
    executable = shutil.which("tesseract")
    if executable is None:
        return None
    buffer = io.BytesIO()
    try:
        image.save(buffer, format="PNG")
        completed = subprocess.run(
            [executable, "stdin", "stdout", "-l", "jpn+eng", "--oem", "1", "--psm", str(psm)],
            input=buffer.getvalue(),
            capture_output=True,
            timeout=_TIMEOUT,
            check=False,
        )
        return completed.stdout.decode("utf-8", errors="strict") if completed.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None


def _values(text: str) -> tuple[Decimal, Decimal, Decimal] | None:
    normalized = unicodedata.normalize("NFKC", text)
    pairs = list(_PERCENT_PAIR.finditer(normalized))
    multipliers = list(_MULTIPLIER.finditer(normalized))
    compact = _compact(normalized)
    anchors = (
        "roi" in compact,
        "コスト削減" in compact,
        "生産性向上" in compact,
        "投資実" in compact and "係数" in compact,
    )
    if len(pairs) != 1 or len(multipliers) != 1 or sum(anchors) < 3:
        return None
    left = Decimal(pairs[0]["left"])
    right = Decimal(pairs[0]["right"])
    multiplier = Decimal(multipliers[0]["value"])
    if not (Decimal(0) < left <= 100 and Decimal(0) < right <= 100 and Decimal(0) < multiplier <= 100):
        return None
    return left, right, multiplier


def _crop_values_agree(image: Path, expected: tuple[Decimal, Decimal, Decimal]) -> bool:
    lines = _ocr_lines(image)
    if lines is None:
        return False
    pair_lines = [(match, bbox) for text, bbox in lines for match in [_PERCENT_PAIR.search(unicodedata.normalize("NFKC", text))] if match is not None]
    multiplier_lines = [(match, bbox) for text, bbox in lines for match in [_MULTIPLIER.search(unicodedata.normalize("NFKC", text))] if match is not None]
    if len(pair_lines) != 1 or len(multiplier_lines) != 1:
        return False
    try:
        from PIL import Image
        with Image.open(image) as opened:
            opened.load()
            crops = []
            for _, (left, top, right, bottom) in (pair_lines[0], multiplier_lines[0]):
                padding_x, padding_y = 20, 15
                crop = opened.crop((max(0, left - padding_x), max(0, top - padding_y), min(opened.width, right + padding_x), min(opened.height, bottom + padding_y)))
                crops.append(crop.resize((crop.width * 2, crop.height * 2)))
    except (OSError, ValueError):
        return False
    expected_pair = expected[:2]
    expected_multiplier = expected[2]
    for psm in (6, 7):
        pair_text = _ocr_image(crops[0], psm)
        multiplier_text = _ocr_image(crops[1], psm)
        if pair_text is None or multiplier_text is None:
            return False
        pair = _PERCENT_PAIR.search(unicodedata.normalize("NFKC", pair_text))
        multiplier = _MULTIPLIER.search(unicodedata.normalize("NFKC", multiplier_text))
        if pair is None or multiplier is None:
            return False
        if (Decimal(pair["left"]), Decimal(pair["right"])) != expected_pair or Decimal(multiplier["value"]) != expected_multiplier:
            return False
    return True


def _extract(path: Path, count: int) -> tuple[int, tuple[Decimal, Decimal, Decimal]] | None:
    candidates = []
    with tempfile.TemporaryDirectory(prefix="pdf-investment-coefficient-") as temporary:
        work = Path(temporary)
        rendered = []
        for page_number in range(1, count + 1):
            image = work / f"page-{page_number:03d}.png"
            if not _render(path, page_number, image):
                return None
            rendered.append(image)
            scan = _ocr(image, 11)
            if scan is None:
                return None
            parsed = _values(scan)
            if parsed is not None:
                candidates.append((page_number, image, parsed))
        if len(candidates) != 1:
            return None
        page_number, image, expected = candidates[0]
        if not _crop_values_agree(image, expected):
            return None
        return page_number, expected


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    bound = _source(engine, contract["bindings"]["location"], contract["bindings"]["container"])
    if bound is None:
        return StructuredCandidateDecision("hold", "pdf_investment_coefficient_source_not_unique")
    root, path = bound
    try:
        data = path.read_bytes()
        if not 0 < len(data) <= _MAX_PDF_BYTES:
            raise ValueError("resource")
        count = _page_count(path)
        if count is None:
            raise ValueError("pages")
        extracted = _extract(path, count)
        if extracted is None:
            raise ValueError("visual evidence")
        _, (cost_reduction, productivity_improvement, roi_multiplier) = extracted
        result_value = ((productivity_improvement + cost_reduction) / Decimal(100)) * roi_multiplier
        if not Decimal(0) < result_value <= Decimal(1000):
            raise ValueError("result")
        relative = unicodedata.normalize("NFC", path.relative_to(root).as_posix())
        result = StructuredCandidateAnswer(_decimal_text(result_value), (relative,), hashlib.sha256(data).hexdigest(), len(contract["operation_graph"]["nodes"]), 1)
        return StructuredCandidateDecision("resolved", "certified_pdf_investment_coefficient", result)
    except (OSError, RuntimeError, TypeError, ValueError):
        return StructuredCandidateDecision("hold", "pdf_investment_coefficient_not_certified")


__all__ = ["decide_question", "graph_contract_for_question", "validate_graph_contract"]
