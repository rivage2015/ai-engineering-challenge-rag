#!/usr/bin/env python3
"""Build and deterministically validate a small claim graph before LLM audit."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata


CURRENT_MARKERS = ("現在", "今は", "いまは", "現在は")
PAST_MARKERS = ("過去", "以前", "かつて", "住んでいました", "住んでいた", "当時")
ALL_MARKERS = ("すべて", "全て", "全部", "全件", "漏れなく", "すべて挙げ")
PERSON_MARKERS = ("人物", "本人", "パイロット", "質問者", "担当者", "誰")
NAME_MARKERS = ("名前", "氏名", "パイロットネーム", "氏名は", "名前は")


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
    if any(marker in combined for marker in ("過去", "以前", "かつて", "住んでいた")):
        return "past"
    if any(marker in combined for marker in ("現在", "今", "いま")):
        return "current"
    return "unspecified"


def infer_entity_type(query: str, label: str) -> str:
    combined = query + " " + label
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
        nodes.append({"node_id": value_id, "node_type": "value", "value": value})
        edges.append({"edge_id": f"A_{claim_id}", "source": field_id, "predicate": "answered_by", "target": value_id})
        for evidence_id in evidence_ids:
            if evidence_id in packet_ids:
                edges.append({"edge_id": f"S_{claim_id}_{evidence_id}", "source": evidence_id, "predicate": "supports", "target": claim_id})
    body = {"contract_hash": contract["contract_hash"], "nodes": nodes, "edges": edges, "claims": claims}
    return {"artifact_version": "1.0", "artifact_hash": stable_hash(body), **body}


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
        cited_text = "\n".join(packet_map[evidence_id] for evidence_id in evidence_ids)
        for value_part in claim.get("value_parts", []):
            if normalize(value_part) not in normalize(cited_text):
                failures.append({"code": "value_not_in_evidence", "claim_id": claim_id, "detail": f"原文にない値: {value_part}"})
            if _time_relation_conflicts(str(claim.get("time_scope", "unspecified")), value_part, cited_text):
                failures.append({"code": "time_scope_conflict", "claim_id": claim_id, "detail": f"質問の時制と一致しない値: {value_part}"})
        if normalize(claim.get("value", "")) not in answer_text:
            failures.append({"code": "value_not_in_answer", "claim_id": claim_id, "detail": "検証済み値が最終回答へ投影されていません。"})
        if claim.get("entity_type") == "person_name" and not _name_relation_is_explicit(str(claim.get("value", "")), cited_text):
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

    status = "blocked" if failures else "pass"
    return {
        "validator_version": "1.0",
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
