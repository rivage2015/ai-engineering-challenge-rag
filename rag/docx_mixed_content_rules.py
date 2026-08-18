"""Fail-closed rules for mixed native content inside DOCX packages.

The rules in this module read authored package data rather than raster OCR:

* nested ``w:tbl`` structures are read recursively without flattening child
  tables into their parent cell;
* EMF ``EMR_EXTTEXTOUTW`` records retain the original UTF-16 text and device
  coordinates, allowing a vector table to be reconstructed without OCR;
* native chart caches are resolved in document-body order and external chart
  links are never followed;
* comment ranges are joined to their authored anchor text by comment id.

Every question grammar is complete.  A non-unique source, table, row, series,
point, comment range, or unit returns a hold decision.
"""

from __future__ import annotations

import bisect
import colorsys
import hashlib
import json
import posixpath
import re
import struct
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from structured_candidate import (
    StructuredCandidateAnswer,
    StructuredCandidateDecision,
    _candidate_values,
    _location_matches,
)


DOCX_MIXED_RULE_VERSION = "0.1"

EMF_AVERAGE_DIFFERENCE = re.compile(
    r"^(?P<location>.+?)の(?P<document>[^、。]+?)資料において、"
    r"(?P<basis>[^、。]+?)における(?P<left>[^、。]+?)と"
    r"(?P<right>[^、。]+?)の差はいくらですか。?$"
)

NESTED_TABLE_DIFFERENCE = re.compile(
    r"^(?P<location>.+?)の(?P<document>[^、。]+?)において、"
    r"(?P<source>[^、。]+?)\s*が公表している(?P<subject>[^、。]+?)について、"
    r"(?P<upper>上位[0-9０-９]+%)の層と(?P<baseline>中央値)の差は"
    r"いくらですか。?$"
)

NATIVE_CHART_POINT = re.compile(
    r"^(?P<location>.+?)の(?P<container>[^、。]+?\.docx)のグラフ"
    r"(?P<chart>[0-9０-９]+)で、x=(?P<x>[+-]?[0-9０-９]+(?:\.[0-9０-９]+)?)"
    r"のときの(?:(?P<color>[^、。]+?)の折れ線の)?yの値を"
    r"小数第(?P<digits>[0-9０-９]+)位で答えてください。?$"
)

COMMENTED_ANCHOR_TEXT = re.compile(
    r"^(?P<location>.+?)の(?P<document>会議録)において、"
    r"コメントがついている部分をそのまま抽出してください。?$"
)


_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_C_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PR_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_W = "{" + _W_NS + "}"
_A = "{" + _A_NS + "}"
_C = "{" + _C_NS + "}"
_R = "{" + _R_NS + "}"
_PR = "{" + _PR_NS + "}"

_MAX_DOCX_BYTES = 256 * 1024 * 1024
_MAX_ZIP_ENTRIES = 10_000
_MAX_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 200.0
_MAX_EMF_RECORDS = 100_000
_MAX_EMF_TEXT_RUNS = 10_000
_MAX_EMF_CHARS = 16_384
_MAX_CHART_POINTS = 100_000
_MAX_RENDERED_INTEGER_DIGITS = 1_000
_EMF_COORDINATE_STATE_RECORDS = {9, 10, 11, 12, 16, 17, 31, 32}
_XML_FORBIDDEN = (b"<!DOCTYPE", b"<!ENTITY")

_ARCHIVE_ENGLISH = re.compile(
    r"(?:^|[._\-\s])(?:old|draft|copy|backup|bak|archive|archived|obsolete|tmp)"
    r"(?:$|[._\-\s])",
    flags=re.IGNORECASE,
)
_ARCHIVE_JAPANESE = ("旧", "過去", "草案", "ドラフト", "コピー", "バックアップ", "アーカイブ")

_COLOR_ALIASES = {
    "青": "blue",
    "青色": "blue",
    "blue": "blue",
    "赤": "red",
    "赤色": "red",
    "red": "red",
    "橙": "orange",
    "オレンジ": "orange",
    "orange": "orange",
    "黄": "yellow",
    "黄色": "yellow",
    "yellow": "yellow",
    "緑": "green",
    "緑色": "green",
    "green": "green",
}


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


def _compact_key(value: object) -> str:
    return re.sub(r"\s+", "", _normalized(value))


def _document_key(value: object) -> str:
    rendered = _compact_key(value)
    if rendered.endswith(".docx"):
        rendered = rendered[:-5]
    if rendered.endswith("資料") and len(rendered) > 2:
        rendered = rendered[:-2]
    return rendered


def _is_archived_component(value: str) -> bool:
    rendered = _normalized(value)
    return bool(_ARCHIVE_ENGLISH.search(rendered)) or any(
        marker in rendered for marker in _ARCHIVE_JAPANESE
    )


def _safe_root(engine: Any) -> Path | None:
    try:
        raw = Path(engine.source_root)
        if not raw.is_dir() or raw.is_symlink():
            return None
        return raw.resolve()
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


