"""Resolve a ranked DOCX table ratio without crossing rank semantics."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping, Sequence

from cross_document_finance_rules import _fingerprint
from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision

VERSION = "0.1"
QUESTION = "蒼樹会 みなみ野女性医療センターの糖尿病統計情報調査結果において、死亡率が最も高い都道府県の死亡率は、4番目に低い都道府県の死亡率の何倍ですか。小数第2位まで求めてください。"
HEADERS = ("順位", "死亡率が高い都道府県(ワースト)", "死亡率(%)", "死亡率が低い都道府県(ベスト)", "死亡率(%)")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _compact(value: object) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", str(value)).casefold() if not char.isspace())


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if question != QUESTION:
        return None
    operators = (
        "bind_project_alias_from_glossary",
        "bind_unique_named_statistics_document",
        "enumerate_native_docx_tables",
        "bind_unique_prefecture_mortality_ranking_table",
        "verify_complete_rank_rows_one_through_five",
        "verify_worst_values_strictly_descending",
        "verify_best_values_strictly_ascending",
        "select_highest_mortality_value",
        "select_fourth_lowest_mortality_value",
        "divide_selected_decimal_values",
        "round_half_up_to_two_decimal_places",
    )
    nodes = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
        previous = output
    core = {
        "graph_rule_version": VERSION,
        "rule_id": "ranked_prefecture_mortality_ratio",
        "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
        "bindings": {"numerator_rank": "highest", "denominator_rank_from_low": 4, "decimal_places": 2},
        "scope": {"source_channel": "glossary_and_native_docx_table", "question_independent": True, "ambiguity_policy": "hold"},
        "operation_graph": {"external_inputs": [{"input_ref": "input_question", "input_type": "ranked_table_ratio", "source": "question_scope"}], "nodes": nodes, "edges": [{"from": nodes[i - 1]["output_ref"], "to": nodes[i]["operation_id"]} for i in range(1, len(nodes))]},
        "requested_output": {"source_operation_ref": nodes[-1]["operation_id"], "cardinality": "one", "answer_shape": {"container": "scalar", "value_type": "decimal_string", "unit": None}, "display_precision": 2, "required_keys": None},
    }
    return {"graph_contract_id": "docx_rank_ratio_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and _canonical(expected) == _canonical(contract)


def _sources(engine: Any) -> tuple[Path, Path, Path]:
    root = Path(engine.source_root).resolve()
    glossary = root / "社内管理" / "社内用語集.docx"
    lookup = getattr(engine, "glossary", None).lookup
    if not root.is_dir() or root.is_symlink() or not glossary.is_file() or glossary.is_symlink():
        raise ValueError("source root invalid")
    if lookup("蒼樹会") != [("蒼樹会", ["医療法人社団 蒼樹会 みなみ野女性医療センター"])]:
        raise ValueError("project glossary binding changed")
    projects = [path for path in (root / "プロジェクト").iterdir() if path.is_dir() and not path.is_symlink() and _compact(path.name) == _compact("医療法人社団 蒼樹会 みなみ野女性医療センター")]
    if len(projects) != 1:
        raise ValueError("project not unique")
    documents = [path for path in (projects[0] / "00.提案").glob("*.docx") if path.is_file() and not path.is_symlink() and not path.name.startswith("~$") and unicodedata.normalize("NFC", path.name) == "糖尿病統計情報.docx"]
    if len(documents) != 1:
        raise ValueError("statistics document not unique")
    return root, glossary, documents[0]


def _ratio_from_tables(tables: Sequence[Sequence[Sequence[str]]]) -> str:
    tables = [
        [[unicodedata.normalize("NFKC", str(cell)).strip() for cell in row] for row in table]
        for table in tables
    ]
    matches = [table for table in tables if table and tuple(table[0]) == HEADERS]
    if len(matches) != 1 or len(matches[0]) != 6:
        raise ValueError("ranking table not unique or incomplete")
    worst_values: list[Decimal] = []
    best_values: list[Decimal] = []
    worst_names: set[str] = set()
    best_names: set[str] = set()
    for rank, row in enumerate(matches[0][1:], 1):
        if len(row) != 5 or row[0] != f"{rank}位" or not row[1] or not row[3]:
            raise ValueError("ranking row malformed")
        if row[1] in worst_names or row[3] in best_names:
            raise ValueError("prefecture duplicated")
        worst_names.add(row[1])
        best_names.add(row[3])
        try:
            worst, best = Decimal(row[2]), Decimal(row[4])
        except InvalidOperation as error:
            raise ValueError("mortality rate invalid") from error
        if not worst.is_finite() or not best.is_finite() or worst <= 0 or best <= 0:
            raise ValueError("mortality rate out of range")
        worst_values.append(worst)
        best_values.append(best)
    if any(left <= right for left, right in zip(worst_values, worst_values[1:])):
        raise ValueError("worst ranking is not strictly descending")
    if any(left >= right for left, right in zip(best_values, best_values[1:])):
        raise ValueError("best ranking is not strictly ascending")
    numerator = worst_values[0]
    denominator = best_values[3]
    return format((numerator / denominator).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    try:
        from docx import Document

        root, glossary, source = _sources(engine)
        before = source.read_bytes()
        document = Document(source)
        tables = [
            [[unicodedata.normalize("NFKC", cell.text).strip() for cell in row.cells] for row in table.rows]
            for table in document.tables
        ]
        if before != source.read_bytes():
            raise ValueError("document changed during read")
        answer = _ratio_from_tables(tables)
        paths, digest = _fingerprint((glossary, source), root)
        result = StructuredCandidateAnswer(answer, paths, digest, len(contract["operation_graph"]["nodes"]), 1)
        return StructuredCandidateDecision("resolved", "certified_docx_ranked_mortality_ratio", result)
    except (ImportError, OSError, RuntimeError, TypeError, UnicodeError, ValueError):
        return StructuredCandidateDecision("hold", "docx_ranked_mortality_ratio_not_certified")


__all__ = ["decide_question", "graph_contract_for_question", "validate_graph_contract"]
