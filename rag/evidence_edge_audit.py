"""Separated machine, blind, and falsification audits for evidence edges."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from evidence_graph_memory import EvidenceGraphError, refresh_integrity, sha256_json

BlindAuditor = Callable[[Mapping[str, Any]], Mapping[str, Any]]
Falsifier = Callable[[Mapping[str, Any]], Mapping[str, Any]]
ModelCall = Callable[[Mapping[str, Any]], Mapping[str, Any]]

BLIND_KEYS = {
    "verdict",
    "allowed_edge_types",
    "rejected_edge_types",
    "evidence_node_ids",
    "missing_checks",
    "reason",
}
FALSIFIER_KEYS = {"falsified", "counterexamples", "unresolved_risks", "reason"}
BLIND_VERDICTS = {"supported", "contradicted", "ambiguous", "insufficient"}
FINAL_STATUSES = {"verified", "ambiguous", "contradicted", "insufficient_evidence"}


@dataclass(frozen=True)
class EqualityCheck:
    from_path: str
    to_path: str
    normalizer: str = "exact"


@dataclass(frozen=True)
class EdgePolicy:
    edge_type: str
    from_node_types: tuple[str, ...]
    to_node_types: tuple[str, ...]
    equality_checks: tuple[EqualityCheck, ...] = ()
    require_observed_nodes: bool = True
    blind_required: bool = True
    falsifier_required: bool = True


def _path(value: Mapping[str, Any], dotted_path: str) -> Any:
    current: Any = value
    for part in dotted_path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            raise EvidenceGraphError(f"audit field is missing: {dotted_path}")
        current = current[part]
    return current


def _normalize(value: Any, mode: str) -> Any:
    if mode == "exact":
        return value
    if mode == "nfc_trim":
        import unicodedata

        return unicodedata.normalize("NFC", str(value)).strip()
    if mode == "nfc_compact":
        import unicodedata

        return "".join(
            char for char in unicodedata.normalize("NFC", str(value)) if not char.isspace()
        )
    raise EvidenceGraphError(f"unsupported audit normalizer: {mode}")


def _node_view(node: Mapping[str, Any]) -> dict[str, Any]:
    """Return evidence only; deliberately omit graph question and builder rationale."""

    return {
        "node_id": node["node_id"],
        "node_type": node["node_type"],
        "value": copy.deepcopy(node["value"]),
        "normalized_value": copy.deepcopy(node["normalized_value"]),
        "status": node["status"],
        "source": copy.deepcopy(node["source"]),
        "content_sha256": node["content_sha256"],
    }


def _edge_and_nodes(graph: Mapping[str, Any], edge_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    matches = [edge for edge in graph.get("edges", []) if edge.get("edge_id") == edge_id]
    if len(matches) != 1:
        raise EvidenceGraphError(f"edge must exist exactly once: {edge_id}")
    edge = matches[0]
    nodes = {node.get("node_id"): node for node in graph.get("nodes", [])}
    try:
        return edge, nodes[edge["from_node_id"]], nodes[edge["to_node_id"]]
    except KeyError as exc:
        raise EvidenceGraphError(f"edge endpoint is missing: {edge_id}") from exc


def machine_audit(
    graph: Mapping[str, Any], edge_id: str, policy: EdgePolicy
) -> dict[str, Any]:
    edge, source, target = _edge_and_nodes(graph, edge_id)
    checks: list[dict[str, Any]] = []

    def record(check: str, passed: bool, detail: str) -> None:
        checks.append({"check": check, "passed": passed, "detail": detail})

    record("edge_type_allowed", edge["edge_type"] == policy.edge_type, edge["edge_type"])
    record("distinct_endpoints", edge["from_node_id"] != edge["to_node_id"], "node IDs differ")
    record("from_node_type_allowed", source["node_type"] in policy.from_node_types, source["node_type"])
    record("to_node_type_allowed", target["node_type"] in policy.to_node_types, target["node_type"])
    if policy.require_observed_nodes:
        record(
            "both_nodes_observed",
            source["status"] == target["status"] == "observed",
            f"{source['status']}/{target['status']}",
        )
    for index, check in enumerate(policy.equality_checks):
        try:
            left = _normalize(_path(source, check.from_path), check.normalizer)
            right = _normalize(_path(target, check.to_path), check.normalizer)
            passed = left == right and left not in (None, "", [], {})
            detail = f"left_sha256={sha256_json(left)} right_sha256={sha256_json(right)}"
        except EvidenceGraphError as exc:
            passed = False
            detail = str(exc)
        record(f"equality_{index + 1}", passed, detail)
    policy_payload = {
        "edge_type": policy.edge_type,
        "from_node_types": list(policy.from_node_types),
        "to_node_types": list(policy.to_node_types),
        "equality_checks": [check.__dict__ for check in policy.equality_checks],
        "require_observed_nodes": policy.require_observed_nodes,
        "blind_required": policy.blind_required,
        "falsifier_required": policy.falsifier_required,
    }
    return {
        "status": "passed" if all(item["passed"] for item in checks) else "failed",
        "policy": policy_payload,
        "policy_sha256": sha256_json(policy_payload),
        "checks": checks,
    }


def blind_audit_packet(
    graph: Mapping[str, Any],
    edge_id: str,
    policy: EdgePolicy,
    *,
    decoy_node_ids: Sequence[str] = (),
) -> dict[str, Any]:
    edge, source, target = _edge_and_nodes(graph, edge_id)
    nodes = {node["node_id"]: node for node in graph["nodes"]}
    decoys = []
    for node_id in decoy_node_ids:
        if node_id in {source["node_id"], target["node_id"]}:
            raise EvidenceGraphError("an endpoint cannot also be a decoy")
        if node_id not in nodes:
            raise EvidenceGraphError(f"unknown decoy node: {node_id}")
        decoys.append(_node_view(nodes[node_id]))
    packet = {
        "audit_role": "blind_relation_classifier",
        "instruction": (
            "Classify only relations supported by the supplied evidence. "
            "Do not assume the proposed relation is correct; use ambiguous or insufficient when needed."
        ),
        "edge_id": edge_id,
        "proposed_edge_type": edge["edge_type"],
        "allowed_edge_types": [policy.edge_type],
        "from_node": _node_view(source),
        "to_node": _node_view(target),
        "decoy_nodes": decoys,
    }
    packet["packet_sha256"] = sha256_json(packet)
    return packet


def falsifier_packet(
    graph: Mapping[str, Any],
    edge_id: str,
    policy: EdgePolicy,
    *,
    decoy_node_ids: Sequence[str] = (),
) -> dict[str, Any]:
    blind = blind_audit_packet(graph, edge_id, policy, decoy_node_ids=decoy_node_ids)
    packet = {
        "audit_role": "relation_falsifier",
        "instruction": (
            "Assume the proposed relation may be wrong. Search for identity, scope, time, "
            "row, column, unit, or competing-candidate evidence that falsifies it."
        ),
        "edge_id": blind["edge_id"],
        "proposed_edge_type": blind["proposed_edge_type"],
        "from_node": blind["from_node"],
        "to_node": blind["to_node"],
        "decoy_nodes": blind["decoy_nodes"],
    }
    packet["packet_sha256"] = sha256_json(packet)
    return packet


def _validate_blind_response(response: Mapping[str, Any], packet: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(response, Mapping) or set(response) != BLIND_KEYS:
        raise EvidenceGraphError("blind audit response has an invalid shape")
    value = copy.deepcopy(dict(response))
    if value["verdict"] not in BLIND_VERDICTS:
        raise EvidenceGraphError("blind audit verdict is invalid")
    for key in ("allowed_edge_types", "rejected_edge_types", "evidence_node_ids", "missing_checks"):
        if not isinstance(value[key], list) or any(not isinstance(item, str) for item in value[key]):
            raise EvidenceGraphError(f"blind audit {key} must be a string array")
    if not isinstance(value["reason"], str) or not value["reason"].strip():
        raise EvidenceGraphError("blind audit reason must be non-empty")
    supplied_nodes = {
        packet["from_node"]["node_id"],
        packet["to_node"]["node_id"],
        *(node["node_id"] for node in packet["decoy_nodes"]),
    }
    if any(node_id not in supplied_nodes for node_id in value["evidence_node_ids"]):
        raise EvidenceGraphError("blind audit cites evidence outside its packet")
    value["packet_sha256"] = packet["packet_sha256"]
    value["response_sha256"] = sha256_json(response)
    return value


def _validate_falsifier_response(response: Mapping[str, Any], packet: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(response, Mapping) or set(response) != FALSIFIER_KEYS:
        raise EvidenceGraphError("falsifier response has an invalid shape")
    value = copy.deepcopy(dict(response))
    if not isinstance(value["falsified"], bool):
        raise EvidenceGraphError("falsifier falsified must be boolean")
    if not isinstance(value["counterexamples"], list) or any(
        not isinstance(item, Mapping) for item in value["counterexamples"]
    ):
        raise EvidenceGraphError("falsifier counterexamples must be an object array")
    if not isinstance(value["unresolved_risks"], list) or any(
        not isinstance(item, str) for item in value["unresolved_risks"]
    ):
        raise EvidenceGraphError("falsifier unresolved_risks must be a string array")
    if not isinstance(value["reason"], str) or not value["reason"].strip():
        raise EvidenceGraphError("falsifier reason must be non-empty")
    value["packet_sha256"] = packet["packet_sha256"]
    value["response_sha256"] = sha256_json(response)
    return value


def _final_status(
    machine: Mapping[str, Any], blind: Mapping[str, Any], falsifier: Mapping[str, Any], policy: EdgePolicy
) -> str:
    if machine["status"] != "passed":
        return "contradicted"
    if falsifier.get("falsified"):
        return "contradicted"
    verdict = blind.get("verdict")
    if verdict == "contradicted":
        return "contradicted"
    if verdict == "ambiguous" or falsifier.get("unresolved_risks"):
        return "ambiguous"
    if verdict == "insufficient" or blind.get("missing_checks"):
        return "insufficient_evidence"
    supported = (
        verdict == "supported"
        and policy.edge_type in blind.get("allowed_edge_types", [])
        and policy.edge_type not in blind.get("rejected_edge_types", [])
    )
    return "verified" if supported else "insufficient_evidence"


def audit_edge(
    graph: dict[str, Any],
    edge_id: str,
    policy: EdgePolicy,
    *,
    blind_auditor: BlindAuditor,
    falsifier: Falsifier,
    decoy_node_ids: Sequence[str] = (),
) -> str:
    """Audit one edge with isolated calls and persist a hash-bound audit trail."""

    from evidence_graph_memory import validate_graph

    graph_errors = validate_graph(graph)
    if graph_errors:
        raise EvidenceGraphError("cannot audit invalid graph: " + "; ".join(graph_errors))
    edge, _, _ = _edge_and_nodes(graph, edge_id)
    if edge.get("audit") is not None or edge.get("status") != "proposed":
        raise EvidenceGraphError("only an unaudited proposed edge can be audited")
    machine = machine_audit(graph, edge_id, policy)
    blind_packet = blind_audit_packet(graph, edge_id, policy, decoy_node_ids=decoy_node_ids)
    falsify_packet = falsifier_packet(graph, edge_id, policy, decoy_node_ids=decoy_node_ids)

    # Separate invocations are deliberate. Neither receives builder basis, question,
    # answer projection, the other auditor's response, or the final decision rule.
    blind = _validate_blind_response(blind_auditor(copy.deepcopy(blind_packet)), blind_packet)
    falsification = _validate_falsifier_response(falsifier(copy.deepcopy(falsify_packet)), falsify_packet)
    final_status = _final_status(machine, blind, falsification, policy)
    if final_status not in FINAL_STATUSES:  # defensive invariant
        raise EvidenceGraphError("edge audit produced an invalid final status")
    audit_core = {
        "audit_version": "0.1",
        "separation_mode": "same_model_separate_context",
        "decoy_node_ids": list(decoy_node_ids),
        "machine": machine,
        "blind": blind,
        "falsifier": falsification,
        "final_status": final_status,
    }
    edge["audit"] = {**audit_core, "audit_sha256": sha256_json(audit_core)}
    edge["status"] = final_status
    refresh_integrity(graph)
    return final_status


def audit_edge_with_same_model(
    graph: dict[str, Any],
    edge_id: str,
    policy: EdgePolicy,
    *,
    model_call: ModelCall,
    decoy_node_ids: Sequence[str] = (),
) -> str:
    """Use one model through two deliberately isolated role packets.

    This is procedural separation, not statistical model independence.  The
    callback is invoked twice and receives neither the other response nor the
    builder rationale.
    """

    return audit_edge(
        graph,
        edge_id,
        policy,
        blind_auditor=lambda packet: model_call(copy.deepcopy(packet)),
        falsifier=lambda packet: model_call(copy.deepcopy(packet)),
        decoy_node_ids=decoy_node_ids,
    )


__all__ = [
    "EdgePolicy", "EqualityCheck", "audit_edge", "audit_edge_with_same_model",
    "blind_audit_packet", "falsifier_packet", "machine_audit",
]