def _named_docx(
    engine: Any,
    location: str,
    document: str,
    *,
    meeting_scope: bool = False,
) -> tuple[Path, ...]:
    root = _safe_root(engine)
    if root is None:
        return ()
    locations = _candidate_values(location, getattr(engine, "glossary", None))
    names = {
        _document_key(value)
        for value in _candidate_values(document, getattr(engine, "glossary", None))
    }
    matches: list[Path] = []
    try:
        for path in root.rglob("*.docx"):
            if (
                not path.is_file()
                or path.is_symlink()
                or path.name.startswith(("~$", "."))
                or _has_symlink_component(path, root)
            ):
                continue
            relative = path.relative_to(root)
            if any(_is_archived_component(part) for part in relative.parts):
                continue
            if not _location_matches(relative.parts[:-1], locations):
                continue
            if meeting_scope:
                if not any("会議録" in _compact_key(part) for part in relative.parts[:-1]):
                    continue
                if "会議録" not in _document_key(path.stem):
                    continue
            elif _document_key(path.stem) not in names:
                continue
            size = path.stat().st_size
            if not 0 < size <= _MAX_DOCX_BYTES:
                continue
            matches.append(path.resolve())
    except OSError:
        return ()
    return tuple(sorted(set(matches), key=lambda item: item.as_posix()))


def _validate_archive(archive: zipfile.ZipFile) -> bool:
    infos = archive.infolist()
    if not infos or len(infos) > _MAX_ZIP_ENTRIES:
        return False
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        return False
    total = 0
    for info in infos:
        name = info.filename
        pure = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or pure.is_absolute()
            or ".." in pure.parts
            or info.flag_bits & 0x1
            or info.file_size < 0
            or info.file_size > _MAX_MEMBER_BYTES
        ):
            return False
        total += info.file_size
        if total > _MAX_TOTAL_BYTES:
            return False
        if info.file_size and not info.compress_size:
            return False
        if info.compress_size and info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO:
            return False
    return True


def _read_member(
    archive: zipfile.ZipFile,
    name: str,
    *,
    xml: bool = False,
) -> bytes | None:
    pure = PurePosixPath(name)
    if pure.is_absolute() or ".." in pure.parts or "\\" in name:
        return None
    try:
        info = archive.getinfo(name)
        if info.is_dir() or not 0 < info.file_size <= _MAX_MEMBER_BYTES:
            return None
        value = archive.read(name)
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile):
        return None
    if len(value) != info.file_size:
        return None
    if xml and any(marker in value for marker in _XML_FORBIDDEN):
        return None
    return value


def _xml_root(archive: zipfile.ZipFile, name: str) -> ET.Element | None:
    value = _read_member(archive, name, xml=True)
    if value is None:
        return None
    try:
        return ET.fromstring(value)
    except ET.ParseError:
        return None


def _relationship_map(root: ET.Element) -> dict[str, tuple[str, str, str | None]] | None:
    result: dict[str, tuple[str, str, str | None]] = {}
    for node in root:
        if node.tag != _PR + "Relationship":
            continue
        relation_id = node.get("Id")
        relation_type = node.get("Type")
        target = node.get("Target")
        if not relation_id or not relation_type or not target or relation_id in result:
            return None
        result[relation_id] = (relation_type, target, node.get("TargetMode"))
    return result


def _safe_part(base: str, target: str, required_prefix: str) -> str | None:
    if ":" in target or target.startswith(("/", "\\")) or "\\" in target:
        return None
    value = posixpath.normpath(posixpath.join(posixpath.dirname(base), target))
    if value == ".." or value.startswith("../") or not value.startswith(required_prefix):
        return None
    return value


def _nodes(operators: Sequence[str]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append(
            {
                "operation_id": f"op_{index:03d}_{operator}",
                "operator": operator,
                "input_refs": [previous],
                "output_ref": output,
            }
        )
        previous = output
    return nodes


def _contract(
    question: str,
    rule_id: str,
    bindings: Mapping[str, Any],
    scope: Mapping[str, Any],
    operators: Sequence[str],
    *,
    value_type: str,
    display_precision: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    nodes = _nodes(operators)
    core = {
        "graph_rule_version": DOCX_MIXED_RULE_VERSION,
        "rule_id": rule_id,
        "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
        "bindings": dict(bindings),
        "scope": dict(scope),
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
                "value_type": value_type,
                "unit": None,
            },
            "display_precision": dict(display_precision) if display_precision else None,
            "required_keys": None,
        },
    }
    return {
        "graph_contract_id": "docx_mixed_"
        + hashlib.sha256(_canonical_json(core).encode("utf-8")).hexdigest()[:32],
        **core,
    }


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if not isinstance(question, str):
        return None
    match = EMF_AVERAGE_DIFFERENCE.fullmatch(question)
    if match:
        bindings = {key: match[key] for key in ("location", "document", "basis", "left", "right")}
        return _contract(
            question,
            "docx_emf_table_average_difference",
            bindings,
            {
                "location": bindings["location"],
                "container": bindings["document"] + ".docx",
                "source_channel": "embedded_emf_unicode_text",
                "measure": bindings["basis"],
                "rows": [bindings["left"], bindings["right"]],
            },
            (
                "retrieve",
                "resolve_embedded_image",
                "parse_native_unicode",
                "restore_table",
                "select_rows",
                "select_measure",
                "absolute_distance",
                "format_unit",
            ),
            value_type="number",
        )
    match = NESTED_TABLE_DIFFERENCE.fullmatch(question)
    if match:
        bindings = {
            key: match[key]
            for key in ("location", "document", "source", "subject", "upper", "baseline")
        }
        return _contract(
            question,
            "docx_nested_table_value_difference",
            bindings,
            {
                "location": bindings["location"],
                "container": bindings["document"] + ".docx",
                "source_channel": "wordprocessingml_nested_table",
                "row_selector": bindings["source"],
                "columns": [bindings["upper"], bindings["baseline"]],
            },
            (
                "retrieve",
                "parse_nested_tables",
                "select_unique_table",
                "select_unique_row",
                "select_columns",
                "absolute_distance",
                "format_unit",
            ),
            value_type="number",
        )
    match = NATIVE_CHART_POINT.fullmatch(question)
    if match:
        digits = int(unicodedata.normalize("NFKC", match["digits"]))
        chart = int(unicodedata.normalize("NFKC", match["chart"]))
        if not 1 <= chart <= 100 or not 0 <= digits <= 12:
            return None
        bindings = {
            key: match[key]
            for key in ("location", "container", "chart", "x", "color", "digits")
        }
        return _contract(
            question,
            "docx_native_chart_point_lookup",
            bindings,
            {
                "location": bindings["location"],
                "container": bindings["container"],
                "chart_ordinal": chart,
                "series_color": bindings["color"],
                "x": bindings["x"],
                "source_channel": "wordprocessingml_chart_cache",
                "external_links": "cache_only_not_followed",
            },
            (
                "retrieve",
                "resolve_chart_order",
                "parse_chart_cache",
                "resolve_series",
                "select_x",
                "round",
            ),
            value_type="number",
            display_precision={"mode": "decimal_places", "digits": digits},
        )
    match = COMMENTED_ANCHOR_TEXT.fullmatch(question)
    if match:
        bindings = {key: match[key] for key in ("location", "document")}
        return _contract(
            question,
            "docx_commented_anchor_text",
            bindings,
            {
                "location": bindings["location"],
                "container": "05.会議/会議録/*.docx",
                "source_channel": "word_comment_range",
                "projection": "anchor_text",
            },
            ("retrieve", "resolve_comment_part", "join_comment_range", "verify_unique", "project"),
            value_type="string",
        )
    return None


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    if expected is None or not isinstance(contract, Mapping):
        return False
    try:
        return _canonical_json(expected) == _canonical_json(contract)
    except (TypeError, ValueError):
        return False


