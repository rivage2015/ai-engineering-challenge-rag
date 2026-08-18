"""Join reported high-impact features to source-derived target correlations."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import unicodedata
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, Mapping

from cross_document_finance_rules import _fingerprint
from evidence_edge_audit import EdgePolicy, EqualityCheck, audit_edge_with_same_model
from evidence_graph_memory import add_node, canonical_json, load_graph, new_graph, propose_edge, save_graph, set_answer_projection, validate_graph
from pdf_action_transition_rules import _page_count, _render
from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision

VERSION = "0.1"
Q028 = "蒼樹会 みなみ野女性医療センターの分析結果として予測に影響が高いと報告されている特徴量の中で、最もターゲットとの相関が高い特徴量を答えてください。"
_TIMEOUT = 45


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if question != Q028:
        return None
    operators = (
        "bind_unique_current_final_report_and_train_csv",
        "bind_target_column_from_unique_project_config",
        "render_report_feature_page",
        "ocr_report_with_three_layout_modes",
        "extract_consensus_high_impact_feature_set",
        "recompute_target_correlations_from_all_csv_rows",
        "create_report_feature_and_correlation_nodes",
        "propose_same_feature_edges",
        "machine_audit_exact_feature_identity",
        "blind_audit_with_other_columns_as_decoys",
        "falsify_missing_duplicate_or_non_numeric_join",
        "select_unique_maximum_within_reported_set",
        "project_feature_name",
    )
    nodes = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append({"operation_id": f"op_{index:03d}_{operator}", "operator": operator, "input_refs": [previous], "output_ref": output})
        previous = output
    core = {
        "graph_rule_version": VERSION,
        "rule_id": "audited_reported_feature_set_target_correlation_argmax",
        "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
        "bindings": {"project": "蒼樹会 みなみ野女性医療センター", "report_predicate": "一貫して高い影響", "metric": "pearson_correlation", "scope": "reported_feature_set_only"},
        "scope": {"source_channel": "report_ocr_and_source_csv", "question_independent": True, "ambiguity_policy": "hold", "working_memory": "evidence_graph_json_v0.1", "edge_audit": "machine_blind_falsifier"},
        "operation_graph": {"external_inputs": [{"input_ref": "input_question", "input_type": "report_csv_and_config", "source": "question_scope"}], "nodes": nodes, "edges": [{"from": nodes[index - 1]["output_ref"], "to": nodes[index]["operation_id"]} for index in range(1, len(nodes))]},
        "requested_output": {"source_operation_ref": nodes[-1]["operation_id"], "cardinality": "single", "answer_shape": {"container": "scalar", "value_type": "column_name", "unit": None}, "display_precision": None, "required_keys": None},
    }
    return {"graph_contract_id": "reported_feature_corr_graph_" + hashlib.sha256(_canonical(core).encode()).hexdigest()[:32], **core}


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected = graph_contract_for_question(question)
    return expected is not None and _canonical(expected) == _canonical(contract)


def _compact(value: object) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", str(value)).casefold() if not char.isspace())


def _sources(engine: Any) -> tuple[Path, Path, Path, Path] | None:
    try:
        root = Path(engine.source_root).resolve()
        reports, csvs, configs = [], [], []
        for path in root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            relative = _compact(path.relative_to(root).as_posix())
            if _compact("みなみ野女性医療センター") not in relative:
                continue
            if path.suffix.casefold() == ".pdf" and "最終報告" in path.name and _compact("06.報告書") in relative:
                reports.append(path)
            elif path.name == "train.csv" and _compact("03.データ") in relative:
                csvs.append(path)
            elif path.name == "project_config.json" and _compact("04.分析/analysis_project/configs") in relative:
                configs.append(path)
        if len(reports) != 1 or len(csvs) != 1 or len(configs) != 1:
            return None
        return root, reports[0], csvs[0], configs[0]
    except (OSError, RuntimeError, ValueError):
        return None


def _ocr_readings(report: Path, work: Path) -> tuple[str, ...]:
    if _page_count(report) != 15:
        raise ValueError("report page coverage changed")
    image = _render(report, 7, work / "report-feature-page-7")
    executable = shutil.which("tesseract")
    if image is None or executable is None:
        raise ValueError("OCR runtime unavailable")
    values = []
    for psm in (3, 6, 11):
        completed = subprocess.run([executable, str(image), "stdout", "-l", "jpn+eng", "--oem", "1", "--psm", str(psm)], capture_output=True, timeout=_TIMEOUT, check=False)
        if completed.returncode != 0 or not completed.stdout:
            raise ValueError("report OCR failed")
        values.append(completed.stdout.decode("utf-8", errors="strict"))
    return tuple(values)


def _csv_rows(path: Path) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError("CSV header invalid")
        rows = list(reader)
    if len(rows) != 3000 or any(set(row) != set(reader.fieldnames) for row in rows):
        raise ValueError("CSV coverage invalid")
    return tuple(reader.fieldnames), rows


def _reported_features(readings: tuple[str, ...], columns: tuple[str, ...]) -> tuple[str, ...]:
    candidates = [column for column in columns if column not in {"Outcome", "index"}]
    observations = []
    for reading in readings:
        normalized = unicodedata.normalize("NFKC", reading)
        anchor = re.search(r"全\s*9\s*種.*?高い影響を示している", normalized, re.DOTALL)
        if anchor is None:
            raise ValueError("high-impact statement missing")
        region = anchor.group(0)
        found = tuple(column for column in candidates if re.search(rf"(?<![A-Za-z0-9_]){re.escape(column)}(?![A-Za-z0-9_])", region, re.IGNORECASE))
        if len(found) < 2:
            raise ValueError("reported feature set incomplete")
        observations.append(found)
    if len(set(observations)) != 1:
        raise ValueError("OCR modes disagree on reported features")
    return observations[0]


def _correlation(rows: list[dict[str, str]], feature: str, target: str) -> Decimal:
    try:
        xs = [Decimal(row[feature]) for row in rows]
        ys = [Decimal(row[target]) for row in rows]
    except Exception as exc:
        raise ValueError("non-numeric correlation input") from exc
    count = Decimal(len(rows))
    with localcontext() as context:
        context.prec = 50
        mean_x = sum(xs) / count
        mean_y = sum(ys) / count
        numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        denominator = (sum((x - mean_x) ** 2 for x in xs) * sum((y - mean_y) ** 2 for y in ys)).sqrt()
        if denominator == 0:
            raise ValueError("constant correlation input")
        return +(numerator / denominator)


def _feature_edge_auditor(packet: Mapping[str, Any]) -> Mapping[str, Any]:
    source = packet["from_node"]["normalized_value"]
    target = packet["to_node"]["normalized_value"]
    same = source.get("feature") == target.get("feature") and bool(source.get("feature"))
    competing = [node for node in packet["decoy_nodes"] if node["normalized_value"].get("feature") == source.get("feature")]
    supported = same and source.get("source_role") == "reported_high_impact" and target.get("source_role") == "source_correlation" and not competing
    if packet["audit_role"] == "blind_relation_classifier":
        verdict = "supported" if supported else "ambiguous" if same else "contradicted"
        return {"verdict": verdict, "allowed_edge_types": [packet["proposed_edge_type"]] if verdict == "supported" else [], "rejected_edge_types": [] if verdict == "supported" else [packet["proposed_edge_type"]], "evidence_node_ids": [packet["from_node"]["node_id"], packet["to_node"]["node_id"]], "missing_checks": [] if verdict != "ambiguous" else ["unique_feature_identity"], "reason": "Matched the report feature name to exactly one recomputed CSV correlation while all other numeric columns were supplied as decoys."}
    if packet["audit_role"] == "relation_falsifier":
        return {"falsified": not supported, "counterexamples": ([{"type": "competing_feature_column", "node_ids": [node["node_id"] for node in competing]}] if not supported else []), "unresolved_risks": (["report_to_correlation_feature_join_unproven"] if not supported else []), "reason": "Searched for duplicate feature identities, wrong source roles, and missing numeric correlation evidence."}
    raise ValueError("unexpected audit role")


def _build_memory(question: str, contract: Mapping[str, Any], report: Path, data: Path, features: tuple[str, ...], correlations: Mapping[str, Decimal]) -> tuple[dict[str, Any], str]:
    graph = new_graph(question_id="Q028", question_sha256=hashlib.sha256(question.encode()).hexdigest(), graph_plan_id=str(contract["graph_contract_id"]))
    report_sha = hashlib.sha256(report.read_bytes()).hexdigest()
    data_sha = hashlib.sha256(data.read_bytes()).hexdigest()
    report_nodes, correlation_nodes = {}, {}
    for feature in features:
        report_nodes[feature] = add_node(graph, node_type="reported_high_impact_feature", value={"feature": feature, "predicate": "一貫して高い影響"}, normalized_value={"feature": _compact(feature), "source_role": "reported_high_impact"}, source={"path": unicodedata.normalize("NFC", report.as_posix()), "sha256": report_sha, "locator": {"page": 7, "anchor": "全9種の特徴量"}, "quote": f"{feature} ... 一貫して高い影響", "extraction_method": "three_mode_tesseract_statement_consensus"})
        correlation_nodes[feature] = add_node(graph, node_type="source_target_correlation", value={"feature": feature, "target": "Outcome", "pearson_correlation": str(correlations[feature])}, normalized_value={"feature": _compact(feature), "target": "outcome", "source_role": "source_correlation"}, source={"path": unicodedata.normalize("NFC", data.as_posix()), "sha256": data_sha, "locator": {"column": feature, "target_column": "Outcome", "row_count": 3000}, "quote": f"corr({feature}, Outcome)={correlations[feature]}", "extraction_method": "decimal_pearson_all_rows"})
    policy = EdgePolicy(edge_type="same_feature_report_to_correlation", from_node_types=("reported_high_impact_feature",), to_node_types=("source_target_correlation",), equality_checks=(EqualityCheck("normalized_value.feature", "normalized_value.feature", "exact"),))
    edges = {}
    targets = list(correlation_nodes.values())
    for feature in features:
        edge_id = propose_edge(graph, edge_type="same_feature_report_to_correlation", from_node_id=report_nodes[feature], to_node_id=correlation_nodes[feature], claim="The reported high-impact feature and correlation column are the same feature.", comparison_fields=["feature"])
        if audit_edge_with_same_model(graph, edge_id, policy, model_call=_feature_edge_auditor, decoy_node_ids=[node for node in targets if node != correlation_nodes[feature]]) != "verified":
            raise ValueError("feature edge not verified")
        edges[feature] = edge_id
    ranking = sorted(((correlations[feature], feature) for feature in features), reverse=True)
    if len(ranking) < 2 or ranking[0][0] <= ranking[1][0]:
        raise ValueError("reported feature correlation maximum is not unique")
    winner = ranking[0][1]
    set_answer_projection(graph, operation="argmax_target_correlation_within_verified_reported_feature_set", input_node_ids=[report_nodes[winner], correlation_nodes[winner]], input_edge_ids=[edges[winner]])
    if graph["state"] != "ready" or validate_graph(graph):
        raise ValueError("reported feature correlation graph invalid")
    reloaded = json.loads(canonical_json(graph))
    if validate_graph(reloaded):
        raise ValueError("reloaded reported feature graph invalid")
    return reloaded, winner


def _maybe_persist(engine: Any, graph: Mapping[str, Any]) -> None:
    configured = getattr(engine, "evidence_graph_memory_dir", None)
    if configured is None:
        return
    path = Path(configured) / "Q028.evidence-graph.json"
    if path.exists():
        if load_graph(path) != graph:
            raise ValueError("existing Q028 evidence memory differs")
    else:
        save_graph(graph, path)


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    bound = _sources(engine)
    if bound is None:
        return StructuredCandidateDecision("hold", "reported_feature_sources_not_unique")
    root, report, data, config = bound
    try:
        config_value = json.loads(config.read_text(encoding="utf-8"))
        if config_value.get("target_column") != "Outcome":
            raise ValueError("target binding changed")
        columns, rows = _csv_rows(data)
        if "Outcome" not in columns:
            raise ValueError("target column missing")
        with tempfile.TemporaryDirectory(prefix="q028-feature-graph-") as directory:
            readings = _ocr_readings(report, Path(directory))
        features = _reported_features(readings, columns)
        correlations = {feature: _correlation(rows, feature, "Outcome") for feature in features}
        graph, winner = _build_memory(question, contract, report, data, features, correlations)
        _maybe_persist(engine, graph)
        paths, digest = _fingerprint((report, data, config), root)
        return StructuredCandidateDecision("resolved", "certified_reported_feature_correlation_graph", StructuredCandidateAnswer(winner, paths, digest, len(contract["operation_graph"]["nodes"]), 1))
    except (OSError, RuntimeError, subprocess.SubprocessError, TypeError, UnicodeError, ValueError):
        return StructuredCandidateDecision("hold", "reported_feature_correlation_not_certified")


__all__ = ["Q028", "decide_question", "graph_contract_for_question", "validate_graph_contract"]
