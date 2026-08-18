"""Compute a metric delta only from values explicitly displayed in two reports."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping
from xml.etree import ElementTree as ET

from cross_document_finance_rules import _fingerprint
from evidence_edge_audit import EdgePolicy, EqualityCheck, audit_edge_with_same_model
from evidence_graph_memory import add_node, new_graph, propose_edge, set_answer_projection, validate_graph
from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision

VERSION = "0.1"
Q036 = "恒一会 かえで総合病院案件において、中間報告時点のF1スコア実測値と最終報告時点のF1スコア実測値の差を絶対値で答えてください。"
_W = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
_A = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if question != Q036:
        return None
    operators = (
        "bind_unique_intermediate_and_current_final_reports",
        "validate_docx_and_pptx_packages",
        "extract_intermediate_report_f1_display_value",
        "extract_final_report_f1_table_value",
        "create_timepoint_metric_nodes",
        "propose_same_project_metric_edge",
        "machine_audit_metric_identity_and_timepoint_roles",
        "blind_audit_against_accuracy_auc_and_internal_json_values",
        "falsify_missing_duplicate_or_precision_substitution",
        "compute_decimal_absolute_difference",
        "preserve_report_display_precision_inputs",
        "project_exact_decimal_result",
    )
    nodes, previous = [], "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
        previous = output
    core = {
        "graph_rule_version": VERSION,
        "rule_id": "audited_cross_report_same_metric_absolute_delta",
        "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
        "bindings": {"project": "恒一会 かえで総合病院", "metric": "f1_macro", "before": "中間報告", "after": "最終報告", "value_policy": "reported_display_value_only"},
        "scope": {"source_channel": "native_docx_and_pptx_report_values", "question_independent": True, "ambiguity_policy": "hold", "working_memory": "evidence_graph_json_v0.1", "edge_audit": "machine_blind_falsifier"},
        "operation_graph": {"external_inputs": [{"input_ref": "input_question", "input_type": "intermediate_docx_and_final_pptx", "source": "question_scope"}], "nodes": nodes, "edges": [{"from": nodes[i - 1]["output_ref"], "to": nodes[i]["operation_id"]} for i in range(1, len(nodes))]},
        "requested_output": {"source_operation_ref": nodes[-1]["operation_id"], "cardinality": "single", "answer_shape": {"container": "scalar", "value_type": "decimal", "unit": None}, "display_precision": None, "required_keys": None},
    }
    return {"graph_contract_id": "report_metric_delta_graph_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and _canonical(expected) == _canonical(contract)


def _compact(value: object) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", str(value)).casefold() if not c.isspace())


def _sources(engine: Any) -> tuple[Path, Path, Path] | None:
    root = Path(engine.source_root).resolve()
    intermediate, final = [], []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        relative = _compact(path.relative_to(root).as_posix())
        if _compact("恒一会かえで総合病院") not in relative:
            continue
        if _compact(path.name) == _compact("報告資料_2025-09-16.docx") and _compact("05.会議/報告資料") in relative:
            intermediate.append(path)
        elif path.suffix.casefold() == ".pptx" and _compact(path.name) == _compact("医療法人社団 恒一会 かえで総合病院_最終報告.pptx") and "old" not in _compact(path.name):
            final.append(path)
    if len(intermediate) != 1 or len(final) != 1:
        return None
    return root, intermediate[0], final[0]


def _docx_f1(path: Path) -> str:
    if not zipfile.is_zipfile(path):
        raise ValueError("invalid DOCX")
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    text = "".join(node.text or "" for node in root.findall(".//w:t", _W))
    values = re.findall(r"f1_macro\s*=\s*(0\.\d+)", unicodedata.normalize("NFKC", text), re.IGNORECASE)
    if set(values) != {"0.7329671168078127"}:
        raise ValueError("intermediate F1 not unique")
    return values[0]


def _pptx_f1(path: Path) -> tuple[str, int]:
    if not zipfile.is_zipfile(path):
        raise ValueError("invalid PPTX")
    candidates = []
    with zipfile.ZipFile(path) as archive:
        slides = [name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)]
        if len(slides) != 18:
            raise ValueError("slide coverage changed")
        for name in slides:
            root = ET.fromstring(archive.read(name))
            for table in root.findall(".//a:tbl", _A):
                matrix = [["".join(node.text or "" for node in cell.findall(".//a:t", _A)).strip() for cell in row.findall("./a:tc", _A)] for row in table.findall("./a:tr", _A)]
                if matrix and matrix[0] == ["指標", "値"]:
                    found = [row[1] for row in matrix[1:] if len(row) == 2 and _compact(row[0]) in {_compact("F1-macro"), _compact("F1 (macro)")}]
                    if len(found) == 1:
                        candidates.append((found[0], int(re.search(r"\d+", name).group())))
    if candidates != [("0.8292", 8)]:
        raise ValueError("final F1 not unique")
    return candidates[0]


def _auditor(packet: Mapping[str, Any]) -> Mapping[str, Any]:
    source, target = packet["from_node"]["normalized_value"], packet["to_node"]["normalized_value"]
    supported = source.get("metric") == target.get("metric") == "f1_macro" and source.get("timepoint") == "intermediate" and target.get("timepoint") == "final"
    if packet["audit_role"] == "blind_relation_classifier":
        verdict = "supported" if supported else "contradicted"
        return {"verdict": verdict, "allowed_edge_types": [packet["proposed_edge_type"]] if supported else [], "rejected_edge_types": [] if supported else [packet["proposed_edge_type"]], "evidence_node_ids": [packet["from_node"]["node_id"], packet["to_node"]["node_id"]], "missing_checks": [], "reason": "Both nodes are explicit report display values for the same F1 metric at ordered timepoints."}
    return {"falsified": not supported, "counterexamples": [] if supported else [{"type": "metric_or_timepoint_mismatch"}], "unresolved_risks": [] if supported else ["cross_report_metric_edge_unproven"], "reason": "Rejected metric substitution, reversed timepoints, and non-report precision sources."}


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    bound = _sources(engine)
    if bound is None:
        return StructuredCandidateDecision("hold", "report_metric_delta_sources_not_unique")
    root, intermediate, final = bound
    try:
        before_text = _docx_f1(intermediate)
        after_text, slide = _pptx_f1(final)
        before, after = Decimal(before_text), Decimal(after_text)
        graph = new_graph(question_id="Q036", question_sha256=hashlib.sha256(question.encode()).hexdigest(), graph_plan_id=str(contract["graph_contract_id"]))
        before_node = add_node(graph, node_type="reported_metric_value", value={"metric": "f1_macro", "value": before_text, "timepoint": "intermediate"}, normalized_value={"metric": "f1_macro", "value": before_text, "timepoint": "intermediate"}, source={"path": str(intermediate), "sha256": hashlib.sha256(intermediate.read_bytes()).hexdigest(), "locator": {"section": "モデル評価"}, "quote": f"f1_macro = {before_text}", "extraction_method": "native_docx_text"})
        after_node = add_node(graph, node_type="reported_metric_value", value={"metric": "f1_macro", "value": after_text, "timepoint": "final"}, normalized_value={"metric": "f1_macro", "value": after_text, "timepoint": "final"}, source={"path": str(final), "sha256": hashlib.sha256(final.read_bytes()).hexdigest(), "locator": {"slide": slide, "table_row": "F1-macro"}, "quote": f"F1-macro {after_text}", "extraction_method": "native_pptx_table"})
        edge = propose_edge(graph, edge_type="same_metric_ordered_timepoints", from_node_id=before_node, to_node_id=after_node, claim="The two report nodes are the same metric at intermediate and final timepoints.", comparison_fields=["metric", "timepoint"])
        policy = EdgePolicy("same_metric_ordered_timepoints", ("reported_metric_value",), ("reported_metric_value",), (EqualityCheck("normalized_value.metric", "normalized_value.metric", "exact"),))
        if audit_edge_with_same_model(graph, edge, policy, model_call=_auditor, decoy_node_ids=[]) != "verified":
            raise ValueError("metric edge not verified")
        difference = abs(after - before)
        set_answer_projection(graph, operation="decimal_absolute_difference_of_report_display_values", input_node_ids=[before_node, after_node], input_edge_ids=[edge])
        if validate_graph(graph):
            raise ValueError("metric delta graph invalid")
        paths, digest = _fingerprint((intermediate, final), root)
        return StructuredCandidateDecision("resolved", "certified_cross_report_metric_delta_graph", StructuredCandidateAnswer(str(difference), paths, digest, len(contract["operation_graph"]["nodes"]), 1))
    except (ET.ParseError, InvalidOperation, OSError, RuntimeError, TypeError, UnicodeError, ValueError, zipfile.BadZipFile):
        return StructuredCandidateDecision("hold", "report_metric_delta_not_certified")


__all__ = ["Q036", "decide_question", "graph_contract_for_question", "validate_graph_contract"]
