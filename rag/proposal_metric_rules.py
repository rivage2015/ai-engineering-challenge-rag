"""Fail-closed extraction of an explicitly prioritised proposal metric.

The rule is intentionally narrow.  It matches one complete question grammar,
selects one current proposal presentation in the requested project, and reads
only visible text runs from active PPTX slides.  A metric is returned only when
exactly one run contains both a metric token and an explicit decorated priority
marker.  Narrative uses of words such as ``重視`` are not priority markers.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from structured_candidate import (
    StructuredCandidateAnswer,
    StructuredCandidateDecision,
    _candidate_values,
    _location_matches,
    _normalized,
)


PROPOSAL_METRIC_RULE_VERSION = "0.1"

PROPOSAL_PRIORITY_METRIC = re.compile(
    r"^(?P<location>[^\r\n]+?)の提案書内で、"
    r"重視するとされている評価指標を"
    r"(?:答えて|教えて)ください。?$"
)

_PML_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_DML_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_SLIDE_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
)
_P = "{" + _PML_NS + "}"
_A = "{" + _DML_NS + "}"
_R = "{" + _REL_NS + "}"
_PR = "{" + _PKG_REL_NS + "}"

_MAX_PPTX_BYTES = 256 * 1024 * 1024
_MAX_XML_MEMBER_BYTES = 32 * 1024 * 1024
_MAX_TOTAL_XML_BYTES = 512 * 1024 * 1024
_XML_FORBIDDEN = (b"<!DOCTYPE", b"<!ENTITY")

_ARCHIVE_COMPONENT = re.compile(
    r"(?:^|[._\-\s])(?:old|archive|archived|backup|bak|copy)(?:$|[._\-\s])",
    flags=re.IGNORECASE,
)
_ARCHIVE_JAPANESE = ("旧", "過去", "バックアップ", "アーカイブ")

# Plain "重視" is deliberately excluded: proposals commonly use it in prose.
# A decorative symbol plus an explicit priority word is an author-supplied tag.
_PRIORITY_MARKER = re.compile(
    r"[★☆◎◆◇●]"
    r"\s*(?:最重要|最?重視|重点|優先(?:評価)?(?:指標)?)"
    r"(?![A-Za-z0-9一-鿿ぁ-ゟァ-ヺー])",
    flags=re.IGNORECASE,
)
_METRIC_LABEL = re.compile(r"^(?:評価指標|指標|KPI)\s*[:：]\s*", re.IGNORECASE)
_METRIC_CHARS = re.compile(
    r"[A-Za-z0-9一-鿿ァ-ヺー々〆ヵヶ%％²³^_+.−ー\- ]+"
)
_EMPTY_BRACKETS = re.compile(r"(?:\(\s*\)|（\s*）|\[\s*\]|【\s*】)")
_TRIM_PUNCTUATION = " \t\r\n:：;；,、.。()（）[]【】<>＜＞-－—―・"
_SHAPE_TAGS = frozenset(
    {
        _P + "sp",
        _P + "graphicFrame",
        _P + "grpSp",
        _P + "cxnSp",
        _P + "pic",
    }
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    """Compile the complete proposal-priority question into a typed graph."""

    if not isinstance(question, str):
        return None
    match = PROPOSAL_PRIORITY_METRIC.fullmatch(question)
    if match is None:
        return None
    bindings = {"location": match["location"]}
    operators = (
        "retrieve",
        "select_authoritative",
        "parse_visible_text_runs",
        "filter_explicit_priority_marker",
        "verify_unique",
        "project_metric_token",
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
        "graph_rule_version": PROPOSAL_METRIC_RULE_VERSION,
        "rule_id": "proposal_explicit_priority_metric",
        "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
        "bindings": bindings,
        "scope": {
            "location": bindings["location"],
            "container": "proposal/direct/*.pptx",
            "document_kind": "提案書",
            "version_state": "current_unique",
            "excluded_version_states": ["old", "archive", "backup", "copy"],
            "evidence_channel": "active_slide_visible_text_run",
            "priority_channel": "decorated_explicit_marker_same_run",
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
        "graph_contract_id": "proposal_metric_"
        + hashlib.sha256(_canonical_json(core).encode("utf-8")).hexdigest()[:32],
        **core,
    }


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    """Recompile the contract so caller-supplied graph claims are not trusted."""

    if not isinstance(contract, Mapping):
        return False
    expected = graph_contract_for_question(question)
    if expected is None:
        return False
    try:
        return _canonical_json(expected) == _canonical_json(contract)
    except (TypeError, ValueError):
        return False


def _is_archived_component(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    appended_suffix = re.search(
        r"(?:^|[^a-z0-9])"
        r"(?:old|archive|archived|backup|bak|copy)(?:[._\-\s]*[0-9]+)?$",
        normalized,
    )
    return (
        bool(_ARCHIVE_COMPONENT.search(normalized))
        or appended_suffix is not None
        or any(marker in normalized for marker in _ARCHIVE_JAPANESE)
    )


def _proposal_paths(engine: Any, location: str) -> tuple[Path, ...]:
    root = Path(engine.source_root).resolve()
    if not root.is_dir() or root.is_symlink():
        return ()
    candidates = _candidate_values(location, engine.glossary)
    matches: list[Path] = []
    for path in root.rglob("*"):
        if (
            not path.is_file()
            or path.is_symlink()
            or path.name.startswith("~$")
            or path.suffix.casefold() != ".pptx"
        ):
            continue
        relative = path.relative_to(root)
        if not _location_matches(relative.parts[:-1], candidates):
            continue
        # Current proposals must be direct children of the proposal folder.
        # A nested old/archive directory therefore cannot be silently selected.
        if "提案" not in _normalized(path.parent.name):
            continue
        if "提案" not in _normalized(path.stem):
            continue
        # Do not inspect the project/client name for version words: those are
        # valid scope values.  Version state belongs to the proposal folder
        # and document name only.
        if _is_archived_component(path.parent.name) or _is_archived_component(
            path.stem
        ):
            continue
        matches.append(path)
    return tuple(
        sorted(matches, key=lambda item: unicodedata.normalize("NFC", item.as_posix()))
    )


def _safe_xml(data: bytes) -> ET.Element | None:
    if len(data) > _MAX_XML_MEMBER_BYTES:
        return None
    upper = data[:4096].upper()
    if any(token in upper for token in _XML_FORBIDDEN):
        return None
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        return None


def _zip_records(path: Path) -> tuple[zipfile.ZipFile, dict[str, zipfile.ZipInfo]] | None:
    if path.stat().st_size > _MAX_PPTX_BYTES:
        return None
    archive = zipfile.ZipFile(path)
    infos = archive.infolist()
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        archive.close()
        return None
    if sum(info.file_size for info in infos) > _MAX_TOTAL_XML_BYTES:
        archive.close()
        return None
    records: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        pure = PurePosixPath(info.filename)
        if pure.is_absolute() or ".." in pure.parts or "\\" in info.filename:
            archive.close()
            return None
        records[info.filename] = info
    return archive, records


def _active_slide_names(
    archive: zipfile.ZipFile,
    records: Mapping[str, zipfile.ZipInfo],
) -> tuple[str, ...] | None:
    required = ("ppt/presentation.xml", "ppt/_rels/presentation.xml.rels")
    if any(name not in records for name in required):
        return None
    presentation = _safe_xml(archive.read(required[0]))
    relationships = _safe_xml(archive.read(required[1]))
    if presentation is None or relationships is None:
        return None
    targets: dict[str, str] = {}
    seen_relationship_ids: set[str] = set()
    for relationship in relationships.findall(_PR + "Relationship"):
        relationship_id = relationship.get("Id")
        target = relationship.get("Target")
        if not relationship_id or relationship_id in seen_relationship_ids:
            return None
        seen_relationship_ids.add(relationship_id)
        if relationship.get("Type") != _SLIDE_REL_TYPE:
            continue
        if not target or relationship.get("TargetMode") == "External":
            return None
        normalized = posixpath.normpath(posixpath.join("ppt", target))
        pure = PurePosixPath(normalized)
        if (
            pure.is_absolute()
            or ".." in pure.parts
            or not re.fullmatch(r"ppt/slides/slide[1-9][0-9]*\.xml", normalized)
            or normalized not in records
            or relationship_id in targets
        ):
            return None
        targets[relationship_id] = normalized
    slide_list = presentation.find(_P + "sldIdLst")
    if slide_list is None:
        return None
    names: list[str] = []
    for slide_id in slide_list.findall(_P + "sldId"):
        relationship_id = slide_id.get(_R + "id")
        if not relationship_id or relationship_id not in targets:
            return None
        # show="0" is PowerPoint's hidden-slide flag.
        if slide_id.get("show", "1") in {"0", "false", "False"}:
            continue
        names.append(targets[relationship_id])
    return tuple(names) if names and len(names) == len(set(names)) else None


def _shape_hidden(element: ET.Element) -> bool:
    for child in element:
        if not child.tag.startswith(_P + "nv"):
            continue
        for property_node in child:
            if property_node.tag == _P + "cNvPr":
                return property_node.get("hidden", "0") in {"1", "true", "True"}
    return False


def _visible_runs(element: ET.Element, hidden: bool = False) -> list[str]:
    if element.tag in _SHAPE_TAGS:
        hidden = hidden or _shape_hidden(element)
    if hidden:
        return []
    if element.tag == _A + "r":
        text = "".join(node.text or "" for node in element.iter(_A + "t"))
        return [text] if text.strip() else []
    runs: list[str] = []
    for child in element:
        runs.extend(_visible_runs(child, hidden))
    return runs


def _pptx_visible_text_runs(path: Path) -> tuple[str, ...] | None:
    try:
        opened = _zip_records(path)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return None
    if opened is None:
        return None
    archive, records = opened
    try:
        slide_names = _active_slide_names(archive, records)
        if slide_names is None:
            return None
        runs: list[str] = []
        for slide_name in slide_names:
            info = records[slide_name]
            if info.file_size > _MAX_XML_MEMBER_BYTES:
                return None
            root = _safe_xml(archive.read(info))
            if root is None or root.tag != _P + "sld":
                return None
            if root.get("show", "1") in {"0", "false", "False"}:
                continue
            runs.extend(_visible_runs(root))
        return tuple(runs)
    except (OSError, KeyError, RuntimeError, zipfile.BadZipFile):
        return None
    finally:
        archive.close()


def _metric_from_marked_run(value: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", value)
    markers = tuple(_PRIORITY_MARKER.finditer(normalized))
    if len(markers) != 1:
        return None
    without_marker = (
        normalized[: markers[0].start()] + normalized[markers[0].end() :]
    )
    without_marker = _EMPTY_BRACKETS.sub("", without_marker)
    candidate = re.sub(r"\s+", " ", without_marker).strip(_TRIM_PUNCTUATION)
    candidate = _METRIC_LABEL.sub("", candidate).strip(_TRIM_PUNCTUATION)
    if not candidate or len(candidate) > 80:
        return None
    if (
        _METRIC_CHARS.fullmatch(candidate) is None
        or re.search(r"[ぁ-ゟ]", candidate)
        or re.search(r"\s", candidate)
        or "/" in candidate
        or "／" in candidate
        or "評価指標" in candidate
    ):
        return None
    return candidate


def _decision(
    answer: str,
    path: Path,
    root: Path,
) -> StructuredCandidateDecision:
    source_bytes = path.read_bytes()
    relative = unicodedata.normalize("NFC", path.relative_to(root).as_posix())
    return StructuredCandidateDecision(
        "resolved",
        "certified_proposal_priority_metric",
        StructuredCandidateAnswer(
            answer=answer,
            source_paths=(relative,),
            source_sha256=hashlib.sha256(source_bytes).hexdigest(),
            operation_count=6,
            output_count=1,
        ),
    )


def decide_extended(
    engine: Any,
    question_id: str,
    question: str,
) -> StructuredCandidateDecision | None:
    """Resolve the metric, hold on ambiguity, or return ``None`` if unsupported."""

    del question_id
    if not isinstance(question, str):
        return None
    match = PROPOSAL_PRIORITY_METRIC.fullmatch(question)
    if match is None:
        return None
    paths = _proposal_paths(engine, match["location"])
    if len(paths) != 1:
        return StructuredCandidateDecision("hold", "proposal_source_not_unique")
    path = paths[0]
    runs = _pptx_visible_text_runs(path)
    if runs is None:
        return StructuredCandidateDecision("hold", "proposal_source_invalid")
    marked_runs = [
        run
        for run in runs
        if _PRIORITY_MARKER.search(unicodedata.normalize("NFKC", run))
    ]
    if len(marked_runs) != 1:
        return StructuredCandidateDecision("hold", "priority_marker_not_unique")
    metric = _metric_from_marked_run(marked_runs[0])
    if metric is None:
        return StructuredCandidateDecision("hold", "priority_metric_ambiguous")
    return _decision(metric, path, Path(engine.source_root).resolve())


__all__ = [
    "PROPOSAL_METRIC_RULE_VERSION",
    "PROPOSAL_PRIORITY_METRIC",
    "decide_extended",
    "graph_contract_for_question",
    "validate_graph_contract",
]
