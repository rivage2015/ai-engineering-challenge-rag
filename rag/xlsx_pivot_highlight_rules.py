"""Fail-closed extraction and recomputation of highlighted XLSX pivot values."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import zipfile
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision
from xlsx_highlight_projection_rules import (
    _InvalidSource,
    _Sheet,
    _field_key,
    _named_workbooks,
    _normalized,
    _source_root,
    _validate_archive,
    _value_equal,
    _workbook_sheets,
    _yellow_styles,
)

VERSION = "0.1"
PIVOT_HIGHLIGHT = re.compile(
    r"^(?P<location>蒼泉会 ひがし丘総合病院)の(?P<container>train\.xlsx)の"
    r"(?P<sheet>Sheet[12])において、黄色ハイライトされている数値に対応する"
    r"データの抽出条件と集計内容を答えてください。$"
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if not isinstance(question, str):
        return None
    match = PIVOT_HIGHLIGHT.fullmatch(question)
    if match is None:
        return None
    operators = (
        "bind_unique_workbook",
        "validate_ooxml_package",
        "select_visible_sheet",
        "locate_unique_direct_yellow_numeric_cell",
        "identify_containing_pivot_layout",
        "restore_sparse_row_hierarchy",
        "resolve_pivot_data_field_and_aggregate",
        "bind_unique_raw_worksheet",
        "recompute_filtered_aggregate",
        "verify_exact_decimal",
        "project_conditions_and_aggregate",
    )
    nodes = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
        previous = output
    bindings = {key: match[key] for key in ("location", "container", "sheet")}
    core = {
        "xlsx_pivot_highlight_version": VERSION,
        "rule_id": "xlsx_pivot_yellow_aggregate_recomputation",
        "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
        "bindings": bindings,
        "scope": {"source_channel": "native_ooxml_cells_and_raw_rows", "question_independent": True, "ambiguity_policy": "hold"},
        "operation_graph": {
            "external_inputs": [{"input_ref": "input_question", "input_type": "xlsx_workbook", "source": "question_scope"}],
            "nodes": nodes,
            "edges": [{"from": nodes[i - 1]["output_ref"], "to": nodes[i]["operation_id"]} for i in range(1, len(nodes))],
        },
        "requested_output": {"source_operation_ref": nodes[-1]["operation_id"], "cardinality": "multiple", "answer_shape": {"container": "key_value", "value_type": "string", "unit": None}, "display_precision": None, "required_keys": None},
    }
    return {"graph_contract_id": "xlsx_pivot_highlight_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and isinstance(contract, Mapping) and _canonical(expected) == _canonical(contract)


def _target_sheet(sheets: tuple[_Sheet, ...], name: str) -> _Sheet:
    selected = [sheet for sheet in sheets if sheet.state == "visible" and _normalized(sheet.name) == _normalized(name)]
    if len(selected) != 1:
        raise _InvalidSource("sheet")
    return selected[0]


def _yellow_cell(sheet: _Sheet, styles: frozenset[int]) -> tuple[int, int, Decimal]:
    matches = []
    for (row, column), cell in sheet.cells.items():
        if cell.style not in styles:
            continue
        try:
            value = Decimal(cell.display)
        except Exception:
            continue
        matches.append((row, column, value))
    if len(matches) != 1:
        raise _InvalidSource("yellow numeric cell")
    return matches[0]


def _nearest(sheet: _Sheet, row: int, column: int) -> str:
    for current in range(row, 0, -1):
        cell = sheet.cells.get((current, column))
        if cell is not None and cell.display.strip():
            value = cell.display.strip()
            if "集計" not in value and _normalized(value) not in {"総計", "grand total"}:
                return value
    raise _InvalidSource("sparse hierarchy")


def _projection(sheet: _Sheet, marked: tuple[int, int, Decimal]) -> tuple[tuple[str, ...], tuple[str, ...], str, str, Decimal]:
    row, column, highlighted = marked
    if _normalized(sheet.name) == "sheet1":
        if column not in (5, 6) or row <= 3:
            raise _InvalidSource("sheet1 layout")
        fields = tuple(sheet.cells[(3, col)].display.strip() for col in range(1, 5))
        values = tuple(_nearest(sheet, row, col) for col in range(1, 5))
        caption = sheet.cells[(3, column)].display.strip()
    elif _normalized(sheet.name) == "sheet2":
        if not (4 <= row <= 22 and column in (5, 6)):
            raise _InvalidSource("sheet2 layout")
        # The D:F pivot uses Excel's compact row layout: the parent item is the
        # closest preceding style-3 cell and the child is the target style-4 cell.
        child = sheet.cells.get((row, 4))
        if child is None or child.style != 4 or not child.display.strip():
            raise _InvalidSource("compact child")
        parents = [sheet.cells[(r, 4)].display.strip() for r in range(row - 1, 3, -1) if (r, 4) in sheet.cells and sheet.cells[(r, 4)].style == 3]
        if not parents:
            raise _InvalidSource("compact parent")
        fields = ("children", "smoker")
        values = (parents[0], child.display.strip())
        caption = sheet.cells[(3, column)].display.strip()
    else:
        raise _InvalidSource("unsupported sheet")
    match = re.fullmatch(r"(平均|合計)\s*/\s*(\S+)", caption)
    if match is None:
        raise _InvalidSource("aggregate caption")
    return fields, values, match[2], match[1], highlighted


def _raw_sheet(sheets: tuple[_Sheet, ...], target: _Sheet, required: tuple[str, ...]) -> tuple[_Sheet, int, dict[str, int]]:
    keys = {_field_key(value) for value in required}
    matches = []
    for sheet in sheets:
        if sheet is target or sheet.state != "visible":
            continue
        for row in range(1, 21):
            mapping = {_field_key(cell.display): column for (cell_row, column), cell in sheet.cells.items() if cell_row == row and cell.display.strip()}
            if keys.issubset(mapping):
                matches.append((sheet, row, mapping))
    if len(matches) != 1:
        raise _InvalidSource("raw worksheet")
    return matches[0]


def _verify(sheets: tuple[_Sheet, ...], target: _Sheet, fields: tuple[str, ...], values: tuple[str, ...], measure: str, aggregate: str, highlighted: Decimal) -> bool:
    raw, header_row, headers = _raw_sheet(sheets, target, fields + (measure,))
    selected = []
    maximum = max((row for row, _ in raw.cells), default=header_row)
    for row in range(header_row + 1, maximum + 1):
        if not all(_value_equal(raw.cells.get((row, headers[_field_key(field)])), value) for field, value in zip(fields, values)):
            continue
        cell = raw.cells.get((row, headers[_field_key(measure)]))
        if cell is None:
            continue
        try:
            selected.append(Decimal(cell.display))
        except Exception:
            return False
    if not selected:
        return False
    computed = sum(selected, Decimal(0)) if aggregate == "合計" else sum(selected, Decimal(0)) / Decimal(len(selected))
    return abs(computed - highlighted) <= Decimal("1e-12")


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    try:
        root = _source_root(engine)
        if root is None:
            raise _InvalidSource("root")
        bindings = contract["bindings"]
        paths = _named_workbooks(engine, bindings["location"], bindings["container"])
        if len(paths) != 1:
            raise _InvalidSource("workbook")
        path = paths[0]
        with zipfile.ZipFile(path) as archive:
            members = _validate_archive(archive)
        sheets = _workbook_sheets(members)
        target = _target_sheet(sheets, bindings["sheet"])
        yellow_styles, style_count = _yellow_styles(members)
        if any(cell.style >= style_count for sheet in sheets for cell in sheet.cells.values()):
            raise _InvalidSource("style")
        fields, values, measure, aggregate, highlighted = _projection(target, _yellow_cell(target, yellow_styles))
        if not _verify(sheets, target, fields, values, measure, aggregate, highlighted):
            raise _InvalidSource("raw aggregate mismatch")
        conditions = "、".join(f"{field}={value}" for field, value in zip(fields, values))
        answer = f"抽出条件：{conditions}。集計内容：{measure}の{aggregate}。"
        relative = unicodedata.normalize("NFC", path.relative_to(root).as_posix())
        result = StructuredCandidateAnswer(answer, (relative,), hashlib.sha256(path.read_bytes()).hexdigest(), len(contract["operation_graph"]["nodes"]), 1)
        return StructuredCandidateDecision("resolved", "certified_xlsx_pivot_highlight", result)
    except (OSError, RuntimeError, KeyError, TypeError, ValueError, zipfile.BadZipFile):
        return StructuredCandidateDecision("hold", "xlsx_pivot_highlight_not_certified")


__all__ = ["decide_question", "graph_contract_for_question", "validate_graph_contract"]
