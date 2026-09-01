#!/usr/bin/env python3
"""Build and deterministically validate a small claim graph before LLM audit."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation


CURRENT_MARKERS = ("現在", "今は", "いまは", "現在は")
PAST_MARKERS = ("過去", "以前", "かつて", "住んでいました", "住んでいた", "当時")
ALL_MARKERS = ("すべて", "全て", "全部", "全件", "漏れなく", "すべて挙げ")
PERSON_MARKERS = ("人物", "本人", "パイロット", "質問者", "担当者", "誰")
NAME_MARKERS = ("名前", "氏名", "パイロットネーム", "氏名は", "名前は")
PROVISIONAL_MARKER = "[暫定読取]"
COUNT_MARKERS = ("何回", "回数", "何枠", "枠数", "何件", "件数", "総数", "合計")
EXPLICIT_PERIOD = re.compile(r"20\d{2}\s*年\s*(?:1[0-2]|0?[1-9])\s*月")


def normalize(value: object) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKC", str(value)).casefold()
        if char.isalnum() or "ぁ" <= char <= "龥"
    )


def stable_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def infer_time_scope(query: str, label: str) -> str:
    combined = query + " " + label
    if EXPLICIT_PERIOD.search(unicodedata.normalize("NFKC", combined)):
        return "specified_period"
    if any(marker in combined for marker in ("過去", "以前", "かつて", "住んでいた")):
        return "past"
    if any(marker in combined for marker in CURRENT_MARKERS):
        return "current"
    return "unspecified"


def infer_entity_type(query: str, label: str) -> str:
    combined = query + " " + label
    if any(marker in combined for marker in COUNT_MARKERS):
        return "numeric_count"
    if "氏名" in combined or (
        any(marker in combined for marker in NAME_MARKERS)
        and any(marker in combined for marker in PERSON_MARKERS)
    ):
        return "person_name"
    if "場所" in combined or "どこ" in combined or "居住地" in combined:
        return "place"
    if "誰" in combined or "同居者" in combined:
        return "person_or_relation"
    return "text_value"


def build_question_contract(query: str, plan: dict) -> dict:
    items = []
    for item in plan.get("items", []):
        label = str(item.get("label", ""))
        items.append({
            "field_id": str(item.get("item_id", "")),
            "label": label,
            "required_claim": str(item.get("required_claim", "")),
            "required": bool(item.get("required", False)),
            "entity_type": infer_entity_type(query, label),
            "time_scope": infer_time_scope(query, label),
            "require_all": any(marker in query for marker in ALL_MARKERS),
        })
    body = {"query": query, "items": items}
    return {"contract_version": "1.0", "contract_hash": stable_hash(body), **body}


def split_values(value: str) -> list[str]:
    parts = [part.strip(" \t-・") for part in re.split(r"[、,，;/／\n]+", value) if part.strip(" \t-・")]
    return parts or ([value.strip()] if value.strip() else [])


def non_provisional_text(value: str) -> str:
    """Return only text that is not labelled as a provisional reading.

    A direct provisional Evidence packet starts with the marker and is wholly
    provisional. A derived image packet can contain multiple OCR lines; in
    that form only each marker-prefixed line is provisional, while independently
    accepted lines in the same packet remain eligible support.
    """
    lines = value.splitlines() or [value]
    first_nonempty = next((line for line in lines if line.strip()), "")
    if first_nonempty.lstrip().startswith(PROVISIONAL_MARKER):
        return ""
    accepted = []
    for line in lines:
        prefix, marker, _provisional = line.partition(PROVISIONAL_MARKER)
        accepted.append(prefix if marker else line)
    return "\n".join(accepted)


def packet_has_provisional_reading(value: str) -> bool:
    """Return whether a packet contains any explicitly provisional OCR line.

    SearchUnits keep high and provisional image readings in separate packets.
    A supported claim therefore never needs a provisional packet in its support
    set. Keeping those packets diagnostic also prevents relation laundering.
    """
    return any(
        line.lstrip().startswith(PROVISIONAL_MARKER)
        for line in value.splitlines()
    )


def numeric_value(value: object) -> Decimal | None:
    match = re.search(r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)", unicodedata.normalize("NFKC", str(value)))
    if not match:
        return None
    try:
        parsed = Decimal(match.group(0))
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def validate_question_graph_binding(
    record: dict,
    contract: dict,
    graph: dict,
    packet_map: dict[str, str],
    failures: list[dict],
    warnings: list[dict],
) -> None:
    """Bind numeric count claims to the verified pre-answer aggregate graph."""
    count_fields = {
        item.get("field_id") for item in contract.get("items", [])
        if item.get("entity_type") == "numeric_count"
    }
    if not count_fields:
        return
    artifact = record.get("question_evidence_graph")
    validation = record.get("question_evidence_graph_validation")
    if not isinstance(artifact, dict) or not isinstance(validation, dict):
        failures.append({
            "code": "question_graph_missing",
            "detail": "回数主張に必要な回答前Question Evidence Graphがありません。",
        })
        return
    if artifact.get("status") == "unsupported" and validation.get("status") == "not_applicable":
        failures.append({
            "code": "structured_aggregate_graph_required",
            "detail": "回数主張に必要な構造化集計Graphがありません。",
        })
        return
    if artifact.get("status") != "ready" or validation.get("status") != "pass":
        failures.append({
            "code": "question_graph_not_verified",
            "detail": f"集計Graph未検証: {artifact.get('reason', '')}",
        })
        return
    body = {key: value for key, value in artifact.items() if key not in {"artifact_hash", "artifact_id"}}
    expected_hash = stable_hash(body)
    if artifact.get("artifact_hash") != expected_hash or artifact.get("artifact_id") != f"qeg_{expected_hash[:24]}":
        failures.append({"code": "question_graph_hash_mismatch", "detail": "集計Graphのhashが一致しません。"})
        return
    selection = artifact.get("selection") or {}
    validation_ids = set(selection.get("validation_evidence_ids", []))
    missing_ids = sorted(validation_ids - set(packet_map))
    if missing_ids:
        failures.append({
            "code": "question_graph_evidence_missing",
            "detail": f"集計GraphのEvidenceが不足: {missing_ids[:6]}",
        })
    expected_value = numeric_value(selection.get("value"))
    mandatory_ids = set(selection.get("mandatory_aggregation_evidence_ids", []))
    for claim in graph.get("claims", []):
        if claim.get("field_id") not in count_fields:
            continue
        claim_id = claim.get("claim_id", "")
        actual_value = numeric_value(claim.get("value"))
        if expected_value is None or actual_value != expected_value:
            failures.append({
                "code": "question_graph_value_mismatch",
                "claim_id": claim_id,
                "detail": "回数主張が機械的に再集計した値と一致しません。",
            })
        if not (set(claim.get("evidence_ids", [])) & mandatory_ids):
            failures.append({
                "code": "question_graph_evidence_escape",
                "claim_id": claim_id,
                "detail": "回数主張が検証済みの合計Evidenceを参照していません。",
            })


def build_claim_graph(record: dict, packets: list[dict], contract: dict | None = None) -> dict:
    contract = contract or build_question_contract(record.get("query", ""), record.get("question_plan", {}))
    packet_ids = {str(packet.get("evidence_id", "")) for packet in packets}
    nodes = [{"node_id": "Q1", "node_type": "question", "value": record.get("query", "")}]
    edges = []
    claims = []
    item_by_id = {item["field_id"]: item for item in contract["items"]}
    for item in contract["items"]:
        nodes.append({"node_id": item["field_id"], "node_type": "field", "value": item["required_claim"]})
        edges.append({"edge_id": f"R_{item['field_id']}", "source": "Q1", "predicate": "requires", "target": item["field_id"]})
    for packet in packets:
        evidence_id = str(packet.get("evidence_id", ""))
        nodes.append({"node_id": evidence_id, "node_type": "evidence", "value": packet.get("text", "")})
    for row in record.get("field_runs", []):
        audit = row.get("audit", {})
        if audit.get("verdict") != "supported":
            continue
        field_id = str(audit.get("item_id", ""))
        value = str(audit.get("supported_value", "")).strip()
        evidence_ids = list(dict.fromkeys(str(value) for value in audit.get("supporting_packet_ids", [])))
        claim_id = f"C{len(claims) + 1}"
        value_id = f"V{len(claims) + 1}"
        contract_item = item_by_id.get(field_id, {})
        claim = {
            "claim_id": claim_id,
            "field_id": field_id,
            "predicate": contract_item.get("label", ""),
            "value": value,
            "value_parts": split_values(value),
            "entity_type": contract_item.get("entity_type", "text_value"),
            "time_scope": contract_item.get("time_scope", "unspecified"),
            "evidence_ids": evidence_ids,
        }
        claims.append(claim)
        nodes.append({
            "node_id": claim_id,
            "node_type": "claim",
            "field_id": field_id,
            "predicate": claim["predicate"],
        })
        nodes.append({"node_id": value_id, "node_type": "value", "value": value})
        edges.append({
            "edge_id": f"A_{claim_id}", "source": field_id,
            "predicate": "answered_by", "target": claim_id,
        })
        edges.append({
            "edge_id": f"V_{claim_id}", "source": claim_id,
            "predicate": "has_value", "target": value_id,
        })
        for evidence_id in evidence_ids:
            if evidence_id in packet_ids:
                edges.append({"edge_id": f"S_{claim_id}_{evidence_id}", "source": evidence_id, "predicate": "supports", "target": claim_id})
    body = {"contract_hash": contract["contract_hash"], "nodes": nodes, "edges": edges, "claims": claims}
    return {"artifact_version": "1.1", "artifact_hash": stable_hash(body), **body}


def _name_relation_is_explicit(value: str, text: str) -> bool:
    value_key = normalize(value)
    if not value_key:
        return False
    text_key = normalize(text)
    position = text_key.find(value_key)
    if position < 0:
        return False
    prefix = text_key[max(0, position - 60):position]
    return any(normalize(marker) in prefix for marker in NAME_MARKERS)


def _time_relation_conflicts(time_scope: str, value: str, text: str) -> bool:
    if time_scope == "unspecified":
        return False
    value_key = normalize(value)
    text_key = normalize(text)
    position = text_key.find(value_key)
    if position < 0:
        return False
    before = text_key[:position]
    if time_scope == "past":
        last_current = max((before.rfind(normalize(marker)) for marker in CURRENT_MARKERS), default=-1)
        last_past = max((before.rfind(normalize(marker)) for marker in PAST_MARKERS), default=-1)
        return last_current > last_past
    if time_scope == "current":
        window = text_key[max(0, position - 80):position + len(value_key) + 80]
        return any(normalize(marker) in window for marker in PAST_MARKERS) and not any(
            normalize(marker) in window for marker in CURRENT_MARKERS
        )
    return False


def validate_claim_graph(record: dict, packets: list[dict], contract: dict, graph: dict) -> dict:
    failures = []
    warnings = []
    packet_map = {str(packet.get("evidence_id", "")): str(packet.get("text", "")) for packet in packets}
    expected_contract = build_question_contract(contract.get("query", ""), {"items": [
        {
            "item_id": item.get("field_id", ""), "label": item.get("label", ""),
            "required_claim": item.get("required_claim", ""), "required": item.get("required", False),
        }
        for item in contract.get("items", [])
    ]})
    if contract.get("contract_hash") != expected_contract.get("contract_hash"):
        failures.append({"code": "contract_hash_mismatch", "detail": "質問契約が作成後に変更されています。"})
    graph_body = {key: graph.get(key) for key in ("contract_hash", "nodes", "edges", "claims")}
    if graph.get("artifact_hash") != stable_hash(graph_body):
        failures.append({"code": "artifact_hash_mismatch", "detail": "主張グラフが作成後に変更されています。"})
    if graph.get("contract_hash") != contract.get("contract_hash"):
        failures.append({"code": "artifact_contract_mismatch", "detail": "主張グラフが別の質問契約を参照しています。"})

    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    node_ids = [node.get("node_id") for node in nodes if isinstance(node, dict)]
    if len(node_ids) != len(set(node_ids)) or any(not node_id for node_id in node_ids):
        failures.append({"code": "graph_node_ids_invalid", "detail": "Node IDが空または重複しています。"})
    known_nodes = set(node_ids)
    edge_ids = [edge.get("edge_id") for edge in edges if isinstance(edge, dict)]
    if len(edge_ids) != len(set(edge_ids)) or any(not edge_id for edge_id in edge_ids):
        failures.append({"code": "graph_edge_ids_invalid", "detail": "Edge IDが空または重複しています。"})
    for edge in edges:
        if not isinstance(edge, dict):
            failures.append({"code": "graph_edge_invalid", "detail": "Edgeがobjectではありません。"})
            continue
        if edge.get("source") not in known_nodes or edge.get("target") not in known_nodes:
            failures.append({
                "code": "graph_edge_endpoint_missing",
                "detail": f"Edge端点がありません: {edge.get('edge_id', '')}",
            })
    claim_node_ids = {
        node.get("node_id") for node in nodes
        if isinstance(node, dict) and node.get("node_type") == "claim"
    }
    missing_claim_nodes = [
        claim.get("claim_id") for claim in graph.get("claims", [])
        if claim.get("claim_id") not in claim_node_ids
    ]
    if missing_claim_nodes:
        failures.append({
            "code": "claim_node_missing",
            "detail": f"主張を表すNodeがありません: {missing_claim_nodes}",
        })

    field_ids = {item.get("field_id") for item in contract.get("items", [])}
    answer_text = normalize(record.get("answer", {}).get("answer", ""))
    for claim in graph.get("claims", []):
        claim_id = claim.get("claim_id", "")
        if claim.get("field_id") not in field_ids:
            failures.append({"code": "unknown_field_id", "claim_id": claim_id, "detail": "質問契約にない項目です。"})
        evidence_ids = claim.get("evidence_ids", [])
        missing_ids = [evidence_id for evidence_id in evidence_ids if evidence_id not in packet_map]
        if not evidence_ids or missing_ids:
            failures.append({"code": "invalid_evidence_reference", "claim_id": claim_id, "detail": f"不明なEvidence ID: {missing_ids}"})
            continue
        provisional_support_ids = [
            evidence_id for evidence_id in evidence_ids
            if packet_has_provisional_reading(packet_map[evidence_id])
        ]
        if provisional_support_ids:
            failures.append({
                "code": "provisional_evidence_only",
                "claim_id": claim_id,
                "detail": (
                    "Provisional OCR packets cannot be used as confirmed support; "
                    f"keep them diagnostic: {provisional_support_ids}"
                ),
            })
        cited_text = "\n".join(packet_map[evidence_id] for evidence_id in evidence_ids)
        non_provisional_cited_text = "\n".join(
            non_provisional_text(packet_map[evidence_id]) for evidence_id in evidence_ids
        )
        for value_part in claim.get("value_parts", []):
            if normalize(value_part) not in normalize(cited_text):
                failures.append({"code": "value_not_in_evidence", "claim_id": claim_id, "detail": f"原文にない値: {value_part}"})
            if _time_relation_conflicts(
                str(claim.get("time_scope", "unspecified")),
                value_part,
                non_provisional_cited_text,
            ):
                failures.append({"code": "time_scope_conflict", "claim_id": claim_id, "detail": f"質問の時制と一致しない値: {value_part}"})
        if normalize(claim.get("value", "")) not in answer_text:
            failures.append({"code": "value_not_in_answer", "claim_id": claim_id, "detail": "検証済み値が最終回答へ投影されていません。"})
        if claim.get("entity_type") == "person_name" and not _name_relation_is_explicit(
            str(claim.get("value", "")), non_provisional_cited_text
        ):
            failures.append({"code": "person_name_relation_missing", "claim_id": claim_id, "detail": "人物名と値を結ぶ明示的な記述がありません。"})

    supported_fields = {claim.get("field_id") for claim in graph.get("claims", [])}
    answer = record.get("answer", {})
    if answer.get("answer_mode") == "grounded":
        missing_required = [
            item.get("field_id") for item in contract.get("items", [])
            if item.get("required") and item.get("field_id") not in supported_fields
        ]
        if missing_required:
            failures.append({"code": "required_field_missing", "detail": f"必須項目が未充足です: {missing_required}"})
    for item in contract.get("items", []):
        if item.get("require_all") and item.get("field_id") in supported_fields:
            warnings.append({"code": "coverage_requires_semantic_audit", "field_id": item.get("field_id"), "detail": "全件性は独立監査役がEvidenceの列挙範囲を再確認します。"})

    validate_question_graph_binding(record, contract, graph, packet_map, failures, warnings)

    status = "blocked" if failures else "pass"
    return {
        "validator_version": "1.3",
        "status": status,
        "failures": failures,
        "warnings": warnings,
        "checked_claim_ids": [claim.get("claim_id") for claim in graph.get("claims", [])],
    }


def build_and_validate(record: dict, packets: list[dict]) -> tuple[dict, dict, dict]:
    contract = build_question_contract(record.get("query", ""), record.get("question_plan", {}))
    graph = build_claim_graph(record, packets, contract)
    report = validate_claim_graph(record, packets, contract, graph)
    return contract, graph, report
