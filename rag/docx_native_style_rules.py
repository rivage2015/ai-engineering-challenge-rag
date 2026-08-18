"""Fail-closed extraction of text selected by native DOCX run styling.

The module deliberately works from WordprocessingML rather than rendered OCR.
It first binds the requested document from question-only scope and source
semantics, then evaluates the effective run style in authored story,
paragraph, and run order.  Any ambiguous source, style cascade, colour, or
match is held instead of being guessed.
"""

from __future__ import annotations

import colorsys
import hashlib
import json
import posixpath
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from structured_candidate import (
    StructuredCandidateAnswer,
    StructuredCandidateDecision,
    _candidate_values,
    _location_matches,
)


DOCX_NATIVE_STYLE_RULE_VERSION = "0.2"

HIGHLIGHT_AND_FONT = re.compile(
    r"^(?P<location>.+?)の(?P<document_role>中間報告資料)にて、"
    r"(?P<highlight_color>[^、。]+?)ハイライトかつ"
    r"(?P<font_color>[^、。]+?)となっている部分を"
    r"抜き出してください。?$"
)
EFFECTIVE_BOLD = re.compile(
    r"^(?P<location>.+?)との(?P<document_role>契約書)において、"
    r"(?P<style>太字)で記載されている部分を"
    r"抽出してください。?$"
)

_HIGHLIGHT_ALIASES = {
    "黄": "yellow",
    "黄色": "yellow",
    "yellow": "yellow",
}
_FONT_COLOR_ALIASES = {
    "赤": "red",
    "赤色": "red",
    "赤字": "red",
    "red": "red",
}

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PR_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_W = "{" + _W_NS + "}"
_A = "{" + _A_NS + "}"
_R = "{" + _R_NS + "}"
_PR = "{" + _PR_NS + "}"
_CT = "{" + _CT_NS + "}"
_MC = "{" + _MC_NS + "}"

_MAX_DOCX_BYTES = 128 * 1024 * 1024
_MAX_ZIP_ENTRIES = 4_096
_MAX_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_XML_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_BYTES = 256 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 100.0
_MAX_XML_NODES = 250_000
_MAX_XML_DEPTH = 256
_MAX_TEXT_CHARS = 5_000_000
_MAX_STYLED_RUNS = 500_000
_MAX_SOURCE_CANDIDATES = 256
_MAX_SCANNED_DOCX = 100_000
_XML_FORBIDDEN = (b"<!DOCTYPE", b"<!ENTITY")
_ARCHIVE_WORDS = (
    "archive",
    "archived",
    "backup",
    "backups",
    "old",
    "アーカイブ",
    "バックアップ",
    "旧版",
    "履歴",
)
_STYLE_PROPERTIES = frozenset({"b", "bCs", "vanish", "highlight", "color"})
_SKIP_CONTENT = frozenset({_W + "del", _W + "moveFrom"})
_OFFICE_REL_PREFIXES = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/",
    "http://purl.oclc.org/ooxml/officeDocument/relationships/",
)


class _DocxStyleError(ValueError):
    """A stable fail-closed reason for an unsafe or ambiguous package."""


@dataclass(frozen=True)
class _ColorSpec:
    value: str | None
    theme: str | None
    tint: str | None
    shade: str | None


@dataclass(frozen=True)
class _RunDelta:
    bold: bool | None = None
    bold_cs: bool | None = None
    vanish: bool | None = None
    highlight_present: bool = False
    highlight: str | None = None
    color_present: bool = False
    color: _ColorSpec | None = None


@dataclass(frozen=True)
class _RunState:
    bold: bool = False
    bold_cs: bool = False
    vanish: bool = False
    highlight: str | None = None
    color_rgb: str | None = None
    color_unresolved: bool = False


@dataclass(frozen=True)
class _StyleDef:
    style_id: str
    style_type: str
    based_on: str | None
    delta: _RunDelta
    has_table_run_effects: bool


@dataclass(frozen=True)
class _StyleSheet:
    doc_defaults: _RunDelta
    styles: Mapping[str, _StyleDef]
    default_by_type: Mapping[str, str]
    theme: Mapping[str, str]


@dataclass(frozen=True)
class _StyledRun:
    story: str
    paragraph: int
    run: int
    text: str
    state: _RunState


@dataclass(frozen=True)
class _Relationship:
    relation_id: str
    relation_type: str
    target: str
    external: bool


@dataclass(frozen=True)
class _PackageGraph:
    main_name: str
    main_root: ET.Element
    relationships: Mapping[str, _Relationship]
    styles_name: str
    theme_name: str | None
    numbering_name: str | None
    content_types: Mapping[str, str]


