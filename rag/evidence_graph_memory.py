"""Persistent, source-bound working memory for multi-step evidence graphs."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "0.1"
BUILDER_VERSION = "0.1.0"
MAX_GRAPH_BYTES = 32 * 1024 * 1024
MAX_NODES = 100_000
MAX_EDGES = 200_000


class EvidenceGraphError(ValueError):
    """Raised when graph memory is malformed, stale, or internally inconsistent."""


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise EvidenceGraphError(f"value is not canonical JSON: {exc}") from exc


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _stable_id(prefix: str, payload: Any) -> str:
    return f"{prefix}_{sha256_json(payload)[:32]}"


def _without_integrity(graph: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(dict(graph))
    value.pop("integrity_sha256", None)
    return value


def refresh_integrity(graph: dict[str, Any]) -> dict[str, Any]:
    graph["integrity_sha256"] = sha256_json(_without_integrity(graph))
    return graph


def new_graph(
    *, question_id: str, question_sha256: str, graph_plan_id: str
) -> dict[str, Any]:
    if not question_id or not graph_plan_id:
        raise EvidenceGraphError("question_id and graph_plan_id must be non-empty")
    if len(question_sha256) != 64:
        raise EvidenceGraphError("question_sha256 must be a SHA-256 digest")
    identity = {
        "schema_version": SCHEMA_VERSION,
        "question_id": question_id,
        "question_sha256": question_sha256,
        "graph_plan_id": graph_plan_id,
    }
    graph = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "evidence_graph_memory",
        "graph_id": _stable_id("egm", identity),
        "question_id": question_id,
        "question_sha256": question_sha256,
        "graph_plan_id": graph_plan_id,
        "state": "building",
        "nodes": [],
        "edges": [],
        "unresolved": [],
        "answer_projection": None,
        "provenance": {
            "builder": "evidence-graph-memory",
            "builder_version": BUILDER_VERSION,
            "question_independent_evidence": True,
            "gold_used": False,
            "public_score_used": False,
        },
        "integrity_sha256": "0" * 64,
    }
    return refresh_integrity(graph)


def add_node(
    graph: dict[str, Any],
    *,
    node_type: str,
    value: Any,
    normalized_value: Any,
    source: Mapping[str, Any],
    status: str = "observed",
) -> str:
    core = {
        "node_type": node_type,
        "value": copy.deepcopy(value),
        "normalized_value": copy.deepcopy(normalized_value),
        "status": status,
        "source": copy.deepcopy(dict(source)),
    }
    node_id = _stable_id("egn", core)
    node = {"node_id": node_id, **core, "content_sha256": sha256_json(core)}
    if any(item.get("node_id") == node_id for item in graph.get("nodes", [])):
        raise EvidenceGraphError(f"duplicate node: {node_id}")
    graph.setdefault("nodes", []).append(node)
    refresh_integrity(graph)
    return node_id


def propose_edge(
    graph: dict[str, Any],
    *,
    edge_type: str,
    from_node_id: str,
    to_node_id: str,
    claim: str,
    comparison_fields: Sequence[str],
    evidence_node_ids: Sequence[str] = (),
) -> str:
    core = {
        "edge_type": edge_type,
        "from_node_id": from_node_id,
        "to_node_id": to_node_id,
        "basis": {
            "claim": claim,
            "comparison_fields": list(comparison_fields),
            "evidence_node_ids": list(evidence_node_ids),
        },
    }
    edge_id = _stable_id("ege", core)
    edge = {
        "edge_id": edge_id,
        **core,
        "status": "proposed",
        "audit": None,
        "content_sha256": sha256_json(core),
    }
    if any(item.get("edge_id") == edge_id for item in graph.get("edges", [])):
        raise EvidenceGraphError(f"duplicate edge: {edge_id}")
    graph.setdefault("edges", []).append(edge)
    graph["state"] = "auditing"
    refresh_integrity(graph)
    return edge_id


def add_unresolved(
    graph: dict[str, Any], *, kind: str, description: str, required_checks: Sequence[str]
) -> str:
    core = {
        "kind": kind,
        "description": description,
        "required_checks": list(required_checks),
    }
    unresolved_id = _stable_id("egu", core)
    record = {"unresolved_id": unresolved_id, **core, "status": "open"}
    if any(item.get("unresolved_id") == unresolved_id for item in graph.get("unresolved", [])):
        raise EvidenceGraphError(f"duplicate unresolved item: {unresolved_id}")
    graph.setdefault("unresolved", []).append(record)
    refresh_integrity(graph)
    return unresolved_id


def set_answer_projection(
    graph: dict[str, Any],
    *,
    operation: str,
    input_node_ids: Sequence[str],
    input_edge_ids: Sequence[str],
) -> None:
    edge_status = {edge["edge_id"]: edge["status"] for edge in graph.get("edges", [])}
    node_ids = {node["node_id"] for node in graph.get("nodes", [])}
    verified = (
        all(node_id in node_ids for node_id in input_node_ids)
        and all(edge_status.get(edge_id) == "verified" for edge_id in input_edge_ids)
        and not any(item.get("status") == "open" for item in graph.get("unresolved", []))
    )
    graph["answer_projection"] = {
        "operation": operation,
        "input_node_ids": list(input_node_ids),
        "input_edge_ids": list(input_edge_ids),
        "status": "verified" if verified else "blocked",
    }
    graph["state"] = "ready" if verified else "blocked"
    refresh_integrity(graph)


def _schema_errors(graph: Mapping[str, Any], schema_path: Path) -> list[str]:
    try:
        import jsonschema
    except ImportError as exc:  # pragma: no cover - production dependency guard
        raise EvidenceGraphError("jsonschema is required to validate graph memory") from exc
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(validator.iter_errors(graph), key=lambda error: list(error.absolute_path))
    ]


def validate_graph(
    graph: Mapping[str, Any], *, schema_path: Path | None = None
) -> list[str]:
    schema_path = schema_path or Path(__file__).resolve().parents[1] / "schemas" / "evidence-graph-memory.schema.json"
    errors = _schema_errors(graph, schema_path)
    if errors:
        return errors
    if len(graph["nodes"]) > MAX_NODES or len(graph["edges"]) > MAX_EDGES:
        errors.append("graph size limit exceeded")
    if graph["integrity_sha256"] != sha256_json(_without_integrity(graph)):
        errors.append("graph integrity SHA-256 mismatch")
    node_by_id = {node["node_id"]: node for node in graph["nodes"]}
    if len(node_by_id) != len(graph["nodes"]):
        errors.append("duplicate node_id")
    edge_by_id = {edge["edge_id"]: edge for edge in graph["edges"]}
    if len(edge_by_id) != len(graph["edges"]):
        errors.append("duplicate edge_id")
    for node in graph["nodes"]:
        core = {key: value for key, value in node.items() if key not in {"node_id", "content_sha256"}}
        if node["node_id"] != _stable_id("egn", core):
            errors.append(f"node ID mismatch: {node['node_id']}")
        if node["content_sha256"] != sha256_json(core):
            errors.append(f"node content hash mismatch: {node['node_id']}")
    for edge in graph["edges"]:
        core = {key: edge[key] for key in ("edge_type", "from_node_id", "to_node_id", "basis")}
        if edge["edge_id"] != _stable_id("ege", core):
            errors.append(f"edge ID mismatch: {edge['edge_id']}")
        if edge["content_sha256"] != sha256_json(core):
            errors.append(f"edge content hash mismatch: {edge['edge_id']}")
        refs = [edge["from_node_id"], edge["to_node_id"], *edge["basis"]["evidence_node_ids"]]
        if any(ref not in node_by_id for ref in refs):
            errors.append(f"edge has unknown node reference: {edge['edge_id']}")
        audit = edge.get("audit")
        if audit is not None:
            core_audit = {key: value for key, value in audit.items() if key != "audit_sha256"}
            if audit["audit_sha256"] != sha256_json(core_audit):
                errors.append(f"edge audit hash mismatch: {edge['edge_id']}")
            if audit["final_status"] != edge["status"]:
                errors.append(f"edge status/audit mismatch: {edge['edge_id']}")
            machine = audit.get("machine", {})
            blind = audit.get("blind", {})
            falsifier = audit.get("falsifier", {})
            expected_machine_keys = {"status", "policy", "policy_sha256", "checks"}
            expected_blind_keys = {
                "verdict", "allowed_edge_types", "rejected_edge_types",
                "evidence_node_ids", "missing_checks", "reason",
                "packet_sha256", "response_sha256",
            }
            expected_falsifier_keys = {
                "falsified", "counterexamples", "unresolved_risks", "reason",
                "packet_sha256", "response_sha256",
            }
            if set(machine) != expected_machine_keys:
                errors.append(f"edge machine audit shape mismatch: {edge['edge_id']}")
                continue
            if set(blind) != expected_blind_keys:
                errors.append(f"edge blind audit shape mismatch: {edge['edge_id']}")
                continue
            if set(falsifier) != expected_falsifier_keys:
                errors.append(f"edge falsifier shape mismatch: {edge['edge_id']}")
                continue
            if machine["policy_sha256"] != sha256_json(machine["policy"]):
                errors.append(f"edge audit policy hash mismatch: {edge['edge_id']}")
            blind_response = {key: blind[key] for key in expected_blind_keys - {"packet_sha256", "response_sha256"}}
            falsifier_response = {
                key: falsifier[key]
                for key in expected_falsifier_keys - {"packet_sha256", "response_sha256"}
            }
            if blind["response_sha256"] != sha256_json(blind_response):
                errors.append(f"edge blind response hash mismatch: {edge['edge_id']}")
            if falsifier["response_sha256"] != sha256_json(falsifier_response):
                errors.append(f"edge falsifier response hash mismatch: {edge['edge_id']}")
            try:
                from evidence_edge_audit import (
                    EdgePolicy,
                    EqualityCheck,
                    _final_status,
                    blind_audit_packet,
                    falsifier_packet,
                )

                policy_value = machine["policy"]
                policy = EdgePolicy(
                    edge_type=policy_value["edge_type"],
                    from_node_types=tuple(policy_value["from_node_types"]),
                    to_node_types=tuple(policy_value["to_node_types"]),
                    equality_checks=tuple(
                        EqualityCheck(**check) for check in policy_value["equality_checks"]
                    ),
                    require_observed_nodes=policy_value["require_observed_nodes"],
                    blind_required=policy_value["blind_required"],
                    falsifier_required=policy_value["falsifier_required"],
                )
                decoys = audit["decoy_node_ids"]
                expected_blind_packet = blind_audit_packet(
                    graph, edge["edge_id"], policy, decoy_node_ids=decoys
                )
                expected_falsifier_packet = falsifier_packet(
                    graph, edge["edge_id"], policy, decoy_node_ids=decoys
                )
                if blind["packet_sha256"] != expected_blind_packet["packet_sha256"]:
                    errors.append(f"edge blind packet hash mismatch: {edge['edge_id']}")
                if falsifier["packet_sha256"] != expected_falsifier_packet["packet_sha256"]:
                    errors.append(f"edge falsifier packet hash mismatch: {edge['edge_id']}")
                if audit["final_status"] != _final_status(machine, blind, falsifier, policy):
                    errors.append(f"edge final audit decision mismatch: {edge['edge_id']}")
            except (KeyError, TypeError, EvidenceGraphError) as exc:
                errors.append(f"edge audit reconstruction failed: {edge['edge_id']}: {exc}")
    projection = graph.get("answer_projection")
    if projection is not None:
        if any(ref not in node_by_id for ref in projection["input_node_ids"]):
            errors.append("answer projection has unknown node reference")
        if any(ref not in edge_by_id for ref in projection["input_edge_ids"]):
            errors.append("answer projection has unknown edge reference")
        if projection["status"] == "verified" and any(
            edge_by_id[ref]["status"] != "verified" for ref in projection["input_edge_ids"]
        ):
            errors.append("verified answer projection uses unverified edge")
    return errors


def save_graph(graph: Mapping[str, Any], path: Path) -> None:
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise EvidenceGraphError(f"refusing to overwrite graph memory: {path}")
    errors = validate_graph(graph)
    if errors:
        raise EvidenceGraphError("invalid graph memory: " + "; ".join(errors))
    payload = (canonical_json(graph) + "\n").encode("utf-8")
    if len(payload) > MAX_GRAPH_BYTES:
        raise EvidenceGraphError("graph memory exceeds byte limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temp_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def load_graph(path: Path) -> dict[str, Any]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise EvidenceGraphError(f"graph memory is not a regular file: {path}")
    if path.stat().st_size > MAX_GRAPH_BYTES:
        raise EvidenceGraphError("graph memory exceeds byte limit")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceGraphError(f"invalid graph JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise EvidenceGraphError("graph memory root must be an object")
    errors = validate_graph(value)
    if errors:
        raise EvidenceGraphError("invalid graph memory: " + "; ".join(errors))
    return value


__all__ = [
    "EvidenceGraphError", "add_node", "add_unresolved", "canonical_json",
    "load_graph", "new_graph", "propose_edge", "refresh_integrity",
    "save_graph", "set_answer_projection", "sha256_json", "validate_graph",
]
