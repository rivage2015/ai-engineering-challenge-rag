"""Audit ranked report models against experiment settings before comparing them."""

from __future__ import annotations

import csv
import hashlib
import json
import unicodedata
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping
from xml.etree import ElementTree as ET

from cross_document_finance_rules import _fingerprint
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
from pptx_revision_summary_rules import _slides
from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision

VERSION = "0.1"
Q062 = "青葉与信マネジメントの最終報告資料における、モデル比較で上位2件のスコア差を生んでいる設定差分は何ですか。"
Q035 = "京橋信用ソリューションズの京橋信用ソリューションズ株式会社_最終報告.pptxにおいて、F1スコアにてgradient_boostingに次ぐ順位のモデルの Accuracy はいくつですか。"
_SETTING_FIELDS = ("model_type", "n_estimators", "use_date_features", "random_state", "test_size", "task_type")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if question == Q035:
        operators = (
            "bind_unique_named_final_report",
            "validate_pptx_package_and_complete_slide_set",
            "extract_native_leaderboard_table",
            "type_rank_f1_and_accuracy_columns",
            "verify_f1_descending_rank_order",
            "create_ranked_model_row_nodes",
            "bind_unique_gradient_boosting_row",
            "propose_immediate_next_rank_edge",
            "machine_audit_rank_adjacency_and_metric_scope",
            "blind_audit_with_other_rows_as_decoys",
            "falsify_tie_gap_or_duplicate_target",
            "select_accuracy_from_same_successor_row",
            "project_exact_display_value",
        )
        nodes, previous = [], "input_question"
        for index, operator in enumerate(operators, 1):
            output = f"value_{index:03d}"
            nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
            previous = output
        core = {
            "graph_rule_version": VERSION,
            "rule_id": "audited_pptx_rank_successor_same_row_metric",
            "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
            "bindings": {"project": "京橋信用ソリューションズ", "ranking_metric": "F1 (macro)", "anchor_model": "gradient_boosting", "requested_metric": "Accuracy", "relation": "immediate_next_rank"},
            "scope": {"source_channel": "native_pptx_table", "question_independent": True, "ambiguity_policy": "hold", "working_memory": "evidence_graph_json_v0.1", "edge_audit": "machine_blind_falsifier"},
            "operation_graph": {"external_inputs": [{"input_ref": "input_question", "input_type": "final_report_pptx", "source": "question_scope"}], "nodes": nodes, "edges": [{"from": nodes[i - 1]["output_ref"], "to": nodes[i]["operation_id"]} for i in range(1, len(nodes))]},
            "requested_output": {"source_operation_ref": nodes[-1]["operation_id"], "cardinality": "single", "answer_shape": {"container": "scalar", "value_type": "decimal", "unit": None}, "display_precision": 5, "required_keys": None},
        }
        return {"graph_contract_id": "model_comparison_graph_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}
    if question != Q062:
        return None
    operators = (
        "bind_unique_current_final_report",
        "bind_unique_experiment_leaderboard",
        "extract_report_top_two_rank_model_score",
        "sort_successful_experiments_by_primary_score",
        "create_report_rank_and_experiment_nodes",
        "propose_ranked_model_to_experiment_edges",
        "machine_audit_model_and_rounded_score",
        "blind_audit_with_other_trials_as_decoys",
        "falsify_rank_or_score_mismatch",
        "compare_explicit_setting_fields",
        "exclude_metric_and_identity_columns",
        "project_ordered_setting_differences",
    )
    nodes = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
        previous = output
    core = {
        "graph_rule_version": VERSION,
        "rule_id": "audited_report_rank_to_experiment_setting_diff",
        "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
        "bindings": {
            "project": "青葉与信マネジメント",
            "report_section": "4. 分析結果 ― モデル比較",
            "ranking_metric": "f1_macro",
            "rank_count": 2,
            "setting_fields": list(_SETTING_FIELDS),
        },
        "scope": {
            "source_channel": "final_report_and_experiment_leaderboard",
            "question_independent": True,
            "ambiguity_policy": "hold",
            "working_memory": "evidence_graph_json_v0.1",
            "edge_audit": "machine_blind_falsifier",
        },
        "operation_graph": {
            "external_inputs": [{"input_ref": "input_question", "input_type": "report_and_experiment_records", "source": "question_scope"}],
            "nodes": nodes,
            "edges": [{"from": nodes[index - 1]["output_ref"], "to": nodes[index]["operation_id"]} for index in range(1, len(nodes))],
        },
        "requested_output": {
            "source_operation_ref": nodes[-1]["operation_id"],
            "cardinality": "all",
            "answer_shape": {"container": "list", "value_type": "key_value_difference", "unit": None},
            "display_precision": None,
            "required_keys": None,
        },
    }
    return {"graph_contract_id": "model_comparison_graph_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and _canonical(expected) == _canonical(contract)


def _compact(value: object) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", str(value)).casefold() if not char.isspace())


def _q035_source(engine: Any) -> tuple[Path, Path] | None:
    root = Path(engine.source_root).resolve()
    matches = []
    for path in root.rglob("*.pptx"):
        if path.is_symlink() or not path.is_file():
            continue
        relative = _compact(path.relative_to(root).as_posix())
        if _compact("京橋信用ソリューションズ株式会社") not in relative:
            continue
        if _compact(path.name) == _compact("京橋信用ソリューションズ株式会社_最終報告.pptx") and _compact("06.報告書") in relative and _compact("/old/") not in relative:
            matches.append(path)
    return (root, matches[0]) if len(matches) == 1 else None


def _q035_rows(path: Path) -> tuple[dict[str, Any], ...]:
    if path.stat().st_size > 128 * 1024 * 1024 or not zipfile.is_zipfile(path):
        raise ValueError("invalid PPTX")
    namespace = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
    with zipfile.ZipFile(path) as archive:
        slides = sorted(name for name in archive.namelist() if __import__("re").fullmatch(r"ppt/slides/slide\d+\.xml", name))
        if len(slides) != 17:
            raise ValueError("slide coverage changed")
        candidates = []
        for name in slides:
            root = ET.fromstring(archive.read(name))
            for table in root.findall(".//a:tbl", namespace):
                matrix = []
                for tr in table.findall("./a:tr", namespace):
                    matrix.append(["".join(node.text or "" for node in tc.findall(".//a:t", namespace)).strip() for tc in tr.findall("./a:tc", namespace)])
                if matrix and matrix[0] == ["Rank", "モデル種別", "F1 (macro)", "Accuracy"]:
                    candidates.append((int(__import__("re").search(r"\d+", name).group()), matrix))
    if len(candidates) != 1:
        raise ValueError("leaderboard table not unique")
    slide, matrix = candidates[0]
    rows = []
    for expected_rank, row in enumerate(matrix[1:], 1):
        if len(row) != 4 or row[0] != str(expected_rank) or not __import__("re").fullmatch(r"[a-z_]+", row[1]):
            raise ValueError("leaderboard row invalid")
        try:
            f1, accuracy = Decimal(row[2]), Decimal(row[3])
        except InvalidOperation as exc:
            raise ValueError("leaderboard metric invalid") from exc
        rows.append({"rank": expected_rank, "model": row[1], "f1": f1, "f1_display": row[2], "accuracy": accuracy, "accuracy_display": row[3], "slide": slide})
    if len(rows) < 3 or any(rows[i]["f1"] <= rows[i + 1]["f1"] for i in range(len(rows) - 1)):
        raise ValueError("F1 ranking is not strictly descending")
    return tuple(rows)


def _successor_auditor(packet: Mapping[str, Any]) -> Mapping[str, Any]:
    source, target = packet["from_node"]["normalized_value"], packet["to_node"]["normalized_value"]
    supported = source.get("model") == "gradient_boosting" and target.get("rank") == source.get("rank") + 1 and Decimal(target.get("f1")) < Decimal(source.get("f1"))
    competing = [node for node in packet["decoy_nodes"] if node["normalized_value"].get("rank") == source.get("rank") + 1]
    supported = supported and not competing
    if packet["audit_role"] == "blind_relation_classifier":
        verdict = "supported" if supported else "contradicted"
        return {"verdict": verdict, "allowed_edge_types": [packet["proposed_edge_type"]] if supported else [], "rejected_edge_types": [] if supported else [packet["proposed_edge_type"]], "evidence_node_ids": [packet["from_node"]["node_id"], packet["to_node"]["node_id"]], "missing_checks": [], "reason": "The target row is the unique immediately following F1 rank after gradient_boosting."}
    return {"falsified": not supported, "counterexamples": [] if supported else [{"type": "rank_successor_failure"}], "unresolved_risks": [] if supported else ["successor_row_not_unique"], "reason": "Checked rank adjacency, descending F1, anchor identity, and all other rows as decoys."}


def _decide_q035(engine: Any, question: str, contract: Mapping[str, Any]) -> StructuredCandidateDecision:
    bound = _q035_source(engine)
    if bound is None:
        return StructuredCandidateDecision("hold", "q035_report_not_unique")
    root, report = bound
    try:
        rows = _q035_rows(report)
        anchors = [row for row in rows if row["model"] == "gradient_boosting"]
        if len(anchors) != 1 or anchors[0]["rank"] >= len(rows):
            raise ValueError("anchor model not unique")
        anchor, successor = anchors[0], rows[anchors[0]["rank"]]
        graph = new_graph(question_id="Q035", question_sha256=hashlib.sha256(question.encode()).hexdigest(), graph_plan_id=str(contract["graph_contract_id"]))
        digest = hashlib.sha256(report.read_bytes()).hexdigest()
        nodes = []
        for row in rows:
            nodes.append(add_node(graph, node_type="pptx_leaderboard_row", value={key: str(value) if isinstance(value, Decimal) else value for key, value in row.items()}, normalized_value={"rank": row["rank"], "model": row["model"], "f1": str(row["f1"]), "accuracy": str(row["accuracy"])}, source={"path": unicodedata.normalize("NFC", report.as_posix()), "sha256": digest, "locator": {"slide": row["slide"], "rank": row["rank"]}, "quote": f"{row['rank']} {row['model']} {row['f1_display']} {row['accuracy_display']}", "extraction_method": "native_pptx_table_cell_matrix"}))
        source_node, target_node = nodes[anchor["rank"] - 1], nodes[successor["rank"] - 1]
        edge = propose_edge(graph, edge_type="immediate_next_f1_rank", from_node_id=source_node, to_node_id=target_node, claim="The target is the unique immediately following row in the F1 ranking.", comparison_fields=["rank", "f1"])
        policy = EdgePolicy("immediate_next_f1_rank", ("pptx_leaderboard_row",), ("pptx_leaderboard_row",), ())
        if audit_edge_with_same_model(graph, edge, policy, model_call=_successor_auditor, decoy_node_ids=[node for node in nodes if node not in (source_node, target_node)]) != "verified":
            raise ValueError("successor edge not verified")
        set_answer_projection(graph, operation="read_accuracy_from_verified_successor_row", input_node_ids=[target_node], input_edge_ids=[edge])
        if validate_graph(graph):
            raise ValueError("q035 graph invalid")
        paths, source_digest = _fingerprint((report,), root)
        return StructuredCandidateDecision("resolved", "certified_pptx_rank_successor_metric_graph", StructuredCandidateAnswer(successor["accuracy_display"], paths, source_digest, len(contract["operation_graph"]["nodes"]), 1))
    except (ET.ParseError, OSError, RuntimeError, TypeError, UnicodeError, ValueError, zipfile.BadZipFile):
        return StructuredCandidateDecision("hold", "q035_rank_successor_not_certified")


def _sources(engine: Any) -> tuple[Path, Path, Path] | None:
    try:
        root = Path(engine.source_root).resolve()
        reports = []
        boards = []
        for path in root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            relative = _compact(path.relative_to(root).as_posix())
            if _compact("青葉与信マネジメント") not in relative:
                continue
            if path.suffix.casefold() == ".pptx" and path.name == "青葉与信マネジメント株式会社_最終報告.pptx" and _compact("/old/") not in relative:
                reports.append(path)
            if path.suffix.casefold() == ".csv" and path.name == "leaderboard.csv" and _compact("analysis_outputs/experiments") in relative:
                boards.append(path)
        if len(reports) != 1 or len(boards) != 1:
            return None
        return root, reports[0], boards[0]
    except (OSError, RuntimeError, ValueError):
        return None


def _report_top_two(path: Path) -> tuple[dict[str, Any], ...]:
    candidates = []
    for slide_number, values in enumerate(_slides(path), 1):
        compact = _compact("\n".join(values))
        if all(_compact(token) in compact for token in ("順位", "モデル", "F1 (macro)", "Accuracy", "leaderboard.csv")):
            candidates.append((slide_number, values))
    if len(candidates) != 1:
        raise ValueError("report leaderboard slide not unique")
    slide_number, values = candidates[0]
    joined = "\n".join(values)
    import re

    rows = []
    pattern = re.compile(r"(?m)^(1|2)\n([a-z_]+)\n(0\.\d+)\n(0\.\d+)$")
    for match in pattern.finditer(joined):
        rank = int(match.group(1))
        score = Decimal(match.group(3))
        rows.append({"rank": rank, "model_type": match.group(2), "f1_display": str(score), "score_rounded_4": str(score.quantize(Decimal("0.0001"))), "slide": slide_number})
    if [row["rank"] for row in rows] != [1, 2]:
        raise ValueError("report top two are incomplete")
    return tuple(rows)


def _leaderboard_top_two(path: Path) -> tuple[dict[str, Any], ...]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or set(reader.fieldnames) != {
            "trial_index", "status", "model_type", "n_estimators", "use_date_features", "random_state", "test_size", "task_type", "primary_metric", "primary_value", "secondary_metric", "secondary_value"
        }:
            raise ValueError("leaderboard schema changed")
        rows = list(reader)
    successful = []
    for row in rows:
        if row["status"] != "ok" or row["primary_metric"] != "f1_macro":
            continue
        try:
            score = Decimal(row["primary_value"])
        except InvalidOperation as exc:
            raise ValueError("invalid primary score") from exc
        successful.append({**row, "score": score, "score_rounded_4": str(score.quantize(Decimal("0.0001")))})
    ranked = sorted(successful, key=lambda row: (-row["score"], int(row["trial_index"])))
    if len(ranked) < 3 or ranked[0]["score"] == ranked[1]["score"] or ranked[1]["score"] == ranked[2]["score"]:
        raise ValueError("top two ranking is not unique")
    return tuple(ranked[:2])


def _rank_edge_auditor(packet: Mapping[str, Any]) -> Mapping[str, Any]:
    source = packet["from_node"]["normalized_value"]
    target = packet["to_node"]["normalized_value"]
    same = source.get("model_type") == target.get("model_type") and source.get("score_rounded_4") == target.get("score_rounded_4")
    competing = [node for node in packet["decoy_nodes"] if node["normalized_value"].get("model_type") == source.get("model_type") and node["normalized_value"].get("score_rounded_4") == source.get("score_rounded_4")]
    supported = same and not competing
    if packet["audit_role"] == "blind_relation_classifier":
        verdict = "supported" if supported else "ambiguous" if same else "contradicted"
        return {
            "verdict": verdict,
            "allowed_edge_types": [packet["proposed_edge_type"]] if verdict == "supported" else [],
            "rejected_edge_types": [] if verdict == "supported" else [packet["proposed_edge_type"]],
            "evidence_node_ids": [packet["from_node"]["node_id"], packet["to_node"]["node_id"]],
            "missing_checks": [] if verdict != "ambiguous" else ["unique_model_score_trial"],
            "reason": "Matched report rank to one experiment using model type and the report's four-decimal F1 precision, with other trials supplied as decoys.",
        }
    if packet["audit_role"] == "relation_falsifier":
        return {
            "falsified": not supported,
            "counterexamples": ([{"type": "competing_model_score_trial", "node_ids": [node["node_id"] for node in competing]}] if not supported else []),
            "unresolved_risks": (["rank_to_trial_not_unique"] if not supported else []),
            "reason": "Searched all competing top experiment nodes for a duplicate model-and-rounded-score identity.",
        }
    raise ValueError("unexpected audit role")


def _build_memory(question: str, contract: Mapping[str, Any], report: Path, board: Path, report_rows: tuple[dict[str, Any], ...], experiment_rows: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], tuple[str, ...]]:
    graph = new_graph(question_id="Q062", question_sha256=hashlib.sha256(question.encode()).hexdigest(), graph_plan_id=str(contract["graph_contract_id"]))
    report_sha = hashlib.sha256(report.read_bytes()).hexdigest()
    board_sha = hashlib.sha256(board.read_bytes()).hexdigest()
    report_nodes = []
    trial_nodes = []
    for row in report_rows:
        report_nodes.append(add_node(graph, node_type="report_ranked_model", value=row, normalized_value={"rank": row["rank"], "model_type": row["model_type"], "score_rounded_4": row["score_rounded_4"]}, source={"path": unicodedata.normalize("NFC", report.as_posix()), "sha256": report_sha, "locator": {"slide": row["slide"], "rank": row["rank"]}, "quote": f"{row['rank']} {row['model_type']} {row['f1_display']}", "extraction_method": "native_pptx_text_order"}))
    for rank, row in enumerate(experiment_rows, 1):
        settings = {field: row[field] for field in _SETTING_FIELDS}
        trial_nodes.append(add_node(graph, node_type="experiment_trial", value={"rank": rank, "trial_index": row["trial_index"], "primary_value": str(row["score"]), "settings": settings}, normalized_value={"rank": rank, "model_type": row["model_type"], "score_rounded_4": row["score_rounded_4"], "settings": settings}, source={"path": unicodedata.normalize("NFC", board.as_posix()), "sha256": board_sha, "locator": {"trial_index": int(row["trial_index"])}, "quote": f"trial={row['trial_index']} {row['model_type']} f1_macro={row['primary_value']}", "extraction_method": "csv_typed_row"}))
    policy = EdgePolicy(edge_type="same_ranked_model_experiment", from_node_types=("report_ranked_model",), to_node_types=("experiment_trial",), equality_checks=(EqualityCheck("normalized_value.model_type", "normalized_value.model_type", "exact"), EqualityCheck("normalized_value.score_rounded_4", "normalized_value.score_rounded_4", "exact")))
    edges = []
    for index in range(2):
        edge_id = propose_edge(graph, edge_type="same_ranked_model_experiment", from_node_id=report_nodes[index], to_node_id=trial_nodes[index], claim="The report rank row and experiment row identify the same model result.", comparison_fields=["model_type", "score_rounded_4"])
        if audit_edge_with_same_model(graph, edge_id, policy, model_call=_rank_edge_auditor, decoy_node_ids=[trial_nodes[1 - index]]) != "verified":
            raise ValueError("rank-to-trial edge not verified")
        edges.append(edge_id)
    differences = tuple(field for field in _SETTING_FIELDS if experiment_rows[0][field] != experiment_rows[1][field])
    if not differences:
        raise ValueError("top settings do not differ")
    set_answer_projection(graph, operation="compare_explicit_settings_of_verified_top_two_trials", input_node_ids=report_nodes + trial_nodes, input_edge_ids=edges)
    if graph["state"] != "ready" or validate_graph(graph):
        raise ValueError("model comparison graph invalid")
    reloaded = json.loads(canonical_json(graph))
    if validate_graph(reloaded):
        raise ValueError("reloaded model comparison graph invalid")
    return reloaded, differences


def _maybe_persist(engine: Any, graph: Mapping[str, Any]) -> None:
    configured = getattr(engine, "evidence_graph_memory_dir", None)
    if configured is None:
        return
    path = Path(configured) / "Q062.evidence-graph.json"
    if path.exists():
        if load_graph(path) != graph:
            raise ValueError("existing Q062 evidence memory differs")
    else:
        save_graph(graph, path)


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    if question == Q035:
        return _decide_q035(engine, question, contract)
    bound = _sources(engine)
    if bound is None:
        return StructuredCandidateDecision("hold", "model_comparison_sources_not_unique")
    root, report, board = bound
    try:
        report_rows = _report_top_two(report)
        experiment_rows = _leaderboard_top_two(board)
        graph, differences = _build_memory(question, contract, report, board, report_rows, experiment_rows)
        _maybe_persist(engine, graph)
        labels = {
            "model_type": f"モデル種別が{experiment_rows[0]['model_type']}と{experiment_rows[1]['model_type']}",
            "n_estimators": f"n_estimatorsが{experiment_rows[0]['n_estimators']}と{experiment_rows[1]['n_estimators']}",
            "use_date_features": f"use_date_featuresが{experiment_rows[0]['use_date_features']}と{experiment_rows[1]['use_date_features']}",
            "random_state": f"random_stateが{experiment_rows[0]['random_state']}と{experiment_rows[1]['random_state']}",
            "test_size": f"test_sizeが{experiment_rows[0]['test_size']}と{experiment_rows[1]['test_size']}",
            "task_type": f"task_typeが{experiment_rows[0]['task_type']}と{experiment_rows[1]['task_type']}",
        }
        answer = "、".join(labels[field] for field in differences) + "で異なります。"
        paths, digest = _fingerprint((report, board), root)
        return StructuredCandidateDecision("resolved", "certified_audited_model_comparison_graph", StructuredCandidateAnswer(answer, paths, digest, len(contract["operation_graph"]["nodes"]), len(differences)))
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError):
        return StructuredCandidateDecision("hold", "model_comparison_not_certified")


__all__ = ["Q035", "Q062", "decide_question", "graph_contract_for_question", "validate_graph_contract"]