def _decision(
    answer: str,
    paths: Sequence[Path],
    root: Path,
    operations: int,
) -> StructuredCandidateDecision:
    relative = tuple(
        sorted(
            {
                unicodedata.normalize("NFC", path.relative_to(root).as_posix())
                for path in paths
            }
        )
    )
    digest = hashlib.sha256()
    for path in sorted(set(paths), key=lambda item: item.as_posix()):
        digest.update(path.read_bytes())
    return StructuredCandidateDecision(
        "resolved",
        "certified_docx_mixed",
        StructuredCandidateAnswer(
            answer=answer,
            source_paths=relative,
            source_sha256=digest.hexdigest(),
            operation_count=operations,
            output_count=1,
        ),
    )


def _hold(reason: str) -> StructuredCandidateDecision:
    return StructuredCandidateDecision("hold", reason)


def _currency_units(value: str) -> frozenset[str]:
    """Return every explicitly authored currency marker in ``value``."""

    key = _compact_key(value)
    units: set[str] = set()
    if "米ドル" in key or "ドル" in key or "usd" in key or "$" in key:
        units.add("ドル")
    if "円" in key or "jpy" in key:
        units.add("円")
    return frozenset(units)


def _currency_unit(*values: str) -> str | None:
    units = set().union(*(_currency_units(value) for value in values))
    return next(iter(units)) if len(units) == 1 else None


def _parse_number(value: str) -> Decimal | None:
    matches = re.findall(r"(?<![0-9])([+-]?[0-9]{1,3}(?:,[0-9]{3})*|[+-]?[0-9]+)(?![0-9])", value)
    if len(matches) != 1:
        return None
    try:
        result = Decimal(matches[0].replace(",", ""))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def _format_integer_distance(left: Decimal, right: Decimal, unit: str) -> str | None:
    distance = abs(left - right)
    if distance != distance.to_integral_value():
        return None
    coefficient = distance.as_tuple().digits
    rendered_digits = max(1, len(coefficient) + distance.as_tuple().exponent)
    if rendered_digits > _MAX_RENDERED_INTEGER_DIGITS:
        return None
    try:
        return f"{int(distance):,}{unit}"
    except (OverflowError, ValueError):
        return None


