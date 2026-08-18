#!/usr/bin/env python3
"""Build question-independent, page-level PDF observations.

The builder is deliberately shadow-only. It never reads competition
questions, answers, predictions, or gold data, and it does not write Evidence,
SearchUnit, an index, or a submission.

Each PDF page follows exactly one route:

* native_bbox: Poppler returned one or more native words with PDF-point
  bounding boxes.
* ocr_raw: Poppler returned no words and one existing visual asset plus one
  existing OCR observation were bound to the same source hash and page.
* unresolved: Poppler returned no words, but the required OCR lineage was
  missing or ambiguous.

Native and OCR text are never merged. OCR disagreements remain unresolved.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator, FormatChecker
from PIL import Image

import build_visual_asset_manifest as visual_manifest_builder
import validate_ocr_observations as ocr_semantic_validator
import validate_visual_asset_manifest as visual_manifest_validator


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "pdf-page-observation.schema.json"
VISUAL_ASSET_SCHEMA_PATH = ROOT / "schemas" / "visual-asset.schema.json"
OCR_SCHEMA_PATH = ROOT / "schemas" / "ocr-observation.schema.json"

SCHEMA_VERSION = "0.1"
RECORD_TYPE = "pdf_page_observation"
BUILDER = "pdf-page-observation-builder"
BUILDER_VERSION = "0.1.0"

MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_JSONL_BYTES = 256 * 1024 * 1024
MAX_JSONL_RECORDS = 200_000
MAX_COMMAND_OUTPUT_BYTES = 128 * 1024 * 1024
MAX_COMMAND_SECONDS = 180
MAX_PAGES = 1000
MAX_WORDS_PER_PAGE = 1_000_000
MAX_OCR_LINES_PER_RUN = 2000
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FILE_ID_RE = re.compile(r"^file_([0-9a-f]{32})$")
FORBIDDEN_DATA_RE = re.compile(
    r"(?:(?:^|[-_.])(questions?|gold|predictions?|answers?)(?:[-_.]|$)|"
    r"質問|正解|予測|回答)",
    re.IGNORECASE,
)
REQUIRED_INVENTORY_FIELDS = {
    "file_id",
    "file_path",
    "extension",
    "file_size",
    "source_sha256",
    "document_type",
    "page_count",
}


class PDFObservationError(ValueError):
    """Raised when an input or generated observation violates the contract."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise PDFObservationError(f"duplicate JSON key: {key}")
        output[key] = value
    return output


def _reject_nonfinite_constant(value: str) -> None:
    raise PDFObservationError(f"non-finite JSON number is forbidden: {value}")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _has_symlink_component(path: Path, trusted_root: Path) -> bool:
    current = trusted_root
    for component in path.relative_to(trusted_root).parts:
        current = current / component
        if current.is_symlink():
            return True
    return False


def _forbid_sensitive_path(path: Path, label: str) -> None:
    for component in path.parts:
        if FORBIDDEN_DATA_RE.search(component):
            raise PDFObservationError(
                f"{label} contains a forbidden data component: {component}"
            )


def resolve_trusted_root(raw: Path, label: str) -> Path:
    candidate = raw if raw.is_absolute() else Path.cwd() / raw
    candidate = Path(os.path.abspath(candidate))
    current = Path(candidate.anchor)
    for component in candidate.parts[1:]:
        current = current / component
        if current.is_symlink():
            raise PDFObservationError(f"{label} contains a symlink component")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_dir():
        raise PDFObservationError(f"{label} is not a directory")
    return resolved


def resolve_repo_input(repository_root: Path, raw: Path, label: str) -> Path:
    if ".." in raw.parts:
        raise PDFObservationError(f"{label} contains a parent traversal")
    candidate = raw if raw.is_absolute() else repository_root / raw
    candidate = Path(os.path.abspath(candidate))
    if not _inside(candidate, repository_root):
        raise PDFObservationError(f"{label} must be inside repository root")
    if _has_symlink_component(candidate, repository_root):
        raise PDFObservationError(f"{label} contains a symlink component")
    _forbid_sensitive_path(candidate.relative_to(repository_root), label)
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file():
        raise PDFObservationError(f"{label} is not a regular file")
    return resolved


def resolve_repo_output(repository_root: Path, raw: Path) -> Path:
    if ".." in raw.parts:
        raise PDFObservationError("output contains a parent traversal")
    candidate = raw if raw.is_absolute() else repository_root / raw
    candidate = Path(os.path.abspath(candidate))
    if not _inside(candidate, repository_root):
        raise PDFObservationError("output must be inside repository root")
    _forbid_sensitive_path(candidate.relative_to(repository_root), "output")
    current = repository_root
    for component in candidate.relative_to(repository_root).parts[:-1]:
        current = current / component
        if current.exists() and current.is_symlink():
            raise PDFObservationError("output parent contains a symlink component")
    if candidate.is_symlink():
        raise PDFObservationError("output must not be a symlink")
    return candidate


def resolve_source_file(source_root: Path, relative_path: str) -> Path:
    normalized = unicodedata.normalize("NFC", relative_path)
    raw = Path(normalized)
    if raw.is_absolute() or ".." in raw.parts:
        raise PDFObservationError("source relative path is unsafe")
    candidate = source_root.joinpath(*raw.parts)
    if not candidate.exists():
        nfd = Path(*(unicodedata.normalize("NFD", part) for part in raw.parts))
        candidate = source_root.joinpath(*nfd.parts)
    candidate = Path(os.path.abspath(candidate))
    if not _inside(candidate, source_root):
        raise PDFObservationError("source path escapes source root")
    if _has_symlink_component(candidate, source_root):
        raise PDFObservationError("source path contains a symlink component")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file():
        raise PDFObservationError("source path is not a regular file")
    return resolved


def resolve_materialized_file(repository_root: Path, raw: str) -> Path:
    candidate_raw = Path(raw)
    if ".." in candidate_raw.parts:
        raise PDFObservationError("materialized path contains a parent traversal")
    candidate = (
        candidate_raw
        if candidate_raw.is_absolute()
        else repository_root / candidate_raw
    )
    candidate = Path(os.path.abspath(candidate))
    if not _inside(candidate, repository_root):
        raise PDFObservationError("materialized path escapes repository root")
    if _has_symlink_component(candidate, repository_root):
        raise PDFObservationError("materialized path contains a symlink component")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file():
        raise PDFObservationError("materialized path is not a regular file")
    return resolved


