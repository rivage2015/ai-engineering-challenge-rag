"""Fail-closed version diffs for embedded notebook statistics tables.

This rule intentionally certifies a narrow, source-derived change.  A matched
question names two notebooks.  The notebooks must be structurally identical
apart from one markdown cell's id and one embedded PNG.  That PNG must be a
wrapped statistics table whose existing header glyphs are unchanged and whose
new version contains exactly one appended header slot.  The appended slot is
named only when the notebook-bound CSV schema and four Tesseract
page-segmentation readings of that slot agree exactly after one fixed
normalization.
"""

from __future__ import annotations

import ast
import base64
import binascii
import codecs
import csv
import hashlib
import io
import json
import math
import re
import shutil
import struct
import subprocess
import unicodedata
import zlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from structured_candidate import (
    StructuredCandidateAnswer,
    StructuredCandidateDecision,
    _candidate_values,
    _location_matches,
)


NOTEBOOK_VERSION_DIFF_RULE_VERSION = "0.2"

NOTEBOOK_CONTENT_DIFF = re.compile(
    r"^(?P<location>[^\r\n/\\]{1,180}?)の"
    r"(?P<before>[^\r\n/\\,、。]{1,180}\.ipynb)から"
    r"(?P<after>[^\r\n/\\,、。]{1,180}\.ipynb)への変更内容のうち、"
    r"内容として変わっている点は何ですか[。．]?$",
    flags=re.IGNORECASE,
)

_DATA_URI_MARKDOWN = re.compile(
    r"!\[[^\]\r\n]{0,128}\]\(data:image/png;base64,"
    r"(?P<payload>[A-Za-z0-9+/]+={0,2})\)\n?\Z"
)
_BASIC_STATISTICS_HEADING = re.compile(r"^#{1,6}\s*基本統計量\s*$")
_SAFE_ENCODINGS = frozenset(
    {"utf-8-sig", "utf-8", "cp932", "shift_jis", "euc_jp"}
)

_MAX_NOTEBOOK_BYTES = 16 * 1024 * 1024
_MAX_CELLS = 1_024
_MAX_SOURCE_CHARS = 12 * 1024 * 1024
_MAX_DATA_URI_CHARS = 12 * 1024 * 1024
_MAX_PNG_BYTES = 10 * 1024 * 1024
_MAX_PNG_CHUNKS = 2_048
_MAX_PNG_CHUNK_BYTES = 10 * 1024 * 1024
_MAX_PNG_DIMENSION = 8_192
_MAX_PNG_PIXELS = 20_000_000
_MAX_DECODED_PNG_BYTES = 96 * 1024 * 1024
_MAX_CSV_BYTES = 64 * 1024 * 1024
_MAX_CSV_ROWS = 1_000_000
_MAX_CSV_COLUMNS = 20_000
_MAX_HEADER_CROP_PIXELS = 2_000_000
_HEADER_CROP_SCALE = 2
_HEADER_OCR_PSMS = (3, 6, 7, 10)
_HEADER_OCR_TIMEOUT_SECONDS = 20
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class _InvalidSource(ValueError):
    pass


@dataclass(frozen=True)
class _PngImage:
    width: int
    height: int
    rgba: bytes


@dataclass(frozen=True)
class _PixelBox:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(frozen=True)
class _HeaderEvidence:
    count: int
    slots_per_block: int
    masks: tuple[frozenset[tuple[int, int]], ...]
    occupied_boxes: tuple[_PixelBox, ...]


@dataclass(frozen=True)
class _NotebookDiff:
    before_png: bytes
    after_png: bytes
    code_source: str


@dataclass(frozen=True)
class _DatasetBinding:
    relative_path: PurePosixPath
    target_column: str
    encodings: tuple[str, ...]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _normalized(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value)).casefold().strip()


