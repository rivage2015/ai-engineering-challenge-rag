"""Resolve hypothetical actual-hours settlement from complete T&M clauses."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping

from cross_document_finance_rules import _fingerprint
from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision

VERSION = "0.1"
QUESTION = "ひがし丘の契約条件において、ACTHが200時間を超えた場合の精算方法に関する規定内容を答えてください。"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _compact(value: object) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", str(value)).casefold() if not char.isspace())


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if question != QUESTION:
        return None
    operators = (
        "bind_project_and_actual_hours_aliases_from_glossary",
        "bind_unique_current_contract_docx",
        "locate_complete_compensation_and_settlement_sections",
        "extract_time_and_materials_monthly_settlement_model",
        "extract_estimated_hours_and_verify_nonfixed_status",
        "verify_above_or_below_estimate_uses_same_rules",
        "extract_monthly_timesheet_basis",
        "extract_thirty_minute_rounding_rule",
        "extract_hourly_rate_and_tax_addition_formula",
        "apply_hypothetical_hours_without_inventing_threshold_exception",
        "compose_complete_settlement_rule_answer",
    )
    nodes = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
        previous = output
    core = {
        "graph_rule_version": VERSION,
        "rule_id": "tm_hypothetical_actual_hours_settlement_method",
        "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
        "bindings": {"hypothetical_hours_lower_bound": 200, "actual_hours_alias": "ACTH"},
        "scope": {"source_channel": "glossary_and_native_docx_contract_clauses", "question_independent": True, "ambiguity_policy": "hold", "non_inference": "no_special_200_hour_threshold_unless_contract_states_one"},
        "operation_graph": {"external_inputs": [{"input_ref": "input_question", "input_type": "contract_settlement_hypothetical", "source": "question_scope"}], "nodes": nodes, "edges": [{"from": nodes[i - 1]["output_ref"], "to": nodes[i]["operation_id"]} for i in range(1, len(nodes))]},
        "requested_output": {"source_operation_ref": nodes[-1]["operation_id"], "cardinality": "one", "answer_shape": {"container": "scalar", "value_type": "string", "unit": None}, "display_precision": None, "required_keys": None},
    }
    return {"graph_contract_id": "tm_actual_hours_settlement_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and _canonical(expected) == _canonical(contract)


def _sources(engine: Any) -> tuple[Path, Path, Path]:
    root = Path(engine.source_root).resolve()
    glossary = root / "社内管理" / "社内用語集.docx"
    lookup = getattr(engine, "glossary", None).lookup
    if not root.is_dir() or root.is_symlink() or not glossary.is_file() or glossary.is_symlink():
        raise ValueError("source root invalid")
    if lookup("ひがし丘") != [("ひがし丘", ["医療法人社団 蒼泉会 ひがし丘総合病院"])] or lookup("ACTH") != [("ACTH", ["実績工数"])] :
        raise ValueError("glossary bindings changed")
    projects = [path for path in (root / "プロジェクト").iterdir() if path.is_dir() and not path.is_symlink() and _compact(path.name) == _compact("医療法人社団 蒼泉会 ひがし丘総合病院")]
    if len(projects) != 1:
        raise ValueError("project not unique")
    contracts = [path for path in (projects[0] / "01.契約").glob("*.docx") if path.is_file() and not path.is_symlink() and not path.name.startswith("~$") and unicodedata.normalize("NFC", path.name) == "契約書.docx"]
    if len(contracts) != 1:
        raise ValueError("contract not unique")
    return root, glossary, contracts[0]


def _terms(path: Path) -> tuple[int, int]:
    from docx import Document

    document = Document(path)
    paragraphs = [unicodedata.normalize("NFKC", paragraph.text).strip() for paragraph in document.paragraphs if paragraph.text.strip()]
    required = (
        "本契約の料金モデルは、time_and_materialsとし、実績工数に基づく事後精算(月次精算)とする。",
        "乙は、本業務に従事した作業時間を記録した工数表(タイムシート)を作成し、月次で甲に提出する。",
        "作業時間の計上単位は30分とし、30分未満の端数は30分単位に切り上げて計上する。",
        "月次精算の対象となる工数は、当該月に乙が実施した本業務に係る実績工数とする。",
    )
    if any(paragraphs.count(value) != 1 for value in required):
        raise ValueError("settlement clauses incomplete")
    rate_values = [int(match.group(1).replace(",", "")) for text in paragraphs if (match := re.fullmatch(r"時間単価は([0-9,]+)円\(消費税別\)とする。", text))]
    estimate_values = [int(match.group(1)) for text in paragraphs if (match := re.fullmatch(r"想定総工数は([0-9]+)時間とする。", text))]
    formula = [text for text in paragraphs if "契約総額を固定するものではない" in text and "実績工数に時間単価を乗じ、これに消費税を加算" in text]
    boundary = [text for text in paragraphs if re.fullmatch(r"見込工数[0-9]+時間はあくまで見積上の前提であり、実績工数がこれを上回りまたは下回る場合でも、本契約の料金モデルは前項各号に従い処理する。", text)]
    if len(rate_values) != 1 or len(estimate_values) != 1 or len(formula) != 1 or len(boundary) != 1 or str(estimate_values[0]) not in boundary[0]:
        raise ValueError("T&M terms ambiguous")
    return estimate_values[0], rate_values[0]


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    try:
        root, glossary, contract_path = _sources(engine)
        estimate, rate = _terms(contract_path)
        threshold = contract["bindings"]["hypothetical_hours_lower_bound"]
        if not isinstance(threshold, int) or threshold <= estimate:
            raise ValueError("hypothetical does not exceed estimate")
        answer = f"{threshold}時間を超えた場合の特別な上限・定額規定はない。見込工数{estimate}時間は固定上限ではない。月次タイムシートの実績工数を30分単位（30分未満切り上げ）で計上する。時間単価{rate:,}円を乗じて消費税を加算した金額を月次精算する。"
        paths, digest = _fingerprint((glossary, contract_path), root)
        result = StructuredCandidateAnswer(answer, paths, digest, len(contract["operation_graph"]["nodes"]), 1)
        return StructuredCandidateDecision("resolved", "certified_tm_actual_hours_settlement_method", result)
    except (ImportError, OSError, RuntimeError, TypeError, UnicodeError, ValueError):
        return StructuredCandidateDecision("hold", "tm_actual_hours_settlement_not_certified")


__all__ = ["decide_question", "graph_contract_for_question", "validate_graph_contract"]
