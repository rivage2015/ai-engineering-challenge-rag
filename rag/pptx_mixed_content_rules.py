"""Fail-closed rules for mixed native and vector content inside PPTX.

The module deliberately supports only complete question grammars.  It never
selects a slide by a retrieval score:

* an amount-summary page must have one strong heading and several independent
  authored amount roles on exactly one visible slide;
* an embedded EMF table is read from ``EMR_EXTTEXTOUTW`` and solid-brush
  ``PATCOPY`` records, so highlighted values and their row/column coordinates
  are recovered without OCR.

Source aliases come only from the question-independent glossary.  Any
non-unique alias, file, slide, relationship, marker, table axis, or value
returns a hold decision.
"""

from __future__ import annotations

import hashlib
import itertools
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
from typing import Any, Mapping, Sequence

from structured_candidate import (
    StructuredCandidateAnswer,
    StructuredCandidateDecision,
    _location_matches,
)


PPTX_MIXED_RULE_VERSION = "0.1"

PPTX_AMOUNT_SUMMARY_PAGE = re.compile(
    r"^(?P<location>[^\r\n、。]+?)の"
    r"(?P<container>[^、。\r\n]+?\.pptx)において、"
    r"(?P<subject>(?:(?:この|本)(?:案件|プロジェクト)(?:にかかる|の)|"
    r"(?:案件|プロジェクト)の)?"
    r"(?:金額|費用|見積(?:金額)?|料金))"
    r"の提示がまとまっているのは何ページですか。?$",
    flags=re.IGNORECASE,
)

PPTX_HIGHLIGHTED_TABLE_VALUE = re.compile(
    r"^(?P<location>[^\r\n、。]+?)の"
    r"(?P<container>[^、。\r\n]+?\.pptx)において、"
    r"(?P<color>[^、。\s]+?)ハイライトされている数値に対応するデータの"
    r"抽出条件と集計内容を(?:答えて|教えて)ください。?$",
    flags=re.IGNORECASE,
)


_PML_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_DML_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_P = "{" + _PML_NS + "}"
_A = "{" + _DML_NS + "}"
_R = "{" + _REL_NS + "}"
_PR = "{" + _PKG_REL_NS + "}"

_SLIDE_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
)
_IMAGE_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
)

_MAX_PPTX_BYTES = 256 * 1024 * 1024
_MAX_ZIP_ENTRIES = 10_000
_MAX_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 200.0
_MAX_XML_BYTES = 32 * 1024 * 1024
_MAX_EMF_RECORDS = 100_000
_MAX_EMF_TEXT_RUNS = 10_000
_MAX_EMF_CHARS = 16_384
_MAX_GDI_OBJECTS = 10_000
_MAX_FILENAME_VARIANTS = 64
_XML_FORBIDDEN = (b"<!DOCTYPE", b"<!ENTITY")
_EMF_COORDINATE_STATE_RECORDS = {9, 10, 11, 12, 16, 17, 31, 32}
_ALLOWED_EMF_RECORD_TYPES = frozenset(
    {
        1,   # HEADER
        10,  # SETWINDOWORGEX; only the authored no-op origin is accepted
        14,  # EOF
        18,  # SETBKMODE
        20,  # SETROP2
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
        82,  # EXTCREATEFONTINDIRECTW
        84,  # EXTTEXTOUTW
    }
)
_FIXED_EMF_RECORD_SIZES = {
    10: 16,
    18: 12,
    20: 12,
    24: 12,
    25: 12,
    30: 24,
    54: 16,
    75: 16,
}
_PATCOPY = 0x00F00021

_SHAPE_TAGS = frozenset(
    {_P + "sp", _P + "graphicFrame", _P + "grpSp", _P + "cxnSp", _P + "pic"}
)

_COLOR_ALIASES = {
    "黄": "yellow",
    "黄色": "yellow",
    "yellow": "yellow",
    "青": "blue",
    "青色": "blue",
    "blue": "blue",
    "赤": "red",
    "赤色": "red",
    "red": "red",
    "緑": "green",
    "緑色": "green",
    "green": "green",
}