def _emf_text_runs(data: bytes) -> tuple[tuple[int, int, int, str], ...] | None:
    position = 0
    record_index = 0
    runs: list[tuple[int, int, int, str]] = []
    declared_records: int | None = None
    saw_eof = False
    text_align = 0
    current_position: tuple[int, int] | None = None
    current_position_fresh = False
    state_stack: list[tuple[int, tuple[int, int] | None, bool]] = []
    while position < len(data):
        if position + 8 > len(data) or record_index >= _MAX_EMF_RECORDS:
            return None
        record_type, size = struct.unpack_from("<II", data, position)
        if size < 8 or size % 4 or position + size > len(data):
            return None
        if record_index == 0:
            if record_type != 1 or size < 88:
                return None
            signature, version, declared_bytes, declared_records = struct.unpack_from(
                "<IIII", data, position + 40
            )
            if (
                signature != 0x464D4520
                or version < 0x00010000
                or declared_bytes != len(data)
                or not 2 <= declared_records <= _MAX_EMF_RECORDS
            ):
                return None
        if record_type in {35, 36, 83, 108}:
            return None
        # Text coordinates before this point may share one logical mapping,
        # but a midstream mapping change would mix incomparable coordinate
        # systems in the reconstructed grid.
        if record_type in _EMF_COORDINATE_STATE_RECORDS and runs:
            return None
        if record_type == 22:
            if size != 12:
                return None
            text_align = struct.unpack_from("<I", data, position + 8)[0]
            # Coordinate recovery below supports only left/top alignment, with
            # or without TA_UPDATECP.  Other modes need glyph metrics.
            if text_align not in {0, 1}:
                return None
        elif record_type == 27:
            if size != 16:
                return None
            current_position = struct.unpack_from("<ii", data, position + 8)
            current_position_fresh = True
        elif record_type == 33:
            if size != 8:
                return None
            state_stack.append((text_align, current_position, current_position_fresh))
        elif record_type == 34:
            if size != 12 or struct.unpack_from("<i", data, position + 8)[0] != -1:
                return None
            if not state_stack:
                return None
            text_align, current_position, current_position_fresh = state_stack.pop()
        if record_type == 84:
            if size < 76:
                return None
            x, y, chars, offset = struct.unpack_from("<iiII", data, position + 36)
            if text_align == 1:
                if (
                    current_position is None
                    or not current_position_fresh
                    or current_position != (x, y)
                ):
                    return None
                x, y = current_position
                # The post-text current position depends on glyph advances.
                # A fresh MoveToEx is required before another UPDATECP run.
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
                return None
            try:
                text = data[position + offset : position + offset + byte_count].decode(
                    "utf-16le", errors="strict"
                )
            except UnicodeDecodeError:
                return None
            if "\x00" in text:
                return None
            if text.strip():
                runs.append((record_index, x, y, text))
                if len(runs) > _MAX_EMF_TEXT_RUNS:
                    return None
        if record_type == 14:
            if size != 20:
                return None
            palette_entries, palette_offset, last_size = struct.unpack_from(
                "<III", data, position + 8
            )
            if palette_entries != 0 or palette_offset != 16 or last_size != 20:
                return None
            saw_eof = True
            if position + size != len(data):
                return None
        position += size
        record_index += 1
    if (
        not saw_eof
        or not runs
        or declared_records != record_index
        or state_stack
    ):
        return None
    return tuple(runs)


def _cluster_y(
    runs: Sequence[tuple[int, int, int, str]], tolerance: int = 3
) -> list[list[tuple[int, int, int, str]]]:
    groups: list[list[tuple[int, int, int, str]]] = []
    for run in sorted(runs, key=lambda item: (item[2], item[1], item[0])):
        if not groups:
            groups.append([run])
            continue
        center = sum(item[2] for item in groups[-1]) / len(groups[-1])
        if abs(run[2] - center) <= tolerance:
            groups[-1].append(run)
        else:
            groups.append([run])
    return groups


def _join_runs(runs: Iterable[tuple[int, int, int, str]]) -> str:
    value = "".join(item[3] for item in sorted(runs, key=lambda item: (item[1], item[2], item[0])))
    return re.sub(r"\s+", " ", value).strip()


def _emf_matrix(
    runs: Sequence[tuple[int, int, int, str]],
) -> tuple[tuple[str, ...], ...] | None:
    minimum_y = min(item[2] for item in runs)
    header_runs = [item for item in runs if item[2] <= minimum_y + 3]
    anchors = sorted({item[1] for item in header_runs})
    if not 2 <= len(anchors) <= 16 or any(
        right - left < 20 for left, right in zip(anchors, anchors[1:])
    ):
        return None

    def column_for(x: int) -> int | None:
        index = bisect.bisect_right(anchors, x) - 1
        return index if 0 <= index < len(anchors) else None

    header_cells: list[list[tuple[int, int, int, str]]] = [[] for _ in anchors]
    for run in header_runs:
        column = column_for(run[1])
        if column is None:
            return None
        header_cells[column].append(run)
    headers = tuple(_join_runs(value) for value in header_cells)
    if any(not value for value in headers) or len({_compact_key(value) for value in headers}) != len(headers):
        return None

    body = [item for item in runs if item not in header_runs]
    first_column = [item for item in body if column_for(item[1]) == 0]
    row_groups = _cluster_y(first_column)
    if not 2 <= len(row_groups) <= 10_000:
        return None
    row_centers = [sum(item[2] for item in group) / len(group) for group in row_groups]
    if len(row_centers) > 1:
        minimum_spacing = min(
            right - left for left, right in zip(row_centers, row_centers[1:])
        )
        if minimum_spacing <= 6:
            return None
        maximum_distance = minimum_spacing * 0.45
    else:
        maximum_distance = 30.0

    cells: list[list[list[tuple[int, int, int, str]]]] = [
        [[] for _ in anchors] for _ in row_groups
    ]
    for run in body:
        column = column_for(run[1])
        if column is None:
            return None
        distances = [abs(run[2] - center) for center in row_centers]
        minimum = min(distances)
        if minimum > maximum_distance or distances.count(minimum) != 1:
            return None
        cells[distances.index(minimum)][column].append(run)

    matrix = [headers]
    for row in cells:
        rendered = tuple(_join_runs(value) for value in row)
        if any(not value for value in rendered):
            return None
        matrix.append(rendered)
    row_keys = [_compact_key(row[0]) for row in matrix[1:]]
    if any(not value for value in row_keys) or len(row_keys) != len(set(row_keys)):
        return None
    return tuple(matrix)


