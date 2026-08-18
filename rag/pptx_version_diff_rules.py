"""Fail-closed semantic version diffs for project-execution PPTX content.

This module compares complete visible slide content while deliberately
discarding layout and styling.  It certifies only complete question grammars
and only when every execution-related change can be classified as a concrete
source-derived fact (currently personnel assignment or identifier notation).
Any unmatched semantic change, unsupported visible object, ambiguous version
pair, reversed version direction, or malformed OOXML package produces a hold.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
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


PPTX_VERSION_DIFF_RULE_VERSION = "0.1"

EXPLICIT_VERSION_DIFF = re.compile(
    r"^(?P<location>.+?)の(?P<before>[^,、。]+?\.pptx)から"
    r"(?P<after>[^,、。]+?\.pptx)に修正されたもののうち、"
    r"案件遂行に関連する変更を挙げてください。?$"
)

OLD_LATEST_REPORT_DIFF = re.compile(
    r"^(?P<location>.+?)の(?P<document>[^,、。]+?)old版と最新版を比較したとき、"
    r"案件遂行に関連する実質的な変更を挙げてください。?$",
    flags=re.IGNORECASE,
)

_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PR_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_C_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_P = "{" + _P_NS + "}"
_A = "{" + _A_NS + "}"
_R = "{" + _R_NS + "}"
_PR = "{" + _PR_NS + "}"
_C = "{" + _C_NS + "}"

_MAX_PPTX_BYTES = 256 * 1024 * 1024
_MAX_ZIP_ENTRIES = 20_000
_MAX_MEMBER_BYTES = 128 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 500.0
_MAX_SLIDES = 1_000
_MAX_SHAPES_PER_SLIDE = 10_000
_MAX_TEXT_CHARS = 2_000_000
_XML_FORBIDDEN = (b"<!DOCTYPE", b"<!ENTITY")
_VERSION_TOKEN = re.compile(r"(?:^|[_\-\s])v(?P<number>[0-9]+)(?:$|[_\-\s])", re.I)
_OLD_TOKEN = re.compile(
    r"(?:^|[._\-\s])(?:old|older|legacy|archive|archived)(?:$|[._\-\s])",
    re.I,
)
_OLD_JAPANESE = ("旧版", "旧", "過去", "アーカイブ")
_EXECUTION_TITLE = re.compile(
    r"実施|遂行|体制|スコープ|工程|作業|アプローチ|計画|役割|担当|変更管理",
    re.I,
)
_ROLE_WORD = re.compile(
    r"担当|責任者|マネージャ|アナリスト|エンジニア|スポンサー|レビューア|"
    r"リーダ|ディレクタ|オーナー|窓口|manager|analyst|engineer|sponsor|"
    r"reviewer|lead|owner|director",
    re.I,
)
_JAPANESE_PERSON = re.compile(r"^[一-龯々]{1,12}\s+[一-龯々]{1,12}$")
_LATIN_PERSON = re.compile(r"^[A-Za-z][A-Za-z.'-]+(?:\s+[A-Za-z][A-Za-z.'-]+){1,3}$")
_IDENTIFIER_FORM = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+|"
    r"[A-Za-z][A-Za-z0-9]*(?:\s+[A-Za-z][A-Za-z0-9]*)+"
    r")(?![A-Za-z0-9_])"
)
_STRUCTURAL_LABELS = frozenset(
    {
        "ステップ",
        "フェーズ",
        "主な作業内容",
        "step",
        "phase",
        "work items",
    }
)
_DECORATIVE = frozenset({"✕", "×", "▼", "▽", "▸", "▶", "⇄", "↔", "→", "←"})
_TOKEN = re.compile(
    r"[a-z][a-z0-9_.@%/+\-]*|\d+(?:\.\d+)?(?:%|円|時間|週|ヶ月|月|日|年)?|"
    r"[一-龯々ぁ-んァ-ヶー]",
    re.I,
)
_PLAIN_NUMBER = re.compile(r"^[++＋-]?(?:\d+(?:\.\d+)?|\.\d+)%?$")
_DERIVED_DELTA_ANNOTATION = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9_.@%/+\- ]*|[\u4e00-\u9fa5々ぁ-んァ-ヶー]+)"
    r"\s*[:：]\s*[++＋-]?(?:\d+(?:\.\d+)?|\.\d+)"
    r"(?:%|ポイント)?\s*(?:改善|向上)",
    re.I,
)
_INLINE_NUMERIC_FACT = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?P<label>[A-Za-z][A-Za-z0-9_.@%+\-]*)\s*[:=]\s*"
    r"(?P<value>[++＋-]?(?:\d+(?:\.\d+)?|\.\d+)%?)",
    re.I,
)
_CARD_LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9_.@%+\-]*$", re.I)


class _InvalidSource(ValueError):
    pass


@dataclass(frozen=True)
class _Chart:
    titles: tuple[str, ...]
    categories: tuple[str, ...]
    series: tuple[tuple[str, tuple[Decimal, ...]], ...]


@dataclass(frozen=True)
class _Slide:
    number: int
    title: str
    groups: tuple[tuple[str, ...], ...]
    table_rows: tuple[tuple[str, ...], ...]
    tables: tuple[tuple[tuple[str, ...], ...], ...]
    table_groups: tuple[tuple[str, ...], ...]
    chart_groups: tuple[tuple[str, ...], ...]
    charts: tuple[_Chart, ...]


@dataclass(frozen=True)
class _Deck:
    slides: tuple[_Slide, ...]


@dataclass(frozen=True)
class _RoleRecord:
    slide: int
    order: int
    title: str
    role: str
    person: str
    responsibility: str


@dataclass(frozen=True)
class _IdentifierChange:
    slide: int
    order: int
    before: str
    after: str
    semantic_key: str


@dataclass(frozen=True)
class _PersonnelChange:
    slide: int
    order: int
    role: str
    before: str
    after: str
    responsibility: str


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


def _compact(value: object) -> str:
    return re.sub(r"\s+", "", _normalized(value))


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


def _is_old_path(path: Path) -> bool:
    for part in path.parts:
        rendered = _normalized(part)
        if _OLD_TOKEN.search(rendered) or any(marker in rendered for marker in _OLD_JAPANESE):
            return True
    return False


def _named_presentations(
    engine: Any, location: str, filename: str
) -> tuple[Path, ...]:
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
        for path in root.rglob("*.pptx"):
            if (
                not path.is_file()
                or path.is_symlink()
                or path.name.startswith(("~$", "."))
                or _has_symlink_component(path, root)
                or _normalized(path.name) not in names
            ):
                continue
            relative = path.relative_to(root)
            if not _location_matches(relative.parts[:-1], locations):
                continue
            size = path.stat().st_size
            if 0 < size <= _MAX_PPTX_BYTES:
                matches.append(path.resolve())
    except OSError:
        return ()
    return tuple(sorted(set(matches), key=lambda item: item.as_posix()))


def _report_pair(engine: Any, location: str, document: str) -> tuple[Path, Path] | None:
    root = _source_root(engine)
    if root is None:
        return None
    locations = _candidate_values(location, getattr(engine, "glossary", None))
    document_candidates = {
        re.sub(r"書$", "", _compact(value))
        for value in _candidate_values(document, getattr(engine, "glossary", None))
    }
    candidates: list[Path] = []
    try:
        for path in root.rglob("*.pptx"):
            if (
                not path.is_file()
                or path.is_symlink()
                or path.name.startswith(("~$", "."))
                or _has_symlink_component(path, root)
                or not _location_matches(path.relative_to(root).parts[:-1], locations)
                or not any("報告" in _normalized(part) for part in path.parts[:-1])
            ):
                continue
            stem = re.sub(r"書$", "", _compact(path.stem))
            if not any(candidate and candidate in stem for candidate in document_candidates):
                continue
            size = path.stat().st_size
            if 0 < size <= _MAX_PPTX_BYTES:
                candidates.append(path.resolve())
    except OSError:
        return None
    old = [path for path in candidates if _is_old_path(path)]
    latest = [path for path in candidates if not _is_old_path(path)]
    if len(old) != 1 or len(latest) != 1:
        return None
    old_base = _OLD_TOKEN.sub("", _normalized(old[0].stem))
    old_base = re.sub(r"[._\-\s]+", "", old_base)
    latest_base = re.sub(r"[._\-\s]+", "", _normalized(latest[0].stem))
    if old_base != latest_base:
        return None
    return old[0], latest[0]


def _version_number(path: Path) -> int | None:
    match = _VERSION_TOKEN.search(_normalized(path.stem))
    if match is None:
        return None
    value = int(match["number"])
    return value if 0 <= value <= 1_000_000 else None


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
) -> dict[str, Any]:
    operators = (
        "retrieve",
        "select_before_version",
        "select_after_version",
        "extract_visible_content",
        "align_slides",
        "classify_execution_changes",
        "ignore_layout_style_noise",
        "verify_no_unclassified_change",
        "project",
    )
    nodes = _nodes(operators)
    core = {
        "graph_rule_version": PPTX_VERSION_DIFF_RULE_VERSION,
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
            # The source can contain zero, one, or several execution changes.
            # The no-change sentence and one-change answers are valid
            # singleton lists; multi-change answers use Japanese list
            # separators and must not be mislabeled as a scalar.
            "cardinality": "multiple",
            "answer_shape": {
                "container": "list",
                "value_type": "string",
                "unit": None,
            },
            "display_precision": None,
            "required_keys": None,
        },
    }
    return {
        "graph_contract_id": "pptx_version_diff_"
        + hashlib.sha256(_canonical_json(core).encode("utf-8")).hexdigest()[:32],
        **core,
    }


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if not isinstance(question, str):
        return None
    explicit = EXPLICIT_VERSION_DIFF.fullmatch(question)
    if explicit is not None:
        bindings = {
            key: explicit[key] for key in ("location", "before", "after")
        }
        before_version = _version_number(Path(bindings["before"]))
        after_version = _version_number(Path(bindings["after"]))
        if (
            before_version is None
            or after_version is None
            or before_version >= after_version
        ):
            return None
        return _contract(
            question,
            "pptx_explicit_version_project_execution_diff",
            bindings,
            {
                "location": bindings["location"],
                "before": bindings["before"],
                "after": bindings["after"],
                "direction": "strictly_increasing_explicit_version",
                "content_channel": "visible_native_text_table_chart",
                "noise_policy": "layout_and_style_only",
            },
        )
    report = OLD_LATEST_REPORT_DIFF.fullmatch(question)
    if report is None:
        return None
    bindings = {key: report[key] for key in ("location", "document")}
    return _contract(
        question,
        "pptx_old_latest_report_project_execution_diff",
        bindings,
        {
            "location": bindings["location"],
            "document": bindings["document"],
            "before": "unique_old_marked_report",
            "after": "unique_current_report",
            "direction": "old_to_latest",
            "content_channel": "visible_native_text_table_chart",
            "noise_policy": "layout_and_style_only",
        },
    )


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    if expected is None or not isinstance(contract, Mapping):
        return False
    try:
        return _canonical_json(expected) == _canonical_json(contract)
    except (TypeError, ValueError):
        return False


def _validate_archive(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if not 1 <= len(infos) <= _MAX_ZIP_ENTRIES:
            raise _InvalidSource("pptx_version_diff_archive_invalid")
        names: set[str] = set()
        total = 0
        members: dict[str, bytes] = {}
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
                raise _InvalidSource("pptx_version_diff_archive_invalid")
            names.add(name)
            if not 0 <= info.file_size <= _MAX_MEMBER_BYTES:
                raise _InvalidSource("pptx_version_diff_archive_resource_limit")
            if info.file_size and info.compress_size == 0:
                raise _InvalidSource("pptx_version_diff_archive_invalid")
            if (
                info.compress_size
                and info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO
            ):
                raise _InvalidSource("pptx_version_diff_archive_resource_limit")
            total += info.file_size
            if total > _MAX_TOTAL_BYTES:
                raise _InvalidSource("pptx_version_diff_archive_resource_limit")
            data = archive.read(info)
            if name.casefold().endswith((".xml", ".rels")):
                upper = data.upper()
                if any(marker in upper for marker in _XML_FORBIDDEN):
                    raise _InvalidSource("pptx_version_diff_xml_unsafe")
            members[name] = data
    required = {
        "[Content_Types].xml",
        "ppt/presentation.xml",
        "ppt/_rels/presentation.xml.rels",
    }
    if not required.issubset(members):
        raise _InvalidSource("pptx_version_diff_archive_invalid")
    return members


def _xml(members: Mapping[str, bytes], name: str) -> ET.Element:
    data = members.get(name)
    if data is None:
        raise _InvalidSource("pptx_version_diff_part_missing")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise _InvalidSource("pptx_version_diff_xml_malformed") from exc


def _relationship_part(source: str) -> str:
    directory, name = posixpath.split(source)
    return posixpath.join(directory, "_rels", name + ".rels")


def _safe_target(source: str, target: str) -> str:
    if not target or "\\" in target or target.startswith("/"):
        raise _InvalidSource("pptx_version_diff_relationship_invalid")
    value = posixpath.normpath(posixpath.join(posixpath.dirname(source), target))
    if value == ".." or value.startswith("../") or PurePosixPath(value).is_absolute():
        raise _InvalidSource("pptx_version_diff_relationship_invalid")
    return value


def _relationships(
    members: Mapping[str, bytes], source: str
) -> dict[str, tuple[str, str]]:
    root = _xml(members, _relationship_part(source))
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
            raise _InvalidSource("pptx_version_diff_relationship_invalid")
        result[relation_id] = (relation_type, _safe_target(source, target))
    return result


def _paragraphs(text_body: ET.Element | None) -> tuple[str, ...]:
    if text_body is None:
        return ()
    if text_body.find(".//" + _A + "fld") is not None:
        raise _InvalidSource("pptx_version_diff_dynamic_field_unsupported")
    result: list[str] = []
    for paragraph in text_body.findall("./" + _A + "p"):
        pieces: list[str] = []
        for node in paragraph.iter():
            if node.tag == _A + "t":
                pieces.append(node.text or "")
            elif node.tag in {_A + "br", _A + "tab"}:
                pieces.append(" ")
        rendered = " ".join(unicodedata.normalize("NFKC", "".join(pieces)).split())
        if rendered:
            result.append(rendered)
    return tuple(result)


def _shape_visible(shape: ET.Element) -> bool:
    properties = shape.find(".//" + _P + "cNvPr")
    if properties is None:
        raise _InvalidSource("pptx_version_diff_shape_properties_missing")
    hidden = _normalized(properties.get("hidden", "0"))
    if hidden not in {"", "0", "false", "1", "true"}:
        raise _InvalidSource("pptx_version_diff_shape_visibility_invalid")
    return hidden not in {"1", "true"}


def _placeholder_type(shape: ET.Element) -> str:
    placeholders = shape.findall(".//" + _P + "nvPr/" + _P + "ph")
    if len(placeholders) > 1:
        raise _InvalidSource("pptx_version_diff_placeholder_ambiguous")
    return str(placeholders[0].get("type") or "obj") if placeholders else ""


def _point_values(container: ET.Element | None) -> tuple[str, ...]:
    if container is None:
        return ()
    points: list[tuple[int, str]] = []
    for point in container.findall(".//" + _C + "pt"):
        index = point.get("idx")
        value = point.findtext("./" + _C + "v")
        if index is None or value is None:
            raise _InvalidSource("pptx_version_diff_chart_cache_invalid")
        points.append((int(index), value))
    if not points:
        direct = container.findtext("./" + _C + "v")
        return (direct,) if direct is not None else ()
    points.sort()
    if [index for index, _ in points] != list(range(len(points))):
        raise _InvalidSource("pptx_version_diff_chart_cache_invalid")
    return tuple(value for _, value in points)


def _chart(members: Mapping[str, bytes], part: str) -> _Chart:
    root = _xml(members, part)
    title_parts = tuple(
        " ".join((node.text or "").split())
        for node in root.findall(".//" + _C + "title//" + _A + "t")
        if (node.text or "").strip()
    )
    series_nodes = root.findall(".//" + _C + "ser")
    if not series_nodes:
        raise _InvalidSource("pptx_version_diff_chart_series_missing")
    categories: tuple[str, ...] | None = None
    series: list[tuple[str, tuple[Decimal, ...]]] = []
    for node in series_nodes:
        name_values = _point_values(node.find("./" + _C + "tx"))
        name = name_values[0] if name_values else ""
        category_values = _point_values(node.find("./" + _C + "cat"))
        numeric_values = _point_values(node.find("./" + _C + "val"))
        if not category_values or len(category_values) != len(numeric_values):
            raise _InvalidSource("pptx_version_diff_chart_series_invalid")
        if categories is None:
            categories = category_values
        elif categories != category_values:
            raise _InvalidSource("pptx_version_diff_chart_categories_ambiguous")
        numbers: list[Decimal] = []
        for value in numeric_values:
            try:
                number = Decimal(value)
            except InvalidOperation as exc:
                raise _InvalidSource("pptx_version_diff_chart_value_invalid") from exc
            if not number.is_finite():
                raise _InvalidSource("pptx_version_diff_chart_value_invalid")
            numbers.append(number)
        series.append((name, tuple(numbers)))
    assert categories is not None
    return _Chart(title_parts, categories, tuple(series))


def _slide(
    members: Mapping[str, bytes], part: str, number: int
) -> _Slide | None:
    root = _xml(members, part)
    show = _normalized(root.get("show", "1"))
    if show not in {"", "0", "false", "1", "true"}:
        raise _InvalidSource("pptx_version_diff_slide_visibility_invalid")
    if show in {"0", "false"}:
        return None
    relationships = _relationships(members, part)
    shape_tree = root.find("./" + _P + "cSld/" + _P + "spTree")
    if shape_tree is None:
        raise _InvalidSource("pptx_version_diff_shape_tree_missing")
    groups: list[tuple[str, ...]] = []
    rows: list[tuple[str, ...]] = []
    tables: list[tuple[tuple[str, ...], ...]] = []
    table_groups: list[tuple[str, ...]] = []
    chart_groups: list[tuple[str, ...]] = []
    charts: list[_Chart] = []
    title_candidates: list[str] = []
    shape_count = 0
    allowed = {_P + "nvGrpSpPr", _P + "grpSpPr", _P + "extLst"}
    for child in shape_tree:
        if child.tag in allowed:
            continue
        shape_count += 1
        if shape_count > _MAX_SHAPES_PER_SLIDE:
            raise _InvalidSource("pptx_version_diff_shape_resource_limit")
        if child.tag in {_P + "sp", _P + "cxnSp"}:
            if not _shape_visible(child):
                continue
            placeholder = _placeholder_type(child)
            if placeholder in {"dt", "ftr", "sldNum"}:
                continue
            paragraphs = _paragraphs(child.find("./" + _P + "txBody"))
            if paragraphs:
                groups.append(paragraphs)
                if placeholder in {"title", "ctrTitle"}:
                    title_candidates.append(" / ".join(paragraphs))
            continue
        if child.tag == _P + "pic":
            if not _shape_visible(child):
                continue
            properties = child.find(".//" + _P + "cNvPr")
            assert properties is not None
            if (properties.get("descr") or "").strip() or (properties.get("title") or "").strip():
                raise _InvalidSource("pptx_version_diff_visible_picture_unsupported")
            raise _InvalidSource("pptx_version_diff_visible_picture_unsupported")
        if child.tag != _P + "graphicFrame":
            raise _InvalidSource("pptx_version_diff_visible_object_unsupported")
        if not _shape_visible(child):
            continue
        placeholder = _placeholder_type(child)
        if placeholder in {"dt", "ftr", "sldNum"}:
            continue
        graphic_data = child.find("./" + _A + "graphic/" + _A + "graphicData")
        if graphic_data is None or len(list(graphic_data)) != 1:
            raise _InvalidSource("pptx_version_diff_graphic_frame_invalid")
        payload = list(graphic_data)[0]
        if payload.tag == _A + "tbl":
            current_rows: list[tuple[str, ...]] = []
            for table_row in payload.findall("./" + _A + "tr"):
                cells: list[str] = []
                for cell in table_row.findall("./" + _A + "tc"):
                    paragraphs = _paragraphs(cell.find("./" + _A + "txBody"))
                    rendered = " / ".join(paragraphs)
                    cells.append(rendered)
                    if paragraphs:
                        groups.append(paragraphs)
                        table_groups.append(paragraphs)
                if cells:
                    rendered_row = tuple(cells)
                    rows.append(rendered_row)
                    current_rows.append(rendered_row)
            if current_rows:
                tables.append(tuple(current_rows))
            continue
        if payload.tag != _C + "chart":
            raise _InvalidSource("pptx_version_diff_graphic_frame_unsupported")
        relation_id = payload.get(_R + "id")
        relation = relationships.get(str(relation_id))
        if (
            relation is None
            or not relation[0].endswith("/chart")
            or not relation[1].startswith("ppt/charts/")
            or not relation[1].endswith(".xml")
        ):
            raise _InvalidSource("pptx_version_diff_chart_relationship_invalid")
        parsed_chart = _chart(members, relation[1])
        charts.append(parsed_chart)
        for fragment in parsed_chart.titles:
            groups.append((fragment,))
        for fragment in (
            *(name for name, _ in parsed_chart.series if name),
            *parsed_chart.categories,
        ):
            group = (fragment,)
            groups.append(group)
            chart_groups.append(group)
    if len(title_candidates) > 1:
        raise _InvalidSource("pptx_version_diff_title_ambiguous")
    if title_candidates:
        title = title_candidates[0]
    else:
        flattened = [value for group in groups for value in group]
        candidates = [
            value
            for value in flattened
            if re.search(r"(?:^|\s)[0-9]{1,3}[.．]\s*", value)
            or _EXECUTION_TITLE.search(value)
        ]
        title = candidates[0] if candidates else (flattened[0] if flattened else "")
    if not title:
        raise _InvalidSource("pptx_version_diff_title_missing")
    if sum(len(value) for group in groups for value in group) > _MAX_TEXT_CHARS:
        raise _InvalidSource("pptx_version_diff_text_resource_limit")
    return _Slide(
        number,
        title,
        tuple(groups),
        tuple(rows),
        tuple(tables),
        tuple(table_groups),
        tuple(chart_groups),
        tuple(charts),
    )


def _deck(path: Path) -> _Deck:
    members = _validate_archive(path)
    presentation = _xml(members, "ppt/presentation.xml")
    relations = _relationships(members, "ppt/presentation.xml")
    slide_ids = presentation.findall(".//" + _P + "sldId")
    if not 1 <= len(slide_ids) <= _MAX_SLIDES:
        raise _InvalidSource("pptx_version_diff_slide_count_invalid")
    slides: list[_Slide] = []
    parts: set[str] = set()
    for ordinal, node in enumerate(slide_ids, 1):
        relation = relations.get(str(node.get(_R + "id")))
        if (
            relation is None
            or not relation[0].endswith("/slide")
            or not relation[1].startswith("ppt/slides/")
            or not relation[1].endswith(".xml")
            or relation[1] in parts
        ):
            raise _InvalidSource("pptx_version_diff_slide_relationship_invalid")
        parts.add(relation[1])
        parsed = _slide(members, relation[1], ordinal)
        # Hidden slides are intentionally fail-closed rather than silently
        # disappearing from version coverage.  A deck with authored but
        # invisible execution content needs an explicit policy before it can
        # support a certified "no substantive change" result.
        if parsed is None:
            raise _InvalidSource("pptx_version_diff_hidden_slide_unsupported")
        slides.append(parsed)
    if not slides:
        raise _InvalidSource("pptx_version_diff_no_visible_slides")
    return _Deck(tuple(slides))


def _title_key(value: str) -> str:
    rendered = re.sub(r"^\s*[0-9]{1,3}(?:[.．]|\s)+\s*", "", _normalized(value))
    return re.sub(r"\s+", "", rendered)


def _aligned_slides(before: _Deck, after: _Deck) -> tuple[tuple[_Slide, _Slide], ...]:
    if len(before.slides) != len(after.slides):
        raise _InvalidSource("pptx_version_diff_slide_topology_changed")
    result: list[tuple[_Slide, _Slide]] = []
    for left, right in zip(before.slides, after.slides):
        if left.number != right.number:
            raise _InvalidSource("pptx_version_diff_slide_alignment_ambiguous")
        result.append((left, right))
    return tuple(result)


def _execution_slides(before: _Deck, after: _Deck) -> tuple[tuple[_Slide, _Slide], ...]:
    result: list[tuple[_Slide, _Slide]] = []
    for left, right in _aligned_slides(before, after):
        in_scope = bool(
            _EXECUTION_TITLE.search(left.title) or _EXECUTION_TITLE.search(right.title)
        )
        if in_scope and _title_key(left.title) != _title_key(right.title):
            raise _InvalidSource("pptx_version_diff_slide_alignment_ambiguous")
        if in_scope:
            result.append((left, right))
    if not result:
        raise _InvalidSource("pptx_version_diff_execution_scope_missing")
    return tuple(result)


def _looks_person(value: str) -> bool:
    rendered = " ".join(unicodedata.normalize("NFKC", value).split())
    return bool(_JAPANESE_PERSON.fullmatch(rendered) or _LATIN_PERSON.fullmatch(rendered))


def _unstructured_groups(slide: _Slide) -> tuple[tuple[str, ...], ...]:
    """Return authored text groups excluding table cells/chart data labels."""

    structured = Counter((*slide.table_groups, *slide.chart_groups))
    result: list[tuple[str, ...]] = []
    for group in slide.groups:
        if structured[group] > 0:
            structured[group] -= 1
        else:
            result.append(group)
    if any(structured.values()):
        raise _InvalidSource("pptx_version_diff_structured_text_alignment_invalid")
    return tuple(result)


def _role_records(slide: _Slide) -> tuple[_RoleRecord, ...]:
    sequences: list[tuple[str, ...]] = list(_unstructured_groups(slide))
    sequences.extend(row for row in slide.table_rows if any(cell.strip() for cell in row))
    records: list[_RoleRecord] = []
    for order, sequence in enumerate(sequences):
        values = tuple(value.strip() for value in sequence if value.strip())
        if len(values) < 3 or not _ROLE_WORD.search(values[0]) or not _looks_person(values[1]):
            continue
        records.append(
            _RoleRecord(
                slide=slide.number,
                order=order,
                title=slide.title,
                role=values[0],
                person=values[1],
                responsibility=" / ".join(values[2:]),
            )
        )
    return tuple(records)


def _personnel_changes(
    pairs: Sequence[tuple[_Slide, _Slide]]
) -> tuple[_PersonnelChange, ...]:
    result: list[_PersonnelChange] = []
    for before, after in pairs:
        left_records = _role_records(before)
        right_records = _role_records(after)
        left_map: dict[tuple[str, str], _RoleRecord] = {}
        right_map: dict[tuple[str, str], _RoleRecord] = {}
        for record, destination in (
            *((record, left_map) for record in left_records),
            *((record, right_map) for record in right_records),
        ):
            key = (_normalized(record.role), _normalized(record.responsibility))
            if key in destination:
                raise _InvalidSource("pptx_version_diff_role_record_ambiguous")
            destination[key] = record
        if set(left_map) != set(right_map):
            raise _InvalidSource("pptx_version_diff_role_add_remove_unsupported")
        for key, left in left_map.items():
            right = right_map[key]
            if _normalized(left.person) != _normalized(right.person):
                result.append(
                    _PersonnelChange(
                        slide=before.number,
                        order=left.order,
                        role=left.role,
                        before=left.person,
                        after=right.person,
                        responsibility=left.responsibility,
                    )
                )
    return tuple(result)


def _identifier_occurrences(slide: _Slide) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    order = 0
    for group in slide.groups:
        for value in group:
            for match in _IDENTIFIER_FORM.finditer(value):
                rendered = " ".join(match.group(0).split())
                result.append((order, rendered))
                order += 1
    return result


def _identifier_key(value: str) -> str:
    return re.sub(r"[\s_]", "", _normalized(value))


def _identifier_changes(
    pairs: Sequence[tuple[_Slide, _Slide]]
) -> tuple[_IdentifierChange, ...]:
    result: list[_IdentifierChange] = []
    for before, after in pairs:
        left_occurrences = _identifier_occurrences(before)
        right_occurrences = _identifier_occurrences(after)
        left_counter = Counter(value for _, value in left_occurrences)
        right_counter = Counter(value for _, value in right_occurrences)
        keys = {_identifier_key(value) for value in left_counter} | {
            _identifier_key(value) for value in right_counter
        }
        for key in keys:
            left_forms = {
                value: count for value, count in left_counter.items() if _identifier_key(value) == key
            }
            right_forms = {
                value: count for value, count in right_counter.items() if _identifier_key(value) == key
            }
            removed = [
                value
                for value, count in left_forms.items()
                if count > right_forms.get(value, 0)
            ]
            added = [
                value
                for value, count in right_forms.items()
                if count > left_forms.get(value, 0)
            ]
            if not removed and not added:
                continue
            if (
                len(removed) != 1
                or len(added) != 1
                or ("_" in removed[0]) == ("_" in added[0])
                or not ({"_", " "} <= set("_ " + removed[0] + added[0]))
            ):
                continue
            order = next(
                position for position, value in left_occurrences if value == removed[0]
            )
            result.append(
                _IdentifierChange(
                    slide=before.number,
                    order=order,
                    before=removed[0],
                    after=added[0],
                    semantic_key=key,
                )
            )
    return tuple(sorted(result, key=lambda item: (item.slide, item.order)))


def _replace_literal(value: str, old: str, replacement: str) -> str:
    return re.sub(
        rf"(?<![A-Za-z0-9_]){re.escape(old)}(?![A-Za-z0-9_])",
        replacement,
        value,
        flags=re.IGNORECASE,
    )


def _replace_certified_changes(
    value: str,
    slide_number: int,
    personnel: Sequence[_PersonnelChange],
    identifiers: Sequence[_IdentifierChange],
    *,
    before: bool,
) -> str:
    rendered = value
    for change in personnel:
        if change.slide == slide_number:
            rendered = rendered.replace(
                change.before if before else change.after,
                f" personchange{change.slide}x{change.order} ",
            )
    for change in identifiers:
        if change.slide == slide_number:
            rendered = _replace_literal(
                rendered,
                change.before if before else change.after,
                f" identifierchange{change.slide}x{change.order} ",
            )
    return rendered


def _canonical_number(value: str | Decimal) -> str | None:
    rendered = unicodedata.normalize("NFKC", str(value)).strip().replace(",", "")
    if _PLAIN_NUMBER.fullmatch(rendered) is None:
        return None
    percent = rendered.endswith("%")
    if percent:
        rendered = rendered[:-1]
    try:
        number = Decimal(rendered)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    if number == 0:
        canonical = "0"
    else:
        # Chart caches often expose binary floating-point tails whereas a
        # table displays the authored decimal (for example
        # 0.82499999999999996 versus 0.825).  Twelve significant digits are
        # ample for a slide's visible values and remove only that cache noise.
        rounded = Decimal(format(number, ".12g"))
        canonical = format(rounded.normalize(), "f")
    return canonical + ("%" if percent else "")


def _canonical_label(value: str) -> str:
    return re.sub(r"\s+", "", _normalized(value))


def _series_key(value: str) -> str:
    rendered = _canonical_label(value)
    if re.search(r"改善|差分|変化|delta|change", rendered, re.I):
        return "delta"
    if re.search(r"中間|旧|従来|before|old|base(?:line)?", rendered, re.I):
        return "before"
    if re.search(r"最終|新|修正後|after|latest|current", rendered, re.I):
        return "after"
    return rendered


def _canonical_fact_value(
    value: str,
    slide_number: int,
    personnel: Sequence[_PersonnelChange],
    identifiers: Sequence[_IdentifierChange],
    *,
    before: bool,
) -> str:
    rendered = _replace_certified_changes(
        value,
        slide_number,
        personnel,
        identifiers,
        before=before,
    )
    number = _canonical_number(rendered)
    return "number:" + number if number is not None else "text:" + _canonical_label(rendered)


def _structured_facts(
    slide: _Slide,
    personnel: Sequence[_PersonnelChange],
    identifiers: Sequence[_IdentifierChange],
    *,
    before: bool,
) -> Counter[str]:
    """Map tables and charts into the same row/series/value fact space."""

    result: Counter[str] = Counter()
    for table_index, table in enumerate(slide.tables):
        widths = {len(row) for row in table}
        if len(table) >= 2 and len(widths) == 1 and next(iter(widths), 0) >= 2:
            header = table[0]
            column_keys = tuple(_series_key(value) for value in header[1:])
            if any(not key for key in column_keys) or len(set(column_keys)) != len(column_keys):
                raise _InvalidSource("pptx_version_diff_table_header_ambiguous")
            for row in table[1:]:
                row_key = _canonical_label(row[0])
                if not row_key:
                    raise _InvalidSource("pptx_version_diff_table_row_ambiguous")
                for column_key, value in zip(column_keys, row[1:]):
                    canonical = _canonical_fact_value(
                        value,
                        slide.number,
                        personnel,
                        identifiers,
                        before=before,
                    )
                    result[f"matrix:{row_key}|{column_key}|{canonical}"] += 1
            continue
        # Non-matrix tables remain fully covered by conservative positional
        # facts.  They cannot be mistaken for a chart or ignored as layout.
        for row_index, row in enumerate(table):
            for column_index, value in enumerate(row):
                canonical = _canonical_fact_value(
                    value,
                    slide.number,
                    personnel,
                    identifiers,
                    before=before,
                )
                result[
                    f"table:{table_index}:{row_index}:{column_index}:{canonical}"
                ] += 1

    for chart_index, chart in enumerate(slide.charts):
        keyed_series: dict[str, tuple[Decimal, ...]] = {}
        for name, values in chart.series:
            key = _series_key(name)
            if not key or key in keyed_series:
                raise _InvalidSource("pptx_version_diff_chart_series_ambiguous")
            keyed_series[key] = values
            for category, value in zip(chart.categories, values):
                row_key = _canonical_label(category)
                number = _canonical_number(value)
                if not row_key or number is None:
                    raise _InvalidSource("pptx_version_diff_chart_fact_invalid")
                result[f"matrix:{row_key}|{key}|number:{number}"] += 1
        if "before" in keyed_series and "after" in keyed_series:
            earlier = keyed_series["before"]
            later = keyed_series["after"]
            if len(earlier) != len(later):
                raise _InvalidSource("pptx_version_diff_chart_series_ambiguous")
            for category, left, right in zip(chart.categories, earlier, later):
                row_key = _canonical_label(category)
                number = _canonical_number(right - left)
                if not row_key or number is None:
                    raise _InvalidSource("pptx_version_diff_chart_fact_invalid")
                result[f"matrix:{row_key}|delta|number:{number}"] += 1
        if not chart.series:
            raise _InvalidSource(
                f"pptx_version_diff_chart_{chart_index}_series_missing"
            )
    return result


def _canonical_clause(value: str) -> str:
    rendered = unicodedata.normalize("NFKC", value).casefold().strip()
    rendered = re.sub(r"(?:^|\s)[•●](?=\s|$)", " ", rendered)
    rendered = re.sub(r"^[•●・]\s*", "", rendered)
    # Remove only separators that are visibly used between layout blocks.
    # Embedded punctuation such as ``AUC-ROC`` and ``上位/下位`` remains semantic.
    rendered = re.sub(r"(?:^|\s)[|\uff5c/—–]+(?=\s|$)", " ", rendered)
    rendered = re.sub(r"\s*_\s*", "_", rendered)
    compact = re.sub(r"\s+", "", rendered)
    return "" if re.fullmatch(r"[|\uff5c/—–]+", compact) else compact


def _paragraph_signature(
    slide: _Slide,
    personnel: Sequence[_PersonnelChange],
    identifiers: Sequence[_IdentifierChange],
    facts: Counter[str],
    *,
    before: bool,
    structured_mode: bool,
) -> Counter[str]:
    groups = _unstructured_groups(slide) if structured_mode else slide.groups
    has_delta_facts = any("|delta|" in fact for fact in facts)
    fragments: list[str] = []

    def numeric_fact(match: re.Match[str]) -> str:
        label = _canonical_label(match.group("label"))
        number = _canonical_number(match.group("value"))
        if not label or number is None:
            raise _InvalidSource("pptx_version_diff_inline_fact_invalid")
        facts[f"kv:{label}|number:{number}"] += 1
        return " "

    for group in groups:
        for raw in group:
            rendered = unicodedata.normalize("NFKC", raw).strip()
            normalized = _normalized(rendered)
            if (
                not rendered
                or normalized in {_normalized(value) for value in _STRUCTURAL_LABELS}
                or rendered in _DECORATIVE
                or re.fullmatch(r"[0-9]{1,2}", rendered)
            ):
                continue
            # A list/step marker can move into a standalone shape.  Decimal
            # content is not matched because whitespace after the dot is
            # mandatory.
            rendered = re.sub(r"^[0-9]{1,3}[.．]\s+", "", rendered)
            rendered = _replace_certified_changes(
                rendered,
                slide.number,
                personnel,
                identifiers,
                before=before,
            )
            if has_delta_facts:
                rendered = _DERIVED_DELTA_ANNOTATION.sub(" ", rendered)
            rendered = _INLINE_NUMERIC_FACT.sub(numeric_fact, rendered)
            canonical = _canonical_clause(rendered)
            if canonical:
                fragments.append(canonical)

    # Metric cards commonly store value and label in adjacent independent
    # shapes, while an older version writes ``label: value`` inline.  Convert
    # only an unambiguous adjacent Latin-label/numeric pair into the same fact.
    consumed: set[int] = set()
    for index in range(len(fragments) - 1):
        if index in consumed or index + 1 in consumed:
            continue
        first, second = fragments[index], fragments[index + 1]
        first_number = _canonical_number(first)
        second_number = _canonical_number(second)
        if first_number is not None and _CARD_LABEL.fullmatch(second):
            facts[f"kv:{_canonical_label(second)}|number:{first_number}"] += 1
        elif second_number is not None and _CARD_LABEL.fullmatch(first):
            facts[f"kv:{_canonical_label(first)}|number:{second_number}"] += 1
        else:
            continue
        consumed.update({index, index + 1})

    return Counter(
        fragment for index, fragment in enumerate(fragments) if index not in consumed
    )


def _unique_segmentation(text: str, pieces: Counter[str]) -> Counter[str] | None:
    """Return one unambiguous split of ``text`` into available layout pieces."""

    candidates = tuple(
        sorted(
            (
                value
                for value, count in pieces.items()
                if count > 0 and 0 < len(value) < len(text)
            ),
            key=lambda value: (-len(value), value),
        )
    )
    if not candidates:
        return None
    by_first: dict[str, list[str]] = {}
    for candidate in candidates:
        by_first.setdefault(candidate[0], []).append(candidate)
    used: Counter[str] = Counter()
    solutions: set[tuple[tuple[str, int], ...]] = set()
    state_count = 0
    exhausted = False

    def visit(position: int) -> None:
        nonlocal state_count, exhausted
        if exhausted or len(solutions) > 1:
            return
        state_count += 1
        if state_count > 50_000:
            exhausted = True
            return
        if position == len(text):
            if sum(used.values()) >= 2:
                solutions.add(tuple(sorted(used.items())))
            return
        for candidate in by_first.get(text[position], ()):
            if used[candidate] >= pieces[candidate] or not text.startswith(
                candidate, position
            ):
                continue
            used[candidate] += 1
            visit(position + len(candidate))
            used[candidate] -= 1
            if used[candidate] == 0:
                del used[candidate]

    visit(0)
    if exhausted or len(solutions) != 1:
        return None
    return Counter(dict(next(iter(solutions))))


def _paragraph_multisets_equal(
    left_values: Counter[str], right_values: Counter[str]
) -> bool:
    left = left_values.copy()
    right = right_values.copy()
    common = left & right
    left.subtract(common)
    right.subtract(common)
    left = +left
    right = +right

    while left and right:
        progress = False
        for text in sorted(left, key=lambda value: (-len(value), value)):
            segmentation = _unique_segmentation(text, right)
            if segmentation is None:
                continue
            left[text] -= 1
            if left[text] == 0:
                del left[text]
            right.subtract(segmentation)
            right = +right
            progress = True
            break
        if progress:
            continue
        for text in sorted(right, key=lambda value: (-len(value), value)):
            segmentation = _unique_segmentation(text, left)
            if segmentation is None:
                continue
            right[text] -= 1
            if right[text] == 0:
                del right[text]
            left.subtract(segmentation)
            left = +left
            progress = True
            break
        if not progress:
            break
    return not left and not right


def _semantic_counter(
    slide: _Slide,
    personnel: Sequence[_PersonnelChange],
    identifiers: Sequence[_IdentifierChange],
    *,
    before: bool,
    structured_mode: bool,
) -> tuple[Counter[str], Counter[str]]:
    facts = (
        _structured_facts(
            slide,
            personnel,
            identifiers,
            before=before,
        )
        if structured_mode
        else Counter()
    )
    if not structured_mode:
        # Numeric chart caches have no equivalent text group.  Retain them in
        # bag mode so a chart-to-card conversion cannot silently lose or alter
        # values; a genuine table-to-chart pair uses structured_mode instead.
        for chart in slide.charts:
            for _, values in chart.series:
                for value in values:
                    number = _canonical_number(value)
                    if number is None:
                        raise _InvalidSource("pptx_version_diff_chart_fact_invalid")
                    facts[f"chart-number:{number}"] += 1
    paragraphs = _paragraph_signature(
        slide,
        personnel,
        identifiers,
        facts,
        before=before,
        structured_mode=structured_mode,
    )
    return facts, paragraphs


def _classify_changes(before: _Deck, after: _Deck) -> tuple[str, ...]:
    all_pairs = _aligned_slides(before, after)
    execution_pairs = _execution_slides(before, after)
    personnel = _personnel_changes(execution_pairs)
    identifiers = _identifier_changes(execution_pairs)
    # Every visible slide is covered, including slides outside the execution
    # title scope.  Such slides may differ only when their authored facts are
    # provably identical after representation normalization (for example a
    # table rendered as a chart).  Any other change holds rather than being
    # silently discarded as irrelevant.
    for left, right in all_pairs:
        structured_mode = bool(
            left.charts
            or right.charts
            or (left.tables and right.tables)
        )
        left_facts, left_paragraphs = _semantic_counter(
            left,
            personnel,
            identifiers,
            before=True,
            structured_mode=structured_mode,
        )
        right_facts, right_paragraphs = _semantic_counter(
            right,
            personnel,
            identifiers,
            before=False,
            structured_mode=structured_mode,
        )
        if left_facts != right_facts or not _paragraph_multisets_equal(
            left_paragraphs, right_paragraphs
        ):
            raise _InvalidSource("pptx_version_diff_unclassified_execution_change")
    rendered: list[tuple[int, int, str]] = []
    for change in personnel:
        rendered.append(
            (
                change.slide,
                change.order,
                f"役割「{change.role}」の担当者: {change.before}→{change.after}",
            )
        )
    for change in identifiers:
        rendered.append(
            (
                change.slide,
                change.order,
                f"カラム表記: {change.before}→{change.after}",
            )
        )
    rendered.sort(key=lambda item: (item[0], item[1], item[2]))
    return tuple(value for _, _, value in rendered)


def _answer(changes: Sequence[str]) -> str:
    if not changes:
        return "案件遂行に関連する実質的な変更はありません"
    return "、".join(changes)


def _decision(
    answer: str,
    paths: Sequence[Path],
    root: Path,
    operations: int,
) -> StructuredCandidateDecision:
    # Keep provenance in semantic direction (before, then after).  Sorting
    # would invert pairs such as ``report_old`` and ``report`` even though the
    # comparison itself is old-to-current.
    unique = tuple(dict.fromkeys(paths))
    digest = hashlib.sha256()
    for path in unique:
        digest.update(path.read_bytes())
    return StructuredCandidateDecision(
        "resolved",
        "certified_pptx_project_execution_version_diff",
        StructuredCandidateAnswer(
            answer=answer,
            source_paths=tuple(
                unicodedata.normalize("NFC", path.relative_to(root).as_posix())
                for path in unique
            ),
            source_sha256=digest.hexdigest(),
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
        return _hold("pptx_version_diff_source_root_invalid")
    bindings = contract["bindings"]
    if contract["rule_id"] == "pptx_explicit_version_project_execution_diff":
        before_paths = _named_presentations(
            engine, bindings["location"], bindings["before"]
        )
        after_paths = _named_presentations(
            engine, bindings["location"], bindings["after"]
        )
        if len(before_paths) != 1 or len(after_paths) != 1:
            return _hold("pptx_version_diff_pair_not_unique")
        before_path, after_path = before_paths[0], after_paths[0]
        before_version = _version_number(before_path)
        after_version = _version_number(after_path)
        if (
            before_path == after_path
            or before_version is None
            or after_version is None
            or before_version >= after_version
        ):
            return _hold("pptx_version_diff_direction_invalid")
    else:
        pair = _report_pair(engine, bindings["location"], bindings["document"])
        if pair is None:
            return _hold("pptx_version_diff_pair_not_unique")
        before_path, after_path = pair
    try:
        before_deck = _deck(before_path)
        after_deck = _deck(after_path)
        changes = _classify_changes(before_deck, after_deck)
        answer = _answer(changes)
    except (
        ArithmeticError,
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        zipfile.BadZipFile,
    ):
        return _hold("pptx_version_diff_source_not_certified")
    return _decision(
        answer,
        (before_path, after_path),
        root,
        len(contract["operation_graph"]["nodes"]),
    )


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
        return _hold("pptx_version_diff_graph_plan_not_certified")
    branches = getattr(graph_plan, "branch_intents", ())
    if not isinstance(branches, tuple) or len(branches) != 1:
        return _hold("pptx_version_diff_graph_plan_not_certified")
    branch = branches[0]
    if not isinstance(branch, Mapping) or branch.get("status") != "resolved":
        return _hold("pptx_version_diff_graph_plan_not_certified")
    intent = branch.get("intent")
    supplied = (
        intent.get("extended_graph_contract") if isinstance(intent, Mapping) else None
    )
    if (
        not isinstance(supplied, Mapping)
        or not validate_graph_contract(question, supplied)
        or _canonical_json(supplied) != _canonical_json(contract)
    ):
        return _hold("pptx_version_diff_graph_plan_contract_mismatch")
    return decide_question(engine, question)


__all__ = [
    "EXPLICIT_VERSION_DIFF",
    "OLD_LATEST_REPORT_DIFF",
    "PPTX_VERSION_DIFF_RULE_VERSION",
    "decide_from_graph",
    "decide_question",
    "graph_contract_for_question",
    "validate_graph_contract",
]
