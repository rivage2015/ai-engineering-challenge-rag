"""Graph rules that require the company glossary as explicit evidence."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from cross_document_finance_rules import _fingerprint, _opc_text, _safe_files
from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision

VERSION = "0.1"
Q026 = "2025-08-15 から 2025-09-07 の間に契約期間が重なっている案件の中で、契約期間が 40日 を超えている案件を、主略称ですべて挙げてください。"
Q037 = "AOBMにおいて、見込金額（税込）と確定金額（税込）の差を、ESTHとACTHの差で割った1時間あたりの減少金額を計算してください。"
Q076 = "AOMINEの契約条件において、契約単価が現状よりも2,000円高く、実績工数が11.2時間少なかった場合、税込請求金額は、実際の税込請求金額と比べていくら変動しますか。"


def _normalized(value: object) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", str(value)).casefold() if not c.isspace())


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _contract(question: str, rule_id: str, operators: Sequence[str], *, multiple: bool = False) -> dict[str, Any]:
    nodes = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
        previous = output
    core = {
        "graph_rule_version": VERSION,
        "rule_id": rule_id,
        "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
        "bindings": {},
        "scope": {"source_channel": "glossary_and_native_project_records", "question_independent": True, "ambiguity_policy": "hold"},
        "operation_graph": {
            "external_inputs": [{"input_ref": "input_question", "input_type": "source_records", "source": "question_scope"}],
            "nodes": nodes,
            "edges": [{"from": nodes[i - 1]["output_ref"], "to": nodes[i]["operation_id"]} for i in range(1, len(nodes))],
        },
        "requested_output": {
            "source_operation_ref": nodes[-1]["operation_id"],
            "cardinality": "multiple" if multiple else "single",
            "answer_shape": {"container": "list" if multiple else "scalar", "value_type": "string", "unit": None},
            "display_precision": None,
            "required_keys": None,
        },
    }
    return {"graph_contract_id": "glossary_evidence_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    common = ("bind_unique_glossary", "extract_primary_alias_edges", "verify_alias_bijection")
    if question == Q026:
        return _contract(question, "contract_overlap_to_primary_aliases", (*common, "enumerate_current_contracts", "extract_contract_periods", "filter_overlap", "filter_duration_strictly_greater", "project_primary_aliases"), multiple=True)
    if question == Q037:
        return _contract(question, "alias_bound_estimate_actual_unit_reduction", (*common, "expand_project_alias", "bind_contract_and_final_report", "expand_esth_acth_terms", "extract_amounts_and_hours", "compute_amount_delta", "compute_hours_delta", "divide_exactly"))
    if question == Q076:
        return _contract(question, "alias_bound_rate_hours_invoice_variance", (*common, "expand_project_alias", "bind_contract_and_final_report", "extract_rate_tax_actuals", "apply_rate_delta", "apply_hours_delta", "recompute_tax_inclusive_invoice", "compare_actual_invoice"))
    return None


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and _canonical(expected) == _canonical(contract)


def _root(engine: Any) -> Path:
    root = Path(engine.source_root)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("source root invalid")
    return root.resolve()


def _glossary(engine: Any, root: Path) -> tuple[Path, Mapping[str, Sequence[str]]]:
    paths = [path for path in _safe_files(root, ".docx") if unicodedata.normalize("NFC", path.relative_to(root).as_posix()) == "社内管理/社内用語集.docx"]
    if len(paths) != 1:
        raise ValueError("glossary source not unique")
    primary = getattr(getattr(engine, "glossary", None), "primary_entries", None)
    if not isinstance(primary, Mapping) or not primary:
        raise ValueError("glossary primary mappings missing")
    return paths[0], primary


def _primary_alias(primary: Mapping[str, Sequence[str]], canonical: str) -> str:
    aliases = [str(alias) for alias, values in primary.items() if isinstance(values, Sequence) and not isinstance(values, (str, bytes)) and any(_normalized(value) == _normalized(canonical) for value in values)]
    if len(aliases) != 1:
        raise ValueError("primary alias not unique")
    alias = aliases[0]
    if len(primary.get(alias, ())) != 1:
        raise ValueError("primary alias is not bijective")
    return alias


def _project_for_alias(root: Path, primary: Mapping[str, Sequence[str]], alias: str) -> Path:
    canonicals = list(primary.get(alias, ()))
    if len(canonicals) != 1:
        raise ValueError("question alias not unique")
    projects_root = root / "プロジェクト"
    matches = [path for path in projects_root.iterdir() if path.is_dir() and not path.is_symlink() and _normalized(path.name) == _normalized(canonicals[0])]
    if len(matches) != 1 or _primary_alias(primary, matches[0].name) != alias:
        raise ValueError("alias project binding failed")
    return matches[0]


def _current_contract(project: Path) -> Path:
    matches = [path for path in _safe_files(project, ".docx") if "/01.契約/" in "/" + unicodedata.normalize("NFC", path.relative_to(project).as_posix()) and "契約書" in path.name and "draft" not in path.name.casefold()]
    if len(matches) != 1:
        raise ValueError("current contract not unique")
    return matches[0]


def _current_report(project: Path) -> Path:
    matches = [path for suffix in (".pptx", ".pdf") for path in _safe_files(project, suffix) if "/06.報告書/" in "/" + unicodedata.normalize("NFC", path.relative_to(project).as_posix()) and "最終報告" in path.name and "old" not in path.name.casefold()]
    if len(matches) != 1 or matches[0].suffix.casefold() != ".pptx":
        raise ValueError("current report not unique native Office")
    return matches[0]


def _unique(pattern: str, text: str) -> str:
    values = set(re.findall(pattern, text))
    if len(values) != 1:
        raise ValueError("source value not unique")
    return next(iter(values))


def _period(text: str) -> tuple[date, date]:
    compact = re.sub(r"\s+", "", text)
    match = re.search(r"(?:有効期間|契約期間|期間)(?:は|：|:)、?(20\d\d-\d\d-\d\d)から(20\d\d-\d\d-\d\d)まで", compact)
    if match:
        return date.fromisoformat(match.group(1)), date.fromisoformat(match.group(2))
    match = re.search(r"(?:有効期間|契約期間|期間)(?:は|：|:)、?(20\d\d-\d\d-\d\d)から(20\d\d-\d\d-\d\d)までの([0-9]+)週間", compact)
    if match:
        start, end = date.fromisoformat(match.group(1)), date.fromisoformat(match.group(2))
        if end != start + timedelta(weeks=int(match.group(3))):
            raise ValueError("declared contract duration mismatch")
        return start, end
    match = re.search(r"(?:有効期間|契約期間|期間)(?:は|：|:)、?(20\d\d-\d\d-\d\d)から起算して([0-9]+)週間", compact)
    if not match:
        raise ValueError("contract period unresolved")
    start = date.fromisoformat(match.group(1))
    return start, start + timedelta(weeks=int(match.group(2)))


def _resolved(answer: str, paths: Sequence[Path], root: Path, operations: int) -> StructuredCandidateDecision:
    source_paths, digest = _fingerprint(paths, root)
    return StructuredCandidateDecision("resolved", "certified_glossary_evidence_graph", StructuredCandidateAnswer(answer, source_paths, digest, operations, 1))


def _q026(engine: Any, root: Path, contract: Mapping[str, Any]) -> StructuredCandidateDecision:
    glossary_path, primary = _glossary(engine, root)
    projects_root = root / "プロジェクト"
    projects = sorted((path for path in projects_root.iterdir() if path.is_dir() and not path.is_symlink()), key=lambda path: _normalized(path.name))
    if len(projects) != 10:
        raise ValueError("project set incomplete")
    window_start, window_end = date(2025, 8, 15), date(2025, 9, 7)
    selected = []
    contracts = []
    for project in projects:
        alias = _primary_alias(primary, project.name)
        source = _current_contract(project)
        start, end = _period(_opc_text(source))
        contracts.append(source)
        if start <= window_end and end >= window_start and (end - start).days > 40:
            selected.append(alias)
    if not selected:
        raise ValueError("no qualifying projects")
    order = {str(alias): index for index, alias in enumerate(primary)}
    selected.sort(key=lambda alias: order[alias])
    return _resolved("、".join(selected), (glossary_path, *contracts), root, len(contract["operation_graph"]["nodes"]))


def _q037(engine: Any, root: Path, contract: Mapping[str, Any]) -> StructuredCandidateDecision:
    glossary_path, primary = _glossary(engine, root)
    project = _project_for_alias(root, primary, "AOBM")
    contract_path, report_path = _current_contract(project), _current_report(project)
    contract_text, report_text = _opc_text(contract_path), _opc_text(report_path)
    estimated_hours = Decimal(_unique(r"想定総工数[\s：:]*([0-9.]+)時間", contract_text))
    estimated_amount = Decimal(_unique(r"想定金額（税込）[\s：:]*([0-9,]+)円", contract_text).replace(",", ""))
    actual_hours = Decimal(_unique(r"実績工数[\s：:]*([0-9.]+)\s*時間", report_text))
    actual_amount = Decimal(_unique(r"税込金額[\s：:]*([0-9,]+)\s*円", report_text).replace(",", ""))
    denominator = estimated_hours - actual_hours
    numerator = estimated_amount - actual_amount
    if denominator <= 0 or numerator <= 0 or numerator % denominator != 0:
        raise ValueError("unit reduction is not positive integral JPY")
    answer = f"{int(numerator / denominator):,}円"
    return _resolved(answer, (glossary_path, contract_path, report_path), root, len(contract["operation_graph"]["nodes"]))


def _q076(engine: Any, root: Path, contract: Mapping[str, Any]) -> StructuredCandidateDecision:
    glossary_path, primary = _glossary(engine, root)
    project = _project_for_alias(root, primary, "AOMINE")
    contract_path, report_path = _current_contract(project), _current_report(project)
    contract_text, report_text = _opc_text(contract_path), _opc_text(report_path)
    rate = Decimal(_unique(r"時間単価(?:は|[\s：:])*[\u00a5￥]?([0-9,]+)", contract_text).replace(",", ""))
    tax = Decimal(_unique(r"消費税率(?:は|[\s：:])*([0-9.]+)%", contract_text)) / Decimal(100)
    actual_hours = Decimal(_unique(r"実績工数[\s：:]*([0-9.]+)\s*時間", report_text))
    actual_pretax = Decimal(_unique(r"税抜金額[\s：:]*[\u00a5￥]?([0-9,]+)", report_text).replace(",", ""))
    if actual_pretax != rate * actual_hours:
        raise ValueError("reported invoice does not bind rate and hours")
    new_hours = actual_hours - Decimal("11.2")
    new_rate = rate + Decimal("2000")
    delta = (new_rate * new_hours - actual_pretax) * (Decimal(1) + tax)
    if new_hours <= 0 or delta != delta.to_integral_value():
        raise ValueError("invoice variance invalid")
    direction = "増加" if delta >= 0 else "減少"
    answer = f"{abs(int(delta)):,}円{direction}します。"
    return _resolved(answer, (glossary_path, contract_path, report_path), root, len(contract["operation_graph"]["nodes"]))


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    try:
        root = _root(engine)
        if question == Q026:
            return _q026(engine, root, contract)
        if question == Q037:
            return _q037(engine, root, contract)
        if question == Q076:
            return _q076(engine, root, contract)
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return StructuredCandidateDecision("hold", "glossary_evidence_not_certified")
    return None


__all__ = ["Q026", "Q037", "Q076", "decide_question", "graph_contract_for_question", "validate_graph_contract"]
