"""Fail-closed source execution for XLSX histogram questions.

The rule reconstructs Excel's automatic histogram from the unique numeric
source column, then requires an independently bound chart representation to
agree with that reconstruction.  Native chart caches are preferred when
present; static PNG chart pictures are accepted only when a fixed OCR runtime
binds the title and their bar-height profile agrees with the recomputed counts.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import posixpath
import re
import shutil
import stat
import struct
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from structured_candidate import (
    StructuredCandidateAnswer,
    StructuredCandidateDecision,
    _candidate_values,
    _location_matches,
)


XLSX_HISTOGRAM_RULE_VERSION = "0.1"

_MAX_COUNT = re.compile(
    r"^(?P<location>.+?)の(?P<container>[^,、。]+?\.xlsx)"
    r"(?P<join>において、?|内の)(?P<measure>[^,、。]+?)のヒストグラムで"
    r"最も多いカウント数はいくつですか。?$"
)
_RANKED_BIN = re.compile(
    r"^(?P<location>.+?)の(?P<container>[^,、。]+?\.xlsx)"
    r"(?P<join>において、?|内の)(?P<measure>[^,、。]+?)のヒストグラムで、?"
    r"(?P<rank>[0-9０-９]+)番目にカウント数が多いビンの範囲を"
    r"小数第(?P<precision>[0-9０-９]+)位までで答えてください。?$"
)

_S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PR_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_XDR_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_C_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_S = "{" + _S_NS + "}"
_R = "{" + _R_NS + "}"
_PR = "{" + _PR_NS + "}"
_XDR = "{" + _XDR_NS + "}"
_A = "{" + _A_NS + "}"
_C = "{" + _C_NS + "}"

_MAX_WORKBOOK_BYTES = 256 * 1024 * 1024
_MAX_ZIP_ENTRIES = 10_000
_MAX_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 200.0
_MAX_CELLS = 2_000_000
_MAX_ROWS = 1_000_000
_MAX_SHARED_STRINGS = 1_000_000
_MAX_IMAGES = 128
_MAX_IMAGE_PIXELS = 20_000_000
_MAX_PNG_DIMENSION = 4096
_MAX_PNG_DECOMPRESSED = 100 * 1024 * 1024
_MAX_PNG_PROFILE_WORK = 30_000_000
_MAX_PICTURE_BINS = 512
_XML_FORBIDDEN = (b"<!DOCTYPE", b"<!ENTITY")
_ARCHIVE_ENGLISH = re.compile(
    r"(?:^|[._\-\s])(?:old|draft|copy|backup|bak|archive|archived|obsolete|tmp)"
    r"(?:$|[._\-\s])",
    flags=re.IGNORECASE,
)
_ARCHIVE_JAPANESE = ("旧", "過去", "草案", "ドラフト", "コピー", "バックアップ", "アーカイブ")
_CELL_REF = re.compile(r"^([A-Z]{1,3})([1-9][0-9]{0,6})$")
_TESSERACT_VERSION = "tesseract 5.5.2"


class _InvalidSource(ValueError):
    pass


@dataclass(frozen=True)
class _SheetRef:
    name: str
    state: str
    part: str


@dataclass(frozen=True)
class _Histogram:
    minimum: Decimal
    width: Decimal
    counts: tuple[int, ...]


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


def _title_key(value: object) -> str:
    return re.sub(r"[^\w]+", "", _normalized(value), flags=re.UNICODE)


def _is_archived_component(value: str) -> bool:
    rendered = _normalized(value)
    return bool(_ARCHIVE_ENGLISH.search(rendered)) or any(
        marker in rendered for marker in _ARCHIVE_JAPANESE
    )


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


def _named_workbooks(engine: Any, location: str, container: str) -> tuple[Path, ...]:
    root = _source_root(engine)
    if root is None:
        return ()
    locations = _candidate_values(location, getattr(engine, "glossary", None))
    names = {
        _normalized(value)
        for value in _candidate_values(container, getattr(engine, "glossary", None))
    }
    matches: list[Path] = []
    try:
        for path in root.rglob("*.xlsx"):
            if (
                not path.is_file()
                or path.is_symlink()
                or path.name.startswith(("~$", "."))
                or _has_symlink_component(path, root)
                or _normalized(path.name) not in names
            ):
                continue
            relative = path.relative_to(root)
            if any(_is_archived_component(part) for part in relative.parts):
                continue
            if not _location_matches(relative.parts[:-1], locations):
                continue
            size = path.stat().st_size
            if 0 < size <= _MAX_WORKBOOK_BYTES:
                matches.append(path.resolve())
    except OSError:
        return ()
    return tuple(sorted(set(matches), key=lambda item: item.as_posix()))


def _nodes(operators: Sequence[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        result.append(
            {
                "operation_id": f"op_{index:03d}_{operator}",
                "operator": operator,
                "input_refs": [previous],
                "output_ref": output,
            }
        )
        previous = output
    return result


def _contract(
    question: str,
    bindings: Mapping[str, Any],
    operators: Sequence[str],
) -> dict[str, Any]:
    nodes = _nodes(operators)
    mode = str(bindings["mode"])
    precision = bindings.get("precision")
    core = {
        "rule_id": f"xlsx_histogram_{mode}_v1",
        "rule_version": XLSX_HISTOGRAM_RULE_VERSION,
        "question": question,
        "bindings": dict(bindings),
        "scope": {
            "location": bindings["location"],
            "container": bindings["container"],
            "measure": bindings["measure"],
            "histogram": "excel_automatic_scott",
            "source_channel": "native_chart_cache_or_bound_png",
            "verification": "chart_representation_equals_raw_recomputation",
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
                "value_type": "integer" if mode == "max_count" else "string",
                "unit": None,
            },
            "display_precision": (
                {"mode": "decimal_places", "digits": precision}
                if isinstance(precision, int)
                else None
            ),
            "required_keys": None,
        },
    }
    return {
        "graph_contract_id": "xlsx_histogram_"
        + hashlib.sha256(_canonical_json(core).encode("utf-8")).hexdigest()[:32],
        **core,
    }


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if not isinstance(question, str):
        return None
    match = _MAX_COUNT.fullmatch(question)
    if match is not None:
        bindings = {
            "location": match["location"],
            "container": match["container"],
            "measure": match["measure"],
            "mode": "max_count",
            "rank": None,
            "precision": None,
        }
        return _contract(
            question,
            bindings,
            (
                "retrieve_unique_workbook",
                "select_unique_numeric_series",
                "derive_scott_width_two_significant_digits",
                "construct_left_inclusive_then_right_closed_bins",
                "recompute_bin_counts",
                "bind_unique_histogram_visual",
                "verify_chart_profile",
                "project_maximum_count",
            ),
        )
    match = _RANKED_BIN.fullmatch(question)
    if match is None:
        return None
    try:
        rank = int(unicodedata.normalize("NFKC", match["rank"]))
        precision = int(unicodedata.normalize("NFKC", match["precision"]))
    except ValueError:
        return None
    if not 1 <= rank <= 100 or not 0 <= precision <= 12:
        return None
    bindings = {
        "location": match["location"],
        "container": match["container"],
        "measure": match["measure"],
        "mode": "ranked_bin_range",
        "rank": rank,
        "precision": precision,
    }
    return _contract(
        question,
        bindings,
        (
            "retrieve_unique_workbook",
            "select_unique_numeric_series",
            "derive_scott_width_two_significant_digits",
            "construct_left_inclusive_then_right_closed_bins",
            "recompute_bin_counts",
            "bind_unique_histogram_visual",
            "verify_chart_profile",
            "rank_bins_descending",
            "reject_rank_ties",
            "round_interval_half_up",
            "project_ranked_bin_range",
        ),
    )


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    if expected is None or not isinstance(contract, Mapping):
        return False
    try:
        return _canonical_json(expected) == _canonical_json(contract)
    except (TypeError, ValueError):
        return False


def _validate_archive(archive: zipfile.ZipFile) -> dict[str, bytes]:
    infos = archive.infolist()
    if not 1 <= len(infos) <= _MAX_ZIP_ENTRIES:
        raise _InvalidSource("archive entry count")
    result: dict[str, bytes] = {}
    total = 0
    for info in infos:
        name = info.filename
        pure = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or pure.is_absolute()
            or any(part in {"", ".", ".."} for part in pure.parts)
            or name in result
            or info.flag_bits & 0x1
            or info.is_dir()
        ):
            raise _InvalidSource("unsafe archive member")
        if not 0 <= info.file_size <= _MAX_MEMBER_BYTES:
            raise _InvalidSource("archive member size")
        if info.file_size and info.compress_size == 0:
            raise _InvalidSource("archive ratio")
        if info.compress_size and info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO:
            raise _InvalidSource("archive ratio")
        total += info.file_size
        if total > _MAX_TOTAL_BYTES:
            raise _InvalidSource("archive total size")
        data = archive.read(info)
        if len(data) != info.file_size:
            raise _InvalidSource("archive short read")
        if name.casefold().endswith((".xml", ".rels")):
            upper = data.upper()
            if any(marker in upper for marker in _XML_FORBIDDEN):
                raise _InvalidSource("unsafe XML")
        result[name] = data
    if archive.testzip() is not None:
        raise _InvalidSource("archive CRC")
    return result


def _xml(members: Mapping[str, bytes], name: str) -> ET.Element:
    data = members.get(name)
    if data is None:
        raise _InvalidSource("missing XML")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise _InvalidSource("malformed XML") from exc


def _rels_part(source: str) -> str:
    directory, basename = posixpath.split(source)
    return posixpath.join(directory, "_rels", basename + ".rels")


def _resolve_part(source: str, target: str) -> str:
    if not target or "\\" in target or target.startswith("/"):
        raise _InvalidSource("relationship target")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), target))
    pure = PurePosixPath(resolved)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise _InvalidSource("relationship traversal")
    return resolved


def _relationships(members: Mapping[str, bytes], source: str) -> dict[str, tuple[str, str]]:
    root = _xml(members, _rels_part(source))
    result: dict[str, tuple[str, str]] = {}
    for relation in root.findall(_PR + "Relationship"):
        rid = relation.get("Id")
        kind = relation.get("Type")
        target = relation.get("Target")
        if not rid or not kind or not target or rid in result or relation.get("TargetMode"):
            raise _InvalidSource("relationship")
        result[rid] = (kind, _resolve_part(source, target))
    return result


def _sheets(members: Mapping[str, bytes]) -> tuple[_SheetRef, ...]:
    workbook = _xml(members, "xl/workbook.xml")
    relations = _relationships(members, "xl/workbook.xml")
    result: list[_SheetRef] = []
    seen_names: set[str] = set()
    seen_parts: set[str] = set()
    for node in workbook.findall(".//" + _S + "sheet"):
        name = node.get("name")
        rid = node.get(_R + "id")
        state = node.get("state", "visible")
        if not name or not rid or state not in {"visible", "hidden", "veryHidden"}:
            raise _InvalidSource("sheet")
        relation = relations.get(rid)
        if relation is None or not relation[0].endswith("/worksheet"):
            raise _InvalidSource("sheet relation")
        part = relation[1]
        key = _normalized(name)
        if key in seen_names or part in seen_parts or part not in members:
            raise _InvalidSource("duplicate sheet")
        seen_names.add(key)
        seen_parts.add(part)
        result.append(_SheetRef(name, state, part))
    if not result:
        raise _InvalidSource("no sheets")
    return tuple(result)


def _shared_strings(members: Mapping[str, bytes]) -> tuple[str, ...]:
    data = members.get("xl/sharedStrings.xml")
    if data is None:
        return ()
    root = _xml(members, "xl/sharedStrings.xml")
    values: list[str] = []
    for item in root.findall(_S + "si"):
        values.append("".join(node.text or "" for node in item.iter(_S + "t")))
        if len(values) > _MAX_SHARED_STRINGS:
            raise _InvalidSource("shared strings")
    return tuple(values)


def _column_number(reference: str) -> tuple[int, int]:
    match = _CELL_REF.fullmatch(reference)
    if match is None:
        raise _InvalidSource("cell reference")
    column = 0
    for char in match[1]:
        column = column * 26 + ord(char) - 64
    return column, int(match[2])


def _cell_text(cell: ET.Element, shared: Sequence[str]) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(_S + "t"))
    value = cell.find(_S + "v")
    if value is None or value.text is None:
        return ""
    if kind == "s":
        try:
            index = int(value.text)
            return shared[index]
        except (ValueError, IndexError) as exc:
            raise _InvalidSource("shared string index") from exc
    if kind in {None, "n", "str"}:
        return value.text
    if kind in {"b", "e", "d"}:
        raise _InvalidSource("unsupported target cell type")
    raise _InvalidSource("cell type")


def _numeric_series(
    members: Mapping[str, bytes], sheets: Sequence[_SheetRef], measure: str
) -> tuple[Decimal, ...] | None:
    shared = _shared_strings(members)
    target = _normalized(measure)
    candidates: list[tuple[Decimal, ...]] = []
    total_cells = 0
    for sheet in sheets:
        if sheet.state != "visible":
            continue
        root = _xml(members, sheet.part)
        cells: dict[tuple[int, int], ET.Element] = {}
        for cell in root.findall(".//" + _S + "sheetData/" + _S + "row/" + _S + "c"):
            reference = cell.get("r")
            if not reference:
                raise _InvalidSource("cell reference")
            column, row = _column_number(reference)
            if row > _MAX_ROWS or (row, column) in cells:
                raise _InvalidSource("cell coordinate")
            cells[(row, column)] = cell
            total_cells += 1
            if total_cells > _MAX_CELLS:
                raise _InvalidSource("cell count")
        headers: list[tuple[int, int]] = []
        for (row, column), cell in cells.items():
            if row <= 100 and _normalized(_cell_text(cell, shared)) == target:
                headers.append((row, column))
        for header_row, column in headers:
            values: list[Decimal] = []
            maximum_row = max((row for row, col in cells if col == column), default=header_row)
            invalid = False
            for row in range(header_row + 1, maximum_row + 1):
                cell = cells.get((row, column))
                if cell is None:
                    continue
                if cell.find(_S + "f") is not None:
                    invalid = True
                    break
                rendered = _cell_text(cell, shared).strip()
                if not rendered:
                    continue
                try:
                    value = Decimal(rendered)
                except InvalidOperation:
                    invalid = True
                    break
                if not value.is_finite():
                    invalid = True
                    break
                values.append(value)
            if not invalid and len(values) >= 2 and min(values) < max(values):
                candidates.append(tuple(values))
    return candidates[0] if len(candidates) == 1 else None


def _round_significant(value: Decimal, digits: int = 2) -> Decimal:
    if not value.is_finite() or value <= 0 or digits <= 0:
        raise _InvalidSource("Scott width")
    quantum = Decimal(1).scaleb(value.adjusted() - digits + 1)
    rounded = value.quantize(quantum, rounding=ROUND_HALF_UP)
    if rounded <= 0:
        raise _InvalidSource("Scott width")
    return rounded


def _histogram(values: Sequence[Decimal]) -> _Histogram:
    count = len(values)
    if count < 2 or count > _MAX_ROWS:
        raise _InvalidSource("series size")
    minimum = min(values)
    maximum = max(values)
    if minimum == maximum:
        raise _InvalidSource("constant series")
    floats = [float(value) for value in values]
    if any(not math.isfinite(value) for value in floats):
        raise _InvalidSource("non-finite series")
    mean = math.fsum(floats) / count
    variance = math.fsum((value - mean) ** 2 for value in floats) / (count - 1)
    if not math.isfinite(variance) or variance <= 0:
        raise _InvalidSource("series variance")
    scott = Decimal(str(3.5 * math.sqrt(variance) / math.pow(count, 1.0 / 3.0)))
    width = _round_significant(scott, 2)
    span = maximum - minimum
    bins = int((span / width).to_integral_value(rounding=ROUND_CEILING))
    if not 1 <= bins <= 10_000:
        raise _InvalidSource("bin count")
    counts = [0] * bins
    for value in values:
        delta = value - minimum
        if delta == 0:
            index = 0
        else:
            index = int((delta / width).to_integral_value(rounding=ROUND_CEILING)) - 1
        if not 0 <= index < bins:
            raise _InvalidSource("bin assignment")
        counts[index] += 1
    if sum(counts) != count:
        raise _InvalidSource("bin total")
    return _Histogram(minimum, width, tuple(counts))


def _png_rows(data: bytes) -> tuple[int, int, tuple[bytes, ...]]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise _InvalidSource("PNG signature")
    position = 8
    width = height = color_type = bit_depth = interlace = None
    compressed = bytearray()
    saw_end = False
    while position + 12 <= len(data):
        length = struct.unpack_from(">I", data, position)[0]
        kind = data[position + 4 : position + 8]
        end = position + 12 + length
        if length > _MAX_MEMBER_BYTES or end > len(data):
            raise _InvalidSource("PNG chunk")
        payload = data[position + 8 : position + 8 + length]
        expected_crc = struct.unpack_from(">I", data, position + 8 + length)[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
            raise _InvalidSource("PNG CRC")
        if kind == b"IHDR":
            if width is not None or length != 13:
                raise _InvalidSource("PNG IHDR")
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            if (
                width <= 0
                or height <= 0
                or width > _MAX_PNG_DIMENSION
                or height > _MAX_PNG_DIMENSION
                or width * height > _MAX_IMAGE_PIXELS
                or bit_depth != 8
                or color_type not in {2, 6}
                or compression != 0
                or filtering != 0
                or interlace != 0
            ):
                raise _InvalidSource("PNG format")
        elif kind == b"IDAT":
            compressed.extend(payload)
            if len(compressed) > _MAX_MEMBER_BYTES:
                raise _InvalidSource("PNG compressed size")
        elif kind == b"IEND":
            if length != 0 or end != len(data):
                raise _InvalidSource("PNG IEND")
            saw_end = True
            position = end
            break
        position = end
    if not saw_end or width is None or height is None or color_type is None:
        raise _InvalidSource("PNG structure")
    channels = 3 if color_type == 2 else 4
    stride = width * channels
    expected = height * (stride + 1)
    if expected > _MAX_PNG_DECOMPRESSED:
        raise _InvalidSource("PNG size")
    try:
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(bytes(compressed), expected + 1)
        if len(raw) > expected or decompressor.unconsumed_tail:
            raise _InvalidSource("PNG expansion")
        remaining = expected + 1 - len(raw)
        raw += decompressor.flush(remaining)
        if (
            len(raw) > expected
            or not decompressor.eof
            or decompressor.unused_data
            or decompressor.unconsumed_tail
        ):
            raise _InvalidSource("PNG deflate boundary")
    except zlib.error as exc:
        raise _InvalidSource("PNG deflate") from exc
    if len(raw) != expected:
        raise _InvalidSource("PNG raster size")
    rows: list[bytes] = []
    previous = bytearray(stride)
    offset = 0
    for _ in range(height):
        filter_type = raw[offset]
        encoded = raw[offset + 1 : offset + 1 + stride]
        offset += stride + 1
        decoded = bytearray(stride)
        for index, byte in enumerate(encoded):
            left = decoded[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 0:
                value = byte
            elif filter_type == 1:
                value = (byte + left) & 0xFF
            elif filter_type == 2:
                value = (byte + above) & 0xFF
            elif filter_type == 3:
                value = (byte + ((left + above) // 2)) & 0xFF
            elif filter_type == 4:
                predictor = left + above - upper_left
                pa = abs(predictor - left)
                pb = abs(predictor - above)
                pc = abs(predictor - upper_left)
                base = left if pa <= pb and pa <= pc else above if pb <= pc else upper_left
                value = (byte + base) & 0xFF
            else:
                raise _InvalidSource("PNG filter")
            decoded[index] = value
        rgb = bytes(
            component
            for pixel in range(width)
            for component in decoded[pixel * channels : pixel * channels + 3]
        )
        rows.append(rgb)
        previous = decoded
    return width, height, tuple(rows)


def _png_profile_matches(data: bytes, counts: Sequence[int]) -> bool:
    try:
        width, height, rows = _png_rows(data)
    except _InvalidSource:
        return False
    colors: Counter[tuple[int, int, int]] = Counter()
    for row in rows:
        for offset in range(0, len(row), 3):
            color = (row[offset], row[offset + 1], row[offset + 2])
            if max(color) - min(color) >= 45 and 35 <= sum(color) / 3 <= 220:
                colors[color] += 1
    if not colors:
        return False
    bar_color, frequency = colors.most_common(1)[0]
    if frequency < max(100, width * height // 500):
        return False
    ys_by_x: list[list[int]] = [[] for _ in range(width)]
    for y, row in enumerate(rows):
        for x in range(width):
            offset = x * 3
            if (row[offset], row[offset + 1], row[offset + 2]) == bar_color:
                ys_by_x[x].append(y)
    colored_y = [y for values in ys_by_x for y in values]
    if not colored_y:
        return False
    baseline = max(colored_y)
    heights_by_x = [baseline - min(values) + 1 if values else 0 for values in ys_by_x]
    observed = Counter(height for height in heights_by_x if height)
    maximum_count = max(counts, default=0)
    maximum_height = max(observed, default=0)
    if maximum_count <= 0 or maximum_height < 8:
        return False
    significant_indices = [
        index for index, value in enumerate(counts) if value > 0 and value * 12 >= maximum_count
    ]
    if len(significant_indices) < min(3, len({value for value in counts if value > 0})):
        return False
    modes = {height for height, columns in observed.items() if columns >= 2}
    if not any(abs(height - maximum_height) <= 1 for height in modes):
        return False

    # Recover the plot rectangle without trusting OCR coordinates.  Excel's
    # histogram bars occupy a uniformly divided horizontal span with small
    # left/right margins.  Search only plausible margins, then compare the
    # ordered normalized bar heights.  This rejects a picture with the same
    # count multiset in a different bin order.
    bin_count = len(counts)
    if not 1 <= bin_count <= min(width, _MAX_PICTURE_BINS):
        return False
    margin_limit = min(80, width // 5)
    if width * (margin_limit + 1) ** 2 > _MAX_PNG_PROFILE_WORK:
        return False
    best_error: float | None = None
    best_heights: list[int] | None = None
    for left_margin in range(margin_limit + 1):
        for right_margin in range(margin_limit + 1):
            # Authored Excel plots use small, approximately symmetric plot
            # margins.  Allow raster rounding and y-axis label accommodation,
            # but never a large one-sided crop that could shift a reversed bar
            # sequence into accidental agreement.
            if (
                abs(left_margin - right_margin) > max(8, width // 50)
                or left_margin + right_margin > width // 4
            ):
                continue
            span = width - left_margin - right_margin
            if span < bin_count:
                continue
            candidate: list[int] = []
            for index in range(bin_count):
                left = round(left_margin + span * index / bin_count)
                right = round(left_margin + span * (index + 1) / bin_count)
                if right <= left:
                    candidate = []
                    break
                candidate.append(max(heights_by_x[left:right], default=0))
            if not candidate or max(candidate) <= 0:
                continue
            candidate_maximum = max(candidate)
            error = sum(
                abs(
                    candidate[index] / candidate_maximum
                    - counts[index] / maximum_count
                )
                for index in significant_indices
            ) / len(significant_indices)
            if best_error is None or error < best_error:
                best_error = error
                best_heights = candidate
    if best_error is None or best_heights is None or best_error > 0.06:
        return False
    required = min(5, len(significant_indices))
    matched = 0
    candidate_maximum = max(best_heights)
    for index in significant_indices:
        expected = max(1, round(candidate_maximum * counts[index] / maximum_count))
        if abs(best_heights[index] - expected) <= 3:
            matched += 1
    return matched >= required


def _tesseract_executable() -> Path | None:
    raw = shutil.which("tesseract")
    if not raw:
        return None
    try:
        path = Path(raw).resolve(strict=True)
        mode = path.stat().st_mode
    except OSError:
        return None
    if not stat.S_ISREG(mode) or not os.access(path, os.X_OK):
        return None
    try:
        result = subprocess.run(
            [str(path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    first = (result.stdout or result.stderr).splitlines()
    if result.returncode != 0 or not first or first[0].strip() != _TESSERACT_VERSION:
        return None
    return path


def _ocr_title(data: bytes) -> str | None:
    executable = _tesseract_executable()
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [str(executable), "stdin", "stdout", "-l", "eng", "--psm", "11", "tsv"],
            input=data,
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        text = result.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    rows = list(csv.DictReader(text.splitlines(), delimiter="\t"))
    words: list[tuple[int, str]] = []
    for row in rows:
        try:
            if int(row.get("level", "0")) != 5:
                continue
            top = int(row.get("top", "-1"))
            left = int(row.get("left", "-1"))
            height = int(row.get("height", "0"))
            confidence = float(row.get("conf", "-1"))
        except ValueError:
            return None
        value = (row.get("text") or "").strip()
        if value and top >= 0 and top < 90 and height >= 15 and confidence >= 20:
            words.append((left, value))
    if not words:
        return None
    return " ".join(value for _, value in sorted(words))


def _drawing_targets(
    members: Mapping[str, bytes], sheets: Sequence[_SheetRef]
) -> tuple[tuple[bytes, ...], tuple[bytes, ...]]:
    images: list[bytes] = []
    charts: list[bytes] = []
    for sheet in sheets:
        if sheet.state != "visible":
            continue
        root = _xml(members, sheet.part)
        drawing_ids = [node.get(_R + "id") for node in root.findall(_S + "drawing")]
        if any(not value for value in drawing_ids) or len(drawing_ids) != len(set(drawing_ids)):
            raise _InvalidSource("drawing references")
        if not drawing_ids:
            continue
        sheet_relations = _relationships(members, sheet.part)
        for rid in drawing_ids:
            assert rid is not None
            relation = sheet_relations.get(rid)
            if relation is None or not relation[0].endswith("/drawing"):
                raise _InvalidSource("drawing relation")
            drawing_part = relation[1]
            drawing = _xml(members, drawing_part)
            drawing_relations = _relationships(members, drawing_part)
            for blip in drawing.findall(".//" + _A + "blip"):
                embed = blip.get(_R + "embed")
                relation = drawing_relations.get(embed or "")
                if relation is None or not relation[0].endswith("/image"):
                    raise _InvalidSource("image relation")
                part = relation[1]
                if part.casefold().endswith(".png"):
                    data = members.get(part)
                    if data is None:
                        raise _InvalidSource("image missing")
                    images.append(data)
            for chart in drawing.findall(".//" + _C + "chart"):
                rid_value = chart.get(_R + "id")
                relation = drawing_relations.get(rid_value or "")
                if relation is None or not relation[0].endswith("/chart"):
                    raise _InvalidSource("chart relation")
                data = members.get(relation[1])
                if data is None:
                    raise _InvalidSource("chart missing")
                charts.append(data)
            if len(images) + len(charts) > _MAX_IMAGES:
                raise _InvalidSource("visual count")
    return tuple(images), tuple(charts)


def _chart_cache_matches(data: bytes, measure: str, counts: Sequence[int]) -> bool:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return False
    title = "".join(node.text or "" for node in root.findall(".//" + _C + "title//" + _A + "t"))
    if _title_key(title) != _title_key(measure):
        return False
    expected = tuple(counts)
    for cache in root.findall(".//" + _C + "numCache"):
        values: list[int] = []
        valid = True
        for point in cache.findall(_C + "pt"):
            value = point.find(_C + "v")
            if value is None or value.text is None:
                valid = False
                break
            try:
                decimal = Decimal(value.text)
            except InvalidOperation:
                valid = False
                break
            if decimal != decimal.to_integral_value() or decimal < 0:
                valid = False
                break
            values.append(int(decimal))
        if valid and tuple(values) == expected:
            return True
    return False


def _visual_matches(
    members: Mapping[str, bytes], sheets: Sequence[_SheetRef], measure: str, counts: Sequence[int]
) -> bool:
    images, charts = _drawing_targets(members, sheets)
    matches = sum(_chart_cache_matches(data, measure, counts) for data in charts)
    for data in images:
        title = _ocr_title(data)
        if title is None or _title_key(title) != _title_key(measure):
            continue
        if _png_profile_matches(data, counts):
            matches += 1
    return matches == 1


def _format_decimal(value: Decimal, precision: int) -> str:
    quantum = Decimal(1).scaleb(-precision)
    rounded = value.quantize(quantum, rounding=ROUND_HALF_UP)
    return f"{rounded:.{precision}f}"


def _answer(histogram: _Histogram, bindings: Mapping[str, Any]) -> str | None:
    mode = bindings["mode"]
    if mode == "max_count":
        return str(max(histogram.counts))
    rank = bindings.get("rank")
    precision = bindings.get("precision")
    if not isinstance(rank, int) or not isinstance(precision, int) or rank > len(histogram.counts):
        return None
    ordered = sorted(
        ((count, index) for index, count in enumerate(histogram.counts)),
        key=lambda item: (-item[0], item[1]),
    )
    selected_count, selected_index = ordered[rank - 1]
    if sum(1 for count, _ in ordered if count == selected_count) != 1:
        return None
    left = histogram.minimum + histogram.width * selected_index
    right = left + histogram.width
    left_text = _format_decimal(left, precision)
    right_text = _format_decimal(right, precision)
    if left_text == right_text:
        return None
    return f"{'[' if selected_index == 0 else '('}{left_text}, {right_text}]"


def _hold(reason: str) -> StructuredCandidateDecision:
    return StructuredCandidateDecision("hold", reason)


def _decision(answer: str, path: Path, root: Path, operations: int) -> StructuredCandidateDecision:
    return StructuredCandidateDecision(
        "resolved",
        "certified_xlsx_histogram",
        StructuredCandidateAnswer(
            answer=answer,
            source_paths=(unicodedata.normalize("NFC", path.relative_to(root).as_posix()),),
            source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            operation_count=operations,
            output_count=1,
        ),
    )


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    bindings = contract["bindings"]
    root = _source_root(engine)
    if root is None:
        return _hold("xlsx_histogram_source_root_invalid")
    paths = _named_workbooks(engine, bindings["location"], bindings["container"])
    if len(paths) != 1:
        return _hold("xlsx_histogram_workbook_not_unique")
    try:
        with zipfile.ZipFile(paths[0]) as archive:
            members = _validate_archive(archive)
        sheets = _sheets(members)
        values = _numeric_series(members, sheets, bindings["measure"])
        if values is None:
            return _hold("xlsx_histogram_series_not_unique")
        histogram = _histogram(values)
        if not _visual_matches(members, sheets, bindings["measure"], histogram.counts):
            return _hold("xlsx_histogram_visual_mismatch")
        answer = _answer(histogram, bindings)
        if answer is None:
            return _hold("xlsx_histogram_rank_or_rounding_ambiguous")
    except (
        OSError,
        RuntimeError,
        KeyError,
        TypeError,
        ValueError,
        struct.error,
        subprocess.SubprocessError,
        zipfile.BadZipFile,
        zlib.error,
    ):
        return _hold("xlsx_histogram_source_invalid")
    return _decision(answer, paths[0], root, len(contract["operation_graph"]["nodes"]))


def decide_from_graph(
    engine: Any, question: str, graph_plan: Any
) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    if (
        graph_plan is None
        or getattr(graph_plan, "original_question", None) != question
        or getattr(graph_plan, "strict_status", None) != "pass"
    ):
        return _hold("xlsx_histogram_graph_plan_not_certified")
    branches = getattr(graph_plan, "branch_intents", ())
    if not isinstance(branches, tuple) or len(branches) != 1:
        return _hold("xlsx_histogram_graph_plan_not_certified")
    branch = branches[0]
    if not isinstance(branch, Mapping) or branch.get("status") != "resolved":
        return _hold("xlsx_histogram_graph_plan_not_certified")
    intent = branch.get("intent")
    supplied = intent.get("extended_graph_contract") if isinstance(intent, Mapping) else None
    if (
        not isinstance(supplied, Mapping)
        or not validate_graph_contract(question, supplied)
        or _canonical_json(supplied) != _canonical_json(contract)
    ):
        return _hold("xlsx_histogram_graph_plan_contract_mismatch")
    return decide_question(engine, question)


__all__ = [
    "XLSX_HISTOGRAM_RULE_VERSION",
    "decide_from_graph",
    "decide_question",
    "graph_contract_for_question",
    "validate_graph_contract",
]