def load_jsonl(path: Path, label: str) -> tuple[list[dict[str, Any]], str]:
    if path.is_symlink():
        raise PDFObservationError(f"{label} must not be a symlink")
    size = path.stat().st_size
    if size <= 0 or size > MAX_JSONL_BYTES:
        raise PDFObservationError(f"{label} size is outside the accepted range")
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                raise PDFObservationError(f"{label}:{line_number}: blank JSONL line")
            value = json.loads(
                raw,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite_constant,
            )
            if not isinstance(value, dict):
                raise PDFObservationError(
                    f"{label}:{line_number}: record must be an object"
                )
            records.append(value)
            if len(records) > MAX_JSONL_RECORDS:
                raise PDFObservationError(f"{label} has too many records")
    return records, sha256_file(path)


def _schema_validator(path: Path) -> Draft202012Validator:
    schema = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_nonfinite_constant,
    )
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _schema_errors(record: Mapping[str, Any], path: Path) -> list[str]:
    errors: list[str] = []
    for error in sorted(
        _schema_validator(path).iter_errors(record), key=lambda item: list(item.path)
    ):
        location = "/" + "/".join(str(value) for value in error.path)
        errors.append(f"{location}: {error.message}")
    return errors


def read_inventory(path: Path) -> tuple[list[dict[str, Any]], str]:
    inventory_sha256 = sha256_file(path)
    rows: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or ())
        missing = REQUIRED_INVENTORY_FIELDS - fields
        if missing:
            raise PDFObservationError(
                "inventory is missing fields: " + ", ".join(sorted(missing))
            )
        for line_number, raw in enumerate(reader, start=2):
            extension = str(raw.get("extension", "")).lower().lstrip(".")
            document_type = str(raw.get("document_type", "")).lower()
            if extension != "pdf" and document_type != "pdf":
                continue
            relative_path = unicodedata.normalize(
                "NFC", str(raw.get("file_path", "")).replace("\\", "/")
            )
            if (
                not relative_path
                or Path(relative_path).is_absolute()
                or ".." in Path(relative_path).parts
                or Path(relative_path).suffix.lower() != ".pdf"
            ):
                raise PDFObservationError(
                    f"inventory:{line_number}: unsafe PDF relative path"
                )
            if relative_path in seen_paths:
                raise PDFObservationError(
                    f"inventory:{line_number}: duplicate PDF path: {relative_path}"
                )
            seen_paths.add(relative_path)
            file_match = FILE_ID_RE.fullmatch(str(raw.get("file_id", "")))
            if not file_match:
                raise PDFObservationError(
                    f"inventory:{line_number}: invalid file_id"
                )
            source_sha256 = str(raw.get("source_sha256", ""))
            if not SHA256_RE.fullmatch(source_sha256):
                raise PDFObservationError(
                    f"inventory:{line_number}: invalid source SHA-256"
                )
            try:
                size_bytes = int(str(raw.get("file_size", "")))
                page_count = int(str(raw.get("page_count", "")))
            except ValueError as exc:
                raise PDFObservationError(
                    f"inventory:{line_number}: invalid numeric field"
                ) from exc
            if not (1 <= size_bytes <= MAX_SOURCE_BYTES):
                raise PDFObservationError(
                    f"inventory:{line_number}: source size is outside the accepted range"
                )
            if not (1 <= page_count <= MAX_PAGES):
                raise PDFObservationError(
                    f"inventory:{line_number}: page count is outside the accepted range"
                )
            rows.append(
                {
                    "file_id": str(raw["file_id"]),
                    "document_id": "doc_" + file_match.group(1),
                    "relative_path": relative_path,
                    "source_sha256": source_sha256,
                    "size_bytes": size_bytes,
                    "page_count": page_count,
                }
            )
    if not rows:
        raise PDFObservationError("inventory contains no PDF rows")
    rows.sort(key=lambda row: row["relative_path"])
    return rows, inventory_sha256


def resolve_executable(name: str, label: str) -> Path:
    resolved = shutil.which(name)
    if resolved is None:
        raw = Path(name)
        if raw.is_absolute() and raw.exists():
            resolved = str(raw)
    if resolved is None:
        raise PDFObservationError(f"{label} executable was not found")
    path = Path(resolved).resolve(strict=True)
    if not path.is_file():
        raise PDFObservationError(f"{label} executable is not a regular file")
    return path


