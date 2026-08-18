"""Certify notebook date-chart maxima from code, CSV, and the saved PNG."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import subprocess
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from cross_document_finance_rules import _fingerprint
from cross_document_finance_rules import _safe_files
from notebook_version_diff_rules import _decode_png
from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision

VERSION = "0.1"
QUESTION = "京橋信用ソリューションズのEDAの日付分析の可視化において、件数が最も高いのは何日ですか。"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if question != QUESTION:
        return None
    operators = (
        "bind_glossary_location",
        "bind_unique_notebook_and_declared_csv",
        "bind_date_analysis_code_cell",
        "verify_day_column_selection",
        "verify_groupby_size_to_count_line",
        "bind_saved_date_chart_png",
        "validate_png_and_ocr_chart_title",
        "count_csv_rows_by_day",
        "require_unique_count_argmax",
        "project_day_of_month",
    )
    nodes = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
        previous = output
    core = {
        "graph_rule_version": VERSION,
        "rule_id": "notebook_date_count_chart_unique_maximum_day",
        "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
        "bindings": {},
        "scope": {"source_channel": "notebook_code_saved_png_and_declared_csv", "question_independent": True, "ambiguity_policy": "hold"},
        "operation_graph": {"external_inputs": [{"input_ref": "input_question", "input_type": "notebook_png_csv", "source": "question_scope"}], "nodes": nodes, "edges": [{"from": nodes[i - 1]["output_ref"], "to": nodes[i]["operation_id"]} for i in range(1, len(nodes))]},
        "requested_output": {"source_operation_ref": nodes[-1]["operation_id"], "cardinality": "single", "answer_shape": {"container": "scalar", "value_type": "integer", "unit": "日"}, "display_precision": 0, "required_keys": None},
    }
    return {"graph_contract_id": "notebook_date_chart_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and _canonical(expected) == _canonical(contract)


def _sources(engine: Any, root: Path) -> tuple[Path, Path, Path]:
    glossary_paths = [path for path in _safe_files(root, ".docx") if path.relative_to(root).as_posix() == "社内管理/社内用語集.docx"]
    hits = getattr(engine, "glossary", None).lookup("京橋信用ソリューションズ")
    canonicals = [canonical for alias, values in hits if alias == "京橋" for canonical in values]
    if len(glossary_paths) != 1 or canonicals != ["京橋信用ソリューションズ株式会社"]:
        raise ValueError("glossary binding not unique")
    canonical_project = unicodedata.normalize("NFC", canonicals[0])
    notebooks = [path for path in _safe_files(root, ".ipynb") if path.name == "01_eda.ipynb" and canonical_project in unicodedata.normalize("NFC", path.relative_to(root).as_posix())]
    if len(notebooks) != 1:
        raise ValueError("notebook not unique")
    notebook = notebooks[0]
    csv_path = notebook.parents[1] / "data" / "train.csv"
    if not csv_path.is_file() or csv_path.is_symlink() or root not in csv_path.resolve().parents:
        raise ValueError("declared CSV invalid")
    return glossary_paths[0], notebook, csv_path


def _date_contract(notebook: Path) -> tuple[str, str]:
    raw = notebook.read_bytes()
    if not 0 < len(raw) <= 20 * 1024 * 1024:
        raise ValueError("notebook resource limit")
    data = json.loads(raw.decode("utf-8"))
    cells = data.get("cells")
    if not isinstance(cells, list) or not 1 <= len(cells) <= 500:
        raise ValueError("notebook cells invalid")
    code = "\n".join("".join(cell.get("source", ())) for cell in cells if cell.get("cell_type") == "code")
    date_hints = re.findall(r"(?m)^date_col_hint\s*=\s*['\"]([^'\"]+)['\"]\.strip\(\)\s+or\s+None\s*$", code)
    targets = re.findall(r"(?m)^target_col\s*=\s*['\"]([^'\"]+)['\"]\s*$", code)
    if date_hints != ["day"] or targets != ["y"]:
        raise ValueError("notebook column bindings invalid")
    date_cells = ["".join(cell.get("source", ())) for cell in cells if cell.get("cell_type") == "code" and "figure_06.png" in "".join(cell.get("source", ()))]
    if len(date_cells) != 1:
        raise ValueError("date analysis cell not unique")
    compact = re.sub(r"\s+", "", date_cells[0])
    required = (
        "used_col=date_col_hint",
        "tmp.groupby(used_col).agg(件数=(target_col,'size'))",
        "ax1.plot(agg.index,agg['件数']",
        "ax1.set_xlabel('日')",
        "ax1.set_ylabel('件数'",
        "plt.savefig(FIG_DIR/'figure_06.png'",
    )
    if any(token not in compact for token in required):
        raise ValueError("date chart data flow not certified")
    return date_hints[0], targets[0]


def _figure(notebook: Path) -> Path:
    path = notebook.parents[1] / "reports" / "figures" / "figure_06.png"
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("date figure invalid")
    raw = path.read_bytes()
    _decode_png(raw)
    executable = shutil.which("tesseract")
    if executable is None:
        raise ValueError("OCR unavailable")
    completed = subprocess.run([executable, "stdin", "stdout", "-l", "jpn+eng", "--oem", "1", "--psm", "11"], input=raw, capture_output=True, check=False, timeout=20)
    if completed.returncode != 0 or not completed.stdout or len(completed.stdout) > 64 * 1024:
        raise ValueError("figure OCR failed")
    title = "".join(completed.stdout.decode("utf-8", errors="strict").split())
    if "dayによる件数推移" not in title or "日" not in title:
        raise ValueError("date figure title not certified")
    return path


def _unique_maximum_day(csv_path: Path, day_column: str, target_column: str) -> int:
    counts: Counter[int] = Counter()
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or day_column not in reader.fieldnames or target_column not in reader.fieldnames:
            raise ValueError("CSV columns missing")
        for index, row in enumerate(reader, 1):
            if index > 1_000_000 or row.get(target_column) is None:
                raise ValueError("CSV row invalid")
            token = row.get(day_column, "")
            if not re.fullmatch(r"[0-9]{1,2}", token):
                raise ValueError("day value invalid")
            day = int(token)
            if not 1 <= day <= 31:
                raise ValueError("day outside calendar range")
            counts[day] += 1
    if not counts:
        raise ValueError("CSV empty")
    maximum = max(counts.values())
    winners = [day for day, count in counts.items() if count == maximum]
    if len(winners) != 1:
        raise ValueError("count maximum not unique")
    return winners[0]


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    try:
        root = Path(engine.source_root).resolve()
        glossary, notebook, csv_path = _sources(engine, root)
        day_column, target_column = _date_contract(notebook)
        figure = _figure(notebook)
        day = _unique_maximum_day(csv_path, day_column, target_column)
        paths, digest = _fingerprint((glossary, notebook, csv_path, figure), root)
        result = StructuredCandidateAnswer(f"{day}日", paths, digest, len(contract["operation_graph"]["nodes"]), 1)
        return StructuredCandidateDecision("resolved", "certified_notebook_date_chart_maximum", result)
    except (csv.Error, json.JSONDecodeError, OSError, subprocess.SubprocessError, TypeError, UnicodeError, ValueError):
        return StructuredCandidateDecision("hold", "notebook_date_chart_not_certified")


__all__ = ["decide_question", "graph_contract_for_question", "validate_graph_contract"]