def _strict_json_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping):
        return left.keys() == right.keys() and all(
            _strict_json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _strict_json_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def _source_root(engine: Any) -> Path | None:
    try:
        root = Path(engine.source_root)
        if not root.is_dir() or root.is_symlink():
            return None
        return root.resolve()
    except (AttributeError, OSError, RuntimeError, TypeError):
        return None


def _has_symlink_component(path: Path, root: Path) -> bool:
    current = path
    while True:
        if current.is_symlink():
            return True
        if current == root:
            return False
        if root not in current.parents:
            return True
        current = current.parent


def _named_notebooks(engine: Any, location: str, filename: str) -> tuple[Path, ...]:
    root = _source_root(engine)
    if root is None:
        return ()
    locations = _candidate_values(location, getattr(engine, "glossary", None))
    names = {
        _normalized(value)
        for value in _candidate_values(filename, getattr(engine, "glossary", None))
    }
    matches: list[Path] = []
    try:
        for path in root.rglob("*.ipynb"):
            if (
                not path.is_file()
                or path.is_symlink()
                or path.name.startswith((".", "~$"))
                or _has_symlink_component(path, root)
                or _normalized(path.name) not in names
            ):
                continue
            relative = path.relative_to(root)
            if not _location_matches(relative.parts[:-1], locations):
                continue
            size = path.stat().st_size
            if 0 < size <= _MAX_NOTEBOOK_BYTES:
                matches.append(path.resolve())
    except OSError:
        return ()
    return tuple(sorted(set(matches), key=lambda item: item.as_posix()))


def _contract(question: str, bindings: Mapping[str, str]) -> dict[str, Any]:
    operators = (
        "retrieve_unique_notebook_pair",
        "parse_strict_notebook_json",
        "verify_single_embedded_image_cell_diff",
        "decode_bounded_png",
        "measure_wrapped_statistics_headers",
        "extract_dataset_binding_from_notebook_source",
        "validate_bound_dataset_numeric_schema",
        "derive_single_appended_column",
        "ocr_appended_header_crop_multipsm",
        "verify_header_text_matches_source_column",
        "project_content_change",
    )
    nodes: list[dict[str, Any]] = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output_ref = f"value_{index:03d}"
        nodes.append(
            {
                "operation_id": f"op_{index:03d}_{operator}",
                "operator": operator,
                "input_refs": [previous],
                "output_ref": output_ref,
            }
        )
        previous = output_ref
    core = {
        "graph_rule_version": NOTEBOOK_VERSION_DIFF_RULE_VERSION,
        "rule_id": "notebook_version_embedded_statistics_diff",
        "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
        "bindings": dict(bindings),
        "scope": {
            "location": bindings["location"],
            "before": bindings["before"],
            "after": bindings["after"],
            "direction": "question_declared_before_to_after",
            "notebook_comparison": "strict_json_single_markdown_png_cell",
            "image_evidence": "bounded_png_wrapped_table_header_geometry",
            "column_name_evidence": (
                "notebook_ast_bound_csv_order_and_appended_header_ocr_agreement"
            ),
            "header_ocr": {
                "engine": "tesseract_jpn_eng_oem1",
                "page_segmentation_modes": list(_HEADER_OCR_PSMS),
                "crop_scale": _HEADER_CROP_SCALE,
                "normalization": (
                    "nfkc_remove_unicode_whitespace_case_sensitive_exact"
                ),
                "consensus": "all_readings_must_match_source_column",
            },
            "ambiguity_policy": "hold",
        },
        "operation_graph": {
            "external_inputs": [
                {
                    "input_ref": "input_question",
                    "input_type": "source_records",
                    "source": "question_scope",
                }
            ],
            "nodes": nodes,
            "edges": [
                {"from": nodes[index - 1]["output_ref"], "to": nodes[index]["operation_id"]}
                for index in range(1, len(nodes))
            ],
        },
        "requested_output": {
            "source_operation_ref": nodes[-1]["operation_id"],
            "cardinality": "single",
            "answer_shape": {
                "container": "scalar",
                "value_type": "string",
                "unit": None,
            },
            "display_precision": None,
            "required_keys": ["appended_column"],
        },
    }
    return {
        "graph_contract_id": "notebook_version_diff_"
        + hashlib.sha256(_canonical_json(core).encode("utf-8")).hexdigest()[:32],
        **core,
    }


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if not isinstance(question, str):
        return None
    match = NOTEBOOK_CONTENT_DIFF.fullmatch(question)
    if match is None:
        return None
    bindings = {
        key: match[key] for key in ("location", "before", "after")
    }
    if _normalized(bindings["before"]) == _normalized(bindings["after"]):
        return None
    return _contract(question, bindings)


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    if expected is None or not isinstance(contract, Mapping):
        return False
    try:
        return _canonical_json(expected) == _canonical_json(contract)
    except (TypeError, ValueError):
        return False


def _reject_duplicate_pairs(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidSource("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise _InvalidSource(f"invalid JSON constant: {value}")


def _cell_source(cell: Mapping[str, Any]) -> str:
    source = cell.get("source")
    if isinstance(source, str):
        rendered = source
    elif isinstance(source, list) and all(isinstance(item, str) for item in source):
        rendered = "".join(source)
    else:
        raise _InvalidSource("notebook cell source is invalid")
    if len(rendered) > _MAX_SOURCE_CHARS:
        raise _InvalidSource("notebook cell source is too large")
    return rendered


def _read_notebook(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if not 0 < len(raw) <= _MAX_NOTEBOOK_BYTES:
        raise _InvalidSource("notebook size is invalid")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _InvalidSource("notebook is not UTF-8 JSON") from exc
    value = json.loads(
        text,
        object_pairs_hook=_reject_duplicate_pairs,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(value, dict) or set(value) != {
        "cells",
        "metadata",
        "nbformat",
        "nbformat_minor",
    }:
        raise _InvalidSource("notebook root shape is unsupported")
    if value["nbformat"] != 4 or not isinstance(value["nbformat_minor"], int):
        raise _InvalidSource("notebook format is unsupported")
    cells = value["cells"]
    if not isinstance(cells, list) or not 1 <= len(cells) <= _MAX_CELLS:
        raise _InvalidSource("notebook cell count is invalid")
    for cell in cells:
        if not isinstance(cell, dict) or cell.get("cell_type") not in {
            "code",
            "markdown",
            "raw",
        }:
            raise _InvalidSource("notebook cell is invalid")
        _cell_source(cell)
    return value


def _embedded_png(source: str) -> bytes:
    if len(source) > _MAX_DATA_URI_CHARS:
        raise _InvalidSource("embedded image is too large")
    match = _DATA_URI_MARKDOWN.fullmatch(source)
    if match is None:
        raise _InvalidSource("markdown cell is not one embedded PNG")
    payload = match["payload"]
    if len(payload) % 4 != 0:
        raise _InvalidSource("base64 length is invalid")
    try:
        decoded = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _InvalidSource("base64 is invalid") from exc
    if not 0 < len(decoded) <= _MAX_PNG_BYTES:
        raise _InvalidSource("embedded PNG size is invalid")
    return decoded


def _compare_notebooks(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> _NotebookDiff:
    for key in ("metadata", "nbformat", "nbformat_minor"):
        if not _strict_json_equal(before[key], after[key]):
            raise _InvalidSource("notebook root metadata changed")
    before_cells = before["cells"]
    after_cells = after["cells"]
    if len(before_cells) != len(after_cells):
        raise _InvalidSource("notebook cell count changed")
    changed = [
        index
        for index, (left, right) in enumerate(zip(before_cells, after_cells))
        if not _strict_json_equal(left, right)
    ]
    if len(changed) != 1:
        raise _InvalidSource("notebook does not have one changed cell")
    index = changed[0]
    if index == 0:
        raise _InvalidSource("embedded table has no heading")
    left = before_cells[index]
    right = after_cells[index]
    required_keys = {"cell_type", "metadata", "source", "id"}
    if set(left) != required_keys or set(right) != required_keys:
        raise _InvalidSource("changed cell shape is unsupported")
    if left["cell_type"] != "markdown" or right["cell_type"] != "markdown":
        raise _InvalidSource("changed cell is not markdown")
    if not _strict_json_equal(left["metadata"], right["metadata"]):
        raise _InvalidSource("changed cell metadata differs")
    if (
        not isinstance(left["id"], str)
        or not isinstance(right["id"], str)
        or not left["id"]
        or not right["id"]
        or left["id"] == right["id"]
    ):
        raise _InvalidSource("changed cell id relation is invalid")
    left_without = {key: value for key, value in left.items() if key not in {"id", "source"}}
    right_without = {key: value for key, value in right.items() if key not in {"id", "source"}}
    if not _strict_json_equal(left_without, right_without):
        raise _InvalidSource("changed cell contains another difference")

    heading_cell = before_cells[index - 1]
    if (
        heading_cell.get("cell_type") != "markdown"
        or not _BASIC_STATISTICS_HEADING.fullmatch(_cell_source(heading_cell).strip())
        or not _strict_json_equal(heading_cell, after_cells[index - 1])
    ):
        raise _InvalidSource("embedded table is not under the statistics heading")

    before_source = _cell_source(left)
    after_source = _cell_source(right)
    before_png = _embedded_png(before_source)
    after_png = _embedded_png(after_source)
    if before_png == after_png:
        raise _InvalidSource("embedded PNG did not change")

    for cells in (before_cells, after_cells):
        embedded = 0
        for cell in cells:
            source = _cell_source(cell)
            if "data:image/png;base64," in source:
                if _DATA_URI_MARKDOWN.fullmatch(source) is None:
                    raise _InvalidSource("unsupported embedded PNG occurrence")
                embedded += 1
        if embedded != 1:
            raise _InvalidSource("embedded PNG is not unique")

    code_sources = [
        _cell_source(cell) for cell in after_cells if cell.get("cell_type") == "code"
    ]
    if not code_sources:
        raise _InvalidSource("notebook has no code source")
    return _NotebookDiff(before_png, after_png, "\n\n".join(code_sources))


def _decode_png(data: bytes) -> _PngImage:
    if not 0 < len(data) <= _MAX_PNG_BYTES or not data.startswith(_PNG_SIGNATURE):
        raise _InvalidSource("PNG signature or size is invalid")
    offset = len(_PNG_SIGNATURE)
    chunks = 0
    width = height = 0
    idat_parts: list[bytes] = []
    seen_ihdr = False
    seen_idat = False
    idat_closed = False
    seen_iend = False
    while offset < len(data):
        if chunks >= _MAX_PNG_CHUNKS or offset + 12 > len(data):
            raise _InvalidSource("PNG chunk table is invalid")
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        if length > _MAX_PNG_CHUNK_BYTES or offset + 12 + length > len(data):
            raise _InvalidSource("PNG chunk length is invalid")
        payload_start = offset + 8
        payload_end = payload_start + length
        payload = data[payload_start:payload_end]
        expected_crc = struct.unpack(">I", data[payload_end : payload_end + 4])[0]
        actual_crc = binascii.crc32(chunk_type)
        actual_crc = binascii.crc32(payload, actual_crc) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise _InvalidSource("PNG chunk CRC is invalid")
        chunks += 1
        offset = payload_end + 4

        if chunks == 1:
            if chunk_type != b"IHDR" or length != 13:
                raise _InvalidSource("PNG IHDR is invalid")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            if (
                not 1 <= width <= _MAX_PNG_DIMENSION
                or not 1 <= height <= _MAX_PNG_DIMENSION
                or width * height > _MAX_PNG_PIXELS
                or depth != 8
                or color != 6
                or compression != 0
                or filtering != 0
                or interlace != 0
            ):
                raise _InvalidSource("PNG pixel format is unsupported")
            seen_ihdr = True
            continue
        if chunk_type == b"IHDR":
            raise _InvalidSource("PNG contains multiple IHDR chunks")
        if chunk_type == b"IDAT":
            if not seen_ihdr or idat_closed or seen_iend:
                raise _InvalidSource("PNG IDAT order is invalid")
            seen_idat = True
            idat_parts.append(payload)
            continue
        if seen_idat:
            idat_closed = True
        if chunk_type == b"IEND":
            if length != 0 or not seen_idat or seen_iend or offset != len(data):
                raise _InvalidSource("PNG IEND is invalid")
            seen_iend = True
            break
        # Reject unknown critical chunks; bounded ancillary metadata is ignored.
        if not chunk_type or not (chunk_type[0] & 0x20):
            raise _InvalidSource("PNG contains an unsupported critical chunk")
    if not seen_ihdr or not seen_idat or not seen_iend:
        raise _InvalidSource("PNG is incomplete")

    row_bytes = width * 4
    expected_size = (row_bytes + 1) * height
    if expected_size > _MAX_DECODED_PNG_BYTES:
        raise _InvalidSource("decoded PNG exceeds the limit")
    compressed = b"".join(idat_parts)
    decompressor = zlib.decompressobj()
    try:
        filtered = decompressor.decompress(compressed, expected_size + 1)
    except zlib.error as exc:
        raise _InvalidSource("PNG deflate stream is invalid") from exc
    if (
        len(filtered) > expected_size
        or decompressor.unconsumed_tail
        or not decompressor.eof
        or decompressor.unused_data
    ):
        raise _InvalidSource("PNG deflate bounds are invalid")
    try:
        filtered += decompressor.flush()
    except zlib.error as exc:
        raise _InvalidSource("PNG deflate flush failed") from exc
    if len(filtered) != expected_size:
        raise _InvalidSource("PNG decoded size is invalid")

    decoded = bytearray()
    decoded_extend = decoded.extend
    previous = bytearray(row_bytes)
    cursor = 0
    for _ in range(height):
        filter_type = filtered[cursor]
        scanline = bytearray(filtered[cursor + 1 : cursor + 1 + row_bytes])
        cursor += row_bytes + 1
        if filter_type == 0:
            pass
        elif filter_type == 1:
            for index in range(4, row_bytes):
                scanline[index] = (scanline[index] + scanline[index - 4]) & 0xFF
        elif filter_type == 2:
            for index in range(row_bytes):
                scanline[index] = (scanline[index] + previous[index]) & 0xFF
        elif filter_type in {3, 4}:
            for index in range(row_bytes):
                left = scanline[index - 4] if index >= 4 else 0
                above = previous[index]
                if filter_type == 3:
                    predictor = (left + above) // 2
                else:
                    upper_left = previous[index - 4] if index >= 4 else 0
                    p = left + above - upper_left
                    pa = abs(p - left)
                    pb = abs(p - above)
                    pc = abs(p - upper_left)
                    predictor = (
                        left
                        if pa <= pb and pa <= pc
                        else above if pb <= pc else upper_left
                    )
                scanline[index] = (scanline[index] + predictor) & 0xFF
        else:
            raise _InvalidSource("PNG row filter is invalid")
        decoded_extend(scanline)
        previous = scanline
    return _PngImage(width, height, bytes(decoded))


def _runs(values: Sequence[int]) -> tuple[tuple[int, int], ...]:
    result: list[tuple[int, int]] = []
    for value in values:
        if not result or value > result[-1][1] + 1:
            result.append((value, value))
        else:
            result[-1] = (result[-1][0], value)
    return tuple(result)


def _horizontal_grid_runs(image: _PngImage) -> tuple[tuple[int, int], ...]:
    rows: list[int] = []
    stride = image.width * 4
    minimum = math.ceil(image.width * 0.90)
    for y in range(image.height):
        row = image.rgba[y * stride : (y + 1) * stride]
        alpha = row[3::4]
        if len(alpha) - alpha.count(0) >= minimum:
            rows.append(y)
    runs = _runs(rows)
    if len(runs) < 4:
        raise _InvalidSource("PNG does not contain a table grid")
    return runs


def _vertical_grid_runs(
    image: _PngImage, horizontal: Sequence[tuple[int, int]]
) -> tuple[tuple[int, int], ...]:
    y0 = horizontal[0][1] + 1
    y1 = horizontal[1][0]
    if y1 - y0 < 3:
        raise _InvalidSource("PNG header band is too small")
    minimum = math.ceil((y1 - y0) * 0.70)
    columns: list[int] = []
    stride = image.width * 4
    for x in range(image.width):
        present = 0
        offset = x * 4
        for y in range(y0, y1):
            base = y * stride + offset
            red, green, blue, alpha = image.rgba[base : base + 4]
            if alpha >= 16 and min(red, green, blue) >= 150:
                present += 1
        if present >= minimum:
            columns.append(x)
    runs = _runs(columns)
    if len(runs) < 5 or len(runs) % 2 != 1:
        raise _InvalidSource("PNG vertical table grid is unsupported")
    return runs


def _black_points(
    image: _PngImage, x0: int, x1: int, y0: int, y1: int
) -> tuple[tuple[int, int], ...]:
    points: list[tuple[int, int]] = []
    stride = image.width * 4
    for y in range(y0, y1):
        offset = y * stride + x0 * 4
        for x in range(x0, x1):
            red, green, blue, alpha = image.rgba[offset : offset + 4]
            if alpha >= 16 and max(red, green, blue) < 128:
                points.append((x, y))
            offset += 4
    return tuple(points)


def _normalize_points(points: Sequence[tuple[int, int]]) -> frozenset[tuple[int, int]]:
    if len(points) < 4:
        raise _InvalidSource("header glyph is too small")
    min_x = min(x for x, _ in points)
    max_x = max(x for x, _ in points)
    min_y = min(y for _, y in points)
    max_y = max(y for _, y in points)
    if max_x == min_x or max_y == min_y:
        raise _InvalidSource("header glyph has no two-dimensional extent")
    return frozenset(
        (
            round((x - min_x) * 127 / (max_x - min_x)),
            round((y - min_y) * 31 / (max_y - min_y)),
        )
        for x, y in points
    )


def _header_evidence(image: _PngImage, expected_count: int) -> _HeaderEvidence:
    if expected_count < 2:
        raise _InvalidSource("statistics column count is too small")
    horizontal = _horizontal_grid_runs(image)
    vertical = _vertical_grid_runs(image, horizontal)
    slots = (len(vertical) - 1) // 2
    if not 2 <= slots <= 100:
        raise _InvalidSource("statistics slots per block are invalid")
    blocks = math.ceil(expected_count / slots)
    if blocks < 1 or len(horizontal) % blocks != 0:
        raise _InvalidSource("wrapped table block geometry is invalid")
    stride = len(horizontal) // blocks
    if stride < 3:
        raise _InvalidSource("wrapped table row geometry is invalid")

    masks: list[frozenset[tuple[int, int]]] = []
    occupied_boxes: list[_PixelBox] = []
    for block in range(blocks):
        header_index = block * stride
        if header_index + 1 >= len(horizontal):
            raise _InvalidSource("wrapped table header index is invalid")
        y0 = horizontal[header_index][1] + 1
        y1 = horizontal[header_index + 1][0]
        occupied_in_block = min(slots, expected_count - block * slots)
        seen_empty = False
        for slot in range(slots):
            x0 = vertical[slot * 2][1] + 1
            x1 = vertical[slot * 2 + 2][0]
            points = _black_points(image, x0, x1, y0, y1)
            occupied = len(points) >= 4
            if 1 <= len(points) < 4:
                raise _InvalidSource("header cell occupancy is ambiguous")
            if slot < occupied_in_block:
                if not occupied or seen_empty:
                    raise _InvalidSource("statistics headers are not contiguous")
                masks.append(_normalize_points(points))
                occupied_boxes.append(_PixelBox(x0, y0, x1, y1))
            else:
                seen_empty = True
                if occupied:
                    raise _InvalidSource("statistics table has unexpected header content")
    if len(masks) != expected_count:
        raise _InvalidSource("statistics header count is invalid")
    return _HeaderEvidence(
        expected_count,
        slots,
        tuple(masks),
        tuple(occupied_boxes),
    )


def _shape_similarity(
    left: frozenset[tuple[int, int]], right: frozenset[tuple[int, int]]
) -> float:
    offsets = tuple(
        (dx, dy)
        for dx in range(-2, 3)
        for dy in range(-2, 3)
        if dx * dx + dy * dy <= 4
    )

    def directional(
        source: frozenset[tuple[int, int]],
        target: frozenset[tuple[int, int]],
    ) -> float:
        matches = sum(
            any((x + dx, y + dy) in target for dx, dy in offsets)
            for x, y in source
        )
        return matches / len(source)

    forward = directional(left, right)
    backward = directional(right, left)
    if forward + backward == 0:
        return 0.0
    return 2 * forward * backward / (forward + backward)


def _verify_appended_header(before: _HeaderEvidence, after: _HeaderEvidence) -> None:
    if (
        after.count != before.count + 1
        or after.slots_per_block != before.slots_per_block
    ):
        raise _InvalidSource("statistics header count did not append by one")
    for left, right in zip(before.masks, after.masks[: before.count]):
        if _shape_similarity(left, right) < 0.84:
            raise _InvalidSource("an existing statistics header changed")


def _header_crop_pgm(image: _PngImage, box: _PixelBox) -> bytes:
    """Return a bounded 2x grayscale crop, composited onto opaque white."""

    if (
        box.left < 0
        or box.top < 0
        or box.right > image.width
        or box.bottom > image.height
        or box.width <= 0
        or box.height <= 0
        or len(image.rgba) != image.width * image.height * 4
    ):
        raise _InvalidSource("appended header crop bounds are invalid")
    output_width = box.width * _HEADER_CROP_SCALE
    output_height = box.height * _HEADER_CROP_SCALE
    if output_width * output_height > _MAX_HEADER_CROP_PIXELS:
        raise _InvalidSource("appended header crop is too large")

    stride = image.width * 4
    pixels = bytearray()
    for y in range(box.top, box.bottom):
        scaled_row = bytearray()
        for x in range(box.left, box.right):
            offset = y * stride + x * 4
            red, green, blue, alpha = image.rgba[offset : offset + 4]
            luma = (red * 299 + green * 587 + blue * 114) // 1000
            gray = (luma * alpha + 255 * (255 - alpha)) // 255
            scaled_row.extend((gray,) * _HEADER_CROP_SCALE)
        for _ in range(_HEADER_CROP_SCALE):
            pixels.extend(scaled_row)
    header = f"P5\n{output_width} {output_height}\n255\n".encode("ascii")
    return header + bytes(pixels)


def _tesseract_header_reading(pgm: bytes, psm: int) -> str | None:
    """Production OCR adapter; any execution or decoding anomaly fails closed."""

    executable = shutil.which("tesseract")
    if executable is None or psm not in _HEADER_OCR_PSMS:
        return None
    try:
        completed = subprocess.run(
            [
                executable,
                "stdin",
                "stdout",
                "-l",
                "jpn+eng",
                "--oem",
                "1",
                "--psm",
                str(psm),
            ],
            input=pgm,
            capture_output=True,
            check=False,
            timeout=_HEADER_OCR_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0 or not isinstance(completed.stdout, bytes):
            return None
        if not completed.stdout or len(completed.stdout) > 64 * 1024:
            return None
        reading = completed.stdout.decode("utf-8", errors="strict")
        if "\x00" in reading or not reading:
            return None
        return reading
    except (OSError, UnicodeDecodeError, subprocess.SubprocessError):
        return None


def _header_token(value: str) -> str:
    """NFKC and remove Unicode whitespace; preserve case for exact matching."""

    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value)
        if not character.isspace()
    )


def _added_header_matches_target(
    image: _PngImage,
    evidence: _HeaderEvidence,
    target_column: str,
) -> bool:
    if (
        len(evidence.occupied_boxes) != evidence.count
        or not evidence.occupied_boxes
        or not isinstance(target_column, str)
    ):
        return False
    target_token = _header_token(target_column)
    if not target_token:
        return False
    pgm = _header_crop_pgm(image, evidence.occupied_boxes[-1])
    readings = tuple(
        _tesseract_header_reading(pgm, psm) for psm in _HEADER_OCR_PSMS
    )
    if any(not isinstance(reading, str) or not reading for reading in readings):
        return False
    return all(_header_token(reading) == target_token for reading in readings)


def _literal_path_assignment(tree: ast.AST) -> PurePosixPath:
    values: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        value = node.value
        if (
            isinstance(target, ast.Name)
            and target.id == "csv_rel"
            and isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "Path"
            and len(value.args) == 1
            and not value.keywords
            and isinstance(value.args[0], ast.Constant)
            and isinstance(value.args[0].value, str)
        ):
            values.append(value.args[0].value)
    if len(values) != 1:
        raise _InvalidSource("CSV relative path is not unique")
    rendered = values[0]
    pure = PurePosixPath(rendered)
    if (
        not rendered
        or "\\" in rendered
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.suffix.casefold() != ".csv"
    ):
        raise _InvalidSource("CSV relative path is unsafe")
    return pure


def _literal_target_assignment(tree: ast.AST) -> str:
    values: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        value = node.value
        if (
            isinstance(target, ast.Name)
            and target.id == "target_col"
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            values.append(value.value)
    if len(values) != 1 or not values[0].strip() or len(values[0]) > 256:
        raise _InvalidSource("target column literal is not unique")
    return values[0]


def _encoding_assignment(tree: ast.AST) -> tuple[str, ...]:
    assignments: list[tuple[str, ...]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        value = node.value
        if not isinstance(target, ast.Name) or target.id != "ENCODINGS":
            continue
        if not isinstance(value, (ast.Tuple, ast.List)) or not all(
            isinstance(item, ast.Constant) and isinstance(item.value, str)
            for item in value.elts
        ):
            raise _InvalidSource("encoding declaration is invalid")
        assignments.append(tuple(item.value for item in value.elts))
    if len(assignments) != 1 or not 1 <= len(assignments[0]) <= 8:
        raise _InvalidSource("encoding declaration is not unique")
    normalized: list[str] = []
    for value in assignments[0]:
        try:
            canonical = codecs.lookup(value).name
        except LookupError as exc:
            raise _InvalidSource("unknown CSV encoding") from exc
        if _normalized(value).replace("_", "-") not in {
            item.replace("_", "-") for item in _SAFE_ENCODINGS
        }:
            raise _InvalidSource("CSV encoding is outside the safe set")
        if canonical not in normalized:
            normalized.append(canonical)
    return tuple(normalized)


def _dataset_binding(code_source: str) -> _DatasetBinding:
    try:
        tree = ast.parse(code_source)
    except SyntaxError as exc:
        raise _InvalidSource("notebook code is not valid Python") from exc
    return _DatasetBinding(
        _literal_path_assignment(tree),
        _literal_target_assignment(tree),
        _encoding_assignment(tree),
    )


def _bound_dataset_path(notebook: Path, root: Path, relative: PurePosixPath) -> Path:
    try:
        project = notebook.parent.parent.resolve(strict=True)
        candidate = project.joinpath(*relative.parts)
        if (
            not candidate.is_file()
            or candidate.is_symlink()
            or _has_symlink_component(candidate, project)
            or root not in candidate.resolve().parents
            or not 0 < candidate.stat().st_size <= _MAX_CSV_BYTES
        ):
            raise _InvalidSource("bound CSV is unsafe or missing")
        return candidate.resolve()
    except OSError as exc:
        raise _InvalidSource("bound CSV cannot be inspected") from exc


def _decode_csv(raw: bytes, encodings: Sequence[str]) -> str:
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\x00" not in text:
            return text
    raise _InvalidSource("CSV cannot be decoded with notebook encodings")


def _numeric_column_order(path: Path, encodings: Sequence[str]) -> tuple[str, ...]:
    raw = path.read_bytes()
    if not 0 < len(raw) <= _MAX_CSV_BYTES:
        raise _InvalidSource("CSV size is invalid")
    text = _decode_csv(raw, encodings)
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(reader)
    except StopIteration as exc:
        raise _InvalidSource("CSV has no header") from exc
    if (
        not 2 <= len(header) <= _MAX_CSV_COLUMNS
        or any(not value.strip() for value in header)
        or len({_normalized(value) for value in header}) != len(header)
    ):
        raise _InvalidSource("CSV header is invalid")
    possible = [True] * len(header)
    seen = [False] * len(header)
    row_count = 0
    for row in reader:
        row_count += 1
        if row_count > _MAX_CSV_ROWS or len(row) != len(header):
            raise _InvalidSource("CSV row shape is invalid")
        for index, raw_value in enumerate(row):
            value = raw_value.strip()
            if not value:
                continue
            seen[index] = True
            if not possible[index]:
                continue
            try:
                number = Decimal(value)
            except InvalidOperation:
                possible[index] = False
            else:
                if not number.is_finite():
                    possible[index] = False
    if row_count == 0:
        raise _InvalidSource("CSV has no data rows")
    numeric = tuple(
        header[index]
        for index in range(len(header))
        if seen[index] and possible[index]
    )
    nonnumeric_count = len(header) - len(numeric)
    if len(numeric) < 2 or nonnumeric_count != 1:
        raise _InvalidSource("CSV numeric schema is unsupported")
    return numeric


def _source_digest(paths: Sequence[Path], root: Path) -> str:
    digest = hashlib.sha256()
    for path in paths:
        relative = unicodedata.normalize("NFC", path.relative_to(root).as_posix())
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        file_digest = hashlib.sha256(path.read_bytes()).digest()
        digest.update(file_digest)
    return digest.hexdigest()


def _resolved(
    column: str,
    paths: Sequence[Path],
    root: Path,
    operations: int,
) -> StructuredCandidateDecision:
    unique = tuple(dict.fromkeys(paths))
    return StructuredCandidateDecision(
        "resolved",
        "certified_notebook_embedded_statistics_diff",
        StructuredCandidateAnswer(
            answer=f"基本統計量の表に「{column}」列が追加されています。",
            source_paths=tuple(
                unicodedata.normalize("NFC", path.relative_to(root).as_posix())
                for path in unique
            ),
            source_sha256=_source_digest(unique, root),
            operation_count=operations,
            output_count=1,
        ),
    )


def _hold(reason: str) -> StructuredCandidateDecision:
    return StructuredCandidateDecision("hold", reason)


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    root = _source_root(engine)
    if root is None:
        return _hold("notebook_version_diff_source_root_invalid")
    bindings = contract["bindings"]
    before_paths = _named_notebooks(engine, bindings["location"], bindings["before"])
    after_paths = _named_notebooks(engine, bindings["location"], bindings["after"])
    if len(before_paths) != 1 or len(after_paths) != 1:
        return _hold("notebook_version_diff_pair_not_unique")
    before_path, after_path = before_paths[0], after_paths[0]
    if before_path == after_path or before_path.parent != after_path.parent:
        return _hold("notebook_version_diff_pair_not_unique")
    try:
        before = _read_notebook(before_path)
        after = _read_notebook(after_path)
        difference = _compare_notebooks(before, after)
        binding = _dataset_binding(difference.code_source)
        dataset = _bound_dataset_path(after_path, root, binding.relative_path)
        numeric_columns = _numeric_column_order(dataset, binding.encodings)
        if (
            numeric_columns[-1] != binding.target_column
            or len(numeric_columns) < 2
        ):
            raise _InvalidSource("target is not the final numeric column")
        before_count = len(numeric_columns) - 1
        after_count = len(numeric_columns)
        before_image = _decode_png(difference.before_png)
        after_image = _decode_png(difference.after_png)
        before_headers = _header_evidence(before_image, before_count)
        after_headers = _header_evidence(after_image, after_count)
        _verify_appended_header(before_headers, after_headers)
        if not _added_header_matches_target(
            after_image,
            after_headers,
            binding.target_column,
        ):
            raise _InvalidSource("appended header text does not match target column")
        return _resolved(
            binding.target_column,
            (before_path, after_path, dataset),
            root,
            len(contract["operation_graph"]["nodes"]),
        )
    except (
        ArithmeticError,
        csv.Error,
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        return _hold("notebook_version_diff_source_not_certified")


def decide_from_graph(
    engine: Any,
    question: str,
    graph_plan: Any,
) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    if (
        graph_plan is None
        or getattr(graph_plan, "original_question", None) != question
        or getattr(graph_plan, "strict_status", None) != "pass"
    ):
        return _hold("notebook_version_diff_graph_plan_not_certified")
    branches = getattr(graph_plan, "branch_intents", ())
    if not isinstance(branches, tuple) or len(branches) != 1:
        return _hold("notebook_version_diff_graph_plan_not_certified")
    branch = branches[0]
    intent = branch.get("intent") if isinstance(branch, Mapping) else None
    supplied = (
        intent.get("extended_graph_contract") if isinstance(intent, Mapping) else None
    )
    if (
        not isinstance(branch, Mapping)
        or branch.get("status") != "resolved"
        or not isinstance(supplied, Mapping)
        or not validate_graph_contract(question, supplied)
        or _canonical_json(supplied) != _canonical_json(contract)
    ):
        return _hold("notebook_version_diff_graph_plan_contract_mismatch")
    return decide_question(engine, question)


__all__ = [
    "NOTEBOOK_CONTENT_DIFF",
    "NOTEBOOK_VERSION_DIFF_RULE_VERSION",
    "decide_from_graph",
    "decide_question",
    "graph_contract_for_question",
    "validate_graph_contract",
]
