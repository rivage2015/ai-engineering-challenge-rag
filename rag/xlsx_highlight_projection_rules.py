"""Fail-closed extraction of authored yellow aggregate cells in XLSX files.

Two source channels are supported without OCR or answer-specific constants:

* a native worksheet cell with a direct solid-yellow fill; and
* a yellow PATCOPY rectangle in a single embedded EMF table.

In both cases the sparse hierarchy above the marked aggregate is restored,
then independently recomputed from the unique raw worksheet.  Any ambiguity,
unsupported coordinate transform, malformed package, or aggregate mismatch is
held for the normal RAG path.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import struct
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from structured_candidate import (
    StructuredCandidateAnswer,
    StructuredCandidateDecision,
    _candidate_values,
    _location_matches,
)


XLSX_HIGHLIGHT_PROJECTION_RULE_VERSION = "0.2"

HIGHLIGHT_PROJECTION = re.compile(
    r"^(?P<location>.+?)の(?P<container>[^,、。]+?\.xlsx)において、"
    r"(?P<sheet>[^,、。]+?)の(?P<color>[^,、。]+?)にハイライトされたセルの"
    r"抽出条件と集計内容を(?:答えて|教えて)ください。?$"
)

_S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PR_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_XDR_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_S = "{" + _S_NS + "}"
_R = "{" + _R_NS + "}"
_PR = "{" + _PR_NS + "}"
_XDR = "{" + _XDR_NS + "}"
_A = "{" + _A_NS + "}"

_MAX_WORKBOOK_BYTES = 256 * 1024 * 1024
_MAX_ZIP_ENTRIES = 10_000
_MAX_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 200.0
_MAX_CELLS = 2_000_000
_MAX_SHARED_STRINGS = 1_000_000
_MAX_EMF_RECORDS = 100_000
_MAX_EMF_TEXT_RUNS = 10_000
_MAX_EMF_CHARS = 16_384
_MAX_GDI_OBJECTS = 10_000
_XML_FORBIDDEN = (b"<!DOCTYPE", b"<!ENTITY")
_COORDINATE_STATE_RECORDS = {9, 10, 11, 12, 16, 17, 31, 32}
_UNSUPPORTED_TRANSFORMS = {35, 36, 83, 108}
# Only record families whose state/size semantics are validated below are
# accepted.  In particular, brush-driven primitives such as RECTANGLE,
# POLYGON, FILLPATH, FILLRGN, and STROKEANDFILLPATH are intentionally absent:
# otherwise a second yellow region could bypass the unique PATCOPY marker.
_ALLOWED_EMF_RECORD_TYPES = frozenset(
    {
        1,   # HEADER
        10,  # SETWINDOWORGEX (only the authored zero origin is supported)
        14,  # EOF
        18,  # SETBKMODE
        20,  # SETROP2
        21,  # SETSTRETCHBLTMODE
        22,  # SETTEXTALIGN
        24,  # SETTEXTCOLOR
        25,  # SETBKCOLOR
        27,  # MOVETOEX
        30,  # INTERSECTCLIPRECT
        33,  # SAVEDC
        34,  # RESTOREDC
        37,  # SELECTOBJECT
        38,  # CREATEPEN
        39,  # CREATEBRUSHINDIRECT
        40,  # DELETEOBJECT
        54,  # LINETO (pen-only)
        70,  # GDICOMMENT
        75,  # EXTSELECTCLIPRGN
        76,  # BITBLT
        81,  # STRETCHDIBITS
        82,  # EXTCREATEFONTINDIRECTW
        84,  # EXTTEXTOUTW
        98,  # SETICMMODE
    }
)
_FIXED_EMF_RECORD_SIZES = {
    10: 16,
    18: 12,
    20: 12,
    21: 12,
    24: 12,
    25: 12,
    30: 24,
    54: 16,
    75: 16,
    98: 12,
}
_YELLOW_NAMES = frozenset({"黄", "黄色", "yellow"})
_YELLOW_ARGB = "FFFFFF00"
_YELLOW_COLORREF = 0x0000FFFF
_PATCOPY = 0x00F00021
_CELL_REF = re.compile(r"^([A-Z]{1,3})([1-9][0-9]{0,6})$")
_RANGE_REF = re.compile(
    r"^([A-Z]{1,3})([1-9][0-9]{0,6}):([A-Z]{1,3})([1-9][0-9]{0,6})$"
)
_ARCHIVE_ENGLISH = re.compile(
    r"(?:^|[._\-\s])(?:old|draft|copy|backup|bak|archive|archived|obsolete|tmp)"
    r"(?:$|[._\-\s])",
    flags=re.IGNORECASE,
)
_ARCHIVE_JAPANESE = ("旧", "過去", "草案", "ドラフト", "コピー", "バックアップ", "アーカイブ")
_SUBTOTAL_KEYS = frozenset(
    {"合計", "総計", "小計", "grandtotal", "subtotal", "(blank)", "空白", "(空白)"}
)


@dataclass(frozen=True)
class _Cell:
    value: str | Decimal | bool
    display: str
    style: int


@dataclass(frozen=True)
class _Sheet:
    name: str
    part: str
    state: str
    cells: Mapping[tuple[int, int], _Cell]
    merges: tuple[tuple[int, int, int, int], ...]
    drawing_ids: tuple[str, ...]
    has_conditional_formatting: bool
    has_formula: bool
    hidden_rows: frozenset[int]
    hidden_columns: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class _EmfRun:
    record: int
    x: int
    y: int
    text: str


@dataclass(frozen=True)
class _Rect:
    record: int
    x: int
    y: int
    cx: int
    cy: int
    color: int


@dataclass(frozen=True)
class _Projection:
    fields: tuple[str, ...]
    values: tuple[str, ...]
    aggregate_caption: str
    highlighted: Decimal


class _InvalidSource(ValueError):
    pass


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


def _field_key(value: object) -> str:
    return re.sub(r"\s+", " ", _normalized(value))


def _compact_key(value: object) -> str:
    return re.sub(r"\s+", "", _normalized(value))


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
    core = {
        "graph_rule_version": XLSX_HIGHLIGHT_PROJECTION_RULE_VERSION,
        "rule_id": "xlsx_yellow_highlight_projection_recomputed",
        "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
        "bindings": dict(bindings),
        "scope": {
            "location": bindings["location"],
            "container": bindings["container"],
            "sheet": bindings["sheet"],
            "color": bindings["color"],
            "source_channel": "native_cell_or_embedded_emf",
            "hierarchy": "sparse_forward_fill",
            "verification": "unique_raw_sheet_exact_recomputation",
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
            "cardinality": "multiple",
            "answer_shape": {
                "container": "key_value",
                "value_type": "string",
                "unit": None,
            },
            "display_precision": None,
            "required_keys": None,
        },
    }
    return {
        "graph_contract_id": "xlsx_highlight_projection_"
        + hashlib.sha256(_canonical_json(core).encode("utf-8")).hexdigest()[:32],
        **core,
    }


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if not isinstance(question, str):
        return None
    match = HIGHLIGHT_PROJECTION.fullmatch(question)
    if match is None or _normalized(match["color"]) not in _YELLOW_NAMES:
        return None
    bindings = {
        key: match[key]
        for key in ("location", "container", "sheet", "color")
    }
    return _contract(
        question,
        bindings,
        (
            "retrieve",
            "select_sheet",
            "locate_unique_yellow",
            "restore_sparse_hierarchy",
            "resolve_aggregate",
            "select_unique_raw_sheet",
            "recompute_raw",
            "verify_exact",
            "project",
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
    names: set[str] = set()
    total = 0
    result: dict[str, bytes] = {}
    for info in infos:
        name = info.filename
        pure = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or pure.is_absolute()
            or any(part in {"", ".", ".."} for part in pure.parts)
            or name in names
            or info.flag_bits & 0x1
            or info.is_dir()
        ):
            raise _InvalidSource("unsafe archive member")
        names.add(name)
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


def _safe_target(source: str, target: str) -> str:
    if not target or "\\" in target or target.startswith("/"):
        raise _InvalidSource("unsafe relationship target")
    value = posixpath.normpath(posixpath.join(posixpath.dirname(source), target))
    if value == ".." or value.startswith("../") or PurePosixPath(value).is_absolute():
        raise _InvalidSource("unsafe relationship target")
    return value


def _relationships(
    members: Mapping[str, bytes], source: str, *, required: bool = True
) -> dict[str, tuple[str, str]]:
    name = _rels_part(source)
    if name not in members:
        if required:
            raise _InvalidSource("missing relationships")
        return {}
    root = _xml(members, name)
    result: dict[str, tuple[str, str]] = {}
    for node in root.findall(_PR + "Relationship"):
        relation_id = node.get("Id")
        relation_type = node.get("Type")
        target = node.get("Target")
        if (
            not relation_id
            or not relation_type
            or not target
            or relation_id in result
            or node.get("TargetMode") is not None
        ):
            raise _InvalidSource("invalid relationship")
        result[relation_id] = (relation_type, _safe_target(source, target))
    return result


def _column_number(letters: str) -> int:
    result = 0
    for char in letters:
        result = result * 26 + ord(char) - 64
    if not 1 <= result <= 16_384:
        raise _InvalidSource("cell column")
    return result


def _coordinate(value: str) -> tuple[int, int]:
    match = _CELL_REF.fullmatch(value)
    if match is None:
        raise _InvalidSource("cell coordinate")
    row = int(match.group(2))
    if row > 1_048_576:
        raise _InvalidSource("cell row")
    return row, _column_number(match.group(1))


def _decimal(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None
    return result if result.is_finite() else None


def _render_decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _shared_strings(members: Mapping[str, bytes]) -> tuple[str, ...]:
    name = "xl/sharedStrings.xml"
    if name not in members:
        return ()
    root = _xml(members, name)
    strings: list[str] = []
    for item in root.findall(_S + "si"):
        text = "".join(node.text or "" for node in item.iter(_S + "t"))
        if "\x00" in text:
            raise _InvalidSource("shared string")
        strings.append(text)
        if len(strings) > _MAX_SHARED_STRINGS:
            raise _InvalidSource("shared strings")
    return tuple(strings)


def _yellow_styles(members: Mapping[str, bytes]) -> tuple[frozenset[int], int]:
    root = _xml(members, "xl/styles.xml")
    fills_node = root.find(_S + "fills")
    xfs_node = root.find(_S + "cellXfs")
    if fills_node is None or xfs_node is None:
        raise _InvalidSource("styles")
    yellow_fills: set[int] = set()
    fills = list(fills_node.findall(_S + "fill"))
    for index, fill in enumerate(fills):
        pattern = fill.find(_S + "patternFill")
        if pattern is None or pattern.get("patternType") != "solid":
            continue
        foreground = pattern.find(_S + "fgColor")
        if foreground is None:
            continue
        if foreground.get("rgb", "").upper() == _YELLOW_ARGB and set(foreground.attrib) == {"rgb"}:
            yellow_fills.add(index)
    result: set[int] = set()
    xfs = list(xfs_node.findall(_S + "xf"))
    if not xfs:
        raise _InvalidSource("cell styles")
    for index, xf in enumerate(xfs):
        try:
            fill_id = int(xf.get("fillId", "0"))
        except ValueError as exc:
            raise _InvalidSource("style fill id") from exc
        if not 0 <= fill_id < len(fills):
            raise _InvalidSource("style fill id")
        apply_fill = xf.get("applyFill")
        if apply_fill not in {None, "0", "1", "false", "true", "False", "True"}:
            raise _InvalidSource("style applyFill")
        # A cellXf that explicitly disables fill application does not expose
        # its referenced fill visually.  Require an affirmative flag for the
        # certified direct-highlight lane; omission remains fail-closed too.
        if fill_id in yellow_fills and apply_fill in {"1", "true", "True"}:
            result.add(index)
    return frozenset(result), len(xfs)


def _cell_value(node: ET.Element, shared: Sequence[str]) -> tuple[str | Decimal | bool, str] | None:
    if node.find(_S + "f") is not None:
        raise _InvalidSource("formula cell")
    value_type = node.get("t", "n")
    value_node = node.find(_S + "v")
    if value_type == "inlineStr":
        inline = node.find(_S + "is")
        if inline is None:
            return None
        value = "".join(item.text or "" for item in inline.iter(_S + "t"))
        return (value, value)
    if value_node is None or value_node.text is None:
        return None
    raw = value_node.text
    if value_type == "s":
        try:
            index = int(raw)
        except ValueError as exc:
            raise _InvalidSource("shared string index") from exc
        if not 0 <= index < len(shared):
            raise _InvalidSource("shared string index")
        return shared[index], shared[index]
    if value_type in {"str", "e"}:
        if value_type == "e":
            raise _InvalidSource("error cell")
        return raw, raw
    if value_type == "b":
        if raw not in {"0", "1"}:
            raise _InvalidSource("boolean cell")
        return raw == "1", "TRUE" if raw == "1" else "FALSE"
    if value_type not in {"n", ""}:
        raise _InvalidSource("unsupported cell type")
    number = _decimal(raw)
    if number is None:
        raise _InvalidSource("numeric cell")
    return number, _render_decimal(number)


def _parse_merges(root: ET.Element) -> tuple[tuple[int, int, int, int], ...]:
    result: list[tuple[int, int, int, int]] = []
    occupied: set[tuple[int, int]] = set()
    for node in root.findall(".//" + _S + "mergeCell"):
        ref = node.get("ref", "")
        match = _RANGE_REF.fullmatch(ref)
        if match is None:
            raise _InvalidSource("merge range")
        r1, c1 = int(match.group(2)), _column_number(match.group(1))
        r2, c2 = int(match.group(4)), _column_number(match.group(3))
        if r1 > r2 or c1 > c2 or r2 > 1_048_576:
            raise _InvalidSource("merge range")
        if (r2 - r1 + 1) * (c2 - c1 + 1) > 100_000:
            raise _InvalidSource("merge range size")
        for row in range(r1, r2 + 1):
            for column in range(c1, c2 + 1):
                if (row, column) in occupied:
                    raise _InvalidSource("overlapping merge")
                occupied.add((row, column))
        result.append((r1, c1, r2, c2))
    return tuple(result)


def _parse_sheet(
    members: Mapping[str, bytes],
    name: str,
    part: str,
    state: str,
    shared: Sequence[str],
) -> _Sheet:
    root = _xml(members, part)
    cells: dict[tuple[int, int], _Cell] = {}
    seen_coordinates: set[tuple[int, int]] = set()
    has_formula = False
    for node in root.findall(".//" + _S + "c"):
        if len(seen_coordinates) >= _MAX_CELLS:
            raise _InvalidSource("cell count")
        ref = node.get("r")
        if ref is None:
            raise _InvalidSource("cell without coordinate")
        coordinate = _coordinate(ref)
        if coordinate in seen_coordinates:
            raise _InvalidSource("duplicate cell")
        seen_coordinates.add(coordinate)
        if node.find(_S + "f") is not None:
            has_formula = True
        try:
            style = int(node.get("s", "0"))
        except ValueError as exc:
            raise _InvalidSource("cell style") from exc
        if style < 0:
            raise _InvalidSource("cell style")
        parsed = _cell_value(node, shared)
        if parsed is None:
            cells[coordinate] = _Cell("", "", style)
        else:
            cells[coordinate] = _Cell(parsed[0], parsed[1], style)
    hidden_rows: set[int] = set()
    for row in root.findall(".//" + _S + "row"):
        if row.get("hidden") in {"1", "true", "True"}:
            try:
                hidden_rows.add(int(row.get("r", "0")))
            except ValueError as exc:
                raise _InvalidSource("hidden row") from exc
    hidden_columns: list[tuple[int, int]] = []
    for column in root.findall(".//" + _S + "col"):
        if column.get("hidden") in {"1", "true", "True"}:
            try:
                minimum, maximum = int(column.get("min", "0")), int(column.get("max", "0"))
            except ValueError as exc:
                raise _InvalidSource("hidden column") from exc
            if not 1 <= minimum <= maximum <= 16_384:
                raise _InvalidSource("hidden column")
            hidden_columns.append((minimum, maximum))
    drawing_ids: list[str] = []
    for drawing in root.findall(_S + "drawing"):
        relation_id = drawing.get(_R + "id")
        if not relation_id:
            raise _InvalidSource("drawing relationship")
        drawing_ids.append(relation_id)
    if len(drawing_ids) != len(set(drawing_ids)):
        raise _InvalidSource("duplicate drawing")
    return _Sheet(
        name=name,
        part=part,
        state=state,
        cells=cells,
        merges=_parse_merges(root),
        drawing_ids=tuple(drawing_ids),
        has_conditional_formatting=root.find(".//" + _S + "conditionalFormatting") is not None,
        has_formula=has_formula,
        hidden_rows=frozenset(hidden_rows),
        hidden_columns=tuple(hidden_columns),
    )


def _workbook_sheets(members: Mapping[str, bytes]) -> tuple[_Sheet, ...]:
    workbook = _xml(members, "xl/workbook.xml")
    relations = _relationships(members, "xl/workbook.xml")
    shared = _shared_strings(members)
    result: list[_Sheet] = []
    names: set[str] = set()
    sheets = workbook.find(_S + "sheets")
    if sheets is None:
        raise _InvalidSource("workbook sheets")
    for node in sheets.findall(_S + "sheet"):
        name = node.get("name")
        relation_id = node.get(_R + "id")
        state = node.get("state", "visible")
        if not name or not relation_id or _normalized(name) in names or state not in {"visible", "hidden", "veryHidden"}:
            raise _InvalidSource("workbook sheet")
        names.add(_normalized(name))
        relation = relations.get(relation_id)
        if relation is None or not relation[0].endswith("/worksheet"):
            raise _InvalidSource("worksheet relationship")
        part = relation[1]
        if not part.startswith("xl/worksheets/") or not part.endswith(".xml"):
            raise _InvalidSource("worksheet target")
        result.append(_parse_sheet(members, name, part, state, shared))
    if not result:
        raise _InvalidSource("empty workbook")
    return tuple(result)


def _merged_value(sheet: _Sheet, row: int, column: int) -> _Cell | None:
    direct = sheet.cells.get((row, column))
    matches = [value for value in sheet.merges if value[0] <= row <= value[2] and value[1] <= column <= value[3]]
    if len(matches) > 1:
        raise _InvalidSource("merge ambiguity")
    if not matches:
        return direct
    r1, c1, _, _ = matches[0]
    top = sheet.cells.get((r1, c1))
    if direct is not None and direct.display.strip() and (row, column) != (r1, c1):
        raise _InvalidSource("non-top-left merged value")
    return top


def _aggregate(caption: str) -> tuple[str, str | None] | None:
    rendered = unicodedata.normalize("NFKC", caption).strip()
    if _compact_key(rendered) in {"個数", "件数", "count"}:
        return "rows", None
    match = re.fullmatch(r"(?:個数|件数|count(?:\s+of)?)\s*/?\s*(.+)", rendered, flags=re.IGNORECASE)
    if match is None or not match.group(1).strip():
        return None
    return "nonblank", match.group(1).strip()


def _native_projection(sheet: _Sheet, yellow_styles: frozenset[int]) -> _Projection | None:
    if sheet.has_conditional_formatting or sheet.has_formula or sheet.drawing_ids:
        return None
    yellow = [(coord, cell) for coord, cell in sheet.cells.items() if cell.style in yellow_styles]
    if len(yellow) != 1:
        return None
    (target_row, target_column), target = yellow[0]
    if target_row in sheet.hidden_rows or any(left <= target_column <= right for left, right in sheet.hidden_columns):
        return None
    highlighted = _decimal(target.display)
    if highlighted is None:
        return None
    header_candidates: list[tuple[int, tuple[_Cell, ...]]] = []
    for row in range(1, min(target_row, 51)):
        values = tuple(_merged_value(sheet, row, column) for column in range(1, target_column + 1))
        if all(value is not None and value.display.strip() for value in values):
            assert all(value is not None for value in values)
            concrete = tuple(value for value in values if value is not None)
            if _aggregate(concrete[-1].display) is not None:
                header_candidates.append((row, concrete))
    if len(header_candidates) != 1:
        return None
    header_row, headers = header_candidates[0]
    keys = tuple(_field_key(cell.display) for cell in headers)
    if any(not key for key in keys) or len(keys) != len(set(keys)):
        return None
    fields = tuple(cell.display.strip() for cell in headers[:-1])
    values: list[str] = []
    for column in range(1, target_column):
        candidates = []
        for row in range(header_row + 1, target_row + 1):
            cell = _merged_value(sheet, row, column)
            if cell is not None and cell.display.strip():
                candidates.append((row, cell.display.strip()))
        if not candidates:
            return None
        value = candidates[-1][1]
        if _compact_key(value) in _SUBTOTAL_KEYS:
            return None
        values.append(value)
    return _Projection(fields, tuple(values), headers[-1].display.strip(), highlighted)


def _emf_records(data: bytes) -> tuple[tuple[_EmfRun, ...], tuple[_Rect, ...]]:
    position = 0
    record_index = 0
    declared_records: int | None = None
    saw_eof = False
    text_align = 0
    current_position: tuple[int, int] | None = None
    current_position_fresh = False
    objects: dict[int, tuple[str, int, int]] = {}
    selected_brush: int | None = None
    state_stack: list[tuple[int, tuple[int, int] | None, bool, int | None]] = []
    runs: list[_EmfRun] = []
    rectangles: list[_Rect] = []
    yellow_brushes: set[int] = set()
    yellow_uses: dict[int, int] = {}
    while position < len(data):
        if position + 8 > len(data) or record_index >= _MAX_EMF_RECORDS:
            raise _InvalidSource("EMF record")
        record_type, size = struct.unpack_from("<II", data, position)
        if size < 8 or size % 4 or position + size > len(data):
            raise _InvalidSource("EMF record size")
        if record_type not in _ALLOWED_EMF_RECORD_TYPES:
            raise _InvalidSource("unsupported EMF record")
        required_size = _FIXED_EMF_RECORD_SIZES.get(record_type)
        if required_size is not None and size != required_size:
            raise _InvalidSource("EMF fixed record size")
        if record_index == 0:
            if record_type != 1 or size < 88:
                raise _InvalidSource("EMF header")
            signature, version, declared_bytes, declared_records = struct.unpack_from(
                "<IIII", data, position + 40
            )
            if (
                signature != 0x464D4520
                or version < 0x00010000
                or declared_bytes != len(data)
                or not 2 <= declared_records <= _MAX_EMF_RECORDS
            ):
                raise _InvalidSource("EMF header")
        elif record_type == 1:
            raise _InvalidSource("duplicate EMF header")
        if record_type in _UNSUPPORTED_TRANSFORMS:
            raise _InvalidSource("EMF transform")
        if record_type in _COORDINATE_STATE_RECORDS and (runs or rectangles):
            raise _InvalidSource("EMF coordinate state")
        if record_type == 10:
            # Per MS-EMF, type 10 is EMR_SETWINDOWORGEX (window extents are
            # type 9).  Recovered text and PATCOPY coordinates are compared in
            # one unshifted logical space, so only the actual zero origin is
            # certified.  Extent records (types 9/11) are not allowlisted.
            if struct.unpack_from("<ii", data, position + 8) != (0, 0):
                raise _InvalidSource("EMF window origin")
        elif record_type == 22:
            if size != 12:
                raise _InvalidSource("EMF text align")
            text_align = struct.unpack_from("<I", data, position + 8)[0]
            if text_align not in {0, 1}:
                raise _InvalidSource("EMF text align")
        elif record_type == 27:
            if size != 16:
                raise _InvalidSource("EMF move")
            current_position = struct.unpack_from("<ii", data, position + 8)
            current_position_fresh = True
        elif record_type == 33:
            if size != 8 or len(state_stack) >= 1024:
                raise _InvalidSource("EMF save")
            state_stack.append((text_align, current_position, current_position_fresh, selected_brush))
        elif record_type == 34:
            if size != 12 or struct.unpack_from("<i", data, position + 8)[0] != -1 or not state_stack:
                raise _InvalidSource("EMF restore")
            text_align, current_position, current_position_fresh, selected_brush = state_stack.pop()
        elif record_type == 38:
            if size != 28:
                raise _InvalidSource("EMF pen")
            handle = struct.unpack_from("<I", data, position + 8)[0]
            if handle == 0 or handle in objects or len(objects) >= _MAX_GDI_OBJECTS:
                raise _InvalidSource("EMF object")
            objects[handle] = ("pen", 0, 0)
        elif record_type == 82:
            # EMR_EXTCREATEFONTINDIRECTW has a fixed handle followed by a
            # version-dependent EXTLOGFONTW payload.  Its payload is not used
            # for table geometry, but tracking its handle keeps SELECTOBJECT
            # type-safe (selecting a font must not change the selected brush).
            if size < 332 or size % 4:
                raise _InvalidSource("EMF font")
            handle = struct.unpack_from("<I", data, position + 8)[0]
            if handle == 0 or handle in objects or len(objects) >= _MAX_GDI_OBJECTS:
                raise _InvalidSource("EMF object")
            objects[handle] = ("font", 0, 0)
        elif record_type == 39:
            if size != 24:
                raise _InvalidSource("EMF brush")
            handle, style, color, hatch = struct.unpack_from("<IIII", data, position + 8)
            if handle == 0 or handle in objects or len(objects) >= _MAX_GDI_OBJECTS:
                raise _InvalidSource("EMF object")
            if style != 0 or hatch != 0:
                raise _InvalidSource("EMF brush style")
            objects[handle] = ("brush", style, color)
            if color == _YELLOW_COLORREF:
                yellow_brushes.add(handle)
                yellow_uses[handle] = 0
        elif record_type == 37:
            if size != 12:
                raise _InvalidSource("EMF select")
            handle = struct.unpack_from("<I", data, position + 8)[0]
            if handle & 0x80000000:
                selected_brush = None
            elif handle not in objects:
                raise _InvalidSource("EMF unknown object")
            elif objects[handle][0] == "brush":
                selected_brush = handle
        elif record_type == 40:
            if size != 12:
                raise _InvalidSource("EMF delete")
            handle = struct.unpack_from("<I", data, position + 8)[0]
            if handle not in objects or handle == selected_brush:
                raise _InvalidSource("EMF delete object")
            del objects[handle]
        elif record_type == 76:
            if size < 100:
                raise _InvalidSource("EMF BITBLT")
            x, y, cx, cy, raster = struct.unpack_from("<iiiii", data, position + 24)
            if x < 0 or y < 0 or cx < 0 or cy < 0:
                raise _InvalidSource("EMF BITBLT")
            if raster == _PATCOPY:
                if selected_brush is None or selected_brush not in objects:
                    raise _InvalidSource("EMF BITBLT brush")
                kind, style, color = objects[selected_brush]
                if kind != "brush" or style != 0:
                    raise _InvalidSource("EMF BITBLT brush")
                # Excel emits zero-height/zero-width grid-line no-ops.  They
                # have no visible area and therefore are not highlight uses.
                if cx and cy:
                    rectangles.append(_Rect(record_index, x, y, cx, cy, color))
                    if selected_brush in yellow_uses:
                        yellow_uses[selected_brush] += 1
            elif selected_brush in yellow_uses:
                # A non-pattern blit while the unique yellow brush is selected
                # is visually ambiguous without decoding its source bitmap.
                raise _InvalidSource("EMF yellow bitmap use")
        elif record_type == 70:
            if size < 12:
                raise _InvalidSource("EMF comment")
            payload_size = struct.unpack_from("<I", data, position + 8)[0]
            padded_size = (12 + payload_size + 3) // 4 * 4
            if payload_size == 0 or padded_size != size:
                raise _InvalidSource("EMF comment")
            padding = data[position + 12 + payload_size : position + size]
            if any(padding):
                raise _InvalidSource("EMF comment padding")
        elif record_type == 81:
            if size < 80:
                raise _InvalidSource("EMF DIB")
            x_dest, y_dest = struct.unpack_from("<ii", data, position + 24)
            off_bmi, cb_bmi, off_bits, cb_bits = struct.unpack_from(
                "<IIII", data, position + 48
            )
            usage = struct.unpack_from("<I", data, position + 64)[0]
            cx_dest, cy_dest = struct.unpack_from("<ii", data, position + 72)
            if (
                x_dest < 0
                or y_dest < 0
                or cx_dest <= 0
                or cy_dest <= 0
                or usage not in {0, 1, 2}
                or off_bmi < 80
                or cb_bmi == 0
                or off_bits < 80
                or cb_bits == 0
                or off_bmi + cb_bmi > size
                or off_bits + cb_bits > size
            ):
                raise _InvalidSource("EMF DIB")
        elif record_type == 84:
            if size < 76:
                raise _InvalidSource("EMF text")
            x, y, chars, offset = struct.unpack_from("<iiII", data, position + 36)
            if text_align == 1:
                if current_position is None or not current_position_fresh or current_position != (x, y):
                    raise _InvalidSource("EMF text position")
                x, y = current_position
                current_position_fresh = False
            byte_count = chars * 2
            if (
                chars <= 0
                or chars > _MAX_EMF_CHARS
                or offset < 76
                or offset % 2
                or offset + byte_count > size
                or x < 0
                or y < 0
            ):
                raise _InvalidSource("EMF text")
            try:
                text = data[position + offset : position + offset + byte_count].decode("utf-16le", errors="strict")
            except UnicodeDecodeError as exc:
                raise _InvalidSource("EMF UTF-16") from exc
            if "\x00" in text:
                raise _InvalidSource("EMF text")
            if text.strip():
                runs.append(_EmfRun(record_index, x, y, text))
                if len(runs) > _MAX_EMF_TEXT_RUNS:
                    raise _InvalidSource("EMF text count")
        if record_type == 14:
            if size != 20:
                raise _InvalidSource("EMF EOF")
            palette_entries, palette_offset, last_size = struct.unpack_from("<III", data, position + 8)
            if palette_entries != 0 or palette_offset != 16 or last_size != 20 or position + size != len(data):
                raise _InvalidSource("EMF EOF")
            saw_eof = True
        position += size
        record_index += 1
    if (
        not saw_eof
        or not runs
        or declared_records != record_index
        or state_stack
        or len(yellow_brushes) != 1
        or list(yellow_uses.values()) != [1]
    ):
        raise _InvalidSource("EMF state")
    return tuple(runs), tuple(rectangles)


def _cluster_y(runs: Iterable[_EmfRun], tolerance: int = 3) -> tuple[tuple[_EmfRun, ...], ...]:
    groups: list[list[_EmfRun]] = []
    for run in sorted(runs, key=lambda value: (value.y, value.x, value.record)):
        if not groups:
            groups.append([run])
            continue
        center = sum(value.y for value in groups[-1]) / len(groups[-1])
        if abs(run.y - center) <= tolerance:
            groups[-1].append(run)
        else:
            groups.append([run])
    return tuple(tuple(group) for group in groups)


def _emf_projection(data: bytes) -> _Projection | None:
    runs, rectangles = _emf_records(data)
    yellow = [rect for rect in rectangles if rect.color == _YELLOW_COLORREF]
    if len(yellow) != 1:
        return None
    marker = yellow[0]
    marked_runs = [
        run
        for run in runs
        if marker.x <= run.x < marker.x + marker.cx and marker.y <= run.y < marker.y + marker.cy
    ]
    if len(marked_runs) != 1:
        return None
    highlighted = _decimal(marked_runs[0].text)
    if highlighted is None:
        return None
    header_groups: list[tuple[_EmfRun, ...]] = []
    for group in _cluster_y(run for run in runs if run.y < marker.y):
        ordered = tuple(sorted(group, key=lambda value: (value.x, value.record)))
        if not 2 <= len(ordered) <= 16:
            continue
        target_headers = [run for run in ordered if marker.x <= run.x < marker.x + marker.cx and _aggregate(run.text) is not None]
        if len(target_headers) == 1 and target_headers[0] is ordered[-1]:
            header_groups.append(ordered)
    if len(header_groups) != 1:
        return None
    headers = header_groups[0]
    header_y = sum(run.y for run in headers) / len(headers)
    padding = headers[-1].x - marker.x
    if not 0 <= padding <= 32:
        return None
    boundaries = [run.x - padding for run in headers]
    boundaries.append(marker.x + marker.cx)
    if boundaries[-2] != marker.x or boundaries[0] < 0 or any(
        right - left < 20 for left, right in zip(boundaries, boundaries[1:])
    ):
        return None
    for index, run in enumerate(headers):
        if not boundaries[index] <= run.x < boundaries[index + 1]:
            return None
    fields = tuple(run.text.strip() for run in headers[:-1])
    keys = tuple(_field_key(value) for value in fields + (headers[-1].text,))
    if any(not value for value in keys) or len(keys) != len(set(keys)):
        return None
    values: list[str] = []
    for column in range(len(fields)):
        candidates: dict[int, list[_EmfRun]] = {}
        for run in runs:
            if run.y <= header_y + 3 or run.y > marked_runs[0].y:
                continue
            if boundaries[column] <= run.x < boundaries[column + 1]:
                candidates.setdefault(run.y, []).append(run)
        if not candidates:
            return None
        latest_y = max(candidates)
        cell_runs = candidates[latest_y]
        if len(cell_runs) != 1:
            return None
        value = cell_runs[0].text.strip()
        if not value or _compact_key(value) in _SUBTOTAL_KEYS:
            return None
        values.append(value)
    return _Projection(fields, tuple(values), headers[-1].text.strip(), highlighted)


def _embedded_emf(members: Mapping[str, bytes], sheet: _Sheet) -> bytes | None:
    if sheet.cells or sheet.merges or sheet.has_conditional_formatting or sheet.has_formula or len(sheet.drawing_ids) != 1:
        return None
    sheet_relations = _relationships(members, sheet.part)
    relation = sheet_relations.get(sheet.drawing_ids[0])
    if relation is None or not relation[0].endswith("/drawing"):
        return None
    drawing_part = relation[1]
    if not drawing_part.startswith("xl/drawings/") or not drawing_part.endswith(".xml"):
        return None
    drawing = _xml(members, drawing_part)
    anchors = list(drawing.findall(_XDR + "twoCellAnchor")) + list(drawing.findall(_XDR + "oneCellAnchor"))
    if len(anchors) != 1:
        return None
    pictures = anchors[0].findall(".//" + _XDR + "pic")
    if len(pictures) != 1:
        return None
    blips = pictures[0].findall(".//" + _A + "blip")
    if len(blips) != 1:
        return None
    embed = blips[0].get(_R + "embed")
    if not embed:
        return None
    drawing_relations = _relationships(members, drawing_part)
    image = drawing_relations.get(embed)
    if image is None or not image[0].endswith("/image"):
        return None
    image_part = image[1]
    if not image_part.startswith("xl/media/") or not image_part.casefold().endswith(".emf"):
        return None
    return members.get(image_part)


def _raw_header(sheet: _Sheet, required: frozenset[str]) -> tuple[int, dict[str, int]] | None:
    if sheet.state != "visible" or sheet.has_formula or sheet.merges or sheet.drawing_ids or sheet.has_conditional_formatting:
        return None
    candidates: list[tuple[int, dict[str, int]]] = []
    for row in range(1, 21):
        values = [(column, cell.display.strip()) for (cell_row, column), cell in sheet.cells.items() if cell_row == row and cell.display.strip()]
        if not values:
            continue
        mapping: dict[str, int] = {}
        duplicate = False
        for column, value in values:
            key = _field_key(value)
            if not key or key in mapping:
                duplicate = True
                break
            mapping[key] = column
        if duplicate:
            continue
        if required.issubset(mapping):
            candidates.append((row, mapping))
    return candidates[0] if len(candidates) == 1 else None


def _value_equal(cell: _Cell | None, expected: str) -> bool:
    if cell is None:
        return False
    left = _decimal(cell.display)
    right = _decimal(expected)
    if left is not None and right is not None:
        return left == right
    return unicodedata.normalize("NFKC", cell.display).strip() == unicodedata.normalize("NFKC", expected).strip()


def _verify_raw(sheets: Sequence[_Sheet], target: _Sheet, projection: _Projection) -> bool:
    aggregate = _aggregate(projection.aggregate_caption)
    if aggregate is None:
        return False
    mode, count_field = aggregate
    condition_keys = tuple(_field_key(value) for value in projection.fields)
    if any(not value for value in condition_keys) or len(condition_keys) != len(set(condition_keys)):
        return False
    required = set(condition_keys)
    if count_field is not None:
        required.add(_field_key(count_field))
    candidates: list[tuple[_Sheet, int, dict[str, int]]] = []
    for sheet in sheets:
        if sheet is target:
            continue
        header = _raw_header(sheet, frozenset(required))
        if header is not None:
            candidates.append((sheet, header[0], header[1]))
    if len(candidates) != 1:
        return False
    raw, header_row, headers = candidates[0]
    maximum_row = max((row for row, _ in raw.cells), default=header_row)
    populated_rows = {row for row, _ in raw.cells}
    matched = 0
    for row in range(header_row + 1, maximum_row + 1):
        if row not in populated_rows:
            continue
        if not all(
            _value_equal(raw.cells.get((row, headers[key])), expected)
            for key, expected in zip(condition_keys, projection.values)
        ):
            continue
        if mode == "rows":
            matched += 1
        else:
            assert count_field is not None
            cell = raw.cells.get((row, headers[_field_key(count_field)]))
            if cell is not None and cell.display.strip():
                matched += 1
    return Decimal(matched) == projection.highlighted


def _answer(projection: _Projection) -> str:
    conditions = "、".join(
        f"{field}={value}" for field, value in zip(projection.fields, projection.values)
    )
    aggregate = _aggregate(projection.aggregate_caption)
    if aggregate is None:
        raise _InvalidSource("aggregate caption became invalid during formatting")
    mode, count_field = aggregate
    aggregation = "行の個数（COUNT）" if mode == "rows" else f"{count_field}の個数（COUNT）"
    return f"抽出条件：{conditions}。集計内容：{aggregation}。"


def _decision(answer: str, path: Path, root: Path, operations: int) -> StructuredCandidateDecision:
    return StructuredCandidateDecision(
        "resolved",
        "certified_xlsx_highlight_projection",
        StructuredCandidateAnswer(
            answer=answer,
            source_paths=(unicodedata.normalize("NFC", path.relative_to(root).as_posix()),),
            source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
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
    bindings = contract["bindings"]
    root = _source_root(engine)
    if root is None:
        return _hold("xlsx_highlight_source_root_invalid")
    paths = _named_workbooks(engine, bindings["location"], bindings["container"])
    if len(paths) != 1:
        return _hold("xlsx_highlight_workbook_not_unique")
    try:
        with zipfile.ZipFile(paths[0]) as archive:
            members = _validate_archive(archive)
        sheets = _workbook_sheets(members)
        selected = [
            sheet
            for sheet in sheets
            if sheet.state == "visible" and _normalized(sheet.name) == _normalized(bindings["sheet"])
        ]
        if len(selected) != 1:
            return _hold("xlsx_highlight_sheet_not_unique")
        target = selected[0]
        yellow_styles, style_count = _yellow_styles(members)
        if any(cell.style >= style_count for sheet in sheets for cell in sheet.cells.values()):
            raise _InvalidSource("cell style index")
        projection = _native_projection(target, yellow_styles)
        if projection is None:
            emf = _embedded_emf(members, target)
            projection = _emf_projection(emf) if emf is not None else None
        if projection is None:
            return _hold("xlsx_highlight_marker_not_unique")
        if not _verify_raw(sheets, target, projection):
            return _hold("xlsx_highlight_raw_mismatch")
        answer = _answer(projection)
    except (
        OSError,
        RuntimeError,
        KeyError,
        TypeError,
        ValueError,
        struct.error,
        zipfile.BadZipFile,
    ):
        return _hold("xlsx_highlight_source_invalid")
    return _decision(answer, paths[0], root, len(contract["operation_graph"]["nodes"]))


__all__ = [
    "HIGHLIGHT_PROJECTION",
    "XLSX_HIGHLIGHT_PROJECTION_RULE_VERSION",
    "decide_question",
    "graph_contract_for_question",
    "validate_graph_contract",
]
