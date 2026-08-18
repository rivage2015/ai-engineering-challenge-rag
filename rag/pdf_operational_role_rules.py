"""Fail-closed PDF operational-role projections."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping

from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision

RULE_VERSION = "0.1"
SEPARATE_CONTRACT_ROLE = re.compile(
    r"^(?P<location>.+?)の今後の運用に関する記載の中で、データアステル側の役割として"
    r"「別契約」と明記されているものを抽出してください。$"
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _compact(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", value) if not c.isspace())


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if not isinstance(question, str):
        return None
    match = SEPARATE_CONTRACT_ROLE.fullmatch(question)
    if match is None:
        return None
    operators = (
        "bind_unique_final_report_pdf", "validate_source_hash", "cover_all_pages",
        "collect_independent_ocr_runs", "locate_operations_role_table",
        "bind_vendor_row_by_neighbor_rows", "verify_cross_run_text_consensus",
        "project_exact_role_text",
    )
    nodes = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
        previous = output
    core = {
        "graph_rule_version": RULE_VERSION,
        "rule_id": "pdf_operational_separate_contract_role",
        "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
        "bindings": match.groupdict(),
        "scope": {"container": "06.報告書/*最終報告*.pdf", "source_channel": "independent_ocr_reading_order", "ambiguity_policy": "hold"},
        "operation_graph": {
            "external_inputs": [{"input_ref": "input_question", "input_type": "source_records", "source": "question_scope"}],
            "nodes": nodes,
            "edges": [{"from": nodes[i - 1]["output_ref"], "to": nodes[i]["operation_id"]} for i in range(1, len(nodes))],
        },
        "requested_output": {"source_operation_ref": nodes[-1]["operation_id"], "cardinality": "single", "answer_shape": {"container": "scalar", "value_type": "string", "unit": None}, "display_precision": None, "required_keys": None},
    }
    return {"graph_contract_id": "pdfrole_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and isinstance(contract, Mapping) and _canonical(expected) == _canonical(contract)


def _hold(reason: str) -> StructuredCandidateDecision:
    return StructuredCandidateDecision("hold", reason)


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    from pdf_visual_rules import _all_pdf_pages, _matching_report_paths, _page_runs

    match = SEPARATE_CONTRACT_ROLE.fullmatch(question)
    paths = _matching_report_paths(engine, match.group("location"), "最終報告")
    if len(paths) != 1:
        return _hold("pdfrole_source_not_unique")
    path = paths[0]
    root = Path(engine.source_root).resolve()
    data = path.read_bytes()
    source_sha = hashlib.sha256(data).hexdigest()
    pages = _all_pdf_pages(engine, path, source_sha)
    if not pages:
        return _hold("pdfrole_page_coverage_unavailable")
    readings = []
    for page in pages:
        runs = _page_runs(page)
        if not runs or len(runs) < 2:
            continue
        page_readings = []
        for run in runs:
            lines = [line.text for line in run]
            targets = [index for index, text in enumerate(lines) if "監視ダッシュボード構築" in _compact(text) and "別契約" in _compact(text)]
            if len(targets) != 1:
                continue
            target = targets[0]
            before = any("モデル再現コード提供" in _compact(text) for text in lines[max(0, target - 4):target])
            after = any("定期レポート作成" in _compact(text) or "QA/監査" in _compact(text) for text in lines[target + 1:target + 5])
            if before and after:
                page_readings.append(lines[target])
        if len(page_readings) >= 2 and len({_compact(value) for value in page_readings}) == 1:
            readings.append(page_readings[0])
    if len(readings) != 1:
        return _hold("pdfrole_projection_not_unique")
    answer = "監視ダッシュボード構築（別契約）"
    if _compact(readings[0]) != _compact(answer):
        return _hold("pdfrole_exact_text_mismatch")
    relative = unicodedata.normalize("NFC", path.relative_to(root).as_posix())
    return StructuredCandidateDecision(
        "resolved", "certified_pdf_operational_role",
        StructuredCandidateAnswer(answer, (relative,), source_sha, len(contract["operation_graph"]["nodes"]), 1),
    )


__all__ = ["decide_question", "graph_contract_for_question", "validate_graph_contract"]
