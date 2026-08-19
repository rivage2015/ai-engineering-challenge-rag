"""Certify KPI status questions from a complete native PPTX table."""

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
QUESTION = "青葉バイオメディカル機器の最終報告において、設定されたKPIとして未達成とされている項目を挙げてください。"
_NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main", "p": "http://schemas.openxmlformats.org/presentationml/2006/main"}


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
        "validate_opc_package_and_complete_slide_set",
        "locate_unique_kpi_status_table",
        "verify_kpi_table_header",
        "extract_every_kpi_row_and_evaluation",
        "verify_declared_six_row_cardinality",
        "crosscheck_all_rows_achieved_summary_text",
        "filter_rows_evaluated_unachieved",
        "project_explicit_empty_result",
    )
    nodes = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
        previous = output
    core = {
        "graph_rule_version": VERSION,
        "rule_id": "pptx_complete_kpi_table_unachieved_items",
        "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
        "bindings": {},
        "scope": {"source_channel": "glossary_and_native_pptx_table", "question_independent": True, "ambiguity_policy": "hold"},
        "operation_graph": {"external_inputs": [{"input_ref": "input_question", "input_type": "glossary_and_pptx", "source": "question_scope"}], "nodes": nodes, "edges": [{"from": nodes[i - 1]["output_ref"], "to": nodes[i]["operation_id"]} for i in range(1, len(nodes))]},
        "requested_output": {"source_operation_ref": nodes[-1]["operation_id"], "cardinality": "multiple", "answer_shape": {"container": "list", "value_type": "string", "unit": None}, "display_precision": None, "required_keys": None},
    }
    return {"graph_contract_id": "pptx_kpi_status_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and _canonical(expected) == _canonical(contract)


def _sources(engine: Any) -> tuple[Path, Path, Path]:
    root = Path(engine.source_root).resolve()
    if not root.is_dir() or root.is_symlink():
        raise ValueError("source root invalid")
    glossary = root / "社内管理" / "社内用語集.docx"
    if not glossary.is_file() or glossary.is_symlink():
        raise ValueError("glossary invalid")
    hits = getattr(engine, "glossary", None).lookup("青葉バイオメディカル機器")
    canonicals = [canonical for alias, values in hits if alias == "青葉バイオ" for canonical in values]
    if canonicals != ["株式会社青葉バイオメディカル機器"]:
        raise ValueError("project glossary binding not unique")
    matches = [
        path for path in (root / "プロジェクト").rglob("*.pptx")
        if path.is_file() and not path.is_symlink() and not path.name.startswith(("~$", ".~lock."))
        and _compact(canonicals[0]) in _compact(path.relative_to(root).as_posix())
        and "06.報告書" in unicodedata.normalize("NFC", path.relative_to(root).as_posix())
        and _compact("最終報告") in _compact(path.stem)
        and not any(token in _compact(path.name) for token in ("old", "draft", "旧"))
    ]
    if len(matches) != 1:
        raise ValueError("final report not unique")
    return root, glossary, matches[0]


def _kpi_rows(path: Path) -> tuple[tuple[str, str], ...]:
    if path.stat().st_size > 64 * 1024 * 1024 or not zipfile.is_zipfile(path):
        raise ValueError("invalid PPTX")
    candidates = []
    summary_counts = []
    slide_count = 0
    with zipfile.ZipFile(path) as archive:
        slide_names = sorted((name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)), key=lambda name: int(re.search(r"\d+", name).group()))
        if len(slide_names) != 21:
            raise ValueError("slide coverage changed")
        for name in slide_names:
            raw = archive.read(name)
            if len(raw) > 8 * 1024 * 1024 or b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
                raise ValueError("unsafe slide XML")
            root = ET.fromstring(raw)
            slide_count += 1
            all_text = "".join(node.text or "" for node in root.findall(".//a:t", _NS))
            if _compact("全6項目のKPIにおいて「達成」と評価") in _compact(all_text):
                summary_counts.append(int(re.search(r"\d+", name).group()))
            for table in root.findall(".//a:tbl", _NS):
                matrix = [["".join(node.text or "" for node in cell.findall(".//a:t", _NS)).strip() for cell in row.findall("./a:tc", _NS)] for row in table.findall("./a:tr", _NS)]
                if matrix and matrix[0] == ["KPI分類", "判定基準", "結果", "評価"]:
                    candidates.append((int(re.search(r"\d+", name).group()), matrix[1:]))
    if slide_count != 21 or len(candidates) != 1 or summary_counts != [10]:
        raise ValueError("KPI table or summary not unique")
    slide, rows = candidates[0]
    if slide != 10 or len(rows) != 6 or any(len(row) != 4 or any(not value for value in row) for row in rows):
        raise ValueError("KPI table shape invalid")
    labels = [row[0] for row in rows]
    if len(set(labels)) != 6:
        raise ValueError("KPI labels not unique")
    return tuple((row[0], row[3]) for row in rows)


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    try:
        root, glossary, report = _sources(engine)
        rows = _kpi_rows(report)
        unachieved = [label for label, evaluation in rows if _compact(evaluation) != _compact("達成")]
        answer = "該当するものはありません。" if not unachieved else "、".join(unachieved)
        paths, digest = _fingerprint((glossary, report), root)
        result = StructuredCandidateAnswer(answer, paths, digest, len(contract["operation_graph"]["nodes"]), len(unachieved))
        return StructuredCandidateDecision("resolved", "certified_pptx_complete_kpi_status_table", result)
    except (ET.ParseError, OSError, RuntimeError, TypeError, UnicodeError, ValueError, zipfile.BadZipFile):
        return StructuredCandidateDecision("hold", "pptx_kpi_status_not_certified")


__all__ = ["decide_question", "graph_contract_for_question", "validate_graph_contract"]
