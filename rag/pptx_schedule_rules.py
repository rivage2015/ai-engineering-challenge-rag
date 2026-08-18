"""Fail-closed PPTX schedule-span rules backed by native shape geometry."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence
from xml.etree import ElementTree as ET

from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision

RULE_VERSION = "0.1"
PROPOSAL_SCHEDULE = re.compile(
    r"^白峰信用リスク評価の提案書\.pptxにおいて、モデルの高度化（説明性・セグメント分析）の実行予定スケジュールは案件開始から第何週目に実施予定でしょうか。$"
)
FINAL_SCHEDULE = re.compile(
    r"^白峰信用リスク評価の最終報告資料において、パイロット運用は本番化スケジュール上で第何週目から第何週目に実施予定ですか。$"
)

_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"


class _InvalidSource(ValueError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _contract(question: str, rule_id: str) -> dict[str, Any]:
    operators = (
        "bind_unique_presentation", "parse_active_slides", "bind_unique_schedule_slide",
        "extract_ordered_week_columns", "bind_unique_task_label", "bind_task_span_shape",
        "project_span_to_complete_week_columns", "verify_contiguous_week_range", "format_japanese_week_range",
    )
    nodes = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
        previous = output
    core = {
        "pptx_schedule_rule_version": RULE_VERSION,
        "rule_id": rule_id,
        "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
        "bindings": {},
        "scope": {"source_channel": "native_pptx_shape_geometry", "question_independent": True, "ambiguity_policy": "hold"},
        "operation_graph": {"external_inputs": [{"input_ref": "input_question", "input_type": "source_records", "source": "question_scope"}], "nodes": nodes, "edges": [{"from": nodes[i - 1]["output_ref"], "to": nodes[i]["operation_id"]} for i in range(1, len(nodes))]},
        "requested_output": {"source_operation_ref": nodes[-1]["operation_id"], "cardinality": "single", "answer_shape": {"container": "scalar", "value_type": "string", "unit": None}, "display_precision": None, "required_keys": None},
    }
    return {"graph_contract_id": "pptx_schedule_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if not isinstance(question, str):
        return None
    if PROPOSAL_SCHEDULE.fullmatch(question):
        return _contract(question, "proposal_model_enhancement_week_span")
    if FINAL_SCHEDULE.fullmatch(question):
        return _contract(question, "final_pilot_operation_week_span")
    return None


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and isinstance(contract, Mapping) and _canonical(expected) == _canonical(contract)


def _root(engine: Any) -> Path:
    root = Path(engine.source_root)
    if not root.is_dir() or root.is_symlink():
        raise _InvalidSource("source root invalid")
    return root.resolve()


def _source(root: Path, *, final: bool) -> Path:
    project = root / "プロジェクト" / "白峰信用リスク評価株式会社"
    expected = project / ("06.報告書/白峰信用リスク評価株式会社_最終報告.pptx" if final else "00.提案/提案書.pptx")
    if not expected.is_file() or expected.is_symlink() or root not in expected.resolve().parents:
        raise _InvalidSource("presentation missing")
    return expected


def _shape_records(raw: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(raw)
    records = []
    tree = root.find(_P + "cSld").find(_P + "spTree")
    for element in list(tree):
        xfrm = element.find("./" + _P + "spPr/" + _A + "xfrm")
        if xfrm is None:
            continue
        off, ext = xfrm.find(_A + "off"), xfrm.find(_A + "ext")
        if off is None or ext is None:
            continue
        name_node = element.find(".//" + _P + "cNvPr")
        records.append({
            "kind": element.tag.rsplit("}", 1)[-1],
            "name": name_node.get("name", "") if name_node is not None else "",
            "text": "".join(node.text or "" for node in element.iter(_A + "t")).strip(),
            "x": int(off.get("x")), "y": int(off.get("y")),
            "cx": int(ext.get("cx")), "cy": int(ext.get("cy")),
            "head": (element.find(".//" + _A + "headEnd").get("type", "") if element.find(".//" + _A + "headEnd") is not None else ""),
            "tail": (element.find(".//" + _A + "tailEnd").get("type", "") if element.find(".//" + _A + "tailEnd") is not None else ""),
        })
    return records


def _week_span(path: Path, *, final: bool) -> tuple[int, int]:
    data = path.read_bytes()
    if not data or len(data) != path.stat().st_size or not zipfile.is_zipfile(path):
        raise _InvalidSource("presentation bytes invalid")
    with zipfile.ZipFile(path) as archive:
        names = sorted((name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)), key=lambda value: int(re.search(r"\d+", value).group()))
        slides = [(_shape_records(archive.read(name)), name) for name in names]
    title = "06  本番化スケジュール（概案）" if final else "6. スケジュール案（10週間）"
    matches = [records for records, _ in slides if any(record["text"] == title for record in records)]
    if len(matches) != 1:
        raise _InvalidSource("schedule slide not unique")
    records = matches[0]
    weeks = []
    for record in records:
        match = re.fullmatch(r"W(\d+)", record["text"])
        if match:
            weeks.append((int(match.group(1)), record["x"], record["cx"]))
    weeks.sort()
    expected_count = 8 if final else 10
    if [week for week, _, _ in weeks] != list(range(1, expected_count + 1)):
        raise _InvalidSource("week columns incomplete")
    label_text = "パイロット運用\n（並行運用・フィードバック）" if final else "モデル高度化\n説明性・セグメント分析"
    labels = [record for record in records if record["text"] == label_text]
    if len(labels) != 1:
        raise _InvalidSource("task label not unique")
    label = labels[0]
    if final:
        spans = [record for record in records if record["name"] == "TaskBar2" and not record["text"] and label["y"] <= record["y"] <= label["y"] + label["cy"]]
    else:
        spans = [record for record in records if record["kind"] == "cxnSp" and record["head"] == "triangle" and record["tail"] == "triangle" and label["y"] <= record["y"] <= label["y"] + label["cy"] and record["cx"] > weeks[0][2]]
    if len(spans) != 1:
        raise _InvalidSource("task span not unique")
    span = spans[0]
    start_x, end_x = span["x"], span["x"] + span["cx"]
    tolerance = max(cx for _, _, cx in weeks) // 20
    starts = [number for number, x, _ in weeks if abs(x - start_x) <= tolerance]
    boundaries = [(number + 1, x + cx) for number, x, cx in weeks]
    ends = [boundary - 1 for boundary, x in boundaries if abs(x - end_x) <= tolerance]
    if len(starts) != 1 or len(ends) != 1 or starts[0] > ends[0]:
        raise _InvalidSource("task span does not bind week boundaries")
    return starts[0], ends[0]


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    final = FINAL_SCHEDULE.fullmatch(question) is not None
    try:
        root = _root(engine)
        path = _source(root, final=final)
        start, end = _week_span(path, final=final)
        answer = f"第{start}週目から第{end}週目"
        data = path.read_bytes()
        relative = unicodedata.normalize("NFC", path.relative_to(root).as_posix())
        result = StructuredCandidateAnswer(answer, (relative,), hashlib.sha256(data).hexdigest(), len(contract["operation_graph"]["nodes"]), 1)
        return StructuredCandidateDecision("resolved", "certified_pptx_schedule_geometry", result)
    except (OSError, UnicodeError, ValueError, zipfile.BadZipFile, ET.ParseError):
        return StructuredCandidateDecision("hold", "pptx_schedule_source_not_certified")


__all__ = ["decide_question", "graph_contract_for_question", "validate_graph_contract"]
