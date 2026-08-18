"""Certify axis tick questions from the notebook's embedded rendered PNG."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping

from cross_document_finance_rules import _fingerprint
from notebook_correlation_rules import _sources
from notebook_version_diff_rules import _decode_png
from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision

VERSION = "0.1"
QUESTION = "蒼泉会 ひがし丘総合病院の01_eda.ipynbにおける目的変数分析の可視化において、y軸に実際に表示されている目盛りの最大値は何ですか。"
_PSMS = (3, 11, 12)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if question != QUESTION:
        return None
    operators = (
        "bind_glossary_location",
        "bind_unique_notebook",
        "parse_bounded_notebook_json",
        "bind_unique_target_distribution_cell",
        "bind_embedded_display_png",
        "validate_png_structure_and_limits",
        "ocr_rendered_axis_with_multiple_layout_modes",
        "extract_positive_y_axis_tick_sequence",
        "require_two_complete_sequences_and_third_maximum_corroboration",
        "project_maximum_displayed_tick",
    )
    nodes = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
        previous = output
    core = {
        "graph_rule_version": VERSION,
        "rule_id": "notebook_embedded_target_chart_maximum_displayed_y_tick",
        "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
        "bindings": {},
        "scope": {"source_channel": "notebook_embedded_rendered_png", "question_independent": True, "ambiguity_policy": "hold"},
        "operation_graph": {"external_inputs": [{"input_ref": "input_question", "input_type": "notebook_and_glossary", "source": "question_scope"}], "nodes": nodes, "edges": [{"from": nodes[i - 1]["output_ref"], "to": nodes[i]["operation_id"]} for i in range(1, len(nodes))]},
        "requested_output": {"source_operation_ref": nodes[-1]["operation_id"], "cardinality": "single", "answer_shape": {"container": "scalar", "value_type": "integer", "unit": None}, "display_precision": 0, "required_keys": None},
    }
    return {"graph_contract_id": "notebook_axis_tick_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and _canonical(expected) == _canonical(contract)


def _embedded_target_png(path: Path) -> bytes:
    raw = path.read_bytes()
    if not 0 < len(raw) <= 20 * 1024 * 1024:
        raise ValueError("notebook resource limit")
    notebook = json.loads(raw.decode("utf-8"))
    cells = notebook.get("cells")
    if not isinstance(cells, list) or not 1 <= len(cells) <= 500:
        raise ValueError("notebook cells invalid")
    matched = []
    for cell in cells:
        if cell.get("cell_type") != "code":
            continue
        source = "".join(cell.get("source", ()))
        required = ("target_col", "目的変数の分布", "plt.ylabel('件数')", "target_distribution.png")
        if not all(token in source for token in required):
            continue
        outputs = cell.get("outputs")
        if not isinstance(outputs, list) or any(output.get("output_type") == "error" for output in outputs):
            raise ValueError("target cell output invalid")
        images = [output.get("data", {}).get("image/png") for output in outputs if isinstance(output, dict) and output.get("data", {}).get("image/png") is not None]
        if len(images) != 1:
            raise ValueError("target PNG not unique")
        payload = "".join(images[0]) if isinstance(images[0], list) else images[0]
        if not isinstance(payload, str) or not 0 < len(payload) <= 8 * 1024 * 1024:
            raise ValueError("PNG payload invalid")
        try:
            decoded = base64.b64decode(payload, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("PNG base64 invalid") from exc
        _decode_png(decoded)
        matched.append(decoded)
    if len(matched) != 1:
        raise ValueError("target distribution cell not unique")
    return matched[0]


def _ocr(png: bytes, psm: int) -> str:
    executable = shutil.which("tesseract")
    if executable is None or psm not in _PSMS:
        raise ValueError("OCR unavailable")
    completed = subprocess.run([executable, "stdin", "stdout", "-l", "jpn+eng", "--oem", "1", "--psm", str(psm)], input=png, capture_output=True, check=False, timeout=20)
    if completed.returncode != 0 or not completed.stdout or len(completed.stdout) > 64 * 1024:
        raise ValueError("OCR failed")
    return completed.stdout.decode("utf-8", errors="strict")


def _maximum_tick(png: bytes) -> int:
    readings = {}
    for psm in _PSMS:
        values = tuple(sorted({int(token) for token in re.findall(r"(?<![0-9])[1-9][0-9]{2,4}(?![0-9])", _ocr(png, psm))}))
        if not 4 <= len(values) <= 20:
            raise ValueError("displayed tick sequence not certified")
        readings[psm] = values
    complete = readings[3]
    if readings[12] != complete:
        raise ValueError("complete OCR readings disagree")
    intervals = {right - left for left, right in zip(complete, complete[1:])}
    if len(intervals) != 1 or next(iter(intervals)) <= 0:
        raise ValueError("displayed ticks are not an arithmetic sequence")
    corroborating = readings[11]
    if not set(corroborating).issubset(complete) or corroborating[-1] != complete[-1]:
        raise ValueError("third OCR mode does not corroborate the maximum")
    return complete[-1]


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    try:
        root = Path(engine.source_root).resolve()
        glossary, notebook, _csv = _sources(engine, root)
        maximum = _maximum_tick(_embedded_target_png(notebook))
        paths, digest = _fingerprint((glossary, notebook), root)
        result = StructuredCandidateAnswer(f"{maximum:,}", paths, digest, len(contract["operation_graph"]["nodes"]), 1)
        return StructuredCandidateDecision("resolved", "certified_notebook_embedded_axis_ticks", result)
    except (json.JSONDecodeError, OSError, subprocess.SubprocessError, TypeError, UnicodeError, ValueError):
        return StructuredCandidateDecision("hold", "notebook_axis_ticks_not_certified")


__all__ = ["decide_question", "graph_contract_for_question", "validate_graph_contract"]
