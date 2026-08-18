from __future__ import annotations

import copy
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from xml.sax.saxutils import escape, quoteattr

from xlsx_version_diff_rules import (
    decide_from_graph,
    decide_question,
    graph_contract_for_question,
    validate_graph_contract,
)


HEADERS = (
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


def _column_name(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _styles() -> str:
    xfs = "".join(
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        for _ in range(8)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border/></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        f'<cellXfs count="8">{xfs}</cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '<dxfs count="0"/>'
        '</styleSheet>'
    )


def _cell(ref: str, value: object, style: int, formula: bool = False) -> str:
    if formula:
        return f'<c r={quoteattr(ref)} s={quoteattr(str(style))}><f>1+1</f><v>2</v></c>'
    if isinstance(value, int):
        return f'<c r={quoteattr(ref)} s={quoteattr(str(style))}><v>{value}</v></c>'
    text = escape(str(value))
    return (
        f'<c r={quoteattr(ref)} s={quoteattr(str(style))} t="inlineStr">'
        f'<is><t xml:space="preserve">{text}</t></is></c>'
    )


def _write_workbook(
    path: Path,
    records: list[dict[str, object]],
    *,
    columns: tuple[str, ...] = HEADERS,
    header_row: int = 1,
    style_seed: int = 0,
    duplicate_header: bool = False,
    formula: tuple[str, str] | None = None,
    hidden_task: str | None = None,
) -> None:
    rows: list[str] = []

    def rendered_row(row_number: int, values: list[tuple[str, object]], task: str | None) -> str:
        cells = []
        for column_number, (field, value) in enumerate(values, 1):
            ref = f"{_column_name(column_number)}{row_number}"
            is_formula = formula == (task, field)
            cells.append(
                _cell(ref, value, (row_number + column_number + style_seed) % 8, is_formula)
            )
        hidden = (
            ' hidden="1"'
            if hidden_task is not None and task == hidden_task
            else ""
        )
        return f'<row r="{row_number}"{hidden}>{"".join(cells)}</row>'

    rows.append(rendered_row(header_row, list(zip(columns, columns)), None))
    for offset, record in enumerate(records, 1):
        values = [(field, record.get(field, "")) for field in columns]
        rows.append(
            rendered_row(
                header_row + offset,
                values,
                str(record.get("タスクID", "")),
            )
        )
    if duplicate_header:
        duplicate_row = header_row + len(records) + 2
        rows.append(rendered_row(duplicate_row, list(zip(columns, columns)), None))

    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" state="frozen"/></sheetView></sheetViews>'
        f'<sheetData>{"".join(rows)}</sheetData>'
        '</worksheet>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<workbookPr date1904="0"/><sheets>'
        '<sheet name="工程" sheetId="1" r:id="rId1"/>'
        '</sheets></workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        '</Relationships>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
        archive.writestr("xl/styles.xml", _styles())


def _records(status: str, second_assignee: str = "担当乙") -> list[dict[str, object]]:
    result = []
    for number, task_id, task_name, assignee in (
        (1, "TASK-A", "第1工程", "担当甲"),
        (2, "TASK-B", "第2工程", second_assignee),
    ):
        result.append(
            {
                "No.": number,
                "タスクID": task_id,
                "依存タスク": "-" if number == 1 else "TASK-A",
                "ステータス": status,
                "フェーズ": "P1",
                "タスク名": task_name,
                "詳細・内容": f"{task_name}の実施",
                "クリティカルパス": "○" if number == 1 else "",
                "マイルストーン": "MS1" if number == 1 else "",
                "チェックポイント": "CP1" if number == 1 else "",
                "成果物": f"成果物{number}",
                "開始日": 45000 + number,
                "終了日": 45001 + number,
                "担当者": assignee,
                "備考": "",
            }
        )
    return result


class XlsxVersionDiffRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "プロジェクト" / "匿名案件" / "02.計画"
        self.project.mkdir(parents=True)
        self.left = self.project / "工程_r1.xlsx"
        self.right = self.project / "工程_r2.xlsx"
        self.question = (
            "匿名案件の工程_r1.xlsxと工程_r2.xlsxを比較したとき、"
            "未着手から完了への変更を除いて、"
            "案件遂行に関連する変更点を挙げてください。"
        )
        self.engine = SimpleNamespace(
            source_root=self.root,
            glossary=SimpleNamespace(entries={}),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_positive(self) -> None:
        _write_workbook(self.left, _records("未着手"), header_row=3, style_seed=1)
        reordered = (
            "No.",
            "フェーズ",
            "タスクID",
            "タスク名",
            "詳細・内容",
            "担当者",
            "開始日",
            "終了日",
            "依存タスク",
            "成果物",
            "ステータス",
            "クリティカルパス",
            "マイルストーン",
            "チェックポイント",
            "備考",
        )
        after = _records("完了", "担当乙 / 担当丙")
        _write_workbook(
            self.right,
            list(reversed(after)),
            columns=reordered,
            header_row=1,
            style_seed=5,
        )

    def test_contract_is_deterministic_and_tamper_evident(self) -> None:
        first = graph_contract_for_question(self.question)
        second = graph_contract_for_question(self.question)
        self.assertEqual(first, second)
        self.assertIsNotNone(first)
        self.assertTrue(first["graph_contract_id"].startswith("xlsx_version_diff_"))
        self.assertTrue(validate_graph_contract(self.question, first))
        tampered = copy.deepcopy(first)
        tampered["bindings"]["exclude_to"] = "進行中"
        self.assertFalse(validate_graph_contract(self.question, tampered))
        reversed_question = self.question.replace("工程_r1.xlsxと工程_r2.xlsx", "工程_r2.xlsxと工程_r1.xlsx")
        self.assertIsNone(graph_contract_for_question(reversed_question))

    def test_semantic_diff_ignores_layout_style_and_row_order(self) -> None:
        self._write_positive()
        decision = decide_question(self.engine, self.question)
        self.assertEqual(decision.status, "resolved")
        self.assertEqual(decision.reason, "certified_xlsx_version_diff")
        self.assertEqual(
            decision.result.answer,
            "TASK-B（第2工程）の担当者: 担当乙 → 担当乙 / 担当丙（追加: 担当丙）",
        )
        self.assertEqual(len(decision.result.source_paths), 2)
        self.assertEqual(decision.result.operation_count, 7)

    def test_live_graph_plan_requires_exact_contract(self) -> None:
        self._write_positive()
        contract = graph_contract_for_question(self.question)
        plan = SimpleNamespace(
            original_question=self.question,
            strict_status="pass",
            branch_intents=(
                {
                    "status": "resolved",
                    "intent": {"extended_graph_contract": contract},
                },
            ),
        )
        self.assertEqual(
            decide_from_graph(self.engine, self.question, plan).status,
            "resolved",
        )
        plan.strict_status = "fail"
        self.assertEqual(
            decide_from_graph(self.engine, self.question, plan).reason,
            "xlsx_version_diff_graph_plan_not_certified",
        )
        plan.strict_status = "pass"
        bad_contract = copy.deepcopy(contract)
        bad_contract["scope"]["identity_field"] = "No."
        plan.branch_intents = (
            {
                "status": "resolved",
                "intent": {"extended_graph_contract": bad_contract},
            },
        )
        self.assertEqual(
            decide_from_graph(self.engine, self.question, plan).reason,
            "xlsx_version_diff_graph_plan_contract_mismatch",
        )

    def test_duplicate_task_id_fails_closed(self) -> None:
        before = _records("未着手")
        before.append(dict(before[0], **{"No.": 3}))
        _write_workbook(self.left, before)
        _write_workbook(self.right, _records("完了", "担当乙 / 担当丙"))
        self.assertEqual(decide_question(self.engine, self.question).status, "hold")

    def test_duplicate_or_missing_header_fails_closed(self) -> None:
        _write_workbook(self.left, _records("未着手"), duplicate_header=True)
        _write_workbook(self.right, _records("完了", "担当乙 / 担当丙"))
        self.assertEqual(decide_question(self.engine, self.question).status, "hold")
        _write_workbook(self.left, _records("未着手"), columns=HEADERS[:-1])
        self.assertEqual(decide_question(self.engine, self.question).status, "hold")

    def test_formula_or_hidden_task_fails_closed(self) -> None:
        _write_workbook(
            self.left,
            _records("未着手"),
            formula=("TASK-A", "担当者"),
        )
        _write_workbook(self.right, _records("完了", "担当乙 / 担当丙"))
        self.assertEqual(decide_question(self.engine, self.question).status, "hold")
        _write_workbook(
            self.left,
            _records("未着手"),
            hidden_task="TASK-A",
        )
        self.assertEqual(decide_question(self.engine, self.question).status, "hold")

    def test_ambiguous_source_or_task_set_fails_closed(self) -> None:
        self._write_positive()
        duplicate = self.root / "複製" / "匿名案件"
        duplicate.mkdir(parents=True)
        _write_workbook(duplicate / "工程_r1.xlsx", _records("未着手"))
        _write_workbook(duplicate / "工程_r2.xlsx", _records("完了"))
        self.assertEqual(decide_question(self.engine, self.question).status, "hold")

        for path in duplicate.glob("*.xlsx"):
            path.unlink()
        after = _records("完了", "担当乙 / 担当丙")
        after[1]["タスクID"] = "TASK-C"
        _write_workbook(self.right, after)
        self.assertEqual(decide_question(self.engine, self.question).status, "hold")

    def test_no_remaining_change_fails_closed(self) -> None:
        _write_workbook(self.left, _records("未着手"))
        _write_workbook(self.right, _records("完了"))
        decision = decide_question(self.engine, self.question)
        self.assertEqual(decision.status, "hold")
        self.assertEqual(decision.reason, "xlsx_version_diff_source_not_certified")

    def test_declared_transition_is_excluded_only_from_status(self) -> None:
        before = _records("未着手")
        after = _records("完了")
        before[0]["タスク名"] = "未着手"
        after[0]["タスク名"] = "完了"
        _write_workbook(self.left, before)
        _write_workbook(self.right, after)
        decision = decide_question(self.engine, self.question)
        self.assertEqual(decision.status, "resolved")
        self.assertIn("タスク名: 未着手 → 完了", decision.result.answer)


if __name__ == "__main__":
    unittest.main()