def _embedded_emf_matrices(archive: zipfile.ZipFile) -> tuple[tuple[tuple[str, ...], ...], ...] | None:
    document = _xml_root(archive, "word/document.xml")
    relationships_root = _xml_root(archive, "word/_rels/document.xml.rels")
    if document is None or relationships_root is None:
        return None
    relationships = _relationship_map(relationships_root)
    if relationships is None:
        return None
    relation_ids: list[str] = []
    for node in document.iter():
        relation_id = None
        if node.tag == _A + "blip":
            relation_id = node.get(_R + "embed")
        elif node.tag.endswith("}imagedata"):
            relation_id = node.get(_R + "id")
        if relation_id:
            relation_ids.append(relation_id)
    if len(relation_ids) != len(set(relation_ids)):
        return None
    matrices: list[tuple[tuple[str, ...], ...]] = []
    for relation_id in relation_ids:
        relation = relationships.get(relation_id)
        if relation is None:
            return None
        relation_type, target, target_mode = relation
        if target_mode is not None or not relation_type.endswith("/image"):
            continue
        member = _safe_part("word/document.xml", target, "word/media/")
        if member is None or not member.casefold().endswith(".emf"):
            continue
        data = _read_member(archive, member)
        if data is None:
            return None
        runs = _emf_text_runs(data)
        matrix = _emf_matrix(runs) if runs is not None else None
        if matrix is None:
            return None
        matrices.append(matrix)
    return tuple(matrices)


def _select_column(headers: Sequence[str], requested: str) -> int | None:
    key = _compact_key(requested)
    matches = [index for index, value in enumerate(headers) if key and key in _compact_key(value)]
    return matches[0] if len(matches) == 1 else None


def _average_from_cell(value: str) -> Decimal | None:
    matches = list(
        re.finditer(r"平均(?:約)?\s*([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)", value)
    )
    if len(matches) != 1:
        return None
    # ``平均約 44,000～161,000`` is a range, not one certified mean.
    trailing = value[matches[0].end() :]
    if re.match(r"\s*(?:～|〜|~|–|—|-|から|to)\s*[0-9]", trailing, re.IGNORECASE):
        return None
    try:
        result = Decimal(matches[0].group(1).replace(",", ""))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def _emf_average_difference(
    engine: Any, match: re.Match[str]
) -> StructuredCandidateDecision:
    paths = _named_docx(engine, match["location"], match["document"])
    if len(paths) != 1:
        return _hold("docx_source_not_unique")
    try:
        with zipfile.ZipFile(paths[0]) as archive:
            if not _validate_archive(archive):
                return _hold("docx_archive_invalid")
            matrices = _embedded_emf_matrices(archive)
    except (OSError, RuntimeError, zipfile.BadZipFile):
        return _hold("docx_archive_invalid")
    if not matrices:
        return _hold("emf_table_not_found")
    candidates: list[tuple[Decimal, Decimal, str]] = []
    left_key = _compact_key(match["left"])
    right_key = _compact_key(match["right"])
    for matrix in matrices:
        column = _select_column(matrix[0], match["basis"])
        if column is None:
            continue
        rows = {_compact_key(row[0]): row for row in matrix[1:]}
        if left_key not in rows or right_key not in rows:
            continue
        left = _average_from_cell(rows[left_key][column])
        right = _average_from_cell(rows[right_key][column])
        unit = _currency_unit(
            matrix[0][column],
            rows[left_key][column],
            rows[right_key][column],
        )
        if left is not None and right is not None and unit is not None:
            candidates.append((left, right, unit))
    if len(candidates) != 1:
        return _hold("emf_table_semantics_not_unique")
    answer = _format_integer_distance(*candidates[0])
    if answer is None:
        return _hold("emf_difference_not_integral")
    root = _safe_root(engine)
    if root is None:
        return _hold("source_root_invalid")
    return _decision(answer, paths, root, 8)


def _direct_cell_text(cell: ET.Element) -> str:
    values: list[str] = []

    def visit(node: ET.Element) -> None:
        if node is not cell and node.tag == _W + "tbl":
            return
        if node.tag == _W + "t":
            values.append(node.text or "")
        elif node.tag == _W + "tab":
            values.append("\t")
        elif node.tag == _W + "br":
            values.append("\n")
        for child in node:
            visit(child)

    visit(cell)
    return re.sub(r"\s+", " ", "".join(values)).strip()


def _leaf_table_matrices(document: ET.Element) -> tuple[tuple[tuple[str, ...], ...], ...]:
    matrices: list[tuple[tuple[str, ...], ...]] = []
    for table in document.iter(_W + "tbl"):
        if any(child is not table for child in table.iter(_W + "tbl")):
            continue
        rows: list[tuple[str, ...]] = []
        for row in table.findall("./" + _W + "tr"):
            cells = tuple(_direct_cell_text(cell) for cell in row.findall("./" + _W + "tc"))
            if cells:
                rows.append(cells)
        if not rows:
            continue
        width = max(len(row) for row in rows)
        padded = [row + ("",) * (width - len(row)) for row in rows]
        while width and all(not row[width - 1] for row in padded):
            width -= 1
        if width:
            matrices.append(tuple(tuple(row[:width]) for row in padded))
    return tuple(matrices)


def _leaf_tables_with_context(
    document: ET.Element,
) -> tuple[tuple[tuple[tuple[str, ...], ...], tuple[str, ...]], ...]:
    """Return leaf tables with their nearby authored body paragraphs."""

    body = document.find(_W + "body")
    if body is None:
        return ()
    preceding: list[str] = []
    output: list[tuple[tuple[tuple[str, ...], ...], tuple[str, ...]]] = []
    for child in body:
        if child.tag == _W + "p":
            text = "".join(
                node.text or "" for node in child.iter() if node.tag == _W + "t"
            ).strip()
            if text:
                preceding.append(text)
        elif child.tag == _W + "tbl":
            context = tuple(preceding[-8:])
            output.extend(
                (matrix, context) for matrix in _leaf_table_matrices(child)
            )
    return tuple(output)


