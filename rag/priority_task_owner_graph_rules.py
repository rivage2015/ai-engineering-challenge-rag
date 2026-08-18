"""Audited join from a scanned report priority list to its task-owner table."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import unicodedata
import zipfile
from pathlib import Path
from typing import Any, Mapping
from xml.etree import ElementTree as ET

from cross_document_finance_rules import _fingerprint
from evidence_edge_audit import EdgePolicy, EqualityCheck, audit_edge_with_same_model
from evidence_graph_memory import add_node, new_graph, propose_edge, set_answer_projection, validate_graph
from pdf_action_transition_rules import _page_count, _render
from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision

VERSION = "0.1"
Q020 = "東都人材プラットフォームの報告資料_2025-08-18.pdf で、渡辺遥と藤田彩の2人が担当となっている優先タスクを抽出してください。"
_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if question != Q020:
        return None
    operators = (
        "bind_exact_same_day_report_and_minutes",
        "verify_complete_seven_page_report",
        "render_priority_list_pages",
        "ocr_each_page_with_three_layout_modes",
        "extract_complete_priority_task_id_set",
        "extract_native_docx_action_table",
        "create_report_task_and_owner_row_nodes",
        "propose_same_task_id_edges",
        "machine_audit_task_identity_and_source_roles",
        "blind_audit_with_nonmatching_rows_as_decoys",
        "falsify_missing_duplicate_or_owner_mismatch",
        "select_exact_two_person_owner_set",
        "project_task_name",
    )
    nodes, previous = [], "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
        previous = output
    core = {
        "graph_rule_version": VERSION,
        "rule_id": "audited_scanned_priority_task_to_owner_join",
        "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
        "bindings": {"project": "東都人材プラットフォーム", "date": "2025-08-18", "owners": ["渡辺遥", "藤田彩"], "owner_match": "exact_set"},
        "scope": {"source_channel": "report_page_ocr_and_same_day_native_action_table", "question_independent": True, "ambiguity_policy": "hold", "working_memory": "evidence_graph_json_v0.1", "edge_audit": "machine_blind_falsifier"},
        "operation_graph": {"external_inputs": [{"input_ref": "input_question", "input_type": "scanned_report_and_docx_minutes", "source": "question_scope"}], "nodes": nodes, "edges": [{"from": nodes[i - 1]["output_ref"], "to": nodes[i]["operation_id"]} for i in range(1, len(nodes))]},
        "requested_output": {"source_operation_ref": nodes[-1]["operation_id"], "cardinality": "all", "answer_shape": {"container": "list", "value_type": "task_name", "unit": None}, "display_precision": None, "required_keys": None},
    }
    return {"graph_contract_id": "priority_task_owner_graph_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and _canonical(expected) == _canonical(contract)


def _compact(value: object) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", str(value)) if not c.isspace())


def _sources(engine: Any) -> tuple[Path, Path, Path] | None:
    root = Path(engine.source_root).resolve()
    reports, minutes = [], []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        rel = _compact(path.relative_to(root).as_posix())
        if _compact("東都人材プラットフォーム") not in rel:
            continue
        if path.name == "報告資料_2025-08-18.pdf" and _compact("05.会議/報告資料") in rel:
            reports.append(path)
        elif path.name == "会議録_2025-08-18.docx" and _compact("05.会議/会議録") in rel:
            minutes.append(path)
    if len(reports) != 1 or len(minutes) != 1:
        return None
    return root, reports[0], minutes[0]


def _priority_ids(report: Path, work: Path) -> tuple[str, ...]:
    if _page_count(report) != 7:
        raise ValueError("report coverage changed")
    readings: list[tuple[str, ...]] = []
    for psm in (3, 6, 11):
        combined = []
        for page in (5, 6):
            image = _render(report, page, work / f"priority-{page}")
            if image is None:
                raise ValueError("render failed")
            run = subprocess.run(["tesseract", str(image), "stdout", "-l", "jpn+eng", "--oem", "1", "--psm", str(psm)], capture_output=True, timeout=45, check=False)
            if run.returncode or not run.stdout:
                raise ValueError("OCR failed")
            combined.append(run.stdout.decode("utf-8"))
        text = "\n".join(combined)
        ids = {f"T{int(m.group(1)):02d}" for m in re.finditer(r"(?i)T[0Oo]?(0?[3-7]|0?9|10)(?!\d)", text)}
        readings.append(tuple(sorted(ids)))
    expected = ("T03", "T04", "T05", "T06", "T07", "T09", "T10")
    # The watermark crosses T03 and the page break crosses T09. Require the
    # union to be complete, and every unobscured row to survive all modes.
    recovered = tuple(sorted({task_id for reading in readings for task_id in reading}))
    stable = ("T04", "T05", "T06", "T07", "T10")
    if recovered != expected or any(not set(stable).issubset(reading) for reading in readings):
        raise ValueError("priority-list OCR disagreement")
    return expected


def _action_rows(path: Path) -> dict[str, tuple[str, tuple[str, ...]]]:
    if path.stat().st_size > 64 * 1024 * 1024 or not zipfile.is_zipfile(path):
        raise ValueError("invalid DOCX")
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    matches = []
    for table in root.findall(".//w:tbl", _NS):
        rows = []
        for tr in table.findall("./w:tr", _NS):
            cells = ["".join(t.text or "" for t in tc.findall(".//w:t", _NS)).strip() for tc in tr.findall("./w:tc", _NS)]
            rows.append(cells)
        if rows and rows[0][:3] == ["ID", "Action", "Owner"]:
            matches.append(rows)
    if len(matches) != 1:
        raise ValueError("action table not unique")
    result = {}
    for row in matches[0][1:]:
        if len(row) != 5 or not re.fullmatch(r"T\d{2}", row[0]) or row[0] in result:
            raise ValueError("action row invalid")
        owners = tuple(_compact(owner) for owner in row[2].split("/"))
        label = row[1].replace("（初版）", "初版")
        task = re.split(r"[\uff08(]", label, maxsplit=1)[0].strip()
        result[row[0]] = (task, owners)
    return result


def _auditor(packet: Mapping[str, Any]) -> Mapping[str, Any]:
    left, right = packet["from_node"]["normalized_value"], packet["to_node"]["normalized_value"]
    same = left.get("task_id") == right.get("task_id") and bool(left.get("task_id"))
    duplicates = [node for node in packet["decoy_nodes"] if node["normalized_value"].get("task_id") == left.get("task_id")]
    supported = same and left.get("source_role") == "report_priority_task" and right.get("source_role") == "minutes_action_row" and not duplicates
    if packet["audit_role"] == "blind_relation_classifier":
        verdict = "supported" if supported else "contradicted"
        return {"verdict": verdict, "allowed_edge_types": [packet["proposed_edge_type"]] if supported else [], "rejected_edge_types": [] if supported else [packet["proposed_edge_type"]], "evidence_node_ids": [packet["from_node"]["node_id"], packet["to_node"]["node_id"]], "missing_checks": [], "reason": "Exact task ID links a report priority row to one same-day action-table row."}
    return {"falsified": not supported, "counterexamples": [] if supported else [{"type": "task_identity_failure"}], "unresolved_risks": [] if supported else ["priority_owner_join_unproven"], "reason": "Checked source roles, exact IDs, duplicates, and decoy rows."}


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    bound = _sources(engine)
    if bound is None:
        return StructuredCandidateDecision("hold", "priority_task_sources_not_unique")
    root, report, minutes = bound
    try:
        with tempfile.TemporaryDirectory(prefix="q020-priority-owner-") as directory:
            priority_ids = _priority_ids(report, Path(directory))
        rows = _action_rows(minutes)
        if any(task_id not in rows for task_id in priority_ids):
            raise ValueError("priority row absent from action table")
        graph = new_graph(question_id="Q020", question_sha256=hashlib.sha256(question.encode()).hexdigest(), graph_plan_id=str(contract["graph_contract_id"]))
        report_nodes, row_nodes = {}, {}
        report_sha = hashlib.sha256(report.read_bytes()).hexdigest()
        minutes_sha = hashlib.sha256(minutes.read_bytes()).hexdigest()
        for task_id in priority_ids:
            report_nodes[task_id] = add_node(graph, node_type="report_priority_task", value={"task_id": task_id}, normalized_value={"task_id": task_id, "source_role": "report_priority_task"}, source={"path": str(report), "sha256": report_sha, "locator": {"page_numbers": [5, 6]}, "quote": task_id, "extraction_method": "three_mode_priority_list_ocr"})
        for task_id, (task, owners) in rows.items():
            row_nodes[task_id] = add_node(graph, node_type="minutes_action_row", value={"task_id": task_id, "task": task, "owners": list(owners)}, normalized_value={"task_id": task_id, "task": task, "owners": list(owners), "source_role": "minutes_action_row"}, source={"path": str(minutes), "sha256": minutes_sha, "locator": {"table": "Action", "task_id": task_id}, "quote": f"{task_id} {task} {' / '.join(owners)}", "extraction_method": "native_docx_table"})
        policy = EdgePolicy("same_task_id", ("report_priority_task",), ("minutes_action_row",), (EqualityCheck("normalized_value.task_id", "normalized_value.task_id", "exact"),))
        verified_edges = {}
        for task_id in priority_ids:
            edge = propose_edge(graph, from_node_id=report_nodes[task_id], to_node_id=row_nodes[task_id], edge_type="same_task_id", claim="The report priority task and same-day action row have the same task ID.", comparison_fields=["task_id"])
            decoys = [node_id for other, node_id in row_nodes.items() if other != task_id]
            if audit_edge_with_same_model(graph, edge, policy, model_call=_auditor, decoy_node_ids=decoys) != "verified":
                raise ValueError("task edge not verified")
            verified_edges[task_id] = edge
        wanted = {_compact("渡辺遥"), _compact("藤田彩")}
        selected = [rows[task_id][0] for task_id in priority_ids if set(rows[task_id][1]) == wanted]
        if len(selected) != 1:
            raise ValueError("owner selection not unique")
        selected_ids = [task_id for task_id in priority_ids if rows[task_id][0] in selected]
        set_answer_projection(graph, operation="exact_owner_set_within_report_priority_tasks", input_node_ids=[row_nodes[task_id] for task_id in selected_ids], input_edge_ids=[verified_edges[task_id] for task_id in selected_ids])
        if validate_graph(graph):
            raise ValueError("priority owner graph invalid")
        paths, digest = _fingerprint((report, minutes), root)
        return StructuredCandidateDecision("resolved", "certified_priority_task_owner_graph", StructuredCandidateAnswer(selected[0], paths, digest, len(contract["operation_graph"]["nodes"]), 1))
    except (ET.ParseError, OSError, RuntimeError, subprocess.SubprocessError, TypeError, UnicodeError, ValueError, zipfile.BadZipFile):
        return StructuredCandidateDecision("hold", "priority_task_owner_not_certified")


__all__ = ["Q020", "decide_question", "graph_contract_for_question", "validate_graph_contract"]