_AMOUNT_HEADING = re.compile(
    r"(?:第?[0-9]+[.:、\-]?)?"
    r"(?:費用見積|見積(?:金額|費用)?|費用(?:内訳|総額)?|"
    r"料金(?:表|内訳)?|価格(?:表|内訳)?)"
)
_CURRENCY_AMOUNT = re.compile(
    r"(?:[¥￥]\s*[+-]?[0-9][0-9,]*(?:\.[0-9]+)?|"
    r"[+-]?[0-9][0-9,]*(?:\.[0-9]+)?\s*円)"
)
_TOTAL_LABEL = re.compile(r"契約金額|見積金額|料金合計|費用合計|総額|合計")
_AMOUNT_ROLE_PATTERNS = (
    re.compile(r"税抜"),
    re.compile(r"消費税"),
    re.compile(r"税込"),
    re.compile(r"支払条件|着手金|検収金|支払"),
    re.compile(r"固定価格"),
)
_NET_AMOUNT_LABEL = re.compile(
    r"^(?:契約|見積|料金|費用)(?:金額|合計)?\(?税抜\)?"
)
_TAX_AMOUNT_LABEL = re.compile(r"^消費税(?:額)?")
_GROSS_AMOUNT_LABEL = re.compile(
    r"^(?:契約|見積|料金|費用)(?:金額|合計)?\(?税込\)?"
)

_ROW_FIELD = re.compile(r"(?:行(?:ラベル|項目|見出し)|row\s*(?:labels?|field)?)", re.I)
_COLUMN_FIELD = re.compile(r"(?:カラム|列|column)", re.I)
_NUMBER_TEXT = re.compile(
    r"[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?%?"
)


@dataclass(frozen=True)
class _Slide:
    ordinal: int
    member: str
    hidden: bool
    root: ET.Element


@dataclass(frozen=True)
class _TextRun:
    record_index: int
    x: int
    y: int
    text: str


@dataclass(frozen=True)
class _Marker:
    record_index: int
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class _EmfObservation:
    runs: tuple[_TextRun, ...]
    markers: tuple[_Marker, ...]


@dataclass(frozen=True)
class _HighlightedCell:
    row_field: str
    row_value: str
    column_field: str
    column_value: str
    aggregate_value: str


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


def _contract(
    question: str,
    rule_id: str,
    bindings: Mapping[str, str],
    scope: Mapping[str, Any],
    operators: Sequence[str],
    *,
    container: str,
    value_type: str,
    unit: str | None,
    cardinality: str,
    required_keys: Sequence[str] | None = None,
) -> dict[str, Any]:
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
        "graph_rule_version": PPTX_MIXED_RULE_VERSION,
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
                {
                    "from": nodes[index - 1]["output_ref"],
                    "to": nodes[index]["operation_id"],
                }
                for index in range(1, len(nodes))
            ],
        },
        "requested_output": {
            "source_operation_ref": nodes[-1]["operation_id"],
            "cardinality": cardinality,
            "answer_shape": {
                "container": container,
                "value_type": value_type,
                "unit": unit,
            },
            "display_precision": None,
            "required_keys": list(required_keys) if required_keys else None,
        },
    }
    return {
        "graph_contract_id": "pptx_mixed_"
        + hashlib.sha256(_canonical_json(core).encode("utf-8")).hexdigest()[:32],
        **core,
    }


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    """Compile one supported complete question into a typed graph."""

    if not isinstance(question, str):
        return None
    match = PPTX_AMOUNT_SUMMARY_PAGE.fullmatch(question)
    if match:
        bindings = {
            key: match[key]
            for key in ("location", "container", "subject")
        }
        return _contract(
            question,
            "pptx_amount_summary_page",
            bindings,
            {
                "location": bindings["location"],
                "container": bindings["container"],
                "source_channel": "pptx_active_slide_visible_structure",
                "page_numbering": "presentation_order_preserve_hidden_ordinal",
                "subject": bindings["subject"],
            },
            (
                "retrieve",
                "resolve_filename_alias",
                "parse_slide_order",
                "extract_visible_slide_units",
                "verify_amount_summary_proof",
                "verify_unique",
                "project_page_ordinal",
            ),
            container="scalar",
            value_type="integer",
            unit="ページ",
            cardinality="single",
        )
    match = PPTX_HIGHLIGHTED_TABLE_VALUE.fullmatch(question)
    if match:
        color = _COLOR_ALIASES.get(_normalized(match["color"]))
        if color is None:
            return None
        bindings = {
            "location": match["location"],
            "container": match["container"],
            "color": match["color"],
        }
        return _contract(
            question,
            "pptx_emf_highlighted_table_value",
            bindings,
            {
                "location": bindings["location"],
                "container": bindings["container"],
                "source_channel": "pptx_embedded_emf_unicode_and_brush_geometry",
                "declared_color": color,
                "page_numbering": "presentation_order_preserve_hidden_ordinal",
            },
            (
                "retrieve",
                "parse_slide_order",
                "resolve_picture_relationship",
                "parse_emf_state",
                "detect_declared_color_marker",
                "reconstruct_table_axes",
                "verify_unique",
                "project_conditions_and_value",
            ),
            container="key_value",
            value_type="string",
            unit=None,
            cardinality="multiple",
        )
    return None


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    """Rebuild a graph contract rather than trusting caller-supplied data."""

    if not isinstance(contract, Mapping):
        return False
    expected = graph_contract_for_question(question)
    if expected is None:
        return False
    try:
        return _canonical_json(expected) == _canonical_json(contract)
    except (TypeError, ValueError):
        return False


