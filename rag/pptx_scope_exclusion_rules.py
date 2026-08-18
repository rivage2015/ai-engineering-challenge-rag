"""Count explicitly marked out-of-scope items in native PPTX speaker notes."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import zipfile
from pathlib import Path
from typing import Any, Mapping
from xml.etree import ElementTree as ET

from cross_document_finance_rules import _fingerprint
from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision

VERSION = "0.1"
QUESTION = re.compile(r"^(?P<project>.+?)の提案書において、スコープ対象外としている項目はいくつありますか。$")
_A = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _compact(value: object) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", str(value)).casefold() if not c.isspace())


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    match = QUESTION.fullmatch(question) if isinstance(question, str) else None
    if match is None:
        return None
    operators = (
        "bind_unique_project_proposal_pptx",
        "validate_opc_package_and_notes_parts",
        "extract_speaker_note_paragraphs_separately_from_slide_text",
        "locate_unique_scope_exclusion_heading",
        "select_explicit_cross_marked_items_in_same_note_body",
        "reject_unmarked_slide_scope_and_preprocessing_exclusions",
        "verify_unique_nonempty_item_texts",
        "count_scope_exclusion_items",
    )
    nodes, previous = [], "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
        previous = output
    core = {
        "graph_rule_version": VERSION,
        "rule_id": "pptx_speaker_notes_scope_exclusion_count",
        "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
        "bindings": match.groupdict(),
        "scope": {"source_channel": "native_pptx_speaker_notes", "question_independent": True, "ambiguity_policy": "hold"},
        "operation_graph": {"external_inputs": [{"input_ref": "input_question", "input_type": "pptx_package", "source": "question_scope"}], "nodes": nodes, "edges": [{"from": nodes[i - 1]["output_ref"], "to": nodes[i]["operation_id"]} for i in range(1, len(nodes))]},
        "requested_output": {"source_operation_ref": nodes[-1]["operation_id"], "cardinality": "single", "answer_shape": {"container": "scalar", "value_type": "integer", "unit": None}, "display_precision": None, "required_keys": None},
    }
    return {"graph_contract_id": "pptx_scope_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and _canonical(expected) == _canonical(contract)


def _source(engine: Any, project: str) -> tuple[Path, Path] | None:
    root = Path(engine.source_root).resolve()
    matches = [
        path for path in (root / "プロジェクト").rglob("*.pptx")
        if path.is_file() and not path.is_symlink() and not path.name.startswith("~$")
        and _compact(project) in _compact(path.relative_to(root).as_posix())
        and _compact("00.提案") in _compact(path.relative_to(root).as_posix())
        and _compact("提案書") in _compact(path.stem)
    ]
    return (root, matches[0]) if len(matches) == 1 else None


def _scope_exclusions(path: Path) -> tuple[str, ...]:
    if path.stat().st_size > 64 * 1024 * 1024 or not zipfile.is_zipfile(path):
        raise ValueError("invalid PPTX")
    note_groups = []
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        note_names = sorted(name for name in names if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", name))
        if not note_names or len(note_names) != len(set(note_names)):
            raise ValueError("notes parts unavailable")
        for name in note_names:
            raw = archive.read(name)
            if len(raw) > 8 * 1024 * 1024 or b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
                raise ValueError("unsafe notes XML")
            root = ET.fromstring(raw)
            paragraphs = [
                "".join(node.text or "" for node in paragraph.findall(".//a:t", _A)).strip()
                for paragraph in root.findall(".//a:p", _A)
            ]
            headings = [index for index, value in enumerate(paragraphs) if _compact(value) == _compact("スコープ対象外")]
            if not headings:
                continue
            if len(headings) != 1:
                raise ValueError("scope heading not unique in notes")
            items = tuple(re.sub(r"^[✖✕×xX]\s*", "", value).strip() for value in paragraphs[headings[0] + 1 :] if re.match(r"^[✖✕×xX]\s*\S", value))
            note_groups.append(items)
    if len(note_groups) != 1 or not note_groups[0] or len(note_groups[0]) != len(set(note_groups[0])):
        raise ValueError("scope exclusions not unique")
    return note_groups[0]


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    try:
        match = QUESTION.fullmatch(question)
        bound = _source(engine, match.group("project"))
        if bound is None:
            raise ValueError("proposal source not unique")
        root, source = bound
        items = _scope_exclusions(source)
        paths, digest = _fingerprint((source,), root)
        return StructuredCandidateDecision("resolved", "certified_pptx_speaker_notes_scope_count", StructuredCandidateAnswer(str(len(items)), paths, digest, len(contract["operation_graph"]["nodes"]), 1))
    except (ET.ParseError, OSError, RuntimeError, TypeError, UnicodeError, ValueError, zipfile.BadZipFile):
        return StructuredCandidateDecision("hold", "pptx_scope_exclusions_not_certified")


__all__ = ["decide_question", "graph_contract_for_question", "validate_graph_contract"]