def _header_match(headers: Sequence[str], token: str) -> int | None:
    key = _compact_key(token)
    matches = [index for index, value in enumerate(headers) if key and key in _compact_key(value)]
    return matches[0] if len(matches) == 1 else None


def _nested_table_difference(
    engine: Any, match: re.Match[str]
) -> StructuredCandidateDecision:
    paths = _named_docx(engine, match["location"], match["document"])
    if len(paths) != 1:
        return _hold("docx_source_not_unique")
    candidates: list[tuple[Decimal, Decimal, str]] = []
    try:
        with zipfile.ZipFile(paths[0]) as archive:
            if not _validate_archive(archive):
                return _hold("docx_archive_invalid")
            document = _xml_root(archive, "word/document.xml")
            if document is None:
                return _hold("docx_document_xml_invalid")
            subject_key = _compact_key(match["subject"])
            for matrix, context in _leaf_tables_with_context(document):
                if not subject_key or subject_key not in _compact_key(" ".join(context)):
                    continue
                headers = matrix[0]
                source_columns = [
                    index
                    for index, value in enumerate(headers)
                    if "情報源" in _compact_key(value)
                    or "調査主体" in _compact_key(value)
                ]
                source_column = source_columns[0] if len(source_columns) == 1 else None
                upper_column = _header_match(headers, match["upper"])
                baseline_column = _header_match(headers, match["baseline"])
                if None in {source_column, upper_column, baseline_column}:
                    continue
                active_headers = [value for value in headers if value]
                if len({_compact_key(value) for value in active_headers}) != len(active_headers):
                    continue
                rows = [
                    row
                    for row in matrix[1:]
                    if _compact_key(row[int(source_column)]) == _compact_key(match["source"])
                ]
                if len(rows) != 1:
                    continue
                upper = _parse_number(rows[0][int(upper_column)])
                baseline = _parse_number(rows[0][int(baseline_column)])
                unit = _currency_unit(
                    headers[int(upper_column)],
                    headers[int(baseline_column)],
                    rows[0][int(upper_column)],
                    rows[0][int(baseline_column)],
                )
                if upper is not None and baseline is not None and unit is not None:
                    candidates.append((upper, baseline, unit))
    except (OSError, RuntimeError, zipfile.BadZipFile):
        return _hold("docx_archive_invalid")
    if len(candidates) != 1:
        return _hold("nested_table_semantics_not_unique")
    answer = _format_integer_distance(*candidates[0])
    if answer is None:
        return _hold("nested_table_difference_not_integral")
    root = _safe_root(engine)
    if root is None:
        return _hold("source_root_invalid")
    return _decision(answer, paths, root, 7)


def _chart_parts_in_body_order(
    archive: zipfile.ZipFile,
) -> tuple[str, ...] | None:
    document = _xml_root(archive, "word/document.xml")
    relationships_root = _xml_root(archive, "word/_rels/document.xml.rels")
    if document is None or relationships_root is None:
        return None
    relationships = _relationship_map(relationships_root)
    if relationships is None:
        return None
    parts: list[str] = []
    for node in document.iter(_C + "chart"):
        relation_id = node.get(_R + "id")
        relation = relationships.get(relation_id or "")
        if relation is None:
            return None
        relation_type, target, target_mode = relation
        if target_mode is not None or not relation_type.endswith("/chart"):
            return None
        part = _safe_part("word/document.xml", target, "word/charts/")
        if part is None or not part.casefold().endswith(".xml"):
            return None
        parts.append(part)
    if not parts or len(parts) != len(set(parts)):
        return None
    return tuple(parts)


def _theme_colors(archive: zipfile.ZipFile) -> dict[str, str] | None:
    relationships_root = _xml_root(archive, "word/_rels/document.xml.rels")
    if relationships_root is None:
        return None
    relationships = _relationship_map(relationships_root)
    if relationships is None:
        return None
    theme_relations = [
        relation
        for relation in relationships.values()
        if relation[0].endswith("/theme")
    ]
    if len(theme_relations) != 1 or theme_relations[0][2] is not None:
        return None
    part = _safe_part("word/document.xml", theme_relations[0][1], "word/theme/")
    if part is None or not part.casefold().endswith(".xml"):
        return None
    theme = _xml_root(archive, part)
    if theme is None:
        return None
    scheme = next((node for node in theme.iter() if node.tag == _A + "clrScheme"), None)
    if scheme is None:
        return None
    result: dict[str, str] = {}
    for entry in scheme:
        name = entry.tag.rsplit("}", 1)[-1]
        colors = list(entry)
        if len(colors) != 1:
            continue
        value = colors[0].get("val") or colors[0].get("lastClr")
        if value and re.fullmatch(r"[0-9A-Fa-f]{6}", value):
            result[name] = value.upper()
    return result


def _series_rgb(series: ET.Element, theme: Mapping[str, str]) -> str | None:
    properties = series.find(_C + "spPr")
    if properties is None:
        return None
    line = properties.find(_A + "ln")
    parent = line if line is not None else properties
    fill_tags = {
        _A + "solidFill",
        _A + "noFill",
        _A + "gradFill",
        _A + "pattFill",
        _A + "blipFill",
    }
    fills = [child for child in parent if child.tag in fill_tags]
    if len(fills) != 1 or fills[0].tag != _A + "solidFill" or len(fills[0]) != 1:
        return None
    color = fills[0][0]
    if len(color):
        return None
    if color.tag == _A + "srgbClr":
        value = color.get("val")
    elif color.tag == _A + "schemeClr":
        value = theme.get(color.get("val") or "")
    else:
        return None
    return value.upper() if value and re.fullmatch(r"[0-9A-Fa-f]{6}", value) else None