def _hold(reason: str) -> StructuredCandidateDecision:
    return StructuredCandidateDecision("hold", reason)


def _decision(
    answer: str,
    path: Path,
    root: Path,
    operations: int,
) -> StructuredCandidateDecision:
    relative = unicodedata.normalize("NFC", path.relative_to(root).as_posix())
    return StructuredCandidateDecision(
        "resolved",
        "certified_pptx_mixed",
        StructuredCandidateAnswer(
            answer=answer,
            source_paths=(relative,),
            source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            operation_count=operations,
            output_count=1,
        ),
    )


def _safe_root(engine: Any) -> Path | None:
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


def _exact_alias_values(value: str, glossary: Any) -> tuple[str, ...] | None:
    entries = getattr(glossary, "entries", {})
    if not isinstance(entries, Mapping):
        return (value,)
    canonical: set[str] = set()
    for alias, raw_values in entries.items():
        if _normalized(alias) != _normalized(value):
            continue
        if not isinstance(raw_values, Sequence) or isinstance(
            raw_values, (str, bytes, bytearray)
        ):
            return None
        for raw in raw_values:
            rendered = str(raw).strip()
            if rendered:
                canonical.add(rendered)
    if len(canonical) > 1:
        return None
    values = [value]
    if canonical:
        resolved = next(iter(canonical))
        if _normalized(resolved) != _normalized(value):
            values.append(resolved)
    return tuple(values)


def _filename_variants(container: str, glossary: Any) -> tuple[str, ...] | None:
    normalized = unicodedata.normalize("NFKC", container).strip()
    if not normalized.casefold().endswith(".pptx"):
        return None
    stem = normalized[:-5]
    parts = re.split(r"([_.\-\s]+)", stem)
    choices: list[tuple[str, ...]] = []
    combinations = 1
    for part in parts:
        if not part or re.fullmatch(r"[_.\-\s]+", part):
            options = (part,)
        else:
            options = _exact_alias_values(part, glossary)
            if options is None:
                return None
            if any("/" in option or "\\" in option for option in options):
                return None
        choices.append(options)
        combinations *= len(options)
        if combinations > _MAX_FILENAME_VARIANTS:
            return None
    variants = {
        "".join(values) + ".pptx"
        for values in itertools.product(*choices)
    }
    return tuple(sorted(variants, key=lambda item: (_normalized(item), item)))


def _named_pptx(
    engine: Any,
    location: str,
    container: str,
) -> tuple[Path, ...] | None:
    root = _safe_root(engine)
    if root is None:
        return None
    glossary = getattr(engine, "glossary", None)
    location_values = _exact_alias_values(location, glossary)
    variants = _filename_variants(container, glossary)
    if location_values is None or variants is None:
        return None
    names = {_normalized(value) for value in variants}
    matches: list[Path] = []
    try:
        paths = root.rglob("*")
        for path in paths:
            if (
                not path.is_file()
                or path.name.startswith("~$")
                or path.suffix.casefold() != ".pptx"
                or _has_symlink_component(path, root)
            ):
                continue
            relative = path.relative_to(root)
            if not _location_matches(relative.parts[:-1], location_values):
                continue
            if _normalized(path.name) not in names:
                continue
            if path.stat().st_size > _MAX_PPTX_BYTES:
                continue
            matches.append(path.resolve())
    except (OSError, RuntimeError):
        return None
    return tuple(
        sorted(set(matches), key=lambda item: unicodedata.normalize("NFC", item.as_posix()))
    )


def _safe_xml(data: bytes) -> ET.Element | None:
    if len(data) > _MAX_XML_BYTES:
        return None
    upper = data.upper()
    if any(token in upper for token in _XML_FORBIDDEN):
        return None
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        return None


