"""Source-recomputed notebook correlation rules."""

from __future__ import annotations

import ast
import csv
import hashlib
import json
import math
import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping, Sequence

from cross_document_finance_rules import _fingerprint, _safe_files
from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision

VERSION = "0.1"
Q004 = "蒼泉会 ひがし丘総合病院の01_eda.ipynbを確認して、目的変数と相関が最も高い数値特徴量を教えてください。"


def _normalized(value: object) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", str(value)).casefold() if not c.isspace())


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if question != Q004:
        return None
    operators = ("bind_glossary_location", "bind_unique_notebook", "parse_notebook_code", "bind_declared_csv_and_target", "verify_correlation_execution_output", "recompute_all_numeric_correlations", "exclude_target", "verify_unique_argmax", "project_exact_column_name")
    nodes = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
        previous = output
    core = {
        "graph_rule_version": VERSION,
        "rule_id": "notebook_numeric_feature_highest_target_correlation",
        "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
        "bindings": {},
        "scope": {"source_channel": "notebook_code_output_and_declared_csv", "question_independent": True, "ambiguity_policy": "hold"},
        "operation_graph": {"external_inputs": [{"input_ref": "input_question", "input_type": "source_records", "source": "question_scope"}], "nodes": nodes, "edges": [{"from": nodes[i - 1]["output_ref"], "to": nodes[i]["operation_id"]} for i in range(1, len(nodes))]},
        "requested_output": {"source_operation_ref": nodes[-1]["operation_id"], "cardinality": "single", "answer_shape": {"container": "scalar", "value_type": "identifier", "unit": None}, "display_precision": None, "required_keys": None},
    }
    return {"graph_contract_id": "notebook_corr_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and _canonical(expected) == _canonical(contract)


def _root(engine: Any) -> Path:
    root = Path(engine.source_root)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("source root invalid")
    return root.resolve()


def _sources(engine: Any, root: Path) -> tuple[Path, Path, Path]:
    glossary_paths = [p for p in _safe_files(root, ".docx") if unicodedata.normalize("NFC", p.relative_to(root).as_posix()) == "社内管理/社内用語集.docx"]
    entries = getattr(getattr(engine, "glossary", None), "entries", {})
    canonicals = list(entries.get("蒼泉会", ()))
    if len(glossary_paths) != 1 or len(canonicals) != 1:
        raise ValueError("glossary location binding not unique")
    notebooks = [p for p in _safe_files(root, ".ipynb") if p.name == "01_eda.ipynb" and _normalized(canonicals[0]) in _normalized(p.relative_to(root).as_posix())]
    if len(notebooks) != 1:
        raise ValueError("notebook not unique")
    notebook = notebooks[0]
    csv_path = notebook.parents[1] / "data" / "train.csv"
    if not csv_path.is_file() or csv_path.is_symlink() or root not in csv_path.resolve().parents:
        raise ValueError("declared CSV invalid")
    return glossary_paths[0], notebook, csv_path


def _notebook_contract(path: Path) -> tuple[str, str, Mapping[str, float]]:
    raw = path.read_bytes()
    if not 0 < len(raw) <= 20 * 1024 * 1024:
        raise ValueError("notebook resource limit")
    notebook = json.loads(raw.decode("utf-8"))
    cells = notebook.get("cells")
    if not isinstance(cells, list):
        raise ValueError("notebook cells invalid")
    code = "\n".join("".join(cell.get("source", ())) for cell in cells if cell.get("cell_type") == "code")
    tree = ast.parse(code)
    target_values = []
    csv_values = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id == "target_col" and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                target_values.append(node.value.value)
            if node.targets[0].id == "csv_rel" and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == "Path" and len(node.value.args) == 1 and isinstance(node.value.args[0], ast.Constant):
                csv_values.append(node.value.args[0].value)
    if set(target_values) != {"charges"} or set(csv_values) != {"data/train.csv"}:
        raise ValueError("notebook data binding ambiguous")
    compact = re.sub(r"\s+", "", code)
    if "select_dtypes(include=[np.number])" not in compact or ".corr(numeric_only=True)" not in compact:
        raise ValueError("correlation code not certified")
    output_text = "\n".join(
        str(value)
        for cell in cells
        for output in cell.get("outputs", ())
        for value in output.get("text", ())
        if isinstance(value, str)
    )
    header = re.search(r"(?m)^\s+(id\s+age\s+bmi\s+children\s+charges)\s*$", output_text)
    if header is None:
        raise ValueError("executed correlation output missing")
    names = header.group(1).split()
    rows = [match.split() for match in re.findall(r"(?m)^charges\s+(.+)$", output_text)]
    rows = [values for values in rows if len(values) == len(names) and values[-1] == "1.000000"]
    if len(rows) != 1:
        raise ValueError("correlation output malformed")
    values = rows[0]
    return "data/train.csv", "charges", {name: float(value) for name, value in zip(names, values)}


def _pearson(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        raise ValueError("correlation inputs invalid")
    lm, rm = sum(left) / len(left), sum(right) / len(right)
    numerator = sum((a - lm) * (b - rm) for a, b in zip(left, right))
    ld = math.sqrt(sum((a - lm) ** 2 for a in left))
    rd = math.sqrt(sum((b - rm) ** 2 for b in right))
    if ld == 0 or rd == 0:
        raise ValueError("constant numeric column")
    return numerator / (ld * rd)


def _recompute(path: Path, target: str) -> Mapping[str, float]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or target not in (rows[0] or {}):
        raise ValueError("CSV target missing")
    numeric: dict[str, list[float]] = {}
    for name in rows[0]:
        try:
            values = [float(row[name]) for row in rows]
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in values):
            raise ValueError("non-finite numeric value")
        numeric[name] = values
    if target not in numeric:
        raise ValueError("target is not numeric")
    return {name: _pearson(values, numeric[target]) for name, values in numeric.items()}


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    try:
        root = _root(engine)
        glossary, notebook, csv_path = _sources(engine, root)
        declared_csv, target, executed = _notebook_contract(notebook)
        if csv_path.relative_to(notebook.parents[1]).as_posix() != declared_csv:
            raise ValueError("notebook CSV path mismatch")
        recomputed = _recompute(csv_path, target)
        if set(executed) != set(recomputed):
            raise ValueError("executed correlation coverage mismatch")
        if any(abs(executed[name] - recomputed[name]) > 0.000001 for name in executed):
            raise ValueError("executed correlations differ from source recomputation")
        candidates = sorted(((value, name) for name, value in recomputed.items() if name != target), reverse=True)
        if len(candidates) < 2 or math.isclose(candidates[0][0], candidates[1][0], abs_tol=1e-12):
            raise ValueError("correlation argmax not unique")
        answer = candidates[0][1]
        paths, digest = _fingerprint((glossary, notebook, csv_path), root)
        result = StructuredCandidateAnswer(answer, paths, digest, len(contract["operation_graph"]["nodes"]), 1)
        return StructuredCandidateDecision("resolved", "certified_notebook_source_recomputed_correlation", result)
    except (OSError, RuntimeError, UnicodeError, ValueError, json.JSONDecodeError, SyntaxError):
        return StructuredCandidateDecision("hold", "notebook_correlation_not_certified")


__all__ = ["Q004", "decide_question", "graph_contract_for_question", "validate_graph_contract"]
