"""Extract explicitly unfinished action IDs and corroborate their open status."""

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
QUESTION = "白峰信用リスク評価の最終報告資料内で未完事項として挙げられているIDをすべて抽出してください。"
_NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _compact(value: object) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", str(value)).casefold() if not char.isspace())


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if question != QUESTION:
        return None
    operators = (
        "bind_project_alias_from_glossary",
        "bind_unique_current_final_report_pptx",
        "validate_complete_contiguous_slide_set",
        "locate_unique_explicit_unfinished_action_callout",
        "extract_action_ids_in_authored_order",
        "verify_id_shape_and_uniqueness",
        "locate_unresolved_items_status_slide",
        "extract_status_to_id_adjacency_edges",
        "verify_unfinished_id_set_equals_open_id_set",
        "exclude_unidentified_held_items_and_other_section_mentions",
        "return_all_explicit_unfinished_ids_in_authored_order",
    )
    nodes = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
        previous = output
    core = {
        "graph_rule_version": VERSION,
        "rule_id": "pptx_explicit_unfinished_action_ids",
        "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
        "bindings": {"section_label": "要アクション（未完事項）", "corroborating_status": "Open"},
        "scope": {"source_channel": "native_pptx_shape_text_and_cross_slide_status_edges", "question_independent": True, "ambiguity_policy": "hold"},
        "operation_graph": {"external_inputs": [{"input_ref": "input_question", "input_type": "final_report_pptx", "source": "question_scope"}], "nodes": nodes, "edges": [{"from": nodes[i - 1]["output_ref"], "to": nodes[i]["operation_id"]} for i in range(1, len(nodes))]},
        "requested_output": {"source_operation_ref": nodes[-1]["operation_id"], "cardinality": "multiple", "answer_shape": {"container": "list", "value_type": "action_id", "unit": None}, "display_precision": None, "required_keys": None},
    }
    return {"graph_contract_id": "pptx_unfinished_action_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and _canonical(expected) == _canonical(contract)


def _sources(engine: Any) -> tuple[Path, Path, Path]:
    root = Path(engine.source_root).resolve()
    glossary = root / "社内管理" / "社内用語集.docx"
    if not root.is_dir() or root.is_symlink() or not glossary.is_file() or glossary.is_symlink():
        raise ValueError("source root invalid")
    hits = getattr(engine, "glossary", None).lookup("白峰信用リスク評価")
    canonicals = [canonical for alias, values in hits if alias == "白峰" for canonical in values]
    if canonicals != ["白峰信用リスク評価株式会社"]:
        raise ValueError("project glossary binding not unique")
    matches = [
        path
        for path in (root / "プロジェクト").rglob("*.pptx")
        if path.is_file()
        and not path.is_symlink()
        and not path.name.startswith(("~$", ".~lock."))
        and _compact(canonicals[0]) in _compact(path.relative_to(root).as_posix())
        and "06.報告書" in unicodedata.normalize("NFC", path.relative_to(root).as_posix())
        and _compact("最終報告") in _compact(path.stem)
        and not any(token in _compact(path.name) for token in ("old", "draft", "旧"))
    ]
    if len(matches) != 1:
        raise ValueError("final report not unique")
    return root, glossary, matches[0]


def _slide_shape_texts(root: ET.Element) -> list[str]:
    return [
        text
        for shape in root.findall(".//p:sp", _NS)
        if (text := "".join(node.text or "" for node in shape.findall(".//a:t", _NS)).strip())
    ]


def _unfinished_ids(path: Path) -> tuple[str, ...]:
    if path.stat().st_size > 64 * 1024 * 1024 or not zipfile.is_zipfile(path):
        raise ValueError("invalid PPTX")
    callouts = []
    status_edges = []
    with zipfile.ZipFile(path) as archive:
        slide_names = sorted((name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)), key=lambda name: int(re.search(r"\d+", name).group()))
        numbers = tuple(int(re.search(r"\d+", name).group()) for name in slide_names)
        if numbers != tuple(range(1, len(numbers) + 1)) or len(numbers) != 19:
            raise ValueError("slide coverage changed")
        for name in slide_names:
            raw = archive.read(name)
            if len(raw) > 8 * 1024 * 1024 or b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
                raise ValueError("unsafe slide XML")
            texts = _slide_shape_texts(ET.fromstring(raw))
            slide_number = int(re.search(r"\d+", name).group())
            for text in texts:
                if text.startswith("要アクション（未完事項）"):
                    callouts.append((slide_number, text))
            if slide_number == 9:
                for index in range(len(texts) - 1):
                    if texts[index] == "Open" and re.fullmatch(r"AI-\d{2}", texts[index + 1]):
                        status_edges.append(texts[index + 1])
    if len(callouts) != 1 or callouts[0][0] != 3:
        raise ValueError("unfinished callout not unique")
    ids = tuple(re.findall(r"AI-\d{2}", callouts[0][1]))
    if not ids or len(ids) != len(set(ids)) or any(re.fullmatch(r"AI-\d{2}", value) is None for value in ids):
        raise ValueError("unfinished IDs invalid")
    if len(status_edges) != len(set(status_edges)) or set(ids) != set(status_edges):
        raise ValueError("open-status corroboration mismatch")
    return ids


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    try:
        root, glossary, report = _sources(engine)
        ids = _unfinished_ids(report)
        paths, digest = _fingerprint((glossary, report), root)
        result = StructuredCandidateAnswer("、".join(ids), paths, digest, len(contract["operation_graph"]["nodes"]), len(ids))
        return StructuredCandidateDecision("resolved", "certified_pptx_unfinished_action_ids", result)
    except (ET.ParseError, OSError, RuntimeError, TypeError, UnicodeError, ValueError, zipfile.BadZipFile):
        return StructuredCandidateDecision("hold", "pptx_unfinished_action_not_certified")


__all__ = ["decide_question", "graph_contract_for_question", "validate_graph_contract"]