def _open_archive(
    path: Path,
) -> tuple[zipfile.ZipFile, dict[str, zipfile.ZipInfo]] | None:
    try:
        if path.stat().st_size > _MAX_PPTX_BYTES:
            return None
        archive = zipfile.ZipFile(path)
        infos = archive.infolist()
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return None
    if not infos or len(infos) > _MAX_ZIP_ENTRIES:
        archive.close()
        return None
    records: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in infos:
        name = info.filename
        pure = PurePosixPath(name)
        if (
            name in records
            or pure.is_absolute()
            or ".." in pure.parts
            or "\\" in name
            or info.flag_bits & 0x1
            or info.file_size > _MAX_MEMBER_BYTES
        ):
            archive.close()
            return None
        if info.compress_size == 0:
            if info.file_size:
                archive.close()
                return None
        elif info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO:
            archive.close()
            return None
        total += info.file_size
        if total > _MAX_TOTAL_BYTES:
            archive.close()
            return None
        records[name] = info
    return archive, records


def _safe_target(base_member: str, target: str) -> str | None:
    if not target or "\\" in target:
        return None
    normalized = posixpath.normpath(
        posixpath.join(posixpath.dirname(base_member), target)
    )
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts:
        return None
    return normalized


def _slides(
    archive: zipfile.ZipFile,
    records: Mapping[str, zipfile.ZipInfo],
) -> tuple[_Slide, ...] | None:
    presentation_name = "ppt/presentation.xml"
    relationship_name = "ppt/_rels/presentation.xml.rels"
    if presentation_name not in records or relationship_name not in records:
        return None
    try:
        presentation = _safe_xml(archive.read(presentation_name))
        relationships = _safe_xml(archive.read(relationship_name))
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile):
        return None
    if presentation is None or relationships is None:
        return None
    targets: dict[str, str] = {}
    seen_ids: set[str] = set()
    for relationship in relationships.findall(_PR + "Relationship"):
        relationship_id = relationship.get("Id")
        if not relationship_id or relationship_id in seen_ids:
            return None
        seen_ids.add(relationship_id)
        if relationship.get("Type") != _SLIDE_REL_TYPE:
            continue
        if relationship.get("TargetMode") == "External":
            return None
        target = _safe_target(presentation_name, relationship.get("Target") or "")
        if target is None or target not in records:
            return None
        targets[relationship_id] = target
    slide_list = presentation.find(_P + "sldIdLst")
    if slide_list is None:
        return None
    slides: list[_Slide] = []
    seen_members: set[str] = set()
    for ordinal, slide_id in enumerate(slide_list.findall(_P + "sldId"), 1):
        relationship_id = slide_id.get(_R + "id")
        member = targets.get(relationship_id or "")
        if member is None or member in seen_members:
            return None
        seen_members.add(member)
        try:
            root = _safe_xml(archive.read(member))
        except (KeyError, OSError, RuntimeError, zipfile.BadZipFile):
            return None
        if root is None or root.tag != _P + "sld":
            return None
        hidden = (
            slide_id.get("show", "1") in {"0", "false", "False"}
            or root.get("show", "1") in {"0", "false", "False"}
        )
        slides.append(_Slide(ordinal, member, hidden, root))
    return tuple(slides) if slides else None


def _shape_hidden(element: ET.Element) -> bool:
    for child in element:
        if not child.tag.startswith(_P + "nv"):
            continue
        for property_node in child:
            if property_node.tag == _P + "cNvPr":
                return property_node.get("hidden", "0") in {"1", "true", "True"}
    return False


def _visible_shape_texts(root: ET.Element) -> tuple[str, ...]:
    shape_tree = root.find("./" + _P + "cSld/" + _P + "spTree")
    if shape_tree is None:
        return ()
    values: list[str] = []

    def visit(element: ET.Element, inherited_hidden: bool = False) -> None:
        hidden = inherited_hidden
        if element.tag in _SHAPE_TAGS:
            hidden = hidden or _shape_hidden(element)
        if hidden:
            return
        if element.tag == _P + "grpSp":
            for child in element:
                visit(child, hidden)
            return
        if element.tag in _SHAPE_TAGS:
            text = "".join(node.text or "" for node in element.iter(_A + "t"))
            if text.strip():
                values.append(text)
            return
        for child in element:
            visit(child, hidden)

    for child in shape_tree:
        visit(child)
    return tuple(values)


def _amount_summary_candidate(text_units: Sequence[str]) -> bool:
    compact_units = tuple(_compact(value) for value in text_units if value.strip())
    if not any(_AMOUNT_HEADING.fullmatch(value) for value in compact_units):
        return False
    joined = "\n".join(
        unicodedata.normalize("NFKC", value) for value in text_units
    )
    amounts = {_compact(match.group(0)) for match in _CURRENCY_AMOUNT.finditer(joined)}
    if len(amounts) < 3 or _TOTAL_LABEL.search(joined) is None:
        return False
    role_count = sum(pattern.search(joined) is not None for pattern in _AMOUNT_ROLE_PATTERNS)
    return role_count >= 3 and _amount_summary_arithmetic_is_valid(text_units)


