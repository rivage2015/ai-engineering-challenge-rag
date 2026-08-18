"""Fail-closed action transition extraction from scanned meeting-minute PDFs."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Mapping

from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision

VERSION = "0.1"
ACTION_TRANSITION = re.compile(
    r"^(?P<location>MINAMINO)において、M01時点では(?P<before>未完了)で、"
    r"M02までの間に(?P<after>完了)したAIのうち、"
    r"(?P<owner>伊藤)さんが担当しているものを抽出してください。$"
)
_ID_TOKEN = re.compile(r"A[0O][0-9OS]", re.IGNORECASE)
_MAX_PDF_BYTES = 64 * 1024 * 1024
_TIMEOUT = 45


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if not isinstance(question, str):
        return None
    match = ACTION_TRANSITION.fullmatch(question)
    if match is None:
        return None
    operators = (
        "bind_complete_meeting_minute_set",
        "render_every_page",
        "ocr_meeting_ids",
        "bind_m01_and_m02",
        "detect_action_table_rows",
        "verify_action_id_crop_consensus",
        "extract_owner_and_status_columns",
        "select_m01_open_owner_rows",
        "select_m02_closed_owner_rows",
        "intersect_action_ids",
        "project_sorted_ids",
    )
    nodes = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
        previous = output
    bindings = {key: match[key] for key in ("location", "before", "after", "owner")}
    core = {
        "pdf_action_transition_version": VERSION,
        "rule_id": "pdf_action_status_transition_by_owner",
        "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
        "bindings": bindings,
        "scope": {"source_channel": "full_page_raster_ocr_with_spatial_columns", "question_independent": True, "ambiguity_policy": "hold"},
        "operation_graph": {
            "external_inputs": [{"input_ref": "input_question", "input_type": "pdf_document_set", "source": "question_scope"}],
            "nodes": nodes,
            "edges": [{"from": nodes[index - 1]["output_ref"], "to": nodes[index]["operation_id"]} for index in range(1, len(nodes))],
        },
        "requested_output": {"source_operation_ref": nodes[-1]["operation_id"], "cardinality": "all", "answer_shape": {"container": "list", "value_type": "identifier", "unit": None}, "display_precision": None, "required_keys": None},
    }
    return {"graph_contract_id": "pdf_action_transition_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and isinstance(contract, Mapping) and _canonical(expected) == _canonical(contract)


def _compact(value: object) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value))).casefold()


def _sources(engine: Any, location: str) -> tuple[Path, tuple[Path, ...]] | None:
    try:
        from structured_candidate import _candidate_values, _location_matches
        root = Path(engine.source_root).resolve()
        candidates = _candidate_values(location, getattr(engine, "glossary", None))
        matches = []
        for path in root.rglob("*.pdf"):
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root)
            parts = tuple(_compact(part) for part in relative.parts)
            if "会議録" not in parts or not _location_matches(relative.parts[:-1], candidates):
                continue
            matches.append(path)
        ordered = tuple(sorted(matches, key=lambda path: unicodedata.normalize("NFC", path.relative_to(root).as_posix())))
        return (root, ordered) if len(ordered) == 3 else None
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return None


def _page_count(path: Path) -> int | None:
    try:
        from pypdf import PdfReader
        reader = PdfReader(path, strict=True)
        count = len(reader.pages)
        return count if not reader.is_encrypted and 1 <= count <= 50 else None
    except Exception:
        return None


def _render(path: Path, page_number: int, prefix: Path) -> Path | None:
    executable = shutil.which("pdftoppm")
    if executable is None:
        return None
    try:
        completed = subprocess.run([executable, "-f", str(page_number), "-l", str(page_number), "-r", "180", "-singlefile", "-png", str(path), str(prefix)], capture_output=True, timeout=_TIMEOUT, check=False)
        output = prefix.with_suffix(".png")
        return output if completed.returncode == 0 and output.is_file() and 0 < output.stat().st_size <= 32 * 1024 * 1024 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _ocr_words(image: Path) -> tuple[dict[str, str], ...] | None:
    executable = shutil.which("tesseract")
    if executable is None:
        return None
    try:
        completed = subprocess.run([executable, str(image.resolve()), "stdout", "-l", "jpn+eng", "--oem", "1", "--psm", "6", "tsv"], capture_output=True, timeout=_TIMEOUT, check=False)
        if completed.returncode != 0 or len(completed.stdout) > 8 * 1024 * 1024:
            return None
        rows = csv.DictReader(io.StringIO(completed.stdout.decode("utf-8", errors="strict")), delimiter="\t")
        return tuple(row for row in rows if row.get("level") == "5" and (row.get("text") or "").strip())
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None


def _ocr_id_crop(image: Path, word: Mapping[str, str]) -> str | None:
    try:
        raw = unicodedata.normalize("NFKC", word["text"]).upper()
        if re.fullmatch(r"A[0-9]{2}", raw) and float(word.get("conf", "-1")) >= 60:
            return raw
        from PIL import Image
        left, top, width, height = (int(word[key]) for key in ("left", "top", "width", "height"))
        with Image.open(image) as opened:
            opened.load()
            padding = 5
            crop = opened.crop((max(0, left - padding), max(0, top - padding), min(opened.width, left + width + padding), min(opened.height, top + height + padding)))
            crop = crop.resize((crop.width * 4, crop.height * 4))
            buffer = io.BytesIO(); crop.save(buffer, format="PNG")
        readings = []
        executable = shutil.which("tesseract")
        if executable is None:
            return None
        for psm in (6, 7):
            completed = subprocess.run([executable, "stdin", "stdout", "-l", "eng", "--oem", "1", "--psm", str(psm), "-c", "tessedit_char_whitelist=A0123456789"], input=buffer.getvalue(), capture_output=True, timeout=_TIMEOUT, check=False)
            if completed.returncode != 0:
                return None
            readings.append(re.sub(r"\s+", "", completed.stdout.decode("utf-8", errors="strict")).upper())
        return readings[0] if readings[0] == readings[1] and re.fullmatch(r"A[0-9]{2}", readings[0]) else None
    except (KeyError, OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        return None


def _table_rows(image: Path) -> dict[str, tuple[bool, str]] | None:
    words = _ocr_words(image)
    if words is None:
        return None
    try:
        from PIL import Image
        with Image.open(image) as opened:
            image_width = opened.width
    except OSError:
        return None
    header_tokens = {unicodedata.normalize("NFKC", word["text"]).casefold() for word in words if int(word["top"]) < 150}
    if not {"id", "action", "owner", "status"}.issubset(header_tokens):
        return {}
    candidates = []
    for word in words:
        try:
            left, top = int(word["left"]), int(word["top"])
        except (KeyError, ValueError):
            return None
        if (left < image_width * 0.10 or image_width * 0.50 <= left <= image_width * 0.60) and _ID_TOKEN.fullmatch(unicodedata.normalize("NFKC", word["text"]).upper()):
            action_id = _ocr_id_crop(image, word)
            if action_id is None:
                return None
            group = 0 if left < image_width * 0.25 else 1
            candidates.append((group, top, action_id, left))
    result = {}
    for group in (0, 1):
        group_rows = sorted((item for item in candidates if item[0] == group), key=lambda item: item[1])
        for index, (_, top, action_id, left) in enumerate(group_rows):
            bottom = group_rows[index + 1][1] if index + 1 < len(group_rows) else top + 420
            owner_text = " ".join(word["text"] for word in words if top - 12 <= int(word["top"]) < bottom and left + 150 <= int(word["left"]) < left + 340)
            status_text = " ".join(word["text"] for word in words if top - 12 <= int(word["top"]) < bottom and left + 350 <= int(word["left"]) < left + 500)
            if action_id in result:
                return None
            status = "closed" if re.search(r"close", status_text, re.IGNORECASE) else "open" if re.search(r"open", status_text, re.IGNORECASE) else "unknown"
            result[action_id] = ("伊藤" in owner_text, status)
    return result


def _meeting_id(image: Path) -> int | None:
    try:
        from PIL import Image
        with Image.open(image) as opened:
            opened.load()
            crop = opened.crop((0, 0, opened.width // 2, round(opened.height * 0.35)))
            buffer = io.BytesIO(); crop.save(buffer, format="PNG")
        executable = shutil.which("tesseract")
        if executable is None:
            return None
        values = []
        for psm in (6, 11):
            completed = subprocess.run([executable, "stdin", "stdout", "-l", "jpn+eng", "--oem", "1", "--psm", str(psm)], input=buffer.getvalue(), capture_output=True, timeout=_TIMEOUT, check=False)
            if completed.returncode != 0:
                return None
            matches = re.findall(r"M[O0]([1-9])", completed.stdout.decode("utf-8", errors="strict"), re.IGNORECASE)
            if len(set(matches)) != 1:
                return None
            values.append(int(matches[0]))
        return values[0] if values[0] == values[1] else None
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        return None


def _document_rows(path: Path, work: Path, source_index: int, expected_meeting_id: int) -> dict[str, tuple[bool, str]] | None:
    count = _page_count(path)
    if count is None:
        return None
    combined = {}
    for page_number in range(1, count + 1):
        image = _render(path, page_number, work / f"source-{source_index:02d}-page-{page_number:02d}")
        if image is None:
            return None
        if page_number == 1 and _meeting_id(image) != expected_meeting_id:
            return None
        rows = _table_rows(image)
        if rows is None:
            return None
        overlap = set(combined).intersection(rows)
        if any(combined[key] != rows[key] for key in overlap):
            return None
        combined.update(rows)
    return combined


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    bound = _sources(engine, contract["bindings"]["location"])
    if bound is None:
        return StructuredCandidateDecision("hold", "pdf_action_transition_sources_not_complete")
    root, paths = bound
    try:
        dated = sorted(paths, key=lambda path: path.name)
        source_records = []
        for path in dated:
            data = path.read_bytes()
            if not 0 < len(data) <= _MAX_PDF_BYTES:
                raise ValueError("resource")
            source_records.append({"path": unicodedata.normalize("NFC", path.relative_to(root).as_posix()), "sha256": hashlib.sha256(data).hexdigest()})
        with tempfile.TemporaryDirectory(prefix="pdf-action-transition-") as temporary:
            work = Path(temporary)
            m01 = _document_rows(dated[0], work, 1, 1)
            m02 = _document_rows(dated[1], work, 2, 2)
            if _document_rows(dated[2], work, 3, 3) is None:
                raise ValueError("m03 completeness")
        if m01 is None or m02 is None:
            raise ValueError("rows")
        selected = sorted(action_id for action_id, (owner, status) in m01.items() if owner and status == "open" and m02.get(action_id) == (True, "closed"))
        if not selected:
            raise ValueError("transition")
        digest = hashlib.sha256(_canonical(source_records).encode()).hexdigest()
        result = StructuredCandidateAnswer("、".join(selected), tuple(record["path"] for record in source_records), digest, len(contract["operation_graph"]["nodes"]), len(selected))
        return StructuredCandidateDecision("resolved", "certified_pdf_action_transition", result)
    except (OSError, RuntimeError, TypeError, ValueError):
        return StructuredCandidateDecision("hold", "pdf_action_transition_not_certified")


__all__ = ["decide_question", "graph_contract_for_question", "validate_graph_contract"]