@dataclass(frozen=True)
class _Story:
    name: str
    root: ET.Element
    paragraphs: tuple[ET.Element, ...]


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
    bindings: Mapping[str, str],
    scope: Mapping[str, Any],
    operators: Sequence[str],
) -> dict[str, Any]:
    nodes = _nodes(operators)
    core = {
        "graph_rule_version": DOCX_NATIVE_STYLE_RULE_VERSION,
        "rule_id": rule_id,
        "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
        "bindings": dict(bindings),
        "scope": dict(scope),
        "operation_graph": {
            "external_inputs": [
                {
                    "input_ref": "input_question",
                    "input_type": "docx_package_set",
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
            "cardinality": "single",
            "answer_shape": {
                "container": "scalar",
                "value_type": "string",
                "unit": None,
            },
            "display_precision": None,
            "required_keys": None,
        },
    }
    return {
        "graph_contract_id": "docx_mixed_native_style_"
        + hashlib.sha256(_canonical_json(core).encode("utf-8")).hexdigest()[:32],
        **core,
    }


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    """Return a typed contract only for a complete supported question."""

    if not isinstance(question, str):
        return None
    match = HIGHLIGHT_AND_FONT.fullmatch(question)
    if match:
        highlight = _HIGHLIGHT_ALIASES.get(_normalized(match["highlight_color"]))
        font = _FONT_COLOR_ALIASES.get(_normalized(match["font_color"]))
        if highlight != "yellow" or font != "red":
            return None
        bindings = {
            key: match[key]
            for key in (
                "location",
                "document_role",
                "highlight_color",
                "font_color",
            )
        }
        return _contract(
            question,
            "docx_effective_highlight_font_intersection",
            bindings,
            {
                "location": bindings["location"],
                "container": "05.会議/報告資料/*.docx",
                "document_role": "semantic_middle_report",
                "source_channel": "wordprocessingml_effective_run_style",
                "style_predicates": [
                    {"property": "highlight", "operator": "eq", "value": "yellow"},
                    {"property": "font_color", "operator": "is_hue", "value": "red"},
                ],
                "projection": "authored_text_exact",
            },
            (
                "retrieve",
                "semantic_bind_document_role",
                "validate_docx_package",
                "parse_style_cascade",
                "compute_effective_run_style",
                "filter_style_intersection",
                "coalesce_contiguous_runs",
                "verify_unique",
                "project_exact_text",
            ),
        )
    match = EFFECTIVE_BOLD.fullmatch(question)
    if match:
        bindings = {
            key: match[key] for key in ("location", "document_role", "style")
        }
        return _contract(
            question,
            "docx_effective_bold_text",
            bindings,
            {
                "location": bindings["location"],
                "container": "01.契約/契約書.docx",
                "source_channel": "wordprocessingml_effective_run_style",
                "style_predicates": [
                    {"property": "bold", "operator": "eq", "value": True}
                ],
                "projection": "authored_text_exact",
            },
            (
                "retrieve",
                "validate_docx_package",
                "parse_style_cascade",
                "compute_effective_run_style",
                "filter_effective_bold",
                "coalesce_contiguous_runs",
                "verify_unique",
                "project_exact_text",
            ),
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


def _archived_component(value: str) -> bool:
    normalized = _normalized(value)
    return normalized in _ARCHIVE_WORDS or bool(
        re.search(r"(?:^|[_ .-])(?:archive|backup|old)(?:$|[_ .-])", normalized)
    )


def _source_candidates(
    engine: Any,
    location: str,
    *,
    kind: str,
) -> tuple[Path, ...]:
    root = _safe_root(engine)
    if root is None:
        return ()
    locations = _candidate_values(location, getattr(engine, "glossary", None))
    matches: list[Path] = []
    scanned = 0
    try:
        for path in root.rglob("*.docx"):
            scanned += 1
            if scanned > _MAX_SCANNED_DOCX:
                raise _DocxStyleError("docx_source_scope_too_large")
            if (
                not path.is_file()
                or path.is_symlink()
                or path.name.startswith(("~$", "."))
                or _has_symlink_component(path, root)
            ):
                continue
            relative = path.relative_to(root)
            if any(_archived_component(part) for part in relative.parts):
                continue
            if not _location_matches(relative.parts[:-1], locations):
                continue
            compact_parts = tuple(_compact(part) for part in relative.parts[:-1])
            stem = _compact(path.stem)
            if kind == "middle_report":
                if "報告資料" not in stem or not any(
                    "報告資料" in part for part in compact_parts
                ):
                    continue
            elif kind == "contract":
                if stem != "契約書" or not any(
                    "契約" in part for part in compact_parts
                ):
                    continue
            else:  # pragma: no cover - private caller invariant
                raise AssertionError("unsupported DOCX source kind")
            matches.append(path.resolve())
            if len(matches) > _MAX_SOURCE_CANDIDATES:
                raise _DocxStyleError("docx_source_scope_too_large")
    except OSError:
        return ()
    return tuple(sorted(set(matches), key=lambda item: item.as_posix()))


def _validate_archive(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    if not infos or len(infos) > _MAX_ZIP_ENTRIES:
        raise _DocxStyleError("docx_archive_invalid")
    names = [info.filename for info in infos]
    canonical_names = [
        unicodedata.normalize("NFC", name.rstrip("/")).casefold()
        for name in names
    ]
    if len(names) != len(set(names)) or len(canonical_names) != len(
        set(canonical_names)
    ):
        raise _DocxStyleError("docx_archive_invalid")
    total = 0
    for info in infos:
        name = info.filename
        body = name.rstrip("/")
        pure = PurePosixPath(name)
        if (
            not name
            or not body
            or "\\" in name
            or any(marker in name for marker in ("%", "?", "#"))
            or any(ord(character) < 32 for character in name)
            or pure.is_absolute()
            or ".." in pure.parts
            or posixpath.normpath(body) != body
            or info.flag_bits & 0x1
            or info.file_size < 0
            or info.file_size > _MAX_MEMBER_BYTES
            or (info.is_dir() and info.file_size)
        ):
            raise _DocxStyleError("docx_archive_invalid")
        total += info.file_size
        if total > _MAX_TOTAL_BYTES:
            raise _DocxStyleError("docx_archive_invalid")
        if info.file_size and not info.compress_size:
            raise _DocxStyleError("docx_archive_invalid")
        if (
            info.compress_size
            and info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO
        ):
            raise _DocxStyleError("docx_archive_invalid")
    if "[Content_Types].xml" not in names or "_rels/.rels" not in names:
        raise _DocxStyleError("docx_main_story_missing")


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes:
    pure = PurePosixPath(name)
    if pure.is_absolute() or ".." in pure.parts or "\\" in name:
        raise _DocxStyleError("docx_archive_invalid")
    try:
        info = archive.getinfo(name)
        if info.is_dir() or not 0 < info.file_size <= _MAX_XML_BYTES:
            raise _DocxStyleError("docx_archive_invalid")
        value = archive.read(name)
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise _DocxStyleError("docx_archive_invalid") from exc
    if len(value) != info.file_size:
        raise _DocxStyleError("docx_archive_invalid")
    if b"\x00" in value:
        # OPC XML is expected to be UTF-8 here.  Rejecting NUL-bearing UTF-16
        # also prevents byte-level evasion of the DTD/entity guard below.
        raise _DocxStyleError("docx_xml_encoding_unsupported")
    try:
        decoded = value.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _DocxStyleError("docx_xml_encoding_unsupported") from exc
    upper = decoded.upper().encode("utf-8")
    if any(marker in upper for marker in _XML_FORBIDDEN):
        raise _DocxStyleError("docx_xml_unsafe")
    return value


def _xml_root(archive: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        root = ET.fromstring(_read_member(archive, name))
    except ET.ParseError as exc:
        raise _DocxStyleError("docx_xml_malformed") from exc
    count = 0
    text_chars = 0
    stack: list[tuple[ET.Element, int]] = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        count += 1
        text_chars += len(node.text or "") + len(node.tail or "")
        if (
            count > _MAX_XML_NODES
            or depth > _MAX_XML_DEPTH
            or text_chars > _MAX_TEXT_CHARS
        ):
            raise _DocxStyleError("docx_xml_resource_limit")
        if node.tag == _MC + "AlternateContent":
            raise _DocxStyleError("docx_markup_compatibility_ambiguous")
        stack.extend((child, depth + 1) for child in reversed(list(node)))
    return root


def _validate_content_types(archive: zipfile.ZipFile) -> dict[str, str]:
    root = _xml_root(archive, "[Content_Types].xml")
    if root.tag != _CT + "Types":
        raise _DocxStyleError("docx_content_types_invalid")
    defaults: dict[str, str] = {}
    overrides: dict[str, str] = {}
    for node in root:
        if node.tag == _CT + "Default":
            extension = _normalized(node.get("Extension") or "")
            content_type = (node.get("ContentType") or "").strip()
            if not extension or not content_type or extension in defaults:
                raise _DocxStyleError("docx_content_types_invalid")
            defaults[extension] = content_type
        elif node.tag == _CT + "Override":
            part = node.get("PartName") or ""
            content_type = (node.get("ContentType") or "").strip()
            if (
                not part.startswith("/")
                or "\\" in part
                or any(marker in part for marker in ("%", "?", "#"))
                or not content_type
            ):
                raise _DocxStyleError("docx_content_types_invalid")
            canonical = unicodedata.normalize("NFC", part).casefold()
            if canonical in overrides:
                raise _DocxStyleError("docx_content_types_invalid")
            overrides[canonical] = content_type
        else:
            raise _DocxStyleError("docx_content_types_invalid")
    result: dict[str, str] = {}
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = info.filename
        canonical = "/" + unicodedata.normalize("NFC", name).casefold()
        content_type = overrides.get(canonical)
        if content_type is None and "." in posixpath.basename(name):
            content_type = defaults.get(
                _normalized(posixpath.basename(name).rsplit(".", 1)[-1])
            )
        if content_type is not None:
            result[unicodedata.normalize("NFC", name).casefold()] = content_type
    return result


def _require_content_type(
    content_types: Mapping[str, str],
    name: str,
    expected: str,
) -> None:
    actual = content_types.get(unicodedata.normalize("NFC", name).casefold())
    if actual != expected:
        raise _DocxStyleError("docx_content_type_mismatch")


def _relationship_part(source: str | None) -> str:
    if source is None:
        return "_rels/.rels"
    parent = posixpath.dirname(source)
    name = posixpath.basename(source)
    return posixpath.join(parent, "_rels", name + ".rels")


def _relationship_map(
    archive: zipfile.ZipFile,
    source: str | None,
) -> dict[str, _Relationship]:
    part = _relationship_part(source)
    if part not in archive.namelist():
        raise _DocxStyleError("docx_relationship_missing")
    root = _xml_root(archive, part)
    if root.tag != _PR + "Relationships":
        raise _DocxStyleError("docx_relationship_invalid")
    result: dict[str, _Relationship] = {}
    for node in root:
        if node.tag != _PR + "Relationship":
            raise _DocxStyleError("docx_relationship_invalid")
        relation_id = (node.get("Id") or "").strip()
        relation_type = (node.get("Type") or "").strip()
        target = (node.get("Target") or "").strip()
        mode = _normalized(node.get("TargetMode") or "internal")
        if (
            not relation_id
            or not relation_type
            or not target
            or relation_id in result
            or mode not in {"internal", "external"}
        ):
            raise _DocxStyleError("docx_relationship_invalid")
        result[relation_id] = _Relationship(
            relation_id,
            relation_type,
            target,
            mode == "external",
        )
    return result


def _relation_kind(relation: _Relationship, kind: str) -> bool:
    return relation.relation_type in {
        prefix + kind for prefix in _OFFICE_REL_PREFIXES
    }


def _resolve_target(
    archive: zipfile.ZipFile,
    source: str | None,
    relation: _Relationship,
) -> str:
    target = relation.target
    if (
        relation.external
        or not target
        or target.startswith(("/", "\\"))
        or "\\" in target
        or ":" in target
        or any(marker in target for marker in ("%", "?", "#"))
    ):
        raise _DocxStyleError("docx_relationship_invalid")
    base = posixpath.dirname(source or "")
    resolved = posixpath.normpath(posixpath.join(base, target))
    if resolved in {"", ".", ".."} or resolved.startswith("../"):
        raise _DocxStyleError("docx_relationship_invalid")
    try:
        info = archive.getinfo(resolved)
    except KeyError as exc:
        raise _DocxStyleError("docx_relationship_target_missing") from exc
    if info.is_dir():
        raise _DocxStyleError("docx_relationship_target_missing")
    return resolved


def _selected_relation(
    archive: zipfile.ZipFile,
    source: str | None,
    relationships: Mapping[str, _Relationship],
    kind: str,
    *,
    required: bool,
) -> str | None:
    values = [
        relation
        for relation in relationships.values()
        if _relation_kind(relation, kind)
    ]
    if len(values) > 1 or (required and len(values) != 1):
        raise _DocxStyleError("docx_relationship_not_unique")
    if not values:
        return None
    return _resolve_target(archive, source, values[0])


def _package_graph(archive: zipfile.ZipFile) -> _PackageGraph:
    content_types = _validate_content_types(archive)
    root_relationships = _relationship_map(archive, None)
    main_name = _selected_relation(
        archive,
        None,
        root_relationships,
        "officeDocument",
        required=True,
    )
    assert main_name is not None
    _require_content_type(
        content_types,
        main_name,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    )
    main_root = _xml_root(archive, main_name)
    if main_root.tag != _W + "document":
        raise _DocxStyleError("docx_xml_malformed")
    relationships = _relationship_map(archive, main_name)
    styles_name = _selected_relation(
        archive, main_name, relationships, "styles", required=True
    )
    assert styles_name is not None
    _require_content_type(
        content_types,
        styles_name,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml",
    )
    theme_name = _selected_relation(
        archive, main_name, relationships, "theme", required=False
    )
    numbering_name = _selected_relation(
        archive, main_name, relationships, "numbering", required=False
    )
    if theme_name is not None:
        _require_content_type(
            content_types,
            theme_name,
            "application/vnd.openxmlformats-officedocument.theme+xml",
        )
    if numbering_name is not None:
        _require_content_type(
            content_types,
            numbering_name,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml",
        )
    selected = [main_name, styles_name]
    selected.extend(
        value for value in (theme_name, numbering_name) if value is not None
    )
    if len(selected) != len(set(selected)):
        raise _DocxStyleError("docx_relationship_invalid")
    return _PackageGraph(
        main_name,
        main_root,
        relationships,
        styles_name,
        theme_name,
        numbering_name,
        content_types,
    )


def _visible_elements(node: ET.Element) -> Iterable[ET.Element]:
    for child in node:
        if child.tag in _SKIP_CONTENT:
            continue
        yield child
        yield from _visible_elements(child)


def _visible_paragraphs(node: ET.Element) -> tuple[ET.Element, ...]:
    return tuple(
        child for child in _visible_elements(node) if child.tag == _W + "p"
    )


def _related_story(
    archive: zipfile.ZipFile,
    graph: _PackageGraph,
    relation_id: str,
    kind: str,
    expected_root: str,
) -> tuple[str, ET.Element]:
    relation = graph.relationships.get(relation_id)
    if relation is None or not _relation_kind(relation, kind):
        raise _DocxStyleError("docx_story_relationship_invalid")
    name = _resolve_target(archive, graph.main_name, relation)
    _require_content_type(
        graph.content_types,
        name,
        "application/vnd.openxmlformats-officedocument.wordprocessingml."
        + kind
        + "+xml",
    )
    root = _xml_root(archive, name)
    if root.tag != expected_root:
        raise _DocxStyleError("docx_xml_malformed")
    return name, root


def _referenced_note_story(
    archive: zipfile.ZipFile,
    graph: _PackageGraph,
    kind: str,
) -> _Story | None:
    reference_tag = _W + ("footnoteReference" if kind == "footnotes" else "endnoteReference")
    note_tag = _W + ("footnote" if kind == "footnotes" else "endnote")
    root_tag = _W + kind
    identifiers: list[str] = []
    for node in _visible_elements(graph.main_root):
        if node.tag != reference_tag:
            continue
        identifier = node.get(_W + "id")
        if identifier is None:
            raise _DocxStyleError("docx_story_reference_invalid")
        if identifier not in identifiers:
            identifiers.append(identifier)
    if not identifiers:
        return None
    name = _selected_relation(
        archive,
        graph.main_name,
        graph.relationships,
        kind,
        required=True,
    )
    assert name is not None
    _require_content_type(
        graph.content_types,
        name,
        "application/vnd.openxmlformats-officedocument.wordprocessingml."
        + kind
        + "+xml",
    )
    root = _xml_root(archive, name)
    if root.tag != root_tag:
        raise _DocxStyleError("docx_xml_malformed")
    by_id: dict[str, ET.Element] = {}
    for note in root.findall("./" + note_tag):
        identifier = note.get(_W + "id")
        if identifier is None or identifier in by_id:
            raise _DocxStyleError("docx_story_reference_invalid")
        by_id[identifier] = note
    if any(identifier not in by_id for identifier in identifiers):
        raise _DocxStyleError("docx_story_reference_invalid")
    paragraphs = tuple(
        paragraph
        for identifier in identifiers
        for paragraph in _visible_paragraphs(by_id[identifier])
    )
    return _Story(name, root, paragraphs)


def _stories(archive: zipfile.ZipFile, graph: _PackageGraph) -> tuple[_Story, ...]:
    result = [
        _Story(
            graph.main_name,
            graph.main_root,
            _visible_paragraphs(graph.main_root),
        )
    ]
    seen_parts = {graph.main_name}
    for node in _visible_elements(graph.main_root):
        if node.tag not in {_W + "headerReference", _W + "footerReference"}:
            continue
        relation_id = node.get(_R + "id")
        if not relation_id:
            raise _DocxStyleError("docx_story_reference_invalid")
        kind = "header" if node.tag == _W + "headerReference" else "footer"
        expected = _W + ("hdr" if kind == "header" else "ftr")
        name, root = _related_story(
            archive, graph, relation_id, kind, expected
        )
        if name in seen_parts:
            continue
        seen_parts.add(name)
        result.append(_Story(name, root, _visible_paragraphs(root)))
    for kind in ("footnotes", "endnotes"):
        story = _referenced_note_story(archive, graph, kind)
        if story is not None:
            if story.name in seen_parts:
                raise _DocxStyleError("docx_story_relationship_invalid")
            seen_parts.add(story.name)
            result.append(story)
    return tuple(result)


def _visible_runs(node: ET.Element) -> Iterable[ET.Element]:
    for child in node:
        if child.tag in _SKIP_CONTENT:
            continue
        if child.tag == _W + "p":
            continue
        if child.tag == _W + "r":
            yield child
            continue
        yield from _visible_runs(child)


def _run_text(run: ET.Element) -> str:
    values: list[str] = []

    def visit(node: ET.Element) -> None:
        for child in node:
            # A drawing/text-box can contain its own paragraph and runs.  They
            # are processed independently by the story walker and must not
            # inherit the formatting of the enclosing run.
            if child.tag in {_W + "p", _W + "r"}:
                continue
            if child.tag == _W + "t":
                values.append(child.text or "")
            elif child.tag == _W + "tab":
                values.append("\t")
            elif child.tag in {_W + "br", _W + "cr"}:
                values.append("\n")
            elif child.tag == _W + "noBreakHyphen":
                values.append("\u2011")
            elif child.tag == _W + "softHyphen":
                values.append("\u00ad")
            else:
                visit(child)

    visit(run)
    return "".join(values)


def _semantic_body_text(path: Path) -> str:
    main_story, runs = _read_styled_document(path)
    paragraphs: list[str] = []
    fragments: list[str] = []
    current: tuple[str, int] | None = None
    total = 0
    for run in runs:
        if run.story != main_story or run.state.vanish:
            continue
        key = (run.story, run.paragraph)
        if current is not None and key != current:
            paragraphs.append("".join(fragments))
            fragments = []
        current = key
        total += len(run.text)
        if total > _MAX_TEXT_CHARS:
            raise _DocxStyleError("docx_xml_resource_limit")
        fragments.append(run.text)
    if current is not None:
        paragraphs.append("".join(fragments))
    return "\n".join(paragraphs)


def _middle_report_source(engine: Any, location: str) -> Path:
    candidates = _source_candidates(engine, location, kind="middle_report")
    if not candidates:
        raise _DocxStyleError("docx_source_not_found")
    matches: list[Path] = []
    for path in candidates:
        text = unicodedata.normalize("NFKC", _semantic_body_text(path))
        if re.search(r"中間(?:分析|進捗|評価)?報告", text):
            matches.append(path)
    if len(matches) != 1:
        raise _DocxStyleError("docx_semantic_source_not_unique")
    return matches[0]


def _contract_source(engine: Any, location: str) -> Path:
    matches = _source_candidates(engine, location, kind="contract")
    if len(matches) != 1:
        raise _DocxStyleError("docx_source_not_unique")
    return matches[0]


def _single_child(parent: ET.Element | None, tag: str) -> ET.Element | None:
    if parent is None:
        return None
    values = [child for child in parent if child.tag == tag]
    if len(values) > 1:
        raise _DocxStyleError("docx_style_conflict")
    return values[0] if values else None


def _on_off(node: ET.Element) -> bool:
    value = node.get(_W + "val")
    if value is None:
        return True
    return _on_off_value(value)


def _on_off_value(value: str) -> bool:
    normalized = _normalized(value)
    if normalized in {"1", "true", "on", "yes"}:
        return True
    if normalized in {"0", "false", "off", "no"}:
        return False
    raise _DocxStyleError("docx_style_value_invalid")


def _run_delta(rpr: ET.Element | None) -> _RunDelta:
    if rpr is None:
        return _RunDelta()
    relevant: dict[str, ET.Element] = {}
    for child in rpr:
        local = child.tag.rsplit("}", 1)[-1]
        if local not in _STYLE_PROPERTIES:
            continue
        if local in relevant:
            raise _DocxStyleError("docx_style_conflict")
        relevant[local] = child
    highlight_present = "highlight" in relevant
    highlight: str | None = None
    if highlight_present:
        raw = relevant["highlight"].get(_W + "val")
        if raw is None:
            raise _DocxStyleError("docx_style_value_invalid")
        normalized = _normalized(raw)
        highlight = None if normalized in {"none", "nil"} else normalized
        allowed = {
            "black",
            "blue",
            "cyan",
            "green",
            "magenta",
            "red",
            "yellow",
            "white",
            "darkblue",
            "darkcyan",
            "darkgreen",
            "darkmagenta",
            "darkred",
            "darkyellow",
            "darkgray",
            "lightgray",
        }
        if highlight is not None and highlight not in allowed:
            raise _DocxStyleError("docx_style_value_invalid")
    color_present = "color" in relevant
    color: _ColorSpec | None = None
    if color_present:
        node = relevant["color"]
        value = node.get(_W + "val")
        theme = node.get(_W + "themeColor")
        tint = node.get(_W + "themeTint")
        shade = node.get(_W + "themeShade")
        if value is None and theme is None:
            raise _DocxStyleError("docx_style_value_invalid")
        color = _ColorSpec(value, theme, tint, shade)
    return _RunDelta(
        bold=_on_off(relevant["b"]) if "b" in relevant else None,
        bold_cs=_on_off(relevant["bCs"]) if "bCs" in relevant else None,
        vanish=_on_off(relevant["vanish"]) if "vanish" in relevant else None,
        highlight_present=highlight_present,
        highlight=highlight,
        color_present=color_present,
        color=color,
    )


def _theme_colors(
    archive: zipfile.ZipFile,
    theme_name: str | None,
) -> dict[str, str]:
    if theme_name is None:
        return {}
    root = _xml_root(archive, theme_name)
    if root.tag != _A + "theme":
        raise _DocxStyleError("docx_xml_malformed")
    schemes = root.findall(".//" + _A + "clrScheme")
    if len(schemes) != 1:
        if not schemes:
            return {}
        raise _DocxStyleError("docx_theme_color_ambiguous")
    scheme = schemes[0]
    if scheme is None:  # pragma: no cover - guarded by len above
        return {}
    result: dict[str, str] = {}
    allowed_slots = {
        "dk1", "lt1", "dk2", "lt2", "accent1", "accent2",
        "accent3", "accent4", "accent5", "accent6", "hlink", "folHlink",
    }
    for slot in scheme:
        local = slot.tag.rsplit("}", 1)[-1]
        if local not in allowed_slots or local in result:
            raise _DocxStyleError("docx_theme_color_ambiguous")
        children = list(slot)
        if len(children) != 1:
            raise _DocxStyleError("docx_theme_color_ambiguous")
        child = children[0]
        if child.tag == _A + "srgbClr":
            value = child.get("val")
        elif child.tag == _A + "sysClr":
            value = child.get("lastClr")
        else:
            raise _DocxStyleError("docx_theme_color_ambiguous")
        if value and re.fullmatch(r"[0-9A-Fa-f]{6}", value):
            result[local] = value.upper()
        else:
            raise _DocxStyleError("docx_theme_color_ambiguous")
    aliases = {
        "text1": "dk1",
        "background1": "lt1",
        "text2": "dk2",
        "background2": "lt2",
        "dark1": "dk1",
        "light1": "lt1",
        "dark2": "dk2",
        "light2": "lt2",
        "hyperlink": "hlink",
        "followedhyperlink": "folHlink",
    }
    for alias, target in aliases.items():
        if target in result:
            result[alias] = result[target]
    return result


def _resolve_color(spec: _ColorSpec | None, theme: Mapping[str, str]) -> tuple[str | None, bool]:
    if spec is None:
        return None, False
    if spec.tint is not None or spec.shade is not None:
        return None, True
    if spec.theme is not None:
        value = theme.get(_normalized(spec.theme))
        return (value, False) if value is not None else (None, True)
    raw = _normalized(spec.value or "")
    if raw == "auto":
        return None, True
    if re.fullmatch(r"[0-9a-f]{8}", raw):
        raw = raw[2:]
    if not re.fullmatch(r"[0-9a-f]{6}", raw):
        return None, True
    return raw.upper(), False


def _apply_delta(
    state: _RunState,
    delta: _RunDelta,
    theme: Mapping[str, str],
    *,
    style_toggle: bool,
) -> _RunState:
    bold = state.bold
    bold_cs = state.bold_cs
    vanish = state.vanish
    if delta.bold is not None:
        if style_toggle:
            if delta.bold:
                bold = not bold
        else:
            bold = delta.bold
    if delta.bold_cs is not None:
        if style_toggle:
            if delta.bold_cs:
                bold_cs = not bold_cs
        else:
            bold_cs = delta.bold_cs
    if delta.vanish is not None:
        if style_toggle:
            if delta.vanish:
                vanish = not vanish
        else:
            vanish = delta.vanish
    highlight = state.highlight
    if delta.highlight_present:
        highlight = delta.highlight
    color_rgb = state.color_rgb
    color_unresolved = state.color_unresolved
    if delta.color_present:
        color_rgb, color_unresolved = _resolve_color(delta.color, theme)
    return _RunState(
        bold=bold,
        bold_cs=bold_cs,
        vanish=vanish,
        highlight=highlight,
        color_rgb=color_rgb,
        color_unresolved=color_unresolved,
    )


def _style_sheet(
    archive: zipfile.ZipFile,
    styles_name: str,
    theme_name: str | None,
) -> _StyleSheet:
    root = _xml_root(archive, styles_name)
    if root.tag != _W + "styles":
        raise _DocxStyleError("docx_xml_malformed")
    defaults_nodes = root.findall("./" + _W + "docDefaults")
    if len(defaults_nodes) > 1:
        raise _DocxStyleError("docx_style_conflict")
    default_rpr: ET.Element | None = None
    if defaults_nodes:
        rpr_defaults = defaults_nodes[0].findall("./" + _W + "rPrDefault")
        if len(rpr_defaults) > 1:
            raise _DocxStyleError("docx_style_conflict")
        if rpr_defaults:
            default_rpr = _single_child(rpr_defaults[0], _W + "rPr")
    styles: dict[str, _StyleDef] = {}
    style_keys: set[str] = set()
    defaults: dict[str, str] = {}
    for node in root.findall("./" + _W + "style"):
        style_id = node.get(_W + "styleId")
        style_type = node.get(_W + "type")
        style_key = _normalized(style_id or "")
        if (
            not style_id
            or not style_type
            or style_id in styles
            or style_key in style_keys
        ):
            raise _DocxStyleError("docx_style_conflict")
        style_keys.add(style_key)
        based = _single_child(node, _W + "basedOn")
        based_on = based.get(_W + "val") if based is not None else None
        if based is not None and not based_on:
            raise _DocxStyleError("docx_style_value_invalid")
        rpr = _single_child(node, _W + "rPr")
        table_effects = False
        if style_type == "table":
            for descendant in node.iter(_W + "rPr"):
                if any(
                    child.tag.rsplit("}", 1)[-1] in _STYLE_PROPERTIES
                    for child in descendant
                ):
                    table_effects = True
                    break
        styles[style_id] = _StyleDef(
            style_id,
            style_type,
            based_on,
            _run_delta(rpr),
            table_effects,
        )
        default_value = node.get(_W + "default")
        if default_value is not None and _on_off_value(default_value):
            if style_type in defaults:
                raise _DocxStyleError("docx_style_conflict")
            defaults[style_type] = style_id
    for style in styles.values():
        seen: set[str] = set()
        current = style
        while current.based_on is not None:
            if current.style_id in seen:
                raise _DocxStyleError("docx_style_cycle")
            seen.add(current.style_id)
            parent = styles.get(current.based_on)
            if (
                parent is None
                and style.style_type == "table"
                and _normalized(current.based_on) == "tablenormal"
            ):
                break
            if parent is None or parent.style_type != style.style_type:
                raise _DocxStyleError("docx_style_reference_invalid")
            current = parent
    return _StyleSheet(
        _run_delta(default_rpr),
        styles,
        defaults,
        _theme_colors(archive, theme_name),
    )


def _style_chain(sheet: _StyleSheet, style_id: str, expected_type: str) -> tuple[_StyleDef, ...]:
    result: list[_StyleDef] = []
    seen: set[str] = set()
    current_id: str | None = style_id
    while current_id is not None:
        if current_id in seen:
            raise _DocxStyleError("docx_style_cycle")
        seen.add(current_id)
        style = sheet.styles.get(current_id)
        if (
            style is None
            and expected_type == "table"
            and _normalized(current_id) == "tablenormal"
        ):
            break
        if style is None or style.style_type != expected_type:
            raise _DocxStyleError("docx_style_reference_invalid")
        result.append(style)
        current_id = style.based_on
    return tuple(reversed(result))


def _style_value(parent: ET.Element | None, tag: str) -> str | None:
    node = _single_child(parent, tag)
    if node is None:
        return None
    value = node.get(_W + "val")
    if not value:
        raise _DocxStyleError("docx_style_value_invalid")
    return value


def _active_table_styles(
    story: _Story,
    sheet: _StyleSheet,
) -> set[str]:
    active: set[str] = set()
    default = sheet.default_by_type.get("table")
    parent_map = {
        child: parent for parent in story.root.iter() for child in parent
    }
    tables: set[ET.Element] = set()
    for paragraph in story.paragraphs:
        current = parent_map.get(paragraph)
        while current is not None:
            if current.tag == _W + "tbl":
                tables.add(current)
                break
            current = parent_map.get(current)
    for table in tables:
        tbl_pr = _single_child(table, _W + "tblPr")
        style_id = _style_value(tbl_pr, _W + "tblStyle") if tbl_pr is not None else None
        if style_id is None:
            style_id = default
        if style_id is not None:
            active.add(style_id)
    return active


def _validate_contextual_styles(
    archive: zipfile.ZipFile,
    stories: Sequence[_Story],
    sheet: _StyleSheet,
    numbering_name: str | None,
) -> None:
    for story in stories:
        for style_id in _active_table_styles(story, sheet):
            if any(
                style.has_table_run_effects
                for style in _style_chain(sheet, style_id, "table")
            ):
                raise _DocxStyleError("docx_table_style_ambiguous")
    has_numbered_paragraph = any(
        paragraph.find("./" + _W + "pPr/" + _W + "numPr") is not None
        for story in stories
        for paragraph in story.paragraphs
    )
    if has_numbered_paragraph and numbering_name is None:
        raise _DocxStyleError("docx_numbering_style_ambiguous")
    if numbering_name is not None:
        numbering = _xml_root(archive, numbering_name)
        if numbering.tag != _W + "numbering":
            raise _DocxStyleError("docx_xml_malformed")
        has_numbering_effect = any(
            child.tag.rsplit("}", 1)[-1] in _STYLE_PROPERTIES
            for rpr in numbering.iter(_W + "rPr")
            for child in rpr
        )
        if has_numbering_effect:
            raise _DocxStyleError("docx_numbering_style_ambiguous")


def _paragraph_state(
    paragraph: ET.Element,
    sheet: _StyleSheet,
) -> tuple[_RunState, ET.Element | None]:
    ppr = _single_child(paragraph, _W + "pPr")
    style_id = _style_value(ppr, _W + "pStyle") if ppr is not None else None
    if style_id is None:
        style_id = sheet.default_by_type.get("paragraph")
    state = _apply_delta(
        _RunState(), sheet.doc_defaults, sheet.theme, style_toggle=False
    )
    if style_id is not None:
        for style in _style_chain(sheet, style_id, "paragraph"):
            state = _apply_delta(state, style.delta, sheet.theme, style_toggle=True)
    return state, ppr


def _effective_runs(
    story: _Story,
    sheet: _StyleSheet,
) -> tuple[_StyledRun, ...]:
    result: list[_StyledRun] = []
    for paragraph_index, paragraph in enumerate(story.paragraphs):
        paragraph_state, _ = _paragraph_state(paragraph, sheet)
        visible_run_index = 0
        for run in _visible_runs(paragraph):
            text = _run_text(run)
            if not text:
                continue
            rpr = _single_child(run, _W + "rPr")
            state = paragraph_state
            style_id = _style_value(rpr, _W + "rStyle") if rpr is not None else None
            if style_id is not None:
                for style in _style_chain(sheet, style_id, "character"):
                    state = _apply_delta(
                        state, style.delta, sheet.theme, style_toggle=True
                    )
            state = _apply_delta(
                state, _run_delta(rpr), sheet.theme, style_toggle=False
            )
            result.append(
                _StyledRun(
                    story.name,
                    paragraph_index,
                    visible_run_index,
                    text,
                    state,
                )
            )
            visible_run_index += 1
            if len(result) > _MAX_STYLED_RUNS:
                raise _DocxStyleError("docx_xml_resource_limit")
    return tuple(result)


def _read_styled_document(path: Path) -> tuple[str, tuple[_StyledRun, ...]]:
    try:
        size = path.stat().st_size
        if not 0 < size <= _MAX_DOCX_BYTES:
            raise _DocxStyleError("docx_source_resource_limit")
        with zipfile.ZipFile(path) as archive:
            _validate_archive(archive)
            graph = _package_graph(archive)
            stories = _stories(archive, graph)
            sheet = _style_sheet(
                archive, graph.styles_name, graph.theme_name
            )
            _validate_contextual_styles(
                archive, stories, sheet, graph.numbering_name
            )
            result: list[_StyledRun] = []
            for story in stories:
                result.extend(_effective_runs(story, sheet))
                if len(result) > _MAX_STYLED_RUNS:
                    raise _DocxStyleError("docx_xml_resource_limit")
            return graph.main_name, tuple(result)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise _DocxStyleError("docx_archive_invalid") from exc


def _read_styled_runs(path: Path) -> tuple[_StyledRun, ...]:
    return _read_styled_document(path)[1]


def _is_red(rgb: str) -> bool:
    red, green, blue = (int(rgb[index : index + 2], 16) / 255 for index in (0, 2, 4))
    hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    hue_degrees = hue * 360
    distance = min(hue_degrees, 360 - hue_degrees)
    return (
        distance <= 15
        and saturation >= 0.5
        and 0.12 <= lightness <= 0.9
        and red >= 0.35
        and red > green * 1.25
        and red > blue * 1.25
    )


def _run_is_effectively_bold(run: _StyledRun) -> bool:
    if run.state.bold == run.state.bold_cs:
        return run.state.bold
    script_values = [
        unicodedata.bidirectional(character)
        for character in run.text
        if character.isalnum()
    ]
    has_complex = any(value in {"R", "AL", "AN"} for value in script_values)
    has_non_complex = any(value not in {"R", "AL", "AN"} for value in script_values)
    if not script_values or (has_complex and has_non_complex):
        raise _DocxStyleError("docx_bold_script_ambiguous")
    return run.state.bold_cs if has_complex else run.state.bold


def _coalesced_matches(
    runs: Sequence[_StyledRun],
    predicate: Any,
) -> tuple[str, ...]:
    matches: list[str] = []
    current_key: tuple[str, int] | None = None
    current_end: int | None = None
    current_text: list[str] = []
    for run in runs:
        selected = bool(predicate(run))
        key = (run.story, run.paragraph)
        contiguous = (
            selected
            and current_key == key
            and current_end is not None
            and run.run == current_end + 1
        )
        if selected and contiguous:
            current_text.append(run.text)
            current_end = run.run
            continue
        if current_text:
            value = "".join(current_text)
            if value:
                matches.append(value)
            current_text = []
            current_key = None
            current_end = None
        if selected:
            current_key = key
            current_end = run.run
            current_text = [run.text]
    if current_text:
        value = "".join(current_text)
        if value:
            matches.append(value)
    return tuple(matches)


def _decision(
    answer: str,
    path: Path,
    root: Path,
    operations: int,
) -> StructuredCandidateDecision:
    relative = unicodedata.normalize("NFC", path.relative_to(root).as_posix())
    return StructuredCandidateDecision(
        "resolved",
        "certified_docx_native_style",
        StructuredCandidateAnswer(
            answer=answer,
            source_paths=(relative,),
            source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            operation_count=operations,
            output_count=1,
        ),
    )


def _hold(reason: str) -> StructuredCandidateDecision:
    return StructuredCandidateDecision("hold", reason)


def _highlight_and_font(
    engine: Any, match: re.Match[str]
) -> StructuredCandidateDecision:
    root = _safe_root(engine)
    if root is None:
        return _hold("source_root_invalid")
    try:
        path = _middle_report_source(engine, match["location"])
        runs = _read_styled_runs(path)
        if any(
            run.state.highlight == "yellow" and run.state.color_unresolved
            for run in runs
            if not run.state.vanish
        ):
            return _hold("docx_font_color_unresolved")
        values = _coalesced_matches(
            runs,
            lambda run: (
                not run.state.vanish
                and run.state.highlight == "yellow"
                and run.state.color_rgb is not None
                and _is_red(run.state.color_rgb)
            ),
        )
        if len(values) != 1:
            return _hold("docx_style_match_not_unique")
        return _decision(values[0], path, root, 9)
    except _DocxStyleError as exc:
        return _hold(str(exc))
    except OSError:
        return _hold("docx_source_read_error")


def _effective_bold(
    engine: Any, match: re.Match[str]
) -> StructuredCandidateDecision:
    root = _safe_root(engine)
    if root is None:
        return _hold("source_root_invalid")
    try:
        path = _contract_source(engine, match["location"])
        runs = _read_styled_runs(path)
        values = _coalesced_matches(
            runs,
            lambda run: not run.state.vanish and _run_is_effectively_bold(run),
        )
        if len(values) != 1:
            return _hold("docx_style_match_not_unique")
        return _decision(values[0], path, root, 8)
    except _DocxStyleError as exc:
        return _hold(str(exc))
    except OSError:
        return _hold("docx_source_read_error")


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    """Execute a supported style contract without consulting answer history."""

    if not isinstance(question, str):
        return None
    match = HIGHLIGHT_AND_FONT.fullmatch(question)
    if match:
        if graph_contract_for_question(question) is None:
            return None
        return _highlight_and_font(engine, match)
    match = EFFECTIVE_BOLD.fullmatch(question)
    if match:
        return _effective_bold(engine, match)
    return None


__all__ = [
    "DOCX_NATIVE_STYLE_RULE_VERSION",
    "decide_question",
    "graph_contract_for_question",
    "validate_graph_contract",
]