def _labeled_amount(
    text_units: Sequence[str], label: re.Pattern[str]
) -> Decimal | None:
    matches: list[Decimal] = []
    for raw in text_units:
        compact = _compact(raw)
        if label.search(compact) is None:
            continue
        currency = tuple(_CURRENCY_AMOUNT.finditer(compact))
        if len(currency) != 1:
            return None
        token = unicodedata.normalize("NFKC", currency[0].group(0))
        token = token.replace("¥", "").replace("円", "").replace(",", "")
        token = re.sub(r"\s+", "", token)
        digit_count = sum(character.isdigit() for character in token)
        if digit_count == 0 or digit_count > 24:
            return None
        try:
            value = Decimal(token)
        except InvalidOperation:
            return None
        if not value.is_finite() or value < 0:
            return None
        matches.append(value)
    if len(matches) != 1:
        return None
    return matches[0]


def _amount_summary_arithmetic_is_valid(text_units: Sequence[str]) -> bool:
    net = _labeled_amount(text_units, _NET_AMOUNT_LABEL)
    tax = _labeled_amount(text_units, _TAX_AMOUNT_LABEL)
    gross = _labeled_amount(text_units, _GROSS_AMOUNT_LABEL)
    return net is not None and tax is not None and gross is not None and net + tax == gross


def _amount_summary_page(
    engine: Any,
    match: re.Match[str],
) -> StructuredCandidateDecision:
    paths = _named_pptx(engine, match["location"], match["container"])
    if paths is None:
        return _hold("pptx_alias_or_root_ambiguous")
    if len(paths) != 1:
        return _hold("pptx_source_not_unique")
    path = paths[0]
    opened = _open_archive(path)
    if opened is None:
        return _hold("pptx_archive_invalid")
    archive, records = opened
    try:
        slides = _slides(archive, records)
        if slides is None:
            return _hold("pptx_slide_order_invalid")
        candidates = [
            slide.ordinal
            for slide in slides
            if not slide.hidden
            and _amount_summary_candidate(_visible_shape_texts(slide.root))
        ]
    finally:
        archive.close()
    if len(candidates) != 1:
        return _hold("pptx_amount_summary_not_unique")
    root = _safe_root(engine)
    if root is None:
        return _hold("source_root_invalid")
    return _decision(f"{candidates[0]}ページ", path, root, 7)


def _visible_pictures(root: ET.Element) -> tuple[ET.Element, ...]:
    shape_tree = root.find("./" + _P + "cSld/" + _P + "spTree")
    if shape_tree is None:
        return ()
    pictures: list[ET.Element] = []

    def visit(element: ET.Element, inherited_hidden: bool = False) -> None:
        hidden = inherited_hidden
        if element.tag in _SHAPE_TAGS:
            hidden = hidden or _shape_hidden(element)
        if hidden:
            return
        if element.tag == _P + "pic":
            pictures.append(element)
            return
        for child in element:
            visit(child, hidden)

    for child in shape_tree:
        visit(child)
    return tuple(pictures)


def _slide_relationships(
    archive: zipfile.ZipFile,
    records: Mapping[str, zipfile.ZipInfo],
    slide_member: str,
) -> Mapping[str, tuple[str, str, bool]] | None:
    slide_path = PurePosixPath(slide_member)
    relationship_member = str(
        slide_path.parent / "_rels" / (slide_path.name + ".rels")
    )
    if relationship_member not in records:
        return None
    try:
        root = _safe_xml(archive.read(relationship_member))
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile):
        return None
    if root is None:
        return None
    relationships: dict[str, tuple[str, str, bool]] = {}
    for relationship in root.findall(_PR + "Relationship"):
        relationship_id = relationship.get("Id")
        target = relationship.get("Target") or ""
        relationship_type = relationship.get("Type") or ""
        external = relationship.get("TargetMode") == "External"
        if not relationship_id or relationship_id in relationships:
            return None
        resolved = target if external else _safe_target(slide_member, target)
        if resolved is None:
            return None
        relationships[relationship_id] = (resolved, relationship_type, external)
    return relationships


def _picture_members(
    archive: zipfile.ZipFile,
    records: Mapping[str, zipfile.ZipInfo],
    slide: _Slide,
) -> tuple[str, ...] | None:
    pictures = _visible_pictures(slide.root)
    if not pictures:
        return ()
    relationships = _slide_relationships(archive, records, slide.member)
    if relationships is None:
        return None
    members: list[str] = []
    for picture in pictures:
        blips = list(picture.iter(_A + "blip"))
        if len(blips) != 1:
            return None
        embedded = blips[0].get(_R + "embed")
        linked = blips[0].get(_R + "link")
        if not embedded or linked:
            return None
        relation = relationships.get(embedded)
        if relation is None:
            return None
        member, relationship_type, external = relation
        if external or relationship_type != _IMAGE_REL_TYPE or member not in records:
            return None
        members.append(member)
    return tuple(members)