def command_version(executable: Path, label: str) -> str:
    try:
        result = subprocess.run(
            [str(executable), "-v"],
            capture_output=True,
            check=False,
            timeout=30,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
    except subprocess.TimeoutExpired as exc:
        raise PDFObservationError(f"{label} version command timed out") from exc
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace").strip()
    if result.returncode != 0 or not output:
        raise PDFObservationError(f"{label} version command failed")
    if len(output.encode("utf-8")) > 64 * 1024:
        raise PDFObservationError(f"{label} version output is too large")
    return output


def run_bounded(command: Sequence[str], label: str) -> bytes:
    try:
        result = subprocess.run(
            list(command),
            capture_output=True,
            check=False,
            timeout=MAX_COMMAND_SECONDS,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
    except subprocess.TimeoutExpired as exc:
        raise PDFObservationError(f"{label} timed out") from exc
    if len(result.stdout) > MAX_COMMAND_OUTPUT_BYTES:
        raise PDFObservationError(f"{label} output exceeds the accepted limit")
    if len(result.stderr) > 1024 * 1024:
        raise PDFObservationError(f"{label} stderr exceeds the accepted limit")
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise PDFObservationError(f"{label} failed: {detail[:1000]}")
    return result.stdout


def _parse_int(value: str, label: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise PDFObservationError(f"{label} is not an integer") from exc


def parse_pdfinfo(
    output: bytes, *, expected_pages: int
) -> tuple[dict[int, dict[str, Any]], str]:
    text = output.decode("utf-8", errors="strict")
    page_count: int | None = None
    encrypted: str | None = None
    sizes: dict[int, tuple[float, float]] = {}
    rotations: dict[int, int] = {}
    global_size: tuple[float, float] | None = None
    global_rotation = 0
    for line in text.splitlines():
        if match := re.match(r"^Pages:\s+(\d+)\s*$", line):
            page_count = _parse_int(match.group(1), "pdfinfo page count")
        elif match := re.match(r"^Encrypted:\s+(\S+)", line):
            encrypted = match.group(1).lower()
        elif match := re.match(
            r"^Page\s+(\d+)\s+size:\s+([0-9.]+)\s+x\s+([0-9.]+)\s+pts",
            line,
        ):
            sizes[int(match.group(1))] = (
                float(match.group(2)),
                float(match.group(3)),
            )
        elif match := re.match(r"^Page\s+(\d+)\s+rot:\s+(\d+)", line):
            rotations[int(match.group(1))] = int(match.group(2))
        elif match := re.match(
            r"^Page size:\s+([0-9.]+)\s+x\s+([0-9.]+)\s+pts", line
        ):
            global_size = (float(match.group(1)), float(match.group(2)))
        elif match := re.match(r"^Page rot:\s+(\d+)", line):
            global_rotation = int(match.group(1))
    if page_count != expected_pages:
        raise PDFObservationError(
            f"pdfinfo page count mismatch: expected {expected_pages}, got {page_count}"
        )
    if encrypted not in {None, "no"}:
        raise PDFObservationError("encrypted PDFs are not accepted by v0.1")
    pages: dict[int, dict[str, Any]] = {}
    for page_number in range(1, expected_pages + 1):
        size = sizes.get(page_number, global_size)
        if size is None:
            raise PDFObservationError(
                f"pdfinfo did not report dimensions for page {page_number}"
            )
        rotation = rotations.get(page_number, global_rotation) % 360
        if rotation not in {0, 90, 180, 270}:
            raise PDFObservationError(
                f"unsupported page rotation for page {page_number}: {rotation}"
            )
        if (
            not all(math.isfinite(value) and value > 0 for value in size)
            or size[0] > 20000
            or size[1] > 20000
        ):
            raise PDFObservationError(
                f"invalid page dimensions for page {page_number}"
            )
        pages[page_number] = {
            "width_pt": size[0],
            "height_pt": size[1],
            "rotation_degrees": rotation,
        }
    return pages, sha256_bytes(output)


def _local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _finite_float(value: str | None, label: str) -> float:
    if value is None:
        raise PDFObservationError(f"{label} is missing")
    try:
        output = float(value)
    except ValueError as exc:
        raise PDFObservationError(f"{label} is not numeric") from exc
    if not math.isfinite(output):
        raise PDFObservationError(f"{label} is not finite")
    return output


def _round_coordinate(value: float) -> float:
    return round(value, 6)


def parse_bbox_pages(
    output: bytes, *, expected_pages: int
) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(output)
    except ET.ParseError as exc:
        raise PDFObservationError("pdftotext bbox output is not valid XML") from exc
    page_elements = [
        element for element in root.iter() if _local_name(element) == "page"
    ]
    if len(page_elements) != expected_pages:
        raise PDFObservationError(
            "pdftotext page count mismatch: "
            f"expected {expected_pages}, got {len(page_elements)}"
        )
    pages: list[dict[str, Any]] = []
    for page_number, page_element in enumerate(page_elements, start=1):
        width = _finite_float(page_element.get("width"), "page width")
        height = _finite_float(page_element.get("height"), "page height")
        if not (0 < width <= 20000 and 0 < height <= 20000):
            raise PDFObservationError(
                f"pdftotext dimensions are invalid for page {page_number}"
            )
        words: list[dict[str, Any]] = []
        block_index = 0
        line_index = 0
        for block in (
            element
            for element in page_element.iter()
            if _local_name(element) == "block"
        ):
            block_index += 1
            for line in (
                element
                for element in block.iter()
                if _local_name(element) == "line"
            ):
                line_index += 1
                for word in (
                    element
                    for element in line.iter()
                    if _local_name(element) == "word"
                ):
                    raw_text = word.text
                    if not isinstance(raw_text, str) or not raw_text:
                        raise PDFObservationError(
                            f"pdftotext emitted a blank word on page {page_number}"
                        )
                    x_min = _finite_float(word.get("xMin"), "word xMin")
                    y_min = _finite_float(word.get("yMin"), "word yMin")
                    x_max = _finite_float(word.get("xMax"), "word xMax")
                    y_max = _finite_float(word.get("yMax"), "word yMax")
                    tolerance = 0.001
                    if not (
                        -tolerance <= x_min < x_max <= width + tolerance
                        and -tolerance <= y_min < y_max <= height + tolerance
                    ):
                        raise PDFObservationError(
                            f"native word bbox is outside page {page_number}"
                        )
                    x_min = max(0.0, min(width, x_min))
                    y_min = max(0.0, min(height, y_min))
                    x_max = max(0.0, min(width, x_max))
                    y_max = max(0.0, min(height, y_max))
                    reading_order = len(words) + 1
                    words.append(
                        {
                            "word_id": f"word_{reading_order:06d}",
                            "reading_order": reading_order,
                            "block_index": block_index,
                            "line_index": line_index,
                            "word_index": reading_order,
                            "raw_text": raw_text,
                            "bbox": [
                                _round_coordinate(x_min),
                                _round_coordinate(y_min),
                                _round_coordinate(x_max - x_min),
                                _round_coordinate(y_max - y_min),
                            ],
                        }
                    )
                    if len(words) > MAX_WORDS_PER_PAGE:
                        raise PDFObservationError(
                            f"page {page_number} has too many native words"
                        )
        all_word_elements = sum(
            1 for element in page_element.iter() if _local_name(element) == "word"
        )
        if all_word_elements != len(words):
            raise PDFObservationError(
                f"page {page_number} has words outside the expected block/line structure"
            )
        pages.append(
            {
                "page_number": page_number,
                "width_pt": _round_coordinate(width),
                "height_pt": _round_coordinate(height),
                "words": words,
                "page_output_sha256": sha256_bytes(
                    ET.tostring(page_element, encoding="utf-8")
                ),
            }
        )
    return pages


def _index_pdf_records(
    records: Iterable[dict[str, Any]], *, label: str
) -> dict[tuple[str, str, int], list[dict[str, Any]]]:
    output: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for position, record in enumerate(records, start=1):
        origin = record.get("origin")
        source = record.get("source")
        if not isinstance(origin, dict) or not isinstance(source, dict):
            raise PDFObservationError(f"{label}:{position}: missing source or origin")
        if origin.get("kind") != "pdf_page":
            continue
        relative_path = source.get("relative_path")
        source_sha = source.get("sha256")
        page_number = origin.get("page_number")
        if (
            not isinstance(relative_path, str)
            or not relative_path
            or unicodedata.normalize("NFC", relative_path) != relative_path
            or not isinstance(source_sha, str)
            or not SHA256_RE.fullmatch(source_sha)
            or isinstance(page_number, bool)
            or not isinstance(page_number, int)
            or page_number < 1
        ):
            raise PDFObservationError(f"{label}:{position}: invalid PDF binding key")
        output.setdefault((relative_path, source_sha, page_number), []).append(record)
    return output


def validate_visual_inputs(
    records: Sequence[dict[str, Any]], manifest_path: Path
) -> None:
    limits = visual_manifest_builder.office_zip_limits(
        max_archive_entries=visual_manifest_builder.DEFAULT_MAX_OFFICE_ARCHIVE_ENTRIES,
        max_member_uncompressed_bytes=visual_manifest_builder.DEFAULT_MAX_OFFICE_MEMBER_BYTES,
        max_total_uncompressed_bytes=visual_manifest_builder.DEFAULT_MAX_OFFICE_TOTAL_BYTES,
        max_compression_ratio=visual_manifest_builder.DEFAULT_MAX_OFFICE_COMPRESSION_RATIO,
    )
    for index, record in enumerate(records, start=1):
        schema_errors = _schema_errors(record, VISUAL_ASSET_SCHEMA_PATH)
        if schema_errors:
            raise PDFObservationError(
                f"visual assets:{index}: schema invalid: "
                + "; ".join(schema_errors[:20])
            )
        try:
            visual_manifest_validator.validate_record(
                record, index, manifest_path, limits
            )
        except (OSError, ValueError) as exc:
            raise PDFObservationError(
                f"visual assets:{index}: semantic validation failed: {exc}"
            ) from exc


def validate_ocr_inputs(records: Sequence[dict[str, Any]]) -> None:
    for index, record in enumerate(records, start=1):
        schema_errors = _schema_errors(record, OCR_SCHEMA_PATH)
        if schema_errors:
            raise PDFObservationError(
                f"OCR observations:{index}: schema invalid: "
                + "; ".join(schema_errors[:20])
            )
        semantic_errors = ocr_semantic_validator.validate(record)
        if semantic_errors:
            raise PDFObservationError(
                f"OCR observations:{index}: semantic validation failed: "
                + "; ".join(semantic_errors[:20])
            )


def _reading_ref(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "run_id": value["run_id"],
        "line_id": value["line_id"],
        "raw_text": value["raw_text"],
    }


def _normalized_bbox(value: Any, label: str) -> list[int]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
    ):
        raise PDFObservationError(f"{label} must be four integers")
    x, y, width, height = value
    if not (
        0 <= x <= 999
        and 0 <= y <= 999
        and 1 <= width <= 1000
        and 1 <= height <= 1000
        and x + width <= 1000
        and y + height <= 1000
    ):
        raise PDFObservationError(f"{label} is outside normalized image bounds")
    return list(value)


def _unresolved_item(
    *,
    seed: Mapping[str, Any],
    reason: str,
    bbox: list[int] | None = None,
    readings: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    refs = [_reading_ref(value) for value in readings]
    payload = {
        "seed": dict(seed),
        "reason": reason,
        "bbox": bbox,
        "readings": refs,
    }
    return {
        "unresolved_id": "pdfunres_" + sha256_json(payload)[:16],
        "reason": reason,
        "bbox": bbox,
        "readings": refs,
    }


def _conflict_item(
    *,
    seed: Mapping[str, Any],
    bbox: list[int],
    readings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    refs = [_reading_ref(value) for value in readings]
    payload = {"seed": dict(seed), "bbox": bbox, "readings": refs}
    return {
        "conflict_id": "pdfconf_" + sha256_json(payload)[:16],
        "reason": "ocr_text_disagreement",
        "bbox": bbox,
        "readings": refs,
    }


def _manifest_binding_errors(
    record: Mapping[str, Any],
    *,
    row: Mapping[str, Any],
    page_number: int,
) -> list[str]:
    errors = _schema_errors(record, VISUAL_ASSET_SCHEMA_PATH)
    if errors:
        return errors
    if record["source"]["relative_path"] != row["relative_path"]:
        errors.append("manifest source path mismatch")
    if record["source"]["sha256"] != row["source_sha256"]:
        errors.append("manifest source SHA-256 mismatch")
    if record["source"]["size_bytes"] != row["size_bytes"]:
        errors.append("manifest source size mismatch")
    if record["origin"]["kind"] != "pdf_page":
        errors.append("manifest origin is not pdf_page")
    if record["origin"]["page_number"] != page_number:
        errors.append("manifest page mismatch")
    if record["materialization"] is None:
        errors.append("manifest page has no materialization")
    return errors


def _ocr_binding_errors(
    record: Mapping[str, Any],
    *,
    row: Mapping[str, Any],
    page_number: int,
    manifest: Mapping[str, Any],
) -> list[str]:
    errors = _schema_errors(record, OCR_SCHEMA_PATH)
    if errors:
        return errors
    if record["source"]["relative_path"] != row["relative_path"]:
        errors.append("OCR source path mismatch")
    if record["source"]["sha256"] != row["source_sha256"]:
        errors.append("OCR source SHA-256 mismatch")
    if record["source"]["size_bytes"] != row["size_bytes"]:
        errors.append("OCR source size mismatch")
    if record["origin"]["kind"] != "pdf_page":
        errors.append("OCR origin is not pdf_page")
    if record["origin"]["page_number"] != page_number:
        errors.append("OCR page mismatch")
    if record["asset_id"] != manifest["asset_id"]:
        errors.append("OCR asset_id mismatch")
    materialization = manifest["materialization"]
    expected_asset = {
        "materialized_path": manifest["materialized_path"],
        "sha256": materialization["sha256"],
        "mime_type": materialization["mime_type"],
        "dimensions": {
            "width_px": materialization["width_px"],
            "height_px": materialization["height_px"],
        },
    }
    if record["asset"] != expected_asset:
        errors.append("OCR asset does not match manifest materialization")
    return errors


def _verify_materialized_image(
    repository_root: Path, manifest: Mapping[str, Any]
) -> tuple[Path, dict[str, int]]:
    materialization = manifest["materialization"]
    image_path = resolve_materialized_file(
        repository_root, str(manifest["materialized_path"])
    )
    if sha256_file(image_path) != materialization["sha256"]:
        raise PDFObservationError("materialized image SHA-256 mismatch")
    with Image.open(image_path) as image:
        image.load()
        dimensions = {"width_px": image.width, "height_px": image.height}
    expected = {
        "width_px": materialization["width_px"],
        "height_px": materialization["height_px"],
    }
    if dimensions != expected:
        raise PDFObservationError("materialized image dimensions mismatch")
    return image_path, dimensions


def build_ocr_route(
    *,
    repository_root: Path,
    row: Mapping[str, Any],
    page_number: int,
    manifests: Sequence[dict[str, Any]],
    observations: Sequence[dict[str, Any]],
) -> tuple[str, str, dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    seed = {"source_sha256": row["source_sha256"], "page_number": page_number}
    if len(manifests) != 1:
        reason = (
            "missing_materialized_page"
            if not manifests
            else "ambiguous_materialized_page"
        )
        return (
            "unresolved",
            reason,
            None,
            [],
            [_unresolved_item(seed=seed, reason=reason)],
        )
    if len(observations) != 1:
        reason = (
            "missing_ocr_observation"
            if not observations
            else "ambiguous_ocr_observation"
        )
        return (
            "unresolved",
            reason,
            None,
            [],
            [_unresolved_item(seed=seed, reason=reason)],
        )
    manifest = manifests[0]
    observation = observations[0]
    binding_errors = _manifest_binding_errors(
        manifest, row=row, page_number=page_number
    )
    binding_errors.extend(
        _ocr_binding_errors(
            observation,
            row=row,
            page_number=page_number,
            manifest=manifest,
        )
    )
    if binding_errors:
        return (
            "unresolved",
            "upstream_binding_mismatch",
            None,
            [],
            [
                _unresolved_item(
                    seed={**seed, "binding_errors": binding_errors},
                    reason="upstream_binding_mismatch",
                )
            ],
        )
    try:
        image_path, dimensions = _verify_materialized_image(
            repository_root, manifest
        )
    except (OSError, PDFObservationError):
        return (
            "unresolved",
            "upstream_binding_mismatch",
            None,
            [],
            [
                _unresolved_item(
                    seed={**seed, "image": "verification_failed"},
                    reason="upstream_binding_mismatch",
                )
            ],
        )
    raw_runs: list[dict[str, Any]] = []
    line_lookup: dict[tuple[str, str], dict[str, Any]] = {}
    for run in observation["engine_runs"]:
        lines: list[dict[str, Any]] = []
        expected_sequence = 1
        for line in run["lines"]:
            sequence = line["sequence"]
            if sequence != expected_sequence:
                return (
                    "unresolved",
                    "upstream_binding_mismatch",
                    None,
                    [],
                    [
                        _unresolved_item(
                            seed={**seed, "run_id": run["run_id"], "sequence": sequence},
                            reason="upstream_binding_mismatch",
                        )
                    ],
                )
            expected_sequence += 1
            bbox = _normalized_bbox(
                line["bbox"], f"{run['run_id']}:{line['line_id']} bbox"
            )
            normalized = {
                "line_id": line["line_id"],
                "reading_order": sequence,
                "raw_text": line["raw_text"],
                "bbox": bbox,
                "confidence": line["confidence"],
            }
            lines.append(normalized)
            line_lookup[(run["run_id"], line["line_id"])] = {
                "run_id": run["run_id"],
                "line_id": line["line_id"],
                "raw_text": line["raw_text"],
            }
            if len(lines) > MAX_OCR_LINES_PER_RUN:
                raise PDFObservationError("OCR run has too many lines")
        raw_runs.append(
            {
                "run_id": run["run_id"],
                "engine": {
                    "name": run["engine"]["name"],
                    "version": run["engine"]["version"],
                    "digest": run["engine"]["digest"],
                    "independence_group": run["engine"]["independence_group"],
                },
                "status": run["status"],
                "lines": lines,
                "warnings": list(run["warnings"]),
                "error": run["error"],
                "output_sha256": run["hashes"]["output_sha256"],
            }
        )
    if not raw_runs:
        return (
            "unresolved",
            "upstream_ocr_has_no_raw_runs",
            None,
            [],
            [
                _unresolved_item(
                    seed=seed, reason="upstream_ocr_has_no_raw_runs"
                )
            ],
        )
    conflicts: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for consensus_line in observation["consensus"]["lines"]:
        if consensus_line["exactness"] != "unresolved":
            continue
        bbox = _normalized_bbox(
            consensus_line["bbox"],
            f"{consensus_line['consensus_line_id']} bbox",
        )
        readings: list[dict[str, Any]] = []
        for reading in consensus_line["readings"]:
            key = (reading["run_id"], reading["line_id"])
            original = line_lookup.get(key)
            if original is None or original["raw_text"] != reading["raw_text"]:
                return (
                    "unresolved",
                    "upstream_binding_mismatch",
                    None,
                    [],
                    [
                        _unresolved_item(
                            seed={
                                **seed,
                                "consensus_line_id": consensus_line[
                                    "consensus_line_id"
                                ],
                            },
                            reason="upstream_binding_mismatch",
                        )
                    ],
                )
            readings.append(original)
        unresolved.append(
            _unresolved_item(
                seed={
                    **seed,
                    "consensus_line_id": consensus_line["consensus_line_id"],
                },
                reason="ocr_consensus_unresolved",
                bbox=bbox,
                readings=readings,
            )
        )
        if len(readings) >= 2 and len({item["raw_text"] for item in readings}) >= 2:
            conflicts.append(
                _conflict_item(
                    seed={
                        **seed,
                        "consensus_line_id": consensus_line["consensus_line_id"],
                    },
                    bbox=bbox,
                    readings=readings,
                )
            )
    if (
        observation["status"] != "observed"
        and not unresolved
        and observation["exactness"] != "observed"
    ):
        unresolved.append(
            _unresolved_item(
                seed={**seed, "upstream_status": observation["status"]},
                reason="upstream_record_unresolved",
            )
        )
    relative_materialized_path = image_path.relative_to(repository_root).as_posix()
    ocr = {
        "asset_id": manifest["asset_id"],
        "upstream_observation_id": observation["observation_id"],
        "asset": {
            "materialized_path": relative_materialized_path,
            "sha256": manifest["materialization"]["sha256"],
            "mime_type": manifest["materialization"]["mime_type"],
            "dimensions": dimensions,
        },
        "manifest_record_sha256": sha256_json(manifest),
        "upstream_record_sha256": sha256_json(observation),
        "upstream_signature_sha256": observation["hashes"]["signature_sha256"],
        "upstream_status": observation["status"],
        "upstream_exactness": observation["exactness"],
        "coordinate_system": "top_left_normalized_1000",
        "raw_text_modified": False,
        "raw_runs": raw_runs,
    }
    return (
        "ocr_raw",
        "pdftotext_page_has_no_words_and_ocr_is_bound",
        ocr,
        conflicts,
        unresolved,
    )


def _input_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    ocr = record["ocr"]
    ocr_lineage = None
    if ocr is not None:
        ocr_lineage = {
            "asset_id": ocr["asset_id"],
            "asset_sha256": ocr["asset"]["sha256"],
            "manifest_record_sha256": ocr["manifest_record_sha256"],
            "upstream_record_sha256": ocr["upstream_record_sha256"],
            "upstream_signature_sha256": ocr["upstream_signature_sha256"],
        }
    return {
        "document_id": record["document_id"],
        "source": record["source"],
        "page": record["page"],
        "native_probe": record["extraction"]["native_probe"],
        "ocr_lineage": ocr_lineage,
        "inventory_sha256": record["provenance"]["inventory_sha256"],
        "visual_assets_sha256": record["provenance"]["visual_assets_sha256"],
        "ocr_observations_sha256": record["provenance"][
            "ocr_observations_sha256"
        ],
        "pdfinfo": record["provenance"]["pdfinfo"],
    }


def _content_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "page": record["page"],
        "extraction": record["extraction"],
        "native": record["native"],
        "ocr": record["ocr"],
        "conflicts": record["conflicts"],
        "unresolved": record["unresolved"],
        "status": record["status"],
    }


def _signature_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": record["schema_version"],
        "record_type": record["record_type"],
        "document_id": record["document_id"],
        "source": record["source"],
        "page_number": record["page"]["page_number"],
        "input_sha256": record["hashes"]["input_sha256"],
        "content_sha256": record["hashes"]["content_sha256"],
        "builder": record["provenance"]["builder"],
        "builder_version": record["provenance"]["builder_version"],
    }


def record_integrity_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(dict(record))
    payload["hashes"].pop("record_integrity_sha256", None)
    return payload


def rehash_record(record: Mapping[str, Any]) -> dict[str, Any]:
    output = copy.deepcopy(dict(record))
    output["hashes"]["input_sha256"] = sha256_json(_input_payload(output))
    output["hashes"]["content_sha256"] = sha256_json(_content_payload(output))
    output["hashes"]["signature_sha256"] = sha256_json(
        _signature_payload(output)
    )
    output["observation_id"] = (
        "pdfpage_" + output["hashes"]["signature_sha256"][:24]
    )
    output["hashes"]["record_integrity_sha256"] = sha256_json(
        record_integrity_payload(output)
    )
    return output


def _bbox_errors(bbox: Sequence[Any], width: float, height: float, label: str) -> list[str]:
    if len(bbox) != 4:
        return [f"{label}: bbox must contain four values"]
    x, y, box_width, box_height = bbox
    values = (x, y, box_width, box_height)
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        for value in values
    ):
        return [f"{label}: bbox must contain finite numbers"]
    if not (
        0 <= float(x)
        and 0 <= float(y)
        and 0 < float(box_width)
        and 0 < float(box_height)
        and float(x) + float(box_width) <= width + 1e-6
        and float(y) + float(box_height) <= height + 1e-6
    ):
        return [f"{label}: bbox is outside its coordinate space"]
    return []


def semantic_errors(record: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    page = record["page"]
    if page["page_number"] > page["page_count"]:
        errors.append("/page/page_number: exceeds page_count")
    route = record["extraction"]["route"]
    native = record["native"]
    ocr = record["ocr"]
    conflicts = record["conflicts"]
    unresolved = record["unresolved"]
    if route == "native_bbox":
        words = native["words"]
        if record["extraction"]["native_probe"]["word_count"] != len(words):
            errors.append("/extraction/native_probe/word_count: does not match words")
        expected_orders = list(range(1, len(words) + 1))
        if [word["reading_order"] for word in words] != expected_orders:
            errors.append("/native/words: reading_order is not contiguous")
        if [word["word_index"] for word in words] != expected_orders:
            errors.append("/native/words: word_index is not contiguous")
        for index, word in enumerate(words):
            if word["word_id"] != f"word_{index + 1:06d}":
                errors.append(f"/native/words/{index}/word_id: identity mismatch")
            errors.extend(
                _bbox_errors(
                    word["bbox"],
                    page["width_pt"],
                    page["height_pt"],
                    f"/native/words/{index}/bbox",
                )
            )
    elif record["extraction"]["native_probe"]["word_count"] != 0:
        errors.append("/extraction/native_probe/word_count: non-native route must be zero")
    reading_lookup: dict[tuple[str, str], str] = {}
    if ocr is not None:
        run_ids: set[str] = set()
        for run_index, run in enumerate(ocr["raw_runs"]):
            if run["run_id"] in run_ids:
                errors.append(f"/ocr/raw_runs/{run_index}/run_id: duplicate")
            run_ids.add(run["run_id"])
            expected_orders = list(range(1, len(run["lines"]) + 1))
            if [line["reading_order"] for line in run["lines"]] != expected_orders:
                errors.append(
                    f"/ocr/raw_runs/{run_index}/lines: reading_order is not contiguous"
                )
            for line_index, line in enumerate(run["lines"]):
                errors.extend(
                    _bbox_errors(
                        line["bbox"],
                        1000,
                        1000,
                        f"/ocr/raw_runs/{run_index}/lines/{line_index}/bbox",
                    )
                )
                reading_lookup[(run["run_id"], line["line_id"])] = line["raw_text"]
    for collection_name, values in (
        ("conflicts", conflicts),
        ("unresolved", unresolved),
    ):
        for item_index, item in enumerate(values):
            if item["bbox"] is not None:
                errors.extend(
                    _bbox_errors(
                        item["bbox"],
                        1000,
                        1000,
                        f"/{collection_name}/{item_index}/bbox",
                    )
                )
            for reading_index, reading in enumerate(item["readings"]):
                key = (reading["run_id"], reading["line_id"])
                if reading_lookup.get(key) != reading["raw_text"]:
                    errors.append(
                        f"/{collection_name}/{item_index}/readings/{reading_index}: "
                        "does not bind to a raw OCR line"
                    )
    for index, conflict in enumerate(conflicts):
        if len({reading["raw_text"] for reading in conflict["readings"]}) < 2:
            errors.append(f"/conflicts/{index}: readings do not disagree")
    expected_status = (
        "needs_review"
        if route == "unresolved"
        or conflicts
        or unresolved
        or (
            ocr is not None
            and (
                ocr["upstream_status"] != "observed"
                or ocr["upstream_exactness"] != "observed"
            )
        )
        else "observed"
    )
    if record["status"] != expected_status:
        errors.append("/status: does not match route and unresolved state")
    if record["hashes"]["input_sha256"] != sha256_json(_input_payload(record)):
        errors.append("/hashes/input_sha256: mismatch")
    if record["hashes"]["content_sha256"] != sha256_json(_content_payload(record)):
        errors.append("/hashes/content_sha256: mismatch")
    expected_signature = sha256_json(_signature_payload(record))
    if record["hashes"]["signature_sha256"] != expected_signature:
        errors.append("/hashes/signature_sha256: mismatch")
    if record["observation_id"] != "pdfpage_" + expected_signature[:24]:
        errors.append("/observation_id: does not match signature")
    if record["hashes"]["record_integrity_sha256"] != sha256_json(
        record_integrity_payload(record)
    ):
        errors.append("/hashes/record_integrity_sha256: mismatch")
    return errors


def validate_observation(record: Mapping[str, Any]) -> list[str]:
    errors = _schema_errors(record, SCHEMA_PATH)
    if errors:
        return errors
    return semantic_errors(record)


def build_record(
    *,
    repository_root: Path,
    row: Mapping[str, Any],
    page_number: int,
    page_data: Mapping[str, Any],
    pdfinfo_data: Mapping[str, Any],
    pdfinfo_output_sha256: str,
    pdftotext_output_sha256: str,
    pdftotext_version: str,
    pdftotext_binary_sha256: str,
    inventory_sha256: str,
    visual_assets_sha256: str | None,
    ocr_observations_sha256: str | None,
    pdfinfo_version: str,
    pdfinfo_binary_sha256: str,
    manifests: Sequence[dict[str, Any]],
    observations: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    words = page_data["words"]
    if (
        abs(float(page_data["width_pt"]) - float(pdfinfo_data["width_pt"])) > 0.01
        or abs(float(page_data["height_pt"]) - float(pdfinfo_data["height_pt"]))
        > 0.01
    ):
        raise PDFObservationError(
            f"pdfinfo/pdftotext page dimension mismatch on page {page_number}"
        )
    native: dict[str, Any] | None
    ocr: dict[str, Any] | None
    conflicts: list[dict[str, Any]]
    unresolved: list[dict[str, Any]]
    if words:
        route = "native_bbox"
        route_reason = "pdftotext_words_present"
        native = {
            "granularity": "word",
            "raw_text_modified": False,
            "words": words,
        }
        ocr = None
        conflicts = []
        unresolved = []
    else:
        route, route_reason, ocr, conflicts, unresolved = build_ocr_route(
            repository_root=repository_root,
            row=row,
            page_number=page_number,
            manifests=manifests,
            observations=observations,
        )
        native = None
    page = {
        "page_number": page_number,
        "page_count": row["page_count"],
        "width_pt": page_data["width_pt"],
        "height_pt": page_data["height_pt"],
        "rotation_degrees": pdfinfo_data["rotation_degrees"],
        "coordinate_system": "top_left_pdf_points",
        "pdfinfo_output_sha256": pdfinfo_output_sha256,
    }
    status = (
        "needs_review"
        if route == "unresolved"
        or conflicts
        or unresolved
        or (
            ocr is not None
            and (
                ocr["upstream_status"] != "observed"
                or ocr["upstream_exactness"] != "observed"
            )
        )
        else "observed"
    )
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_type": RECORD_TYPE,
        "observation_id": "pdfpage_" + "0" * 24,
        "document_id": row["document_id"],
        "source": {
            "relative_path": row["relative_path"],
            "sha256": row["source_sha256"],
            "size_bytes": row["size_bytes"],
            "mime_type": "application/pdf",
        },
        "page": page,
        "extraction": {
            "route": route,
            "route_reason": route_reason,
            "selector": "native-first-no-mixing-v0.1",
            "native_probe": {
                "backend": "poppler-pdftotext-bbox-layout",
                "backend_version": pdftotext_version,
                "binary_sha256": pdftotext_binary_sha256,
                "document_output_sha256": pdftotext_output_sha256,
                "page_output_sha256": page_data["page_output_sha256"],
                "word_count": len(words),
            },
        },
        "native": native,
        "ocr": ocr,
        "conflicts": conflicts,
        "unresolved": unresolved,
        "status": status,
        "hashes": {
            "input_sha256": "0" * 64,
            "content_sha256": "0" * 64,
            "signature_sha256": "0" * 64,
            "record_integrity_sha256": "0" * 64,
        },
        "provenance": {
            "builder": BUILDER,
            "builder_version": BUILDER_VERSION,
            "inventory_sha256": inventory_sha256,
            "visual_assets_sha256": visual_assets_sha256,
            "ocr_observations_sha256": ocr_observations_sha256,
            "pdfinfo": {
                "backend": "poppler-pdfinfo",
                "backend_version": pdfinfo_version,
                "binary_sha256": pdfinfo_binary_sha256,
            },
            "question_independent": True,
            "question_data_used": False,
            "gold_data_used": False,
            "prediction_data_used": False,
            "answer_data_used": False,
            "shadow_only": True,
            "evidence_connected": False,
            "search_unit_connected": False,
            "production_index_connected": False,
        },
    }
    record = rehash_record(record)
    problems = validate_observation(record)
    if problems:
        raise PDFObservationError(
            "generated observation is invalid: " + "; ".join(problems[:20])
        )
    return record


def _verify_source(row: Mapping[str, Any], source_root: Path) -> Path:
    source_path = resolve_source_file(source_root, row["relative_path"])
    size = source_path.stat().st_size
    if size != row["size_bytes"]:
        raise PDFObservationError(
            f"source size mismatch for {row['relative_path']}"
        )
    if size > MAX_SOURCE_BYTES:
        raise PDFObservationError(
            f"source is too large: {row['relative_path']}"
        )
    if sha256_file(source_path) != row["source_sha256"]:
        raise PDFObservationError(
            f"source SHA-256 mismatch for {row['relative_path']}"
        )
    return source_path


def atomic_write_jsonl(
    path: Path, records: Sequence[Mapping[str, Any]], *, overwrite: bool
) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"output already exists: {path}")
    if path.is_symlink():
        raise PDFObservationError("output must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise PDFObservationError("output parent must not be a symlink")
    payload = "".join(canonical_json(record) + "\n" for record in records).encode(
        "utf-8"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists() and not overwrite:
            raise FileExistsError(f"output already exists: {path}")
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()


def build(
    *,
    repository_root: Path,
    source_root: Path,
    inventory_path: Path,
    output_path: Path,
    visual_assets_path: Path | None = None,
    ocr_observations_path: Path | None = None,
    pdfinfo_command: str = "pdfinfo",
    pdftotext_command: str = "pdftotext",
    overwrite: bool = False,
) -> dict[str, Any]:
    repository_root = resolve_trusted_root(repository_root, "repository root")
    source_root = resolve_trusted_root(source_root, "source root")
    inventory_path = resolve_repo_input(
        repository_root, inventory_path, "inventory"
    )
    output_path = resolve_repo_output(repository_root, output_path)
    visual_records: list[dict[str, Any]] = []
    ocr_records: list[dict[str, Any]] = []
    visual_assets_sha256: str | None = None
    ocr_observations_sha256: str | None = None
    if visual_assets_path is not None:
        visual_assets_path = resolve_repo_input(
            repository_root, visual_assets_path, "visual assets"
        )
        visual_records, visual_assets_sha256 = load_jsonl(
            visual_assets_path, "visual assets"
        )
        validate_visual_inputs(visual_records, visual_assets_path)
    if ocr_observations_path is not None:
        ocr_observations_path = resolve_repo_input(
            repository_root, ocr_observations_path, "OCR observations"
        )
        ocr_records, ocr_observations_sha256 = load_jsonl(
            ocr_observations_path, "OCR observations"
        )
        validate_ocr_inputs(ocr_records)
    manifest_index = _index_pdf_records(visual_records, label="visual assets")
    ocr_index = _index_pdf_records(ocr_records, label="OCR observations")
    rows, inventory_sha256 = read_inventory(inventory_path)
    pdfinfo = resolve_executable(pdfinfo_command, "pdfinfo")
    pdftotext = resolve_executable(pdftotext_command, "pdftotext")
    pdfinfo_version = command_version(pdfinfo, "pdfinfo")
    pdftotext_version = command_version(pdftotext, "pdftotext")
    pdfinfo_binary_sha256 = sha256_file(pdfinfo)
    pdftotext_binary_sha256 = sha256_file(pdftotext)
    records: list[dict[str, Any]] = []
    for row in rows:
        source_path = _verify_source(row, source_root)
        pdfinfo_output = run_bounded(
            [
                str(pdfinfo),
                "-f",
                "1",
                "-l",
                str(row["page_count"]),
                str(source_path),
            ],
            f"pdfinfo {row['relative_path']}",
        )
        pdfinfo_pages, pdfinfo_output_sha256 = parse_pdfinfo(
            pdfinfo_output, expected_pages=row["page_count"]
        )
        pdftotext_output = run_bounded(
            [str(pdftotext), "-bbox-layout", str(source_path), "-"],
            f"pdftotext {row['relative_path']}",
        )
        parsed_pages = parse_bbox_pages(
            pdftotext_output, expected_pages=row["page_count"]
        )
        pdftotext_output_sha256 = sha256_bytes(pdftotext_output)
        for page_data in parsed_pages:
            page_number = page_data["page_number"]
            key = (row["relative_path"], row["source_sha256"], page_number)
            records.append(
                build_record(
                    repository_root=repository_root,
                    row=row,
                    page_number=page_number,
                    page_data=page_data,
                    pdfinfo_data=pdfinfo_pages[page_number],
                    pdfinfo_output_sha256=pdfinfo_output_sha256,
                    pdftotext_output_sha256=pdftotext_output_sha256,
                    pdftotext_version=pdftotext_version,
                    pdftotext_binary_sha256=pdftotext_binary_sha256,
                    inventory_sha256=inventory_sha256,
                    visual_assets_sha256=visual_assets_sha256,
                    ocr_observations_sha256=ocr_observations_sha256,
                    pdfinfo_version=pdfinfo_version,
                    pdfinfo_binary_sha256=pdfinfo_binary_sha256,
                    manifests=manifest_index.get(key, []),
                    observations=ocr_index.get(key, []),
                )
            )
    expected_records = sum(row["page_count"] for row in rows)
    if len(records) != expected_records:
        raise PDFObservationError(
            f"page coverage mismatch: expected {expected_records}, got {len(records)}"
        )
    identities = {
        (
            record["document_id"],
            record["source"]["relative_path"],
            record["source"]["sha256"],
            record["page"]["page_number"],
        )
        for record in records
    }
    if len(identities) != len(records):
        raise PDFObservationError("duplicate source/page observation")
    atomic_write_jsonl(output_path, records, overwrite=overwrite)
    counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    for record in records:
        route = record["extraction"]["route"]
        counts[route] = counts.get(route, 0) + 1
        status = record["status"]
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "documents": len(rows),
        "pages": len(records),
        "routes": dict(sorted(counts.items())),
        "statuses": dict(sorted(status_counts.items())),
        "output": str(output_path),
        "output_sha256": sha256_file(output_path),
        "shadow_only": True,
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--repository-root", type=Path, default=ROOT)
    value.add_argument("--source-root", type=Path, required=True)
    value.add_argument("--inventory", type=Path, required=True)
    value.add_argument("--visual-assets", type=Path)
    value.add_argument("--ocr-observations", type=Path)
    value.add_argument("--output", type=Path, required=True)
    value.add_argument("--pdfinfo", default="pdfinfo")
    value.add_argument("--pdftotext", default="pdftotext")
    value.add_argument("--overwrite", action="store_true")
    return value


def main() -> int:
    args = parser().parse_args()
    summary = build(
        repository_root=args.repository_root,
        source_root=args.source_root,
        inventory_path=args.inventory,
        visual_assets_path=args.visual_assets,
        ocr_observations_path=args.ocr_observations,
        output_path=args.output,
        pdfinfo_command=args.pdfinfo,
        pdftotext_command=args.pdftotext,
        overwrite=args.overwrite,
    )
    print(canonical_json(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
