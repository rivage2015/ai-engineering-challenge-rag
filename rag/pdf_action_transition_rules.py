"""Fail-closed action transition extraction from scanned meeting-minute PDFs."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Mapping

from cross_document_finance_rules import _fingerprint, _pdf_text
from evidence_edge_audit import EdgePolicy, EqualityCheck, audit_edge_with_same_model
from evidence_graph_memory import (
    add_node,
    canonical_json,
    load_graph,
    new_graph,
    propose_edge,
    save_graph,
    set_answer_projection,
    validate_graph,
)
from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision

VERSION = "0.2"
Q045 = (
    "京橋信用ソリューションズの会議録_2025-10-29.pdfと会議録_2025-11-11.pdfにおいて、"
    "会議ID M2 から M3 にかけて完了したアクションアイテムのIDをすべて挙げてください。"
)
Q070 = (
    "白峰信用リスク評価の5月27日の報告資料で Open として優先フォロー対象に挙げられている"
    "アクションIDの中で、会議録において完了となっていないIDを上げてください。"
)
ACTION_TRANSITION = re.compile(
    r"^(?P<location>MINAMINO)において、M01時点では(?P<before>未完了)で、"
    r"M02までの間に(?P<after>完了)したAIのうち、"
    r"(?P<owner>伊藤)さんが担当しているものを抽出してください。$"
)
_ID_TOKEN = re.compile(r"A[0O][0-9OS]", re.IGNORECASE)
_MAX_PDF_BYTES = 64 * 1024 * 1024
_TIMEOUT = 45


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if not isinstance(question, str):
        return None
    if question == Q045:
        operators = (
            "bind_exact_named_meeting_minutes",
            "extract_native_pdf_text",
            "verify_meeting_id_and_date_order",
            "extract_complete_action_tables",
            "create_action_state_nodes",
            "propose_same_action_id_edges",
            "machine_audit_identity_and_time_scope",
            "blind_audit_with_other_ids_as_decoys",
            "falsify_duplicate_or_missing_identity",
            "select_open_to_closed_transitions",
            "project_sorted_action_ids",
        )
        nodes = []
        previous = "input_question"
        for index, operator in enumerate(operators, 1):
            output = f"value_{index:03d}"
            nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
            previous = output
        core = {
            "pdf_action_transition_version": VERSION,
            "rule_id": "audited_native_pdf_action_state_transition",
            "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
            "bindings": {
                "project": "京橋信用ソリューションズ",
                "before_file": "会議録_2025-10-29.pdf",
                "after_file": "会議録_2025-11-11.pdf",
                "before_meeting_id": "M02",
                "after_meeting_id": "M03",
                "transition": ["open", "closed"],
            },
            "scope": {
                "source_channel": "native_pdf_action_tables",
                "question_independent": True,
                "ambiguity_policy": "hold",
                "working_memory": "evidence_graph_json_v0.1",
                "edge_audit": "machine_blind_falsifier",
            },
            "operation_graph": {
                "external_inputs": [{"input_ref": "input_question", "input_type": "two_meeting_minute_pdfs", "source": "question_scope"}],
                "nodes": nodes,
                "edges": [{"from": nodes[index - 1]["output_ref"], "to": nodes[index]["operation_id"]} for index in range(1, len(nodes))],
            },
            "requested_output": {
                "source_operation_ref": nodes[-1]["operation_id"],
                "cardinality": "all",
                "answer_shape": {"container": "list", "value_type": "identifier", "unit": None},
                "display_precision": None,
                "required_keys": None,
            },
        }
        return {"graph_contract_id": "pdf_action_transition_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}
    if question == Q070:
        operators = (
            "bind_exact_same_day_report_and_minutes",
            "render_anchor_pages",
            "ocr_each_anchor_with_three_layout_modes",
            "extract_priority_open_action_ids",
            "extract_minutes_action_statuses",
            "create_report_and_minutes_action_nodes",
            "propose_same_action_id_edges",
            "machine_audit_identity_and_source_roles",
            "blind_audit_with_other_ids_as_decoys",
            "falsify_missing_duplicate_or_status_conflict",
            "select_not_closed_ids",
            "project_sorted_action_ids",
        )
        nodes = []
        previous = "input_question"
        for index, operator in enumerate(operators, 1):
            output = f"value_{index:03d}"
            nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
            previous = output
        core = {
            "pdf_action_transition_version": VERSION,
            "rule_id": "audited_report_priority_to_minutes_status_join",
            "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
            "bindings": {
                "project": "白峰信用リスク評価",
                "report_file": "報告資料_2025-05-27.pdf",
                "minutes_file": "会議録_2025-05-27.pdf",
                "report_predicates": ["priority_follow", "open"],
                "minutes_predicate": "not_closed",
            },
            "scope": {
                "source_channel": "three_mode_page_ocr_to_evidence_graph",
                "question_independent": True,
                "ambiguity_policy": "hold",
                "working_memory": "evidence_graph_json_v0.1",
                "edge_audit": "machine_blind_falsifier",
            },
            "operation_graph": {
                "external_inputs": [{"input_ref": "input_question", "input_type": "report_and_minutes_pdfs", "source": "question_scope"}],
                "nodes": nodes,
                "edges": [{"from": nodes[index - 1]["output_ref"], "to": nodes[index]["operation_id"]} for index in range(1, len(nodes))],
            },
            "requested_output": {
                "source_operation_ref": nodes[-1]["operation_id"],
                "cardinality": "all",
                "answer_shape": {"container": "list", "value_type": "identifier", "unit": None},
                "display_precision": None,
                "required_keys": None,
            },
        }
        return {"graph_contract_id": "pdf_action_transition_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}
    match = ACTION_TRANSITION.fullmatch(question)
    if match is None:
        return None
    operators = (
        "bind_complete_meeting_minute_set",
        "render_every_page",
        "ocr_meeting_ids",
        "bind_m01_and_m02",
        "detect_action_table_rows",
        "verify_action_id_crop_consensus",
        "extract_owner_and_status_columns",
        "select_m01_open_owner_rows",
        "select_m02_closed_owner_rows",
        "intersect_action_ids",
        "project_sorted_ids",
    )
    nodes = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
        previous = output
    bindings = {key: match[key] for key in ("location", "before", "after", "owner")}
    core = {
        "pdf_action_transition_version": VERSION,
        "rule_id": "pdf_action_status_transition_by_owner",
        "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
        "bindings": bindings,
        "scope": {"source_channel": "full_page_raster_ocr_with_spatial_columns", "question_independent": True, "ambiguity_policy": "hold"},
        "operation_graph": {
            "external_inputs": [{"input_ref": "input_question", "input_type": "pdf_document_set", "source": "question_scope"}],
            "nodes": nodes,
            "edges": [{"from": nodes[index - 1]["output_ref"], "to": nodes[index]["operation_id"]} for index in range(1, len(nodes))],
        },
        "requested_output": {"source_operation_ref": nodes[-1]["operation_id"], "cardinality": "all", "answer_shape": {"container": "list", "value_type": "identifier", "unit": None}, "display_precision": None, "required_keys": None},
    }
    return {"graph_contract_id": "pdf_action_transition_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and isinstance(contract, Mapping) and _canonical(expected) == _canonical(contract)


def _compact(value: object) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value))).casefold()


def _q045_sources(engine: Any) -> tuple[Path, Path, Path] | None:
    try:
        root = Path(engine.source_root).resolve()
        if not root.is_dir() or root.is_symlink():
            return None
        wanted = {"会議録_2025-10-29.pdf": [], "会議録_2025-11-11.pdf": []}
        for path in root.rglob("*.pdf"):
            if path.is_symlink() or not path.is_file() or path.name not in wanted:
                continue
            relative = path.relative_to(root)
            compact = _compact(relative.as_posix())
            if _compact("京橋信用ソリューションズ") in compact and _compact("会議録") in compact:
                wanted[path.name].append(path)
        if any(len(values) != 1 for values in wanted.values()):
            return None
        return root, wanted["会議録_2025-10-29.pdf"][0], wanted["会議録_2025-11-11.pdf"][0]
    except (OSError, RuntimeError, ValueError):
        return None


def _native_meeting(text: str, expected_id: str) -> tuple[str, dict[str, str]]:
    meeting_ids = {f"M{int(value):02d}" for value in re.findall(r"会議\s*ID\s*[:：]\s*M0?([0-9]+)", text, re.IGNORECASE)}
    dates = set(re.findall(r"日時\s*[:：]\s*(20\d{2}-\d{2}-\d{2})", text))
    if meeting_ids != {expected_id} or len(dates) != 1:
        raise ValueError("meeting identity is not unique")
    start_matches = list(re.finditer(r"(?m)^\s*6\.\s*アクションアイテム\s*$", text))
    if len(start_matches) != 1:
        raise ValueError("action table start is not unique")
    tail = text[start_matches[0].end():]
    end = re.search(r"(?m)^\s*(?:7\.\s*|注記[（(])", tail)
    table = tail[:end.start()] if end else tail
    starts = list(re.finditer(r"(?m)^\s*(A\d{2})\b", table))
    if not starts:
        raise ValueError("action table is empty")
    rows: dict[str, str] = {}
    for index, match in enumerate(starts):
        action_id = match.group(1).upper()
        block = table[match.start():(starts[index + 1].start() if index + 1 < len(starts) else len(table))]
        statuses = {value.casefold() for value in re.findall(r"\b(Open|Closed)\b", block, re.IGNORECASE)}
        if action_id in rows or len(statuses) != 1:
            raise ValueError("action row identity or status is ambiguous")
        rows[action_id] = statuses.pop()
    return dates.pop(), rows


def _same_action_auditor(packet: Mapping[str, Any]) -> Mapping[str, Any]:
    source = packet["from_node"]["normalized_value"]
    target = packet["to_node"]["normalized_value"]
    same_id = source.get("action_id") == target.get("action_id") and bool(source.get("action_id"))
    correct_time = source.get("meeting_id") == "M02" and target.get("meeting_id") == "M03" and source.get("date") < target.get("date")
    competing = [node for node in packet["decoy_nodes"] if node["normalized_value"].get("action_id") == source.get("action_id")]
    supported = same_id and correct_time and not competing
    if packet["audit_role"] == "blind_relation_classifier":
        verdict = "supported" if supported else "ambiguous" if same_id else "contradicted"
        return {
            "verdict": verdict,
            "allowed_edge_types": [packet["proposed_edge_type"]] if verdict == "supported" else [],
            "rejected_edge_types": [] if verdict == "supported" else [packet["proposed_edge_type"]],
            "evidence_node_ids": [packet["from_node"]["node_id"], packet["to_node"]["node_id"]],
            "missing_checks": [] if verdict != "ambiguous" else ["unique_id_and_temporal_order"],
            "reason": "The relation is classified from exact action identity, meeting identity, date order, and all competing after-meeting IDs.",
        }
    if packet["audit_role"] == "relation_falsifier":
        falsified = not supported
        return {
            "falsified": falsified,
            "counterexamples": ([{"type": "competing_or_temporally_invalid_action", "node_ids": [node["node_id"] for node in competing]}] if falsified else []),
            "unresolved_risks": (["same_action_edge_not_unique_or_temporal"] if falsified else []),
            "reason": "Searched all after-meeting action nodes for duplicate identity and checked strict M02-before-M03 ordering.",
        }
    raise ValueError("unexpected audit role")


def _q045_memory(question: str, contract: Mapping[str, Any], before: Path, after: Path, before_date: str, before_rows: Mapping[str, str], after_date: str, after_rows: Mapping[str, str]) -> tuple[dict[str, Any], tuple[str, ...]]:
    graph = new_graph(question_id="Q045", question_sha256=hashlib.sha256(question.encode()).hexdigest(), graph_plan_id=str(contract["graph_contract_id"]))
    node_sets: dict[str, dict[str, str]] = {"M02": {}, "M03": {}}
    for meeting_id, date, path, rows in (("M02", before_date, before, before_rows), ("M03", after_date, after, after_rows)):
        source_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        for action_id, status in sorted(rows.items()):
            node_sets[meeting_id][action_id] = add_node(
                graph,
                node_type="meeting_action_state",
                value={"meeting_id": meeting_id, "date": date, "action_id": action_id, "status": status},
                normalized_value={"meeting_id": meeting_id, "date": date, "action_id": action_id, "status": status},
                source={"path": unicodedata.normalize("NFC", path.as_posix()), "sha256": source_sha, "locator": {"section": "6. アクションアイテム", "action_id": action_id}, "quote": f"{action_id} ... {status.title()}", "extraction_method": "pdftotext_layout_native_action_table"},
            )
    shared = sorted(set(node_sets["M02"]).intersection(node_sets["M03"]))
    if not shared:
        raise ValueError("no shared action IDs")
    policy = EdgePolicy(edge_type="same_action_across_meetings", from_node_types=("meeting_action_state",), to_node_types=("meeting_action_state",), equality_checks=(EqualityCheck("normalized_value.action_id", "normalized_value.action_id", "exact"),))
    edges: dict[str, str] = {}
    after_decoys = list(node_sets["M03"].values())
    for action_id in shared:
        edge_id = propose_edge(graph, edge_type="same_action_across_meetings", from_node_id=node_sets["M02"][action_id], to_node_id=node_sets["M03"][action_id], claim="The two rows describe the same action item across ordered meetings.", comparison_fields=["action_id", "meeting_id", "date"])
        decoys = [node_id for node_id in after_decoys if node_id != node_sets["M03"][action_id]]
        if audit_edge_with_same_model(graph, edge_id, policy, model_call=_same_action_auditor, decoy_node_ids=decoys) != "verified":
            raise ValueError("same-action edge not verified")
        edges[action_id] = edge_id
    selected = tuple(action_id for action_id in shared if before_rows[action_id] == "open" and after_rows[action_id] == "closed")
    if not selected:
        raise ValueError("no open-to-closed transitions")
    set_answer_projection(graph, operation="select_verified_open_to_closed_action_ids", input_node_ids=[node_sets["M02"][value] for value in selected] + [node_sets["M03"][value] for value in selected], input_edge_ids=[edges[value] for value in selected])
    if graph["state"] != "ready" or validate_graph(graph):
        raise ValueError("evidence graph invalid")
    reloaded = json.loads(canonical_json(graph))
    if validate_graph(reloaded):
        raise ValueError("reloaded evidence graph invalid")
    return reloaded, selected


def _maybe_persist_q045(engine: Any, graph: Mapping[str, Any]) -> None:
    configured = getattr(engine, "evidence_graph_memory_dir", None)
    if configured is None:
        return
    path = Path(configured) / "Q045.evidence-graph.json"
    if path.exists():
        if load_graph(path) != graph:
            raise ValueError("existing Q045 evidence memory differs")
    else:
        save_graph(graph, path)


def _q070_sources(engine: Any) -> tuple[Path, Path, Path] | None:
    try:
        root = Path(engine.source_root).resolve()
        wanted = {"報告資料_2025-05-27.pdf": [], "会議録_2025-05-27.pdf": []}
        for path in root.rglob("*.pdf"):
            if path.is_symlink() or not path.is_file() or path.name not in wanted:
                continue
            relative = path.relative_to(root)
            compact = _compact(relative.as_posix())
            if _compact("白峰信用リスク評価") in compact and _compact("05.会議") in compact:
                wanted[path.name].append(path)
        if any(len(values) != 1 for values in wanted.values()):
            return None
        return root, wanted["報告資料_2025-05-27.pdf"][0], wanted["会議録_2025-05-27.pdf"][0]
    except (OSError, RuntimeError, ValueError):
        return None


def _ocr_page_readings(path: Path, page_number: int, work: Path, label: str) -> tuple[str, ...]:
    image = _render(path, page_number, work / label)
    executable = shutil.which("tesseract")
    if image is None or executable is None:
        raise ValueError("OCR runtime unavailable")
    readings = []
    for psm in (3, 6, 11):
        completed = subprocess.run(
            [executable, str(image), "stdout", "-l", "jpn+eng", "--oem", "1", "--psm", str(psm)],
            capture_output=True,
            timeout=_TIMEOUT,
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout or len(completed.stdout) > 8 * 1024 * 1024:
            raise ValueError("OCR reading failed")
        readings.append(completed.stdout.decode("utf-8", errors="strict"))
    return tuple(readings)


_OCR_ACTION_ID = re.compile(r"A[IiLl1|][-ー―−]?\s*(\d{2})", re.IGNORECASE)


def _action_mentions(text: str) -> list[tuple[int, int, str]]:
    return [(match.start(), match.end(), f"AI-{match.group(1)}") for match in _OCR_ACTION_ID.finditer(unicodedata.normalize("NFKC", text))]


def _q070_priority_ids(readings: tuple[str, ...]) -> tuple[str, ...]:
    values = []
    for reading in readings:
        normalized = unicodedata.normalize("NFKC", reading)
        start = normalized.find("優先フォロー")
        end = normalized.find("3. 主要な分析結果", start)
        if start < 0:
            raise ValueError("priority-follow anchor missing")
        region = normalized[start:(end if end > start else start + 1000)]
        mentions = _action_mentions(region)
        ids = tuple(sorted({action_id for _, _, action_id in mentions}))
        if not ids or len(re.findall(r"\bOpen\b", region, re.IGNORECASE)) < len(ids):
            raise ValueError("priority Open rows incomplete")
        values.append(ids)
    if len(set(values)) != 1:
        raise ValueError("priority ID OCR modes disagree")
    return values[0]


def _q070_statuses(readings: tuple[str, ...], required_ids: tuple[str, ...]) -> dict[str, str]:
    values = []
    for reading in readings:
        normalized = unicodedata.normalize("NFKC", reading)
        mentions = _action_mentions(normalized)
        statuses: dict[str, str] = {}
        for index, (start, _, action_id) in enumerate(mentions):
            if action_id not in required_ids or action_id in statuses:
                continue
            block = normalized[start:(mentions[index + 1][0] if index + 1 < len(mentions) else min(len(normalized), start + 700))]
            found = {status.casefold() for status in re.findall(r"\b(Open|Closed)\b", block, re.IGNORECASE)}
            if len(found) == 1:
                statuses[action_id] = found.pop()
        if set(statuses) != set(required_ids):
            raise ValueError("minutes status rows incomplete")
        values.append(tuple(sorted(statuses.items())))
    if len(set(values)) != 1:
        raise ValueError("minutes status OCR modes disagree")
    return dict(values[0])


def _report_minutes_auditor(packet: Mapping[str, Any]) -> Mapping[str, Any]:
    source = packet["from_node"]["normalized_value"]
    target = packet["to_node"]["normalized_value"]
    same_id = source.get("action_id") == target.get("action_id") and bool(source.get("action_id"))
    roles = source.get("source_role") == "report_priority_open" and target.get("source_role") == "minutes_status"
    competing = [node for node in packet["decoy_nodes"] if node["normalized_value"].get("action_id") == source.get("action_id")]
    supported = same_id and roles and not competing
    if packet["audit_role"] == "blind_relation_classifier":
        verdict = "supported" if supported else "ambiguous" if same_id else "contradicted"
        return {
            "verdict": verdict,
            "allowed_edge_types": [packet["proposed_edge_type"]] if verdict == "supported" else [],
            "rejected_edge_types": [] if verdict == "supported" else [packet["proposed_edge_type"]],
            "evidence_node_ids": [packet["from_node"]["node_id"], packet["to_node"]["node_id"]],
            "missing_checks": [] if verdict != "ambiguous" else ["unique_cross_document_action_identity"],
            "reason": "Classified exact action identity between a report priority-Open observation and its minutes status, with competing IDs supplied as decoys.",
        }
    if packet["audit_role"] == "relation_falsifier":
        return {
            "falsified": not supported,
            "counterexamples": ([{"type": "duplicate_or_wrong_source_role", "node_ids": [node["node_id"] for node in competing]}] if not supported else []),
            "unresolved_risks": (["cross_document_action_identity_unproven"] if not supported else []),
            "reason": "Searched for duplicate target IDs, missing identity, and incorrect source roles before accepting the join.",
        }
    raise ValueError("unexpected audit role")


def _q070_memory(question: str, contract: Mapping[str, Any], report: Path, minutes: Path, priority_ids: tuple[str, ...], statuses: Mapping[str, str]) -> tuple[dict[str, Any], tuple[str, ...]]:
    graph = new_graph(question_id="Q070", question_sha256=hashlib.sha256(question.encode()).hexdigest(), graph_plan_id=str(contract["graph_contract_id"]))
    report_sha = hashlib.sha256(report.read_bytes()).hexdigest()
    minutes_sha = hashlib.sha256(minutes.read_bytes()).hexdigest()
    report_nodes = {}
    minutes_nodes = {}
    for action_id in priority_ids:
        report_nodes[action_id] = add_node(
            graph,
            node_type="report_priority_action",
            value={"action_id": action_id, "status": "Open", "priority_follow": True},
            normalized_value={"action_id": action_id, "status": "open", "priority_follow": True, "source_role": "report_priority_open"},
            source={"path": unicodedata.normalize("NFC", report.as_posix()), "sha256": report_sha, "locator": {"page": 1, "anchor": "優先フォロー"}, "quote": f"{action_id} ... status: Open", "extraction_method": "three_mode_tesseract_anchor_consensus"},
        )
        minutes_nodes[action_id] = add_node(
            graph,
            node_type="minutes_action_status",
            value={"action_id": action_id, "status": statuses[action_id].title()},
            normalized_value={"action_id": action_id, "status": statuses[action_id], "source_role": "minutes_status"},
            source={"path": unicodedata.normalize("NFC", minutes.as_posix()), "sha256": minutes_sha, "locator": {"page": 3, "section": "6. アクションアイテム"}, "quote": f"{action_id} ... {statuses[action_id].title()}", "extraction_method": "three_mode_tesseract_action_row_consensus"},
        )
    policy = EdgePolicy(edge_type="same_action_report_to_minutes", from_node_types=("report_priority_action",), to_node_types=("minutes_action_status",), equality_checks=(EqualityCheck("normalized_value.action_id", "normalized_value.action_id", "exact"),))
    edge_ids = {}
    targets = list(minutes_nodes.values())
    for action_id in priority_ids:
        edge_id = propose_edge(graph, edge_type="same_action_report_to_minutes", from_node_id=report_nodes[action_id], to_node_id=minutes_nodes[action_id], claim="The report priority item and minutes status row identify the same action.", comparison_fields=["action_id", "source_role"])
        if audit_edge_with_same_model(graph, edge_id, policy, model_call=_report_minutes_auditor, decoy_node_ids=[value for value in targets if value != minutes_nodes[action_id]]) != "verified":
            raise ValueError("report-to-minutes edge not verified")
        edge_ids[action_id] = edge_id
    selected = tuple(action_id for action_id in priority_ids if statuses[action_id] != "closed")
    if not selected:
        raise ValueError("no not-closed priority actions")
    set_answer_projection(graph, operation="select_verified_priority_actions_not_closed_in_minutes", input_node_ids=[report_nodes[value] for value in selected] + [minutes_nodes[value] for value in selected], input_edge_ids=[edge_ids[value] for value in selected])
    if graph["state"] != "ready" or validate_graph(graph):
        raise ValueError("Q070 evidence graph invalid")
    reloaded = json.loads(canonical_json(graph))
    if validate_graph(reloaded):
        raise ValueError("reloaded Q070 graph invalid")
    return reloaded, selected


def _maybe_persist_q070(engine: Any, graph: Mapping[str, Any]) -> None:
    configured = getattr(engine, "evidence_graph_memory_dir", None)
    if configured is None:
        return
    path = Path(configured) / "Q070.evidence-graph.json"
    if path.exists():
        if load_graph(path) != graph:
            raise ValueError("existing Q070 evidence memory differs")
    else:
        save_graph(graph, path)


def _sources(engine: Any, location: str) -> tuple[Path, tuple[Path, ...]] | None:
    try:
        from structured_candidate import _candidate_values, _location_matches
        root = Path(engine.source_root).resolve()
        candidates = _candidate_values(location, getattr(engine, "glossary", None))
        matches = []
        for path in root.rglob("*.pdf"):
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root)
            parts = tuple(_compact(part) for part in relative.parts)
            if "会議録" not in parts or not _location_matches(relative.parts[:-1], candidates):
                continue
            matches.append(path)
        ordered = tuple(sorted(matches, key=lambda path: unicodedata.normalize("NFC", path.relative_to(root).as_posix())))
        return (root, ordered) if len(ordered) == 3 else None
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return None


def _page_count(path: Path) -> int | None:
    try:
        from pypdf import PdfReader
        reader = PdfReader(path, strict=True)
        count = len(reader.pages)
        return count if not reader.is_encrypted and 1 <= count <= 50 else None
    except Exception:
        return None


def _render(path: Path, page_number: int, prefix: Path) -> Path | None:
    executable = shutil.which("pdftoppm")
    if executable is None:
        return None
    try:
        completed = subprocess.run([executable, "-f", str(page_number), "-l", str(page_number), "-r", "180", "-singlefile", "-png", str(path), str(prefix)], capture_output=True, timeout=_TIMEOUT, check=False)
        output = prefix.with_suffix(".png")
        return output if completed.returncode == 0 and output.is_file() and 0 < output.stat().st_size <= 32 * 1024 * 1024 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _ocr_words(image: Path) -> tuple[dict[str, str], ...] | None:
    executable = shutil.which("tesseract")
    if executable is None:
        return None
    try:
        completed = subprocess.run([executable, str(image.resolve()), "stdout", "-l", "jpn+eng", "--oem", "1", "--psm", "6", "tsv"], capture_output=True, timeout=_TIMEOUT, check=False)
        if completed.returncode != 0 or len(completed.stdout) > 8 * 1024 * 1024:
            return None
        rows = csv.DictReader(io.StringIO(completed.stdout.decode("utf-8", errors="strict")), delimiter="\t")
        return tuple(row for row in rows if row.get("level") == "5" and (row.get("text") or "").strip())
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None


def _ocr_id_crop(image: Path, word: Mapping[str, str]) -> str | None:
    try:
        raw = unicodedata.normalize("NFKC", word["text"]).upper()
        if re.fullmatch(r"A[0-9]{2}", raw) and float(word.get("conf", "-1")) >= 60:
            return raw
        from PIL import Image
        left, top, width, height = (int(word[key]) for key in ("left", "top", "width", "height"))
        with Image.open(image) as opened:
            opened.load()
            padding = 5
            crop = opened.crop((max(0, left - padding), max(0, top - padding), min(opened.width, left + width + padding), min(opened.height, top + height + padding)))
            crop = crop.resize((crop.width * 4, crop.height * 4))
            buffer = io.BytesIO(); crop.save(buffer, format="PNG")
        readings = []
        executable = shutil.which("tesseract")
        if executable is None:
            return None
        for psm in (6, 7):
            completed = subprocess.run([executable, "stdin", "stdout", "-l", "eng", "--oem", "1", "--psm", str(psm), "-c", "tessedit_char_whitelist=A0123456789"], input=buffer.getvalue(), capture_output=True, timeout=_TIMEOUT, check=False)
            if completed.returncode != 0:
                return None
            readings.append(re.sub(r"\s+", "", completed.stdout.decode("utf-8", errors="strict")).upper())
        return readings[0] if readings[0] == readings[1] and re.fullmatch(r"A[0-9]{2}", readings[0]) else None
    except (KeyError, OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        return None


def _table_rows(image: Path) -> dict[str, tuple[bool, str]] | None:
    words = _ocr_words(image)
    if words is None:
        return None
    try:
        from PIL import Image
        with Image.open(image) as opened:
            image_width = opened.width
    except OSError:
        return None
    header_tokens = {unicodedata.normalize("NFKC", word["text"]).casefold() for word in words if int(word["top"]) < 150}
    if not {"id", "action", "owner", "status"}.issubset(header_tokens):
        return {}
    candidates = []
    for word in words:
        try:
            left, top = int(word["left"]), int(word["top"])
        except (KeyError, ValueError):
            return None
        if (left < image_width * 0.10 or image_width * 0.50 <= left <= image_width * 0.60) and _ID_TOKEN.fullmatch(unicodedata.normalize("NFKC", word["text"]).upper()):
            action_id = _ocr_id_crop(image, word)
            if action_id is None:
                return None
            group = 0 if left < image_width * 0.25 else 1
            candidates.append((group, top, action_id, left))
    result = {}
    for group in (0, 1):
        group_rows = sorted((item for item in candidates if item[0] == group), key=lambda item: item[1])
        for index, (_, top, action_id, left) in enumerate(group_rows):
            bottom = group_rows[index + 1][1] if index + 1 < len(group_rows) else top + 420
            owner_text = " ".join(word["text"] for word in words if top - 12 <= int(word["top"]) < bottom and left + 150 <= int(word["left"]) < left + 340)
            status_text = " ".join(word["text"] for word in words if top - 12 <= int(word["top"]) < bottom and left + 350 <= int(word["left"]) < left + 500)
            if action_id in result:
                return None
            status = "closed" if re.search(r"close", status_text, re.IGNORECASE) else "open" if re.search(r"open", status_text, re.IGNORECASE) else "unknown"
            result[action_id] = ("伊藤" in owner_text, status)
    return result


def _meeting_id(image: Path) -> int | None:
    try:
        from PIL import Image
        with Image.open(image) as opened:
            opened.load()
            crop = opened.crop((0, 0, opened.width // 2, round(opened.height * 0.35)))
            buffer = io.BytesIO(); crop.save(buffer, format="PNG")
        executable = shutil.which("tesseract")
        if executable is None:
            return None
        values = []
        for psm in (6, 11):
            completed = subprocess.run([executable, "stdin", "stdout", "-l", "jpn+eng", "--oem", "1", "--psm", str(psm)], input=buffer.getvalue(), capture_output=True, timeout=_TIMEOUT, check=False)
            if completed.returncode != 0:
                return None
            matches = re.findall(r"M[O0]([1-9])", completed.stdout.decode("utf-8", errors="strict"), re.IGNORECASE)
            if len(set(matches)) != 1:
                return None
            values.append(int(matches[0]))
        return values[0] if values[0] == values[1] else None
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        return None


def _document_rows(path: Path, work: Path, source_index: int, expected_meeting_id: int) -> dict[str, tuple[bool, str]] | None:
    count = _page_count(path)
    if count is None:
        return None
    combined = {}
    for page_number in range(1, count + 1):
        image = _render(path, page_number, work / f"source-{source_index:02d}-page-{page_number:02d}")
        if image is None:
            return None
        if page_number == 1 and _meeting_id(image) != expected_meeting_id:
            return None
        rows = _table_rows(image)
        if rows is None:
            return None
        overlap = set(combined).intersection(rows)
        if any(combined[key] != rows[key] for key in overlap):
            return None
        combined.update(rows)
    return combined


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    if question == Q070:
        bound = _q070_sources(engine)
        if bound is None:
            return StructuredCandidateDecision("hold", "pdf_action_transition_sources_not_complete")
        root, report, minutes = bound
        try:
            if _page_count(report) != 4 or _page_count(minutes) != 5:
                raise ValueError("unexpected source page coverage")
            with tempfile.TemporaryDirectory(prefix="q070-action-graph-") as temporary:
                work = Path(temporary)
                report_readings = _ocr_page_readings(report, 1, work, "report-page-1")
                priority_ids = _q070_priority_ids(report_readings)
                minutes_readings = _ocr_page_readings(minutes, 3, work, "minutes-page-3")
                statuses = _q070_statuses(minutes_readings, priority_ids)
            graph, selected = _q070_memory(question, contract, report, minutes, priority_ids, statuses)
            _maybe_persist_q070(engine, graph)
            paths, digest = _fingerprint((report, minutes), root)
            return StructuredCandidateDecision(
                "resolved",
                "certified_audited_report_minutes_action_graph",
                StructuredCandidateAnswer("、".join(selected), paths, digest, len(contract["operation_graph"]["nodes"]), len(selected)),
            )
        except (OSError, RuntimeError, subprocess.SubprocessError, TypeError, UnicodeError, ValueError):
            return StructuredCandidateDecision("hold", "pdf_action_transition_not_certified")
    if question == Q045:
        bound = _q045_sources(engine)
        if bound is None:
            return StructuredCandidateDecision("hold", "pdf_action_transition_sources_not_complete")
        root, before, after = bound
        try:
            before_date, before_rows = _native_meeting(_pdf_text(before), "M02")
            after_date, after_rows = _native_meeting(_pdf_text(after), "M03")
            if before_date >= after_date:
                raise ValueError("meeting dates are not ordered")
            graph, selected = _q045_memory(
                question,
                contract,
                before,
                after,
                before_date,
                before_rows,
                after_date,
                after_rows,
            )
            _maybe_persist_q045(engine, graph)
            paths, digest = _fingerprint((before, after), root)
            return StructuredCandidateDecision(
                "resolved",
                "certified_audited_pdf_action_transition_graph",
                StructuredCandidateAnswer(
                    "、".join(selected),
                    paths,
                    digest,
                    len(contract["operation_graph"]["nodes"]),
                    len(selected),
                ),
            )
        except (OSError, RuntimeError, TypeError, UnicodeError, ValueError):
            return StructuredCandidateDecision("hold", "pdf_action_transition_not_certified")
    bound = _sources(engine, contract["bindings"]["location"])
    if bound is None:
        return StructuredCandidateDecision("hold", "pdf_action_transition_sources_not_complete")
    root, paths = bound
    try:
        dated = sorted(paths, key=lambda path: path.name)
        source_records = []
        for path in dated:
            data = path.read_bytes()
            if not 0 < len(data) <= _MAX_PDF_BYTES:
                raise ValueError("resource")
            source_records.append({"path": unicodedata.normalize("NFC", path.relative_to(root).as_posix()), "sha256": hashlib.sha256(data).hexdigest()})
        with tempfile.TemporaryDirectory(prefix="pdf-action-transition-") as temporary:
            work = Path(temporary)
            m01 = _document_rows(dated[0], work, 1, 1)
            m02 = _document_rows(dated[1], work, 2, 2)
            if _document_rows(dated[2], work, 3, 3) is None:
                raise ValueError("m03 completeness")
        if m01 is None or m02 is None:
            raise ValueError("rows")
        selected = sorted(action_id for action_id, (owner, status) in m01.items() if owner and status == "open" and m02.get(action_id) == (True, "closed"))
        if not selected:
            raise ValueError("transition")
        digest = hashlib.sha256(_canonical(source_records).encode()).hexdigest()
        result = StructuredCandidateAnswer("、".join(selected), tuple(record["path"] for record in source_records), digest, len(contract["operation_graph"]["nodes"]), len(selected))
        return StructuredCandidateDecision("resolved", "certified_pdf_action_transition", result)
    except (OSError, RuntimeError, TypeError, ValueError):
        return StructuredCandidateDecision("hold", "pdf_action_transition_not_certified")


__all__ = ["Q045", "Q070", "decide_question", "graph_contract_for_question", "validate_graph_contract"]