def _stock_brush(
    handle: int,
) -> tuple[str, tuple[int, tuple[int, int, int]] | None]:
    if handle & 0x80000000 == 0:
        return "not_stock", None
    index = handle & 0x7FFFFFFF
    brushes = {
        0: (0, (255, 255, 255)),
        1: (0, (192, 192, 192)),
        2: (0, (128, 128, 128)),
        3: (0, (64, 64, 64)),
        4: (0, (0, 0, 0)),
        5: None,
    }
    if index not in brushes:
        # Stock pens/fonts may be selected between brush operations.  They do
        # not change the selected brush and must not be mistaken for an
        # unknown caller-created object.
        return "other_stock", None
    return "brush", brushes[index]


def _color_matches(rgb: tuple[int, int, int], declared: str) -> bool:
    red, green, blue = rgb
    if declared == "yellow":
        return red >= 224 and green >= 208 and blue <= 112 and abs(red - green) <= 64
    if declared == "blue":
        return blue >= 144 and blue > red * 1.25 and blue > green * 1.05
    if declared == "red":
        return red >= 160 and red > green * 1.35 and red > blue * 1.35
    if declared == "green":
        return green >= 128 and green > red * 1.15 and green > blue * 1.10
    return False


def _emf_observation(data: bytes, declared_color: str) -> _EmfObservation | None:
    position = 0
    record_index = 0
    declared_records: int | None = None
    saw_eof = False
    geometry_seen = False
    text_align = 0
    current_position: tuple[int, int] | None = None
    current_position_fresh = False
    selected_brush: tuple[int, tuple[int, int, int]] | None = None
    state_stack: list[
        tuple[
            int,
            tuple[int, int] | None,
            bool,
            tuple[int, tuple[int, int, int]] | None,
        ]
    ] = []
    objects: dict[int, tuple[str, Any]] = {}
    runs: list[_TextRun] = []
    markers: list[_Marker] = []

    while position < len(data):
        if position + 8 > len(data) or record_index >= _MAX_EMF_RECORDS:
            return None
        record_type, size = struct.unpack_from("<II", data, position)
        if size < 8 or size % 4 or position + size > len(data):
            return None
        if record_type not in _ALLOWED_EMF_RECORD_TYPES:
            return None
        required_size = _FIXED_EMF_RECORD_SIZES.get(record_type)
        if required_size is not None and size != required_size:
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
        elif record_type == 1:
            return None
        if record_type in _EMF_COORDINATE_STATE_RECORDS and geometry_seen:
            return None

        if record_type == 10:
            if size != 16 or struct.unpack_from("<ii", data, position + 8) != (0, 0):
                return None
        elif record_type == 22:
            if size != 12:
                return None
            text_align = struct.unpack_from("<I", data, position + 8)[0]
            if text_align not in {0, 1}:
                return None
        elif record_type == 27:
            if size != 16:
                return None
            current_position = struct.unpack_from("<ii", data, position + 8)
            current_position_fresh = True
        elif record_type == 33:
            if size != 8 or len(state_stack) >= 1024:
                return None
            state_stack.append(
                (text_align, current_position, current_position_fresh, selected_brush)
            )
        elif record_type == 34:
            if size != 12 or struct.unpack_from("<i", data, position + 8)[0] != -1:
                return None
            if not state_stack:
                return None
            text_align, current_position, current_position_fresh, selected_brush = (
                state_stack.pop()
            )
        elif record_type == 39:
            if size != 24:
                return None
            handle, style, color, hatch = struct.unpack_from(
                "<IIII", data, position + 8
            )
            if handle == 0 or handle in objects or len(objects) >= _MAX_GDI_OBJECTS:
                return None
            if style != 0 or hatch != 0:
                return None
            rgb = (color & 255, (color >> 8) & 255, (color >> 16) & 255)
            objects[handle] = ("brush", (style, rgb, hatch))
        elif record_type == 38:
            if size != 28:
                return None
            handle = struct.unpack_from("<I", data, position + 8)[0]
            if handle == 0 or handle in objects or len(objects) >= _MAX_GDI_OBJECTS:
                return None
            objects[handle] = ("other", None)
        elif record_type == 82:
            if size < 332 or size % 4:
                return None
            handle = struct.unpack_from("<I", data, position + 8)[0]
            if handle == 0 or handle in objects or len(objects) >= _MAX_GDI_OBJECTS:
                return None
            objects[handle] = ("other", None)
        elif record_type == 37:
            if size != 12:
                return None
            handle = struct.unpack_from("<I", data, position + 8)[0]
            stock_kind, stock = _stock_brush(handle)
            if stock_kind == "brush":
                selected_brush = stock
            elif stock_kind == "other_stock":
                pass
            elif handle in objects and objects[handle][0] == "brush":
                style, rgb, _hatch = objects[handle][1]
                selected_brush = (style, rgb)
            elif handle not in objects:
                return None
        elif record_type == 40:
            if size != 12:
                return None
            handle = struct.unpack_from("<I", data, position + 8)[0]
            if handle not in objects:
                return None
            if objects[handle][0] == "brush" and selected_brush is not None:
                _style, rgb, _hatch = objects[handle][1]
                if selected_brush[1] == rgb:
                    return None
            del objects[handle]
        elif record_type == 76:
            if size < 100:
                return None
            x, y, width, height, raster_operation = struct.unpack_from(
                "<iiiiI", data, position + 24
            )
            geometry_seen = True
            if selected_brush is not None and _color_matches(
                selected_brush[1], declared_color
            ):
                if raster_operation != _PATCOPY or selected_brush[0] != 0:
                    return None
                if x < 0 or y < 0 or width < 0 or height < 0:
                    return None
                # One-pixel PATCOPY records are authored grid lines, not
                # highlighted cells.  Any other brush-driven primitive is
                # rejected by the record allowlist above.
                if width > 1 and height > 1:
                    markers.append(_Marker(record_index, x, y, width, height))
        elif record_type == 70:
            if size < 12:
                return None
            payload_size = struct.unpack_from("<I", data, position + 8)[0]
            padded_size = (12 + payload_size + 3) // 4 * 4
            if payload_size == 0 or padded_size != size:
                return None
            padding = data[position + 12 + payload_size : position + size]
            if any(padding):
                return None
        elif record_type == 84:
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
                text = data[
                    position + offset : position + offset + byte_count
                ].decode("utf-16le", errors="strict")
            except UnicodeDecodeError:
                return None
            if "\x00" in text:
                return None
            geometry_seen = True
            if text.strip():
                runs.append(_TextRun(record_index, x, y, text))
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
    return _EmfObservation(tuple(runs), tuple(markers))