def _rgb_family(rgb: str) -> str | None:
    red, green, blue = (int(rgb[index : index + 2], 16) / 255.0 for index in (0, 2, 4))
    # ``rgb_to_hls`` returns hue, lightness, saturation in that order.
    # Keeping the names in the same order matters for dark Office theme colors:
    # treating lightness as saturation incorrectly rejected accent1 (blue).
    hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    degrees = hue * 360.0
    if saturation < 0.25 or not 0.12 <= lightness <= 0.88:
        return None
    if degrees >= 345 or degrees < 15:
        return "red"
    if 20 <= degrees <= 48:
        return "orange"
    if 52 <= degrees <= 72:
        return "yellow"
    if 82 <= degrees <= 165:
        return "green"
    # Office's current accent1 (for example RGB 156082) is a dark cyan-blue
    # around 199 degrees, but is presented and described as blue in the chart.
    if 180 <= degrees <= 260:
        return "blue"
    return None


def _cache_values(parent: ET.Element, *, numeric: bool) -> tuple[str, ...] | None:
    cache_tags = (
        (_C + "numCache", _C + "numLit") if numeric else (_C + "strCache", _C + "strLit")
    )
    caches = [node for node in parent.iter() if node.tag in cache_tags]
    if len(caches) != 1:
        return None
    cache = caches[0]
    count_nodes = cache.findall("./" + _C + "ptCount")
    if len(count_nodes) != 1:
        return None
    try:
        count = int(count_nodes[0].get("val") or "")
    except ValueError:
        return None
    points: dict[int, str] = {}
    for point in cache.findall("./" + _C + "pt"):
        try:
            index = int(point.get("idx") or "")
        except ValueError:
            return None
        values = point.findall("./" + _C + "v")
        if len(values) != 1 or values[0].text is None or index in points:
            return None
        points[index] = values[0].text
    if not 1 <= count <= _MAX_CHART_POINTS:
        return None
    if sorted(points) != list(range(count)):
        return None
    return tuple(points[index] for index in range(count))


def _series_values(series: ET.Element) -> tuple[Decimal, ...] | None:
    parents = [node for node in (series.find(_C + "val"), series.find(_C + "yVal")) if node is not None]
    if len(parents) != 1:
        return None
    values = _cache_values(parents[0], numeric=True)
    if values is None:
        return None
    output: list[Decimal] = []
    for value in values:
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            return None
        if not parsed.is_finite():
            return None
        output.append(parsed)
    return tuple(output)


def _series_x_values(series: ET.Element, count: int) -> tuple[Decimal, ...] | None:
    parents = [node for node in (series.find(_C + "cat"), series.find(_C + "xVal")) if node is not None]
    if not parents:
        # A category line chart without a category cache is rendered by Office
        # with the one-based point ordinal on the category axis.
        return tuple(Decimal(index) for index in range(1, count + 1))
    if len(parents) != 1:
        return None
    numeric = _cache_values(parents[0], numeric=True)
    raw = numeric if numeric is not None else _cache_values(parents[0], numeric=False)
    if raw is None or len(raw) != count:
        return None
    output: list[Decimal] = []
    for value in raw:
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            return None
        if not parsed.is_finite():
            return None
        output.append(parsed)
    return tuple(output)


def _chart_title(root: ET.Element) -> str:
    title = root.find("." + "/" + _C + "chart/" + _C + "title")
    if title is None:
        return ""
    return "".join(node.text or "" for node in title.iter() if node.tag in {_A + "t", _C + "v"}).strip()


def _chart_point(
    engine: Any, match: re.Match[str]
) -> StructuredCandidateDecision:
    paths = _named_docx(engine, match["location"], match["container"])
    if len(paths) != 1:
        return _hold("docx_source_not_unique")
    chart_number = int(unicodedata.normalize("NFKC", match["chart"]))
    digits = int(unicodedata.normalize("NFKC", match["digits"]))
    try:
        requested_x = Decimal(unicodedata.normalize("NFKC", match["x"]))
    except InvalidOperation:
        return _hold("chart_x_invalid")
    if not requested_x.is_finite():
        return _hold("chart_x_invalid")
    try:
        with zipfile.ZipFile(paths[0]) as archive:
            if not _validate_archive(archive):
                return _hold("docx_archive_invalid")
            parts = _chart_parts_in_body_order(archive)
            theme = _theme_colors(archive)
            if parts is None or theme is None or not 1 <= chart_number <= len(parts):
                return _hold("chart_not_unique")
            chart = _xml_root(archive, parts[chart_number - 1])
            if chart is None:
                return _hold("chart_xml_invalid")
            if _compact_key(_chart_title(chart)) != _compact_key(f"グラフ{chart_number}"):
                return _hold("chart_title_order_conflict")
            line_charts = [node for node in chart.iter() if node.tag == _C + "lineChart"]
            if not line_charts:
                return _hold("chart_type_unsupported")
            series = [node for line in line_charts for node in line.findall("./" + _C + "ser")]
            if not series:
                return _hold("chart_series_missing")
            requested_color = match["color"]
            if requested_color:
                family = _COLOR_ALIASES.get(_compact_key(requested_color))
                if family is None:
                    return _hold("chart_color_unsupported")
                matching = [
                    item
                    for item in series
                    if (rgb := _series_rgb(item, theme)) is not None
                    and _rgb_family(rgb) == family
                ]
            else:
                matching = series if len(series) == 1 else []
            if len(matching) != 1:
                return _hold("chart_series_not_unique")
            values = _series_values(matching[0])
            if values is None:
                return _hold("chart_cache_incomplete")
            x_values = _series_x_values(matching[0], len(values))
            if x_values is None:
                return _hold("chart_x_cache_incomplete")
            indices = [index for index, value in enumerate(x_values) if value == requested_x]
            if len(indices) != 1:
                return _hold("chart_x_not_unique")
            value = values[indices[0]]
    except (OSError, RuntimeError, zipfile.BadZipFile):
        return _hold("docx_archive_invalid")
    quantizer = Decimal(1).scaleb(-digits)
    try:
        answer = format(value.quantize(quantizer, rounding=ROUND_HALF_UP), f".{digits}f")
    except (InvalidOperation, ValueError):
        return _hold("chart_value_not_roundable")
    root = _safe_root(engine)
    if root is None:
        return _hold("source_root_invalid")
    return _decision(answer, paths, root, 6)


