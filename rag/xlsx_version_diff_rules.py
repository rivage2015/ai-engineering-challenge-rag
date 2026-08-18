"""Fail-closed semantic comparison of two revisioned XLSX schedules.

The rule compares authored cell values by header meaning and task identity,
not by worksheet coordinates.  Column/row order, styles, widths, frozen panes,
and shared-string indexes are therefore non-semantic.  The transition named in
the question is excluded only in the schedule status field; every other value
change remains reportable.

Parsing reuses the bounded, relationship-safe OOXML reader already exercised
by the native XLSX lane.  Ambiguous sources, headers, task keys, hidden content,
formulas, drawings, merged cells, or unsupported package features fail closed.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from structured_candidate import (
    StructuredCandidateAnswer,
    StructuredCandidateDecision,
)
from xlsx_highlight_projection_rules import (
    _InvalidSource,
    _Sheet,
    _named_workbooks,
    _source_root,
    _validate_archive,
    _workbook_sheets,
    _yellow_styles,
)


XLSX_VERSION_DIFF_RULE_VERSION = "0.1"

VERSION_DIFF = re.compile(
    r"^(?P<location>.+?)の(?P<left>[^,、。]+?\.xlsx)と"
    r"(?P<right>[^,、。]+?\.xlsx)を比較したとき、"
    r"(?P<exclude_from>[^,、。]+?)から(?P<exclude_to>[^,、。]+?)への変更を除いて、"
    r"案件遂行に関連する変更点を挙げてください。?$"
)

_REVISION = re.compile(r"(?:^|[_.\-])r(?P<number>[1-9][0-9]*)$", re.IGNORECASE)
_S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_S = "{" + _S_NS + "}"
_MAX_TASKS = 100_000
_MAX_CHANGES = 256
_MAX_TEXT = 16_384

_HEADERS = (
    "No.",
    "タスクID",
    "依存タスク",
    "ステータス",
    "フェーズ",
    "タスク名",
    "詳細・内容",
    "クリティカルパス",
    "マイルストーン",
    "チェックポイント",
    "成果物",
    "開始日",
    "終了日",
    "担当者",
    "備考",
)
_HEADER_BY_KEY = {
    unicodedata.normalize("NFKC", value).strip(): value for value in _HEADERS
}
_DIFF_FIELDS = tuple(
    value for value in _HEADERS if value not in {"No.", "タスクID"}
)
_FORBIDDEN_PACKAGE_PARTS = (
    "vbaproject.bin",
    "xl/connections.xml",
    "xl/externalLinks/",
    "xl/model/",
    "xl/pivotCache/",
)


@dataclass(frozen=True)
class _Value:
    raw: str | Decimal | bool | None
    display: str


@dataclass(frozen=True)
class _Record:
    task_id: str
    ordinal: int
    values: Mapping[str, _Value]


@dataclass(frozen=True)
class _Table:
    records: Mapping[str, _Record]
    date_1904: bool


@dataclass(frozen=True)
class _Change:
    task_id: str
    field: str
    before: _Value
    after: _Value
    task_name: str


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


def _semantic_text(value: object) -> str:
    rendered = unicodedata.normalize("NFKC", str(value)).replace("\r\n", "\n")
    rendered = rendered.replace("\r", "\n").strip()
    if "\x00" in rendered or len(rendered) > _MAX_TEXT:
        raise _InvalidSource("semantic text")
    if any(ord(char) < 32 and char not in {"\n", "\t"} for char in rendered):
        raise _InvalidSource("semantic control character")
    return rendered


def _revision_number(filename: str) -> int | None:
    match = _REVISION.search(Path(filename).stem)
    return int(match["number"]) if match is not None else None


def _nodes(operators: Sequence[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        result.append(
            {
                "operation_id": f"op_{index:03d}_{operator}",
                "operator": operator,
                "input_refs": [previous],
                "output_ref": output,
            }
        )
        previous = output
    return result


def _contract(question: str, bindings: Mapping[str, str]) -> dict[str, Any]:
    operators = (
        "retrieve_revision_pair",
        "parse_visible_schedule_tables",
        "align_headers",
        "key_rows_by_task_id",
        "semantic_diff",
        "exclude_declared_status_transition",
        "render_change_records",
    )
    nodes = _nodes(operators)
    core = {
        "graph_rule_version": XLSX_VERSION_DIFF_RULE_VERSION,
        "rule_id": "xlsx_keyed_schedule_version_diff",
        "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
        "bindings": dict(bindings),
        "scope": {
            "location": bindings["location"],
            "containers": [bindings["left"], bindings["right"]],
            "source_kind": "exact_revisioned_xlsx_pair",
            "sheet_state": "visible",
            "table_kind": "schedule",
            "identity_field": "タスクID",
            "comparison_channel": "authored_cell_values_by_header",
            "ignored_channels": [
                "cell_style",
                "column_position",
                "row_position",
                "sheet_view",
                "shared_string_index",
            ],
            "excluded_transition": {
                "field": "ステータス",
                "from": bindings["exclude_from"],
                "to": bindings["exclude_to"],
            },
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
            "cardinality": "multiple",
            "answer_shape": {
                "container": "list",
                "value_type": "string",
                "unit": None,
            },
            "display_precision": None,
            "required_keys": None,
        },
    }
    return {
        "graph_contract_id": "xlsx_version_diff_"
        + hashlib.sha256(_canonical_json(core).encode("utf-8")).hexdigest()[:32],
        **core,
    }


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if not isinstance(question, str):
        return None
    match = VERSION_DIFF.fullmatch(question)
    if match is None:
        return None
    bindings = {
        key: match[key]
        for key in ("location", "left", "right", "exclude_from", "exclude_to")
    }
    left_revision = _revision_number(bindings["left"])
    right_revision = _revision_number(bindings["right"])
    if (
        _normalized(bindings["left"]) == _normalized(bindings["right"])
        or left_revision is None
        or right_revision is None
        or left_revision >= right_revision
        or _normalized(bindings["exclude_from"])
        == _normalized(bindings["exclude_to"])
    ):
        return None
    return _contract(question, bindings)


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    if expected is None or not isinstance(contract, Mapping):
        return False
    try:
        return _canonical_json(expected) == _canonical_json(contract)
    except (TypeError, ValueError):
        return False


def _package_is_supported(members: Mapping[str, bytes]) -> bool:
    for name in members:
        folded = name.casefold()
        if any(marker.casefold() in folded for marker in _FORBIDDEN_PACKAGE_PARTS):
            return False
        if folded.endswith((".xlsb", ".bin")):
            return False
    return True


def _date_system(members: Mapping[str, bytes]) -> bool:
    data = members.get("xl/workbook.xml")
    if data is None:
        raise _InvalidSource("missing workbook")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise _InvalidSource("malformed workbook") from exc
    if root.findall(".//" + _S + "definedName"):
        raise _InvalidSource("defined names")
    properties = root.find(_S + "workbookPr")
    raw = "0" if properties is None else properties.get("date1904", "0")
    if raw not in {"0", "1", "false", "true", "False", "True"}:
        raise _InvalidSource("date system")
    return raw in {"1", "true", "True"}


def _cell_value(sheet: _Sheet, row: int, column: int) -> _Value:
    cell = sheet.cells.get((row, column))
    if cell is None or not cell.display.strip():
        return _Value(None, "")
    return _Value(cell.value, _semantic_text(cell.display))


def _integer(value: _Value) -> int | None:
    try:
        number = Decimal(str(value.raw if value.raw is not None else value.display))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number != number.to_integral_value():
        return None
    result = int(number)
    return result if result > 0 else None


def _table_from_sheet(sheet: _Sheet, date_1904: bool) -> _Table:
    if (
        sheet.state != "visible"
        or sheet.merges
        or sheet.drawing_ids
        or sheet.has_formula
        or sheet.hidden_rows
        or sheet.hidden_columns
    ):
        raise _InvalidSource("ambiguous worksheet")

    populated_rows = sorted({row for row, _ in sheet.cells})
    header_candidates: list[tuple[int, dict[str, int]]] = []
    for row in populated_rows[:100]:
        entries = [
            (column, _semantic_text(cell.display))
            for (cell_row, column), cell in sheet.cells.items()
            if cell_row == row and cell.display.strip()
        ]
        if len(entries) != len(_HEADERS):
            continue
        mapping: dict[str, int] = {}
        valid = True
        for column, value in entries:
            canonical = _HEADER_BY_KEY.get(value)
            if canonical is None or canonical in mapping:
                valid = False
                break
            mapping[canonical] = column
        if valid and set(mapping) == set(_HEADERS):
            header_candidates.append((row, mapping))
    if len(header_candidates) != 1:
        raise _InvalidSource("schedule header not unique")
    header_row, columns = header_candidates[0]
    table_columns = set(columns.values())

    for (row, column), cell in sheet.cells.items():
        if not cell.display.strip():
            continue
        if column not in table_columns or row < header_row:
            raise _InvalidSource("content outside schedule table")

    records: dict[str, _Record] = {}
    ordinals: set[int] = set()
    for row in populated_rows:
        if row <= header_row:
            continue
        values = {field: _cell_value(sheet, row, column) for field, column in columns.items()}
        if not any(value.display for value in values.values()):
            continue
        task_id = values["タスクID"].display
        ordinal = _integer(values["No."])
        if (
            not task_id
            or len(task_id) > 128
            or ordinal is None
            or not values["タスク名"].display
            or task_id in records
            or ordinal in ordinals
        ):
            raise _InvalidSource("schedule row identity")
        records[task_id] = _Record(task_id, ordinal, values)
        ordinals.add(ordinal)
        if len(records) > _MAX_TASKS:
            raise _InvalidSource("schedule task count")
    if not records:
        raise _InvalidSource("empty schedule")
    return _Table(records, date_1904)


def _load_table(path: Path) -> _Table:
    with zipfile.ZipFile(path) as archive:
        members = _validate_archive(archive)
    if not _package_is_supported(members):
        raise _InvalidSource("unsupported workbook package")
    sheets = _workbook_sheets(members)
    if len(sheets) != 1:
        raise _InvalidSource("worksheet not unique")
    _, style_count = _yellow_styles(members)
    if any(cell.style >= style_count for cell in sheets[0].cells.values()):
        raise _InvalidSource("cell style index")
    return _table_from_sheet(sheets[0], _date_system(members))


def _value_equal(left: _Value, right: _Value) -> bool:
    if isinstance(left.raw, Decimal) and isinstance(right.raw, Decimal):
        return left.raw == right.raw
    if isinstance(left.raw, bool) or isinstance(right.raw, bool):
        return type(left.raw) is type(right.raw) and left.raw == right.raw
    return _semantic_text(left.display) == _semantic_text(right.display)


def _diff_tables(
    before: _Table,
    after: _Table,
    exclude_from: str,
    exclude_to: str,
) -> tuple[_Change, ...]:
    if set(before.records) != set(after.records):
        raise _InvalidSource("task sets differ")
    excluded_left = _normalized(exclude_from)
    excluded_right = _normalized(exclude_to)
    changes: list[_Change] = []
    ordered = sorted(before.records.values(), key=lambda record: (record.ordinal, record.task_id))
    for old_record in ordered:
        new_record = after.records[old_record.task_id]
        for field in _DIFF_FIELDS:
            left = old_record.values[field]
            right = new_record.values[field]
            if _value_equal(left, right):
                continue
            if (
                field == "ステータス"
                and _normalized(left.display) == excluded_left
                and _normalized(right.display) == excluded_right
            ):
                continue
            task_name = (
                right.display
                if field == "タスク名" and right.display
                else new_record.values["タスク名"].display
            )
            changes.append(
                _Change(old_record.task_id, field, left, right, task_name)
            )
            if len(changes) > _MAX_CHANGES:
                raise _InvalidSource("too many semantic changes")
    if not changes:
        raise _InvalidSource("no remaining semantic changes")
    return tuple(changes)


def _assignees(value: str) -> tuple[str, ...] | None:
    if not value:
        return ()
    parts = tuple(
        _semantic_text(part)
        for part in re.split(r"\s*(?:/|／|,|，|、|;|；)\s*", value)
    )
    if not parts or any(not part for part in parts) or len(parts) != len(set(parts)):
        return None
    return parts


def _excel_date(value: _Value, date_1904: bool) -> str | None:
    if not isinstance(value.raw, Decimal) or value.raw != value.raw.to_integral_value():
        return None
    serial = int(value.raw)
    if date_1904:
        origin = date(1904, 1, 1)
        offset = serial
    else:
        if serial <= 0 or serial == 60:
            return None
        origin = date(1899, 12, 31)
        offset = serial if serial < 60 else serial - 1
    try:
        return (origin + timedelta(days=offset)).isoformat()
    except OverflowError:
        return None


def _render_value(field: str, value: _Value, date_1904: bool) -> str:
    if not value.display:
        return "空欄"
    if field in {"開始日", "終了日"}:
        rendered_date = _excel_date(value, date_1904)
        if rendered_date is not None:
            return rendered_date
    return value.display.replace("\n", " ")


def _render_change(change: _Change, before: _Table, after: _Table) -> str:
    left = _render_value(change.field, change.before, before.date_1904)
    right = _render_value(change.field, change.after, after.date_1904)
    subject = f"{change.task_id}（{change.task_name}）"
    if change.field == "担当者":
        old_people = _assignees(change.before.display)
        new_people = _assignees(change.after.display)
        if old_people is not None and new_people is not None:
            added = tuple(person for person in new_people if person not in old_people)
            removed = tuple(person for person in old_people if person not in new_people)
            details: list[str] = []
            if added:
                details.append("追加: " + "、".join(added))
            if removed:
                details.append("削除: " + "、".join(removed))
            if details:
                return (
                    f"{subject}の担当者: {left} → {right}"
                    f"（{'、'.join(details)}）"
                )
    return f"{subject}の{change.field}: {left} → {right}"


def _decision(
    answer: str,
    paths: Sequence[Path],
    root: Path,
    operations: int,
) -> StructuredCandidateDecision:
    unique_paths = tuple(sorted(set(paths), key=lambda item: item.as_posix()))
    digest = hashlib.sha256()
    for path in unique_paths:
        digest.update(path.read_bytes())
    relative = tuple(
        unicodedata.normalize("NFC", path.relative_to(root).as_posix())
        for path in unique_paths
    )
    return StructuredCandidateDecision(
        "resolved",
        "certified_xlsx_version_diff",
        StructuredCandidateAnswer(
            answer=answer,
            source_paths=relative,
            source_sha256=digest.hexdigest(),
            operation_count=operations,
            output_count=1,
        ),
    )


def _hold(reason: str) -> StructuredCandidateDecision:
    return StructuredCandidateDecision("hold", reason)


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    root = _source_root(engine)
    if root is None:
        return _hold("xlsx_version_diff_source_root_invalid")
    bindings = contract["bindings"]
    left_paths = _named_workbooks(
        engine, bindings["location"], bindings["left"]
    )
    right_paths = _named_workbooks(
        engine, bindings["location"], bindings["right"]
    )
    if len(left_paths) != 1 or len(right_paths) != 1:
        return _hold("xlsx_version_diff_workbook_pair_not_unique")
    left, right = left_paths[0], right_paths[0]
    if left == right or left.parent != right.parent:
        return _hold("xlsx_version_diff_workbook_pair_not_unique")
    try:
        before = _load_table(left)
        after = _load_table(right)
        changes = _diff_tables(
            before,
            after,
            bindings["exclude_from"],
            bindings["exclude_to"],
        )
        answer = "\n".join(
            _render_change(change, before, after) for change in changes
        )
        if not answer or len(answer) > 32_768:
            raise _InvalidSource("rendered answer")
    except (
        ET.ParseError,
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        zipfile.BadZipFile,
    ):
        return _hold("xlsx_version_diff_source_not_certified")
    return _decision(answer, (left, right), root, 7)


def decide_from_graph(
    engine: Any,
    question: str,
    graph_plan: Any,
) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    if (
        graph_plan is None
        or getattr(graph_plan, "original_question", None) != question
        or getattr(graph_plan, "strict_status", None) != "pass"
    ):
        return _hold("xlsx_version_diff_graph_plan_not_certified")
    branches = getattr(graph_plan, "branch_intents", ())
    if not isinstance(branches, tuple) or len(branches) != 1:
        return _hold("xlsx_version_diff_graph_plan_not_certified")
    branch = branches[0]
    if not isinstance(branch, Mapping) or branch.get("status") != "resolved":
        return _hold("xlsx_version_diff_graph_plan_not_certified")
    intent = branch.get("intent")
    supplied = (
        intent.get("extended_graph_contract")
        if isinstance(intent, Mapping)
        else None
    )
    if (
        not isinstance(supplied, Mapping)
        or not validate_graph_contract(question, supplied)
        or _canonical_json(supplied) != _canonical_json(contract)
    ):
        return _hold("xlsx_version_diff_graph_plan_contract_mismatch")
    return decide_question(engine, question)


__all__ = [
    "XLSX_VERSION_DIFF_RULE_VERSION",
    "decide_from_graph",
    "decide_question",
    "graph_contract_for_question",
    "validate_graph_contract",
]