def _is_number(value: str) -> bool:
    return _NUMBER_TEXT.fullmatch(
        unicodedata.normalize("NFKC", value).strip()
    ) is not None


def _row_clusters(
    runs: Sequence[_TextRun], tolerance: int = 3
) -> tuple[tuple[_TextRun, ...], ...]:
    clusters: list[list[_TextRun]] = []
    for run in sorted(runs, key=lambda item: (item.y, item.x, item.record_index)):
        if not clusters or abs(run.y - clusters[-1][0].y) > tolerance:
            clusters.append([run])
        else:
            clusters[-1].append(run)
    return tuple(
        tuple(sorted(cluster, key=lambda item: (item.x, item.record_index)))
        for cluster in clusters
    )


def _highlighted_cell(observation: _EmfObservation) -> _HighlightedCell | None:
    if len(observation.markers) != 1:
        return None
    marker = observation.markers[0]
    inside = [
        run
        for run in observation.runs
        if marker.x <= run.x < marker.x + marker.width
        and marker.y <= run.y < marker.y + marker.height
    ]
    if len(inside) != 1 or not _is_number(inside[0].text):
        return None
    selected = inside[0]
    clusters = _row_clusters(observation.runs)
    selected_rows = [
        cluster
        for cluster in clusters
        if any(run.record_index == selected.record_index for run in cluster)
    ]
    if len(selected_rows) != 1:
        return None
    selected_row = selected_rows[0]
    min_x = min(run.x for run in selected_row)
    row_labels = [run for run in selected_row if run.x == min_x]
    if (
        len(row_labels) != 1
        or row_labels[0].record_index == selected.record_index
        or row_labels[0].x >= marker.x
    ):
        return None

    header_rows = []
    for cluster in clusters:
        if cluster[0].y >= selected.y:
            continue
        field_runs = [run for run in cluster if _ROW_FIELD.search(run.text)]
        right_runs = [run for run in cluster if run.x > min(run.x for run in cluster)]
        if (
            len(field_runs) == 1
            and field_runs[0].x == min(run.x for run in cluster)
            and len(right_runs) >= 2
        ):
            header_rows.append((cluster, field_runs[0]))
    if len(header_rows) != 1:
        return None
    header_row, row_field = header_rows[0]
    column_values = [
        run
        for run in header_row
        if run.record_index != row_field.record_index
        and marker.x <= run.x < marker.x + marker.width
    ]
    if len(column_values) != 1:
        return None

    column_fields = [
        run
        for run in observation.runs
        if run.y < header_row[0].y and _COLUMN_FIELD.search(run.text)
    ]
    if len(column_fields) != 1:
        return None
    return _HighlightedCell(
        row_field=row_field.text.strip(),
        row_value=row_labels[0].text.strip(),
        column_field=column_fields[0].text.strip(),
        column_value=column_values[0].text.strip(),
        aggregate_value=selected.text.strip(),
    )