def _comment_anchors(archive: zipfile.ZipFile) -> tuple[str, ...] | None:
    document = _xml_root(archive, "word/document.xml")
    comments = _xml_root(archive, "word/comments.xml")
    relationships_root = _xml_root(archive, "word/_rels/document.xml.rels")
    if document is None or comments is None or relationships_root is None:
        return None
    relationships = _relationship_map(relationships_root)
    if relationships is None:
        return None
    comment_relations = [
        value
        for value in relationships.values()
        if value[0].endswith("/comments")
    ]
    if len(comment_relations) != 1 or comment_relations[0][2] is not None:
        return None
    part = _safe_part("word/document.xml", comment_relations[0][1], "word/")
    if part != "word/comments.xml":
        return None
    bodies: dict[str, str] = {}
    for comment in comments.findall("./" + _W + "comment"):
        comment_id = comment.get(_W + "id")
        if comment_id is None or comment_id in bodies:
            return None
        body = "".join(node.text or "" for node in comment.iter() if node.tag == _W + "t").strip()
        bodies[comment_id] = body
    active: dict[str, list[str]] = {}
    anchors: dict[str, str] = {}
    references: list[str] = []
    for node in document.iter():
        if node.tag == _W + "p":
            # A range may span paragraphs.  Preserve that authored boundary
            # instead of silently concatenating two paragraphs.
            for values in active.values():
                if values and values[-1] != "\n":
                    values.append("\n")
        elif node.tag == _W + "commentRangeStart":
            comment_id = node.get(_W + "id")
            if comment_id is None or active or comment_id in anchors:
                return None
            active[comment_id] = []
        elif node.tag == _W + "t":
            for values in active.values():
                values.append(node.text or "")
        elif node.tag == _W + "tab":
            for values in active.values():
                values.append("\t")
        elif node.tag == _W + "br":
            for values in active.values():
                values.append("\n")
        elif node.tag == _W + "commentRangeEnd":
            comment_id = node.get(_W + "id")
            if comment_id is None or comment_id not in active:
                return None
            rendered = "".join(active.pop(comment_id))
            if not rendered.strip():
                return None
            anchors[comment_id] = rendered
        elif node.tag == _W + "commentReference":
            comment_id = node.get(_W + "id")
            if comment_id is None:
                return None
            references.append(comment_id)
    if active or len(references) != len(set(references)):
        return None
    valid = [
        anchors[comment_id]
        for comment_id in references
        if comment_id in anchors
        and bodies.get(comment_id)
        and anchors[comment_id].strip()
    ]
    if len(valid) != len(references):
        return None
    return tuple(valid)


def _commented_anchor(
    engine: Any, match: re.Match[str]
) -> StructuredCandidateDecision:
    paths = _named_docx(
        engine,
        match["location"],
        match["document"],
        meeting_scope=True,
    )
    values: list[tuple[str, Path]] = []
    for path in paths:
        try:
            with zipfile.ZipFile(path) as archive:
                if not _validate_archive(archive):
                    return _hold("docx_archive_invalid")
                if "word/comments.xml" not in archive.namelist():
                    continue
                anchors = _comment_anchors(archive)
        except (OSError, RuntimeError, zipfile.BadZipFile):
            return _hold("docx_archive_invalid")
        if anchors is None:
            return _hold("comment_relation_invalid")
        values.extend((value, path) for value in anchors)
    if len(values) != 1:
        return _hold("comment_anchor_not_unique")
    root = _safe_root(engine)
    if root is None:
        return _hold("source_root_invalid")
    return _decision(values[0][0], [values[0][1]], root, 5)


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    match = EMF_AVERAGE_DIFFERENCE.fullmatch(question)
    if match:
        return _emf_average_difference(engine, match)
    match = NESTED_TABLE_DIFFERENCE.fullmatch(question)
    if match:
        return _nested_table_difference(engine, match)
    match = NATIVE_CHART_POINT.fullmatch(question)
    if match:
        return _chart_point(engine, match)
    match = COMMENTED_ANCHOR_TEXT.fullmatch(question)
    if match:
        return _commented_anchor(engine, match)
    return None


__all__ = [
    "DOCX_MIXED_RULE_VERSION",
    "decide_question",
    "graph_contract_for_question",
    "validate_graph_contract",
]