def _highlighted_table_value(
    engine: Any,
    match: re.Match[str],
) -> StructuredCandidateDecision:
    color = _COLOR_ALIASES.get(_normalized(match["color"]))
    if color is None:
        return _hold("pptx_highlight_color_unsupported")
    paths = _named_pptx(engine, match["location"], match["container"])
    if paths is None:
        return _hold("pptx_alias_or_root_ambiguous")
    if len(paths) != 1:
        return _hold("pptx_source_not_unique")
    path = paths[0]
    opened = _open_archive(path)
    if opened is None:
        return _hold("pptx_archive_invalid")
    archive, records = opened
    values: list[_HighlightedCell] = []
    try:
        slides = _slides(archive, records)
        if slides is None:
            return _hold("pptx_slide_order_invalid")
        for slide in slides:
            if slide.hidden:
                continue
            members = _picture_members(archive, records, slide)
            if members is None:
                return _hold("pptx_picture_relationship_invalid")
            for member in members:
                if PurePosixPath(member).suffix.casefold() != ".emf":
                    continue
                try:
                    observation = _emf_observation(archive.read(member), color)
                except (KeyError, OSError, RuntimeError, zipfile.BadZipFile):
                    return _hold("pptx_emf_invalid")
                if observation is None:
                    return _hold("pptx_emf_invalid")
                value = _highlighted_cell(observation)
                if value is not None:
                    values.append(value)
    finally:
        archive.close()
    if len(values) != 1:
        return _hold("pptx_highlighted_cell_not_unique")
    value = values[0]
    answer = (
        f"行条件: {value.row_value}（{value.row_field}）、"
        f"列条件: {value.column_value}（{value.column_field}）、"
        f"集計内容: {value.aggregate_value}"
    )
    root = _safe_root(engine)
    if root is None:
        return _hold("source_root_invalid")
    return _decision(answer, path, root, 8)


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    """Resolve a supported question, holding on any unproved source fact."""

    if not isinstance(question, str):
        return None
    match = PPTX_AMOUNT_SUMMARY_PAGE.fullmatch(question)
    if match:
        return _amount_summary_page(engine, match)
    match = PPTX_HIGHLIGHTED_TABLE_VALUE.fullmatch(question)
    if match:
        return _highlighted_table_value(engine, match)
    return None


def decide_from_graph(
    engine: Any,
    question: str,
    graph_plan: Any,
) -> StructuredCandidateDecision | None:
    """Execute only when the live GraphPlan carries the exact rebuilt contract."""

    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    if (
        graph_plan is None
        or getattr(graph_plan, "original_question", None) != question
        or getattr(graph_plan, "strict_status", None) != "pass"
    ):
        return _hold("pptx_mixed_graph_plan_not_certified")
    branches = getattr(graph_plan, "branch_intents", ())
    if not isinstance(branches, tuple) or len(branches) != 1:
        return _hold("pptx_mixed_graph_plan_not_certified")
    branch = branches[0]
    if not isinstance(branch, Mapping) or branch.get("status") != "resolved":
        return _hold("pptx_mixed_graph_plan_not_certified")
    intent = branch.get("intent") if isinstance(branch, Mapping) else None
    supplied = (
        intent.get("extended_graph_contract")
        if isinstance(intent, Mapping)
        else None
    )
    if not isinstance(supplied, Mapping) or not validate_graph_contract(
        question, supplied
    ):
        return _hold("pptx_mixed_graph_plan_contract_mismatch")
    if _canonical_json(supplied) != _canonical_json(contract):
        return _hold("pptx_mixed_graph_plan_contract_mismatch")
    return decide_question(engine, question)


__all__ = [
    "PPTX_MIXED_RULE_VERSION",
    "PPTX_AMOUNT_SUMMARY_PAGE",
    "PPTX_HIGHLIGHTED_TABLE_VALUE",
    "decide_from_graph",
    "decide_question",
    "graph_contract_for_question",
    "validate_graph_contract",
]
